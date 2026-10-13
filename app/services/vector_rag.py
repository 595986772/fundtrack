"""真实 embedding + Chroma 向量数据库版 RAG。

这个文件是你前面 RAG 学习的工程化版本：

文档 -> Node/chunk -> 真实 embedding -> Chroma 持久化向量库
用户问题 -> embedding -> Chroma 相似度检索 -> DeepSeek 基于命中 chunk 回答

和前两个版本的关系：
- rag.py：手写关键词学习版；
- llamaindex_rag.py：LlamaIndex 对象学习版；
- vector_rag.py：真实 embedding + 持久化向量库版。
"""

from pathlib import Path
from typing import Any
import os
import re

from app.config import settings
from app.schemas import (
    RagQueryRequest,
    VectorRagHit,
    VectorRagQueryResponse,
    VectorRagRebuildResponse,
    VectorRagSearchResponse,
    VectorRagStatsResponse,
)
from app.services.errors import AgentConfigError, AgentRuntimeError
from app.services.llm import get_chat_model
from app.services.llamaindex_rag import FRAMEWORK_REQUIRED_EXTS, _load_llama_index_classes

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = PROJECT_ROOT / "data" / "docs"


class DashScopeEmbeddingClient:
    """通过 OpenAI 兼容接口调用阿里云百炼 / Qwen embedding。"""

    # 百炼 text-embedding 接口限制一次 input 不能超过 10 条。
    # 重建知识库时 chunk 数经常大于 10，所以这里统一分批请求。
    max_batch_size = 10

    def __init__(self, *, timeout: float | None = None, max_retries: int = 2, governed: bool = False) -> None:
        if not settings.dashscope_api_key:
            raise AgentConfigError("请先在 .env 中设置 DASHSCOPE_API_KEY")

        try:
            from openai import OpenAI
        except ModuleNotFoundError as exc:
            raise AgentConfigError("请先安装依赖：pip install -r requirements.txt") from exc

        self._governed, self._timeout = governed, timeout or 45
        self._client = OpenAI(
            api_key=settings.dashscope_api_key,
            base_url=settings.embedding_base_url,
            **({"timeout": timeout} if timeout is not None else {}),
            max_retries=0 if governed else max_retries,
        )

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """把多段文本转换成向量。 把一位数组中的字符串即token词元转化为二维矩阵向量 一行代表一个token的向量

        Chroma 保存的不是 token，而是这里返回的向量数组。
        """

        if not texts:
            return []

        embeddings: list[list[float]] = [] #矩阵向量

        for start in range(0, len(texts), self.max_batch_size):
            batch = texts[start : start + self.max_batch_size] #因为embedding有传入限制 所以有maxsize
            from app.services.ai_governance_context import AIControlError
            try:
                response = self._embed_batch(batch)
            except AIControlError:
                raise
            except Exception as exc:
                raise AgentRuntimeError(f"调用 embedding 模型失败：{exc}") from exc

            # OpenAI 兼容响应通常按 input 顺序返回。这里按 index 再排一次，
            # 防止某些 provider 返回顺序不稳定。
            ordered_items = sorted(
                response.data,
                key=lambda item: getattr(item, "index", 0),
            )
            embeddings.extend(item.embedding for item in ordered_items)

        return embeddings

    def _embed_batch(self, batch):
        request = dict(model=settings.embedding_model, input=batch, dimensions=settings.embedding_dimensions)
        if not self._governed:
            return self._client.embeddings.create(**request)
        # 定时任务的向量请求也占用用户额度；SDK 不暗中重试，每次物理请求单独记账。
        from app.services.governed_model import model_permit
        with model_permit(provider=settings.embedding_provider, model=settings.embedding_model,
                          payload={**request, "max_tokens": 0}, timeout=self._timeout) as permit:
            response = self._client.embeddings.create(**request)
            usage = getattr(response, "usage", None)
            prompt, total = getattr(usage, "prompt_tokens", None), getattr(usage, "total_tokens", None)
            if type(prompt) is int and type(total) is int and prompt >= 0 and total == prompt:
                permit.usage = {"input_tokens": prompt, "output_tokens": 0, "total_tokens": total}
            # 无 usage 或超时保持 None，由账本标记待确认，不当成免费调用。
            return response

    def embed_query(self, query: str) -> list[float]:
        """把用户问题转换成向量，供 Chroma 做相似度检索。"""

        return self.embed_texts([query])[0]


class VectorRagService:
    """真实向量库 RAG service。"""

    def __init__(self) -> None:
        self._collection: Any | None = None

    def rebuild(self) -> VectorRagRebuildResponse:
        """重新加载文档、生成 embedding，并写入 Chroma。"""

        documents, nodes = self._load_nodes()
        embedding_client = DashScopeEmbeddingClient()
        collection = self._reset_collection()

        ids = [str(node.node_id) for node in nodes]
        texts = [node.text for node in nodes]
        metadatas = [self._node_metadata(node) for node in nodes]

        if ids:
            embeddings = embedding_client.embed_texts(texts)
            collection.add(
                ids=ids,
                embeddings=embeddings,
                documents=texts,
                metadatas=metadatas,
            )

        self._collection = collection
        sample = None

        if ids:
            sample = VectorRagHit(
                chunk_id=ids[0],
                document_id=metadatas[0].get("document_id"),
                source=str(metadatas[0].get("source_path") or ""),
                text=texts[0],
                metadata=metadatas[0],
            )

        return VectorRagRebuildResponse(
            document_count=len(documents),
            chunk_count=len(nodes),
            collection_name=settings.chroma_collection,
            persist_dir=self._persist_dir().as_posix(),
            embedding_provider=settings.embedding_provider,
            embedding_model=settings.embedding_model,
            embedding_dimensions=settings.embedding_dimensions,
            sample_chunk=sample,
            pipeline_steps=self._pipeline_steps(),
            message="真实 embedding 已生成，chunk 向量已写入 Chroma 本地数据库",
        )

    def stats(self) -> VectorRagStatsResponse:
        """查看 Chroma collection 当前状态。"""

        collection = self._get_collection()

        return VectorRagStatsResponse(
            collection_name=settings.chroma_collection,
            persist_dir=self._persist_dir().as_posix(),
            chunk_count=collection.count(),
            embedding_provider=settings.embedding_provider,
            embedding_model=settings.embedding_model,
            embedding_dimensions=settings.embedding_dimensions,
            is_ready=collection.count() > 0,
        )

    def search(self, request: RagQueryRequest) -> VectorRagSearchResponse:
        """只做向量检索，不调用 DeepSeek。"""

        hits = self._search_hits(question=request.message, top_k=request.top_k)

        return VectorRagSearchResponse(
            question=request.message,
            top_k=request.top_k,
            hits=hits,
            embedding_model=settings.embedding_model,
            pipeline_steps=[
                "Embed user question",
                "Search Chroma collection by vector similarity",
                "Return matched chunks with metadata",
            ],
        )

    async def query(self, request: RagQueryRequest) -> VectorRagQueryResponse:
        """向量检索 + DeepSeek 生成最终回答。"""

        hits = self._search_hits(question=request.message, top_k=request.top_k)
        context = self._format_context(hits)
        chain = self._build_answer_chain()

        try:
            answer = await chain.ainvoke(
                {
                    "question": request.message,
                    "context": context,
                }
            )
        except AgentConfigError:
            raise
        except Exception as exc:
            raise AgentRuntimeError(f"真实向量库 RAG 调用大模型失败：{exc}") from exc

        return VectorRagQueryResponse(
            user_id=request.user_id,
            question=request.message,
            answer=answer,
            hits=hits,
            used_model=settings.deepseek_model,
            embedding_model=settings.embedding_model,
            pipeline_steps=self._pipeline_steps()
            + [
                "Embed user question",
                "Retrieve top_k chunks from Chroma",
                "Build grounded prompt",
                "DeepSeek generates final answer",
            ],
        )

    def _load_nodes(self) -> tuple[list[Any], list[Any]]:
        """用 LlamaIndex 读取文档并切成 Node。"""

        classes = _load_llama_index_classes()
        SimpleDirectoryReader = classes["SimpleDirectoryReader"]
        MarkdownNodeParser = classes["MarkdownNodeParser"]
        SentenceSplitter = classes["SentenceSplitter"]

        documents = SimpleDirectoryReader(
            input_dir=str(DOCS_DIR),
            recursive=True,
            required_exts=FRAMEWORK_REQUIRED_EXTS,
            filename_as_id=True,
        ).load_data()

        prepared_documents = [self._prepare_document(document) for document in documents]
        markdown_docs = [
            document
            for document in prepared_documents
            if str(document.metadata.get("source_path", "")).endswith(".md")
        ]
        other_docs = [
            document
            for document in prepared_documents
            if not str(document.metadata.get("source_path", "")).endswith(".md")
        ]

        nodes: list[Any] = []

        if markdown_docs:
            markdown_nodes = MarkdownNodeParser().get_nodes_from_documents(markdown_docs)
            nodes.extend(
                SentenceSplitter(chunk_size=512, chunk_overlap=64).get_nodes_from_documents(
                    markdown_nodes
                )
            )

        if other_docs:
            nodes.extend(
                SentenceSplitter(chunk_size=512, chunk_overlap=64).get_nodes_from_documents(
                    other_docs
                )
            )

        self._assign_stable_node_ids(nodes)
        return prepared_documents, nodes

    def _prepare_document(self, document: Any) -> Any:
        metadata = self._jsonable_metadata(document.metadata)
        absolute_path = str(metadata.get("file_path") or document.id_)
        source = self._relative_source(absolute_path)
        file_name = str(metadata.get("file_name") or Path(source).name)

        metadata.update(
            {
                "file_path": source,
                "source_path": source,
                "file_name": file_name,
                "knowledge_type": "agent-learning",
                "loader": "SimpleDirectoryReader",
            }
        )

        document.id_ = source
        document.metadata = metadata
        return document

    def _assign_stable_node_ids(self, nodes: list[Any]) -> None:
        groups: dict[str, list[Any]] = {}

        for node in nodes:
            source = self._source_of_node(node)
            groups.setdefault(source, []).append(node)

        for source, source_nodes in groups.items():
            slug = self._source_slug(source)

            for index, node in enumerate(source_nodes, start=1):
                node.id_ = f"{slug}-vector-{index:03d}"
                node.metadata = self._jsonable_metadata(node.metadata)
                node.metadata["node_index"] = index
                node.metadata["source_path"] = source
                node.metadata["document_id"] = str(node.ref_doc_id or source)

    def _node_metadata(self, node: Any) -> dict[str, Any]:
        metadata = self._jsonable_metadata(node.metadata)
        source = self._source_of_node(node)

        metadata.update(
            {
                "chunk_id": str(node.node_id),
                "document_id": str(node.ref_doc_id or source),
                "source_path": source,
                "text_length": len(node.text),
            }
        )

        return metadata

    def _search_hits(self, *, question: str, top_k: int) -> list[VectorRagHit]:
        collection = self._get_collection()

        if collection.count() == 0:
            raise AgentRuntimeError("Chroma 向量库为空，请先调用 /rag/vector/rebuild")

        query_embedding = DashScopeEmbeddingClient().embed_query(question)
        result = collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )

        ids = result.get("ids", [[]])[0]
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        hits: list[VectorRagHit] = []

        for chunk_id, text, metadata, distance in zip(ids, documents, metadatas, distances):
            safe_metadata = self._jsonable_metadata(metadata)
            distance_value = float(distance) if distance is not None else None
            similarity = 1 / (1 + distance_value) if distance_value is not None else None

            hits.append(
                VectorRagHit(
                    chunk_id=str(chunk_id),
                    document_id=safe_metadata.get("document_id"),
                    source=str(safe_metadata.get("source_path") or ""),
                    text=text,
                    distance=distance_value,
                    similarity=round(similarity, 6) if similarity is not None else None,
                    metadata=safe_metadata,
                )
            )

        return hits

    def _build_answer_chain(self):
        try:
            from langchain_core.output_parsers import StrOutputParser
            from langchain_core.prompts import ChatPromptTemplate
        except ModuleNotFoundError as exc:
            raise AgentConfigError("请先安装依赖：pip install -r requirements.txt") from exc

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "你是一个真实向量库 RAG 学习助手。"
                    "必须基于 Chroma 检索出来的 chunk 回答。"
                    "如果资料不足，就明确说明资料不足。"
                    "回答最后列出使用的 chunk_id 和 source_path。",
                ),
                (
                    "human",
                    "问题：{question}\n\n"
                    "<retrieved_chunks>\n{context}\n</retrieved_chunks>\n\n"
                    "请基于这些 chunk 回答，不要编造资料之外的信息。",
                ),
            ]
        )

        return prompt | get_chat_model() | StrOutputParser()

    def _format_context(self, hits: list[VectorRagHit]) -> str:
        if not hits:
            return "没有从 Chroma 检索到相关 chunk。"

        blocks = []

        for hit in hits:
            blocks.append(
                "\n".join(
                    [
                        f"chunk_id={hit.chunk_id}",
                        f"document_id={hit.document_id}",
                        f"source_path={hit.source}",
                        f"distance={hit.distance}",
                        f"text={hit.text}",
                    ]
                )
            )

        return "\n\n---\n\n".join(blocks)

    def _get_collection(self):
        if self._collection is not None:
            return self._collection

        try:
            import chromadb
        except ModuleNotFoundError as exc:
            raise AgentConfigError("请先安装依赖：pip install -r requirements.txt") from exc

        client = chromadb.PersistentClient(path=str(self._persist_dir()))
        self._collection = client.get_or_create_collection(
            name=settings.chroma_collection,
            metadata={"hnsw:space": "cosine"},
        )
        return self._collection

    def _reset_collection(self):
        try:
            import chromadb
        except ModuleNotFoundError as exc:
            raise AgentConfigError("请先安装依赖：pip install -r requirements.txt") from exc

        client = chromadb.PersistentClient(path=str(self._persist_dir()))

        try:
            client.delete_collection(settings.chroma_collection)
        except Exception:
            pass

        return client.get_or_create_collection(
            name=settings.chroma_collection,
            metadata={"hnsw:space": "cosine"},
        )

    def _persist_dir(self) -> Path:
        path = Path(settings.chroma_persist_dir)

        if not path.is_absolute():
            path = PROJECT_ROOT / path

        path.mkdir(parents=True, exist_ok=True)
        return path

    def _pipeline_steps(self) -> list[str]:
        return [
            "SimpleDirectoryReader: load local documents",
            "NodeParser/SentenceSplitter: create chunks",
            f"{settings.embedding_provider}:{settings.embedding_model}: embed chunk text",
            "Chroma PersistentClient: save chunk + embedding + metadata",
        ]

    def _source_of_node(self, node: Any) -> str:
        metadata = self._jsonable_metadata(node.metadata)

        if metadata.get("source_path"):
            return str(metadata["source_path"])

        if node.ref_doc_id:
            return self._relative_source(str(node.ref_doc_id))

        return "unknown"

    def _relative_source(self, path_value: str) -> str:
        path = Path(path_value)

        try:
            return path.resolve().relative_to(PROJECT_ROOT).as_posix()
        except (OSError, ValueError):
            return path_value

    def _jsonable_metadata(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        jsonable: dict[str, Any] = {}

        for key, value in (metadata or {}).items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                jsonable[key] = value
            elif isinstance(value, list):
                jsonable[key] = ", ".join(str(item) for item in value)
            else:
                jsonable[key] = str(value)

        return jsonable

    def _source_slug(self, source: str) -> str:
        stem = Path(source).stem.lower()
        slug = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")
        return slug or "doc"


vector_rag_service = VectorRagService()

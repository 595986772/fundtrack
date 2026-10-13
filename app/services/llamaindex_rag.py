"""LlamaIndex 框架版 RAG 服务。

这个文件对应你 Obsidian RAG 笔记里的核心链路：

Reader / Loader
-> Document
-> NodeParser / TextSplitter
-> Node / chunk
-> metadata / relationship
-> embedding
-> VectorStoreIndex
-> retriever
-> LLM response

和 app/services/rag.py 的区别：
- rag.py 是手写学习版，方便你理解底层逻辑；
- llamaindex_rag.py 是框架版，方便你理解真实 RAG 框架怎么组织对象和流水线。
"""

from pathlib import Path
from typing import Any
import hashlib
import math
import os
import re

from app.config import settings
from app.schemas import (
    RagFrameworkDocument,
    RagFrameworkNode,
    RagFrameworkNodesResponse,
    RagFrameworkQueryResponse,
    RagFrameworkReloadResponse,
    RagQueryRequest,
    RagSplitterComparisonItem,
    RagSplitterComparisonResponse,
)
from app.services.errors import AgentConfigError, AgentRuntimeError
from app.services.llm import get_chat_model

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = PROJECT_ROOT / "data" / "docs"

# LlamaIndex 的 SimpleDirectoryReader 可以读取很多格式。
# 这个 demo 先体现你文档里最重要的 Markdown / PDF，再保留 txt 方便测试。
FRAMEWORK_REQUIRED_EXTS = [".md", ".txt", ".pdf"]

# LlamaIndex 可能间接触发 matplotlib 缓存目录。
# 在 Codex / 沙箱环境里，用户目录未必可写，所以把缓存放到项目内。
MPLCONFIG_DIR = PROJECT_ROOT / ".cache" / "matplotlib"


def _prepare_runtime_dirs() -> None:
    """准备框架运行时需要的本地缓存目录。"""

    MPLCONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(MPLCONFIG_DIR))


def _load_llama_index_classes() -> dict[str, Any]:
    """集中导入 LlamaIndex 类。

    放在函数里导入有两个好处：
    1. 如果依赖没安装，FastAPI 还能启动，只是框架版 RAG 接口会提示安装依赖；
    2. 这个文件顶部不会一导入就触发较重的框架初始化。
    """

    _prepare_runtime_dirs()

    try:
        from llama_index.core import SimpleDirectoryReader, VectorStoreIndex
        from llama_index.core.embeddings import BaseEmbedding
        from llama_index.core.ingestion import IngestionPipeline
        from llama_index.core.node_parser import MarkdownNodeParser, SentenceSplitter, TokenTextSplitter
    except ModuleNotFoundError as exc:
        raise AgentConfigError("请先安装依赖：pip install -r requirements.txt") from exc

    return {
        "BaseEmbedding": BaseEmbedding,
        "IngestionPipeline": IngestionPipeline,
        "MarkdownNodeParser": MarkdownNodeParser,
        "SentenceSplitter": SentenceSplitter,
        "SimpleDirectoryReader": SimpleDirectoryReader,
        "TokenTextSplitter": TokenTextSplitter,
        "VectorStoreIndex": VectorStoreIndex,
    }


def _create_keyword_hash_embedding(base_embedding_class: Any):
    """创建一个本地关键词 Hash Embedding。

    正式项目通常会使用真实 embedding model，例如 OpenAI、BGE、Jina 等。
    这里为了学习和本地可运行，做一个简单的“关键词 -> 向量”模型：
    - 把中英文文本拆成 token；
    - 用 hash 把 token 放进固定长度向量；
    - 做归一化；
    - VectorStoreIndex 就能用这个向量做相似度检索。

    它不适合生产，但很适合观察 embedding / vector index / retriever 的数据流。
    """

    class KeywordHashEmbedding(base_embedding_class):
        """LlamaIndex BaseEmbedding 的最小本地实现。"""

        embed_dim: int = 256

        def _tokenize(self, text: str) -> list[str]:
            return re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.lower())

        def _embed(self, text: str) -> list[float]:
            vector = [0.0] * self.embed_dim

            for token in self._tokenize(text):
                digest = hashlib.sha1(token.encode("utf-8")).hexdigest()
                index = int(digest, 16) % self.embed_dim
                vector[index] += 1.0

            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            return [value / norm for value in vector]

        def _get_text_embedding(self, text: str) -> list[float]:
            return self._embed(text)

        def _get_query_embedding(self, query: str) -> list[float]:
            return self._embed(query)

        async def _aget_query_embedding(self, query: str) -> list[float]:
            return self._get_query_embedding(query)

    return KeywordHashEmbedding(model_name="local-keyword-hash", embed_dim=256)


class LlamaIndexRagService:
    """把 LlamaIndex 的 RAG 数据链路封装成 service。"""

    def __init__(self) -> None:
        self._documents: list[Any] = []
        self._nodes: list[Any] = []
        self._index: Any | None = None
        self._embed_model: Any | None = None
        self._parser_summary: dict[str, int] = {}

    def reload(self) -> RagFrameworkReloadResponse:
        """用 LlamaIndex 重新加载文档、切 Node、生成 embedding、构建索引。"""

        classes = _load_llama_index_classes()
        SimpleDirectoryReader = classes["SimpleDirectoryReader"]
        IngestionPipeline = classes["IngestionPipeline"]
        MarkdownNodeParser = classes["MarkdownNodeParser"]
        SentenceSplitter = classes["SentenceSplitter"]
        VectorStoreIndex = classes["VectorStoreIndex"]

        self._embed_model = _create_keyword_hash_embedding(classes["BaseEmbedding"])

        # Reader / Loader：从 data/docs 读取本地资料。
        # recursive=True 让它能读子目录；filename_as_id=True 让文件路径作为 Document ID 的来源。
        documents = SimpleDirectoryReader(
            input_dir=str(DOCS_DIR),
            recursive=True,
            required_exts=FRAMEWORK_REQUIRED_EXTS,
            filename_as_id=True,
        ).load_data()
        documents = [self._prepare_document(document) for document in documents]

        markdown_docs = [doc for doc in documents if self._source_of_document(doc).endswith(".md")]
        other_docs = [doc for doc in documents if not self._source_of_document(doc).endswith(".md")]

        nodes: list[Any] = []
        parser_summary: dict[str, int] = {}

        if markdown_docs:
            # Markdown 的重点是保留标题结构：
            # 先用 MarkdownNodeParser 识别标题层级，再用 SentenceSplitter 控制最大长度。
            markdown_pipeline = IngestionPipeline(
                transformations=[
                    MarkdownNodeParser(),
                    SentenceSplitter(chunk_size=512, chunk_overlap=64),
                    self._embed_model,
                ]
            )
            markdown_nodes = markdown_pipeline.run(documents=markdown_docs)
            self._tag_nodes(markdown_nodes, "MarkdownNodeParser -> SentenceSplitter")
            nodes.extend(markdown_nodes)
            parser_summary["MarkdownNodeParser -> SentenceSplitter"] = len(markdown_nodes)

        if other_docs:
            # PDF / TXT 更适合先按句子和长度切。
            # 普通 PDF 的难点主要是解析质量；复杂 PDF 后续可接 LlamaParse。
            sentence_pipeline = IngestionPipeline(
                transformations=[
                    SentenceSplitter(chunk_size=512, chunk_overlap=64),
                    self._embed_model,
                ]
            )
            other_nodes = sentence_pipeline.run(documents=other_docs)
            self._tag_nodes(other_nodes, "SentenceSplitter")
            nodes.extend(other_nodes)
            parser_summary["SentenceSplitter"] = len(other_nodes)

        self._assign_stable_node_ids(nodes)
        self._documents = documents
        self._nodes = nodes
        self._parser_summary = parser_summary

        # VectorStoreIndex：把 Node 的 embedding 放进内存向量索引。
        # 后面的 retriever 会从这个 index 里按向量相似度召回 Node。
        self._index = VectorStoreIndex(nodes, embed_model=self._embed_model) if nodes else None

        return RagFrameworkReloadResponse(
            document_count=len(documents),
            node_count=len(nodes),
            index_ready=self._index is not None,
            documents=[self._document_to_schema(document) for document in documents],
            parser_summary=parser_summary,
            pipeline_steps=self._pipeline_steps(),
            message="LlamaIndex 框架版 RAG 索引已重新构建",
        )

    def list_nodes(self) -> RagFrameworkNodesResponse:
        """查看当前 LlamaIndex Document 和 Node。"""

        if not self._nodes:
            self.reload()

        return RagFrameworkNodesResponse(
            document_count=len(self._documents),
            node_count=len(self._nodes),
            documents=[self._document_to_schema(document) for document in self._documents],
            nodes=[self._node_to_schema(node) for node in self._nodes],
        )

    def compare_splitters(self) -> RagSplitterComparisonResponse:
        """对比不同 NodeParser / TextSplitter 的切分效果。"""

        classes = _load_llama_index_classes()
        SimpleDirectoryReader = classes["SimpleDirectoryReader"]
        MarkdownNodeParser = classes["MarkdownNodeParser"]
        SentenceSplitter = classes["SentenceSplitter"]
        TokenTextSplitter = classes["TokenTextSplitter"]

        documents = SimpleDirectoryReader(
            input_dir=str(DOCS_DIR),
            recursive=True,
            required_exts=FRAMEWORK_REQUIRED_EXTS,
            filename_as_id=True,
        ).load_data()
        documents = [self._prepare_document(document) for document in documents]
        markdown_docs = [doc for doc in documents if self._source_of_document(doc).endswith(".md")]

        comparisons: list[RagSplitterComparisonItem] = []

        if markdown_docs:
            markdown_nodes = MarkdownNodeParser().get_nodes_from_documents(markdown_docs)
            comparisons.append(
                self._comparison_item(
                    parser="MarkdownNodeParser",
                    target_documents="Markdown documents",
                    nodes=markdown_nodes,
                    note="按 Markdown 标题结构切，适合 Obsidian、README、技术文档。",
                )
            )

        sentence_nodes = SentenceSplitter(
            chunk_size=512,
            chunk_overlap=64,
        ).get_nodes_from_documents(documents)
        comparisons.append(
            self._comparison_item(
                parser="SentenceSplitter(chunk_size=512, chunk_overlap=64)",
                target_documents="All supported documents",
                nodes=sentence_nodes,
                note="尽量按句子边界切，适合 PDF 正文、普通文章、说明文档。",
            )
        )

        token_nodes = TokenTextSplitter(
            chunk_size=512,
            chunk_overlap=64,
        ).get_nodes_from_documents(documents)
        comparisons.append(
            self._comparison_item(
                parser="TokenTextSplitter(chunk_size=512, chunk_overlap=64)",
                target_documents="All supported documents",
                nodes=token_nodes,
                note="按 token 控制长度更稳定，但更可能切断语义结构。",
            )
        )

        return RagSplitterComparisonResponse(
            document_count=len(documents),
            comparisons=comparisons,
            conclusion="Markdown 优先保留标题结构；普通文本/PDF 可用 SentenceSplitter；需要严格长度时再考虑 TokenTextSplitter。",
        )

    async def query(self, request: RagQueryRequest) -> RagFrameworkQueryResponse:
        """用 LlamaIndex Retriever 召回 Node，再交给 DeepSeek 生成答案。"""

        if self._index is None:
            self.reload()

        if self._index is None:
            return RagFrameworkQueryResponse(
                user_id=request.user_id,
                question=request.message,
                answer="当前没有可检索的资料，请先放入文档并重建索引。",
                retrieved_nodes=[],
                used_model=settings.deepseek_model,
                pipeline_steps=self._pipeline_steps(),
            )

        retriever = self._index.as_retriever(similarity_top_k=request.top_k)
        retrieved = retriever.retrieve(request.message)
        retrieved_nodes = [self._node_to_schema(item.node, score=item.score) for item in retrieved]
        context = self._format_context(retrieved_nodes)
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
            raise AgentRuntimeError(f"LlamaIndex RAG 调用大模型失败：{exc}") from exc

        return RagFrameworkQueryResponse(
            user_id=request.user_id,
            question=request.message,
            answer=answer,
            retrieved_nodes=retrieved_nodes,
            used_model=settings.deepseek_model,
            pipeline_steps=self._pipeline_steps(),
        )

    def _prepare_document(self, document: Any) -> Any:
        """清理并补充 Document metadata。"""

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
                "internal_path_hidden": absolute_path != source,
            }
        )

        document.id_ = source
        document.metadata = metadata

        # 体现你文档里的“metadata 可见性”：
        # 有些 metadata 适合检索和溯源，但不一定适合直接给 LLM 或 embedding 使用。
        hidden_keys = [
            "file_size",
            "creation_date",
            "last_accessed_date",
            "last_modified_date",
            "internal_path_hidden",
        ]
        document.excluded_llm_metadata_keys = hidden_keys
        document.excluded_embed_metadata_keys = hidden_keys
        return document

    def _tag_nodes(self, nodes: list[Any], parser_name: str) -> None:
        """给 Node 打上 parser 标记，方便你在接口里观察它来自哪个分块器。"""

        for node in nodes:
            node.metadata = self._jsonable_metadata(node.metadata)
            node.metadata["parser"] = parser_name
            node.metadata.setdefault("source_path", self._source_of_node(node))

    def _assign_stable_node_ids(self, nodes: list[Any]) -> None:
        """给 Node 设置更容易读的稳定 ID，并记录前后关系。"""

        groups: dict[str, list[Any]] = {}

        for node in nodes:
            source = self._source_of_node(node)
            groups.setdefault(source, []).append(node)

        for source, source_nodes in groups.items():
            slug = self._source_slug(source)

            for index, node in enumerate(source_nodes, start=1):
                node.id_ = f"{slug}-node-{index:03d}"
                node.metadata["node_index"] = index
                node.metadata["previous_node_id"] = (
                    f"{slug}-node-{index - 1:03d}" if index > 1 else None
                )
                node.metadata["next_node_id"] = (
                    f"{slug}-node-{index + 1:03d}" if index < len(source_nodes) else None
                )

    def _build_answer_chain(self):
        """构造最终生成答案的 LangChain LCEL 链。"""

        try:
            from langchain_core.output_parsers import StrOutputParser
            from langchain_core.prompts import ChatPromptTemplate
        except ModuleNotFoundError as exc:
            raise AgentConfigError("请先安装依赖：pip install -r requirements.txt") from exc

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "你是一个 LlamaIndex RAG 学习助手。"
                    "你必须基于检索到的 Node 回答，资料不足时要说明不足。"
                    "回答最后列出使用的 node_id、ref_doc_id 和 source_path。",
                ),
                (
                    "human",
                    "问题：{question}\n\n"
                    "<retrieved_nodes>\n{context}\n</retrieved_nodes>\n\n"
                    "请基于这些 Node 回答，不要编造资料之外的信息。",
                ),
            ]
        )

        return prompt | get_chat_model() | StrOutputParser()

    def _format_context(self, nodes: list[RagFrameworkNode]) -> str:
        """把检索到的 Node 拼成 LLM 可以阅读的上下文。"""

        if not nodes:
            return "没有检索到相关 Node。"

        blocks = []

        for node in nodes:
            blocks.append(
                "\n".join(
                    [
                        f"node_id={node.node_id}",
                        f"ref_doc_id={node.ref_doc_id}",
                        f"source_path={node.source}",
                        f"score={node.score}",
                        f"text={node.text}",
                    ]
                )
            )

        return "\n\n---\n\n".join(blocks)

    def _comparison_item(
        self,
        *,
        parser: str,
        target_documents: str,
        nodes: list[Any],
        note: str,
    ) -> RagSplitterComparisonItem:
        """把一组切分结果整理成对比指标。"""

        lengths = [len(node.text) for node in nodes]
        average = round(sum(lengths) / len(lengths), 2) if lengths else 0.0
        max_length = max(lengths) if lengths else 0
        sample_text = nodes[0].text[:240] if nodes else ""

        return RagSplitterComparisonItem(
            parser=parser,
            target_documents=target_documents,
            node_count=len(nodes),
            average_chars=average,
            max_chars=max_length,
            sample_text=sample_text,
            note=note,
        )

    def _document_to_schema(self, document: Any) -> RagFrameworkDocument:
        """把 LlamaIndex Document 转成 FastAPI 可返回的 Pydantic 模型。"""

        metadata = self._jsonable_metadata(document.metadata)
        source = str(metadata.get("source_path") or document.id_)

        return RagFrameworkDocument(
            doc_id=str(document.id_),
            source=source,
            file_name=metadata.get("file_name"),
            file_type=metadata.get("file_type"),
            metadata=metadata,
            excluded_llm_metadata_keys=list(getattr(document, "excluded_llm_metadata_keys", [])),
            excluded_embed_metadata_keys=list(getattr(document, "excluded_embed_metadata_keys", [])),
        )

    def _node_to_schema(self, node: Any, score: float | None = None) -> RagFrameworkNode:
        """把 LlamaIndex Node 转成 FastAPI 可返回的 Pydantic 模型。"""

        metadata = self._jsonable_metadata(node.metadata)
        source = str(metadata.get("source_path") or self._source_of_node(node))

        return RagFrameworkNode(
            node_id=str(node.node_id),
            ref_doc_id=str(node.ref_doc_id) if node.ref_doc_id else None,
            source=source,
            text=node.text,
            score=score,
            metadata=metadata,
            relationships=self._relationships_to_dict(node),
        )

    def _relationships_to_dict(self, node: Any) -> dict[str, str | None]:
        """把 LlamaIndex Node 的关系对象转成普通 dict。"""

        relationships: dict[str, str | None] = {}

        for relation, related_info in getattr(node, "relationships", {}).items():
            relation_name = getattr(relation, "name", str(relation))
            relationships[relation_name] = getattr(related_info, "node_id", str(related_info))

        metadata = self._jsonable_metadata(node.metadata)
        relationships["PREVIOUS"] = metadata.get("previous_node_id")
        relationships["NEXT"] = metadata.get("next_node_id")
        return relationships

    def _pipeline_steps(self) -> list[str]:
        """返回框架版 RAG 的学习步骤。"""

        return [
            "SimpleDirectoryReader: load .md/.txt/.pdf from data/docs",
            "Document: normalize doc_id and metadata",
            "MarkdownNodeParser: preserve Markdown heading structure",
            "SentenceSplitter: control chunk_size=512 and chunk_overlap=64",
            "IngestionPipeline: run node parsing transformations",
            "Local KeywordHashEmbedding: generate demo embeddings",
            "VectorStoreIndex: build in-memory vector index",
            "Retriever: retrieve top_k Nodes",
            "DeepSeek + LCEL prompt: generate grounded answer with sources",
        ]

    def _source_of_document(self, document: Any) -> str:
        metadata = self._jsonable_metadata(document.metadata)
        return str(metadata.get("source_path") or self._relative_source(str(document.id_)))

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
        """把 metadata 里不方便 JSON 序列化的值转成字符串。"""

        jsonable: dict[str, Any] = {}

        for key, value in (metadata or {}).items():
            if value is None or isinstance(value, (str, int, float, bool)):
                jsonable[key] = value
            elif isinstance(value, list):
                jsonable[key] = [
                    item if item is None or isinstance(item, (str, int, float, bool)) else str(item)
                    for item in value
                ]
            else:
                jsonable[key] = str(value)

        return jsonable

    def _source_slug(self, source: str) -> str:
        stem = Path(source).stem.lower()
        slug = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")
        return slug or "doc"


llamaindex_rag_service = LlamaIndexRagService()

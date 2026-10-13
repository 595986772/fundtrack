"""用户知识库文档管理。

向量检索仍由 `vector_rag.py` 负责；本模块只提供产品需要的文档列表、受控上传、删除和
重建入口，替代早期前端中的多组 RAG 教学按钮。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.services.llamaindex_rag import FRAMEWORK_REQUIRED_EXTS
from app.services.vector_rag import DOCS_DIR, VectorRagService, vector_rag_service


class KnowledgeDocumentError(ValueError):
    """文档名称、类型、大小或状态不符合知识库约束。"""


class KnowledgeService:
    """管理 `data/docs` 中可以进入 RAG 索引的源文档。"""

    max_upload_bytes = 10 * 1024 * 1024
    allowed_extensions = frozenset(FRAMEWORK_REQUIRED_EXTS)

    def __init__(
        self,
        *,
        docs_dir: Path = DOCS_DIR,
        vector_service: VectorRagService = vector_rag_service,
    ) -> None:
        self.docs_dir = docs_dir.resolve()
        self.vector_service = vector_service
        self.docs_dir.mkdir(parents=True, exist_ok=True)

    def status(self) -> dict[str, Any]:
        """返回源文档和当前 Chroma collection 的组合状态。"""

        documents = self.list_documents()
        try:
            vector_stats = self.vector_service.stats()
            vector_ready = vector_stats.is_ready
            chunk_count = vector_stats.chunk_count
            error = None
        except Exception as exc:
            # 文档管理本身不应因为 Chroma 暂时不可用而完全失效。
            vector_ready = False
            chunk_count = 0
            error = str(exc)

        return {
            "documents": documents,
            "document_count": len(documents),
            "vector_ready": vector_ready,
            "chunk_count": chunk_count,
            "embedding_model": settings.embedding_model,
            "error": error,
        }

    def list_documents(self) -> list[dict[str, Any]]:
        """只列出当前 RAG loader 支持的普通文件。"""

        documents: list[dict[str, Any]] = []
        for path in self.docs_dir.iterdir():
            if not path.is_file() or path.suffix.lower() not in self.allowed_extensions:
                continue
            stat = path.stat()
            documents.append(
                {
                    "name": path.name,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime,
                        tz=timezone.utc,
                    ).isoformat(),
                }
            )

        return sorted(documents, key=lambda item: item["modified_at"], reverse=True)

    def save(self, *, filename: str, content: bytes) -> dict[str, Any]:
        """保存一个新文档；不自动调用收费的 embedding 接口。"""

        target = self._safe_document_path(filename)
        if target.exists():
            raise KnowledgeDocumentError(f"同名文档已存在：{target.name}")
        if not content:
            raise KnowledgeDocumentError("上传文档不能为空。")
        if len(content) > self.max_upload_bytes:
            raise KnowledgeDocumentError("单个文档不能超过 10 MB。")

        target.write_bytes(content)
        stat = target.stat()
        return {
            "name": target.name,
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(
                stat.st_mtime,
                tz=timezone.utc,
            ).isoformat(),
        }

    def delete(self, *, filename: str) -> None:
        """删除源文档；向量索引会在用户下次点击重建后同步。"""

        target = self._safe_document_path(filename)
        if not target.exists():
            raise KnowledgeDocumentError(f"文档不存在：{target.name}")
        target.unlink()

    def rebuild(self):
        """显式重建向量库，避免每次上传都产生 embedding 费用。"""

        return self.vector_service.rebuild()

    def _safe_document_path(self, filename: str) -> Path:
        """拒绝目录穿越、隐藏文件和 loader 不支持的扩展名。"""

        raw_name = str(filename or "").strip()
        if not raw_name or Path(raw_name).name != raw_name or raw_name.startswith("."):
            raise KnowledgeDocumentError("文档名不合法。")
        if Path(raw_name).suffix.lower() not in self.allowed_extensions:
            supported = ", ".join(sorted(self.allowed_extensions))
            raise KnowledgeDocumentError(f"仅支持以下格式：{supported}")

        target = (self.docs_dir / raw_name).resolve()
        if target.parent != self.docs_dir:
            raise KnowledgeDocumentError("文档路径超出知识库目录。")
        return target


knowledge_service = KnowledgeService()

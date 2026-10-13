"""产品知识库文档管理的确定性测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.services.knowledge import KnowledgeDocumentError, KnowledgeService


class FakeVectorStats:
    is_ready = True
    chunk_count = 7


class FakeVectorService:
    def stats(self):
        return FakeVectorStats()

    def rebuild(self):
        return {"chunk_count": 7}


class KnowledgeServiceTests(unittest.TestCase):
    def test_upload_list_and_delete_document(self) -> None:
        """合法文档应进入源目录，删除后从状态中消失。"""

        with TemporaryDirectory() as temporary_directory:
            service = KnowledgeService(
                docs_dir=Path(temporary_directory),
                vector_service=FakeVectorService(),
            )
            uploaded = service.save(
                filename="agent-notes.md",
                content=b"# Agent notes",
            )
            status = service.status()

            self.assertEqual(uploaded["name"], "agent-notes.md")
            self.assertEqual(status["document_count"], 1)
            self.assertTrue(status["vector_ready"])
            self.assertEqual(status["chunk_count"], 7)

            service.delete(filename="agent-notes.md")
            self.assertEqual(service.list_documents(), [])

    def test_rejects_unsafe_duplicate_and_unsupported_paths(self) -> None:
        """上传入口必须拒绝目录穿越、未知类型和静默覆盖。"""

        with TemporaryDirectory() as temporary_directory:
            service = KnowledgeService(
                docs_dir=Path(temporary_directory),
                vector_service=FakeVectorService(),
            )
            service.save(filename="safe.txt", content=b"safe")

            with self.assertRaises(KnowledgeDocumentError):
                service.save(filename="../outside.md", content=b"unsafe")
            with self.assertRaises(KnowledgeDocumentError):
                service.save(filename="script.py", content=b"print('unsafe')")
            with self.assertRaises(KnowledgeDocumentError):
                service.save(filename="safe.txt", content=b"overwrite")


if __name__ == "__main__":
    unittest.main()

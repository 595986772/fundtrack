"""板块词典、DeepSeek 补充词和 SQL 缓存的确定性测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from app.database import Database
from app.services.market_topic_keywords import (
    MarketTopicKeywordError,
    MarketTopicKeywordRepository,
    MarketTopicKeywordService,
    ModelTopicExpansion,
    ModelTopicExpansionBatch,
)


class MarketTopicKeywordServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database = Database(
            f"sqlite:///{Path(self.temporary_directory.name) / 'topic-cache.db'}"
        )
        self.database = database
        self.repository = MarketTopicKeywordRepository(database)
        self.service = MarketTopicKeywordService(self.repository)

    def tearDown(self) -> None:
        self.database.engine.dispose()
        self.temporary_directory.cleanup()

    async def test_fixed_dictionary_works_without_model(self) -> None:
        result = await self.service.expand(topics=["半导体"], use_model=False)

        self.assertFalse(result["model_called"])
        self.assertIn("芯片", result["keywords"])
        self.assertIn("集成电路", result["keywords"])
        self.assertEqual(result["expansions"][0]["source"], "dictionary")
        self.assertIsNone(self.repository.get(topic="半导体"))

    async def test_model_terms_are_validated_saved_and_reused(self) -> None:
        model_result = ModelTopicExpansionBatch(
            expansions=[
                ModelTopicExpansion(
                    topic="半导体",
                    # “市场”太宽泛，句式含标点，都会被 Python 丢弃。
                    terms=["HBM", "先进封装", "市场", "请买入！"],
                )
            ]
        )
        invoke = AsyncMock(return_value=model_result)
        with patch.object(self.service, "_invoke_model", invoke):
            first = await self.service.expand(topics=["半导体"], use_model=True)
            second = await self.service.expand(topics=["半导体"], use_model=True)

        invoke.assert_awaited_once()
        self.assertEqual(first["expansions"][0]["source"], "model")
        self.assertEqual(second["expansions"][0]["source"], "cache")
        self.assertIn("HBM", second["keywords"])
        self.assertIn("先进封装", second["keywords"])
        self.assertNotIn("市场", second["keywords"])
        self.assertNotIn("请买入！", second["keywords"])
        cached = self.repository.get(topic="半导体")
        self.assertEqual(cached["model_terms"], ["HBM", "先进封装"])

    async def test_model_failure_falls_back_to_dictionary(self) -> None:
        with patch.object(
            self.service,
            "_invoke_model",
            AsyncMock(side_effect=MarketTopicKeywordError("模拟模型不可用")),
        ):
            result = await self.service.expand(topics=["新能源"], use_model=True)

        self.assertIn("光伏", result["keywords"])
        self.assertEqual(result["expansions"][0]["source"], "dictionary")
        self.assertIn("固定词典", result["warnings"][0])

    async def test_rejects_more_than_three_topics(self) -> None:
        with self.assertRaisesRegex(MarketTopicKeywordError, "最多选择 3 个"):
            await self.service.expand(
                topics=["半导体", "新能源", "医药", "消费"],
                use_model=False,
            )


if __name__ == "__main__":
    unittest.main()

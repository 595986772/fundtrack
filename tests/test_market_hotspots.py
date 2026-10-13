"""热点历史快照、生命周期和查询筛选测试。

测试使用临时 SQLite 与 Fake Embedding，不访问新闻网站、DeepSeek 或 DashScope。
先保存一篇报道作为基线，再加入同主题第二来源，验证热点会被判为正在升温。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.database import Database
from app.services.market_event_clusters import (
    MarketEventClusterRepository,
    MarketEventClusterService,
)
from app.services.market_events import MarketEventRepository
from app.services.market_hotspots import (
    MarketHotspotRepository,
    MarketHotspotTrackingService,
)


class SameTopicEmbeddingClient:
    """所有测试文本返回同一向量，使两家来源稳定归入一个事件簇。"""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


def hotspot_event(
    *,
    event_id: str,
    event_date: str,
    source_name: str,
    source_level: str,
) -> dict:
    digest = (event_id.encode("utf-8").hex() * 64)[:64]
    return {
        "event_id": event_id,
        "event_date": event_date,
        "published_date": event_date,
        "event_type": "market_notice",
        "title": "半导体先进封装需求升温",
        "summary": "产业链披露先进封装订单与产能变化。",
        "impact_direction": "neutral",
        "impact_horizon": "short_term",
        "impact_reason": "等待更多来源确认实际影响。",
        "affected_fund_codes": ["110020"],
        "tags": ["半导体", "先进封装"],
        "source_name": source_name,
        "source_level": source_level,
        "source_grade_reason": "测试来源",
        "source_url": f"https://example.com/{event_id}",
        "content_hash": f"sha256:{digest}",
    }


class MarketHotspotTrackingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "hotspots.db"
        self.database = Database(f"sqlite:///{database_path}")
        self.event_repository = MarketEventRepository(self.database)
        cluster_repository = MarketEventClusterRepository(self.database)
        self.cluster_service = MarketEventClusterService(
            repository=cluster_repository,
            event_repository=self.event_repository,
            embedding_factory=SameTopicEmbeddingClient,
            embedding_model="fake-hotspot-embedding-v1",
        )
        self.hotspot_service = MarketHotspotTrackingService(
            MarketHotspotRepository(self.database)
        )

    def tearDown(self) -> None:
        self.database.engine.dispose()
        self.temporary_directory.cleanup()

    def test_new_source_turns_baseline_into_rising_hotspot(self) -> None:
        first = hotspot_event(
            event_id="chip-official",
            event_date="2026-09-01",
            source_name="交易所公告",
            source_level="A",
        )
        self.event_repository.upsert(
            events=[first],
            fetched_at=datetime(2026, 9, 1, 9, tzinfo=timezone.utc),
        )
        self.cluster_service.build()
        self.hotspot_service.capture(
            observed_at=datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
        )

        second = hotspot_event(
            event_id="chip-media",
            event_date="2026-09-02",
            source_name="产业媒体",
            source_level="B",
        )
        self.event_repository.upsert(
            events=[second],
            fetched_at=datetime(2026, 9, 2, 11, tzinfo=timezone.utc),
        )
        self.cluster_service.build()
        capture = self.hotspot_service.capture(
            observed_at=datetime(2026, 9, 2, 11, tzinfo=timezone.utc)
        )
        result = self.hotspot_service.overview(
            query="先进封装",
            fund_code="110020",
            history_days=30,
            limit=10,
        )

        self.assertEqual(capture["captured_count"], 1)
        self.assertEqual(result["count"], 1)
        hotspot = result["hotspots"][0]
        self.assertEqual(hotspot["lifecycle"], "rising")
        self.assertEqual(hotspot["member_delta"], 1)
        self.assertEqual(hotspot["source_delta"], 1)
        self.assertGreater(hotspot["change_since_previous"], 0)
        self.assertGreater(hotspot["heat_change_24h"], 0)
        self.assertEqual(hotspot["snapshot_count"], 2)
        self.assertEqual(len(hotspot["timeline"]), 2)
        self.assertEqual(len(hotspot["evidence"]), 2)
        self.assertIn("110020", hotspot["affected_fund_codes"])

    def test_empty_database_returns_stable_unavailable_response(self) -> None:
        result = self.hotspot_service.overview(history_days=30)

        self.assertEqual(result["data_mode"], "unavailable")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["hotspots"], [])
        self.assertIn("获取最新市场情报", result["disclaimer"])


if __name__ == "__main__":
    unittest.main()

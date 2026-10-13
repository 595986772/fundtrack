"""多来源事件聚类、向量缓存和证据去重的确定性测试。

测试使用 Fake Embedding 和临时 SQLite，不访问阿里云。向量由标题映射得到，因此可以
精确验证哪些报道应该聚合，也能证明基金范围约束会阻止语义相似但对象不同的公告误合并。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from app.database import Database
from app.services.background_jobs import BackgroundJobService
from app.services.fund_assessment import EvidenceFusionService
from app.services.market_event_clusters import (
    MarketEventClusterRepository,
    MarketEventClusterService,
)
from app.services.market_events import MarketEventRepository, MarketEventService


def event(
    *,
    event_id: str,
    title: str,
    source_name: str,
    source_level: str,
    event_date: str = "2026-08-27",
    event_type: str = "policy",
    fund_code: str = "110020",
) -> dict:
    """生成内容哈希各不相同的原始事件，避免 Repository 的原文去重先行合并。"""

    digest = (event_id.encode("utf-8").hex() * 64)[:64]
    return {
        "event_id": event_id,
        "event_date": event_date,
        "published_date": event_date,
        "event_type": event_type,
        "title": title,
        "summary": f"{title}的测试摘要。",
        "impact_direction": "positive",
        "impact_horizon": "short_term",
        "impact_reason": "该事件提供测试用支撑证据。",
        "affected_fund_codes": [fund_code],
        "tags": ["测试"],
        "source_name": source_name,
        "source_level": source_level,
        "source_grade_reason": "测试来源等级",
        "source_url": f"https://example.com/{event_id}",
        "content_hash": f"sha256:{digest}",
    }


class FakeEmbeddingClient:
    """把预设主题映射到单位向量，并记录真实调用次数。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            if "消费支持政策" in text or "扩大消费支持举措" in text:
                vectors.append([1.0, 0.0, 0.0])
            elif "基金公告" in text:
                vectors.append([0.0, 0.0, 1.0])
            else:
                vectors.append([0.0, 1.0, 0.0])
        return vectors


class MarketEventClusterServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        path = Path(self.temporary_directory.name) / "clusters.db"
        self.database = Database(f"sqlite:///{path}")
        self.event_repository = MarketEventRepository(self.database)
        self.cluster_repository = MarketEventClusterRepository(self.database)
        self.fake_embedding = FakeEmbeddingClient()
        self.service = MarketEventClusterService(
            repository=self.cluster_repository,
            event_repository=self.event_repository,
            embedding_factory=lambda: self.fake_embedding,
            embedding_model="fake-embedding-v1",
        )

        self.events = [
            event(
                event_id="consumption-official",
                title="消费支持政策发布",
                source_name="官方来源",
                source_level="A",
            ),
            event(
                event_id="consumption-media",
                title="扩大消费支持举措落地",
                source_name="权威媒体",
                source_level="B",
                event_date="2026-08-26",
            ),
            event(
                event_id="market-data",
                title="另一市场数据发布",
                source_name="数据机构",
                source_level="B",
            ),
            event(
                event_id="fund-a",
                title="基金公告调整事项",
                source_name="基金公司甲",
                source_level="A",
                event_type="fund_announcement",
                fund_code="110020",
            ),
            event(
                event_id="fund-b",
                title="基金公告调整事项",
                source_name="基金公司乙",
                source_level="A",
                event_type="fund_announcement",
                fund_code="161725",
            ),
        ]
        self.event_repository.upsert(
            events=self.events,
            fetched_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        )

    def tearDown(self) -> None:
        self.database.engine.dispose()
        self.temporary_directory.cleanup()

    def test_bounded_embedding_keeps_all_events_and_reuses_cached_vectors(self) -> None:
        first = self.service.build(max_new_embeddings=2)
        self.assertEqual(first["generated_embedding_count"], 2)
        self.assertEqual(first["deferred_embedding_count"], len(self.events) - 2)
        self.assertEqual(first["event_count"], len(self.events))
        self.assertEqual(len(self.cluster_repository.load_embeddings()), 2)
        second = self.service.build(max_new_embeddings=0)
        self.assertEqual(second["reused_embedding_count"], 2)
        self.assertEqual(len(self.fake_embedding.calls), 1)
        self.assertEqual(second["cluster_count"], first["cluster_count"])

    def test_clusters_similar_reports_but_keeps_disjoint_funds_separate(self) -> None:
        result = self.service.build()
        timeline = MarketEventService(
            fixture_path=Path("/fixture/not-used.json"),
            repository=self.event_repository,
        ).search(limit=20)
        by_id = {item["event_id"]: item for item in timeline["events"]}

        self.assertEqual(result["event_count"], 5)
        self.assertEqual(result["cluster_count"], 4)
        self.assertEqual(result["merged_report_count"], 1)
        self.assertEqual(
            by_id["consumption-official"]["cluster"]["cluster_id"],
            by_id["consumption-media"]["cluster"]["cluster_id"],
        )
        self.assertNotEqual(
            by_id["fund-a"]["cluster"]["cluster_id"],
            by_id["fund-b"]["cluster"]["cluster_id"],
        )
        consumption_cluster = by_id["consumption-official"]["cluster"]
        self.assertEqual(consumption_cluster["member_count"], 2)
        self.assertEqual(consumption_cluster["source_count"], 2)
        self.assertEqual(consumption_cluster["heat_level"], "high")
        self.assertGreaterEqual(consumption_cluster["heat_score"], 70)
        self.assertEqual(timeline["cluster_count"], 4)

    def test_second_build_reuses_all_cached_embeddings(self) -> None:
        first = self.service.build()
        second = self.service.build()

        self.assertEqual(first["generated_embedding_count"], 5)
        self.assertEqual(second["generated_embedding_count"], 0)
        self.assertEqual(second["reused_embedding_count"], 5)
        self.assertEqual(len(self.fake_embedding.calls), 1)

    def test_same_source_reports_require_higher_similarity(self) -> None:
        """同一家媒体的主题相似快讯不能仅凭 0.85 相似度被误当成同一事件。"""

        first = event(
            event_id="same-source-one",
            title="同源快讯甲",
            source_name="同一家媒体",
            source_level="C",
        )
        second = event(
            event_id="same-source-two",
            title="同源快讯乙",
            source_name="同一家媒体",
            source_level="C",
        )

        compatible, threshold = self.service._compatible(first, second)
        similarity = self.service._cosine_similarity(
            [1.0, 0.0],
            [0.85, 0.5267826876],
        )

        self.assertTrue(compatible)
        self.assertAlmostEqual(threshold, 0.90)
        self.assertAlmostEqual(similarity, 0.85, places=6)
        self.assertLess(similarity, threshold)

    def test_evidence_fusion_counts_one_score_per_cluster(self) -> None:
        self.service.build()
        timeline = MarketEventService(
            fixture_path=Path("/fixture/not-used.json"),
            repository=self.event_repository,
        ).search(fund_code="110020", limit=20)
        raw_evidence = [
            EvidenceFusionService._event_to_evidence(item)
            for item in timeline["events"]
        ]
        deduplicated = EvidenceFusionService._deduplicate_event_clusters(raw_evidence)

        self.assertEqual(len(raw_evidence), 4)
        self.assertEqual(len(deduplicated), 3)
        self.assertGreater(
            EvidenceFusionService._score(evidence=raw_evidence)["supportive_points"],
            EvidenceFusionService._score(evidence=deduplicated)["supportive_points"],
        )


class MarketEventClusterJobTests(unittest.TestCase):
    def test_background_job_runs_cluster_workflow(self) -> None:
        jobs = BackgroundJobService()
        fake_result = {
            "summary": "5 条报道已归并为 4 个逻辑事件簇。",
            "workflow": "market_event_clustering",
        }
        try:
            with patch(
                "app.services.market_event_clusters.market_event_cluster_service.build",
                return_value=fake_result,
            ) as build_mock:
                jobs.start_job(
                    user_id="chen",
                    job_id="cluster-job-test",
                    title="聚合事件",
                    job_type="market_event_clustering",
                )
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    job = jobs.get_job(user_id="chen", job_id="cluster-job-test")["job"]
                    if job["status"] in {"completed", "failed", "cancelled"}:
                        break
                    time.sleep(0.01)

            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["result"]["workflow"], "market_event_clustering")
            build_mock.assert_called_once_with()
        finally:
            jobs.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()

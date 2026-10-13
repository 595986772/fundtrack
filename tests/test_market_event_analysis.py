"""市场事件持久化语义分析的 Harness 边界测试。

测试全部使用临时 SQLite 和 Fake 模型，不访问 DeepSeek。重点验证模型只拥有“提出
判断”的权限，Python 仍负责 event_id、基金白名单、置信度、版本和持久化边界。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import AsyncMock, patch

from app.database import Database
from app.models import MarketEventModel
from app.services.background_jobs import BackgroundJobService
from app.services.market_event_analysis import (
    MarketEventAnalysisService,
    ModelMarketEventBatch,
    ModelMarketEventJudgment,
)
from app.services.market_events import MarketEventRepository, MarketEventService


def sample_event(*, event_id: str, content_digit: str) -> dict:
    """生成一条默认中性的原始采集事件。"""

    return {
        "event_id": event_id,
        "event_date": "2026-08-27",
        "published_date": "2026-08-27",
        "event_type": "policy",
        "title": f"测试市场事件 {event_id}",
        "summary": "用于验证模型语义分析与原始数据是否正确隔离。",
        "impact_direction": "neutral",
        "impact_horizon": "short_term",
        "impact_reason": "尚未进行模型语义复核。",
        "affected_fund_codes": [],
        "tags": ["测试"],
        "source_name": "测试官方来源",
        "source_level": "A",
        "source_grade_reason": "发布主体官方来源",
        "source_url": f"https://example.com/{event_id}",
        "content_hash": "sha256:" + content_digit * 64,
    }


class MarketEventAnalysisServiceTests(unittest.IsolatedAsyncioTestCase):
    """验证结构化模型输出经过校验后才会进入事件解释层。"""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "analysis.db"
        self.database = Database(f"sqlite:///{database_path}")
        self.repository = MarketEventRepository(self.database)
        self.repository.upsert(
            events=[
                sample_event(event_id="evt-accepted", content_digit="1"),
                sample_event(event_id="evt-abstained", content_digit="2"),
            ],
            fetched_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )
        self.service = MarketEventAnalysisService(self.repository)

    def tearDown(self) -> None:
        self.database.engine.dispose()
        self.temporary_directory.cleanup()

    async def test_accepts_valid_result_and_abstains_low_confidence(self) -> None:
        model_result = ModelMarketEventBatch(
            judgments=[
                ModelMarketEventJudgment(
                    event_id="evt-accepted",
                    entities=["财政部门"],
                    themes=["消费"],
                    direction="supportive",
                    horizon="medium_term",
                    severity=3,
                    relevance="direct",
                    confidence=0.88,
                    reason="政策语义对消费主题形成阶段性支撑。",
                    # 999999 会被 Python 基金白名单移除。
                    affected_fund_codes=["161725", "999999"],
                    uncertainties=[],
                ),
                ModelMarketEventJudgment(
                    event_id="evt-abstained",
                    entities=[],
                    themes=["宏观"],
                    direction="caution",
                    horizon="short_term",
                    severity=2,
                    relevance="related",
                    confidence=0.42,
                    reason="信息不足，方向可信度较低。",
                    affected_fund_codes=["110020"],
                    uncertainties=["缺少量化影响。"],
                ),
                # 模型即使返回虚构事件，也不能把它写进数据库。
                ModelMarketEventJudgment(
                    event_id="invented-event",
                    entities=[],
                    themes=[],
                    direction="supportive",
                    horizon="long_term",
                    severity=3,
                    relevance="direct",
                    confidence=0.99,
                    reason="不存在的事件。",
                    affected_fund_codes=["110020"],
                    uncertainties=[],
                ),
            ]
        )

        with patch.object(
            self.service,
            "_invoke_model",
            new=AsyncMock(return_value=model_result),
        ):
            result = await self.service.analyze_pending()

        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(result["abstained_count"], 1)
        self.assertEqual(result["ignored_model_event_ids"], ["invented-event"])
        self.assertEqual(
            self.repository.analysis_status(),
            {
                "pending_analysis_count": 0,
                "analyzed_count": 2,
                "accepted_analysis_count": 1,
                "abstained_analysis_count": 1,
            },
        )

        public_events = MarketEventService(
            fixture_path=Path("/fixture/not-used.json"),
            repository=self.repository,
        ).search()["events"]
        accepted = next(item for item in public_events if item["event_id"] == "evt-accepted")
        abstained = next(item for item in public_events if item["event_id"] == "evt-abstained")
        self.assertEqual(accepted["impact_direction"], "positive")
        self.assertEqual(accepted["affected_fund_codes"], ["161725"])
        self.assertEqual(accepted["semantic_analysis"]["status"], "accepted")
        self.assertEqual(abstained["impact_direction"], "neutral")
        self.assertEqual(abstained["semantic_analysis"]["status"], "abstained")

        # 对外解释层可以显示 supportive，但原始表必须继续保持采集时的 neutral。
        with self.database.session() as session:
            raw_event = session.get(MarketEventModel, "evt-accepted")
            self.assertEqual(raw_event.impact_direction, "neutral")
            self.assertNotEqual(raw_event.impact_reason, accepted["impact_reason"])

    async def test_unchanged_events_do_not_call_model_twice(self) -> None:
        first_result = ModelMarketEventBatch(judgments=[])
        with patch.object(
            self.service,
            "_invoke_model",
            new=AsyncMock(return_value=first_result),
        ):
            await self.service.analyze_pending()

        with patch.object(
            self.service,
            "_invoke_model",
            new=AsyncMock(),
        ) as model_call:
            second = await self.service.analyze_pending()

        self.assertEqual(second["requested_count"], 0)
        model_call.assert_not_awaited()

    async def test_oversized_model_lists_are_trimmed_without_failing_batch(
        self,
    ) -> None:
        """信息密集新闻多返回几个实体时，应保留整批结果并裁到业务上限。"""

        model_result = ModelMarketEventBatch(
            judgments=[
                ModelMarketEventJudgment(
                    event_id="evt-accepted",
                    entities=[f"主体{index}" for index in range(15)],
                    themes=[f"主题{index}" for index in range(12)],
                    direction="caution",
                    horizon="short_term",
                    severity=2,
                    relevance="related",
                    confidence=0.8,
                    reason="风险说明" * 200,
                    affected_fund_codes=["110020"] * 12,
                    uncertainties=[f"不确定性{index}" for index in range(9)],
                )
            ]
        )

        with patch.object(
            self.service,
            "_invoke_model",
            new=AsyncMock(return_value=model_result),
        ):
            result = await self.service.analyze_pending()

        public_event = next(
            item
            for item in MarketEventService(
                fixture_path=Path("/fixture/not-used.json"),
                repository=self.repository,
            ).search()["events"]
            if item["event_id"] == "evt-accepted"
        )
        semantic = public_event["semantic_analysis"]
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(result["abstained_count"], 1)
        self.assertEqual(len(semantic["entities"]), 10)
        self.assertEqual(len(semantic["themes"]), 8)
        self.assertLessEqual(len(semantic["reason"]), 500)
        self.assertEqual(semantic["affected_fund_codes"], ["110020"])
        self.assertEqual(len(semantic["uncertainties"]), 6)

    async def test_content_change_makes_old_analysis_pending_again(self) -> None:
        with patch.object(
            self.service,
            "_invoke_model",
            new=AsyncMock(return_value=ModelMarketEventBatch(judgments=[])),
        ):
            await self.service.analyze_pending()

        changed = sample_event(event_id="evt-accepted", content_digit="3")
        changed["summary"] = "原始来源内容已经发生变化。"
        self.repository.upsert(
            events=[changed],
            fetched_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        )

        self.assertEqual(self.repository.analysis_status()["pending_analysis_count"], 1)

    async def test_dynamic_fund_context_controls_python_whitelist(self) -> None:
        """新导入基金可以进入模型上下文，旧样例代码不能绕过动态白名单。"""

        dynamic_service = MarketEventAnalysisService(
            self.repository,
            fund_context_loader=lambda: [
                {
                    "fund_code": "008888",
                    "short_name": "华夏国证半导体芯片ETF联接C",
                    "fund_type": "股票型-标准指数",
                    "theme_keywords": ["半导体", "芯片", "集成电路"],
                }
            ],
        )
        model_result = ModelMarketEventBatch(
            judgments=[
                ModelMarketEventJudgment(
                    event_id="evt-accepted",
                    entities=["集成电路产业"],
                    themes=["半导体"],
                    direction="supportive",
                    horizon="medium_term",
                    severity=2,
                    relevance="direct",
                    confidence=0.9,
                    reason="事件与半导体主题直接相关。",
                    affected_fund_codes=["008888", "161725"],
                    uncertainties=[],
                )
            ]
        )

        with patch.object(
            dynamic_service,
            "_invoke_model",
            new=AsyncMock(return_value=model_result),
        ):
            await dynamic_service.analyze_pending()

        accepted = next(
            item
            for item in MarketEventService(
                fixture_path=Path("/fixture/not-used.json"),
                repository=self.repository,
            ).search()["events"]
            if item["event_id"] == "evt-accepted"
        )
        self.assertEqual(accepted["affected_fund_codes"], ["008888"])


class MarketEventAnalysisJobTests(unittest.TestCase):
    """验证 HTTP 背后的 JobRunner 会调用真实语义分析服务入口。"""

    def test_background_job_runs_analysis_service(self) -> None:
        background_jobs = BackgroundJobService()
        fake_result = {
            "summary": "市场事件语义分析完成：接受 1 条，弃权 1 条。",
            "workflow": "market_event_analysis",
        }
        try:
            with patch(
                "app.services.market_event_analysis.market_event_analysis_service.analyze_pending",
                new=AsyncMock(return_value=fake_result),
            ) as analysis_mock:
                background_jobs.start_job(
                    user_id="chen",
                    job_id="market-event-analysis-test",
                    title="分析市场事件",
                    job_type="market_event_analysis",
                )
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    job = background_jobs.get_job(
                        user_id="chen",
                        job_id="market-event-analysis-test",
                    )["job"]
                    if job["status"] in {"completed", "failed", "cancelled"}:
                        break
                    time.sleep(0.01)

            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["result"]["workflow"], "market_event_analysis")
            analysis_mock.assert_awaited_once_with()
        finally:
            background_jobs.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()

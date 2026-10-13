"""市场情报五阶段流水线的隔离、检查点恢复和 Cron 模式测试。

运行方式：

    conda run --no-capture-output -n dl \
      python -m unittest tests.test_market_intelligence_pipeline -v

所有外部数据源和模型都由 Fake 替代，不会访问网络或消耗 API Token。
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from app.services.background_jobs import BackgroundJobService
from app.services.cron_scheduler import CronSchedulerService
from app.services.market_event_sync import (
    MARKET_EVENT_SYNC_SCHEDULE_ID,
    MARKET_EVENT_SYNC_SYSTEM_USER_ID,
)
from app.services.market_intelligence_pipeline import (
    MarketIntelligencePipelineService,
)


CHEN_PORTFOLIO_ID = "portfolio_0123456789abcdef"
OTHER_PORTFOLIO_ID = "portfolio_fedcba9876543210"


class FakeSyncer:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def sync(self) -> dict:
        self.calls.append("sync")
        return {
            "workflow": "market_event_sync",
            "summary": "同步 2 条事件。",
            "fetched_count": 2,
            "inserted_count": 2,
        }

    def status(self) -> dict:
        self.calls.append("status")
        return {"pending_analysis_count": 0}


class FakeAnalyzer:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.index = 0

    async def analyze_pending(self, *, limit: int = 10) -> dict:
        self.calls.append(f"analyze:{limit}")
        self.index += 1
        if self.index == 1:
            return {
                "workflow": "market_event_analysis",
                "summary": "接受 1 条，弃权 1 条。",
                "requested_count": 2,
                "accepted_count": 1,
                "abstained_count": 1,
            }
        return {
            "workflow": "market_event_analysis",
            "summary": "没有待分析事件。",
            "requested_count": 0,
            "accepted_count": 0,
            "abstained_count": 0,
        }


class FakeTopicExpander:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def expand(self, *, topics, use_model: bool) -> dict:
        self.calls.append({"topics": list(topics), "use_model": use_model})
        return {
            "topics": list(topics),
            "keywords": ["半导体", "芯片", "HBM"],
            "warnings": [],
        }


class TopicAwareSyncer(FakeSyncer):
    def __init__(self, calls: list[str]) -> None:
        super().__init__(calls)
        self.keyword_expansion = None

    def sync(self, *, keyword_expansion=None) -> dict:
        self.keyword_expansion = keyword_expansion
        return super().sync()


class FakeClusterer:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def build(self) -> dict:
        self.calls.append("cluster")
        return {
            "workflow": "market_event_clustering",
            "summary": "2 条报道形成 1 个事件簇。",
            "event_count": 2,
            "cluster_count": 1,
        }


class FakeHotspotTracker:
    """确认聚类完成后才保存热点，且它不构成新的恢复阶段。"""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def capture(self) -> dict:
        self.calls.append("hotspot")
        return {
            "workflow": "market_hotspot_capture",
            "captured_count": 1,
            "summary": "保存 1 个热点快照。",
        }


class FakeImpactReader:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def analyze(self, *, user_id: str, portfolio_id: str, limit: int) -> dict:
        self.calls.append(f"impact:{user_id}:{portfolio_id}:{limit}")
        return {
            "portfolio_id": portfolio_id,
            "portfolio_name": "测试组合",
            "summary": "1 条事件与持仓形成关联。",
            "total_matched": 1,
            "caution_event_count": 1,
            "impacts": [
                {
                    "title": "制造业政策变化",
                    "direction": "caution",
                    "affected_exposure_pct": 12.5,
                }
            ],
        }


class FakeMonitoring:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.policies = [
            {
                "user_id": "chen",
                "portfolio_id": CHEN_PORTFOLIO_ID,
                "enabled": True,
            },
            {
                "user_id": "other",
                "portfolio_id": OTHER_PORTFOLIO_ID,
                "enabled": True,
            },
        ]
        self.monitor_calls: list[dict] = []

    def list_enabled_policies(self, *, user_id: str | None = None) -> list[dict]:
        self.calls.append(f"policies:{user_id or 'all'}")
        if user_id is None:
            return list(self.policies)
        return [item for item in self.policies if item["user_id"] == user_id]

    def run_monitor(self, **kwargs: object) -> dict:
        call = dict(kwargs)
        self.monitor_calls.append(call)
        self.calls.append(
            f"monitor:{call['user_id']}:{call['portfolio_id']}"
        )
        return {
            "status": "alerts_created",
            "summary": "生成 1 条预警。",
            "alert_count": 1,
            "alerts": [
                {
                    "id": "alert_0123456789abcdef",
                    "alert_type": "portfolio_event_exposure_changed",
                    "severity": "medium",
                    "title": "新的风险事件影响 12.50% 持仓",
                }
            ],
        }


class RecordingBackgroundJobs:
    def start_job(self, **_: object) -> dict:
        return {"job": {"id": "unused"}}


class MarketIntelligencePipelineServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []
        self.syncer = FakeSyncer(self.calls)
        self.analyzer = FakeAnalyzer(self.calls)
        self.clusterer = FakeClusterer(self.calls)
        self.hotspot_tracker = FakeHotspotTracker(self.calls)
        self.impacts = FakeImpactReader(self.calls)
        self.monitoring = FakeMonitoring(self.calls)
        self.service = MarketIntelligencePipelineService(
            syncer=self.syncer,
            analyzer=self.analyzer,
            clusterer=self.clusterer,
            hotspot_tracker=self.hotspot_tracker,
            impacts=self.impacts,
            monitoring=self.monitoring,
            max_analysis_batches=3,
        )

    async def test_manual_pipeline_runs_five_stages_and_is_user_scoped(self) -> None:
        checkpoints = []

        result = await self.service.run(
            run_id="job-pipeline-test",
            requesting_user_id="chen",
            stage_callback=lambda step, key, message, payload: checkpoints.append(
                (step, key, message, payload)
            ),
        )

        self.assertEqual(
            [item[1] for item in checkpoints],
            [
                "event_sync",
                "semantic_analysis",
                "event_clustering",
                "portfolio_impacts",
                "portfolio_monitoring",
            ],
        )
        self.assertIn("policies:chen", self.calls)
        self.assertLess(self.calls.index("cluster"), self.calls.index("hotspot"))
        self.assertEqual(
            result["stages"]["event_clustering"]["hotspot_tracking"]["captured_count"],
            1,
        )
        self.assertNotIn(f"impact:other:{OTHER_PORTFOLIO_ID}:30", self.calls)
        self.assertEqual(len(self.monitoring.monitor_calls), 1)
        monitor_call = self.monitoring.monitor_calls[0]
        self.assertEqual(monitor_call["user_id"], "chen")
        self.assertEqual(
            monitor_call["event_impact_report"]["total_matched"],
            1,
        )
        self.assertTrue(str(monitor_call["run_id"]).startswith("pipeline-"))
        self.assertEqual(result["monitored_portfolio_count"], 1)
        self.assertEqual(result["alert_count"], 1)
        self.assertEqual(result["status"], "completed")

    async def test_resume_skips_durable_stages_and_recomputes_transient_impacts(self) -> None:
        previous = {
            "event_sync": {"workflow": "market_event_sync"},
            "semantic_analysis": {
                "workflow": "market_event_analysis_batches",
                "processed_count": 2,
                "pending_analysis_count": 0,
            },
            "event_clustering": {
                "workflow": "market_event_clustering",
                "cluster_count": 1,
            },
        }
        checkpoints = []

        result = await self.service.run(
            run_id="job-pipeline-resume",
            requesting_user_id="chen",
            resume_from_step=3,
            previous_stage_results=previous,
            stage_callback=lambda step, key, message, payload: checkpoints.append(
                (step, key)
            ),
        )

        self.assertNotIn("sync", self.calls)
        self.assertFalse(any(item.startswith("analyze:") for item in self.calls))
        self.assertNotIn("cluster", self.calls)
        self.assertNotIn("hotspot", self.calls)
        self.assertEqual(
            checkpoints,
            [(4, "portfolio_impacts"), (5, "portfolio_monitoring")],
        )
        self.assertIn(f"impact:chen:{CHEN_PORTFOLIO_ID}:30", self.calls)
        self.assertEqual(result["alert_count"], 1)

    async def test_system_pipeline_can_process_all_enabled_users(self) -> None:
        result = await self.service.run(
            run_id="job-system-pipeline",
            requesting_user_id=MARKET_EVENT_SYNC_SYSTEM_USER_ID,
        )

        self.assertIn("policies:all", self.calls)
        self.assertEqual(result["monitored_portfolio_count"], 2)
        self.assertEqual(
            {item["user_id"] for item in self.monitoring.monitor_calls},
            {"chen", "other"},
        )

    async def test_selected_topic_is_expanded_before_event_sync(self) -> None:
        topic_expander = FakeTopicExpander()
        topic_syncer = TopicAwareSyncer(self.calls)
        service = MarketIntelligencePipelineService(
            syncer=topic_syncer,
            analyzer=self.analyzer,
            clusterer=self.clusterer,
            impacts=self.impacts,
            monitoring=self.monitoring,
            topic_expander=topic_expander,
            max_analysis_batches=3,
        )

        await service.run(
            run_id="job-topic-pipeline",
            requesting_user_id="chen",
            market_topics=["半导体"],
            use_model_topic_expansion=False,
        )

        self.assertEqual(
            topic_expander.calls,
            [{"topics": ["半导体"], "use_model": False}],
        )
        self.assertEqual(
            topic_syncer.keyword_expansion["keywords"],
            ["半导体", "芯片", "HBM"],
        )


class CheckpointThenRecoverPipeline:
    """第一次在第二阶段后失败，第二次必须从 SQL/Store 检查点继续。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run(self, **kwargs: object) -> dict:
        call = dict(kwargs)
        self.calls.append(call)
        callback = call["stage_callback"]
        if len(self.calls) == 1:
            callback(1, "event_sync", "同步完成", {"fetched_count": 2})
            callback(
                2,
                "semantic_analysis",
                "分析完成",
                {"processed_count": 2, "pending_analysis_count": 0},
            )
            raise RuntimeError("模拟聚类服务暂时不可用")

        self.assert_resume(call)
        callback(3, "event_clustering", "聚类完成", {"cluster_count": 1})
        callback(4, "portfolio_impacts", "归因完成", {"portfolio_count": 1})
        callback(
            5,
            "portfolio_monitoring",
            "监控完成",
            {"portfolio_count": 1, "alert_count": 0},
        )
        return {
            "workflow": "market_intelligence_pipeline",
            "status": "completed",
            "summary": "恢复后完成。",
            "stages": {
                **call["previous_stage_results"],
                "event_clustering": {"cluster_count": 1},
                "portfolio_impacts": {"portfolio_count": 1},
                "portfolio_monitoring": {
                    "portfolio_count": 1,
                    "alert_count": 0,
                },
            },
            "pending_analysis_count": 0,
            "monitored_portfolio_count": 1,
            "alert_count": 0,
        }

    @staticmethod
    def assert_resume(call: dict) -> None:
        if call["resume_from_step"] != 2:
            raise AssertionError("重试没有从第二阶段检查点继续")
        if set(call["previous_stage_results"]) != {
            "event_sync",
            "semantic_analysis",
        }:
            raise AssertionError("重试没有读取已持久化阶段结果")


class MarketIntelligencePipelineInfrastructureTests(unittest.TestCase):
    def test_background_job_retries_from_saved_stage_checkpoint(self) -> None:
        fake_pipeline = CheckpointThenRecoverPipeline()
        service = BackgroundJobService()
        try:
            with patch(
                "app.services.market_intelligence_pipeline."
                "market_intelligence_pipeline_service",
                fake_pipeline,
            ):
                accepted = service.start_job(
                    user_id="chen",
                    title="市场情报流水线",
                    job_type="market_intelligence_pipeline",
                    max_retries=1,
                    timeout_seconds=30,
                )
                job_id = accepted["job"]["id"]
                deadline = time.monotonic() + 4
                job = accepted["job"]
                while time.monotonic() < deadline:
                    job = service.get_job(user_id="chen", job_id=job_id)["job"]
                    if job["status"] in {"completed", "failed"}:
                        break
                    time.sleep(0.02)

            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["attempt_count"], 2)
            self.assertEqual(job["current_step"], 5)
            self.assertEqual(job["total_steps"], 5)
            self.assertEqual(len(fake_pipeline.calls), 2)
            self.assertEqual(job["result"]["summary"], "恢复后完成。")
        finally:
            service.shutdown(wait=True)

    def test_daily_schedule_replaces_legacy_sync_job_in_place(self) -> None:
        with TemporaryDirectory() as directory:
            scheduler = CronSchedulerService(
                root=Path(directory) / "schedules",
                background_jobs=RecordingBackgroundJobs(),  # type: ignore[arg-type]
            )
            scheduler.create_schedule(
                user_id=MARKET_EVENT_SYNC_SYSTEM_USER_ID,
                schedule_id=MARKET_EVENT_SYNC_SCHEDULE_ID,
                cron="30 20 * * 1-5",
                prompt="旧版只同步事件",
                job_type="market_event_sync",
                recurring=True,
                durable=True,
            )

            result = MarketIntelligencePipelineService().ensure_daily_schedule(
                scheduler
            )
            schedules = scheduler.list_schedules(
                user_id=MARKET_EVENT_SYNC_SYSTEM_USER_ID
            )["schedules"]

            self.assertTrue(result["created"])
            self.assertEqual(len(schedules), 1)
            self.assertEqual(schedules[0]["id"], MARKET_EVENT_SYNC_SCHEDULE_ID)
            self.assertEqual(
                schedules[0]["job_type"],
                "market_intelligence_pipeline",
            )

    def test_manual_mode_removes_persisted_schedule_idempotently(self) -> None:
        """关闭自动模式后必须删除旧文件，第二次启动也不能因为找不到而报错。"""

        with TemporaryDirectory() as directory:
            scheduler = CronSchedulerService(
                root=Path(directory) / "schedules",
                background_jobs=RecordingBackgroundJobs(),  # type: ignore[arg-type]
            )
            scheduler.create_schedule(
                user_id=MARKET_EVENT_SYNC_SYSTEM_USER_ID,
                schedule_id=MARKET_EVENT_SYNC_SCHEDULE_ID,
                cron="30 20 * * 1-5",
                prompt="自动更新市场情报",
                job_type="market_intelligence_pipeline",
                recurring=True,
                durable=True,
            )
            service = MarketIntelligencePipelineService()

            first = service.disable_daily_schedule(scheduler)
            second = service.disable_daily_schedule(scheduler)
            schedules = scheduler.list_schedules(
                user_id=MARKET_EVENT_SYNC_SYSTEM_USER_ID
            )

            self.assertTrue(first["removed"])
            self.assertFalse(second["removed"])
            self.assertEqual(schedules["count"], 0)
            self.assertEqual(schedules["schedules"], [])


if __name__ == "__main__":
    unittest.main()

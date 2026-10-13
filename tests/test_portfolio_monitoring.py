"""组合持续监控、Cron 投递和 Background Job 的确定性测试。

运行方式：

    conda run --no-capture-output -n dl \
      python -m unittest tests.test_portfolio_monitoring -v

测试用 Fake 依赖制造前后两份风险快照，不会访问网络或调用大模型。
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from app.database import Database
from app.services.background_jobs import BackgroundJobService
from app.services.cron_scheduler import CronSchedulerService
from app.services.portfolio_monitoring import PortfolioMonitoringService
from app.services.portfolios import PortfolioService


PORTFOLIO_ID_PATTERN = "portfolio_"
HOLDINGS = [
    {"fund_code": "110020", "weight_pct": 60},
    {"fund_code": "110007", "weight_pct": 40},
]


def assessment_snapshot(
    *,
    state_code: str,
    state_label: str,
    max_weight_pct: float,
    include_event: bool,
) -> dict:
    """构造 MonitoringService 真正读取的最小组合研判结果。"""

    risk_evidence = []
    if include_event:
        risk_evidence.append(
            {
                "evidence_id": "event:test:policy-change",
                "kind": "market_event",
                "title": "行业政策出现新的不确定性",
                "heat_score": 88.0,
                "decay_factor": 1.0,
                "cluster_id": "cluster_test_policy_change",
                "event_id": "event_test_policy_change",
                "data_date": "2026-08-29",
                "source_name": "测试权威来源",
                "source_url": "https://example.com/event",
                "affected_fund_codes": ["110020"],
            }
        )
    return {
        "assessment_date": "2026-08-29",
        "state_code": state_code,
        "state_label": state_label,
        "summary": f"当前组合状态为{state_label}。",
        "concentration": {
            "hhi": 0.52 if max_weight_pct >= 70 else 0.40,
            "max_weight_pct": max_weight_pct,
        },
        "score_breakdown": {
            "caution_points": 7.5 if state_code == "high_attention" else 3.0,
            "supportive_points": 1.0,
        },
        "risk_evidence": risk_evidence,
        "event_count": len(risk_evidence),
        "active_event_count": len(risk_evidence),
    }


def risk_snapshot(*, drawdown: float, volatility: float) -> dict:
    """构造 Riskfolio 适配层会返回的关键量化指标。"""

    return {
        "annualized_volatility_pct": volatility,
        "max_drawdown": {"drawdown_pct": drawdown},
        "daily_cvar_95_pct": 1.8,
        "period_start": "2025-08-29",
        "period_end": "2026-08-29",
        "top_risk_contributor": {"fund_code": "110020"},
    }


def event_impact_snapshot(
    *,
    include_event: bool,
    exposure_pct: float = 36.02,
    direction: str = "caution",
) -> dict:
    """构造持仓事件归因结果，模拟一条政策事件命中真实基金重仓。"""

    impacts = []
    if include_event:
        impacts.append(
            {
                "impact_evidence_id": (
                    "portfolio-impact:test:cluster_test_policy_change"
                ),
                "event_id": "event_test_policy_change",
                "cluster_id": "cluster_test_policy_change",
                "title": "行业政策出现新的不确定性",
                "event_date": "2026-08-29",
                "direction": direction,
                "severity": 3,
                "confidence": 0.92,
                "freshness_status": "fresh",
                "affected_exposure_pct": exposure_pct,
                "priority_score": 30.4,
                "match_basis": "fine_sector",
                "match_label": "匹配细分行业重仓",
                "source_name": "测试权威来源",
                "source_url": "https://example.com/event",
                "matched_securities": [],
                "matched_sectors": [
                    {
                        "sector_name": "先进制造",
                        "portfolio_exposure_pct": exposure_pct,
                    }
                ],
                "matched_industries": [],
                "matched_funds": [],
            }
        )
    return {
        "summary": f"当前有 {len(impacts)} 条事件与持仓形成可验证关联。",
        "event_data_cutoff": "2026-08-29",
        "quality": {"status": "good", "matched_event_count": len(impacts)},
        "impacts": impacts,
    }


class SequenceAssessmentReader:
    """每次调用依次返回一份快照，用来模拟风险随时间变化。"""

    def __init__(self, values: list[dict]) -> None:
        self.values = list(values)
        self.calls = 0

    def assess(self, **_: object) -> dict:
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return value


class SequenceRiskReader:
    def __init__(self, values: list[dict]) -> None:
        self.values = list(values)
        self.calls = 0

    def analyze(self, **_: object) -> dict:
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return value


class SequenceEventImpactReader:
    """按监控运行顺序返回两份持仓事件归因快照。"""

    def __init__(self, values: list[dict]) -> None:
        self.values = list(values)
        self.calls = 0

    def analyze(self, **_: object) -> dict:
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return value


class FakeExplanationService:
    """记录模型是否只在 Python 已命中规则后才被调用。"""

    def __init__(self) -> None:
        self.explain_calls = 0

    def explain(self, *, rule_alerts: list[dict], **_: object) -> dict:
        self.explain_calls += 1
        return {
            "status": "accepted",
            "model_used": True,
            "model": "fake-deepseek",
            "prompt_version": "test-v1",
            "generated_at": "2026-08-29T00:00:00+00:00",
            "headline": "组合风险变化需要关注",
            "summary": f"共有 {len(rule_alerts)} 类变化达到阈值。",
            "why_it_matters": ["风险状态与历史基线相比有所上升。"],
            "next_observations": ["重新运行组合研究核对证据。"],
            "fallback_reason": None,
        }

    @staticmethod
    def skipped() -> dict:
        return {
            "status": "skipped",
            "model_used": False,
            "model": None,
            "prompt_version": "test-v1",
            "generated_at": None,
            "headline": "",
            "summary": "",
            "why_it_matters": [],
            "next_observations": [],
            "fallback_reason": None,
        }


class RecordingBackgroundJobs:
    """Cron 测试只关心交付参数，不启动真实 Worker。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def start_job(self, **kwargs: object) -> dict:
        self.calls.append(dict(kwargs))
        return {"job": {"id": str(kwargs["job_id"])}}


class FakeMonitorRunner:
    """JobRunner 集成测试使用的监控 Service。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run_monitor(self, **kwargs: object) -> dict:
        self.calls.append(dict(kwargs))
        callback = kwargs.get("progress_callback")
        assert callable(callback)
        callback("assessment", "组合规则证据已更新")
        callback("quant_risk", "组合量化风险已更新")
        callback("event_impact", "持仓事件影响已更新")
        callback("persist", "风险快照与预警已持久化")
        return {
            "summary": "首份风险基线已保存。",
            "workflow": "portfolio_monitor",
            "status": "baseline_created",
            "portfolio_id": kwargs["portfolio_id"],
            "alerts": [],
        }


class PortfolioMonitoringServiceTests(unittest.TestCase):
    """验证 SQL 快照、变化比较、幂等和已读状态。"""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.database_url = (
            f"sqlite:///{Path(self.directory.name) / 'monitoring.sqlite3'}"
        )
        self.database = Database(self.database_url)
        self.portfolios = PortfolioService(self.database)
        self.portfolio = self.portfolios.create(
            user_id="chen",
            name="长期观察组合",
            holdings=HOLDINGS,
        )
        self.assessments = SequenceAssessmentReader(
            [
                assessment_snapshot(
                    state_code="mixed",
                    state_label="多空分化",
                    max_weight_pct=60,
                    include_event=False,
                ),
                assessment_snapshot(
                    state_code="high_attention",
                    state_label="高关注",
                    max_weight_pct=70,
                    include_event=True,
                ),
            ]
        )
        self.risks = SequenceRiskReader(
            [
                risk_snapshot(drawdown=8.0, volatility=12.0),
                risk_snapshot(drawdown=15.0, volatility=18.0),
            ]
        )
        self.event_impacts = SequenceEventImpactReader(
            [
                event_impact_snapshot(include_event=False),
                event_impact_snapshot(include_event=True),
            ]
        )
        self.explainer = FakeExplanationService()
        self.service = PortfolioMonitoringService(
            database=self.database,
            portfolios=self.portfolios,
            assessments=self.assessments,
            risks=self.risks,
            event_impacts=self.event_impacts,
            explainer=self.explainer,
        )
        self.policy = self.service.upsert_policy(
            user_id="chen",
            portfolio_id=self.portfolio["id"],
            enabled=True,
            risk_preference="conservative",
            investment_horizon="over_3y",
            monitor_months=12,
            max_single_adjustment_pct=10,
            drawdown_alert_pct_points=3,
            concentration_alert_pct_points=5,
            major_event_heat_threshold=70,
            event_impact_exposure_threshold_pct=5,
            alert_on_risk_state_worsening=True,
            schedule_cron="0 21 * * 1-5",
        )

    def tearDown(self) -> None:
        self.database.engine.dispose()
        self.directory.cleanup()

    def test_baseline_then_precise_event_alert_and_model_explanation(self) -> None:
        baseline = self.service.run_monitor(
            user_id="chen",
            portfolio_id=self.portfolio["id"],
            run_id="monitor-baseline",
        )
        changed = self.service.run_monitor(
            user_id="chen",
            portfolio_id=self.portfolio["id"],
            run_id="monitor-changed",
        )

        self.assertEqual(baseline["status"], "baseline_created")
        self.assertEqual(baseline["alert_count"], 0)
        self.assertEqual(changed["status"], "alerts_created")
        self.assertEqual(changed["alert_count"], 4)
        self.assertEqual(self.explainer.explain_calls, 1)
        self.assertEqual(
            {item["alert_type"] for item in changed["alerts"]},
            {
                "risk_state_worsened",
                "drawdown_expanded",
                "concentration_increased",
                "portfolio_event_exposure_changed",
            },
        )
        event_alert = next(
            item
            for item in changed["alerts"]
            if item["alert_type"] == "portfolio_event_exposure_changed"
        )
        self.assertEqual(
            event_alert["metric_data"]["highest_affected_exposure_pct"],
            36.02,
        )
        self.assertIn("影响组合 36.02%", event_alert["detail"])
        self.assertEqual(len(changed["snapshot"]["event_impacts"]), 1)
        self.assertFalse(changed["execution"]["auto_trade"])
        self.assertFalse(changed["execution"]["research_job_created"])

    def test_same_run_is_idempotent_and_alert_can_be_acknowledged(self) -> None:
        self.service.run_monitor(
            user_id="chen",
            portfolio_id=self.portfolio["id"],
            run_id="monitor-baseline",
        )
        first = self.service.run_monitor(
            user_id="chen",
            portfolio_id=self.portfolio["id"],
            run_id="monitor-changed",
        )
        repeated = self.service.run_monitor(
            user_id="chen",
            portfolio_id=self.portfolio["id"],
            run_id="monitor-changed",
        )

        self.assertEqual(repeated["snapshot"]["id"], first["snapshot"]["id"])
        self.assertEqual(self.assessments.calls, 2)
        self.assertEqual(self.risks.calls, 2)
        self.assertEqual(self.event_impacts.calls, 2)

        acknowledged = self.service.acknowledge_alert(
            user_id="chen",
            portfolio_id=self.portfolio["id"],
            alert_id=first["alerts"][0]["id"],
        )
        self.assertEqual(acknowledged["status"], "acknowledged")

        recreated = PortfolioMonitoringService(
            database=Database(self.database_url),
            portfolios=PortfolioService(Database(self.database_url)),
            assessments=self.assessments,
            risks=self.risks,
            event_impacts=self.event_impacts,
            explainer=self.explainer,
        )
        overview = recreated.get_overview(
            user_id="chen",
            portfolio_id=self.portfolio["id"],
        )
        self.assertEqual(overview["latest_snapshot"]["run_id"], "monitor-changed")
        self.assertEqual(len(overview["alerts"]), 4)

    def test_pipeline_can_reuse_precomputed_event_impact_report(self) -> None:
        """流水线第四阶段已经算过归因时，监控阶段不应再次读取归因服务。"""

        report = event_impact_snapshot(include_event=True)
        result = self.service.run_monitor(
            user_id="chen",
            portfolio_id=self.portfolio["id"],
            run_id="pipeline-precomputed-impact",
            event_impact_report=report,
        )

        self.assertEqual(self.event_impacts.calls, 0)
        self.assertEqual(len(result["snapshot"]["event_impacts"]), 1)

    def test_event_impact_alert_ignores_supportive_and_below_threshold(self) -> None:
        """支撑事件和低于用户阈值的小仓位关联都不应触发风险预警。"""

        previous = {
            "source_summary": {"event_impact_snapshot_version": 1},
            "event_impacts": [],
        }
        policy = {"event_impact_exposure_threshold_pct": 5.0}
        supportive = {
            "event_impacts": event_impact_snapshot(
                include_event=True,
                exposure_pct=40,
                direction="supportive",
            )["impacts"]
        }
        below_threshold = {
            "event_impacts": event_impact_snapshot(
                include_event=True,
                exposure_pct=4.99,
            )["impacts"]
        }

        self.assertIsNone(
            self.service._compare_event_impacts(
                previous=previous,
                current=supportive,
                policy=policy,
            )
        )
        self.assertIsNone(
            self.service._compare_event_impacts(
                previous=previous,
                current=below_threshold,
                policy=policy,
            )
        )

    def test_existing_event_only_alerts_after_material_exposure_increase(self) -> None:
        """同一事件的小幅波动不打扰用户，增加达到阈值后才重新提醒。"""

        previous = {
            "source_summary": {"event_impact_snapshot_version": 1},
            "event_impacts": event_impact_snapshot(
                include_event=True,
                exposure_pct=10,
            )["impacts"],
        }
        policy = {"event_impact_exposure_threshold_pct": 5.0}
        small_change = {
            "event_impacts": event_impact_snapshot(
                include_event=True,
                exposure_pct=14.99,
            )["impacts"]
        }
        material_change = {
            "event_impacts": event_impact_snapshot(
                include_event=True,
                exposure_pct=16,
            )["impacts"]
        }

        self.assertIsNone(
            self.service._compare_event_impacts(
                previous=previous,
                current=small_change,
                policy=policy,
            )
        )
        alert = self.service._compare_event_impacts(
            previous=previous,
            current=material_change,
            policy=policy,
        )
        self.assertIsNotNone(alert)
        self.assertEqual(
            alert["metric_data"]["events"][0]["change_type"],
            "exposure_increased",
        )
        self.assertEqual(
            alert["metric_data"]["events"][0][
                "exposure_change_pct_points"
            ],
            6.0,
        )

    def test_existing_event_alerts_when_exposure_first_crosses_threshold(self) -> None:
        """即使增幅较小，只要从阈值下方升到上方也应提醒一次。"""

        previous = {
            "source_summary": {"event_impact_snapshot_version": 1},
            "event_impacts": event_impact_snapshot(
                include_event=True,
                exposure_pct=4,
            )["impacts"],
        }
        current = {
            "event_impacts": event_impact_snapshot(
                include_event=True,
                exposure_pct=6,
            )["impacts"]
        }
        alert = self.service._compare_event_impacts(
            previous=previous,
            current=current,
            policy={"event_impact_exposure_threshold_pct": 5.0},
        )

        self.assertIsNotNone(alert)
        self.assertEqual(
            alert["metric_data"]["events"][0]["change_type"],
            "threshold_crossed",
        )
        self.assertIn("首次超过 5.00%", alert["detail"])


class PortfolioMonitoringInfrastructureTests(unittest.TestCase):
    """验证策略参数能穿过 Cron 和 Background Job 两层。"""

    def test_cron_persists_structured_payload_and_delivers_it(self) -> None:
        with TemporaryDirectory() as directory:
            background_jobs = RecordingBackgroundJobs()
            scheduler = CronSchedulerService(
                root=Path(directory) / "schedules",
                background_jobs=background_jobs,  # type: ignore[arg-type]
            )
            created = scheduler.create_schedule(
                user_id="chen",
                schedule_id="portfolio-monitor-test",
                cron="0 21 * * 1-5",
                prompt="监控长期观察组合",
                job_type="portfolio_monitor",
                job_payload={
                    "portfolio_id": "portfolio_0123456789abcdef",
                    "portfolio_months": 36,
                },
                durable=True,
            )
            self.assertEqual(
                created["schedule"]["job_payload"]["portfolio_months"],
                36,
            )

            restarted = CronSchedulerService(
                root=Path(directory) / "schedules",
                background_jobs=background_jobs,  # type: ignore[arg-type]
            )
            restarted.trigger_now(
                user_id="chen",
                schedule_id="portfolio-monitor-test",
            )
            delivery = restarted.process_next_event()

            self.assertEqual(delivery["status"], "delivered")
            self.assertEqual(background_jobs.calls[0]["job_type"], "portfolio_monitor")
            self.assertEqual(
                background_jobs.calls[0]["portfolio_id"],
                "portfolio_0123456789abcdef",
            )
            self.assertEqual(background_jobs.calls[0]["portfolio_months"], 36)

    def test_background_job_runner_executes_monitor_workflow(self) -> None:
        service = BackgroundJobService()
        fake_monitor = FakeMonitorRunner()
        try:
            with patch(
                "app.services.portfolio_monitoring.portfolio_monitoring_service",
                fake_monitor,
            ):
                accepted = service.start_job(
                    user_id="chen",
                    title="组合风险监控",
                    job_type="portfolio_monitor",
                    portfolio_id="portfolio_0123456789abcdef",
                    portfolio_months=12,
                )
                job_id = accepted["job"]["id"]
                deadline = time.monotonic() + 2
                job = accepted["job"]
                while time.monotonic() < deadline:
                    job = service.get_job(user_id="chen", job_id=job_id)["job"]
                    if job["status"] in {"completed", "failed"}:
                        break
                    time.sleep(0.01)

            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["current_step"], 4)
            self.assertEqual(job["result"]["workflow"], "portfolio_monitor")
            self.assertEqual(len(fake_monitor.calls), 1)
        finally:
            service.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()

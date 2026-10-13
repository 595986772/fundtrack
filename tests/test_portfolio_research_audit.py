"""组合研究变化审计的确定性测试。

运行：
    conda run --no-capture-output -n dl \
      python -m unittest tests.test_portfolio_research_audit -v
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.database import Database
from app.main import app
from app.models import BackgroundJobModel
from app.services.portfolio_research_audit import (
    PortfolioResearchAuditNotFoundError,
    PortfolioResearchAuditService,
    PortfolioResearchAuditValidationError,
    PortfolioResearchComparisonUnavailableError,
)
from app.services.portfolio_research_snapshots import (
    PortfolioResearchSnapshotService,
    public_snapshot_metadata,
)


USER_ID = "chen"
PORTFOLIO_ID = "portfolio_1234567890abcdef"


class FakeChangeExplainer:
    """不调用真实模型，只验证审计层传入的是有界差异事实。"""

    def __init__(self) -> None:
        self.last_change_set: dict | None = None

    def interpret(self, *, change_set: dict) -> dict:
        self.last_change_set = deepcopy(change_set)
        first = change_set["facts"][0]
        return {
            "status": "accepted",
            "model_used": True,
            "model": "fake-change-model",
            "prompt_version": "test-change-v1",
            "attempt_count": 1,
            "generated_at": "2026-09-01T12:00:00+00:00",
            "headline": "组合研究依据发生变化",
            "summary": "解释只引用后端已经确认的变化事实。",
            "drivers": [
                {
                    "tone": first["tone"],
                    "title": first["label"],
                    "detail": "这是测试解释。",
                    "fact_keys": [first["fact_key"]],
                    "references": [first],
                }
            ],
            "uncertainties": [],
            "follow_up_items": [],
            "fallback_reason": None,
        }

    def fallback(self, *, change_set: dict, reason: str) -> dict:
        self.last_change_set = deepcopy(change_set)
        return {
            "status": "fallback",
            "model_used": False,
            "model": "fake-change-model",
            "prompt_version": "test-change-v1",
            "attempt_count": 0,
            "generated_at": "2026-09-01T12:00:00+00:00",
            "headline": "确定性变化说明",
            "summary": reason,
            "drivers": [],
            "uncertainties": [],
            "follow_up_items": [],
            "fallback_reason": reason,
        }


class FakePortfolioService:
    def get(self, *, user_id: str, portfolio_id: str) -> dict:
        if user_id != USER_ID or portfolio_id != PORTFOLIO_ID:
            from app.services.portfolios import PortfolioNotFoundError

            raise PortfolioNotFoundError("组合不存在。")
        return {"id": portfolio_id, "name": "变化审计测试组合"}


def research_payload(
    *,
    first_weight: float = 60.0,
    volatility: float = 8.5,
    include_new_event: bool = False,
) -> dict:
    events = [
        {
            "event_id": "event-base",
            "event_date": "2026-08-28",
            "title": "基础市场事件",
            "impact_direction": "neutral",
            "impact_horizon": "short_term",
            "freshness_status": "fresh",
            "decay_factor": 1.0,
        }
    ]
    if include_new_event:
        events.append(
            {
                "event_id": "event-new",
                "event_date": "2026-09-01",
                "title": "新增行业风险事件",
                "impact_direction": "caution",
                "impact_horizon": "short_term",
                "freshness_status": "fresh",
                "decay_factor": 1.0,
            }
        )
    return {
        "portfolio": {
            "id": PORTFOLIO_ID,
            "name": "变化审计测试组合",
            "holdings": [
                {
                    "fund_code": "110020",
                    "short_name": "沪深300联接",
                    "weight_pct": first_weight,
                },
                {
                    "fund_code": "110007",
                    "short_name": "稳健债券",
                    "weight_pct": 100.0 - first_weight,
                },
            ],
        },
        "assessment": {
            "assessment_date": "2026-09-01",
            "state_code": "balanced",
            "state_label": "风险收益均衡",
            "score_breakdown": {
                "caution_points": 1.5,
                "supportive_points": 1.5,
            },
            "concentration": {
                "state_code": "balanced",
                "state_label": "适度集中",
                "max_weight_pct": first_weight,
                "effective_fund_count": 1.92,
            },
        },
        "quant_risk": {
            "period_return_pct": 4.2,
            "annualized_volatility_pct": volatility,
            "daily_cvar_95_pct": 1.15,
            "max_drawdown": {"drawdown_pct": 6.4},
        },
        "performance_snapshot": {
            "twr_pct": 4.2,
            "annualized_return_pct": 4.3,
            "annualized_volatility_pct": 8.5,
            "sharpe_ratio": 0.42,
            "sortino_ratio": 0.61,
            "max_drawdown": {"drawdown_pct": -6.4},
            "benchmark": {"excess_return_pct": 1.2},
        },
        "market_context": {
            "state_code": "balanced",
            "state_label": "震荡均衡",
            "relevant_event_count": len(events),
            "events": events,
        },
        "decision_history": {
            "decision_count": 0,
            "decisions": [],
        },
    }


class PortfolioResearchAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "audit.db"
        self.database = Database(f"sqlite:///{database_path}")
        self.database.create_schema()
        self.snapshots = PortfolioResearchSnapshotService(self.database)
        self.explainer = FakeChangeExplainer()
        self.service = PortfolioResearchAuditService(
            database=self.database,
            snapshots=self.snapshots,
            explainer=self.explainer,
        )
        self.base_time = datetime(2026, 8, 30, 8, tzinfo=timezone.utc)
        self._create_run(
            job_id="research-base",
            completed_at=self.base_time,
            payload=research_payload(),
            headline="组合状态保持均衡",
            model_state="balanced",
        )
        self._create_run(
            job_id="research-target",
            completed_at=self.base_time + timedelta(days=2),
            payload=research_payload(
                first_weight=70.0,
                volatility=11.2,
                include_new_event=True,
            ),
            headline="组合风险需要提高关注",
            model_state="cautious",
        )

    def tearDown(self) -> None:
        self.database.engine.dispose()
        self.temp_dir.cleanup()

    def _create_run(
        self,
        *,
        job_id: str,
        completed_at: datetime,
        payload: dict,
        headline: str,
        model_state: str,
        months: int = 12,
        user_id: str = USER_ID,
    ) -> None:
        snapshot = self.snapshots.create_once(
            run_id=job_id,
            user_id=user_id,
            portfolio_id=PORTFOLIO_ID,
            period_months=months,
            graph_version="portfolio-research-v6",
            capture_started_at=completed_at - timedelta(seconds=1),
            source_manifest={"source_dates": {"market": "2026-09-01"}},
            payload=payload,
        )
        report = {
            "data_kind": "langgraph_portfolio_research",
            "workflow": "portfolio_research",
            "graph_version": "portfolio-research-v6",
            "run_id": job_id,
            "thread_id": job_id,
            "portfolio_id": PORTFOLIO_ID,
            "portfolio_name": "变化审计测试组合",
            "period_months": months,
            "assessment_date": "2026-09-01",
            "headline": headline,
            "summary": f"{headline}的测试摘要。",
            "assessment_snapshot": {
                "state_code": payload["assessment"]["state_code"],
                "state_label": payload["assessment"]["state_label"],
            },
            "model_interpretation": {
                "current_state": model_state,
                "confidence": 0.8,
            },
            "generation": {
                "model_used": True,
                "prompt_version": "portfolio-research-synthesis-v2",
            },
            "research_snapshot": public_snapshot_metadata(snapshot),
        }
        record = BackgroundJobModel(
            user_id=user_id,
            id=job_id,
            title="组合研究",
            job_type="portfolio_research",
            status="completed",
            execution_backend="local",
            current_step=8,
            total_steps=8,
            progress_message="研究完成。",
            payload={"portfolio_id": PORTFOLIO_ID, "portfolio_months": months},
            result=report,
            error=None,
            cancel_requested=False,
            attempt_count=1,
            max_retries=2,
            timeout_seconds=300,
            idempotency_key=None,
            celery_task_id=None,
            created_at=completed_at - timedelta(minutes=1),
            updated_at=completed_at,
            started_at=completed_at - timedelta(seconds=10),
            completed_at=completed_at,
        )
        with self.database.session() as session, session.begin():
            session.add(record)

    def test_exact_report_read_validates_owner_portfolio_and_snapshot(self) -> None:
        report = self.service.read_report(user_id=USER_ID, portfolio_id=PORTFOLIO_ID, job_id="research-base")
        self.assertEqual(report["run_id"], "research-base")
        self.assertEqual(report["diagnosis"]["report_id"], "research-base")
        self.assertIsNone(self.explainer.last_change_set)
        history = self.service.list_runs(user_id=USER_ID, portfolio_id=PORTFOLIO_ID)
        self.assertEqual(history["latest_report"]["diagnosis"]["report_id"], "research-target")
        with self.database.session() as session:
            persisted = session.get(BackgroundJobModel, (USER_ID, "research-base"))
            self.assertNotIn("diagnosis", persisted.result)
        for user, portfolio in (("other-user", PORTFOLIO_ID), (USER_ID, "portfolio_other")):
            with self.assertRaises(PortfolioResearchAuditNotFoundError):
                self.service.read_report(user_id=user, portfolio_id=portfolio, job_id="research-base")
        # 即使报告 JSON 仍在，证据校验失败也不能作为可追问的冻结资料。
        with patch.object(self.service, "_snapshot_for_record", side_effect=PortfolioResearchAuditValidationError("证据损坏")):
            with self.assertRaises(PortfolioResearchAuditValidationError):
                self.service.read_report(user_id=USER_ID, portfolio_id=PORTFOLIO_ID, job_id="research-base")
        with patch("app.routers.portfolios.portfolio_research_audit_service", self.service), patch("app.routers.portfolios.portfolio_service", FakePortfolioService()):
            client = TestClient(app)
            response = client.get(f"/api/v1/portfolios/{PORTFOLIO_ID}/research-runs/research-base?user_id={USER_ID}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["run_id"], "research-base")
            self.assertEqual(client.get(f"/api/v1/portfolios/{PORTFOLIO_ID}/research-runs/research-base?user_id=other-user").status_code, 404)

    def test_history_restores_latest_report_and_keeps_rows_lightweight(self) -> None:
        history = self.service.list_runs(
            user_id=USER_ID,
            portfolio_id=PORTFOLIO_ID,
            months=12,
        )

        self.assertEqual(history["count"], 2)
        self.assertEqual(history["omitted_count"], 0)
        self.assertEqual(history["message"], "")
        self.assertEqual(history["runs"][0]["job_id"], "research-target")
        self.assertEqual(history["latest_report"]["run_id"], "research-target")
        self.assertNotIn("report", history["runs"][0])
        self.assertEqual(len(history["runs"][0]["content_hash"]), 64)

    def test_history_skips_legacy_report_without_breaking_newer_ui(self) -> None:
        original_get_for_run = self.snapshots.get_for_run

        def get_for_run(*, user_id: str, run_id: str):
            if run_id == "research-target":
                return None
            return original_get_for_run(user_id=user_id, run_id=run_id)

        with patch.object(self.snapshots, "get_for_run", side_effect=get_for_run):
            history = self.service.list_runs(
                user_id=USER_ID,
                portfolio_id=PORTFOLIO_ID,
                months=12,
            )

        self.assertEqual(history["count"], 1)
        self.assertEqual(history["omitted_count"], 1)
        self.assertIn("缺少不可变快照", history["message"])
        self.assertEqual(history["latest_report"]["run_id"], "research-base")

    def test_compare_finds_snapshot_metrics_holdings_events_and_conclusion(self) -> None:
        audit = self.service.compare(
            user_id=USER_ID,
            portfolio_id=PORTFOLIO_ID,
            target_job_id="research-target",
        )

        self.assertEqual(audit["base_run"]["job_id"], "research-base")
        self.assertEqual(audit["target_run"]["job_id"], "research-target")
        self.assertTrue(audit["input_changed"])
        self.assertTrue(audit["conclusion_changed"])
        self.assertEqual(audit["change_scope"], "broad")
        changed = {
            item["section"]
            for item in audit["changed_sections"]
            if item["changed"]
        }
        self.assertEqual(
            changed,
            {"portfolio", "assessment", "quant_risk", "market_context"},
        )
        fact_keys = {item["fact_key"] for item in audit["facts"]}
        self.assertIn("holding:110020:weight_pct", fact_keys)
        self.assertIn(
            "metric:quant_risk.annualized_volatility_pct",
            fact_keys,
        )
        self.assertIn("event:event-new:added", fact_keys)
        self.assertIn("conclusion:model_state", fact_keys)
        self.assertIsNotNone(self.explainer.last_change_set)
        # 模型输入边界由 change_set 体现，不包含两份完整 payload 或 report。
        self.assertNotIn("base_snapshot", self.explainer.last_change_set)
        self.assertNotIn("target_report", self.explainer.last_change_set)

    def test_identical_inputs_with_changed_wording_are_not_called_data_changes(self) -> None:
        self._create_run(
            job_id="research-wording",
            completed_at=self.base_time + timedelta(days=3),
            payload=research_payload(
                first_weight=70.0,
                volatility=11.2,
                include_new_event=True,
            ),
            headline="相同数据的另一种表达",
            model_state="cautious",
        )

        audit = self.service.compare(
            user_id=USER_ID,
            portfolio_id=PORTFOLIO_ID,
            target_job_id="research-wording",
            base_job_id="research-target",
        )

        self.assertFalse(audit["input_changed"])
        self.assertTrue(audit["conclusion_changed"])
        self.assertEqual(audit["change_scope"], "wording_only")
        self.assertEqual(audit["changed_section_count"], 0)

    def test_comparison_requires_an_older_report_in_the_same_period(self) -> None:
        with self.assertRaises(PortfolioResearchComparisonUnavailableError):
            self.service.compare(
                user_id=USER_ID,
                portfolio_id=PORTFOLIO_ID,
                target_job_id="research-base",
            )

        self._create_run(
            job_id="research-six-month",
            completed_at=self.base_time + timedelta(days=4),
            payload=research_payload(),
            headline="六个月研究",
            model_state="balanced",
            months=6,
        )
        with self.assertRaises(PortfolioResearchAuditValidationError):
            self.service.compare(
                user_id=USER_ID,
                portfolio_id=PORTFOLIO_ID,
                target_job_id="research-six-month",
                base_job_id="research-target",
            )

    def test_job_and_snapshot_are_isolated_by_user(self) -> None:
        with self.assertRaises(PortfolioResearchAuditNotFoundError):
            self.service.compare(
                user_id="other-user",
                portfolio_id=PORTFOLIO_ID,
                target_job_id="research-target",
            )

    def test_fastapi_history_and_comparison_endpoints_use_the_audit_service(self) -> None:
        client = TestClient(app)
        from app.routers import portfolios as portfolio_router

        with (
            patch.object(portfolio_router, "portfolio_service", FakePortfolioService()),
            patch.object(portfolio_router, "portfolio_research_audit_service", self.service),
        ):
            history_response = client.get(
                f"/api/v1/portfolios/{PORTFOLIO_ID}/research-runs",
                params={"user_id": USER_ID, "months": 12},
            )
            audit_response = client.get(
                f"/api/v1/portfolios/{PORTFOLIO_ID}/research-runs/"
                "research-target/comparison",
                params={"user_id": USER_ID},
            )

        self.assertEqual(history_response.status_code, 200)
        self.assertEqual(history_response.json()["count"], 2)
        self.assertEqual(audit_response.status_code, 200)
        self.assertEqual(
            audit_response.json()["data_kind"],
            "portfolio_research_change_audit",
        )


if __name__ == "__main__":
    unittest.main()

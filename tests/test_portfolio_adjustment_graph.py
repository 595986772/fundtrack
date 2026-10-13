"""组合调整 HITL LangGraph、Background Job 与 API 的确定性测试。

运行方式：

    conda run --no-capture-output -n dl \
      python -m unittest tests.test_portfolio_adjustment_graph -v

测试使用 Fake Generator，不会调用 DeepSeek、Embedding、AKShare 或任何网络服务。
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.background_jobs import BackgroundJobService
from app.services.job_store import BackgroundJobRecord, InMemoryJobStore, utc_now
from app.services.portfolio_adjustment_generation import (
    ModelPortfolioAdjustment,
    PortfolioAdjustmentGenerationService,
)
from app.services.portfolio_adjustment_graph import (
    PortfolioAdjustmentGraphService,
)
from app.services.portfolio_research_checkpoints import (
    create_portfolio_research_checkpoint_runtime,
)


PORTFOLIO_ID = "portfolio_0123456789abcdef"
SOURCE_JOB_ID = "portfolio-research-source"


def source_research_report() -> dict:
    """构造调整图真正会读取的最小研究报告。"""

    return {
        "workflow": "portfolio_research",
        "run_id": SOURCE_JOB_ID,
        "portfolio_id": PORTFOLIO_ID,
        "portfolio_name": "HITL 测试组合",
        "period_months": 12,
        "headline": "权益风险贡献较高，市场证据仍有分歧",
        "summary": "组合需要结合本人风险偏好再决定是否调整。",
        "assessment_snapshot": {
            "state_code": "cautious",
            "state_label": "谨慎观察",
            "fund_contributions": [
                {
                    "fund_code": "110020",
                    "short_name": "沪深300联接",
                    "weight_pct": 60.0,
                },
                {
                    "fund_code": "110007",
                    "short_name": "稳健债券",
                    "weight_pct": 40.0,
                },
            ],
        },
        "quant_risk_snapshot": {
            "annualized_volatility_pct": 12.5,
            "daily_cvar_95_pct": 1.4,
            "max_drawdown": {"drawdown_pct": 9.2},
            "top_risk_contributor": {"fund_code": "110020"},
        },
        "market_snapshot": {
            "data_date": "2026-08-28",
            "state_code": "mixed_weakening",
            "state_label": "分化走弱",
            "summary": "风险偏好回落。",
        },
        "key_findings": [
            {
                "tone": "caution",
                "title": "权益风险贡献集中",
                "detail": "沪深300联接贡献了主要组合波动。",
                "evidence_ids": ["risk:test:equity"],
                "metric_references": [],
                "fund_codes": ["110020"],
            }
        ],
        "model_interpretation": {
            "conditional_view": "若更看重回撤控制，可降低权益暴露。",
            "conflicts": ["长期收益与短期回撤控制存在取舍。"],
            "uncertainties": ["未来市场方向不可确定。"],
        },
        "limitations": ["历史数据不代表未来。"],
    }


class FakeAdjustmentGenerator:
    """固定生成一份满足用户最大调整边界的模型结果。"""

    def __init__(self) -> None:
        self.generate_calls = 0
        self.fallback_calls = 0

    def generate(self, *, research_context: dict, user_constraints: dict) -> dict:
        self.generate_calls += 1
        return {
            "status": "accepted",
            "model_used": True,
            "model": "fake-deepseek",
            "prompt_version": "test-v1",
            "attempt_count": 1,
            "fallback_reason": None,
            "stance": "reduce_risk",
            "confidence": 0.82,
            "headline": "小幅降低权益风险暴露",
            "summary": "在用户稳健偏好下，把 10 个百分点转向债券基金。",
            "adjustments": [
                {
                    "fund_code": "110020",
                    "short_name": "沪深300联接",
                    "action": "decrease",
                    "current_weight_pct": 60.0,
                    "target_weight_pct": 50.0,
                    "change_pct_points": -10.0,
                    "rationale": "降低主要风险贡献来源。",
                    "conditions": ["权益风险贡献仍显著偏高"],
                    "evidence_ids": ["risk:test:equity"],
                },
                {
                    "fund_code": "110007",
                    "short_name": "稳健债券",
                    "action": "increase",
                    "current_weight_pct": 40.0,
                    "target_weight_pct": 50.0,
                    "change_pct_points": 10.0,
                    "rationale": "平衡组合短期波动。",
                    "conditions": ["债券基金风险状态未明显恶化"],
                    "evidence_ids": [],
                },
            ],
            "key_tradeoffs": ["降低波动也可能牺牲权益上涨阶段的收益。"],
            "monitoring_items": ["继续观察权益基金风险贡献。"],
        }

    def fallback(
        self,
        *,
        research_context: dict,
        user_constraints: dict,
        reason: str,
    ) -> dict:
        self.fallback_calls += 1
        raise AssertionError("Fake Generator 不应进入 fallback")


class FakeScenarioValidator:
    """证明 Graph 会在候选生成后调用 Python 场景验证节点。"""

    def validate(self, **_: object) -> dict:
        return {
            "status": "completed",
            "execution_ready": False,
            "scenarios": [
                {
                    "scenario_id": "baseline",
                    "comparison_status": "passed",
                },
                {
                    "scenario_id": "model_proposal",
                    "comparison_status": "passed",
                    "objective_status": "met",
                },
            ],
            "comparison_facts": [
                {
                    "metric_key": "scenario:model_proposal:risk",
                    "scenario_id": "model_proposal",
                }
            ],
            "limitations": [],
        }


class FakeScenarioExplainer:
    def interpret(self, *, comparison: dict) -> dict:
        return {
            "status": "accepted",
            "model_used": True,
            "model": "fake-deepseek",
            "prompt_version": "test-scenario-v1",
            "attempt_count": 1,
            "fallback_reason": None,
            "headline": "Python 验证后的场景标题",
            "summary": "最终说明来自已经计算完成的场景事实。",
            "drivers": [],
            "tradeoffs": ["历史风险与收益暴露需要共同理解。"],
            "uncertainties": [],
            "follow_up_items": ["补充真实费用。"],
        }

    def fallback(self, *, comparison: dict, reason: str) -> dict:
        raise AssertionError("Fake Scenario Explainer 不应进入 fallback")


class FakePortfolioService:
    """Router 测试只需要确认组合属于 chen。"""

    def get(self, *, user_id: str, portfolio_id: str) -> dict:
        if user_id != "chen" or portfolio_id != PORTFOLIO_ID:
            raise AssertionError("测试传入了错误的用户或组合 ID")
        return {
            "id": portfolio_id,
            "user_id": user_id,
            "name": "HITL 测试组合",
            "holding_count": 2,
            "holdings": [],
        }


def human_review(*, decision: str = "continue") -> dict:
    return {
        "decision": decision,
        "risk_preference": "conservative",
        "investment_horizon": "one_to_three_years",
        "max_single_adjustment_pct": 15,
        "max_single_fund_weight_pct": 65,
        "max_turnover_pct": 12,
        "additional_constraints": "债券基金至少保留 30%",
    }


def completed_source_record() -> BackgroundJobRecord:
    """把研究结果直接放进 Store，测试重点就能集中在调整流程。"""

    now = utc_now()
    return BackgroundJobRecord(
        id=SOURCE_JOB_ID,
        user_id="chen",
        title="已完成组合研究",
        job_type="portfolio_research",
        status="completed",
        execution_backend="local",
        current_step=6,
        total_steps=6,
        progress_message="后台工作已完成。",
        payload={"portfolio_id": PORTFOLIO_ID, "portfolio_months": 12},
        result=source_research_report(),
        error=None,
        cancel_requested=False,
        attempt_count=1,
        max_retries=2,
        timeout_seconds=300,
        idempotency_key=None,
        celery_task_id=None,
        created_at=now,
        updated_at=now,
        started_at=now,
        completed_at=now,
    )


class PortfolioAdjustmentGraphTests(unittest.TestCase):
    def test_validated_explanation_replaces_candidate_wording(self) -> None:
        service = PortfolioAdjustmentGraphService(
            generator=FakeAdjustmentGenerator(),
            scenario_validator=FakeScenarioValidator(),
            scenario_explainer=FakeScenarioExplainer(),
        )
        service.run(
            run_id="adjustment-validated-run",
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            source_research_job_id=SOURCE_JOB_ID,
            source_report=source_research_report(),
        )
        completed = service.run(
            run_id="adjustment-validated-run",
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            source_research_job_id=SOURCE_JOB_ID,
            resume_payload=human_review(),
        )

        self.assertEqual(
            completed.result["headline"],
            "Python 验证后的场景标题",
        )
        self.assertEqual(
            completed.result["generation"]["candidate_headline"],
            "小幅降低权益风险暴露",
        )
        self.assertEqual(
            completed.result["scenario_validation"]["status"],
            "completed",
        )

    def test_sqlite_interrupt_survives_service_recreation_and_resumes(self) -> None:
        generator = FakeAdjustmentGenerator()
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "adjustment-checkpoints.sqlite3"
            database_url = f"sqlite:///{database_path}"

            runtime_1 = create_portfolio_research_checkpoint_runtime(
                backend="sqlite",
                database_url=database_url,
            )
            progress_before_pause: list[str] = []
            try:
                service_1 = PortfolioAdjustmentGraphService(
                    generator=generator,
                    checkpointer=runtime_1.checkpointer,
                    checkpoint_backend=runtime_1.backend,
                    checkpoint_durable=runtime_1.durable,
                )
                paused = service_1.run(
                    run_id="adjustment-durable-run",
                    user_id="chen",
                    portfolio_id=PORTFOLIO_ID,
                    source_research_job_id=SOURCE_JOB_ID,
                    source_report=source_research_report(),
                    progress_callback=lambda node, _message: (
                        progress_before_pause.append(node)
                    ),
                )
                self.assertEqual(paused.status, "waiting_input")
                self.assertEqual(progress_before_pause, ["prepare_review"])
                self.assertTrue(paused.result["graph_trace"]["checkpoint_durable"])
            finally:
                runtime_1.close()

            # 模拟 API/Worker 进程退出后重建 Service，再用同一个 thread_id 恢复。
            runtime_2 = create_portfolio_research_checkpoint_runtime(
                backend="sqlite",
                database_url=database_url,
            )
            progress_after_resume: list[str] = []
            try:
                service_2 = PortfolioAdjustmentGraphService(
                    generator=generator,
                    checkpointer=runtime_2.checkpointer,
                    checkpoint_backend=runtime_2.backend,
                    checkpoint_durable=runtime_2.durable,
                )
                completed = service_2.run(
                    run_id="adjustment-durable-run",
                    user_id="chen",
                    portfolio_id=PORTFOLIO_ID,
                    source_research_job_id=SOURCE_JOB_ID,
                    resume_payload=human_review(),
                    progress_callback=lambda node, _message: (
                        progress_after_resume.append(node)
                    ),
                )
            finally:
                runtime_2.close()

        self.assertEqual(completed.status, "completed")
        self.assertEqual(
            progress_after_resume,
            [
                "human_review",
                "generate_adjustment",
                "validate_scenarios",
                "compose_plan",
            ],
        )
        self.assertEqual(generator.generate_calls, 1)
        self.assertTrue(completed.result["graph_trace"]["resumed_from_checkpoint"])
        self.assertEqual(completed.result["execution"]["orders_created"], 0)
        self.assertEqual(completed.result["adjustments"][0]["target_weight_pct"], 50.0)
        self.assertEqual(
            completed.result["scenario_validation"]["status"],
            "information_insufficient",
        )

    def test_cancel_completes_without_calling_model(self) -> None:
        generator = FakeAdjustmentGenerator()
        service = PortfolioAdjustmentGraphService(generator=generator)
        paused = service.run(
            run_id="adjustment-cancel-run",
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            source_research_job_id=SOURCE_JOB_ID,
            source_report=source_research_report(),
        )
        completed = service.run(
            run_id="adjustment-cancel-run",
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            source_research_job_id=SOURCE_JOB_ID,
            resume_payload=human_review(decision="cancel"),
        )

        self.assertEqual(paused.status, "waiting_input")
        self.assertEqual(completed.result["status"], "cancelled_by_user")
        self.assertEqual(completed.result["generation"]["status"], "skipped")
        self.assertEqual(generator.generate_calls, 0)

    def test_python_rejects_model_change_beyond_human_limit(self) -> None:
        service = PortfolioAdjustmentGenerationService()
        model_result = ModelPortfolioAdjustment.model_validate(
            {
                "stance": "reduce_risk",
                "confidence": 0.8,
                "headline": "测试方案",
                "summary": "测试模型边界校验。",
                "adjustments": [
                    {
                        "fund_code": "110020",
                        "action": "decrease",
                        "target_weight_pct": 45,
                        "rationale": "降低风险。",
                        "evidence_ids": ["risk:test:equity"],
                    },
                    {
                        "fund_code": "110007",
                        "action": "increase",
                        "target_weight_pct": 55,
                        "rationale": "提高稳健配置。",
                    },
                ],
                "key_tradeoffs": ["可能降低上涨收益。"],
                "monitoring_items": ["观察风险贡献。"],
            }
        )
        context_service = PortfolioAdjustmentGraphService()
        context = context_service._compact_research_report(
            source_research_report()
        )

        with self.assertRaisesRegex(ValueError, "最大调整幅度"):
            service._validate_and_resolve(
                model_result=model_result,
                research_context=context,
                user_constraints={"max_single_adjustment_pct": 10},
                attempt_count=1,
            )


class PortfolioAdjustmentJobAndApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryJobStore()
        self.store.create(completed_source_record())
        self.jobs = BackgroundJobService(store=self.store)
        self.generator = FakeAdjustmentGenerator()
        self.graph = PortfolioAdjustmentGraphService(generator=self.generator)

    def tearDown(self) -> None:
        self.jobs.shutdown(wait=True)

    def wait_for_status(self, job_id: str, statuses: set[str]) -> dict:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.jobs.get_job(user_id="chen", job_id=job_id)["job"]
            if job["status"] in statuses:
                return job
            time.sleep(0.01)
        self.fail(f"组合调整 Job 未进入状态：{sorted(statuses)}")

    def test_job_releases_worker_then_resumes_same_record(self) -> None:
        from app.services import portfolio_adjustment_graph as graph_module

        with patch.object(
            graph_module,
            "portfolio_adjustment_graph_service",
            self.graph,
        ):
            started = self.jobs.start_job(
                user_id="chen",
                job_id="adjustment-job-test",
                title="组合调整测试",
                job_type="portfolio_adjustment",
                portfolio_id=PORTFOLIO_ID,
                source_research_job_id=SOURCE_JOB_ID,
            )
            paused = self.wait_for_status(
                started["job"]["id"],
                {"waiting_input", "failed"},
            )
            self.assertEqual(paused["status"], "waiting_input")

            self.jobs.resume_portfolio_adjustment(
                user_id="chen",
                job_id=started["job"]["id"],
                **human_review(),
            )
            completed = self.wait_for_status(
                started["job"]["id"],
                {"completed", "failed"},
            )

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["id"], paused["id"])
        self.assertEqual(completed["result"]["workflow"], "portfolio_adjustment")
        self.assertEqual(completed["current_step"], 5)
        self.assertEqual(
            completed["result"]["user_constraints"]["max_turnover_pct"],
            12.0,
        )

    def test_api_starts_and_resumes_adjustment(self) -> None:
        client = TestClient(app)
        from app.routers import portfolios as portfolio_router
        from app.services import portfolio_adjustment_graph as graph_module

        with (
            patch.object(
                portfolio_router,
                "portfolio_service",
                FakePortfolioService(),
            ),
            patch.object(
                portfolio_router,
                "background_job_service",
                self.jobs,
            ),
            patch.object(
                graph_module,
                "portfolio_adjustment_graph_service",
                self.graph,
            ),
        ):
            started_response = client.post(
                f"/api/v1/portfolios/{PORTFOLIO_ID}/adjustment-runs",
                json={
                    "user_id": "chen",
                    "source_research_job_id": SOURCE_JOB_ID,
                },
            )
            self.assertEqual(started_response.status_code, 202)
            job_id = started_response.json()["job"]["id"]
            paused = self.wait_for_status(job_id, {"waiting_input", "failed"})
            self.assertEqual(paused["status"], "waiting_input")

            resumed_response = client.post(
                f"/api/v1/portfolios/{PORTFOLIO_ID}/adjustment-runs/{job_id}/resume",
                json={"user_id": "chen", **human_review()},
            )
            self.assertEqual(resumed_response.status_code, 202)
            completed = self.wait_for_status(job_id, {"completed", "failed"})

        self.assertEqual(completed["status"], "completed")
        self.assertTrue(completed["result"]["graph_trace"]["human_in_the_loop"])


if __name__ == "__main__":
    unittest.main()

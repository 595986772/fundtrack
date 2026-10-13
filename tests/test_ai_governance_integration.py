"""治理与业务入口的集成测试，模型使用本地 MockTransport，不调用付费 API。"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from app.config import settings
from app.database import Database
from app.main import app
from app.schemas import AgentChatRequest
from app.services.ai_capacity import MemoryAICapacity
from app.services.ai_governance_context import AIControlError, AIExecutionScope, current_ai_scope
from app.services.ai_usage import AIUsageService
from app.services.auth import AuthService
from app.services.background_jobs import BackgroundJobService
from app.services.job_store import SqlJobStore
from app.services.job_runtime import JobRunner


class CapturingExecutor:
    backend_name = "test"

    def __init__(self):
        self.submitted = []

    def submit(self, *, user_id, job_id, timeout_seconds):
        self.submitted.append((user_id, job_id))
        return f"test:{job_id}"

    def cancel(self, **kwargs):
        pass

    def shutdown(self, **kwargs):
        pass


class JobAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = self.stack.enter_context(TemporaryDirectory())
        self.db = Database(f"sqlite:///{Path(root) / 'jobs.db'}")
        self.stack.callback(self.db.engine.dispose)
        self.store = SqlJobStore(self.db)
        self.executor = CapturingExecutor()
        self.service = BackgroundJobService(store=self.store, executor=self.executor)
        for key, value in {"ai_governance_enabled": True, "ai_global_pending_jobs": 3, "ai_user_pending_jobs": 2}.items():
            self.stack.enter_context(patch.object(settings, key, value))

    def start(self, job_id, user="alice", **kwargs):
        return self.service.start_job(user_id=user, title="offline", job_id=job_id, **kwargs)

    def test_queue_full_does_not_create_or_dispatch_new_job(self):
        self.start("a")
        self.start("b")
        with self.assertRaises(AIControlError):
            self.start("c")
        self.assertEqual(len(self.executor.submitted), 2)
        self.assertEqual(self.store.count(user_id="alice"), 2)

    def test_cancel_frees_admission_and_history_does_not_count(self):
        self.start("a")
        self.start("b")
        self.service.cancel_job(user_id="alice", job_id="a")
        self.start("c")
        self.assertEqual(self.store.count(user_id="alice"), 3)

    def test_user_and_global_pending_limits(self):
        self.start("a")
        self.start("b")
        self.start("c", user="bob")
        with self.assertRaises(AIControlError):
            self.start("d", user="carol")

    def test_idempotent_replay_works_even_when_queue_full(self):
        a = self.start("a", idempotency_key="same-job")
        self.start("b")
        b = self.start("another", idempotency_key="same-job")
        self.assertFalse(b["accepted"])
        self.assertEqual(a["job"]["id"], b["job"]["id"])
        self.assertEqual(len(self.executor.submitted), 2)

    def test_parallel_job_admission_is_atomic(self):
        def submit(i):
            try:
                self.start(f"j-{i}")
                return True
            except AIControlError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(submit, range(8))), 2)

    def test_waiting_input_resume_rechecks_queue_capacity(self):
        self.start("paused")
        self.store.update(user_id="alice", job_id="paused", changes={"status": "waiting_input"})
        self.start("a")
        self.start("b")
        with self.assertRaises(AIControlError):
            self.store.update(user_id="alice", job_id="paused", changes={"status": "queued"})
        self.assertEqual(self.store.get(user_id="alice", job_id="paused").status, "waiting_input")

    def test_worker_has_stable_owner_and_run_scope(self):
        self.start("scoped")
        captured = []
        def work(**kwargs):
            captured.append(current_ai_scope())
            return {"ok": True}
        runner = JobRunner(self.store)
        with patch.object(runner, "_run_demo_steps", side_effect=work):
            runner.run(user_id="alice", job_id="scoped")
            runner.run(user_id="alice", job_id="scoped")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].user_id, "alice")
        self.assertTrue(captured[0].run_id.startswith("job:"))

    def test_control_error_is_not_retried_by_local_worker(self):
        from app.services.job_executor import LocalJobExecutor
        self.start("rejected", max_retries=2)
        worker = LocalJobExecutor(self.store)
        def fail(**kwargs):
            raise AIControlError("token_budget_exceeded", "budget exhausted")
        with patch.object(worker.runner, "_run_demo_steps", side_effect=fail) as work:
            worker._execute_with_retry(user_id="alice", job_id="rejected")
        self.assertEqual(work.call_count, 1)
        self.assertEqual(self.store.get(user_id="alice", job_id="rejected").status, "failed")


class UsageApiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = self.stack.enter_context(TemporaryDirectory())
        self.db = Database(f"sqlite:///{Path(root) / 'api.db'}")
        self.stack.callback(self.db.engine.dispose)
        self.auth = AuthService(self.db)
        self.ledger = AIUsageService(self.db)
        _, self.alice = self.auth.register(user_id="alice", display_name="Alice", password="offline-password")
        _, self.bob = self.auth.register(user_id="bob", display_name="Bob", password="offline-password")
        self.capacity = MemoryAICapacity()
        self.stack.enter_context(patch("app.services.auth.auth_service", self.auth))
        self.stack.enter_context(patch("app.routers.ai_usage.get_ai_usage_service", return_value=self.ledger))
        self.stack.enter_context(patch("app.routers.ai_usage.get_ai_capacity", return_value=self.capacity))
        self.stack.enter_context(patch("app.services.rate_limits.get_ai_capacity", return_value=self.capacity))
        self.stack.enter_context(patch.object(settings, "auth_required", True))
        self.client = TestClient(app)
        self.stack.callback(self.client.close)

    def headers(self, token=None):
        return {"Authorization": f"Bearer {token or self.alice}"}

    def test_usage_reads_authenticated_owner_not_query_user(self):
        self.ledger.reserve(call_id="secret-bob-call", scope=AIExecutionScope("bob", "run:bob", "test"),
            provider="fake", model="fake", tokens=100, ttl_seconds=30)
        response = self.client.get("/api/v1/ai/usage?user_id=bob", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user_id"], "alice")
        self.assertNotIn("secret-bob-call", response.text)
        self.assertEqual(response.json()["control"]["backend"], "memory")

    def test_usage_requires_login_in_strict_mode(self):
        self.assertEqual(self.client.get("/api/v1/ai/usage?user_id=alice").status_code, 401)

    def test_usage_reconciliation_is_admin_only(self):
        response = self.client.post("/api/v1/ai/usage/calls/unknown/reconcile", headers=self.headers(self.bob),
            json={"user_id": "bob", "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "note": "verified invoice"})
        self.assertEqual(response.status_code, 403)

    def test_chat_returns_actionable_429_not_generic_502(self):
        with patch("app.routers.workspace.agent_chat_coordinator.chat", new=AsyncMock(
            side_effect=AIControlError("ai_queue_full", "AI 等待队列已满", retry_after=7))):
            response = self.client.post("/api/v1/agent/chat", headers=self.headers(),
                json={"user_id": "bob", "message": "hello"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "ai_queue_full")
        self.assertEqual(response.headers["retry-after"], "7")

    def test_sse_reports_capacity_error_and_finishes(self):
        with patch("app.routers.workspace.agent_chat_coordinator.chat", new=AsyncMock(
            side_effect=AIControlError("token_budget_exceeded", "今日额度不足"))):
            response = self.client.post("/api/v1/agent/chat/stream", headers=self.headers(),
                json={"user_id": "alice", "message": "hello"})
        self.assertEqual(response.status_code, 200)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertTrue(any(event.get("code") == "token_budget_exceeded" for event in events))
        self.assertIn("event: done", response.text)


class ControlPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_semantic_services_preserve_resource_rejection(self):
        from app.services.fund_semantic_review import FundSemanticReviewService
        from app.services.market_event_analysis import MarketEventAnalysisService
        from app.services.social_signals import SocialSignalAnalysisService

        cases = [
            (FundSemanticReviewService, {"baseline": {}, "events": [], "question": "test"}),
            (MarketEventAnalysisService, {"events": [], "fund_contexts": []}),
            (SocialSignalAnalysisService, {"posts": [], "fund_contexts": []}),
        ]
        for service_type, kwargs in cases:
            with self.subTest(service=service_type.__name__), ExitStack() as stack:
                # 本测试只检查拒绝传播，不启动采集，也不初始化服务的业务数据库。
                service = object.__new__(service_type)
                if hasattr(service, "_model_input"):
                    stack.enter_context(patch.object(service, "_model_input", return_value={}))
                factory = stack.enter_context(patch(f"{service_type.__module__}.get_chat_model"))
                rejected = AIControlError("token_budget_exceeded", "今日额度不足")
                invoke = AsyncMock(side_effect=rejected)
                factory.return_value.with_structured_output.return_value.ainvoke = invoke
                with self.assertRaises(AIControlError) as caught:
                    await service._invoke_model(**kwargs)
                self.assertIs(caught.exception, rejected)
                invoke.assert_awaited_once()

    def test_portfolio_services_do_not_retry_resource_rejection(self):
        from app.services.portfolio_research_interpretation import PortfolioResearchInterpretationService
        from app.services.portfolio_adjustment_generation import PortfolioAdjustmentGenerationService
        from app.services.portfolio_research_change_interpretation import PortfolioResearchChangeInterpretationService
        from app.services.portfolio_adjustment_scenario_interpretation import PortfolioAdjustmentScenarioInterpretationService
        from app.services.portfolio_allocation import parse_question

        cases = [
            (PortfolioResearchInterpretationService(), "interpret", {
                "portfolio": {}, "assessment": {}, "quant_risk": {}, "market_context": {}}),
            (PortfolioAdjustmentGenerationService(), "generate", {
                "research_context": {}, "user_constraints": {}}),
            (PortfolioResearchChangeInterpretationService(), "interpret", {"change_set": {"facts": [{}]}}),
            (PortfolioAdjustmentScenarioInterpretationService(), "interpret", {
                "comparison": {"status": "completed", "comparison_facts": [{}]}}),
        ]
        for service, method, kwargs in cases:
            with self.subTest(service=type(service).__name__), ExitStack() as stack:
                for name in ("_model_input", "_metric_catalog", "_evidence_catalog"):
                    if hasattr(service, name):
                        stack.enter_context(patch.object(service, name, return_value={}))
                factory = stack.enter_context(patch(f"{type(service).__module__}.get_chat_model"))
                rejected = AIControlError("ai_queue_full", "AI 等待队列已满")
                invoke = Mock(side_effect=rejected)
                factory.return_value.with_structured_output.return_value.invoke = invoke
                with self.assertRaises(AIControlError) as caught:
                    getattr(service, method)(**kwargs)
                self.assertIs(caught.exception, rejected)
                invoke.assert_called_once()
        with patch("app.services.portfolio_allocation.get_chat_model") as factory:
            invoke = factory.return_value.with_structured_output.return_value.invoke
            invoke.side_effect = AIControlError("ai_queue_full", "AI 等待队列已满")
            with self.assertRaises(AIControlError):
                parse_question("单只基金不超过三成")
            invoke.assert_called_once()

    async def test_report_runtime_does_not_hide_resource_error(self):
        from app.services.fund_report_runtime import ReportExecution
        async def work(deadline):
            raise AIControlError("ai_queue_full", "queue full")
        with self.assertRaises(AIControlError) as caught:
            await ReportExecution(run_id="test").run("synthesis", work)
        self.assertEqual(caught.exception.code, "ai_queue_full")

    async def test_recovery_never_retries_resource_error(self):
        from app.services.recovery import model_recovery_service
        model = AsyncMock()
        model.ainvoke.side_effect = AIControlError("token_budget_exceeded", "budget exhausted")
        with self.assertRaises(AIControlError):
            await model_recovery_service.invoke(runnable=model, messages=[], phase="agent_round", round_index=1, max_attempts=3)
        self.assertEqual(model.ainvoke.call_count, 1)


if __name__ == "__main__":
    unittest.main()

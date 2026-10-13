"""共享聊天时限：真实 Loop/协调器配合假工具和临时库，不访问收费模型。"""

import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.config import settings
from app.services.ai_governance_context import ai_execution_scope, AIControlError
from app.services.agent_chat_runtime import AgentChatCoordinator, AgentChatExecutionStore, ChatLockRegistry
from app.services.chat_execution_budget import (
    ChatExecutionBudget, ChatTimeBudgetExceeded, chat_budget_scope, chat_stage,
    chat_deadline, check_chat_active, current_chat_budget,
)
from app.services.recovery import ModelRecoveryService, ModelRecoveryError
from app.services import tool_agent as agent_module
from tests import test_agent_loop_guard as fixture


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_invalid_configuration(self):
        for seconds, reserve in ((0, 0), (2, 2), (1, -1), (float("inf"), 1)):
            with self.assertRaises(AIControlError):
                ChatExecutionBudget(seconds, reserve)

    async def test_stages_share_clock_and_no_new_operation_after_cutoff(self):
        budget = ChatExecutionBudget(.18, .06)
        with chat_budget_scope(budget):
            await chat_stage("first", lambda: asyncio.sleep(.04))
            with self.assertRaises(ChatTimeBudgetExceeded):
                await chat_stage("second", lambda: asyncio.sleep(1))
            forbidden = AsyncMock()
            with self.assertRaises(ChatTimeBudgetExceeded):
                await chat_stage("third", forbidden)
            forbidden.assert_not_awaited()
        self.assertLess(monotonic() - budget.started, .5)
        self.assertEqual([row["status"] for row in budget.stages], ["completed", "timeout", "timeout"])

    async def test_nested_stage_cannot_extend_parent_but_closing_can_use_reserve(self):
        budget = ChatExecutionBudget(1, .5)
        with chat_budget_scope(budget):
            async def inner():
                self.assertEqual(chat_deadline(), budget.work_deadline)
            await chat_stage("work", lambda: chat_stage("inner", inner, closing=True))
            budget.work_deadline = monotonic() - 1
            self.assertEqual(await chat_stage("closing", lambda: asyncio.sleep(0, result="done"), closing=True), "done")

    async def test_source_timeout_is_not_whole_request_timeout(self):
        async def failure():
            raise TimeoutError("source")
        with chat_budget_scope(ChatExecutionBudget(1, .1)) as budget:
            with self.assertRaises(TimeoutError):
                await chat_stage("source", failure)
        self.assertIsNone(budget.expired_phase)
        self.assertEqual(budget.stages[-1]["status"], "error")

    async def test_cancel_is_not_converted_to_timeout_or_success(self):
        with chat_budget_scope(ChatExecutionBudget(5, 1)) as budget:
            task = asyncio.create_task(chat_stage("tool", lambda: asyncio.sleep(10)))
            await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(budget.stages[-1]["status"], "cancelled")

    async def test_detached_thread_cannot_start_later_model_step(self):
        release, done = Event(), Event()
        errors = []
        def threaded_source():
            release.wait(2)
            try:
                check_chat_active()
            except ChatTimeBudgetExceeded:
                errors.append("blocked")
            finally:
                done.set()
        try:
            with chat_budget_scope(ChatExecutionBudget(.16, .06)):
                with self.assertRaises(ChatTimeBudgetExceeded):
                    await chat_stage("threaded_tool", lambda: asyncio.to_thread(threaded_source))
        finally:
            release.set()
            await asyncio.to_thread(done.wait, 2)
        self.assertEqual(errors, ["blocked"])

    def test_background_job_uses_its_own_run_not_chat_deadline(self):
        with chat_budget_scope(ChatExecutionBudget(1, .1)) as budget:
            budget.run_id = "chat-run"
            with ai_execution_scope("alice", "chat-run", "agent_chat"):
                self.assertIs(current_chat_budget(), budget)
                with ai_execution_scope("alice", "job-run", "background_job"):
                    self.assertIsNone(current_chat_budget())
                self.assertIs(current_chat_budget(), budget)
        self.assertIsNone(current_chat_budget())

    async def test_cancelled_lock_waiter_releases_only_acquired_locks(self):
        registry = ChatLockRegistry()
        entered = asyncio.Event()
        async with registry.acquire(["b"]):
            async def waiter():
                with chat_budget_scope(ChatExecutionBudget(.12, .02)):
                    async with registry.acquire(["a", "b"]):
                        entered.set()
            with self.assertRaises(ChatTimeBudgetExceeded):
                await waiter()
            self.assertFalse(entered.is_set())
            async with registry.acquire(["a"]):
                pass
            self.assertEqual(len(registry._entries), 1)
        self.assertEqual(registry._entries, {})

    async def test_model_retry_backoff_uses_shared_deadline(self):
        model = SimpleNamespace(ainvoke=AsyncMock(side_effect=TimeoutError("temporary")))
        recovery = ModelRecoveryService()
        with chat_budget_scope(ChatExecutionBudget(.2, .05)):
            # 剩余不足以重试时，沿用 Recovery 的明确失败，不重新发一个请求。
            with self.assertRaises(ModelRecoveryError):
                await chat_stage("model", lambda: recovery.invoke(runnable=model, messages=[],
                    phase="test", round_index=1, max_attempts=3, base_delay_seconds=.01))
        self.assertEqual(model.ainvoke.await_count, 1)

    async def test_never_started_model_when_deadline_already_expired(self):
        with chat_budget_scope(ChatExecutionBudget(1, .1)) as budget:
            budget.work_deadline = monotonic() - 1
            model = SimpleNamespace(ainvoke=AsyncMock())
            with self.assertRaises(ChatTimeBudgetExceeded):
                await ModelRecoveryService().invoke(runnable=model, messages=[], phase="test", round_index=1)
            model.ainvoke.assert_not_awaited()

    async def test_disabled_or_developer_entry_does_not_create_chat_budget(self):
        from app.services.chat_execution_budget import bounded_product_chat
        @bounded_product_chat
        async def entry(self, request, **kwargs):
            return current_chat_budget()
        with patch.object(settings, "agent_chat_budget_enabled", False):
            self.assertIsNone(await entry(None, fixture.request()))
        # 模式由后端上下文控制，不由模型参数控制。
        with patch.object(settings, "agent_chat_budget_enabled", True):
            context = SimpleNamespace(mode="developer")
            self.assertIsNone(await entry(None, fixture.request(), execution_context=context))

    def test_expired_retrieval_and_late_disclosure_do_not_continue(self):
        from app.services.fund_report_retrieval import retrieval_timeout
        from app.services.financial_research_repair import _refresh_disclosure, ResearchRepairBudget
        batch = SimpleNamespace(security_holdings=[SimpleNamespace(asset_type="stock")], industry_allocations=[],
            stock_report_date=None, bond_report_date=None, industry_report_date=None)
        with chat_budget_scope(ChatExecutionBudget(1, .1)) as budget:
            self.assertLessEqual(retrieval_timeout(25), .9)
            def late(*args, **kwargs):
                budget.work_deadline = monotonic() - 1
                return batch
            with patch("app.services.fund_disclosure_fetch.fetch_disclosure_bounded", side_effect=late), \
                 patch("app.services.fund_disclosures.fund_disclosure_repository") as repository:
                with self.assertRaises(ChatTimeBudgetExceeded):
                    _refresh_disclosure(fixture.CODES[0], ResearchRepairBudget())
                repository.replace_latest.assert_not_called()
            with self.assertRaises(ChatTimeBudgetExceeded):
                retrieval_timeout(25)


class LoopBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def run_slow_loop(self, *, parallel=False, cancel=False):
        harness = fixture.LoopIntegrationTests()
        service = agent_module.tool_calling_agent_service
        execute = service._execute_tool_call
        started = asyncio.Event()
        stopped = []
        async def delayed(**kwargs):
            if kwargs["tool_call"]["args"]["fund_code"] == fixture.CODES[1]:
                started.set()
                try:
                    await asyncio.sleep(5)
                finally:
                    stopped.append(True)
            return await execute(**kwargs)
        actions = ([harness.action(fixture.CODES[0]), harness.action(fixture.CODES[1], call_id="b")]
                   if parallel else [harness.action(fixture.CODES[1])])
        with chat_budget_scope(ChatExecutionBudget(.35, .1)), patch.object(service, "_execute_tool_call", side_effect=delayed):
            task = asyncio.create_task(harness.run_loop([actions],
                req=fixture.request(message="比较110020和110007的风险"), selected=fixture.route(fixture.RISK)))
            if cancel:
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                result = None
            else:
                result = await task
        self.assertTrue(stopped)
        self.assertEqual(len(harness.invocations), 1)
        return result

    async def test_slow_tool_produces_partial_not_fake_complete(self):
        result = await self.run_slow_loop()
        self.assertEqual(result.stop_reason, "time_budget_exhausted")
        self.assertIn("尚未取得", result.answer)
        self.assertEqual(result.loop_control.completion_status, "insufficient_evidence")
        self.assertTrue(result.execution_budget["expired_phase"].startswith("tool:"))

    async def test_parallel_fast_result_survives_other_source_timeout(self):
        result = await self.run_slow_loop(parallel=True)
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].tool_args["fund_code"], fixture.CODES[0])
        self.assertIn("已返回的资料", result.answer)
        self.assertEqual(result.usage.total_tokens, 15)

    async def test_user_cancel_does_not_create_timeout_answer(self):
        await self.run_slow_loop(parallel=True, cancel=True)

    async def test_success_retains_answer_and_budget_trace(self):
        harness = fixture.LoopIntegrationTests()
        with chat_budget_scope(ChatExecutionBudget(3, .2)):
            result = await harness.run_loop([[harness.action()], "历史风险资料已返回"])
        self.assertEqual(result.stop_reason, "model_finished")
        self.assertIsNone(result.execution_budget["expired_phase"])
        self.assertTrue(any(row["phase"] == "routing" for row in result.execution_budget["stages"]))

    async def test_model_timeout_returns_program_status_no_unchecked_draft(self):
        harness = fixture.LoopIntegrationTests()
        service = agent_module.tool_calling_agent_service
        async def slow(**kwargs):
            await asyncio.sleep(1)
        with chat_budget_scope(ChatExecutionBudget(.15, .03)), \
             patch.object(service, "_invoke_model_in_budget", new=slow):
            result = await harness.run_loop([])
        self.assertEqual(result.stop_reason, "time_budget_exhausted")
        self.assertIn("尚未形成完整结论", result.answer)

    async def test_reserved_time_only_used_for_tool_free_final_answer(self):
        harness = fixture.LoopIntegrationTests()
        compactor = agent_module.context_compactor_service
        with chat_budget_scope(ChatExecutionBudget(2, .5)) as budget:
            def after_tools(*args, **kwargs):
                budget.work_deadline = monotonic() - .01
                return []
            with patch.object(compactor, "compact_messages", side_effect=after_tools):
                result = await harness.run_loop([[harness.action()], "只读到一只基金的资料。"])
        self.assertEqual(result.stop_reason, "time_budget_exhausted")
        self.assertEqual(len(harness.reads), 1)
        self.assertEqual(harness.invocations[-1]["phase"], "forced_final_answer")
        self.assertEqual(result.execution_budget["expired_phase"], "work")
        self.assertIn("查询时间已用完", result.answer)

    async def test_model_recovery_receives_earlier_of_report_and_chat_deadlines(self):
        from app.services.fund_report_runtime import ReportExecution
        recovery = ModelRecoveryService()
        from langchain_core.messages import AIMessage
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="ok")))
        with chat_budget_scope(ChatExecutionBudget(2, .5)) as budget:
            execution = ReportExecution(run_id="test")
            seen = []
            async def operation(deadline):
                seen.append(deadline)
                return await recovery.invoke(runnable=model, messages=[], phase="test", round_index=1,
                                             deadline=deadline, max_attempts=1)
            with patch.object(recovery, "_invoke_with_retries", wraps=recovery._invoke_with_retries) as attempts:
                await chat_stage("report", lambda: execution.run("synthesis", operation))
                self.assertEqual(attempts.call_args.kwargs["deadline"], budget.work_deadline)
            self.assertLess(budget.work_deadline, seen[0])
            self.assertEqual(model.ainvoke.await_count, 1)


class CoordinatorBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.database import Database
        from app.services.comprehensive_agent import ComprehensiveAgentService
        from app.services.conversations import ConversationService
        from app.services.observability import AgentObservabilityService
        from tests.test_conversations import FakeAgent, FakeRuntimeState
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(TemporaryDirectory()))
        self.database = Database(f"sqlite:///{root / 'chat.db'}")
        self.addCleanup(self.database.engine.dispose)
        self.conversations = ConversationService(self.database)
        self.agent = FakeAgent()
        self.service = ComprehensiveAgentService(agent=self.agent, conversations=self.conversations,
            observability=AgentObservabilityService(self.database), runtime_state=FakeRuntimeState())
        self.coordinator = AgentChatCoordinator(agent=self.service, store=AgentChatExecutionStore(self.database))
        for name, value in {"agent_task_authorization_enabled": False, "agent_chat_timeout_seconds": 2,
                            "agent_chat_finalize_reserve_seconds": .2, "agent_chat_budget_enabled": True}.items():
            self.stack.enter_context(patch.object(settings, name, value))

    async def test_partial_answer_is_persisted_and_same_key_replayed(self):
        from app.services.chat_execution_budget import LoopProgress, partial_loop_response
        async def partial(request, **kwargs):
            budget = current_chat_budget()
            self.assertIsNotNone(budget)
            budget.expired_phase = "tool:test"
            return partial_loop_response(request, LoopProgress(), None)
        with patch.object(self.agent, "chat", new=AsyncMock(side_effect=partial)) as call:
            req = fixture.request(idempotency_key="deadline-test")
            first = await self.coordinator.chat(req)
            second = await self.coordinator.chat(req)
        self.assertEqual(call.await_count, 1)
        self.assertTrue(second.replayed)
        metadata = first.response.assistant_message.run_metadata
        self.assertEqual(metadata["stop_reason"], "time_budget_exhausted")
        self.assertEqual(metadata["execution_budget"]["expired_phase"], "tool:test")
        self.assertEqual(first.response.conversation.message_count, 2)

    async def test_slow_preparation_stops_before_agent_and_no_exchange_saved(self):
        async def slow(**kwargs):
            await asyncio.sleep(5)
        with patch.object(settings, "agent_chat_timeout_seconds", .25), patch.object(settings, "agent_chat_finalize_reserve_seconds", .05), \
             patch.object(self.service.investment_context, "prepare", new=slow):
            with self.assertRaises(ChatTimeBudgetExceeded):
                await self.coordinator.chat(fixture.request())
        self.assertEqual(self.agent.call_count, 0)
        self.assertEqual(self.conversations.list(user_id="loop-test")[0]["message_count"], 0)

    async def test_cancelled_chat_does_not_save_exchange_and_releases_scope(self):
        entered = asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await asyncio.sleep(5)
        with patch.object(self.agent, "chat", new=slow):
            task = asyncio.create_task(self.coordinator.chat(fixture.request(idempotency_key="cancel-test")))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.conversations.list(user_id="loop-test")[0]["message_count"], 0)
        self.assertFalse(self.coordinator.locks._entries)
        self.assertIsNone(current_chat_budget())

    async def test_sse_delivers_partial_answer_and_diagnostics(self):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.services.chat_execution_budget import LoopProgress, partial_loop_response
        async def partial(request, **kwargs):
            return partial_loop_response(request, LoopProgress(), None)
        with patch.object(self.agent, "chat", new=partial):
            coordinated = await self.coordinator.chat(fixture.request())
        # 这里验证 SSE 序列化，不依赖开发机的登录开关；身份隔离另有专项测试。
        with patch.object(settings, "auth_required", False), patch("app.routers.workspace.agent_chat_coordinator.chat", new=AsyncMock(return_value=coordinated)):
            with TestClient(app).stream("POST", "/api/v1/agent/chat/stream",
                json=fixture.request().model_dump(mode="json")) as response:
                self.assertEqual(response.status_code, 200)
                frames = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]
        self.assertEqual("".join(row.get("delta", "") for row in frames), coordinated.response.run.answer, frames)
        result = next(row for row in frames if "assistant_message" in row)
        self.assertEqual(result["run"]["stop_reason"], "time_budget_exhausted")
        self.assertEqual(result["run"]["execution_budget"]["budget_seconds"], 2)


class BudgetGovernanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_resource_queue_timeout_never_sends_model_request(self):
        from app.services.governed_model import async_model_permit
        capacity = SimpleNamespace(consume_rate=Mock(return_value=(True, 0, 0)),
            acquire=Mock(return_value="waiting"), release=Mock())
        ledger, sent = Mock(), []
        async def generate():
            async with async_model_permit(provider="fake", model="fake", payload={}, timeout=90):
                sent.append(True)
        with patch("app.services.governed_model.get_ai_capacity", return_value=capacity), \
             patch("app.services.governed_model.get_ai_usage_service", return_value=ledger), \
             chat_budget_scope(ChatExecutionBudget(.25, .05)):
            with self.assertRaises(ChatTimeBudgetExceeded):
                await chat_stage("model_queue", generate)
        self.assertFalse(sent)
        ledger.reserve.assert_not_called()
        ledger.mark_started.assert_not_called()
        capacity.release.assert_called_once()

    async def test_inflight_timeout_keeps_unknown_usage_and_releases_capacity(self):
        import httpx
        from app.database import Database
        from app.services.ai_capacity import MemoryAICapacity
        from app.services.ai_usage import AIUsageService
        from app.services.governed_model import GovernedChatOpenAI
        from tests.test_ai_governance import model_response
        async def slow(request):
            await asyncio.sleep(5)
            return model_response()
        with TemporaryDirectory() as root:
            database = Database(f"sqlite:///{root}/usage.db")
            self.addCleanup(database.engine.dispose)
            ledger, capacity = AIUsageService(database), MemoryAICapacity()
            async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
                model = GovernedChatOpenAI(model="fake", api_key="offline-placeholder", max_tokens=10,
                    base_url="https://model.invalid/v1", http_async_client=client)
                with patch("app.services.governed_model.get_ai_capacity", return_value=capacity), \
                     patch("app.services.governed_model.get_ai_usage_service", return_value=ledger), \
                     ai_execution_scope("deadline-test", "deadline-run", "test"), \
                     chat_budget_scope(ChatExecutionBudget(.35, .08)):
                    with self.assertRaises(ChatTimeBudgetExceeded):
                        await chat_stage("model", lambda: model.ainvoke("hello"))
            self.assertEqual(ledger.summary(user_id="deadline-test")["calls"][0]["status"], "unknown")
            self.assertEqual(capacity.snapshot("deadline-test")["running"], 0)


if __name__ == "__main__":
    unittest.main()

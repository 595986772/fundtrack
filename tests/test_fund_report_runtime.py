"""季报执行预算离线回归：用毫秒级假等待验证，不调用付费模型。"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.config import settings
from app.database import Database
from app.services.fund_report_runtime import ReportExecution, ReportExecutionError, report_synthesis_scope, synthesis_mode
from app.services.fund_report_query import supports_evidence_synthesis
from app.services.recovery import ModelRecoveryService, ModelRecoveryError
from tests import test_fund_report_answer as answer_tests


class SynthesisRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """真的走恢复层，用毫秒级模型验证兜底没有暗中多领预算或增加工具调用。"""

    async def invoke(self, primary, fallback, *, mode="manager", max_attempts=2, ready=True):
        import app.services.tool_agent as module
        message = ToolMessage(content="same-visible-evidence", tool_call_id="frozen-report")
        with patch.object(settings, "fund_report_timeout_seconds", .2):
            runtime = ReportExecution(run_id="fallback", query_mode=mode)
        runtime.evidence_ready = ready
        calls = []
        async def invoke_recovery(**kwargs):
            calls.append(kwargs)
            return await ModelRecoveryService().invoke(**kwargs)
        with (
            patch.object(module, "_REPORT_REPAIR_TIMEOUT_SECONDS", .02),
            patch.object(module, "_REPORT_SYNTHESIS_FALLBACK_SECONDS", .08),
            patch.object(module, "_REPORT_MIN_RETRY_SECONDS", .003),
            patch.object(module, "get_chat_model", return_value=fallback) as factory,
            patch.object(module.model_recovery_service, "invoke", side_effect=invoke_recovery),
        ):
            result = await module.ToolCallingAgentService()._invoke_model(report_execution=runtime,
                runnable=primary, messages=[message], phase="agent_round", round_index=2,
                max_attempts=max_attempts, continuation_runnable=primary)
        return result, runtime, calls, factory, message

    async def test_timeout_fallback_keeps_same_evidence_and_original_deadline(self):
        primary, fallback = AsyncMock(), AsyncMock()
        cancelled = asyncio.Event()
        async def hang(_messages):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        primary.ainvoke.side_effect = hang
        fallback.ainvoke.return_value = AIMessage(content="checked later",
            usage_metadata={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15})
        result, runtime, calls, factory, message = await self.invoke(primary, fallback)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result.response.content, "checked later")
        self.assertEqual([row["attempt"] for row in runtime.attempts], [1, 2])
        self.assertEqual([row["strategy"] for row in runtime.attempts], ["primary", "timeout_non_thinking"])
        self.assertIs(calls[0]["messages"], calls[1]["messages"])
        self.assertIs(calls[1]["messages"][0], message)
        self.assertEqual(calls[1]["max_attempts"], 1)
        self.assertFalse(calls[1]["allow_continuation"])
        self.assertAlmostEqual(calls[1]["deadline"], runtime.deadline - .02)
        self.assertAlmostEqual(calls[1]["deadline"] - calls[0]["deadline"], .08)
        self.assertFalse(factory.call_args.kwargs["thinking"])
        self.assertEqual(runtime.snapshot()["usage"]["total_tokens"], 15)
        # 超时模型可能仍计费；没有供应商usage时不能冒充已知的零消耗。
        self.assertFalse(runtime.snapshot()["usage_complete"])
        self.assertEqual(runtime.synthesis_thinking, "model_default_then_disabled")
        self.assertEqual(result.traces[-1].action, "report_synthesis_without_thinking")

    async def test_success_never_creates_a_second_model(self):
        primary, fallback = AsyncMock(), AsyncMock()
        primary.ainvoke.return_value = AIMessage(content="done")
        result, runtime, calls, factory, _ = await self.invoke(primary, fallback)
        self.assertEqual(result.response.content, "done")
        self.assertEqual(len(calls), 1)
        factory.assert_not_called()
        self.assertEqual(runtime.synthesis_thinking, "model_default")

    async def test_full_view_unready_and_single_attempt_never_reserve_fallback(self):
        for options in ({"mode": "full"}, {"ready": False}, {"max_attempts": 1}):
            primary, fallback = AsyncMock(), AsyncMock()
            primary.ainvoke.return_value = AIMessage(content="done")
            _, runtime, calls, factory, _ = await self.invoke(primary, fallback, **options)
            self.assertAlmostEqual(calls[0]["deadline"], runtime.deadline - .02)
            factory.assert_not_called()

    async def test_cancel_and_account_failures_are_not_retried_as_synthesis(self):
        from app.services.errors import AgentConfigError
        for error in (asyncio.CancelledError(), AgentConfigError("configuration missing"), ValueError("invalid input")):
            with self.subTest(error=type(error).__name__):
                primary, fallback = AsyncMock(), AsyncMock()
                primary.ainvoke.side_effect = error
                with self.assertRaises((asyncio.CancelledError, AgentConfigError, ReportExecutionError)):
                    await self.invoke(primary, fallback)
                fallback.ainvoke.assert_not_called()

    async def test_both_attempts_timeout_without_a_third_call_or_fresh_budget(self):
        primary, fallback = AsyncMock(), AsyncMock()
        async def hang(_messages):
            await asyncio.Event().wait()
        primary.ainvoke.side_effect = fallback.ainvoke.side_effect = hang
        started = monotonic()
        with self.assertRaises(ReportExecutionError) as error:
            await self.invoke(primary, fallback)
        execution = error.exception.execution_diagnostics
        self.assertEqual(primary.ainvoke.call_count, 1)
        self.assertEqual(fallback.ainvoke.call_count, 1)
        self.assertEqual([row["attempt"] for row in execution["model_attempts"]], [1, 2])
        self.assertEqual(execution["status"], "timeout")
        self.assertLess(monotonic() - started, .4)
        self.assertFalse(execution["usage_complete"])

    async def test_already_used_attempt_limit_does_not_create_fallback(self):
        import app.services.tool_agent as module
        runtime = ReportExecution(run_id="used", query_mode="manager")
        runtime.evidence_ready = True
        async def used_both(**kwargs):
            for attempt in (1, 2):
                kwargs["attempt_callback"]({"attempt": attempt, "status": "timeout", "usage": None})
            raise ModelRecoveryError(phase="agent_round", round_index=2, original_error=TimeoutError(),
                traces=[], compact_traces=[])
        with patch.object(module.model_recovery_service, "invoke", side_effect=used_both) as recovery, \
             patch.object(module, "get_chat_model") as factory:
            with self.assertRaises(ReportExecutionError):
                await module.ToolCallingAgentService()._invoke_model(report_execution=runtime,
                    runnable=AsyncMock(), messages=[], phase="agent_round", round_index=2, max_attempts=2)
        self.assertEqual(recovery.call_count, 1)
        factory.assert_not_called()


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_attempt_usage_and_lengths_without_text_or_thinking(self):
        response = AIMessage(content="private-draft", additional_kwargs={"reasoning_content": "private-reasoning"},
            usage_metadata={"input_tokens": 10, "output_tokens": 8, "total_tokens": 18,
                "output_token_details": {"reasoning": 5}}, response_metadata={"finish_reason": "stop"})
        model = AsyncMock()
        model.ainvoke.return_value = response
        execution = ReportExecution(run_id="metrics")
        await ModelRecoveryService().invoke(runnable=model, messages=[HumanMessage(content="private-prompt")],
            phase="synthesis", round_index=2, attempt_callback=execution.record_attempt)
        trace = execution.snapshot()
        self.assertEqual(trace["usage"]["total_tokens"], 18)
        self.assertTrue(trace["usage_complete"])
        attempt = trace["model_attempts"][0]
        self.assertEqual(attempt["input_chars"], len("private-prompt"))
        self.assertEqual(attempt["output_chars"], len("private-draft"))
        self.assertEqual(attempt["usage"]["reasoning_tokens"], 5)
        self.assertNotIn("private-", json.dumps(trace))
        execution.record_attempt({"phase": "repair", "usage": None})
        self.assertFalse(execution.snapshot()["usage_complete"])
        self.assertEqual(execution.snapshot()["usage"]["total_tokens"], 18)

    async def test_deadline_stops_wait_and_does_not_start_another_attempt(self):
        model = AsyncMock()
        cancelled = asyncio.Event()
        async def hang(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        model.ainvoke.side_effect = hang
        attempts = []
        with self.assertRaises(ModelRecoveryError) as error:
            await ModelRecoveryService().invoke(runnable=model, messages=[], phase="test", round_index=1,
                deadline=monotonic() + .02, attempt_callback=attempts.append, max_attempts=2)
        self.assertTrue(error.exception.is_timeout)
        self.assertEqual(model.ainvoke.call_count, 1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(attempts[0]["status"], "timeout")
        self.assertEqual(error.exception.traces[-1].action, "deadline_exhausted")

    async def test_fast_transient_failure_can_retry_with_the_same_deadline(self):
        model = AsyncMock()
        model.ainvoke.side_effect = [TimeoutError("temporary"), AIMessage(content="ok")]
        attempts = []
        result = await ModelRecoveryService().invoke(runnable=model, messages=[], phase="test", round_index=1,
            deadline=monotonic() + 10, attempt_callback=attempts.append, max_attempts=2, base_delay_seconds=0)
        self.assertEqual(result.response.content, "ok")
        self.assertEqual([row["attempt"] for row in attempts], [1, 2])
        self.assertEqual([row["status"] for row in attempts], ["timeout", "ok"])

    async def test_low_remaining_time_skips_retry_and_expired_budget_never_calls(self):
        model = AsyncMock()
        model.ainvoke.side_effect = TimeoutError("temporary")
        for remaining, calls in ((1, 1), (-1, 0)):
            model.reset_mock()
            with self.assertRaises(ModelRecoveryError):
                await ModelRecoveryService().invoke(runnable=model, messages=[], phase="test", round_index=1,
                    deadline=monotonic() + remaining, max_attempts=2)
            self.assertEqual(model.ainvoke.call_count, calls)

    async def test_stages_share_budget_reserve_repair_and_never_leak_error_text(self):
        with patch.object(settings, "fund_report_timeout_seconds", .08):
            runtime = ReportExecution(run_id="test-runtime")
        async def retrieval(_deadline):
            await asyncio.sleep(.015)
        await runtime.run("retrieval", retrieval, reserve_seconds=.02)
        async def synthesis(_deadline):
            await asyncio.Event().wait()
        with self.assertRaises(ReportExecutionError) as error:
            await runtime.run("synthesis", synthesis, reserve_seconds=.02)
        trace = error.exception.execution_diagnostics
        self.assertEqual([row["phase"] for row in trace["stages"]], ["prepare", "retrieval", "synthesis"])
        self.assertEqual(trace["stages"][-1]["status"], "timeout")
        self.assertLess(trace["stages"][-1]["allotted_ms"], 60)
        self.assertIn("整理季报分析", str(error.exception))
        async def bad(_deadline):
            raise RuntimeError("sensitive-provider-body")
        with self.assertRaises(ReportExecutionError) as failure:
            await runtime.run("repair", bad)
        self.assertNotIn("sensitive-provider-body", str(failure.exception))
        self.assertNotIn("sensitive-provider-body", json.dumps(failure.exception.execution_diagnostics))

    async def test_cancellation_propagates_instead_of_turning_into_a_repair(self):
        runtime = ReportExecution(run_id="cancel")
        async def cancel(_deadline):
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await runtime.run("synthesis", cancel)
        self.assertEqual(runtime.stages[-1]["status"], "cancelled")

    async def test_billing_failure_has_actionable_message_without_provider_body(self):
        class BillingError(Exception):
            status_code = 402
        async def fail(_deadline):
            raise ModelRecoveryError(phase="agent_round", round_index=1,
                original_error=BillingError("sensitive-provider-body"), traces=[], compact_traces=[])
        with self.assertRaises(ReportExecutionError) as error:
            await ReportExecution(run_id="billing-test").run("model_decision", fail)
        self.assertIn("余额不足", str(error.exception))
        self.assertNotIn("sensitive-provider-body", str(error.exception))
        stage = error.exception.execution_diagnostics["stages"][-1]
        self.assertEqual(stage["http_status"], 402)
        self.assertEqual(stage["error_type"], "BillingError")

    async def test_mode_scope_isolated_between_concurrent_requests(self):
        async def request(mode):
            with report_synthesis_scope(mode):
                await asyncio.sleep(0)
                return synthesis_mode()
        before = synthesis_mode()
        self.assertEqual(await asyncio.gather(request("standard"), request("evidence")), ["standard", "evidence"])
        self.assertEqual(synthesis_mode(), before)


class ReportRuntimeLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_and_final_errors_remain_distinct_after_repair(self):
        for repair, expected in (("valid", "corrected"), ("invalid", "rejected"), ("timeout", "failed")):
            result, _, _ = await answer_tests.ReportAnswerLoopTests().run_loop(broken=True, repair=repair)
            initial, final = result.report_answer_check.execution["validation_checks"]
            self.assertEqual(initial["checkpoint"], "initial")
            self.assertTrue(initial["issues"])
            self.assertEqual(final["repair_status"], expected)
            self.assertEqual(final["passed"], repair == "valid")
            self.assertNotIn("Q2全篇未见", json.dumps([initial, final], ensure_ascii=False))

    async def test_balanced_keeps_thinking_without_changing_quote_protocol(self):
        for max_rounds in (1, 3):
            result, _, _ = await answer_tests.ReportAnswerLoopTests().run_loop(mode="balanced", max_rounds=max_rounds)
            self.assertEqual(result.report_answer_check.status, "consistent")
            self.assertEqual(result.report_answer_check.execution["synthesis_thinking"], "enabled_low")
        # 固定数字题和混合推理题不被这一实验开关改变。
        result, _, _ = await answer_tests.ReportAnswerLoopTests().run_loop(mode="balanced", question="固定收益投资比例是多少？")
        self.assertEqual(result.report_answer_check.execution["synthesis_thinking"], "disabled")
        result, _, _ = await answer_tests.ReportAnswerLoopTests().run_loop(mode="balanced", question="为什么权益投资占比提高？")
        self.assertEqual(result.report_answer_check.execution["synthesis_thinking"], "model_default")

    async def test_modes_keep_identical_tool_evidence_and_checked_sources(self):
        harness = answer_tests.ReportAnswerLoopTests()
        standard, _, _ = await harness.run_loop(mode="standard")
        for max_rounds in (1, 3):
            candidate, phases, _ = await harness.run_loop(mode="evidence", max_rounds=max_rounds)
            self.assertEqual(candidate.answer, standard.answer)
            self.assertEqual(candidate.tool_calls[0].tool_result, standard.tool_calls[0].tool_result)
            execution = candidate.report_answer_check.execution
            self.assertEqual(execution["synthesis_thinking"], "disabled")
            self.assertTrue(execution["evidence_ready"])
            self.assertEqual([row["phase"] for row in execution["stages"]],
                ["prepare", "model_decision", "retrieval", "synthesis", "validation", "validation"])

    async def test_reasoning_or_unknown_questions_keep_original_mode(self):
        for question in ("经理两期为何加仓，解释原因", "比较经理策略，是否适合我买入", "帮我做研究", "预测经理未来策略"):
            self.assertFalse(supports_evidence_synthesis(question))
        result, _, _ = await answer_tests.ReportAnswerLoopTests().run_loop(mode="evidence", question="经理两期为何加仓，解释原因")
        self.assertEqual(result.report_answer_check.execution["synthesis_thinking"], "model_default")

    async def test_model_timeout_preserves_stage_and_does_not_attempt_correction(self):
        with patch("app.services.tool_agent._REPORT_REPAIR_TIMEOUT_SECONDS", .01), patch.object(settings, "fund_report_timeout_seconds", .06):
            with self.assertRaises(ReportExecutionError) as error:
                await answer_tests.ReportAnswerLoopTests().run_loop(hang_phase="synthesis")
        self.assertTrue(error.exception.execution_diagnostics["evidence_ready"])
        self.assertEqual(error.exception.execution_diagnostics["stages"][-1]["phase"], "synthesis")
        self.assertFalse(any(row["phase"] == "repair" for row in error.exception.execution_diagnostics["stages"]))

    async def test_failed_repair_retains_trace_but_not_bad_draft(self):
        with patch("app.services.tool_agent._REPORT_REPAIR_TIMEOUT_SECONDS", .01):
            result, _, _ = await answer_tests.ReportAnswerLoopTests().run_loop(broken=True, repair="hanging", mode="evidence")
        self.assertEqual(result.report_answer_check.status, "limited")
        self.assertEqual(result.report_answer_check.execution["status"], "limited")
        self.assertTrue(any(row["phase"] == "repair" and row["status"] == "timeout" for row in result.report_answer_check.execution["stages"]))
        self.assertNotIn("全篇未见", result.answer)
        self.assertIn("引用修正已超时", result.answer)

    async def test_failed_job_trace_is_stored_without_saving_a_chat_answer(self):
        from app.models import AgentChatExecutionModel
        from app.services.agent_chat_runtime import AgentChatExecutionStore
        with TemporaryDirectory() as directory:
            database = Database(f"sqlite:///{Path(directory) / 'failure.db'}")
            database.create_schema()
            try:
                with database.session_factory() as session:
                    now = datetime.now(timezone.utc)
                    session.add(AgentChatExecutionModel(user_id="test", idempotency_key="k", created_at=now, updated_at=now,
                        status="running", response_payload={"research_repair_budget": {"used": 1}}))
                    session.commit()
                store = AgentChatExecutionStore(database)
                store.fail(user_id="test", idempotency_key="k", error="timeout", execution_diagnostics={"status": "timeout"})
                with database.session_factory() as session:
                    record = session.get(AgentChatExecutionModel, {"user_id": "test", "idempotency_key": "k"})
                    self.assertEqual(record.status, "failed")
                    self.assertEqual(record.response_payload["report_execution"], {"status": "timeout"})
                    self.assertEqual(record.response_payload["research_repair_budget"], {"used": 1})
            finally:
                database.engine.dispose()

    async def test_success_timebook_is_saved_in_existing_observability_metrics(self):
        from app.services.observability import AgentObservabilityService
        result, _, _ = await answer_tests.ReportAnswerLoopTests().run_loop(mode="evidence")
        with TemporaryDirectory() as directory:
            database = Database(f"sqlite:///{Path(directory) / 'metrics.db'}")
            try:
                service = AgentObservabilityService(database)
                service.record_run(run=result, user_id=result.user_id, source="chat", duration_ms=12)
                saved = service.recent_runs(user_id=result.user_id)[0]
                self.assertEqual(saved.metrics["report_execution"], result.report_answer_check.execution)
            finally:
                database.engine.dispose()

    async def test_eval_failure_keeps_diagnostics_without_a_fake_quality_score(self):
        from app.services.fund_report_evals import FundReportEvalService
        service = FundReportEvalService()
        execution = ReportExecution(run_id="failed-eval")
        failure = ReportExecutionError(execution, "synthesis", timed_out=True)
        with patch.object(service, "_execute_case", new=AsyncMock(side_effect=failure)):
            result = await service._run_case(scenario={"id": "test-case", "name": "test"}, requested_by="test",
                eval_user_id="test", eval_run_id="test-eval", data_profile="fixed", data_fingerprint="fixed")
        self.assertFalse(result.passed)
        self.assertEqual(result.execution_diagnostics["status"], "timeout")
        self.assertEqual(result.report_quality, {})

    async def test_insufficient_repair_time_does_not_start_a_model_request(self):
        from app.schemas import AgentChatRequest
        from app.services.tool_agent import ToolCallingAgentService
        from langchain_core.messages import ToolMessage
        message = ToolMessage(content=json.dumps(answer_tests.payload()), tool_call_id="test")
        execution = ReportExecution(run_id="no-repair")
        execution.deadline = monotonic() + 1
        with patch("app.services.tool_agent.get_chat_model") as factory:
            _, report = await ToolCallingAgentService()._check_report_answer(check=answer_tests.checker(), message=message,
                request=AgentChatRequest(user_id="test", message="比较经理观点", fund_code=answer_tests.CODE,
                    fund_report_ids=answer_tests.IDS), answer="bad draft", messages=[message], round_index=2,
                usage={}, recovery_trace=[], context_trace=[], pipeline_steps=[], progress_callback=None, execution=execution)
        factory.assert_not_called()
        self.assertEqual(report.repair_attempts, 0)
        self.assertEqual(report.status, "limited")
        self.assertTrue(any("未发起引用修正" in issue for issue in report.issues))

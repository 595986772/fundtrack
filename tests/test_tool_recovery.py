"""工具恢复验收：真实执行器/Loop，假来源和假模型，不消耗 API 或改用户数据。"""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeoutError
from pydantic import ValidationError

from app.services.agent_authorization import AgentAuthorizationError
from app.services.agent_execution_context import AgentExecutionContext
from app.services.agent_loop_guard import LoopGuard, loop_guard_scope
from app.services.agent_tool_executor import agent_tool_executor
from app.services.ai_governance_context import AIControlError, ai_execution_scope
from app.services.chat_execution_budget import ChatExecutionBudget, ChatTimeBudgetExceeded, chat_budget_scope
from app.services.context_compactor import context_compactor_service
from app.services.fund_agent_tools import FundRiskToolInput, get_fund_risk_tool, get_fund_profile_tool
from app.services.funds import FundNotFoundError
from app.services.fund_risk import FundRiskError, FundRiskService
from app.services.portfolio_performance import PortfolioPerformanceService, PortfolioPerformanceError
from app.services.portfolio_agent_tools import get_portfolio_performance_tool
from app.services.tool_recovery import (
    ToolRecoveryState, classify_exception, tool_recovery_scope,
    current_tool_recovery,
)
from tests import test_agent_loop_guard as loop_fixture
from tests.test_agent_loop_guard import request, route, risk, RISK, NEWS, CODES


def status_error(status, retry_after=None):
    req = httpx.Request("GET", "https://source.invalid/?token=not-for-model")
    response = httpx.Response(status, request=req,
        headers={"Retry-After": retry_after} if retry_after else {})
    return httpx.HTTPStatusError("source error with secret", request=req, response=response)


class ToolRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.req = request()
        self.context = AgentExecutionContext.product(user_id=self.req.user_id)
        self.steps = []
        self.authorizer = SimpleNamespace(authorize_tool=Mock(), check_context=Mock())
        # 默认只替换外部授权调用，具体权限拒绝用单独测试控制。
        self.patches = [
            patch("app.services.agent_tool_executor.run_pre_tool_use_hooks", return_value=(True, "", [])),
            patch("app.services.agent_tool_executor.get_task_authorization_service", return_value=self.authorizer),
        ]
        self.pre = self.patches[0].start()
        self.patches[1].start()
        for item in self.patches:
            self.addCleanup(item.stop)

    def tool(self, results, name=RISK):
        async def run(**args):
            self.steps.append((name, args))
            result = results.pop(0)
            if isinstance(result, BaseException):
                raise result
            if callable(result):
                return await result()
            return result if isinstance(result, str) else json.dumps(result)
        return StructuredTool.from_function(coroutine=run, name=name,
            description="本地测试工具", args_schema=FundRiskToolInput)

    async def execute(self, tool, *, args=None, call_id="c1"):
        return await agent_tool_executor.execute(tool_call={"name": tool.name,
            "args": args if args is not None else {"fund_code": CODES[0]}, "id": call_id},
            tool_map={tool.name: tool}, request=self.req, execution_context=self.context,
            tool_index=1, ToolMessage=ToolMessage)

    async def test_transient_then_success_only_one_tool_message(self):
        tool = self.tool([httpx.ReadTimeout("private"), risk()])
        with tool_recovery_scope():
            call, message, hooks = await self.execute(tool)
        self.assertFalse(call.is_error)
        self.assertEqual(len(self.steps), 2)
        self.assertEqual(message.tool_call_id, "c1")
        self.assertEqual(self.pre.call_count, 2)
        self.assertEqual([s["attempt"] for s in call.recovery_trace if s["action"] == "attempt"], [1, 2])
        self.assertTrue(any(h.hook_name == "tool_recovery" for h in hooks))

    async def test_exhausted_retry_cannot_reset_by_changing_call_id_or_defaults(self):
        tool = self.tool([TimeoutError(), TimeoutError(), risk()])
        with tool_recovery_scope():
            first = await self.execute(tool)
            second = await self.execute(tool, args={"fund_code": CODES[0], "months": 12}, call_id="c2")
            await self.execute(tool, args={"fund_code": CODES[0], "ignored": "cannot-reset-retry"}, call_id="c3")
        self.assertEqual(len(self.steps), 2)
        self.assertTrue(first[0].is_error)
        self.assertFalse(json.loads(first[1].content)["retryable"])
        self.assertEqual(json.loads(second[1].content)["error_code"], "source_timeout")
        self.assertEqual(second[1].tool_call_id, "c2")
        self.assertEqual(second[0].recovery_trace[0]["action"], "blocked_duplicate")

    async def test_pool_timeout_is_recoverable_but_arbitrary_sql_error_is_not(self):
        call, _, _ = await self.execute(self.tool([PoolTimeoutError("pool full"), risk()]))
        self.assertFalse(call.is_error)
        self.assertEqual(len(self.steps), 2)
        sql_error = DBAPIError("SELECT ...", None, RuntimeError("syntax"))
        self.assertEqual(classify_exception(sql_error).code, "internal_error")
        disconnected = DBAPIError("SELECT ...", None, RuntimeError("closed"), connection_invalidated=True)
        self.assertEqual(classify_exception(disconnected).code, "source_unavailable")

    async def test_parallel_duplicates_share_failed_attempt_budget(self):
        tool = self.tool([TimeoutError(), TimeoutError(), risk()])
        with tool_recovery_scope():
            results = await asyncio.gather(self.execute(tool), self.execute(tool, call_id="other"))
        self.assertEqual(len(self.steps), 2)
        self.assertTrue(all(r[0].is_error for r in results))

    async def test_no_retry_for_writes_mcp_or_dedicated_repair(self):
        for name in ("write_note_tool", "mcp__test__read", "get_fund_research_brief_tool",
                     "start_portfolio_research_tool", "get_fund_semantic_review_tool"):
            with self.subTest(name=name), tool_recovery_scope():
                before = len(self.steps)
                call, message, _ = await self.execute(self.tool([TimeoutError(), risk()], name))
                self.assertEqual(len(self.steps), before + 1)
                self.assertTrue(call.is_error)
                self.assertEqual(json.loads(message.content)["suggested_action"], "verify_operation_state")

    async def test_business_failure_is_logged_and_not_cached_as_success(self):
        tool = self.tool([{"ok": False, "fund_code": CODES[0], "error": "净值不足",
                           "error_code": "data_unavailable"}, risk()])
        guard = LoopGuard(self.req)
        with loop_guard_scope(guard), tool_recovery_scope():
            call, message, hooks = await self.execute(tool)
            await self.execute(tool, call_id="another")
        payload = json.loads(message.content)
        self.assertTrue(call.is_error)
        self.assertEqual(payload["suggested_action"], "report_data_gap")
        self.assertEqual(payload["fund_code"], CODES[0])
        self.assertEqual(len(self.steps), 1)
        self.assertEqual(guard.cache, {})
        self.assertTrue(any(h.metadata.get("is_error") for h in hooks))

    async def test_returned_retryable_does_not_grant_retry(self):
        tool = self.tool([{"ok": False, "error": "timeout", "error_code": "source_timeout",
                           "retryable": True}, risk()])
        call, message, _ = await self.execute(tool)
        self.assertEqual(len(self.steps), 1)
        self.assertTrue(call.is_error)
        self.assertFalse(json.loads(message.content)["retryable"])

    async def test_legacy_failure_and_plain_error_text_not_transient(self):
        for result in ({"ok": False, "error": "timeout"}, RuntimeError("timeout")):
            with self.subTest(result=result):
                before = len(self.steps)
                call, message, _ = await self.execute(self.tool([result, risk()]))
                self.assertTrue(call.is_error)
                self.assertEqual(len(self.steps), before + 1)
                self.assertNotEqual(json.loads(message.content)["error_code"], "source_timeout")

    async def test_empty_search_is_success_not_data_source_failure(self):
        call, message, _ = await self.execute(self.tool([{"ok": True, "events": []}], NEWS))
        self.assertFalse(call.is_error)
        self.assertEqual(json.loads(message.content)["events"], [])

    async def test_invalid_parameters_then_single_valid_correction(self):
        tool = self.tool([risk()])
        with tool_recovery_scope():
            bad = await self.execute(tool, args={"fund_code": CODES[0], "months": 5})
            repeated = await self.execute(tool, args={"fund_code": CODES[0], "months": 5})
            good = await self.execute(tool, args={"fund_code": CODES[0], "months": 6})
        self.assertTrue(bad[0].is_error)
        payload = json.loads(bad[1].content)
        self.assertEqual(payload["field_errors"][0]["field"], "months")
        self.assertNotIn("input", payload["field_errors"][0])
        self.assertEqual(payload["suggested_action"], "correct_arguments")
        self.assertEqual(repeated[0].recovery_trace[0]["action"], "blocked_duplicate")
        self.assertFalse(good[0].is_error)
        self.assertEqual(len(self.steps), 1)

    async def test_source_validation_error_does_not_consume_parameter_correction(self):
        try:
            FundRiskToolInput.model_validate({"fund_code": "bad-source-field"})
        except ValidationError as exc:
            source_error = exc
        tool = self.tool([source_error, risk(CODES[1])])
        with tool_recovery_scope() as state:
            call, message, _ = await self.execute(tool)
            await self.execute(tool, args={"fund_code": CODES[1]})
            self.assertFalse(state.invalid_counts)
        self.assertTrue(call.is_error)
        self.assertEqual(json.loads(message.content)["error_code"], "internal_error")
        self.assertNotIn("bad-source-field", message.content)

    async def test_second_invalid_correction_stops_even_later_valid_call(self):
        tool = self.tool([risk()])
        with tool_recovery_scope():
            await self.execute(tool, args={"fund_code": CODES[0], "months": 5})
            second = await self.execute(tool, args={"fund_code": CODES[0], "months": 7})
            third = await self.execute(tool)
        self.assertEqual(json.loads(second[1].content)["suggested_action"], "ask_user")
        self.assertEqual(json.loads(third[1].content)["suggested_action"], "ask_user")
        self.assertEqual(len(self.steps), 0)

    async def test_retry_after_seconds_and_date_not_shortened(self):
        for value in ("60", format_datetime(datetime.now(timezone.utc) + timedelta(minutes=1)), "inf"):
            before = len(self.steps)
            call, _, _ = await self.execute(self.tool([status_error(429, value), risk()]))
            self.assertEqual(len(self.steps), before + 1)
            self.assertEqual(call.recovery_trace[-1]["action"], "retry_wait_exceeds_budget")

    async def test_short_retry_after_success_and_transport_details_redacted(self):
        call, _, _ = await self.execute(self.tool([status_error(503, "0.01"), risk()]))
        self.assertFalse(call.is_error)
        self.assertEqual(len(self.steps), 2)
        failure = classify_exception(status_error(403)).payload()
        self.assertEqual(failure["error_code"], "permission_denied")
        self.assertNotIn("secret", json.dumps(failure))
        self.assertNotIn("token", json.dumps(failure))

    async def test_no_retry_for_401_403_or_unknown_500(self):
        for status in (401, 403, 500):
            before = len(self.steps)
            call, _, _ = await self.execute(self.tool([status_error(status), risk()]))
            self.assertTrue(call.is_error)
            self.assertEqual(len(self.steps), before + 1)

    async def test_initial_permission_denial_never_invokes_tool(self):
        self.pre.return_value = False, "当前任务未授权", []
        call, message, _ = await self.execute(self.tool([risk()]))
        self.assertTrue(call.is_error)
        self.assertEqual(json.loads(message.content)["error_code"], "permission_denied")
        self.assertEqual(call.recovery_trace[0]["error_code"], "permission_denied")
        self.assertFalse(self.steps)

    async def test_permission_revoked_during_wait_is_checked_again(self):
        self.context = replace(self.context, task_grant_id="grant", task_run_id="run", task_agent_id="agent")
        self.authorizer.authorize_tool.side_effect = [None, AgentAuthorizationError("revoked", "已撤销")]
        call, message, _ = await self.execute(self.tool([TimeoutError(), risk()]))
        self.assertEqual(len(self.steps), 1)
        self.assertEqual(self.authorizer.authorize_tool.call_count, 2)
        self.assertEqual(json.loads(message.content)["error_code"], "permission_denied")

    async def test_pre_hook_also_rechecked_before_retry(self):
        self.pre.side_effect = [(True, "", []), (False, "已禁用", [])]
        _, message, _ = await self.execute(self.tool([TimeoutError(), risk()]))
        self.assertEqual(len(self.steps), 1)
        self.assertEqual(json.loads(message.content)["error_code"], "permission_denied")

    async def test_quota_lease_and_deadline_propagate_without_retry(self):
        for exc in (AIControlError("daily_quota_exhausted", "额度不足"),
                    AIControlError("job_lease_lost", "执行资格失效"), ChatTimeBudgetExceeded("work")):
            before = len(self.steps)
            with self.assertRaises(type(exc)):
                await self.execute(self.tool([exc, risk()]))
            self.assertEqual(len(self.steps), before + 1)

    async def test_deadline_too_short_skips_retry(self):
        with chat_budget_scope(ChatExecutionBudget(seconds=0.1, reserve_seconds=0)):
            call, _, _ = await self.execute(self.tool([TimeoutError(), risk()]))
        self.assertEqual(len(self.steps), 1)
        self.assertEqual(call.recovery_trace[-1]["action"], "retry_wait_exceeds_budget")

    async def test_cancel_during_backoff_does_not_make_second_request(self):
        tool = self.tool([TimeoutError(), risk()])
        with tool_recovery_scope():
            task = asyncio.create_task(self.execute(tool))
            while not self.steps:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(self.steps), 1)

    async def test_worker_cancel_callback_is_not_swallowed(self):
        class CancelledJob(RuntimeError):
            pass
        cancelled = False
        async def source():
            nonlocal cancelled
            cancelled = True
            raise TimeoutError()
        def check():
            if cancelled:
                raise CancelledJob()
        with ai_execution_scope("user", "run", "test", check):
            with self.assertRaises(CancelledJob):
                await self.execute(self.tool([source, risk()]))
        self.assertEqual(len(self.steps), 1)

    async def test_scope_isolation_and_closed_detached_context(self):
        with tool_recovery_scope() as outer:
            with tool_recovery_scope() as inner:
                self.assertIs(current_tool_recovery(), inner)
            self.assertIs(current_tool_recovery(), outer)
        self.assertIsNone(current_tool_recovery())
        with self.assertRaises(asyncio.CancelledError):
            await outer.run(RISK, {}, object(), self.context, AsyncMock(), [])

    async def test_new_request_can_retry_after_previous_request_failed(self):
        tool = self.tool([TimeoutError(), TimeoutError(), risk()])
        with tool_recovery_scope():
            await self.execute(tool)
        with tool_recovery_scope():
            result = await self.execute(tool)
        self.assertFalse(result[0].is_error)
        self.assertEqual(len(self.steps), 3)

    async def test_failure_memo_is_scoped_by_identity_and_resource(self):
        tool = self.tool([{"ok": False, "error": "无数据"}, risk(CODES[1]), risk()])
        with tool_recovery_scope():
            await self.execute(tool)
            other_resource = await self.execute(tool, args={"fund_code": CODES[1]})
            self.context = replace(self.context, user_id="another")
            other_user = await self.execute(tool)
        self.assertFalse(other_resource[0].is_error)
        self.assertFalse(other_user[0].is_error)
        self.assertEqual(len(self.steps), 3)

    async def test_request_memory_limit_fails_closed(self):
        state = ToolRecoveryState()
        state.locks = {str(i): asyncio.Lock() for i in range(128)}
        invoke = AsyncMock()
        result = await state.run(RISK, {}, object(), self.context, invoke, [])
        self.assertFalse(json.loads(result)["ok"])
        invoke.assert_not_awaited()

    async def test_success_is_still_cached_by_existing_guard(self):
        tool = self.tool([TimeoutError(), risk()])
        with tool_recovery_scope(), loop_guard_scope(LoopGuard(self.req)):
            first = await self.execute(tool)
            second = await self.execute(tool)
        self.assertFalse(first[0].is_error)
        self.assertEqual(second[0].execution_kind, "cached")
        self.assertEqual(len(self.steps), 2)


class BusinessErrorTests(unittest.TestCase):
    def test_prompt_policy_is_part_of_tools_section_not_free_floating_message(self):
        from app.services.prompt_builder import prompt_builder_service
        from app.services.tool_recovery import TOOL_RECOVERY_INSTRUCTION
        section = prompt_builder_service._tools_section(tool_names=[RISK])
        self.assertIn(TOOL_RECOVERY_INSTRUCTION, section.content)
        self.assertNotIn(TOOL_RECOVERY_INSTRUCTION,
                         prompt_builder_service._tools_section(tool_names=[]).content)

    def test_real_risk_insufficient_points_has_explicit_code(self):
        catalog = SimpleNamespace(get_fund=lambda _: {"nav_points": []})
        with self.assertRaises(FundRiskError) as caught:
            FundRiskService(catalog=catalog).analyze(fund_code=CODES[0])
        self.assertEqual(caught.exception.code, "data_unavailable")

    def test_real_empty_portfolio_has_explicit_code(self):
        portfolios = SimpleNamespace(get=lambda **_: {"position_mode": "empty"})
        with self.assertRaises(PortfolioPerformanceError) as caught:
            PortfolioPerformanceService(portfolios=portfolios).analyze(user_id="a", portfolio_id="p")
        self.assertEqual(caught.exception.code, "data_unavailable")

    def test_tool_adapters_preserve_domain_codes(self):
        with patch("app.services.fund_agent_tools.fund_risk_service.analyze",
                   side_effect=FundRiskError("不足三条", code="data_unavailable")):
            result = json.loads(get_fund_risk_tool.invoke({"fund_code": CODES[0]}))
        self.assertEqual(result["error_code"], "data_unavailable")
        with patch("app.services.fund_agent_tools.fund_catalog_service.get_fund", side_effect=FundNotFoundError("未导入")):
            result = json.loads(get_fund_profile_tool.invoke({"fund_code": CODES[0]}))
        self.assertEqual(result["suggested_action"], "clarify_resource")
        with patch("app.services.portfolio_agent_tools.portfolio_performance_service.analyze",
                   side_effect=PortfolioPerformanceError("没有流水", code="data_unavailable")):
            result = json.loads(get_portfolio_performance_tool.invoke({"user_id": "a",
                "portfolio_id": "portfolio_0123456789abcdef"}))
        self.assertEqual(result["suggested_action"], "report_data_gap")

    def test_long_error_compaction_never_turns_failure_into_ok(self):
        content = json.dumps({"ok": False, "error": "无数据" * 3000,
                              "error_code": "data_unavailable", "retryable": False,
                              "suggested_action": "report_data_gap"})
        result = context_compactor_service._build_compacted_content(
            message=ToolMessage(content=content, tool_call_id="x"), saved_path="local",
            reason="large", original_chars=len(content), original_content=content)
        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "data_unavailable")


class LoopRecoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    run_loop = loop_fixture.LoopIntegrationTests.run_loop

    async def test_comparison_retries_only_failed_source_and_feeds_both_results(self):
        counts = dict.fromkeys(CODES, 0)
        def handler(name, args):
            code = args["fund_code"]
            counts[code] += 1
            if code == CODES[1] and counts[code] == 1:
                raise httpx.ConnectError("临时故障")
            return risk(code)
        actions = [[loop_fixture.LoopIntegrationTests.action(code, call_id=code) for code in CODES], "两只的历史指标都已取得。"]
        result = await self.run_loop(actions, req=request(message="比较110020和110007的风险"),
                                     selected=route(RISK), handler=handler)
        self.assertEqual(counts, {CODES[0]: 1, CODES[1]: 2})
        self.assertFalse(any(c.is_error for c in result.tool_calls))
        messages = [m for m in self.invocations[-1]["messages"] if m.type == "tool"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(result.loop_control.completion_status, "evidence_ready")

    async def test_missing_data_is_not_treated_as_completed_comparison(self):
        def handler(name, args):
            return risk() if args["fund_code"] == CODES[0] else {
                "ok": False, "error": "缺少净值", "error_code": "data_unavailable"}
        pair = [loop_fixture.LoopIntegrationTests.action(code, call_id=code) for code in CODES]
        result = await self.run_loop([pair, "都很安全", [pair[1]], "无法取得第二只净值"],
            req=request(message="比较110020和110007的风险"), selected=route(RISK), handler=handler)
        self.assertEqual(len(self.reads), 2)
        self.assertEqual(result.loop_control.completion_status, "insufficient_evidence")
        self.assertNotIn("都很安全", result.answer)
        self.assertTrue(result.tool_calls[-1].is_error)

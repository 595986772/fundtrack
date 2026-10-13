"""Loop 控制的确定性验收：假模型、假来源、临时库，不花 API 费用或改真实持仓。"""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from app.config import settings
from app.schemas import AgentChatRequest, AgentToolCall
from app.services.agent_authorization import AgentAuthorizationError
from app.services.agent_execution_context import AgentExecutionContext
from app.services.agent_loop_guard import LoopGuard, current_loop_guard, loop_guard_scope
from app.services.agent_routing import AgentRequestRoute, agent_request_router
from app.services.agent_tool_executor import agent_tool_executor
from app.services.prompt_builder import PromptBuildResult
from app.services.recovery import ModelCallResult
from app.services import tool_agent as agent_module


RISK = "get_fund_risk_tool"
REPORT = "get_fund_report_research_tool"
NEWS = "search_market_events_tool"
PROFILE = "get_fund_profile_tool"
PERFORMANCE = "get_portfolio_performance_tool"
ASSESSMENT = "get_portfolio_assessment_tool"
CODES = ["110020", "110007"]
PORTFOLIO = "portfolio_0123456789abcdef"


class ReadInput(BaseModel):
    fund_code: str = "110020"
    months: int = 12
    user_id: str = ""
    portfolio_id: str = ""
    question: str = ""
    document_ids: list[str] | None = None


def request(**changes):
    return AgentChatRequest(**{"user_id": "loop-test", "message": "分析当前基金的风险和新闻", **changes})


def route(*names):
    return AgentRequestRoute(name="fund_risk", domains=("fund_risk",), tool_names=names,
                             include_mcp_tools=False, reason="本地测试")


def risk(code=CODES[0], months=12):
    return {"ok": True, "fund_code": code, "period_months": months,
            "period_start": "2025-09-01", "period_end": "2026-09-01",
            "annualized_volatility_pct": 10, "max_drawdown": {"drawdown_pct": 8}}


def call(name=RISK, payload=None, *, args=None, error=False):
    return AgentToolCall(tool_name=name, tool_args=args or {},
                         tool_result=json.dumps(payload if payload is not None else risk()), is_error=error)


def visible(calls):
    return [ToolMessage(content=c.tool_result, tool_call_id=f"call-{i}") for i, c in enumerate(calls)]


class CompletionTests(unittest.TestCase):
    def guard(self, **changes):
        guard = LoopGuard(request(message="对比110020和110007的风险与经理策略", **changes))
        guard.configure(route(RISK, REPORT))
        return guard

    def test_comparison_router_keeps_both_tools(self):
        req = request(message="对比110020和110007的风险和经理观点")
        result = agent_request_router.route(req, execution_context=AgentExecutionContext.product(user_id=req.user_id))
        self.assertEqual(set(result.tool_names), {RISK, REPORT})

    def test_success_does_not_cover_other_fund_or_strategy(self):
        guard = self.guard()
        calls = [call(payload=risk())]
        missing = guard.evaluate(visible(calls), calls)
        self.assertEqual(len(missing), 3)
        self.assertEqual(len(guard.trace.covered), 1)

    def test_empty_or_error_risk_is_not_evidence(self):
        for payload in ({"ok": True}, {**risk(), "ok": False}, {**risk(), "error": "failed"},
                        {**risk(), "annualized_volatility_pct": None},
                        {**risk(), "annualized_volatility_pct": "未取得"},
                        {**risk(), "annualized_volatility_pct": float("nan")},
                        {**risk(), "max_drawdown": {"drawdown_pct": None}},
                        {**risk(), "max_drawdown": "未取得"}):
            guard = self.guard()
            calls = [call(payload=payload)]
            guard.evaluate(visible(calls), calls)
            self.assertEqual(guard.trace.covered, [])

    def test_report_empty_fields_do_not_crash_or_complete(self):
        for documents, evidence in ((None, []), ([], None), ([{}], "未取得"),
                                    ([{"id": []}], [{"document_id": []}])):
            guard = self.guard()
            calls = [call(REPORT, {"ok": True, "fund_code": CODES[0],
                                  "documents": documents, "evidence": evidence})]
            guard.evaluate(visible(calls), calls)
            self.assertEqual(guard.trace.covered, [])

    def test_two_quarters_need_visible_manager_excerpts(self):
        guard = self.guard()
        payload = {"ok": True, "fund_code": CODES[0],
            "documents": [{"id": f"doc{i}", "fund_code": CODES[0], "period": f"2026-Q{i}"} for i in (1, 2)],
            "evidence": [{"document_id": f"doc{i}", "section": "manager_review", "text": "原文摘录",
                          "evidence_id": f"report:doc{i}:p2", "page": 2} for i in (1, 2)]}
        calls = [call(REPORT, payload)]
        guard.evaluate(visible(calls), calls)
        self.assertEqual(guard.trace.covered, [f"{CODES[0]} 两个不同季度的经理原文"])
        payload["evidence"].pop()
        calls = [call(REPORT, payload)]
        guard.evaluate(visible(calls), calls)
        self.assertEqual(guard.trace.covered, [])

    def test_same_quarter_twice_is_not_two_quarters(self):
        guard = self.guard()
        payload = {"ok": True, "fund_code": CODES[0], "documents": [
            {"id": f"d{i}", "fund_code": CODES[0], "period": "2026-Q1"} for i in (1, 2)],
            "evidence": [{"document_id": f"d{i}", "section": "manager_review", "text": "原文", "page": 1,
                          "evidence_id": f"e{i}"} for i in (1, 2)]}
        calls = [call(REPORT, payload)]
        guard.evaluate(visible(calls), calls)
        self.assertFalse(guard.trace.covered)

    def test_compacted_preview_does_not_recover_hidden_audit_evidence(self):
        guard = self.guard()
        calls = [call()]
        messages = visible(calls)
        messages[0].content = json.dumps({"ok": True, "context_compacted": True, "preview": calls[0].tool_result})
        guard.evaluate(messages, calls)
        self.assertFalse(guard.trace.covered)

    def test_risk_windows_must_be_comparable(self):
        guard = LoopGuard(request(message="比较110020和110007的风险"))
        guard.configure(route(RISK))
        calls = [call(payload=risk(CODES[0], 12)), call(payload=risk(CODES[1], 6))]
        self.assertIn("区间不一致", guard.evaluate(visible(calls), calls)[0])

    def test_portfolio_empty_returns_not_complete(self):
        guard = LoopGuard(request(message="解释当前组合的收益与风险", portfolio_id=PORTFOLIO))
        guard.configure(route(PERFORMANCE, ASSESSMENT))
        payload = {"ok": True, "portfolio_id": PORTFOLIO, "period_end": "2026-09-01",
                   "returns": {"twr_pct": None}, "max_drawdown": {"drawdown_pct": 1}}
        calls = [call(PERFORMANCE, payload)]
        self.assertEqual(guard.evaluate(visible(calls), calls), ["当前组合的历史收益指标"])
        payload["returns"]["twr_pct"] = 0  # 零收益是有效数字，不能因为 falsy 当成缺失。
        calls = [call(PERFORMANCE, payload)]
        self.assertEqual(guard.evaluate(visible(calls), calls), [])

    def test_foreign_selected_fund_requires_clarification(self):
        guard = self.guard(fund_code=CODES[0])
        self.assertIsNone(guard.request_correction([], [], can_continue=True))
        answer, reason = guard.finalize("两只都很好", [], [], reason="model_finished")
        self.assertEqual(reason, "needs_clarification")
        self.assertNotIn("都很好", answer)

    def test_one_correction_only_and_no_claim_of_semantic_validation(self):
        guard = self.guard()
        self.assertIsNotNone(guard.request_correction([], [], can_continue=True))
        self.assertIsNone(guard.request_correction([], [], can_continue=True))
        self.assertEqual(guard.trace.completion_retries, 1)
        self.assertIn("不验证语义", guard.trace.verification_scope)

    def test_unspecified_names_and_special_workflows_not_forced(self):
        for req, selected in ((request(message="解释一下风险是什么意思"), route(RISK)),
            (request(message="分析当前组合收益与风险", portfolio_id=PORTFOLIO), route("start_portfolio_research_tool"))):
            guard = LoopGuard(req)
            guard.configure(selected)
            self.assertEqual(guard.trace.completion_status, "not_applicable")

    def test_no_progress_redirect_then_stop_and_new_data_resets(self):
        guard = LoopGuard(request())
        self.assertEqual(guard.observe_round([call()], 1), "continue")
        self.assertEqual(guard.observe_round([call()], 2), "continue")
        self.assertEqual(guard.observe_round([call()], 3), "redirect")
        self.assertEqual(guard.observe_round([call()], 4), "stop")
        self.assertEqual(guard.observe_round([call(payload=risk(months=6))], 5), "continue")
        self.assertEqual(guard.stalled_rounds, 0)

    def test_changing_request_ids_not_progress_and_errors_not_success(self):
        guard = LoopGuard(request())
        for i in range(3):
            guard.observe_round([call(payload={**risk(), "request_id": str(i), "duration_ms": i})], i + 1)
        self.assertEqual(guard.stalled_rounds, 2)
        error = call(payload={"ok": False, "error": "timeout"})
        self.assertEqual(guard.observe_round([error], 4), "stop")


class CacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.request = request()
        self.context = AgentExecutionContext.product(user_id=self.request.user_id)
        self.guard = LoopGuard(self.request)
        self.tool = SimpleNamespace(args_schema=ReadInput)
        self.invoke = AsyncMock(return_value=json.dumps(risk()))

    async def read(self, args=None, **changes):
        return await self.guard.read(changes.get("name", RISK), args or {}, self.tool,
                                     changes.get("context", self.context), self.invoke)

    async def test_default_normalization_and_parameter_change(self):
        await self.read()
        _, hit = await self.read({"months": 12})
        self.assertTrue(hit)
        await self.read({"months": 6})
        self.assertEqual(self.invoke.await_count, 2)

    async def test_same_round_duplicates_share_one_read(self):
        results = await asyncio.gather(self.read(), self.read(), self.read())
        self.assertEqual(self.invoke.await_count, 1)
        self.assertEqual(sum(hit for _, hit in results), 2)

    async def test_invalid_arguments_and_large_payloads_bypass_cache(self):
        await self.read({"months": "invalid"})
        await self.read({"months": "invalid"})
        self.invoke.return_value = json.dumps({"ok": True, "large": "x" * 64001})
        await self.read()
        await self.read()
        self.assertEqual(self.invoke.await_count, 4)
        self.assertFalse(self.guard.cache)

    async def test_refresh_request_bypasses_cache(self):
        self.guard = LoopGuard(request(research_refresh=True))
        await self.read()
        await self.read()
        self.assertEqual(self.invoke.await_count, 2)

    async def test_error_and_cancellation_not_cached(self):
        self.invoke.return_value = json.dumps({"ok": False, "error": "temporary"})
        await self.read()
        await self.read()
        self.assertEqual(self.invoke.await_count, 2)
        self.invoke.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.read()
        self.assertEqual(self.guard.cache, {})

    async def test_write_and_status_not_cached_and_invalidate_reads(self):
        await self.read()
        for name in ("start_portfolio_research_tool", "get_portfolio_research_status_tool"):
            await self.read(name=name)
            await self.read(name=name)
        _, hit = await self.read()
        self.assertFalse(hit)

    async def test_cache_not_shared_between_runs_users_or_grants(self):
        await self.read()
        with self.assertRaises(PermissionError):
            await self.read(context=AgentExecutionContext.product(user_id="another"))
        await self.read(context=replace(self.context, task_grant_id="new-grant"))
        self.guard = LoopGuard(self.request)
        await self.read()
        self.assertEqual(self.invoke.await_count, 3)

    async def test_nested_scope_restores_parent(self):
        with loop_guard_scope(self.guard):
            child = LoopGuard(self.request)
            with loop_guard_scope(child):
                self.assertIs(current_loop_guard(), child)
                self.assertIsNot(child.cache, self.guard.cache)
            self.assertIs(current_loop_guard(), self.guard)
        self.assertIsNone(current_loop_guard())

    async def test_executor_checks_authorization_on_cache_hits(self):
        calls = []
        async def execute(**args):
            calls.append(args)
            return json.dumps(risk())
        tool = StructuredTool.from_function(coroutine=execute, name=RISK, description="测试", args_schema=ReadInput)
        context = replace(self.context, task_grant_id="grant", task_run_id="run", task_agent_id="agent")
        authorizer = SimpleNamespace(authorize_tool=Mock(), check_context=Mock())
        async def once(i):
            return await agent_tool_executor.execute(tool_call={"name": RISK, "id": str(i), "args": {}},
                tool_map={RISK: tool}, request=self.request, execution_context=context, tool_index=i, ToolMessage=ToolMessage)
        with loop_guard_scope(self.guard), patch("app.services.agent_tool_executor.get_task_authorization_service", return_value=authorizer):
            first = await once(1)
            second = await once(2)
            self.assertEqual(second[0].execution_kind, "cached")
            self.assertEqual(second[1].tool_call_id, "2")
            authorizer.check_context.side_effect = AgentAuthorizationError("revoked", "已撤销")
            denied = await once(3)
        self.assertFalse(first[0].is_error)
        self.assertTrue(denied[0].is_error)
        self.assertNotIn("annualized_volatility_pct", denied[1].content)
        self.assertEqual(len(calls), 1)
        self.assertEqual(authorizer.authorize_tool.call_count, 3)


class LoopIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """走真实 Loop、工具执行器、压缩和返回契约，只有模型与业务来源用替身。"""

    async def run_loop(self, actions, *, req=None, selected=None, handler=None, enabled=True):
        req, selected = req or request(), selected or route(RISK, NEWS)
        self.reads, self.invocations, self.events = [], [], []
        def make_tool(name):
            async def execute(**args):
                self.reads.append((name, args))
                result = handler(name, args) if handler else risk(args.get("fund_code", CODES[0]), args.get("months", 12))
                return json.dumps(result)
            return StructuredTool.from_function(coroutine=execute, name=name, description="本地测试", args_schema=ReadInput)
        tools = [make_tool(name) for name in selected.tool_names]
        model = SimpleNamespace(bind_tools=lambda tools: SimpleNamespace(names=[t.name for t in tools]))
        async def invoke(**kwargs):
            self.invocations.append({**kwargs, "messages": list(kwargs["messages"])})
            action = actions[len(self.invocations) - 1]
            response = AIMessage(content=action if isinstance(action, str) else "",
                tool_calls=[] if isinstance(action, str) else action,
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
            return ModelCallResult(response=response)
        with (
            patch.object(settings, "agent_loop_guard_enabled", enabled),
            patch.object(settings, "agent_tool_discovery_enabled", False),
            patch.object(agent_module.agent_request_router, "route_hybrid", new=AsyncMock(return_value=selected)),
            patch.object(agent_module, "select_agent_tools", return_value=tools),
            patch.object(agent_module, "get_chat_model", return_value=model),
            patch.object(agent_module.prompt_builder_service, "build", return_value=PromptBuildResult("测试", (), 2)),
            patch.object(agent_module.model_recovery_service, "invoke", new=AsyncMock(side_effect=invoke)),
            patch.object(agent_module.agent_runtime_state_service, "collect_response_fields", return_value={}),
        ):
            return await agent_module.tool_calling_agent_service.chat(req,
                progress_callback=AsyncMock(side_effect=lambda event: self.events.append(event)))

    @staticmethod
    def action(code=CODES[0], *, name=RISK, call_id="read", months=12):
        return {"name": name, "args": {"fund_code": code, "months": months}, "id": call_id, "type": "tool_call"}

    async def test_stalled_loop_stops_before_ten_rounds(self):
        result = await self.run_loop([[self.action(call_id=str(i))] for i in range(4)] + ["仅有历史风险依据。"])
        self.assertEqual(len(self.reads), 1)
        self.assertEqual(result.loop_control.cache_hits, 3)
        self.assertEqual(result.stop_reason, "no_progress")
        self.assertEqual(len(self.invocations), 5)
        self.assertEqual(self.invocations[-1]["round_index"], 5)
        self.assertEqual(result.usage.total_tokens, 75)
        self.assertTrue(any(e["phase"] == "loop_guard" for e in self.events))
        self.assertEqual([m.tool_call_id for m in self.invocations[-1]["messages"] if isinstance(m, ToolMessage)], ["0", "1", "2", "3"])

    async def test_changed_months_continue_and_can_disable_guard(self):
        result = await self.run_loop([[self.action(months=12)], [self.action(months=6)], "已按不同区间查阅。"])
        self.assertEqual(len(self.reads), 2)
        self.assertEqual(result.stop_reason, "model_finished")
        result = await self.run_loop([[self.action()] for _ in range(4)] + ["完成"], enabled=False)
        self.assertEqual(len(self.reads), 4)
        self.assertFalse(result.loop_control.enabled)

    async def test_missing_fund_corrected_within_existing_round_limit(self):
        result = await self.run_loop([[self.action()], "两个风险都查好了", [self.action(CODES[1])], "两只已使用同区间历史指标。"],
            req=request(message="比较110020和110007的风险"), selected=route(RISK))
        self.assertEqual(result.loop_control.completion_status, "evidence_ready")
        self.assertEqual(result.loop_control.completion_retries, 1)
        self.assertEqual(result.stop_reason, "model_finished")
        self.assertEqual(len(self.reads), 2)
        self.assertNotIn("两个风险都查好了", str(self.invocations[-1]["messages"]))

    async def test_incomplete_answer_is_not_shown_as_complete(self):
        result = await self.run_loop([[self.action()], "两个都好", "都查过了"],
            req=request(message="比较110020和110007的风险"), selected=route(RISK))
        self.assertEqual(result.stop_reason, "insufficient_evidence")
        self.assertIn(CODES[1], result.answer)
        self.assertNotIn("都查过了", result.answer)
        self.assertEqual(result.loop_control.completion_retries, 1)

    async def test_forced_final_still_checks_coverage_without_extra_correction(self):
        result = await self.run_loop([[self.action()], "两只都安全"],
            req=request(message="比较110020和110007的风险", max_tool_rounds=1), selected=route(RISK))
        self.assertEqual(result.stop_reason, "insufficient_evidence")
        self.assertEqual(result.loop_control.completion_retries, 0)
        self.assertEqual(len(self.invocations), 2)

    async def test_parallel_calls_keep_ids_and_complete_contract(self):
        result = await self.run_loop([[self.action(CODES[0], call_id="a"), self.action(CODES[1], call_id="b")], "比较完成"],
            req=request(message="比较110020和110007的风险"), selected=route(RISK))
        self.assertEqual(result.loop_control.completion_status, "evidence_ready")
        self.assertEqual([m.tool_call_id for m in self.invocations[-1]["messages"] if isinstance(m, ToolMessage)], ["a", "b"])

    async def test_semantic_comparison_keeps_both_funds_without_single_fund_protocol(self):
        def reports(name, args):
            code = args["fund_code"]
            return {"ok": True, "fund_code": code, "documents": [
                {"id": f"{code}-{i}", "fund_code": code, "period": f"2026-Q{i}"} for i in (1, 2)],
                "evidence": [{"document_id": f"{code}-{i}", "section": "manager_review", "text": "固定原文",
                              "evidence_id": f"report:{code}-{i}:p1", "page": 1} for i in (1, 2)]}
        result = await self.run_loop([[self.action(CODES[0], name=REPORT)], [self.action(CODES[1], name=REPORT)], "已有两期依据"],
            req=request(message="比较110020和110007的经理策略"), selected=route(REPORT), handler=reports)
        self.assertEqual(result.loop_control.completion_status, "evidence_ready")
        self.assertIsNone(result.report_answer_check)  # 不冒充单基金原句协议的引用校验。

    async def test_failed_tools_stop_and_do_not_count_as_coverage(self):
        result = await self.run_loop([[self.action()] for _ in range(3)] + ["资料不足"],
            handler=lambda *_: {"ok": False, "error": "source timeout"})
        self.assertEqual(result.stop_reason, "no_progress")
        # 三次模型调用意图仍触发无进展收尾，但业务失败只执行一次，后两次返回失败回执。
        self.assertEqual(len(self.reads), 1)
        self.assertTrue(all(call.is_error for call in result.tool_calls))
        self.assertEqual(result.loop_control.cache_hits, 0)

    async def test_trace_saved_to_observation_and_conversation_metadata(self):
        from app.database import Database
        from app.services.comprehensive_agent import ComprehensiveAgentService
        from app.services.conversations import ConversationService
        from app.services.observability import AgentObservabilityService

        result = await self.run_loop([[self.action()], "已有风险依据"])
        with TemporaryDirectory() as directory:
            database = Database("sqlite:///" + str(Path(directory) / "loop.db"))
            try:
                observations = AgentObservabilityService(database)
                conversations = ConversationService(database)
                observations.record_run(run=result, user_id=result.user_id, duration_ms=1, source="eval")
                saved = observations.recent_runs(user_id=result.user_id)[0]
                self.assertEqual(saved.metrics["loop_control"], result.loop_control.model_dump(mode="json"))
                self.assertEqual(saved.tool_calls[0]["recovery_trace"], result.tool_calls[0].recovery_trace)
                service = ComprehensiveAgentService(conversations=conversations, observability=observations)
                metadata = service._compact_run_metadata(result, duration_ms=1)
                self.assertEqual(metadata["loop_control"], result.loop_control.model_dump(mode="json"))
                self.assertEqual(metadata["tool_calls"][0]["recovery_trace"], result.tool_calls[0].recovery_trace)
            finally:
                database.engine.dispose()


if __name__ == "__main__":
    unittest.main()

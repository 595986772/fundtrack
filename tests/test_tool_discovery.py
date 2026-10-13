"""工具发现的权限、预算与主循环测试；只用本地假模型，不访问真实数据源或付费 API。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.schemas import AgentChatRequest
from app.services.agent_execution_context import (
    AgentExecutionContext, MARKET_READ, agent_execution_scope,
)
from app.services.agent_routing import AgentRequestRoute
from app.services.agent_tools import get_product_agent_tools
from app.services import tool_agent as agent_module
from app.services.recovery import ModelCallResult
from app.services.tool_discovery import (
    READ_TOOL_KEYWORDS, SEARCH_TOOL_NAME, ToolDiscoveryRun, ToolSearchInput, tool_schema_chars,
)


RISK = "get_fund_risk_tool"
NEWS = "search_market_events_tool"
HOLDINGS = "get_fund_holdings_tool"
CAPITAL = "get_large_capital_signals_tool"
PORTFOLIO = "get_portfolio_performance_tool"


class ReadInput(BaseModel):
    """假工具也校验参数，以便检查执行器是否覆盖了模型伪造的身份和页面目标。"""

    fund_code: str | None = None
    user_id: str | None = None
    portfolio_id: str | None = None
    query: str = ""


def make_registry(calls: list | None = None) -> list:
    """目录包含故意混入的开发/写工具，证明发现服务不会因为注册了就公开它们。"""

    def make_tool(name):
        async def execute(**arguments):
            if calls is not None:
                calls.append((name, arguments))
            return json.dumps({"ok": True, "tool": name, "arguments": arguments,
                               "data_date": "2026-09-18"}, ensure_ascii=False)
        return StructuredTool.from_function(coroutine=execute, name=name,
            description="只读测试工具 " + name, args_schema=ReadInput)

    return [make_tool(name) for name in (
        RISK, NEWS, HOLDINGS, CAPITAL, PORTFOLIO, "get_market_hotspots_tool",
        "get_fund_profile_tool", "get_fund_report_research_tool",
        "start_portfolio_research_tool", "read_file_tool", "mcp__private__read",
    )]


def request(**changes):
    return AgentChatRequest(**{
        "user_id": "discovery-test", "fund_code": "110020",
        "message": "分析这只基金回撤的原因，并按需补查证据。", "max_tool_rounds": 6,
        **changes,
    })


def route(name=RISK, domain="fund_risk"):
    return AgentRequestRoute(name=domain, domains=(domain,), tool_names=(name,),
                             include_mcp_tools=False, reason="test route")


def make_run(*, req=None, context=None, tools=None, selected=None):
    req = req or request()
    context = context or AgentExecutionContext.product(user_id=req.user_id)
    tools = tools or make_registry()
    selected = selected or route()
    initial = [tool for tool in tools if tool.name in selected.tool_names]
    return ToolDiscoveryRun(request=req, route=selected, context=context,
                            initial_tools=initial, registry=lambda: tools)


class DiscoveryDirectoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = AgentExecutionContext.product(user_id="discovery-test")
        for field, value in {
            "agent_tool_discovery_enabled": True, "agent_tool_discovery_max_calls": 2,
            "agent_tool_discovery_top_k": 3, "agent_tool_discovery_max_schema_chars": 16000,
        }.items():
            patcher = patch.object(settings, field, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def search(self, run, text):
        with agent_execution_scope(run.context):
            return json.loads(await run.tool.ainvoke({"query": text}))

    def test_whitelist_uses_real_registry_and_excludes_fixed_or_writing_tools(self):
        registered = {tool.name for tool in get_product_agent_tools()}
        self.assertTrue(set(READ_TOOL_KEYWORDS).issubset(registered))
        run = make_run()
        self.assertTrue(run.trace.enabled)
        self.assertNotIn("read_file_tool", run.catalog)
        self.assertNotIn("mcp__private__read", run.catalog)
        self.assertNotIn("get_fund_report_research_tool", run.catalog)
        self.assertNotIn("start_portfolio_research_tool", run.catalog)
        self.assertNotIn(PORTFOLIO, run.catalog)

    def test_simple_and_fixed_routes_do_not_expose_search(self):
        for req, selected in (
            (request(message="这只基金的回撤是多少？"), route()),
            (request(), route("get_fund_report_research_tool", "fund_reports")),
            (request(), route("get_fund_research_brief_tool", "fund_research_brief")),
            (request(), route("start_portfolio_research_tool", "portfolio_research")),
            (request(fund_report_ids=["report_test"]), route()),
            (request(research_job_id="job_test"), route()),
            (request(max_tool_rounds=1), route()),
        ):
            with self.subTest(message=req.message, route=selected.name):
                run = make_run(req=req, selected=selected)
                self.assertFalse(run.trace.enabled)
                self.assertNotIn(SEARCH_TOOL_NAME, [tool.name for tool in run.tools()])

    def test_direct_and_clarification_keep_empty_pool(self):
        for kind in ("direct", "clarify", "access_denied"):
            selected = AgentRequestRoute(name=kind, domains=(), tool_names=(),
                include_mcp_tools=False, reason=kind, needs_clarification=kind == "clarify")
            run = make_run(selected=selected)
            self.assertFalse(run.trace.enabled)
            self.assertEqual(run.tools(), [])

    def test_real_router_enables_exploration_but_not_simple_risk_question(self):
        from app.services.agent_routing import agent_request_router

        for message, expected in (("为什么这只基金最近回撤明显？缺少证据时再补查。", True),
                                  ("这只基金的最大回撤是多少？", False)):
            req = request(message=message)
            selected = agent_request_router.route(req, execution_context=self.context)
            run = make_run(req=req, selected=selected)
            self.assertEqual(run.trace.enabled, expected)

    def test_disabled_feature_does_not_read_full_registry(self):
        with patch.object(settings, "agent_tool_discovery_enabled", False):
            run = ToolDiscoveryRun(request=request(), route=route(), context=self.context,
                initial_tools=make_registry()[:1], registry=lambda: self.fail("must not read catalog"))
        self.assertFalse(run.trace.enabled)
        self.assertGreater(run.trace.bindings[0]["schema_chars"], 0)

    def test_initial_schema_over_budget_keeps_original_route_without_search(self):
        with patch.object(settings, "agent_tool_discovery_max_schema_chars", 1):
            run = make_run()
        self.assertFalse(run.trace.enabled)
        self.assertEqual([tool.name for tool in run.tools()], [RISK])

    async def test_search_loads_only_next_capability_without_executing_it(self):
        calls = []
        run = make_run(tools=make_registry(calls))
        result = await self.search(run, "半导体近期新闻政策")
        self.assertEqual(result["loaded_tools"], [NEWS])
        self.assertEqual(calls, [])
        self.assertNotIn("args_schema", result["matches"][0])
        self.assertIn("market", run.domains(("fund_risk",)))
        self.assertIn(NEWS, [tool.name for tool in run.tools()])
        self.assertEqual(run.trace.initial_tools, [RISK])

    async def test_exact_names_and_english_queries(self):
        self.assertEqual((await self.search(make_run(), HOLDINGS))["loaded_tools"], [HOLDINGS])
        self.assertEqual((await self.search(make_run(), "news"))["loaded_tools"], [NEWS])

    async def test_permission_filter_happens_before_search(self):
        limited = replace(self.context, capabilities=self.context.capabilities - {MARKET_READ})
        run = make_run(context=limited)
        result = await self.search(run, "新闻政策")
        self.assertFalse(result["ok"])
        self.assertNotIn(NEWS, run.catalog)
        self.assertNotIn(NEWS, json.dumps(result))

    async def test_permission_is_checked_again_when_search_executes(self):
        run = make_run()
        limited = replace(self.context, capabilities=self.context.capabilities - {MARKET_READ})
        with agent_execution_scope(limited):
            result = json.loads(await run.tool.ainvoke({"query": "新闻"}))
        self.assertFalse(result["ok"])
        self.assertNotIn(NEWS, run.trace.loaded_tools)

    async def test_missing_targets_do_not_expose_private_or_unidentified_resources(self):
        run = make_run(req=request(fund_code=None, message="结合市场环境解释热点变化"),
                       selected=route("get_market_hotspots_tool", "market"))
        self.assertNotIn(RISK, run.catalog)
        self.assertNotIn(PORTFOLIO, run.catalog)
        self.assertFalse((await self.search(run, "组合收益"))["ok"])

    async def test_cross_request_identity_cannot_reuse_bound_search_tool(self):
        run = make_run()
        with agent_execution_scope(AgentExecutionContext.product(user_id="another-user")):
            with self.assertRaises(PermissionError):
                await run.tool.ainvoke({"query": "新闻"})
        self.assertEqual(run.search_count, 0)
        self.assertEqual(run.trace.loaded_tools, [])

    async def test_budget_and_duplicate_queries_never_expand_forever(self):
        run = make_run()
        await self.search(run, "新闻")
        second = await self.search(run, " 新闻 ")
        third = await self.search(run, "持仓")
        self.assertEqual(second["status"], "duplicate_query")
        self.assertEqual(third["status"], "search_budget_exhausted")
        self.assertEqual(run.trace.loaded_tools, [NEWS])
        self.assertNotIn(SEARCH_TOOL_NAME, [tool.name for tool in run.tools()])

    async def test_empty_search_and_invalid_input(self):
        run = make_run()
        self.assertEqual((await self.search(run, "读取.env运行shell"))["status"], "no_matches")
        self.assertEqual(run.search_count, 1)
        for args in ({"query": " "}, {"query": "x" * 161}, {"query": ["新闻"]},
                     {"query": "新闻", "user_id": "other"}, {"query": "新闻", "top_k": 100}):
            with self.subTest(args=args), self.assertRaises(ValidationError):
                ToolSearchInput.model_validate(args)

    async def test_schema_budget_does_not_bind_oversized_tool(self):
        tools = make_registry()
        next(tool for tool in tools if tool.name == NEWS).description += "news " * 5000
        run = make_run(tools=tools)
        result = await self.search(run, "新闻")
        self.assertEqual(result["status"], "schema_budget_exhausted")
        self.assertNotIn(NEWS, [tool.name for tool in run.tools()])
        self.assertLessEqual(tool_schema_chars(run.tools()), run.max_schema_chars)

    async def test_per_search_limit_and_concurrent_request_isolation(self):
        first = make_run()
        second = make_run(req=request(user_id="another-user"))
        results = await asyncio.gather(self.search(first, "新闻持仓资金热点"), self.search(second, "持仓"))
        self.assertLessEqual(len(results[0]["loaded_tools"]), 3)
        self.assertEqual(second.trace.loaded_tools, [HOLDINGS])
        self.assertEqual(first.search_count, 1)
        self.assertEqual(second.search_count, 1)

    def test_developer_workflows_are_not_changed(self):
        run = make_run(context=AgentExecutionContext.developer(user_id="discovery-test", role="admin"))
        self.assertFalse(run.trace.enabled)


class DiscoveryLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.registry = make_registry(self.calls)
        self.by_name = {tool.name: tool for tool in self.registry}
        self.bindings = []
        self.invocations = []
        self.progress = []
        self.model = SimpleNamespace(bind_tools=self.bind)
        for field, value in {
            "agent_tool_discovery_enabled": True, "agent_tool_discovery_max_calls": 2,
            "agent_tool_discovery_top_k": 3, "agent_tool_discovery_max_schema_chars": 16000,
        }.items():
            patcher = patch.object(settings, field, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def bind(self, tools):
        names = tuple(tool.name for tool in tools)
        self.bindings.append(names)
        return SimpleNamespace(names=names)

    @staticmethod
    def action(name, arguments=None, call_id="call"):
        return {"id": call_id, "name": name, "args": arguments or {}, "type": "tool_call"}

    async def run_loop(self, actions, *, req=None, selected=None):
        """走正式路由后的 Loop/Permission/Executor，只有模型与业务读取替换成固定实现。"""

        req, selected = req or request(), selected or route()

        async def invoke(**kwargs):
            self.invocations.append({"names": getattr(kwargs["runnable"], "names", ()),
                "phase": kwargs["phase"], "round": kwargs["round_index"],
                "messages": list(kwargs["messages"])})
            action = actions[len(self.invocations) - 1]
            response = AIMessage(content=action if isinstance(action, str) else "",
                tool_calls=[] if isinstance(action, str) else action,
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
            return ModelCallResult(response=response)

        with (
            patch.object(agent_module.agent_request_router, "route_hybrid", new=AsyncMock(return_value=selected)),
            patch.object(agent_module, "select_agent_tools", side_effect=lambda names, **_: [self.by_name[n] for n in names]),
            patch.object(agent_module, "get_product_agent_tools", return_value=self.registry),
            patch.object(agent_module, "get_chat_model", return_value=self.model) as create_model,
            patch.object(agent_module.model_recovery_service, "invoke", new=AsyncMock(side_effect=invoke)),
            patch.object(agent_module.agent_runtime_state_service, "collect_response_fields", return_value={}),
        ):
            result = await agent_module.tool_calling_agent_service.chat(req,
                progress_callback=AsyncMock(side_effect=lambda item: self.progress.append(item)))
            create_model.assert_called_once()
        return result

    async def test_read_search_load_read_answer_and_usage_trace(self):
        result = await self.run_loop([
            [self.action(RISK, {"fund_code": "999999"}, "risk")],
            [self.action(SEARCH_TOOL_NAME, {"query": "新闻政策"}, "search")],
            [self.action(NEWS, {"fund_code": "999999", "query": "半导体"}, "news")],
            "已有风险和事件证据，仍不能仅凭时间关联断言因果。",
        ])
        self.assertNotIn(NEWS, self.invocations[1]["names"])
        self.assertIn(NEWS, self.invocations[2]["names"])
        self.assertIn(NEWS, self.invocations[2]["messages"][0].content)
        self.assertEqual([name for name, _ in self.calls], [RISK, NEWS])
        self.assertTrue(all(args["fund_code"] == "110020" for _, args in self.calls))
        self.assertEqual(result.tool_discovery.loaded_tools, [NEWS])
        self.assertEqual(result.usage.total_tokens, 60)
        self.assertTrue(any(item["phase"] == "tool_discovery" for item in self.progress))
        self.assertTrue(all(not call.is_error for call in result.tool_calls))

    async def test_discovery_cannot_execute_new_tool_in_same_model_round(self):
        result = await self.run_loop([
            [self.action(SEARCH_TOOL_NAME, {"query": "新闻"}, "search"), self.action(NEWS, call_id="guessed")],
            [self.action(NEWS, call_id="loaded")], "完成",
        ])
        self.assertTrue(result.tool_calls[1].is_error)
        self.assertFalse(result.tool_calls[2].is_error)
        self.assertEqual(len(self.calls), 1)
        # 每条模型 tool_call 都有对应回填；不能丢掉未知调用的消息导致供应商拒绝下一轮。
        messages = self.invocations[1]["messages"]
        self.assertEqual([m.tool_call_id for m in messages if isinstance(m, ToolMessage)], ["search", "guessed"])

    async def test_search_limit_removes_discovery_without_resetting_loop_budget(self):
        result = await self.run_loop([
            [self.action(SEARCH_TOOL_NAME, {"query": "新闻"}, "one")],
            [self.action(SEARCH_TOOL_NAME, {"query": "持仓"}, "two")],
            [self.action(HOLDINGS, call_id="read")], "完成",
        ], req=request(max_tool_rounds=3))
        self.assertNotIn(SEARCH_TOOL_NAME, self.invocations[2]["names"])
        self.assertEqual(self.invocations[3]["phase"], "forced_final_answer")
        self.assertEqual(self.invocations[3]["names"], ())
        self.assertEqual(result.stop_reason, "max_tool_rounds_reached")
        self.assertEqual(result.max_tool_rounds, 3)
        self.assertEqual(result.tool_discovery.loaded_tools, [NEWS, HOLDINGS])

    async def test_last_tool_round_removes_search_even_when_search_budget_remains(self):
        result = await self.run_loop([
            [self.action(SEARCH_TOOL_NAME, {"query": "新闻"}, "one")],
            [self.action(NEWS, call_id="read")], "完成",
        ], req=request(max_tool_rounds=2))
        self.assertNotIn(SEARCH_TOOL_NAME, self.invocations[1]["names"])
        self.assertIn(NEWS, self.invocations[1]["names"])
        self.assertEqual(len(result.tool_discovery.searches), 1)

    async def test_simple_read_keeps_original_fast_path(self):
        result = await self.run_loop([[self.action(RISK)], "完成"],
                                    req=request(message="回撤是多少？"))
        self.assertEqual(self.bindings, [(RISK,)])
        self.assertEqual(self.invocations[1]["names"], ())
        self.assertFalse(result.tool_discovery.enabled)
        self.assertEqual(len(self.invocations), 2)

    async def test_discovered_capital_tool_uses_trusted_identity_and_selection(self):
        selected_portfolio = "portfolio_0123456789abcdef"
        result = await self.run_loop([
            [self.action(SEARCH_TOOL_NAME, {"query": "机构资金"}, "search")],
            [self.action(CAPITAL, {"user_id": "other", "fund_code": "999999",
                                  "portfolio_id": "portfolio_ffffffffffffffff"}, "capital")], "完成",
        ], req=request(portfolio_id=selected_portfolio))
        self.assertFalse(result.tool_calls[-1].is_error)
        arguments = self.calls[0][1]
        self.assertEqual(arguments["user_id"], "discovery-test")
        self.assertEqual(arguments["fund_code"], "110020")
        self.assertEqual(arguments["portfolio_id"], selected_portfolio)

    async def test_product_cannot_guess_developer_tool_even_during_discovery(self):
        result = await self.run_loop([
            [self.action("read_file_tool", {"path": ".env"})], "此操作不在权限内。",
        ])
        self.assertTrue(result.tool_calls[0].is_error)
        self.assertEqual(self.calls, [])
        self.assertNotIn("read_file_tool", result.tools_available)

    async def test_trace_is_saved_to_existing_observation_metrics(self):
        from app.database import Database
        from app.services.comprehensive_agent import ComprehensiveAgentService
        from app.services.conversations import ConversationService
        from app.services.observability import AgentObservabilityService

        result = await self.run_loop([
            [self.action(SEARCH_TOOL_NAME, {"query": "新闻"})], "缺少具体事件证据，暂不下结论。",
        ])
        # 只在临时 SQL 库验证持久化，不向用户真实运行历史写入测试记录。
        with TemporaryDirectory() as directory:
            database = Database("sqlite:///" + str(Path(directory) / "observations.db"))
            try:
                service = AgentObservabilityService(database)
                service.record_run(run=result, user_id=result.user_id, duration_ms=20, source="eval")
                record = service.recent_runs(user_id=result.user_id)[0]
                self.assertEqual(record.metrics["tool_discovery"]["loaded_tools"], [NEWS])
                self.assertGreater(record.metrics["tool_discovery"]["bindings"][0]["schema_chars"], 0)
                self.assertEqual(record.usage.total_tokens, 30)
                # 会话摘要也持久化同一轨迹；它只记录能力发现，不能生成金融来源卡片。
                self.assertEqual(result.evidence, [])
                conversations = ConversationService(database)
                conversation = conversations.ensure(user_id=result.user_id, conversation_id=None)
                product_service = ComprehensiveAgentService(conversations=conversations, observability=service)
                conversations.append_exchange(
                    user_id=result.user_id, conversation_id=conversation["id"],
                    user_content=result.question, assistant_content=result.answer, run_id=result.run_id,
                    expected_updated_at=conversation["updated_at"],
                    run_metadata=product_service._compact_run_metadata(result, duration_ms=20),
                )
                saved = conversations.detail(conversation_id=conversation["id"], user_id=result.user_id)
                self.assertEqual(saved["messages"][-1]["run_metadata"]["tool_discovery"],
                                 result.tool_discovery.model_dump(mode="json"))
            finally:
                database.engine.dispose()


if __name__ == "__main__":
    unittest.main()

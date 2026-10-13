"""动态工具路由与按需 Prompt 的确定性测试。

这些测试不调用大模型，只验证用户问题进入 Agent Loop 前能否选中正确的小工具池。
这样路由错误会在本地快速暴露，不必每次消耗 API Token 才发现。
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.schemas import AgentChatRequest
from app.services.agent_execution_context import AgentExecutionContext
from app.services.agent_routing import HybridRouteDecision, agent_request_router
from app.services.prompt_builder import prompt_builder_service


class AgentRequestRouterTests(unittest.TestCase):
    """覆盖 FundScope 评测集和常用 Harness 请求。"""

    def route(self, message: str, *, developer: bool = False, **context):
        request = AgentChatRequest(
            user_id="chen",
            message=message,
            **context,
        )
        execution_context = (
            AgentExecutionContext.developer(user_id="chen", role="admin")
            if developer
            else AgentExecutionContext.product(user_id="chen")
        )
        return agent_request_router.route(
            request,
            execution_context=execution_context,
        )

    def test_profile_question_selects_only_profile_tool(self) -> None:
        result = self.route(
            "请简要介绍当前基金的名称、类型、风险等级和数据时间。",
            fund_code="110020",
        )

        self.assertEqual(result.name, "fund_profile")
        self.assertEqual(result.tool_names, ("get_fund_profile_tool",))

    def test_risk_question_selects_only_risk_tool(self) -> None:
        result = self.route(
            "这只基金最近一段历史净值的回撤和波动风险怎么样？",
            fund_code="110020",
        )

        self.assertEqual(result.name, "fund_risk")
        self.assertEqual(result.tool_names, ("get_fund_risk_tool",))

    def test_comprehensive_and_opinion_questions_use_assessment(self) -> None:
        assessment = self.route(
            "综合历史风险、市场情绪和消息面，告诉我这只基金当前处于什么状态。",
            fund_code="110020",
        )
        opinion = self.route(
            "结合现有全部数据，我当前更倾向继续持有、减仓还是观察？",
            fund_code="110020",
        )

        expected = ("get_fund_assessment_tool",)
        self.assertEqual(assessment.tool_names, expected)
        self.assertEqual(opinion.tool_names, expected)

    def test_explicit_full_fund_research_uses_one_brief_tool(self) -> None:
        result = self.route(
            "请全面分析这只基金，同时结合风险、持仓、市场情绪和新闻。",
            fund_code="161725",
        )

        self.assertEqual(result.name, "fund_research_brief")
        self.assertEqual(result.tool_names, ("get_fund_research_brief_tool",))

    def test_explicit_full_portfolio_research_uses_one_brief_tool(self) -> None:
        result = self.route(
            "请系统分析当前组合的业绩、量化风险、底层持仓和事件影响。",
            portfolio_id="portfolio_0123456789abcdef",
        )

        self.assertEqual(result.name, "portfolio_research_brief")
        self.assertEqual(
            result.tool_names,
            ("get_portfolio_research_brief_tool",),
        )

    def test_semantic_review_requires_explicit_independent_review(self) -> None:
        result = self.route(
            "请让 AI 独立检查规则误判并做语义复核。",
            fund_code="110020",
        )

        self.assertEqual(result.name, "fund_semantic")
        self.assertEqual(result.tool_names, ("get_fund_semantic_review_tool",))

    def test_specific_fund_and_market_request_merges_related_groups(self) -> None:
        result = self.route(
            "请查询基金档案、近一年净值表现、最大回撤和市场情绪。",
            fund_code="110020",
        )

        self.assertEqual(
            result.domains,
            ("fund_profile", "fund_risk", "fund_nav", "market"),
        )
        self.assertIn("get_fund_profile_tool", result.tool_names)
        self.assertIn("get_fund_risk_tool", result.tool_names)
        self.assertIn("get_market_sentiment_tool", result.tool_names)
        self.assertIn("get_fund_nav_summary_tool", result.tool_names)

    def test_market_snapshot_and_event_questions_use_different_tools(self) -> None:
        """市场领域先做确定性子路由，避免不同市场数据互相误调用。"""

        snapshot = self.route("今天市场宽度和成交活跃度怎么样？")
        capital = self.route("最近国家队和主力机构有什么资金布局线索？")
        hotspots = self.route("半导体热点最近是在升温还是降温？")
        events = self.route(
            "110020 近期有哪些政策和监管消息？",
            fund_code="110020",
        )

        self.assertEqual(snapshot.name, "market")
        self.assertEqual(snapshot.tool_names, ("get_market_sentiment_tool",))
        self.assertEqual(capital.name, "market")
        self.assertEqual(
            capital.tool_names,
            ("get_large_capital_signals_tool",),
        )
        self.assertEqual(events.name, "market")
        self.assertEqual(events.tool_names, ("search_market_events_tool",))
        self.assertEqual(hotspots.name, "market")
        self.assertEqual(hotspots.tool_names, ("get_market_hotspots_tool",))

    def test_sector_flow_questions_select_capital_in_fund_and_market_context(self) -> None:
        """板块资金提问不必带“国家队”等关键词，也应拿到资金统计工具。"""

        for context in ({}, {"fund_code": "008888"}):
            result = self.route("近5日哪些热门板块资金流入较多？", **context)
            self.assertIn("get_large_capital_signals_tool", result.tool_names)
            self.assertNotIn("search_market_events_tool", result.tool_names)

    def test_source_code_question_wins_over_fund_business_route(self) -> None:
        result = self.route(
            "当前项目的基金 Tool Calling 代码是如何实现的？",
            developer=True,
            fund_code="110020",
        )

        self.assertEqual(result.name, "coding")
        self.assertEqual(
            result.tool_names,
            ("list_files_tool", "read_file_tool", "search_text_tool"),
        )

    def test_direct_chat_has_no_tool_schema(self) -> None:
        result = self.route("你好，帮我把这句话改得更自然。")

        self.assertEqual(result.name, "direct")
        self.assertEqual(result.tool_names, ())

    def test_background_task_routes_to_job_tools(self) -> None:
        result = self.route(
            "启动一个后台任务，并告诉我任务进度。",
            developer=True,
        )

        self.assertEqual(result.name, "jobs")
        self.assertIn("background_job_start_tool", result.tool_names)

    def test_home_suggestion_routes_to_short_term_planning(self) -> None:
        result = self.route(
            "为一个复杂需求制定任务计划。",
            developer=True,
        )

        self.assertEqual(result.name, "planning")
        self.assertEqual(result.tool_names, ("todo_write_tool",))

    def test_explicit_tool_name_is_respected(self) -> None:
        result = self.route("请调用 get_market_sentiment_tool 获取当前状态。")

        self.assertEqual(result.name, "market")
        self.assertIn("get_market_sentiment_tool", result.tool_names)

    def test_product_mode_rejects_source_and_explicit_file_tools(self) -> None:
        """产品请求即使点名开发工具，也不能把对应 schema 交给模型。"""

        source = self.route("帮我读取当前项目源码并分析 Tool Calling。")
        explicit = self.route("请调用 read_file_tool 读取 app/config.py。")

        self.assertEqual(source.name, "access_denied")
        self.assertEqual(explicit.name, "access_denied")
        self.assertEqual(source.tool_names, ())
        self.assertEqual(explicit.tool_names, ())

    def test_product_mode_keeps_fund_rag_and_calculator_tools(self) -> None:
        """隔离开发能力不能误伤用户明确需要的产品工具。"""

        knowledge = self.route("请从知识库 RAG 检索相关说明。")
        calculator = self.route("帮我计算 1200 / 3 等于多少。")

        self.assertEqual(knowledge.tool_names, ("rag_search_tool",))
        self.assertEqual(calculator.tool_names, ("calculator_tool",))

    def test_short_follow_up_reuses_previous_user_domain(self) -> None:
        request = AgentChatRequest(user_id="chen", message="那风险呢？", fund_code="110020")
        result = agent_request_router.route(
            request,
            history=[
                {"role": "user", "content": "请分析当前基金"},
                {"role": "assistant", "content": "已完成基础分析"},
            ],
        )

        self.assertEqual(result.name, "fund_risk")


class HybridAgentRequestRouterTests(unittest.IsolatedAsyncioTestCase):
    """只给模糊业务追问使用模型分类；测试通过 Fake decision 避免真实 API。"""

    async def test_ambiguous_fund_follow_up_uses_bounded_model_domain(self) -> None:
        decision = HybridRouteDecision(
            domain="fund_risk",
            confidence=0.91,
            rationale="用户在追问当前基金的风险",
        )
        request = AgentChatRequest(
            user_id="chen",
            fund_code="110020",
            message="它现在怎么样？",
        )

        with patch.object(
            agent_request_router,
            "_classify_business_domain",
            new=AsyncMock(return_value=decision),
        ) as classifier:
            result = await agent_request_router.route_hybrid(request, history=[])

        classifier.assert_awaited_once()
        self.assertEqual(result.tool_names, ("get_fund_risk_tool",))
        self.assertEqual(result.strategy, "model")
        self.assertEqual(result.confidence, 0.91)

    async def test_classifier_failure_falls_back_to_unique_fund_context(self) -> None:
        request = AgentChatRequest(
            user_id="chen",
            fund_code="110020",
            message="它现在怎么样？",
        )
        with patch.object(
            agent_request_router,
            "_classify_business_domain",
            new=AsyncMock(side_effect=RuntimeError("temporary provider error")),
        ):
            result = await agent_request_router.route_hybrid(request, history=[])

        self.assertEqual(result.name, "fund_assessment")
        self.assertEqual(result.strategy, "fallback")
        self.assertEqual(result.tool_names, ("get_fund_assessment_tool",))

    async def test_ambiguous_request_without_target_asks_for_clarification(self) -> None:
        request = AgentChatRequest(
            user_id="chen",
            message="帮我看看这个怎么样？",
        )
        with patch.object(
            agent_request_router,
            "_classify_business_domain",
            new=AsyncMock(),
        ) as classifier:
            result = await agent_request_router.route_hybrid(request, history=[])

        classifier.assert_not_awaited()
        self.assertEqual(result.name, "clarify")
        self.assertTrue(result.needs_clarification)
        self.assertEqual(result.tool_names, ())


class RoutedPromptTests(unittest.TestCase):
    """确认动态 Prompt 不再加载与基金请求无关的运行状态。"""

    def test_profile_route_builds_small_domain_prompt(self) -> None:
        routed = prompt_builder_service.build(
            user_id="chen",
            tool_names=["get_fund_profile_tool"],
            active_fund_code="110020",
            domains=("fund_profile",),
        )
        full = prompt_builder_service.build(
            user_id="chen",
            tool_names=["get_fund_profile_tool"],
            active_fund_code="110020",
        )

        section_names = [section.name for section in routed.sections]
        self.assertEqual(
            section_names,
            ["identity", "fund_context", "tools", "permissions", "response"],
        )
        self.assertNotIn("memory", section_names)
        self.assertNotIn("tasks", section_names)
        self.assertLess(routed.total_chars, full.total_chars)
        self.assertIn("get_fund_profile_tool", routed.system_prompt)
        self.assertNotIn("get_fund_risk_tool", routed.system_prompt)


if __name__ == "__main__":
    unittest.main()

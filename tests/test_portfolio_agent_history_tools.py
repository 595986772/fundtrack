"""组合业绩和决策日志只读 Tools 的确定性集成测试。

测试不访问 DeepSeek，也不读取真实数据库。Fake 服务返回固定业务结果，用来证明：
1. Agent 获得的是压缩后的只读摘要，而不是整条日频曲线；
2. 路由、产品能力、Permission、System Prompt 和主 Agent Loop 都认识新工具；
3. 决策日志只描述历史判断，工具池中没有对应的写操作。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.schemas import AgentChatRequest
from app.services.agent_execution_context import AgentExecutionContext
from app.services.agent_routing import agent_request_router
from app.services.agent_tools import get_agent_tools, get_product_agent_tools
from app.services.portfolio_agent_tools import (
    get_portfolio_performance_tool,
    list_portfolio_decisions_tool,
)
from app.services.prompt_builder import prompt_builder_service
from app.services.tool_permissions import check_tool_permission


PORTFOLIO_ID = "portfolio_0123456789abcdef"


class FakePerformanceService:
    """返回包含日频曲线的完整结果，验证 Tool 会主动删掉大字段。"""

    def analyze(self, *, user_id, portfolio_id, months):
        assert user_id == "chen"
        assert portfolio_id == PORTFOLIO_ID
        return {
            "data_kind": "actual_portfolio_performance",
            "performance_label": "交易流水调整后的真实持仓业绩",
            "portfolio_id": portfolio_id,
            "portfolio_name": "长期组合",
            "position_mode": "actual",
            "period_months": months,
            "period_start": "2025-09-01",
            "period_end": "2026-08-31",
            "observation_count": 242,
            "twr_pct": 8.2,
            "xirr_pct": 7.8,
            "annualized_return_pct": 8.55,
            "annualized_volatility_pct": 12.1,
            "sharpe_ratio": 0.66,
            "sortino_ratio": 0.91,
            "risk_free_rate_annual_pct": 1.5,
            "risk_free_rate_source": "test",
            "max_drawdown": {
                "drawdown_pct": -6.4,
                "trough_date": "2026-04-12",
            },
            "benchmark": {
                "benchmark_name": "持仓对应业绩基准混合",
                "status": "matched",
                "period_return_pct": 5.1,
                "portfolio_aligned_return_pct": 8.2,
                "excess_return_pct": 3.1,
                "observation_count": 242,
                "coverage_pct": 100.0,
                "period_start": "2025-09-01",
                "period_end": "2026-08-31",
                "source": "test benchmark",
                "source_urls": ["https://example.com/benchmark"],
                "methodology": "test",
                "warnings": [],
            },
            "cash_flows": {
                "opening_value": 0,
                "contributions": 10_000,
                "withdrawals": 0,
                "fees": 10,
                "net_invested": 10_000,
                "ending_value": 10_820,
            },
            "attribution": [
                {
                    "fund_code": "110020",
                    "short_name": "基金A",
                    "ending_weight_pct": 60.0,
                    "pnl_contribution_amount": 700.0,
                    "return_contribution_pct_points": 6.9,
                    "return_basis": "total_return_reinvested",
                    "return_basis_label": "分红再投资总回报",
                },
                {
                    "fund_code": "110007",
                    "short_name": "基金B",
                    "ending_weight_pct": 40.0,
                    "pnl_contribution_amount": 120.0,
                    "return_contribution_pct_points": 1.3,
                    "return_basis": "total_return_reinvested",
                    "return_basis_label": "分红再投资总回报",
                },
            ],
            "series": [
                {
                    "date": f"2026-08-{day:02d}",
                    "portfolio_index": 100 + day,
                }
                for day in range(1, 29)
            ],
            "data_sources": [],
            "assumptions": ["交易日收盘后重放流水。"],
            "warnings": [],
            "methodology": "现金流剔除后计算 TWR，并用实际现金流计算 XIRR。",
            "disclaimer": "历史业绩仅用于学习与研究。",
        }


class FakeDecisionService:
    """返回两条日志，用 limit=1 验证服务结果会继续做上下文裁剪。"""

    def list(self, *, user_id, portfolio_id):
        assert user_id == "chen"
        assert portfolio_id == PORTFOLIO_ID
        common_snapshot = {
            "portfolio": {"name": "长期组合"},
            "performance": {
                "period_end": "2026-06-01",
                "twr_pct": 5.0,
                "xirr_pct": 4.8,
                "annualized_volatility_pct": 11.0,
                "max_drawdown_pct": -5.2,
                "sharpe_ratio": 0.5,
                "sortino_ratio": 0.7,
                "benchmark_status": "matched",
                "excess_return_pct": 1.2,
            },
            "assessment": {
                "assessment_date": "2026-06-01",
                "state_code": "balanced",
                "state_label": "风险收益均衡",
                "summary": "风险与支撑证据暂时均衡。",
                "risk_evidence_ids": ["risk:drawdown:test"],
                "supporting_evidence_ids": ["event:support:test"],
            },
            "research": {
                "headline": "维持观察",
                "summary": "当前没有破坏原假设。",
                "evidence_ids": ["risk:drawdown:test"],
            },
        }
        return [
            {
                "id": "decision_1111111111111111",
                "decision_date": date(2026, 6, 1),
                "target_date": date(2026, 8, 30),
                "action": "hold",
                "thesis": "回撤仍在承受范围，等待盈利修复信号。",
                "horizon_days": 90,
                "snapshot": common_snapshot,
                "outcome": {
                    "status": "review_ready",
                    "as_of_date": date(2026, 8, 30),
                    "elapsed_days": 90,
                    "target_days": 90,
                    "portfolio_return_pct": 4.0,
                    "benchmark_return_pct": 2.5,
                    "excess_return_pct": 1.5,
                    "note": "观察期已结束，可以复盘。",
                },
                "review_note": "原假设基本成立。",
                "reviewed_at": datetime(2026, 8, 31, tzinfo=timezone.utc),
            },
            {
                "id": "decision_2222222222222222",
                "decision_date": date(2026, 5, 1),
                "target_date": date(2026, 5, 31),
                "action": "watch",
                "thesis": "等待更多数据。",
                "horizon_days": 30,
                "snapshot": common_snapshot,
                "outcome": {"status": "tracking"},
                "review_note": "",
                "reviewed_at": None,
            },
        ]


class PortfolioHistoryToolTests(unittest.TestCase):
    def test_performance_tool_returns_bounded_summary_without_series(self):
        from app.services import portfolio_agent_tools as tool_module

        with patch.object(
            tool_module,
            "portfolio_performance_service",
            FakePerformanceService(),
        ):
            raw = get_portfolio_performance_tool.invoke(
                {
                    "user_id": "chen",
                    "portfolio_id": PORTFOLIO_ID,
                    "months": 12,
                }
            )
        payload = json.loads(raw)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data_kind"], "actual_portfolio_performance")
        self.assertEqual(payload["returns"]["twr_pct"], 8.2)
        self.assertEqual(payload["benchmark"]["excess_return_pct"], 3.1)
        self.assertEqual(len(payload["top_attribution"]), 2)
        self.assertNotIn("series", payload)
        self.assertIn("不是当前份额乘净值的账本市值", payload["valuation_note"])
        self.assertLess(len(raw), 4_000)

    def test_decision_tool_keeps_snapshot_outcome_boundary_and_limit(self):
        from app.services import portfolio_agent_tools as tool_module

        with patch.object(
            tool_module,
            "portfolio_decision_service",
            FakeDecisionService(),
        ):
            raw = list_portfolio_decisions_tool.invoke(
                {
                    "user_id": "chen",
                    "portfolio_id": PORTFOLIO_ID,
                    "limit": 1,
                }
            )
        payload = json.loads(raw)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["decision_count"], 2)
        self.assertEqual(payload["returned_count"], 1)
        self.assertEqual(payload["omitted_count"], 1)
        decision = payload["decisions"][0]
        self.assertEqual(decision["decision_id"], "decision_1111111111111111")
        self.assertEqual(decision["snapshot"]["performance"]["twr_pct"], 5.0)
        self.assertEqual(decision["outcome"]["portfolio_return_pct"], 4.0)
        self.assertIn("不等于交易已经真实成交", payload["interpretation_rules"][0])
        self.assertLess(len(raw), 4_000)

    def test_tools_are_product_read_only_and_prompt_explains_boundaries(self):
        all_names = {tool.name for tool in get_agent_tools()}
        product_names = {tool.name for tool in get_product_agent_tools()}
        expected = {
            "get_portfolio_performance_tool",
            "list_portfolio_decisions_tool",
        }
        self.assertTrue(expected.issubset(all_names))
        self.assertTrue(expected.issubset(product_names))
        self.assertNotIn("create_portfolio_decision_tool", all_names)

        context = AgentExecutionContext.product(user_id="chen")
        for tool_name in expected:
            permission = check_tool_permission(
                tool_name,
                {"user_id": "chen", "portfolio_id": PORTFOLIO_ID},
                execution_context=context,
            )
            self.assertTrue(permission.allowed)

        prompt = prompt_builder_service.build(
            user_id="chen",
            tool_names=sorted(expected),
            active_portfolio_id=PORTFOLIO_ID,
            domains=("portfolio_performance", "portfolio_decisions"),
        )
        self.assertIn("data_kind", prompt.system_prompt)
        self.assertIn("decision_id", prompt.system_prompt)
        self.assertIn("不能说成已经成交", prompt.system_prompt)
        self.assertNotIn("至少引用一个完整 evidence_id", prompt.system_prompt)

    def test_router_separates_performance_decision_and_single_fund_questions(self):
        performance = agent_request_router.route(
            AgentChatRequest(
                user_id="chen",
                portfolio_id=PORTFOLIO_ID,
                message="我的组合近一年业绩、最大回撤和超额收益怎么样？",
            )
        )
        decisions = agent_request_router.route(
            AgentChatRequest(
                user_id="chen",
                portfolio_id=PORTFOLIO_ID,
                message="我当时为什么维持持有，后来判断对了吗？",
            )
        )
        combined = agent_request_router.route(
            AgentChatRequest(
                user_id="chen",
                portfolio_id=PORTFOLIO_ID,
                message="查询组合当前收益，并复盘历史决策记录。",
            )
        )
        fund = agent_request_router.route(
            AgentChatRequest(
                user_id="chen",
                fund_code="110020",
                portfolio_id=PORTFOLIO_ID,
                message="这只基金最近净值表现怎么样？",
            )
        )

        self.assertEqual(
            performance.tool_names,
            ("get_portfolio_performance_tool",),
        )
        self.assertEqual(
            decisions.tool_names,
            ("list_portfolio_decisions_tool",),
        )
        self.assertEqual(
            combined.tool_names,
            (
                "get_portfolio_performance_tool",
                "list_portfolio_decisions_tool",
            ),
        )
        self.assertEqual(fund.tool_names, ("get_fund_nav_summary_tool",))


class PortfolioHistoryAgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_executes_both_read_tools_before_answering(self):
        """Fake 模型发起真实 tool_calls，证明新工具穿过完整 Agent Loop。"""

        from langchain_core.messages import AIMessage, ToolMessage

        from app.services import portfolio_agent_tools as tool_module
        from app.services import tool_agent as tool_agent_module
        from app.services.recovery import ModelCallResult

        class FakeChatModel:
            def __init__(self):
                self.bound_tool_names = []

            def bind_tools(self, tools):
                self.bound_tool_names = [tool.name for tool in tools]
                return self

        fake_model = FakeChatModel()
        observed_results = []

        async def fake_recovery_invoke(**kwargs):
            if kwargs["round_index"] == 1:
                return ModelCallResult(
                    response=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "portfolio-performance-1",
                                "name": "get_portfolio_performance_tool",
                                "args": {
                                    "user_id": "chen",
                                    "portfolio_id": PORTFOLIO_ID,
                                    "months": 12,
                                },
                                "type": "tool_call",
                            },
                            {
                                "id": "portfolio-decisions-1",
                                "name": "list_portfolio_decisions_tool",
                                "args": {
                                    "user_id": "chen",
                                    "portfolio_id": PORTFOLIO_ID,
                                    "limit": 5,
                                },
                                "type": "tool_call",
                            },
                        ],
                    )
                )
            observed_results.extend(
                message.content
                for message in kwargs["messages"]
                if isinstance(message, ToolMessage)
            )
            return ModelCallResult(
                response=AIMessage(content="已完成组合业绩与历史决策复盘。")
            )

        with (
            patch.object(
                tool_module,
                "portfolio_performance_service",
                FakePerformanceService(),
            ),
            patch.object(
                tool_module,
                "portfolio_decision_service",
                FakeDecisionService(),
            ),
            patch.object(
                tool_agent_module,
                "get_chat_model",
                return_value=fake_model,
            ),
            patch.object(
                tool_agent_module.model_recovery_service,
                "invoke",
                new=AsyncMock(side_effect=fake_recovery_invoke),
            ),
        ):
            response = await tool_agent_module.tool_calling_agent_service.chat(
                AgentChatRequest(
                    user_id="chen",
                    portfolio_id=PORTFOLIO_ID,
                    message="查询组合近一年收益，并复盘历史决策记录。",
                    max_tool_rounds=3,
                ),
                execution_context=AgentExecutionContext.product(user_id="chen"),
            )

        self.assertEqual(
            [call.tool_name for call in response.tool_calls],
            [
                "get_portfolio_performance_tool",
                "list_portfolio_decisions_tool",
            ],
        )
        self.assertTrue(all(not call.is_error for call in response.tool_calls))
        self.assertEqual(
            set(fake_model.bound_tool_names),
            {
                "get_portfolio_performance_tool",
                "list_portfolio_decisions_tool",
                # 按需发现已授权工具是现有产品入口的一部分；上方仍严格要求只执行两次业务工具。
                "search_tools_tool",
            },
        )
        self.assertTrue(any('"twr_pct": 8.2' in item for item in observed_results))
        self.assertTrue(any('"decision_id":' in item for item in observed_results))
        self.assertEqual(response.stop_reason, "model_finished")


if __name__ == "__main__":
    unittest.main()

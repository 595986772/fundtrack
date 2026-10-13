"""综合金融研究 Brief、Agent Tool 和可信参数注入测试。"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.schemas import AgentChatRequest, AgentToolCall
from app.services.agent_execution_context import AgentExecutionContext
from app.services.agent_routing import AgentRequestRoute
from app.services.agent_tool_executor import AgentToolExecutor
from app.services.agent_tools import get_product_agent_tools
from app.services.context_compactor import CompactTrace, ContextCompactorService
from app.services.financial_research_agent_tools import (
    get_fund_research_brief_tool,
    get_portfolio_research_brief_tool,
)
from app.services.financial_research_brief import FinancialResearchBriefService
from app.services.tool_agent import ToolCallingAgentService
from app.services.tool_permissions import check_tool_permission


PORTFOLIO_ID = "portfolio_0123456789abcdef"


class FakeFunds:
    def get_fund(self, fund_code: str) -> dict:
        return {
            "profile": {
                "fund_code": fund_code,
                "short_name": "半导体主题基金",
                "fund_type": "指数型-股票",
                "sample_role": "industry_theme",
                "benchmark": "半导体芯片指数收益率*95%",
                "source_url": "https://example.test/fund",
            }
        }


class FakeAnalyze:
    def __init__(self, payload: dict | None = None, error: str = "") -> None:
        self.payload = payload or {}
        self.error = error

    def analyze(self, **kwargs) -> dict:
        if self.error:
            raise RuntimeError(self.error)
        return self.payload


class FakeAssess(FakeAnalyze):
    def assess(self, **kwargs) -> dict:
        return self.analyze(**kwargs)


class FakeDisclosures:
    def list_for_funds(self, fund_codes: list[str]) -> dict:
        code = fund_codes[0]
        return {
            code: {
                "fund_code": code,
                "data_mode": "database",
                "latest_report_date": "2026-06-30",
                "freshness_status": "fresh",
                "security_count": 2,
                "industry_count": 1,
                "disclosed_stock_weight_pct": 80,
                "disclosed_bond_weight_pct": 0,
                "industry_coverage_pct": 90,
                "security_holdings": [
                    {
                        "asset_type": "stock",
                        "security_code": "688981",
                        "security_name": "中芯国际",
                        "weight_pct": 8.0,
                        "report_date": "2026-06-30",
                        "source_url": "https://example.test/disclosure",
                    }
                ],
                "industry_allocations": [],
                "warnings": [],
            }
        }


class FakePortfolios:
    def get(self, *, user_id: str, portfolio_id: str) -> dict:
        if user_id != "chen" or portfolio_id != PORTFOLIO_ID:
            raise ValueError("组合不存在")
        return {
            "id": portfolio_id,
            "name": "研究组合",
            "position_mode": "allocation",
            "holdings": [
                {
                    "fund_code": "012345",
                    "short_name": "半导体主题基金",
                    "fund_type": "指数型-股票",
                    "sample_role": "industry_theme",
                    "weight_pct": 100,
                    "latest_nav_date": "2026-08-31",
                }
            ],
        }


def fund_risk() -> dict:
    return {
        "period_start": "2025-08-31",
        "period_end": "2026-08-31",
        "observation_count": 240,
        "period_return_pct": 8.5,
        "annualized_volatility_pct": 22.1,
        "downside_volatility_pct": 14.2,
        "current_drawdown_pct": 5.2,
        "worst_daily_return_pct": -4.1,
        "longest_drawdown_days": 80,
        "max_drawdown": {"drawdown_pct": 18.0},
        "return_basis_label": "总回报",
        "return_basis_status": "complete",
        "data_quality_warnings": [],
        "risk_adjusted": {"sharpe_ratio": 0.4},
        "benchmark_comparison": {
            "benchmark_name": "半导体芯片指数",
            "status": "matched",
            "period_return_pct": 10.0,
            "fund_aligned_return_pct": 8.5,
            "excess_return_pct": -1.5,
            "coverage_pct": 100.0,
            "period_end": "2026-08-31",
            "warnings": [],
        },
    }


def assessment(*, portfolio: bool = False) -> dict:
    evidence = {
        "evidence_id": "risk:portfolio:test" if portfolio else "risk:fund:test",
        "kind": "fund_risk",
        "direction": "caution",
        "title": "历史波动",
        "value_text": "22.10%",
        "explanation": "历史波动较高",
        "data_date": "2026-08-31",
        "source_name": "test",
    }
    return {
        "state_code": "attention",
        "state_label": "需要关注",
        "summary": "风险证据高于支撑证据。",
        "score_breakdown": {"caution_points": 4, "supportive_points": 2},
        "assessment_date": "2026-08-31",
        "concentration": {"max_weight_pct": 100},
        "available_dimensions": ["fund_risk", "market_state", "market_event"],
        "active_event_count": 1,
        "component_dates": {
            "market_state": "2026-08-31",
            "market_events": "2026-08-31",
        },
        "risk_evidence": [evidence],
        "supporting_evidence": [],
        "neutral_evidence": [],
    }


def quant_risk() -> dict:
    return {
        "period_start": "2025-08-31",
        "period_end": "2026-08-31",
        "observation_count": 220,
        "period_return_pct": 8,
        "annualized_volatility_pct": 22,
        "daily_cvar_95_pct": 2.4,
        "max_drawdown": {"drawdown_pct": 18},
        "top_risk_contributor": {"fund_code": "012345"},
        "risk_contributions": [],
        "correlation": {},
        "return_basis_label": "总回报",
        "data_quality_warnings": [],
    }


def performance() -> dict:
    return {
        "data_kind": "simulated_allocation_performance",
        "performance_label": "当前目标权重的历史模拟业绩",
        "position_mode": "allocation",
        "period_start": "2025-08-31",
        "period_end": "2026-08-31",
        "twr_pct": 8,
        "xirr_pct": None,
        "annualized_return_pct": 8,
        "annualized_volatility_pct": 22,
        "sharpe_ratio": 0.4,
        "sortino_ratio": 0.6,
        "max_drawdown": {"drawdown_pct": 18},
        "warnings": [],
        "benchmark": {
            "benchmark_name": "半导体芯片指数",
            "status": "matched",
            "period_return_pct": 10,
            "portfolio_aligned_return_pct": 8,
            "excess_return_pct": -2,
            "coverage_pct": 100,
        },
    }


def exposure() -> dict:
    return {
        "summary": "证券穿透覆盖 80%",
        "oldest_report_date": "2026-06-30",
        "latest_report_date": "2026-06-30",
        "data_quality": {
            "status": "complete",
            "freshness_status": "fresh",
            "security_coverage_pct": 80,
            "warnings": [],
        },
        "concentration": {"industry_state": "high"},
        "security_exposures": [
            {
                "evidence_id": "lookthrough:security:test",
                "security_code": "688981",
                "security_name": "中芯国际",
                "portfolio_exposure_pct": 8,
                "report_date": "2026-06-30",
            }
        ],
        "industry_exposures": [],
        "fund_overlaps": [],
    }


def impacts() -> dict:
    return {
        "summary": "一条事件与组合相关。",
        "event_data_cutoff": "2026-08-31",
        "latest_holding_report_date": "2026-06-30",
        "quality": {"status": "complete"},
        "impacts": [
            {
                "impact_evidence_id": "portfolio-impact:test",
                "event_id": "event_test",
                "title": "半导体政策",
                "event_date": "2026-08-30",
                "direction": "supportive",
                "affected_exposure_pct": 80,
            }
        ],
    }


def build_service(*, event_error: str = "") -> FinancialResearchBriefService:
    return FinancialResearchBriefService(
        funds=FakeFunds(),
        fund_risk=FakeAnalyze(fund_risk()),
        fund_assessment=FakeAssess(assessment()),
        fund_disclosures=FakeDisclosures(),
        portfolios=FakePortfolios(),
        portfolio_assessment=FakeAssess(assessment(portfolio=True)),
        portfolio_quant_risk=FakeAnalyze(quant_risk()),
        portfolio_performance=FakeAnalyze(performance()),
        portfolio_exposure=FakeAnalyze(exposure()),
        portfolio_event_impacts=FakeAnalyze(impacts(), error=event_error),
    )


class FinancialResearchBriefTests(unittest.IsolatedAsyncioTestCase):
    def test_fund_brief_is_sufficient_and_bounded(self) -> None:
        result = build_service().build_fund(
            fund_code="012345",
            question="请全面分析这只基金",
            months=12,
        )

        self.assertEqual(result["evidence_gate"]["status"], "sufficient")
        self.assertEqual(result["execution"]["task_count"], 3)
        self.assertEqual(
            result["evidence_summary"]["disclosure"]["top_securities"][0][
                "security_code"
            ],
            "688981",
        )
        self.assertNotIn("nav_points", json.dumps(result, ensure_ascii=False))

    def test_one_failed_branch_returns_limited_portfolio_brief(self) -> None:
        result = build_service(event_error="事件数据库暂时不可用").build_portfolio(
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            question="请系统分析当前组合风险",
            months=12,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["execution"]["completed_count"], 4)
        self.assertEqual(result["execution"]["failed_count"], 1)
        self.assertEqual(result["evidence_gate"]["status"], "limited")
        self.assertEqual(
            result["evidence_summary"]["quantitative_risk"]["daily_cvar_95_pct"],
            2.4,
        )

    def test_agent_tools_return_brief_contract(self) -> None:
        from app.services import financial_research_agent_tools as tool_module

        with patch.object(tool_module, "financial_research_brief_service", build_service()):
            fund = json.loads(
                get_fund_research_brief_tool.invoke(
                    {"fund_code": "012345", "question": "请全面分析", "months": 12}
                )
            )
            portfolio = json.loads(
                get_portfolio_research_brief_tool.invoke(
                    {
                        "user_id": "chen",
                        "portfolio_id": PORTFOLIO_ID,
                        "question": "请系统分析",
                        "months": 12,
                    }
                )
            )

        self.assertEqual(fund["brief_version"], "financial-research-brief-v1")
        self.assertEqual(portfolio["scope"]["kind"], "portfolio")

    async def test_executor_overrides_identity_scope_and_question(self) -> None:
        class CapturingTool:
            arguments = None

            async def ainvoke(self, arguments):
                self.arguments = arguments
                return json.dumps({"ok": True})

        class ToolMessage:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        tool = CapturingTool()
        request = AgentChatRequest(
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            message="我计划持有三年，能承受百分之十五回撤，请全面分析",
        )
        await AgentToolExecutor().execute(
            tool_call={
                "name": "get_portfolio_research_brief_tool",
                "args": {
                    "user_id": "other",
                    "portfolio_id": "portfolio_ffffffffffffffff",
                    "question": "被模型改写的问题",
                },
                "id": "tool-call-1",
            },
            tool_map={"get_portfolio_research_brief_tool": tool},
            request=request,
            execution_context=AgentExecutionContext.product(user_id="chen"),
            tool_index=1,
            ToolMessage=ToolMessage,
        )

        self.assertEqual(tool.arguments["user_id"], "chen")
        self.assertEqual(tool.arguments["portfolio_id"], PORTFOLIO_ID)
        self.assertEqual(tool.arguments["question"], request.message)

    def test_tools_are_product_visible_and_permissioned(self) -> None:
        names = {tool.name for tool in get_product_agent_tools()}
        self.assertIn("get_fund_research_brief_tool", names)
        self.assertIn("get_portfolio_research_brief_tool", names)
        context = AgentExecutionContext.product(user_id="chen")
        self.assertTrue(
            check_tool_permission(
                "get_portfolio_research_brief_tool",
                {"user_id": "chen"},
                execution_context=context,
            ).allowed
        )

    def test_context_compact_keeps_financial_facts_and_evidence_ids(self) -> None:
        """长 Brief 进入模型前应缩短，但不能只剩开头预览。"""

        class FakeToolMessage:
            type = "tool"

            def __init__(self, content: str) -> None:
                self.content = content
                self.tool_call_id = "research-call-1"

        brief = build_service().build_fund(
            fund_code="012345",
            question="请全面分析这只基金",
            months=12,
        )
        original = json.dumps(brief, ensure_ascii=False)
        message = FakeToolMessage(original)
        compactor = ContextCompactorService()

        # 单元测试不需要真的向 data/agent_context 写文件，只验证返回给模型的内容。
        with patch.object(
            compactor,
            "_save_original_tool_result",
            return_value="data/agent_context/research-test.json",
        ):
            traces = compactor.compact_messages(
                [message],
                round_index=1,
                max_tool_message_chars=500,
            )

        compacted = json.loads(message.content)
        self.assertEqual(compacted["compact_strategy"], "financial_research_fields_v1")
        self.assertEqual(compacted["answer_facts"], brief["answer_facts"])
        self.assertEqual(compacted["evidence_gate"]["status"], "sufficient")
        self.assertEqual(
            compacted["evidence_summary"]["historical_risk"][
                "annualized_volatility_pct"
            ],
            22.1,
        )
        self.assertEqual(
            compacted["evidence_summary"]["disclosure"]["top_securities"][0][
                "evidence_id"
            ],
            "disclosure:012345:688981:2026-06-30",
        )
        self.assertLess(len(message.content), len(original))
        self.assertEqual(len(traces), 1)

    def test_portfolio_context_compact_keeps_quant_and_event_evidence(self) -> None:
        """组合摘要删除矩阵等大对象，同时保留回答所需的跨域事实。"""

        class FakeToolMessage:
            type = "tool"

            def __init__(self, content: str) -> None:
                self.content = content
                self.tool_call_id = "portfolio-research-call-1"

        brief = build_service().build_portfolio(
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            question="请系统分析当前组合风险",
            months=12,
        )
        original = json.dumps(brief, ensure_ascii=False)
        message = FakeToolMessage(original)
        compactor = ContextCompactorService()
        with patch.object(
            compactor,
            "_save_original_tool_result",
            return_value="data/agent_context/portfolio-research-test.json",
        ):
            compactor.compact_messages(
                [message],
                round_index=1,
                max_tool_message_chars=500,
            )

        compacted = json.loads(message.content)
        summary = compacted["evidence_summary"]
        self.assertEqual(summary["quantitative_risk"]["daily_cvar_95_pct"], 2.4)
        self.assertEqual(
            summary["event_impacts"]["top_impacts"][0]["impact_evidence_id"],
            "portfolio-impact:test",
        )
        self.assertEqual(
            summary["lookthrough"]["top_securities"][0]["evidence_id"],
            "lookthrough:security:test",
        )
        self.assertNotIn("risk_contributions", message.content)
        self.assertNotIn("correlation", message.content)
        self.assertNotIn("source_url", message.content)
        self.assertLess(len(message.content), len(original))

    def test_structured_compact_can_still_finalize_single_brief_route(self) -> None:
        """模型拿到字段级摘要后应进入解释轮，而不是重复调用同一个 Tool。"""

        route = AgentRequestRoute(
            name="fund_research_brief",
            domains=("fund_research_brief",),
            tool_names=("get_fund_research_brief_tool",),
            include_mcp_tools=False,
            reason="test",
        )
        call = AgentToolCall(
            tool_name="get_fund_research_brief_tool",
            tool_args={"fund_code": "012345"},
            tool_result=json.dumps({"ok": True}),
        )
        trace = CompactTrace(
            event="ContextCompact",
            round_index=1,
            before_chars=10_000,
            after_chars=4_000,
            compacted_messages=1,
            reason="tool_result_too_long",
        )

        self.assertTrue(
            ToolCallingAgentService._should_finalize_single_read_route(
                route=route,
                round_tool_calls=[call],
                compact_traces=[trace],
            )
        )


if __name__ == "__main__":
    unittest.main()

"""金融研究计划器和证据闸门的纯 Python 测试。"""

from __future__ import annotations

import unittest

from app.services.financial_research_planner import (
    FinancialEvidenceGate,
    FinancialResearchPlanner,
)


def completed(data: dict) -> dict:
    return {"status": "completed", "duration_ms": 1.0, "data": data, "error": None}


def good_fund_tasks(*, active_events: int = 1) -> dict:
    return {
        "fund_risk": completed(
            {
                "period_end": "2026-08-31",
                "observation_count": 240,
                "data_quality_warnings": [],
                "benchmark_comparison": {
                    "status": "matched",
                    "coverage_pct": 100,
                    "period_end": "2026-08-31",
                },
            }
        ),
        "fund_assessment": completed(
            {
                "available_dimensions": ["fund_risk", "market_state", "market_event"],
                "active_event_count": active_events,
                "component_dates": {
                    "market_state": "2026-08-31",
                    "market_events": "2026-08-31",
                },
                "risk_evidence": [
                    {"evidence_id": "risk:fund:test", "kind": "fund_risk"}
                ],
                "supporting_evidence": [],
                "neutral_evidence": [],
            }
        ),
        "fund_disclosure": completed(
            {
                "fund_code": "161725",
                "data_mode": "database",
                "freshness_status": "fresh",
                "latest_report_date": "2026-06-30",
                "security_count": 10,
                "industry_count": 3,
                "industry_coverage_pct": 92,
                "disclosed_stock_weight_pct": 94,
                "disclosed_bond_weight_pct": 0,
                "security_holdings": [
                    {"security_code": "600519", "report_date": "2026-06-30"}
                ],
                "warnings": [],
            }
        ),
    }


def good_portfolio_tasks() -> dict:
    return {
        "portfolio_assessment": completed(
            {
                "summary": "规则证据已汇总",
                "assessment_date": "2026-08-31",
                "risk_evidence": [{"evidence_id": "risk:portfolio:test"}],
                "supporting_evidence": [],
                "neutral_evidence": [],
            }
        ),
        "portfolio_quant_risk": completed(
            {
                "observation_count": 220,
                "period_end": "2026-08-31",
                "data_quality_warnings": [],
            }
        ),
        "portfolio_performance": completed(
            {
                "performance_label": "目标权重历史模拟",
                "period_end": "2026-08-31",
                "warnings": [],
                "benchmark": {"status": "matched", "coverage_pct": 98},
            }
        ),
        "portfolio_exposure": completed(
            {
                "latest_report_date": "2026-06-30",
                "data_quality": {
                    "status": "complete",
                    "freshness_status": "fresh",
                    "security_coverage_pct": 82,
                    "warnings": [],
                },
                "security_exposures": [
                    {"evidence_id": "lookthrough:security:test"}
                ],
                "industry_exposures": [],
            }
        ),
        "portfolio_event_impacts": completed(
            {
                "summary": "没有需要优先关注的事件",
                "event_data_cutoff": "2026-08-31",
                "impacts": [],
            }
        ),
    }


class FinancialResearchPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = FinancialResearchPlanner()
        self.gate = FinancialEvidenceGate()

    def test_theme_fund_plan_contains_cross_domain_requirements(self) -> None:
        plan = self.planner.plan(
            question="请全面分析这只半导体基金的主要风险",
            scope="fund",
            profiles=[
                {
                    "fund_code": "012345",
                    "short_name": "半导体主题基金",
                    "fund_type": "指数型-股票",
                    "sample_role": "industry_theme",
                }
            ],
        )

        self.assertEqual(plan["intent"], "risk_overview")
        self.assertEqual(plan["asset_classes"][0]["code"], "equity_theme")
        requirement_keys = {item["key"] for item in plan["requirements"]}
        self.assertTrue(
            {"historical_risk", "holdings_exposure", "market_regime", "event_context"}
            <= requirement_keys
        )
        self.assertEqual(plan["execution_stages"][1]["mode"], "parallel")

    def test_no_active_event_is_valid_evidence_not_missing_data(self) -> None:
        plan = self.planner.plan(
            question="请全面分析当前风险",
            scope="fund",
            profiles=[{"fund_code": "161725", "sample_role": "industry_theme"}],
        )
        result = self.gate.evaluate(
            plan=plan,
            scope_data={
                "profile": {"fund_code": "161725", "short_name": "白酒基金"}
            },
            task_results=good_fund_tasks(active_events=0),
            question="请全面分析当前风险",
        )

        event = next(
            item for item in result["requirements"] if item["key"] == "event_context"
        )
        self.assertEqual(event["status"], "satisfied")
        self.assertIn("没有", event["detail"])
        self.assertEqual(result["status"], "sufficient")

    def test_decision_without_user_context_is_conditional_not_rejected(self) -> None:
        question = "结合全部信息，我现在应该减仓吗？"
        plan = self.planner.plan(
            question=question,
            scope="fund",
            profiles=[{"fund_code": "161725", "sample_role": "industry_theme"}],
        )
        result = self.gate.evaluate(
            plan=plan,
            scope_data={
                "profile": {"fund_code": "161725", "short_name": "白酒基金"}
            },
            task_results=good_fund_tasks(),
            question=question,
        )

        self.assertEqual(plan["intent"], "decision_support")
        self.assertEqual(result["status"], "limited")
        self.assertEqual(result["conclusion_policy"], "conditional_only")
        self.assertIn("investor_context", result["critical_missing_keys"])
        self.assertGreaterEqual(len(result["clarification_questions"]), 3)

    def test_missing_quant_risk_downgrades_portfolio_research(self) -> None:
        plan = self.planner.plan(
            question="请系统分析组合风险",
            scope="portfolio",
            profiles=[{"fund_type": "指数型-股票"}],
        )
        tasks = good_portfolio_tasks()
        tasks["portfolio_quant_risk"] = {
            "status": "failed",
            "duration_ms": 1.0,
            "data": None,
            "error": "共同日期不足",
        }
        result = self.gate.evaluate(
            plan=plan,
            scope_data={"holdings": [{"fund_code": "110020"}]},
            task_results=tasks,
            question="请系统分析组合风险",
        )

        self.assertEqual(result["status"], "limited")
        self.assertIn("quantitative_risk", result["critical_missing_keys"])

    def test_fixed_income_plan_names_unsupported_risks(self) -> None:
        plan = self.planner.plan(
            question="请全面分析债券基金风险",
            scope="fund",
            profiles=[{"fund_type": "债券型-混合二级"}],
        )

        self.assertEqual(plan["asset_classes"][0]["code"], "fixed_income")
        self.assertEqual(
            plan["asset_specific_checks"][0]["status"],
            "not_yet_supported",
        )
        self.assertIn("久期", plan["asset_specific_checks"][0]["label"])

        # 即使通用净值、基准、事件和披露都有数据，缺少债券核心的久期/信用风险
        # 仍不能宣称完成了一份充分的固定收益研究。
        result = self.gate.evaluate(
            plan=plan,
            scope_data={"profile": {"fund_code": "000001", "short_name": "债基"}},
            task_results=good_fund_tasks(),
            question="请全面分析债券基金风险",
        )
        self.assertEqual(result["status"], "limited")
        self.assertEqual(result["conclusion_policy"], "conditional_only")
        self.assertEqual(
            result["asset_specific_gaps"][0]["key"],
            "duration_credit_liquidity",
        )


if __name__ == "__main__":
    unittest.main()

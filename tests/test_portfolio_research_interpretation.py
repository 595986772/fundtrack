"""组合研究 DeepSeek 汇合层的边界测试。

这些测试全部使用 Fake ChatModel，不访问真实模型。测试重点是 Harness 是否能够：
接受合法结构、解析 Python 指标、拒绝越界引用、显式重试，以及安全降级。
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.services.portfolio_research_interpretation import (
    MAX_MODEL_ATTEMPTS,
    ModelPortfolioFinding,
    ModelPortfolioInterpretation,
    PortfolioResearchInterpretationError,
    PortfolioResearchInterpretationService,
)


def research_inputs() -> dict:
    """构造一组覆盖指标目录和证据目录的最小研究输入。"""

    return {
        "portfolio": {
            "id": "portfolio_0123456789abcdef",
            "name": "语义测试组合",
            "holdings": [
                {
                    "fund_code": "110020",
                    "short_name": "沪深300联接",
                    "weight_pct": 60.0,
                },
                {
                    "fund_code": "110007",
                    "short_name": "稳健债券",
                    "weight_pct": 40.0,
                },
            ],
        },
        "assessment": {
            "assessment_date": "2026-08-28",
            "state_code": "cautious",
            "state_label": "谨慎观察",
            "confidence": "medium",
            "summary": "风险证据暂时高于支撑证据。",
            "score_breakdown": {
                "caution_points": 2.4,
                "supportive_points": 1.2,
            },
            "concentration": {
                "max_weight_pct": 60.0,
                "effective_fund_count": 1.92,
                "state_code": "concentrated",
                "state_label": "偏集中",
            },
            "risk_evidence": [
                {
                    "evidence_id": "risk:test:1",
                    "kind": "fund_risk",
                    "title": "波动风险证据",
                    "value_text": "-6.40%",
                    "explanation": "历史波动需要关注。",
                    "data_date": "2026-08-28",
                    "source_name": "测试净值",
                    "affected_fund_codes": ["110020"],
                }
            ],
            "supporting_evidence": [
                {
                    "evidence_id": "support:test:1",
                    "kind": "market_state",
                    "title": "市场支撑证据",
                    "explanation": "市场宽度保持均衡。",
                    "data_date": "2026-08-28",
                    "source_name": "测试市场",
                }
            ],
            "neutral_evidence": [],
            "limitations": ["测试数据不代表真实市场。"],
        },
        "quant_risk": {
            "period_months": 12,
            "period_start": "2025-08-28",
            "period_end": "2026-08-28",
            "period_return_pct": 4.2,
            "annualized_volatility_pct": 8.5,
            "daily_cvar_95_pct": 1.15,
            "max_drawdown": {"drawdown_pct": 6.4},
            "top_risk_contributor": {
                "fund_code": "110020",
                "short_name": "沪深300联接",
                "weight_pct": 60.0,
                "absolute_risk_share_pct": 82.35,
            },
            "limitations": ["历史风险不代表未来。"],
        },
        "performance_snapshot": {
            "data_kind": "simulated_allocation_performance",
            "performance_label": "当前目标权重的历史模拟业绩",
            "position_mode": "allocation",
            "period_months": 12,
            "period_start": "2025-08-28",
            "period_end": "2026-08-28",
            "observation_count": 240,
            "twr_pct": 4.2,
            "xirr_pct": None,
            "annualized_return_pct": 4.3,
            "annualized_volatility_pct": 8.5,
            "max_drawdown": {"drawdown_pct": -6.4},
            "benchmark": {
                "status": "matched",
                "benchmark_name": "测试混合基准",
                "excess_return_pct": 1.2,
            },
            "attribution": [
                {
                    "fund_code": "110020",
                    "short_name": "沪深300联接",
                    "return_contribution_pct_points": 3.5,
                }
            ],
            "warnings": ["当前配置没有真实现金流水。"],
        },
        "market_context": {
            "data_date": "2026-08-28",
            "state_code": "balanced",
            "state_label": "震荡均衡",
            "summary": "市场方向暂未形成一致信号。",
            "signals": [
                {
                    "label": "趋势",
                    "direction": "neutral",
                    "evidence": "短中期方向不同。",
                }
            ],
            "evidence_ids": ["market:test:1"],
            "relevant_event_count": 1,
            "events": [
                {
                    "event_id": "event:test:1",
                    "event_date": "2026-08-27",
                    "title": "组合相关市场事件",
                    "summary": "该事件与宽基持仓相关。",
                    "impact_direction": "neutral",
                    "source_name": "测试新闻",
                }
            ],
        },
        "decision_history": {
            "decision_count": 1,
            "returned_count": 1,
            "omitted_count": 0,
            "has_decisions": True,
            "decisions": [
                {
                    "decision_id": "decision_1111111111111111",
                    "decision_date": "2026-06-01",
                    "action": "hold",
                    "action_label": "维持持有",
                    "thesis": "回撤仍在承受范围，等待市场方向更明确。",
                    "snapshot": {
                        "assessment": {
                            "state_label": "风险收益均衡",
                        }
                    },
                    "outcome": {
                        "status": "review_ready",
                        "as_of_date": "2026-08-28",
                        "portfolio_return_pct": 3.0,
                        "benchmark_return_pct": 2.0,
                        "excess_return_pct": 1.0,
                        "note": "观察期接近结束，可以准备复盘。",
                    },
                }
            ],
            "note": "决策不代表真实成交。",
        },
    }


def valid_model_result() -> ModelPortfolioInterpretation:
    """返回不自造数字、只引用白名单键的合法模型结果。"""

    return ModelPortfolioInterpretation(
        current_state="cautious",
        confidence=0.78,
        headline="组合风险线索略占上风",
        summary="集中度与历史波动需要优先关注，市场环境暂未提供一致方向。",
        key_findings=[
            ModelPortfolioFinding(
                tone="caution",
                title="风险与支撑并不对称",
                detail="规则风险证据较强，应结合历史波动理解当前状态。",
                evidence_ids=["risk:test:1"],
                metric_keys=[
                    "rule.caution_points",
                    "risk.annualized_volatility_pct",
                ],
                fund_codes=[],
            ),
            ModelPortfolioFinding(
                tone="neutral",
                title="市场方向仍有分歧",
                detail="当前市场信号不足以形成单一方向判断。",
                evidence_ids=["market:test:1", "event:test:1"],
                metric_keys=["market.relevant_event_count"],
                fund_codes=[],
            ),
            ModelPortfolioFinding(
                tone="neutral",
                title="历史业绩与决策需要结合",
                detail="后续表现应与当时理由和相对基准结果一起复盘。",
                evidence_ids=["decision_1111111111111111"],
                metric_keys=[
                    "performance.twr_pct",
                    "decision.decision_1111111111111111.excess_return_pct",
                ],
                fund_codes=[],
            ),
        ],
        conflicts=["规则风险信号与市场均衡状态没有完全同向。"],
        uncertainties=["历史净值无法代表未来路径。"],
        conditional_view="若风险承受能力有限，应优先检查集中度；否则可继续观察证据变化。",
        follow_up_items=["关注后续市场方向是否趋于一致。"],
    )


class FakeStructuredModel:
    """按顺序返回测试结果，并记录模型实际收到的有界 JSON。"""

    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.call_count = 0
        self.model_input: dict = {}

    def invoke(self, messages):
        self.model_input = json.loads(messages[-1].content)
        response = self.responses[min(self.call_count, len(self.responses) - 1)]
        self.call_count += 1
        if isinstance(response, Exception):
            raise response
        return response


class FakeChatModel:
    """模拟 ChatOpenAI.with_structured_output 的同步调用接口。"""

    def __init__(self, structured_model: FakeStructuredModel) -> None:
        self.structured_model = structured_model
        self.schema = None
        self.method = None

    def with_structured_output(self, schema, *, method):
        self.schema = schema
        self.method = method
        return self.structured_model


class PortfolioResearchInterpretationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PortfolioResearchInterpretationService()
        self.inputs = research_inputs()

    def test_valid_result_resolves_python_metrics_and_evidence(self) -> None:
        structured = FakeStructuredModel([valid_model_result()])
        model = FakeChatModel(structured)

        with patch(
            "app.services.portfolio_research_interpretation.get_chat_model",
            return_value=model,
        ) as model_factory:
            result = self.service.interpret(**self.inputs)

        self.assertEqual(result["status"], "accepted")
        self.assertTrue(result["model_used"])
        self.assertEqual(result["attempt_count"], 1)
        self.assertEqual(model.schema, ModelPortfolioInterpretation)
        self.assertEqual(model.method, "json_mode")
        model_factory.assert_called_once_with(
            max_retries=0,
            temperature=0,
            thinking=False,
        )

        first_finding = result["key_findings"][0]
        self.assertEqual(
            first_finding["metric_references"][1]["value_text"],
            "8.50%",
        )
        self.assertEqual(
            first_finding["evidence_references"][0]["evidence_id"],
            "risk:test:1",
        )
        self.assertEqual(
            first_finding["evidence_references"][0]["value_text"],
            "-6.40%",
        )
        self.assertEqual(
            structured.model_input["task"],
            "synthesize_portfolio_research",
        )
        self.assertIn("quantitative_metrics", structured.model_input)
        self.assertIn("evidence_catalog", structured.model_input)
        self.assertEqual(
            structured.model_input["performance_snapshot"]["twr_pct"],
            4.2,
        )
        self.assertTrue(structured.model_input["decision_history"]["has_decisions"])
        decision_finding = result["key_findings"][2]
        self.assertEqual(
            decision_finding["evidence_references"][0]["kind"],
            "portfolio_decision",
        )
        self.assertEqual(
            decision_finding["metric_references"][1]["value_text"],
            "+1.00%",
        )

    def test_invalid_reference_retries_then_raises(self) -> None:
        invalid = valid_model_result()
        invalid.key_findings[0].metric_keys = ["invented.metric"]
        structured = FakeStructuredModel([invalid, invalid])

        with patch(
            "app.services.portfolio_research_interpretation.get_chat_model",
            return_value=FakeChatModel(structured),
        ):
            with self.assertRaisesRegex(
                PortfolioResearchInterpretationError,
                "连续两次",
            ):
                self.service.interpret(**self.inputs)

        self.assertEqual(structured.call_count, MAX_MODEL_ATTEMPTS)

    def test_fund_finding_cannot_use_portfolio_level_metric(self) -> None:
        invalid = valid_model_result()
        invalid.key_findings[0].fund_codes = ["110020"]
        invalid.key_findings[0].metric_keys = ["risk.period_return_pct"]
        structured = FakeStructuredModel([invalid, invalid])

        with patch(
            "app.services.portfolio_research_interpretation.get_chat_model",
            return_value=FakeChatModel(structured),
        ):
            with self.assertRaisesRegex(
                PortfolioResearchInterpretationError,
                "连续两次",
            ):
                self.service.interpret(**self.inputs)

        self.assertEqual(structured.call_count, MAX_MODEL_ATTEMPTS)

    def test_unresolved_number_is_rejected_then_valid_result_is_accepted(self) -> None:
        invalid = valid_model_result()
        invalid.summary = "模型自行声称组合未来会上涨 80%，这不是 Python 引用。"
        structured = FakeStructuredModel([invalid, valid_model_result()])

        with patch(
            "app.services.portfolio_research_interpretation.get_chat_model",
            return_value=FakeChatModel(structured),
        ):
            result = self.service.interpret(**self.inputs)

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(structured.call_count, 2)

    def test_fallback_keeps_report_usable_and_hides_raw_error(self) -> None:
        result = self.service.fallback(
            **self.inputs,
            reason="third-party timeout with private request details",
        )

        self.assertEqual(result["status"], "fallback")
        self.assertFalse(result["model_used"])
        self.assertIn("确定性回退", result["fallback_reason"])
        self.assertNotIn("private request details", result["fallback_reason"])
        self.assertEqual(
            result["key_findings"][2]["metric_references"][0]["value_text"],
            "+4.20%",
        )
        self.assertEqual(result["key_findings"][-1]["title"], "最近决策复盘")

    def test_fallback_explains_empty_decision_history(self) -> None:
        self.inputs["decision_history"] = {
            "decision_count": 0,
            "returned_count": 0,
            "omitted_count": 0,
            "has_decisions": False,
            "decisions": [],
            "note": "当前组合暂无可复盘决策。",
        }

        result = self.service.fallback(
            **self.inputs,
            reason="模型服务暂时不可用",
        )

        self.assertNotIn(
            "最近决策复盘",
            [item["title"] for item in result["key_findings"]],
        )
        self.assertTrue(
            any("暂无历史决策" in item for item in result["uncertainties"])
        )

    def test_focused_fallback_never_resolves_missing_market_metric_or_claims_empty_history(self):
        self.inputs["market_context"] = {"status": "not_requested", "state_code": "not_requested",
            "state_label": "本轮未读取", "summary": "本轮未额外读取市场数据。"}
        self.inputs["decision_history"] = {"decisions": []}
        result = self.service.fallback(**self.inputs, reason="模型超时")
        self.assertEqual(result["status"], "fallback")
        self.assertNotIn("市场环境", [row["title"] for row in result["key_findings"]])
        self.assertNotIn("暂无历史决策", " ".join(result["uncertainties"]))
        self.assertIn("未读取历史决策", " ".join(result["uncertainties"]))


if __name__ == "__main__":
    unittest.main()

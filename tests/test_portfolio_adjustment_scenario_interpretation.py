"""候选场景模型解释的引用边界测试，不访问真实 DeepSeek。"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.services.portfolio_adjustment_scenario_interpretation import (
    MAX_MODEL_ATTEMPTS,
    ModelScenarioExplanation,
    PortfolioAdjustmentScenarioInterpretationError,
    PortfolioAdjustmentScenarioInterpretationService,
)


def comparison() -> dict:
    return {
        "status": "completed",
        "scenarios": [
            {
                "scenario_id": "baseline",
                "label": "不调整基线",
                "comparison_status": "passed",
                "objective_status": "baseline",
                "execution_feasibility": "not_applicable",
            },
            {
                "scenario_id": "model_proposal",
                "label": "完整调整",
                "comparison_status": "passed",
                "objective_status": "met",
                "execution_feasibility": "information_insufficient",
            },
        ],
        "comparison_facts": [
            {
                "metric_key": (
                    "scenario:model_proposal:annualized_volatility_pct"
                ),
                "scenario_id": "model_proposal",
                "metric": "annualized_volatility_pct",
                "label": "年化波动率",
                "value": 10.5,
                "value_text": "10.50%",
                "delta": -2.0,
                "delta_text": "-2.00 个百分点",
                "direction": "supportive",
            }
        ],
        "limitations": ["历史结果不代表未来。"],
    }


def valid_model_result() -> dict:
    return {
        "headline": "候选方案的历史风险有所降低",
        "summary": "同口径比较显示风险指标改善，但执行条件仍需核实。",
        "drivers": [
            {
                "tone": "supportive",
                "title": "历史波动下降",
                "detail": "完整调整在冻结样本上的波动低于不调整基线。",
                "scenario_ids": ["model_proposal"],
                "metric_keys": [
                    "scenario:model_proposal:annualized_volatility_pct"
                ],
            }
        ],
        "tradeoffs": ["风险下降可能伴随收益暴露变化。"],
        "uncertainties": ["真实费用与赎回条件仍未知。"],
        "follow_up_items": ["核实费用后再判断执行可行性。"],
    }


class FakeStructuredModel:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.call_count = 0
        self.model_input: dict = {}

    def invoke(self, messages):
        self.model_input = json.loads(messages[-1].content)
        result = self.responses[min(self.call_count, len(self.responses) - 1)]
        self.call_count += 1
        if isinstance(result, Exception):
            raise result
        return result


class FakeChatModel:
    def __init__(self, structured: FakeStructuredModel) -> None:
        self.structured = structured
        self.schema = None
        self.method = None

    def with_structured_output(self, schema, *, method):
        self.schema = schema
        self.method = method
        return self.structured


class PortfolioAdjustmentScenarioInterpretationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PortfolioAdjustmentScenarioInterpretationService()

    def test_valid_output_resolves_metric_references(self) -> None:
        structured = FakeStructuredModel([valid_model_result()])
        model = FakeChatModel(structured)
        with patch(
            "app.services.portfolio_adjustment_scenario_interpretation.get_chat_model",
            return_value=model,
        ):
            result = self.service.interpret(comparison=comparison())

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(model.schema, ModelScenarioExplanation)
        self.assertEqual(model.method, "json_mode")
        self.assertEqual(
            result["drivers"][0]["references"][0]["delta_text"],
            "-2.00 个百分点",
        )
        self.assertEqual(
            structured.model_input["task"],
            "explain_validated_portfolio_scenarios",
        )
        self.assertNotIn("daily_returns", json.dumps(structured.model_input))

    def test_unknown_metric_reference_is_retried_then_rejected(self) -> None:
        invalid = valid_model_result()
        invalid["drivers"][0]["metric_keys"] = ["scenario:invented:risk"]
        structured = FakeStructuredModel([invalid, invalid])
        with patch(
            "app.services.portfolio_adjustment_scenario_interpretation.get_chat_model",
            return_value=FakeChatModel(structured),
        ):
            with self.assertRaises(
                PortfolioAdjustmentScenarioInterpretationError
            ):
                self.service.interpret(comparison=comparison())

        self.assertEqual(structured.call_count, MAX_MODEL_ATTEMPTS)

    def test_number_in_model_prose_is_rejected_before_valid_retry(self) -> None:
        invalid = valid_model_result()
        invalid["summary"] = "模型自行声称未来风险下降百分之 80。"
        structured = FakeStructuredModel([invalid, valid_model_result()])
        with patch(
            "app.services.portfolio_adjustment_scenario_interpretation.get_chat_model",
            return_value=FakeChatModel(structured),
        ):
            result = self.service.interpret(comparison=comparison())

        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(structured.call_count, 2)

    def test_information_insufficient_skips_model(self) -> None:
        unavailable = comparison()
        unavailable["status"] = "information_insufficient"
        unavailable["comparison_facts"] = []
        with patch(
            "app.services.portfolio_adjustment_scenario_interpretation.get_chat_model"
        ) as factory:
            result = self.service.interpret(comparison=unavailable)

        self.assertEqual(result["status"], "fallback")
        self.assertFalse(result["model_used"])
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()

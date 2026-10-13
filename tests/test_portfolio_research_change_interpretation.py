"""研究变化解释器的引用边界测试，不访问真实 DeepSeek。"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.services.portfolio_research_change_interpretation import (
    MAX_MODEL_ATTEMPTS,
    ModelResearchChangeExplanation,
    PortfolioResearchChangeInterpretationError,
    PortfolioResearchChangeInterpretationService,
)


def change_set() -> dict:
    return {
        "change_scope": "limited",
        "input_changed": True,
        "conclusion_changed": True,
        "changed_sections": [
            {"section": "quant_risk", "label": "量化风险", "changed": True}
        ],
        "facts": [
            {
                "fact_key": "metric:quant_risk.annualized_volatility_pct",
                "section": "quant_risk",
                "section_label": "量化风险",
                "kind": "metric",
                "label": "风险年化波动率",
                "before_value": 8.5,
                "after_value": 11.2,
                "before_text": "8.50%",
                "after_text": "11.20%",
                "delta": 2.7,
                "delta_text": "+2.70 个百分点",
                "tone": "neutral",
            }
        ],
        "limitations": ["变化范围不等于投资风险等级。"],
    }


def valid_model_result() -> dict:
    return {
        "headline": "量化风险输入发生变化",
        "summary": "本次报告需要结合新的波动信息理解。",
        "drivers": [
            {
                "tone": "caution",
                "title": "历史波动有所变化",
                "detail": "该变化会影响组合风险结构的理解。",
                "fact_keys": [
                    "metric:quant_risk.annualized_volatility_pct"
                ],
            }
        ],
        "uncertainties": ["历史数据不能代表未来结果。"],
        "follow_up_items": ["等待后续数据后继续观察。"],
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


class PortfolioResearchChangeInterpretationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PortfolioResearchChangeInterpretationService()

    def test_valid_output_resolves_fact_references(self) -> None:
        structured = FakeStructuredModel([valid_model_result()])
        model = FakeChatModel(structured)
        with patch(
            "app.services.portfolio_research_change_interpretation.get_chat_model",
            return_value=model,
        ) as factory:
            result = self.service.interpret(change_set=change_set())

        self.assertEqual(result["status"], "accepted")
        self.assertTrue(result["model_used"])
        self.assertEqual(model.schema, ModelResearchChangeExplanation)
        self.assertEqual(model.method, "json_mode")
        self.assertEqual(
            result["drivers"][0]["references"][0]["after_text"],
            "11.20%",
        )
        self.assertEqual(structured.model_input["task"], "explain_portfolio_research_changes")
        self.assertNotIn("base_snapshot", structured.model_input)
        self.assertNotIn("before_value", structured.model_input["facts"][0])
        factory.assert_called_once_with(
            max_retries=0,
            temperature=0,
            thinking=False,
        )

    def test_unknown_fact_reference_is_retried_then_rejected(self) -> None:
        invalid = valid_model_result()
        invalid["drivers"][0]["fact_keys"] = ["invented:fact"]
        structured = FakeStructuredModel([invalid, invalid])
        with patch(
            "app.services.portfolio_research_change_interpretation.get_chat_model",
            return_value=FakeChatModel(structured),
        ):
            with self.assertRaises(PortfolioResearchChangeInterpretationError):
                self.service.interpret(change_set=change_set())

        self.assertEqual(structured.call_count, MAX_MODEL_ATTEMPTS)

    def test_untrusted_number_is_rejected_before_valid_retry(self) -> None:
        invalid = valid_model_result()
        invalid["summary"] = "模型自行声称风险上升了 80%。"
        structured = FakeStructuredModel([invalid, valid_model_result()])
        with patch(
            "app.services.portfolio_research_change_interpretation.get_chat_model",
            return_value=FakeChatModel(structured),
        ):
            result = self.service.interpret(change_set=change_set())

        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(structured.call_count, 2)

    def test_no_facts_uses_fallback_without_calling_model(self) -> None:
        empty = {
            "change_scope": "none",
            "input_changed": False,
            "conclusion_changed": False,
            "changed_sections": [],
            "facts": [],
            "limitations": [],
        }
        with patch(
            "app.services.portfolio_research_change_interpretation.get_chat_model"
        ) as factory:
            result = self.service.interpret(change_set=empty)

        self.assertEqual(result["status"], "fallback")
        self.assertFalse(result["model_used"])
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()

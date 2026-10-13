"""受约束模型语义研判的回归测试。

全部测试都用 Fake/Mock 模型，不访问 DeepSeek。重点不是验证模型是否“聪明”，而是
验证 Harness 边界：只发送允许的数据、保留规则基线、拒绝未知事件、低置信度弃权，
以及 REST 和 Agent Tool 都能获得相同的分层结果。
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.agent_tools import get_agent_tools
from app.services.fund_agent_tools import get_fund_semantic_review_tool
from app.services.fund_semantic_review import (
    FundSemanticReviewService,
    ModelEventJudgment,
    ModelSemanticReview,
    fund_semantic_review_service,
)
from app.services.fund_assessment import EvidenceFusionService
from app.services.market_events import MarketEventService
from app.services.tool_permissions import check_tool_permission


def _model_review(
    event_ids: list[str],
    *,
    confidence: float = 0.82,
    direction: str = "neutral",
) -> ModelSemanticReview:
    """快速生成与输入事件一一对应的合法模型结构。"""

    return ModelSemanticReview(
        overall_view="mixed",
        overall_confidence=confidence,
        summary="事件同时包含支持与风险语义，需要结合历史指标理解。",
        event_judgments=[
            ModelEventJudgment(
                event_id=event_id,
                direction=direction,
                relevance="related",
                horizon="short_term",
                confidence=confidence,
                reason="事件与目标基金主题相关，但不能据此预测净值方向。",
            )
            for event_id in event_ids
        ],
        conflicts=[],
        uncertainties=["缺少实时持仓穿透。"],
    )


class FakeStructuredModel:
    """读取真正发给模型的 JSON，再根据事件 ID 返回结构化结果。"""

    def __init__(self) -> None:
        self.model_input: dict = {}

    async def ainvoke(self, messages):
        self.model_input = json.loads(messages[-1].content)
        event_ids = [event["event_id"] for event in self.model_input["events"]]
        return _model_review(event_ids)


class FakeChatModel:
    """模拟 ChatOpenAI.with_structured_output，不访问网络。"""

    def __init__(self, structured_model: FakeStructuredModel) -> None:
        self.structured_model = structured_model
        self.schema = None
        self.method = None

    def with_structured_output(self, schema, *, method):
        self.schema = schema
        self.method = method
        return self.structured_model


class FundSemanticReviewServiceTests(unittest.IsolatedAsyncioTestCase):
    """直接验证模型调用前后的数据边界。"""

    @classmethod
    def setUpClass(cls) -> None:
        # 语义校验测试需要稳定 event_id；产品 API 测试仍继续使用全局数据库 Service。
        events = MarketEventService()
        assessments = EvidenceFusionService(event_service=events)
        cls.service = FundSemanticReviewService(
            assessments=assessments,
            events=events,
        )

    async def test_review_keeps_baseline_and_hides_manual_event_labels(self) -> None:
        structured_model = FakeStructuredModel()
        fake_model = FakeChatModel(structured_model)

        with patch(
            "app.services.fund_semantic_review.get_chat_model",
            return_value=fake_model,
        ) as model_factory:
            result = await self.service.review(
                fund_code="161725",
                months=12,
            )

        baseline = result["baseline_assessment"]
        interpretation = result["model_interpretation"]
        self.assertEqual(result["data_kind"], "hybrid_fund_assessment")
        self.assertEqual(baseline["data_kind"], "deterministic_fund_assessment")
        self.assertEqual(baseline["state_code"], "high_attention")
        self.assertFalse(interpretation["is_predictive"])
        self.assertEqual(interpretation["calibration_status"], "uncalibrated")
        self.assertEqual(
            interpretation["reviewed_event_count"],
            baseline["event_count"],
        )
        self.assertEqual(fake_model.schema, ModelSemanticReview)
        self.assertEqual(fake_model.method, "json_mode")
        model_factory.assert_called_once_with(
            max_retries=1,
            temperature=0,
            thinking=False,
        )

        # 模型能看标题和摘要，但看不到 fixture 中原来的人工作答字段。
        serialized_input = json.dumps(structured_model.model_input, ensure_ascii=False)
        self.assertNotIn("impact_direction", serialized_input)
        self.assertNotIn("impact_reason", serialized_input)
        self.assertIn("rule_baseline", structured_model.model_input)
        self.assertIn("market_observations", structured_model.model_input)
        self.assertIn("missing_information", structured_model.model_input)

    async def test_low_confidence_unknown_and_missing_events_are_abstained(self) -> None:
        # 只返回一条低置信度真实事件，再夹带一条不存在的 ID；其余输入事件故意漏答。
        low_confidence_result = ModelSemanticReview(
            overall_view="caution",
            overall_confidence=0.42,
            summary="证据不足，无法形成可靠方向。",
            event_judgments=[
                ModelEventJudgment(
                    event_id="evt-20260820-mofcom-consumption",
                    direction="caution",
                    relevance="related",
                    horizon="short_term",
                    confidence=0.42,
                    reason="方向存在较大不确定性。",
                ),
                ModelEventJudgment(
                    event_id="invented-event-id",
                    direction="supportive",
                    relevance="direct",
                    horizon="long_term",
                    confidence=0.99,
                    reason="这条事件并不存在。",
                ),
            ],
            conflicts=[],
            uncertainties=[],
        )

        with patch.object(
            self.service,
            "_invoke_model",
            new=AsyncMock(return_value=low_confidence_result),
        ):
            result = await self.service.review(
                fund_code="161725",
                months=12,
            )

        interpretation = result["model_interpretation"]
        self.assertEqual(interpretation["status"], "abstained")
        self.assertEqual(interpretation["accepted_count"], 0)
        self.assertEqual(interpretation["effective_overall_view"], "insufficient")
        self.assertTrue(
            all(
                item["effective_direction"] == "uncertain"
                for item in interpretation["event_judgments"]
            )
        )
        self.assertTrue(
            any("invented-event-id" in item for item in interpretation["uncertainties"])
        )

    async def test_conditional_prediction_is_allowed_for_explicit_question(self) -> None:
        predictive_result = ModelSemanticReview(
            overall_view="caution",
            overall_confidence=0.9,
            summary="事件语义偏谨慎，但仍需结合历史指标。",
            event_judgments=[
                ModelEventJudgment(
                    event_id="evt-20260820-mofcom-consumption",
                    direction="caution",
                    relevance="related",
                    horizon="short_term",
                    confidence=0.95,
                    reason="该事件可能引发价格回调，因此建议关注。",
                )
            ],
            conflicts=[],
            uncertainties=[],
        )

        with patch.object(
            self.service,
            "_invoke_model",
            new=AsyncMock(return_value=predictive_result),
        ):
            result = await self.service.review(
                fund_code="161725",
                months=12,
                question="结合当前数据，这只基金短期会回调吗？",
            )

        judgments = result["model_interpretation"]["event_judgments"]
        target = next(
            item
            for item in judgments
            if item["event_id"] == "evt-20260820-mofcom-consumption"
        )
        self.assertEqual(target["review_status"], "accepted")
        self.assertEqual(target["effective_direction"], "caution")
        self.assertIn("价格回调", target["reason"])
        self.assertEqual(
            result["model_interpretation"]["user_question"],
            "结合当前数据，这只基金短期会回调吗？",
        )


class FundSemanticReviewIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """验证 REST 和 Agent Tool 两个正式入口。"""

    async def test_api_returns_hybrid_contract_without_real_model_call(self) -> None:
        baseline = fund_semantic_review_service.assessments.assess(
            fund_code="110020",
            months=6,
        )
        event_ids = [
            item["event_id"]
            for group in (
                baseline["risk_evidence"],
                baseline["supporting_evidence"],
                baseline["neutral_evidence"],
            )
            for item in group
            if item["kind"] == "market_event"
        ]

        with patch.object(
            fund_semantic_review_service,
            "_invoke_model",
            new=AsyncMock(return_value=_model_review(event_ids)),
        ):
            # TestClient 是同步客户端，放到线程里避免嵌套当前测试事件循环。
            import asyncio

            response = await asyncio.to_thread(
                TestClient(app).post,
                "/api/v1/funds/110020/semantic-review?months=6",
                json={"question": "结合全部证据，当前风险是否值得关注？"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["data_kind"], "hybrid_fund_assessment")
        self.assertIn("historical_facts", payload["layer_contract"])
        self.assertFalse(payload["model_interpretation"]["is_predictive"])
        self.assertEqual(
            payload["model_interpretation"]["user_question"],
            "结合全部证据，当前风险是否值得关注？",
        )

    async def test_agent_tool_is_registered_allowed_and_returns_bounded_result(self) -> None:
        baseline = fund_semantic_review_service.assessments.assess(
            fund_code="110007",
            months=12,
        )
        event_ids = [
            item["event_id"]
            for group in (
                baseline["risk_evidence"],
                baseline["supporting_evidence"],
                baseline["neutral_evidence"],
            )
            for item in group
            if item["kind"] == "market_event"
        ]

        with patch.object(
            fund_semantic_review_service,
            "_invoke_model",
            new=AsyncMock(return_value=_model_review(event_ids)),
        ):
            raw_result = await get_fund_semantic_review_tool.ainvoke(
                {
                    "fund_code": "110007",
                    "months": 12,
                    "question": "结合当前数据，这只基金适合继续持有吗？",
                }
            )

        payload = json.loads(raw_result)
        tool_names = {tool.name for tool in get_agent_tools()}
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tool_result_scope"], "bounded_semantic_review")
        self.assertIn("rule_baseline", payload)
        self.assertIn("model_interpretation", payload)
        self.assertEqual(
            payload["model_interpretation"]["user_question"],
            "结合当前数据，这只基金适合继续持有吗？",
        )
        self.assertIn("get_fund_semantic_review_tool", tool_names)
        self.assertTrue(
            check_tool_permission("get_fund_semantic_review_tool", {}).allowed
        )


if __name__ == "__main__":
    unittest.main()

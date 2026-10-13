"""单基金证据融合服务的确定性测试。

这里不调用大模型。测试只验证风险、市场与事件如何变成统一 Evidence，以及公开
权重是否被正确累计。这样将来替换实时数据源时，综合研判规则仍然可以独立回归。
"""

import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.services.fund_assessment import EvidenceFusionService
from app.services.market_events import MarketEventService


# 规则算法测试固定使用离线事件，避免开发机数据库的每日同步结果改变精确断言。
fixture_evidence_fusion_service = EvidenceFusionService(
    event_service=MarketEventService(),
)


class FundAssessmentServiceTests(unittest.TestCase):
    """直接测试业务 Service，定位失败时不需要经过 HTTP。"""

    def test_industry_fund_combines_three_evidence_dimensions(self) -> None:
        result = fixture_evidence_fusion_service.assess(
            fund_code="161725",
            months=12,
        )

        self.assertEqual(result["state_code"], "high_attention")
        self.assertEqual(result["available_dimensions"], [
            "fund_risk",
            "market_state",
            "market_event",
        ])
        self.assertEqual(result["coverage_pct"], 100.0)
        self.assertEqual(result["event_count"], 6)
        self.assertGreater(
            result["score_breakdown"]["caution_points"],
            result["score_breakdown"]["supportive_points"],
        )

        all_evidence = [
            *result["risk_evidence"],
            *result["supporting_evidence"],
            *result["neutral_evidence"],
        ]
        self.assertEqual(len(all_evidence), result["evidence_count"])
        self.assertEqual(
            len({item["evidence_id"] for item in all_evidence}),
            len(all_evidence),
        )
        self.assertTrue(
            any(item["kind"] == "market_event" for item in all_evidence)
        )

    def test_score_is_reproducible_from_public_weights(self) -> None:
        result = fixture_evidence_fusion_service.assess(
            fund_code="110020",
            months=12,
        )
        score = result["score_breakdown"]
        all_evidence = [
            *result["risk_evidence"],
            *result["supporting_evidence"],
            *result["neutral_evidence"],
        ]

        def points(direction: str) -> float:
            return round(
                sum(
                    item["strength"]
                    * score["reliability_weights"][item["reliability"]]
                    * score["relevance_weights"][item["relevance"]]
                    * item.get("decay_factor", 1.0)
                    for item in all_evidence
                    if item["direction"] == direction
                ),
                2,
            )

        self.assertEqual(score["caution_points"], points("caution"))
        self.assertEqual(score["supportive_points"], points("supportive"))

    def test_expired_events_remain_traceable_but_do_not_score(self) -> None:
        result = fixture_evidence_fusion_service.assess(
            fund_code="161725",
            months=12,
        )
        all_evidence = [
            *result["risk_evidence"],
            *result["supporting_evidence"],
            *result["neutral_evidence"],
        ]
        expired_events = [
            item
            for item in all_evidence
            if item["kind"] == "market_event"
            and item["freshness_status"] == "expired"
        ]

        self.assertTrue(expired_events)
        self.assertTrue(all(item["decay_factor"] == 0 for item in expired_events))
        self.assertEqual(result["expired_event_count"], len(expired_events))
        self.assertEqual(
            result["active_event_count"] + result["expired_event_count"],
            result["event_count"],
        )

    def test_bond_fund_keeps_equity_market_as_unscored_context(self) -> None:
        result = fixture_evidence_fusion_service.assess(
            fund_code="110007",
            months=12,
        )
        scored_evidence = [
            *result["risk_evidence"],
            *result["supporting_evidence"],
        ]
        market_context = [
            item
            for item in result["neutral_evidence"]
            if item["kind"] == "market_state"
        ]

        self.assertFalse(
            any(item["kind"] == "market_state" for item in scored_evidence)
        )
        self.assertEqual(len(market_context), 1)
        self.assertEqual(market_context[0]["relevance"], "context")
        self.assertEqual(
            result["score_breakdown"]["relevance_weights"]["context"],
            0.0,
        )

    def test_shorter_window_filters_older_events(self) -> None:
        six_months = fixture_evidence_fusion_service.assess(
            fund_code="161725",
            months=6,
        )
        one_year = fixture_evidence_fusion_service.assess(
            fund_code="161725",
            months=12,
        )

        self.assertLess(six_months["event_count"], one_year["event_count"])
        self.assertEqual(six_months["period_months"], 6)


class FundAssessmentApiTests(unittest.TestCase):
    """验证统一结果能通过正式 REST 契约提供给前端。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_assessment_endpoint_returns_traceable_evidence(self) -> None:
        response = self.client.get(
            "/api/v1/funds/161725/assessment?months=12"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["data_kind"], "deterministic_fund_assessment")
        self.assertEqual(
            payload["assessment_date"],
            payload["component_dates"]["fund_risk"],
        )
        self.assertGreaterEqual(
            payload["component_dates"]["market_state"],
            "2026-08-25",
        )
        self.assertTrue(payload["risk_evidence"][0]["evidence_id"])
        self.assertIn("不构成投资建议", payload["disclaimer"])

    def test_assessment_endpoint_preserves_not_found_semantics(self) -> None:
        response = self.client.get(
            "/api/v1/funds/999999/assessment?months=12"
        )

        self.assertEqual(response.status_code, 404)
        self.assertIn("基金 999999 尚未导入当前工作台", response.json()["detail"])

    def test_assessment_endpoint_rejects_unsupported_period(self) -> None:
        response = self.client.get(
            "/api/v1/funds/161725/assessment?months=7"
        )

        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()

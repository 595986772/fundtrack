"""市场事件有效期与线性时间衰减的确定性测试。"""

from datetime import date
import unittest

from app.services.market_event_freshness import MarketEventFreshnessService


class MarketEventFreshnessServiceTests(unittest.TestCase):
    """使用固定日期验证三种影响周期，不依赖真实系统时间。"""

    def setUp(self) -> None:
        self.service = MarketEventFreshnessService()

    def test_short_term_event_moves_from_fresh_to_expired(self) -> None:
        event_date = date(2026, 8, 1)
        fresh = self.service.evaluate(
            event_date=event_date,
            horizon="short_term",
            as_of=date(2026, 8, 4),
        )
        decaying = self.service.evaluate(
            event_date=event_date,
            horizon="short_term",
            as_of=date(2026, 8, 8),
        )
        expired = self.service.evaluate(
            event_date=event_date,
            horizon="short_term",
            as_of=date(2026, 8, 15),
        )

        self.assertEqual(fresh["freshness_status"], "fresh")
        self.assertEqual(fresh["decay_factor"], 1.0)
        self.assertEqual(decaying["freshness_status"], "decaying")
        self.assertAlmostEqual(decaying["decay_factor"], 7 / 11, places=4)
        self.assertEqual(expired["freshness_status"], "expired")
        self.assertEqual(expired["decay_factor"], 0.0)
        self.assertEqual(expired["valid_until"], "2026-08-15")

    def test_each_horizon_uses_its_public_validity_window(self) -> None:
        expected = {
            "short_term": 14,
            "medium_term": 60,
            "long_term": 180,
        }
        for horizon, valid_days in expected.items():
            with self.subTest(horizon=horizon):
                result = self.service.evaluate(
                    event_date="2026-01-01",
                    horizon=horizon,
                    as_of=date(2026, 1, 1),
                )
                self.assertEqual(result["valid_days"], valid_days)
                self.assertEqual(result["freshness_score"], 100.0)

    def test_future_effective_event_never_exceeds_full_weight(self) -> None:
        result = self.service.evaluate(
            event_date="2026-09-01",
            horizon="medium_term",
            as_of=date(2026, 8, 27),
        )

        self.assertEqual(result["age_days"], 0)
        self.assertEqual(result["decay_factor"], 1.0)


if __name__ == "__main__":
    unittest.main()

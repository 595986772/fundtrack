"""FundRiskService 数学口径的确定性单元测试。"""

from __future__ import annotations

import unittest

from app.services.fund_risk import FundRiskError, FundRiskService


class FakeFundCatalog:
    """只返回人工净值，避免算法测试依赖真实基金快照。"""

    def __init__(self, values: list[float]) -> None:
        self.values = values

    def get_fund(self, fund_code: str):
        points = [
            {
                "nav_date": f"2026-01-{index:02d}",
                "unit_nav": value,
                "daily_growth_rate_pct": None,
            }
            for index, value in enumerate(self.values, start=1)
        ]
        return {
            "profile": {"short_name": "人工测试基金"},
            "nav_points": points,
            "source": "unit-test",
            "snapshot_end": points[-1]["nav_date"],
        }


class FundRiskServiceTests(unittest.TestCase):
    def test_calculates_drawdown_recovery_and_worst_day(self) -> None:
        """120 跌到 90 是 25% 回撤，并在 122 时恢复前高。"""

        service = FundRiskService(FakeFundCatalog([100, 120, 90, 96, 122]))
        result = service.analyze(fund_code="000001", months=6)

        self.assertEqual(result["period_return_pct"], 22.0)
        self.assertEqual(result["max_drawdown"]["drawdown_pct"], 25.0)
        self.assertEqual(result["max_drawdown"]["peak_date"], "2026-01-02")
        self.assertEqual(result["max_drawdown"]["trough_date"], "2026-01-03")
        self.assertEqual(result["max_drawdown"]["recovery_date"], "2026-01-05")
        self.assertEqual(result["max_drawdown"]["recovery_days"], 2)
        self.assertEqual(result["worst_daily_return_pct"], -25.0)
        self.assertEqual(result["worst_daily_return_date"], "2026-01-03")
        self.assertEqual(result["longest_drawdown_days"], 3)
        self.assertEqual(result["current_drawdown_pct"], 0.0)
        self.assertGreater(result["annualized_volatility_pct"], 0)
        self.assertGreater(result["downside_volatility_pct"], 0)

    def test_marks_drawdown_as_unrecovered_at_period_end(self) -> None:
        """最后一天仍低于 110 高点时，恢复日期为空且当前回撤为 10%。"""

        service = FundRiskService(FakeFundCatalog([100, 110, 99]))
        result = service.analyze(fund_code="000001", months=12)

        self.assertEqual(result["max_drawdown"]["drawdown_pct"], 10.0)
        self.assertIsNone(result["max_drawdown"]["recovery_date"])
        self.assertIsNone(result["max_drawdown"]["recovery_days"])
        self.assertEqual(result["current_drawdown_pct"], 10.0)
        self.assertEqual(result["current_peak_date"], "2026-01-02")
        self.assertEqual(result["longest_drawdown_days"], 1)

    def test_rejects_period_outside_supported_options(self) -> None:
        service = FundRiskService(FakeFundCatalog([100, 101, 102]))

        with self.assertRaises(FundRiskError):
            service.analyze(fund_code="000001", months=7)

    def test_dividend_adjusted_series_has_no_false_drawdown(self) -> None:
        """除息使单位净值跌 10% 时，总回报风险仍应为 0%。"""

        class DividendCatalog:
            def get_fund(self, fund_code: str):
                return {
                    "profile": {"short_name": "分红测试基金"},
                    "nav_points": [
                        {"nav_date": "2026-01-01", "unit_nav": 2.0},
                        {"nav_date": "2026-01-02", "unit_nav": 1.8},
                        {"nav_date": "2026-01-03", "unit_nav": 1.8},
                    ],
                    "corporate_actions": [
                        {
                            "action_date": "2026-01-02",
                            "action_type": "cash_dividend",
                            "cash_dividend_per_unit": 0.2,
                            "split_ratio": None,
                        }
                    ],
                    "corporate_action_coverage": {
                        "intervals": [
                            {
                                "coverage_start": "2026-01-01",
                                "coverage_end": "2026-01-03",
                            }
                        ]
                    },
                    "source": "unit-test",
                    "snapshot_end": "2026-01-03",
                }

        result = FundRiskService(DividendCatalog()).analyze(
            fund_code="000001",
            months=6,
        )

        self.assertEqual(result["return_basis"], "total_return_reinvested")
        self.assertEqual(result["period_return_pct"], 0.0)
        self.assertEqual(result["max_drawdown"]["drawdown_pct"], 0.0)
        self.assertEqual(result["current_drawdown_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()

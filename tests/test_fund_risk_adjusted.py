"""年化收益、夏普和 Sortino 公式的确定性测试。"""

from __future__ import annotations

import unittest

from app.services.fund_risk_adjusted import (
    FundRiskAdjustedReturnService,
)


class FundRiskAdjustedReturnServiceTests(unittest.TestCase):
    def test_calculates_geometric_annual_return_sharpe_and_sortino(self) -> None:
        """一个上涨、一个下跌和一个小涨日应同时得到两个有限比率。"""

        service = FundRiskAdjustedReturnService(
            risk_free_rate_annual_pct=0.0
        )
        result = service.analyze(daily_returns=[0.01, -0.005, 0.002])

        self.assertAlmostEqual(result["annualized_return_pct"], 79.07, places=2)
        self.assertAlmostEqual(result["sharpe_ratio"], 4.935, places=3)
        self.assertAlmostEqual(result["sortino_ratio"], 12.831, places=3)
        self.assertEqual(result["risk_free_rate_annual_pct"], 0.0)
        self.assertEqual(result["warnings"], [])

    def test_nonzero_risk_free_rate_reduces_both_ratios(self) -> None:
        daily_returns = [0.004, -0.002, 0.003, -0.001]
        zero_rate = FundRiskAdjustedReturnService(
            risk_free_rate_annual_pct=0.0
        ).analyze(daily_returns=daily_returns)
        higher_rate = FundRiskAdjustedReturnService(
            risk_free_rate_annual_pct=3.0
        ).analyze(daily_returns=daily_returns)

        self.assertLess(higher_rate["sharpe_ratio"], zero_rate["sharpe_ratio"])
        self.assertLess(higher_rate["sortino_ratio"], zero_rate["sortino_ratio"])

    def test_flat_series_returns_null_ratios_instead_of_infinity(self) -> None:
        result = FundRiskAdjustedReturnService(
            risk_free_rate_annual_pct=0.0
        ).analyze(daily_returns=[0.0, 0.0, 0.0])

        self.assertEqual(result["annualized_return_pct"], 0.0)
        self.assertIsNone(result["sharpe_ratio"])
        self.assertIsNone(result["sortino_ratio"])
        self.assertEqual(len(result["warnings"]), 2)


if __name__ == "__main__":
    unittest.main()

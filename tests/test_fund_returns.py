"""基金总回报口径的确定性测试。

这些人工序列专门覆盖单位净值最容易误导风险指标的两种情况：现金分红和份额拆分。
测试不访问数据库、网络或模型。
"""

from __future__ import annotations

import unittest

from app.services.fund_corporate_actions import (
    AkshareFundCorporateActionProvider,
)
from app.services.fund_returns import FundReturnSeriesService


def detail_for(
    *,
    values: list[float],
    actions: list[dict] | None = None,
    growth_rates: list[float | None] | None = None,
    covered: bool = True,
) -> dict:
    """构造三天人工净值，并按需声明公司行为覆盖是否完整。"""

    dates = ["2026-01-01", "2026-01-02", "2026-01-03"]
    growth_rates = growth_rates or [None] * len(values)
    coverage = (
        {
            "intervals": [
                {
                    "coverage_start": dates[0],
                    "coverage_end": dates[-1],
                }
            ]
        }
        if covered
        else {"intervals": []}
    )
    return {
        "nav_points": [
            {
                "nav_date": nav_date,
                "unit_nav": value,
                "daily_growth_rate_pct": growth,
            }
            for nav_date, value, growth in zip(dates, values, growth_rates)
        ],
        "corporate_actions": actions or [],
        "corporate_action_coverage": coverage,
    }


class FundReturnSeriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FundReturnSeriesService()

    def test_cash_dividend_does_not_create_false_loss(self) -> None:
        """2.0 除息到 1.8，同时分红 0.2，投资者经济价值没有下降。"""

        detail = detail_for(
            values=[2.0, 1.8, 1.8],
            actions=[
                {
                    "action_date": "2026-01-02",
                    "action_type": "cash_dividend",
                    "cash_dividend_per_unit": 0.2,
                    "split_ratio": None,
                }
            ],
        )

        result = self.service.build(detail=detail)

        self.assertEqual(result.return_basis, "total_return_reinvested")
        self.assertEqual(result.return_basis_status, "verified")
        self.assertEqual(result.corporate_action_count, 1)
        self.assertAlmostEqual(result.daily_returns[0], 0.0)
        self.assertAlmostEqual(result.period_return_pct, 0.0)

    def test_share_split_does_not_create_false_loss(self) -> None:
        """每份旧份额变成 2 份时，2.0 降到 1.0 也不是 50% 亏损。"""

        detail = detail_for(
            values=[2.0, 1.0, 1.0],
            actions=[
                {
                    "action_date": "2026-01-02",
                    "action_type": "share_split",
                    "cash_dividend_per_unit": None,
                    "split_ratio": 2.0,
                }
            ],
        )

        result = self.service.build(detail=detail)

        self.assertAlmostEqual(result.daily_returns[0], 0.0)
        self.assertAlmostEqual(result.period_return_pct, 0.0)

    def test_provider_daily_return_is_used_when_action_coverage_is_missing(self) -> None:
        detail = detail_for(
            values=[2.0, 1.8, 1.8],
            growth_rates=[None, 0.0, 0.0],
            covered=False,
        )

        result = self.service.build(detail=detail)

        self.assertEqual(result.return_basis, "provider_reported_daily_return")
        self.assertEqual(result.return_basis_status, "provider_reported")
        self.assertAlmostEqual(result.period_return_pct, 0.0)
        self.assertTrue(result.data_quality_warnings)

    def test_unadjusted_fallback_is_explicitly_marked_limited(self) -> None:
        detail = detail_for(
            values=[2.0, 1.8, 1.8],
            covered=False,
        )

        result = self.service.build(detail=detail)

        self.assertEqual(result.return_basis, "unit_nav_unadjusted")
        self.assertEqual(result.return_basis_status, "limited")
        self.assertAlmostEqual(result.period_return_pct, -10.0)
        self.assertIn("未复权", result.data_quality_warnings[0])


class CorporateActionParserTests(unittest.TestCase):
    """验证 AKShare 当前两种中文分红格式和拆分比例格式。"""

    def test_parses_per_ten_and_per_one_dividend(self) -> None:
        parser = AkshareFundCorporateActionProvider()

        self.assertAlmostEqual(
            parser._parse_dividend_per_unit(
                "每10份派现金0.3900元",
                column_name="每10份分红",
                fund_code="110007",
            ),
            0.039,
        )
        self.assertAlmostEqual(
            parser._parse_dividend_per_unit(
                "每份派现金0.0050元",
                column_name="每份分红",
                fund_code="161606",
            ),
            0.005,
        )

    def test_parses_split_ratio_as_new_units_per_old_unit(self) -> None:
        parser = AkshareFundCorporateActionProvider()

        self.assertAlmostEqual(
            parser._parse_split_ratio("1:1.4982", fund_code="161725"),
            1.4982,
        )


if __name__ == "__main__":
    unittest.main()

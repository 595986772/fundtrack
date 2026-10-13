"""组合 TWR、基准和收益归因的确定性测试。"""

from __future__ import annotations

from datetime import date
import unittest

from app.services.fund_returns import FundReturnSeries
from app.services.fund_risk_adjusted import FundRiskAdjustedReturnService
from app.services.portfolio_performance import PortfolioPerformanceService


class FakePortfolioReader:
    def __init__(self, portfolio, transactions=None):
        self.portfolio = portfolio
        self.transactions = transactions or []

    def get(self, *, user_id, portfolio_id):
        assert user_id == "chen"
        assert portfolio_id == self.portfolio["id"]
        return self.portfolio

    def list_transactions(self, *, user_id, portfolio_id):
        self.get(user_id=user_id, portfolio_id=portfolio_id)
        return list(self.transactions)


class FakeFundReader:
    def __init__(self, returns_by_fund):
        self.returns_by_fund = returns_by_fund

    def get_fund(self, fund_code):
        return {
            "profile": {"fund_code": fund_code, "short_name": f"基金{fund_code}"},
            "nav_points": [{"fund_code": fund_code}],
            "test_returns": self.returns_by_fund[fund_code],
        }


class FakeReturnSeriesService:
    def build(self, *, detail, points):
        rows = detail["test_returns"]
        index = 1.0
        index_points = [{"nav_date": rows[0][0].isoformat(), "index_value": index}]
        daily_returns = []
        for point_date, daily_return in rows[1:]:
            daily_returns.append(daily_return)
            index *= 1 + daily_return
            index_points.append(
                {"nav_date": point_date.isoformat(), "index_value": index}
            )
        return FundReturnSeries(
            index_points=index_points,
            daily_returns=daily_returns,
            return_basis="total_return_reinvested",
            return_basis_label="分红再投资总回报",
            return_basis_status="verified",
            corporate_action_count=0,
            corporate_action_source=None,
            corporate_action_coverage_start=None,
            corporate_action_coverage_end=None,
            data_quality_warnings=[],
        )


class FakeBenchmarkReader:
    def __init__(self, returns_by_fund):
        self.returns_by_fund = returns_by_fund

    def get_context(self, *, fund_code):
        return {
            "fund_code": fund_code,
            "benchmark_name": f"基准{fund_code}",
            "benchmark_status": "matched",
            "source": "test benchmark",
            "source_urls": ["https://example.com/benchmark"],
            "warnings": [],
            "points": [
                {
                    "benchmark_date": point_date.isoformat(),
                    "daily_return": daily_return,
                }
                for point_date, daily_return in self.returns_by_fund[fund_code]
            ],
        }


class PortfolioPerformanceTests(unittest.TestCase):
    def test_nav_day_is_not_confirmation_day_for_buy_and_sell_returns(self):
        """周五收盘买入、周一收盘卖出：只赚周一的涨跌，不能多算周五或周二。"""
        days = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 7)]
        portfolio = {"id": "portfolio_1234567890abcdef", "name": "净值日边界", "position_mode": "actual", "holdings": []}
        transactions = [
            {"id": "buy", "fund_code": "110020", "transaction_type": "buy", "trade_date": "2025-01-03",
             "units": 100, "unit_price": 1.5, "fee": 0, "timing": {"confirmation_date": "2025-01-06"}},
            {"id": "sell", "fund_code": "110020", "transaction_type": "sell", "trade_date": "2025-01-06",
             "units": 100, "unit_price": 1.65, "fee": 0, "timing": {"confirmation_date": "2025-01-07"}},
        ]
        returns = list(zip(days, [0, 0.5, 0.1, 0.1]))
        result = self.service(portfolio, {"110020": returns}, {"110020": returns[1:]}, transactions).analyze(
            user_id="chen", portfolio_id=portfolio["id"], months=12)
        self.assertAlmostEqual(result["twr_pct"], 10)
        self.assertAlmostEqual(result["attribution"][0]["pnl_contribution_amount"], 15)
        self.assertAlmostEqual(result["cash_flows"]["withdrawals"], 165)
        self.assertEqual(result["benchmark"]["coverage_pct"], 100)
        self.assertEqual(result["benchmark"]["status"], "insufficient")
        self.assertIn("少于 2 日", " ".join(result["warnings"]))
        self.assertNotIn("低于 80%", " ".join(result["warnings"]))

    def service(self, portfolio, fund_returns, benchmark_returns, transactions=None):
        return PortfolioPerformanceService(
            portfolios=FakePortfolioReader(portfolio, transactions),
            funds=FakeFundReader(fund_returns),
            return_series=FakeReturnSeriesService(),
            risk_adjusted=FundRiskAdjustedReturnService(
                risk_free_rate_annual_pct=0,
            ),
            benchmarks=FakeBenchmarkReader(benchmark_returns),
        )

    def test_actual_performance_removes_cash_flows_and_links_attribution(self):
        start = date(2025, 9, 1)
        gain_date = date(2026, 8, 30)
        end = date(2026, 8, 31)
        portfolio = {
            "id": "portfolio_1234567890abcdef",
            "name": "真实组合",
            "position_mode": "actual",
            "holdings": [
                {
                    "fund_code": "110020",
                    "short_name": "基金110020",
                    "weight_pct": 100,
                }
            ],
        }
        transactions = [
            {
                "id": "txn_1111111111111111",
                "fund_code": "110020",
                "short_name": "基金110020",
                "transaction_type": "buy",
                "trade_date": start.isoformat(),
                "units": 100,
                "unit_price": 1,
                "fee": 0,
                "created_at": "2025-09-01T00:00:00Z",
            },
            {
                "id": "txn_2222222222222222",
                "fund_code": "110020",
                "short_name": "基金110020",
                "transaction_type": "buy",
                "trade_date": end.isoformat(),
                "units": 10,
                "unit_price": 1.1,
                "fee": 0,
                "created_at": "2026-08-31T00:00:00Z",
            },
            {
                "id": "txn_3333333333333333",
                "fund_code": "110020",
                "short_name": "基金110020",
                "transaction_type": "sell",
                "trade_date": end.isoformat(),
                "units": 50,
                "unit_price": 1.1,
                "fee": 0,
                "created_at": "2026-08-31T00:00:01Z",
            },
        ]
        service = self.service(
            portfolio,
            {"110020": [(start, 0), (gain_date, 0.10), (end, 0)]},
            {"110020": [(gain_date, 0.05), (end, 0)]},
            transactions,
        )

        result = service.analyze(
            user_id="chen",
            portfolio_id=portfolio["id"],
            months=12,
        )

        self.assertEqual(result["data_kind"], "actual_portfolio_performance")
        self.assertAlmostEqual(result["twr_pct"], 10.0, places=2)
        self.assertAlmostEqual(result["xirr_pct"], 10.0, delta=0.1)
        # 同一天既买入 11 元又卖出 55 元时，两个毛额不能被净成取出 44 元。
        self.assertEqual(result["cash_flows"]["contributions"], 111.0)
        self.assertEqual(result["cash_flows"]["withdrawals"], 55.0)
        self.assertEqual(result["cash_flows"]["ending_value"], 66.0)
        self.assertEqual(
            result["attribution"][0]["pnl_contribution_amount"],
            10.0,
        )
        self.assertEqual(
            result["attribution"][0]["return_contribution_pct_points"],
            10.0,
        )
        self.assertEqual(result["benchmark"]["period_return_pct"], 5.0)
        self.assertEqual(result["benchmark"]["excess_return_pct"], 5.0)

    def test_allocation_mode_is_explicit_and_contributions_sum_to_twr(self):
        first = date(2025, 9, 1)
        second = date(2026, 1, 2)
        third = date(2026, 8, 31)
        portfolio = {
            "id": "portfolio_abcdef1234567890",
            "name": "配置组合",
            "position_mode": "allocation",
            "holdings": [
                {"fund_code": "110020", "short_name": "基金A", "weight_pct": 60},
                {"fund_code": "110007", "short_name": "基金B", "weight_pct": 40},
            ],
        }
        fund_returns = {
            "110020": [(first, 0), (second, 0.10), (third, -0.02)],
            "110007": [(first, 0), (second, 0.02), (third, 0.01)],
        }
        benchmark_returns = {
            "110020": [(second, 0.08), (third, -0.01)],
            "110007": [(second, 0.01), (third, 0.005)],
        }
        result = self.service(
            portfolio,
            fund_returns,
            benchmark_returns,
        ).analyze(
            user_id="chen",
            portfolio_id=portfolio["id"],
            months=12,
        )

        contribution_sum = sum(
            item["return_contribution_pct_points"]
            for item in result["attribution"]
        )
        self.assertEqual(result["data_kind"], "simulated_allocation_performance")
        self.assertIsNone(result["xirr_pct"])
        self.assertAlmostEqual(contribution_sum, result["twr_pct"], places=2)
        self.assertEqual(result["benchmark"]["status"], "matched")


if __name__ == "__main__":
    unittest.main()

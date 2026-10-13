"""基金业绩基准解析、存储和相对收益比较测试。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.database import Database
from app.services.fund_benchmarks import (
    AkshareFundBenchmarkProvider,
    FundBenchmarkPerformanceService,
    FundBenchmarkPoint,
    FundBenchmarkRepository,
    FundBenchmarkSeries,
)


class FundBenchmarkProviderTests(unittest.TestCase):
    def test_composes_index_and_configured_cash_component(self) -> None:
        provider = AkshareFundBenchmarkProvider(cash_rate_annual_pct=0.0)
        profile = {
            "fund_code": "110020",
            "benchmark": "沪深300指数收益率*95%+活期存款利率(税后)*5%",
        }
        values = {
            date(2026, 1, 1): 100.0,
            date(2026, 1, 2): 110.0,
            date(2026, 1, 3): 121.0,
        }

        with patch.object(
            provider,
            "_load_component_values",
            return_value=values,
        ):
            series = provider.fetch_series(
                profile=profile,
                date_from=date(2026, 1, 2),
                date_to=date(2026, 1, 3),
            )

        self.assertEqual(series.benchmark_status, "proxy")
        self.assertEqual(len(series.points), 2)
        self.assertAlmostEqual(series.points[0].daily_return, 0.095)
        self.assertIn("固定年利率 0.00%", series.warnings[0])

    def test_composes_bond_and_equity_components_without_proxy(self) -> None:
        provider = AkshareFundBenchmarkProvider(cash_rate_annual_pct=0.0)
        profile = {
            "fund_code": "110007",
            "benchmark": "中债新综合财富指数收益率*90%+沪深300指数收益率*10%",
        }
        component_values = {
            "china_bond_new_composite_wealth": {
                date(2026, 1, 1): 100.0,
                date(2026, 1, 2): 101.0,
                date(2026, 1, 3): 102.01,
            },
            "hs300": {
                date(2026, 1, 1): 100.0,
                date(2026, 1, 2): 102.0,
                date(2026, 1, 3): 104.04,
            },
        }

        def load_values(*, component, date_from, date_to):
            del date_from, date_to
            return component_values[component.key]

        with patch.object(
            provider,
            "_load_component_values",
            side_effect=load_values,
        ):
            series = provider.fetch_series(
                profile=profile,
                date_from=date(2026, 1, 2),
                date_to=date(2026, 1, 3),
            )

        self.assertEqual(series.benchmark_status, "matched")
        self.assertAlmostEqual(series.points[0].daily_return, 0.011)
        self.assertEqual(series.warnings, [])

    def test_unknown_benchmark_is_explicitly_unavailable(self) -> None:
        series = AkshareFundBenchmarkProvider().fetch_series(
            profile={
                "fund_code": "000001",
                "benchmark": "某自定义策略指数收益率*100%",
            },
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 3),
        )

        self.assertEqual(series.benchmark_status, "unavailable")
        self.assertEqual(series.points, [])
        self.assertIn("暂不支持", series.warnings[0])


class FundBenchmarkRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        path = Path(self.temporary_directory.name) / "benchmarks.db"
        self.database = Database(f"sqlite:///{path}")
        self.repository = FundBenchmarkRepository(self.database)

    def tearDown(self) -> None:
        self.database.engine.dispose()
        self.temporary_directory.cleanup()

    def test_persists_and_compares_only_common_trading_days(self) -> None:
        fetched_at = datetime(2026, 1, 4, tzinfo=timezone.utc)
        series = FundBenchmarkSeries(
            fund_code="000001",
            benchmark_name="人工基准",
            benchmark_status="matched",
            points=[
                FundBenchmarkPoint(date(2026, 1, 2), 0.01),
                FundBenchmarkPoint(date(2026, 1, 3), 0.01),
            ],
            methodology="人工固定基准",
            source="unit-test",
            source_urls=["https://example.com/benchmark"],
            warnings=[],
        )
        self.repository.replace_series(series=series, fetched_at=fetched_at)

        comparison = FundBenchmarkPerformanceService(
            self.repository
        ).compare(
            fund_code="000001",
            profile={"benchmark": "人工基准"},
            fund_index_points=[
                {"nav_date": "2026-01-01", "index_value": 1.0},
                {"nav_date": "2026-01-02", "index_value": 1.02},
                {"nav_date": "2026-01-03", "index_value": 1.0404},
            ],
            fund_daily_returns=[0.02, 0.02],
        )
        status = self.repository.status()

        self.assertEqual(comparison["status"], "matched")
        self.assertEqual(comparison["period_return_pct"], 2.01)
        self.assertEqual(comparison["fund_aligned_return_pct"], 4.04)
        self.assertEqual(comparison["excess_return_pct"], 2.03)
        self.assertEqual(comparison["coverage_pct"], 100.0)
        self.assertEqual(status["record_count"], 2)
        self.assertEqual(status["matched_fund_count"], 1)


if __name__ == "__main__":
    unittest.main()

"""宽基指数采集、落库和确定性分析测试。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from app.database import Database
from app.services.market_indices import (
    MARKET_INDEX_DEFINITIONS,
    AkshareMarketIndexProvider,
    MarketIndexAnalysisService,
    MarketIndexBar,
    MarketIndexRepository,
    MarketIndexSeries,
    MarketIndexSyncService,
)


class FakeMarketIndexProvider:
    """为每个配置指数生成可预测历史，不访问真实财经网络。"""

    source = "Fake Index Provider"

    def __init__(self) -> None:
        # 创业板设为最强，中证1000设为最弱，便于验证相对强弱排名。
        self.daily_rates = [0.0005, 0.0008, 0.0010, 0.0002, -0.0004, 0.0015, 0.0007]

    def fetch_all(self) -> tuple[MarketIndexSeries, ...]:
        start = date(2026, 4, 23)
        result: list[MarketIndexSeries] = []
        for definition, daily_rate in zip(
            MARKET_INDEX_DEFINITIONS,
            self.daily_rates,
            strict=True,
        ):
            bars = tuple(
                MarketIndexBar(
                    data_date=start + timedelta(days=offset),
                    close=1000 * (1 + daily_rate) ** offset,
                    volume=1_000_000 * (1.15 if offset == 89 else 1.0),
                )
                for offset in range(90)
            )
            result.append(MarketIndexSeries(definition=definition, bars=bars))
        return tuple(result)


class MarketIndexServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "indices.db"
        self.database = Database(f"sqlite:///{database_path}")
        self.repository = MarketIndexRepository(self.database)
        self.sync_service = MarketIndexSyncService(
            repository=self.repository,
            provider=FakeMarketIndexProvider(),
        )
        self.analysis_service = MarketIndexAnalysisService(self.repository)

    def tearDown(self) -> None:
        self.database.engine.dispose()
        self.temporary_directory.cleanup()

    def test_sync_replaces_all_index_series_without_duplicate_rows(self) -> None:
        first = self.sync_service.sync()
        second = self.sync_service.sync()
        status = self.repository.status()

        self.assertEqual(first["write_action"], "inserted")
        self.assertEqual(second["write_action"], "updated")
        self.assertEqual(status["index_count"], 7)
        self.assertEqual(status["index_bar_count"], 630)

    def test_analysis_ranks_indices_and_exposes_risk_dimensions(self) -> None:
        self.sync_service.sync()
        result = self.analysis_service.analyze()

        self.assertEqual(result["data_mode"], "database")
        self.assertEqual(result["index_count"], 7)
        self.assertIn("7 个宽基中", result["summary"])
        self.assertEqual(result["strongest_index_name"], "创业板指")
        self.assertEqual(result["weakest_index_name"], "中证1000")
        self.assertGreater(result["spread_20d_pct"], 0)
        self.assertGreater(result["spread_30d_pct"], 0)
        self.assertGreater(result["spread_3m_pct"], 0)
        self.assertEqual(result["strongest_index_30d_name"], "创业板指")
        self.assertEqual(result["strongest_index_3m_name"], "创业板指")
        self.assertIn("两个周期相对较弱的均为中证1000", result["summary"])
        hs300 = next(
            item for item in result["indices"] if item["index_code"] == "000300"
        )
        self.assertEqual(hs300["trend_label"], "中期偏强")
        self.assertGreater(hs300["return_20d_pct"], 0)
        self.assertGreater(hs300["return_30d_pct"], hs300["return_20d_pct"])
        self.assertEqual(hs300["return_3m_pct"], hs300["return_60d_pct"])
        self.assertLessEqual(hs300["max_drawdown_60d_pct"], 0)
        self.assertEqual(hs300["relative_strength_rank"], 2)
        self.assertEqual(hs300["relative_strength_rank_30d"], 2)
        self.assertEqual(hs300["relative_strength_rank_3m"], 2)

        # 科创50必须有自己的历史计算，不能复制创业板或只加一个空的前端标签。
        star50 = next(item for item in result["indices"] if item["index_code"] == "000688")
        self.assertEqual(star50["index_name"], "科创50")
        self.assertEqual(star50["provider_symbol"], "sh000688")
        self.assertAlmostEqual(star50["return_30d_pct"], ((1.0007 ** 30) - 1) * 100, places=2)
        self.assertAlmostEqual(star50["return_3m_pct"], ((1.0007 ** 60) - 1) * 100, places=2)

    def test_daily_provider_fetches_star50_using_its_own_symbol(self) -> None:
        series = FakeMarketIndexProvider().fetch_all()
        frames = {
            item.definition.provider_symbol: pd.DataFrame([
                {"date": bar.data_date, "close": bar.close, "volume": bar.volume}
                for bar in item.bars
            ])
            for item in series
        }
        with patch("akshare.stock_zh_index_daily", side_effect=lambda *, symbol: frames[symbol]) as fetch:
            result = AkshareMarketIndexProvider().fetch_all()
        self.assertEqual([call.kwargs["symbol"] for call in fetch.call_args_list], list(frames))
        self.assertEqual(result[-1], series[-1])
        self.assertEqual(result[-1].definition.index_code, "000688")

    def test_empty_database_returns_actionable_empty_state(self) -> None:
        result = self.analysis_service.analyze()

        self.assertEqual(result["data_mode"], "unavailable")
        self.assertEqual(result["index_count"], 0)
        self.assertEqual(result["indices"], [])
        self.assertIn("同步", result["summary"])


if __name__ == "__main__":
    unittest.main()

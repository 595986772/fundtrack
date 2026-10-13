"""MarketSentimentService 的公式、标签和冻结快照测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app.services.market_sentiment import (
    DEFAULT_MARKET_FIXTURE,
    MarketSentimentError,
    MarketSentimentService,
)
from app.services.funds import fund_catalog_service


class FakeCatalog:
    """只返回人工净值，让测试不依赖真实基金文件。"""

    def __init__(self, prices: list[float]) -> None:
        start = date(2026, 1, 1)
        self.points = [
            {
                "nav_date": (start + timedelta(days=index)).isoformat(),
                "unit_nav": price,
                "daily_growth_rate_pct": None,
            }
            for index, price in enumerate(prices)
        ]

    def get_fund(self, _: str) -> dict:
        return {"nav_points": self.points}


def _snapshot(data_date: str) -> dict:
    """生成一份上涨宽度占优且成交放量的人工快照。"""

    return {
        "data_date": data_date,
        "broad_market_proxy": {
            "fund_code": "000001",
            "name": "人工宽基代理",
            "tracking_target": "测试指数",
            "source": "test source",
            "source_url": "https://example.com/fund",
        },
        "breadth": {
            "advancing_count": 70,
            "declining_count": 20,
            "flat_count": 10,
            "suspended_count": 0,
            "limit_up_count": 5,
            "limit_down_count": 1,
            "market_activity_pct": 70.0,
            "source": "test breadth",
            "source_url": "https://example.com/breadth",
        },
        "turnover_proxy": {
            "benchmark_code": "TEST",
            "benchmark_name": "测试指数",
            "source": "test amount",
            "source_url": "https://example.com/amount",
            "daily_amounts": [
                {"date": "2026-02-26", "amount_cny": 100},
                {"date": "2026-02-27", "amount_cny": 100},
                {"date": "2026-02-28", "amount_cny": 100},
                {"date": "2026-03-01", "amount_cny": 100},
                {"date": "2026-03-02", "amount_cny": 150},
            ],
        },
    }


class MarketSentimentServiceTests(unittest.TestCase):
    def test_artificial_uptrend_produces_constructive_state(self) -> None:
        prices = [100 + index for index in range(61)]

        with tempfile.TemporaryDirectory() as directory:
            fixture_path = Path(directory) / "market.json"
            fixture_path.write_text(
                json.dumps(_snapshot("2026-03-02")),
                encoding="utf-8",
            )
            service = MarketSentimentService(
                fixture_path=fixture_path,
                catalog=FakeCatalog(prices),
            )
            result = service.analyze()

        self.assertEqual(result["trend"]["label"], "上行")
        self.assertEqual(result["breadth"]["advance_ratio_pct"], 70.0)
        self.assertEqual(result["activity"]["label"], "放量")
        self.assertEqual(result["state_code"], "constructive")
        self.assertEqual(len(result["signals"]), 4)
        self.assertGreater(result["trend"]["return_30d_pct"], 0)
        self.assertEqual(
            result["trend"]["return_3m_pct"],
            result["trend"]["return_60d_pct"],
        )
        self.assertIn("近 3 个月", result["signals"][0]["value_text"])

    def test_project_fixture_returns_expected_mixed_recovery(self) -> None:
        # 显式不注入 Repository，保证该测试永远验证冻结基线本身。
        result = MarketSentimentService(
            fixture_path=DEFAULT_MARKET_FIXTURE,
            catalog=fund_catalog_service,
        ).analyze()

        self.assertEqual(result["data_date"], "2026-08-25")
        self.assertEqual(result["data_mode"], "fixture")
        self.assertEqual(result["state_code"], "mixed_recovery")
        self.assertEqual(result["trend"]["return_60d_pct"], -4.5)
        self.assertIn("return_30d_pct", result["trend"])
        self.assertEqual(result["trend"]["return_3m_pct"], -4.5)
        self.assertEqual(result["breadth"]["advancing_count"], 3966)
        self.assertEqual(
            result["activity"]["latest_vs_five_day_average_pct"],
            84.71,
        )

    def test_missing_fixture_is_rejected(self) -> None:
        service = MarketSentimentService(
            fixture_path=Path("/not/a/real/market-snapshot.json"),
            catalog=FakeCatalog([100 + index for index in range(61)]),
        )

        with self.assertRaises(MarketSentimentError):
            service.analyze()


if __name__ == "__main__":
    unittest.main()

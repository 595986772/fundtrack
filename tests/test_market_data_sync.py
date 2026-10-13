"""每日市场宽度与成交额同步管线的确定性测试。

测试只使用 Fake Provider 和临时 SQLite，不访问 AKShare，也不调用大模型。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import pandas as pd

from app.database import Database
from app.services.background_jobs import BackgroundJobService
from app.services.cron_scheduler import CronSchedulerService
from app.services.market_data_sync import (
    AkshareMarketSnapshotProvider,
    CSINDEX_TURNOVER_SOURCE,
    MarketAmountPoint,
    MarketDataSyncError,
    MarketSnapshot,
    MarketSnapshotRepository,
    MarketSnapshotSyncService,
)
from app.services.market_sentiment import MarketSentimentService


class FakeMarketProvider:
    """返回 2026-08-26 的人工市场截面。"""

    source = "Fake Market Provider"

    def fetch_snapshot(self) -> MarketSnapshot:
        amount_start = date(2026, 8, 20)
        amounts = tuple(
            MarketAmountPoint(
                data_date=amount_start + timedelta(days=index),
                amount_cny=float(100 + index * 10),
            )
            for index in range(4)
        ) + (MarketAmountPoint(date(2026, 8, 26), 180.0),)
        return MarketSnapshot(
            data_date=date(2026, 8, 26),
            observed_at=datetime(2026, 8, 26, 15, tzinfo=timezone.utc),
            advancing_count=3200,
            declining_count=1800,
            flat_count=100,
            suspended_count=5,
            limit_up_count=60,
            limit_down_count=8,
            market_activity_pct=62.75,
            daily_amounts=amounts,
        )


class FailingMarketProvider:
    """模拟全市场截面源临时不可用。"""

    source = "Failing Market Provider"

    def fetch_snapshot(self) -> MarketSnapshot:
        raise MarketDataSyncError("市场宽度与成交额日期暂未对齐")


class FakeCatalog:
    """提供截至市场日期的 61 个宽基代理净值点。"""

    def get_fund(self, _: str) -> dict:
        start = date(2026, 6, 27)
        points = [
            {
                "nav_date": (start + timedelta(days=index)).isoformat(),
                "unit_nav": 1 + index * 0.01,
                "daily_growth_rate_pct": None,
            }
            for index in range(61)
        ]
        return {
            "profile": {
                "source_url": "https://example.com/fund",
            },
            "source": "Fake Fund Database",
            "nav_points": points,
        }


class FakeIndexSyncService:
    """模拟六指数子链路，验证总同步服务会正确组合结果。"""

    def sync(self) -> dict:
        return {
            "index_count": 6,
            "bar_count": 540,
            "data_date": "2026-08-26",
            "source": "Fake Index Provider",
            "summary": "已同步 6 个宽基指数。",
        }

    def status(self) -> dict:
        return {
            "index_bar_count": 540,
            "index_count": 6,
            "index_snapshot_start": "2026-04-01",
            "index_snapshot_end": "2026-08-26",
            "index_last_successful_sync_at": "2026-08-26T08:00:00+00:00",
        }


class MarketDataSyncServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        path = Path(self.temporary_directory.name) / "market.db"
        self.database = Database(f"sqlite:///{path}")
        self.repository = MarketSnapshotRepository(self.database)
        self.service = MarketSnapshotSyncService(
            repository=self.repository,
            provider=FakeMarketProvider(),
        )

    def tearDown(self) -> None:
        self.database.engine.dispose()
        self.temporary_directory.cleanup()

    def test_sync_upserts_same_trading_day(self) -> None:
        first = self.service.sync()
        second = self.service.sync()
        status = self.service.status()

        self.assertEqual(first["write_action"], "inserted")
        self.assertEqual(second["write_action"], "updated")
        self.assertEqual(status["data_mode"], "database")
        self.assertEqual(status["record_count"], 1)
        self.assertEqual(status["snapshot_end"], "2026-08-26")

    def test_sentiment_prefers_database_snapshot(self) -> None:
        self.service.sync()
        sentiment = MarketSentimentService(
            fixture_path=Path("/fixture/should/not/be/read.json"),
            catalog=FakeCatalog(),
            snapshot_repository=self.repository,
        ).analyze()

        self.assertEqual(sentiment["data_mode"], "database")
        self.assertEqual(sentiment["data_kind"], "database_market_snapshot")
        self.assertEqual(sentiment["data_date"], "2026-08-26")
        self.assertEqual(sentiment["breadth"]["advancing_count"], 3200)
        self.assertEqual(sentiment["sources"][0]["data_date"], "2026-08-26")

    def test_product_sync_combines_snapshot_and_index_series(self) -> None:
        service = MarketSnapshotSyncService(
            repository=self.repository,
            provider=FakeMarketProvider(),
            index_sync_service=FakeIndexSyncService(),
        )

        result = service.sync()
        status = service.status()

        self.assertEqual(result["index_sync"]["index_count"], 6)
        self.assertIn("6 个宽基指数", result["summary"])
        self.assertEqual(status["index_count"], 6)
        self.assertEqual(status["index_bar_count"], 540)

    def test_index_success_keeps_job_usable_when_market_overview_is_delayed(self) -> None:
        service = MarketSnapshotSyncService(
            repository=self.repository,
            provider=FailingMarketProvider(),
            index_sync_service=FakeIndexSyncService(),
        )

        result = service.sync()

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["index_sync"]["index_count"], 6)
        self.assertIsNone(result["write_action"])
        self.assertIn("沿用上一份快照", result["warnings"][0])

    def test_daily_schedule_is_durable_and_idempotent(self) -> None:
        background_jobs = BackgroundJobService()
        scheduler = CronSchedulerService(
            root=Path(self.temporary_directory.name) / "schedules",
            background_jobs=background_jobs,
        )
        try:
            first = self.service.ensure_daily_schedule(scheduler)
            second = self.service.ensure_daily_schedule(scheduler)
            schedules = scheduler.list_schedules(user_id="fundscope-system")

            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(schedules["count"], 1)
            self.assertEqual(
                schedules["schedules"][0]["job_type"],
                "market_snapshot_sync",
            )
        finally:
            scheduler.stop(wait=True)
            background_jobs.shutdown(wait=True)


class AkshareMarketSnapshotProviderTests(unittest.TestCase):
    def test_uses_csindex_when_eastmoney_is_unavailable(self) -> None:
        """东方财富失败时，中证官网数据应按亿元转换为人民币元。"""

        activity_frame = pd.DataFrame(
            {
                "item": [
                    "统计日期",
                    "上涨",
                    "下跌",
                    "平盘",
                    "停牌",
                    "涨停",
                    "跌停",
                    "活跃度",
                ],
                "value": [
                    "2026-08-26 15:00:00",
                    "3200",
                    "1800",
                    "100",
                    "5",
                    "60",
                    "8",
                    "62.75%",
                ],
            }
        )
        csindex_frame = pd.DataFrame(
            {
                "日期": [
                    "2026-08-20",
                    "2026-08-21",
                    "2026-08-24",
                    "2026-08-25",
                    "2026-08-26",
                ],
                "成交金额": [5540.21, 5055.01, 5904.26, 4804.22, 4890.57],
            }
        )

        with (
            patch("akshare.stock_market_activity_legu", return_value=activity_frame),
            patch(
                "akshare.stock_zh_index_daily_em",
                side_effect=ConnectionError("temporary disconnect"),
            ),
            patch(
                "akshare.stock_zh_index_hist_csindex",
                return_value=csindex_frame,
            ),
        ):
            snapshot = AkshareMarketSnapshotProvider().fetch_snapshot()

        self.assertEqual(snapshot.turnover_source, CSINDEX_TURNOVER_SOURCE)
        self.assertEqual(snapshot.data_date, date(2026, 8, 26))
        self.assertEqual(snapshot.daily_amounts[-1].amount_cny, 489_057_000_000.0)


class MarketDataSyncJobTests(unittest.TestCase):
    def test_background_job_runs_sync_without_agent_loop(self) -> None:
        service = BackgroundJobService()
        fake_result = {
            "summary": "市场状态快照已同步至 2026-08-26。",
            "workflow": "market_snapshot_sync",
            "data_date": "2026-08-26",
        }
        try:
            with patch(
                "app.services.market_data_sync.market_snapshot_sync_service.sync",
                return_value=fake_result,
            ) as sync_mock:
                service.start_job(
                    user_id="chen",
                    job_id="market-sync-test",
                    title="同步市场快照",
                    job_type="market_snapshot_sync",
                )
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    job = service.get_job(
                        user_id="chen",
                        job_id="market-sync-test",
                    )["job"]
                    if job["status"] in {"completed", "failed", "cancelled"}:
                        break
                    time.sleep(0.01)

            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["result"]["workflow"], "market_snapshot_sync")
            sync_mock.assert_called_once_with()
        finally:
            service.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()

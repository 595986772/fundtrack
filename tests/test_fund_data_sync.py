"""基金每日净值同步管线的确定性测试。

测试使用临时 SQLite 和 Fake Provider，不访问 AKShare。这样既能验证真实的
Provider -> 清洗结构 -> Repository -> Catalog/Job/Cron 连接，又不会把网络稳定性
误当成项目代码是否正确。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from app.database import Database
from app.services.background_jobs import BackgroundJobService
from app.services.cron_scheduler import CronSchedulerService
from app.services.fund_corporate_actions import (
    FundCorporateAction,
    FundCorporateActionRepository,
)
from app.services.fund_benchmarks import (
    FundBenchmarkPoint,
    FundBenchmarkRepository,
    FundBenchmarkSeries,
)
from app.services.fund_data_sync import (
    DEFAULT_FIXTURE_DIR,
    FundNavPoint,
    FundNavSnapshotRepository,
    FundNavSyncService,
)
from app.services.fund_catalog import (
    FundCatalogImportService,
    FundCatalogRepository,
    FundDirectoryEntry,
    FundDirectorySearchBatch,
    FundDirectoryService,
)
from app.services.funds import FundCatalogService


class FakeFundNavProvider:
    """返回两条人工净值，替代单元测试中的真实财经网站。"""

    source = "Fake Fund Provider"

    def fetch_nav(self, *, fund_code: str, date_from: date) -> list[FundNavPoint]:
        return [
            FundNavPoint(
                fund_code=fund_code,
                nav_date=date(2026, 8, 25),
                unit_nav=1.2345,
                daily_growth_rate_pct=0.10,
            ),
            FundNavPoint(
                fund_code=fund_code,
                nav_date=date(2026, 8, 26),
                unit_nav=1.2468,
                daily_growth_rate_pct=1.00,
            ),
        ]


class FakeFundCorporateActionProvider:
    """返回一条人工分红，证明空净值同步之外的复权依据也会落库。"""

    source = "Fake Corporate Action Provider"

    def fetch_actions(
        self,
        *,
        fund_code: str,
        date_from: date,
        date_to: date,
    ) -> list[FundCorporateAction]:
        return [
            FundCorporateAction(
                fund_code=fund_code,
                action_date=date(2026, 8, 26),
                action_type="cash_dividend",
                cash_dividend_per_unit=0.01,
                description="每份派现金0.0100元",
            )
        ]


class FakeFundBenchmarkProvider:
    """返回两天人工基准收益，验证同步编排会把基准独立落库。"""

    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.batch_count = 0

    def begin_batch(self) -> None:
        self.batch_count += 1

    def fetch_series(
        self,
        *,
        profile: dict,
        date_from: date,
        date_to: date,
    ) -> FundBenchmarkSeries:
        if self.should_fail:
            raise RuntimeError("人工基准源暂时不可用")
        return FundBenchmarkSeries(
            fund_code=profile["fund_code"],
            benchmark_name=profile["benchmark"],
            benchmark_status="matched",
            points=[
                FundBenchmarkPoint(date(2026, 8, 25), 0.001),
                FundBenchmarkPoint(date(2026, 8, 26), 0.002),
            ],
            methodology="单元测试固定基准",
            source="Fake Benchmark Provider",
            source_urls=["https://example.com/benchmark"],
            warnings=[],
        )


class FakeFundCatalogProvider:
    """模拟全量目录和基金档案，避免测试访问真实财经网站。"""

    source = "Fake Fund Directory"
    entry = FundDirectoryEntry(
        fund_code="000001",
        short_name="华夏成长混合",
        fund_type="混合型-灵活",
        pinyin_abbreviation="HXCZHH",
        pinyin_name="HUAXIACHENGZHANGHUNHE",
    )

    def search(self, *, query: str, limit: int, refresh: bool = False):
        matches = [self.entry] if query in {"华夏", "000001", "HXCZ"} else []
        return FundDirectorySearchBatch(
            entries=matches[:limit],
            fetched_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
            cache_hit=not refresh,
        )

    def get_entry(self, fund_code: str) -> FundDirectoryEntry:
        if fund_code != self.entry.fund_code:
            raise ValueError("基金不存在")
        return self.entry

    def fetch_profile(self, entry: FundDirectoryEntry) -> dict:
        return {
            "fund_code": entry.fund_code,
            "full_name": "华夏成长证券投资基金",
            "short_name": entry.short_name,
            "fund_type": entry.fund_type,
            "sample_role": "general_fund",
            "source_url": entry.source_url,
        }


class FundDataSyncServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "fund-sync.db"
        self.database = Database(f"sqlite:///{database_path}")
        self.repository = FundNavSnapshotRepository(self.database)
        self.action_repository = FundCorporateActionRepository(self.database)
        self.catalog_repository = FundCatalogRepository(self.database)
        self.service = FundNavSyncService(
            repository=self.repository,
            provider=FakeFundNavProvider(),
            fixture_dir=DEFAULT_FIXTURE_DIR,
            catalog_repository=self.catalog_repository,
            corporate_action_repository=self.action_repository,
            corporate_action_provider=FakeFundCorporateActionProvider(),
        )

    def tearDown(self) -> None:
        self.database.engine.dispose()
        self.temporary_directory.cleanup()

    def test_sync_upserts_rows_and_exposes_database_status(self) -> None:
        """重复同步同一天应该 update，不应该产生重复净值。"""

        first = self.service.sync(fund_codes=["110020"])
        second = self.service.sync(fund_codes=["110020"])
        status = self.service.status()

        self.assertEqual(first["successful_funds"], 1)
        self.assertEqual(first["results"][0]["inserted"], 2)
        self.assertEqual(second["results"][0]["inserted"], 0)
        self.assertEqual(second["results"][0]["updated"], 2)
        self.assertEqual(status["data_mode"], "database")
        self.assertEqual(status["record_count"], 2)
        self.assertEqual(status["snapshot_end"], "2026-08-26")
        self.assertEqual(first["results"][0]["corporate_actions"]["action_count"], 1)
        self.assertEqual(status["corporate_actions"]["action_count"], 1)
        self.assertEqual(status["corporate_actions"]["fund_count"], 1)

    def test_catalog_merges_database_rows_over_frozen_baseline(self) -> None:
        """数据库覆盖相同日期并追加新日期，原有三年基线继续保留。"""

        self.service.sync(fund_codes=["110020"])
        catalog = FundCatalogService(
            fixture_dir=DEFAULT_FIXTURE_DIR,
            snapshot_repository=self.repository,
            catalog_repository=self.catalog_repository,
        )

        detail = catalog.get_fund("110020")
        listed = catalog.list_funds()

        self.assertEqual(detail["data_mode"], "database")
        self.assertEqual(detail["latest_nav_date"], "2026-08-26")
        self.assertEqual(detail["latest_nav"], 1.2468)
        self.assertEqual(len(detail["corporate_actions"]), 1)
        self.assertEqual(
            detail["corporate_action_coverage"]["coverage_end"],
            "2026-08-26",
        )
        self.assertEqual(listed["data_mode"], "database")
        self.assertGreater(detail["nav_record_count"], 500)

    def test_sync_persists_benchmark_without_mixing_it_into_nav_rows(self) -> None:
        """基准与净值共享一次刷新入口，但分别落入独立的数据表。"""

        benchmark_repository = FundBenchmarkRepository(self.database)
        benchmark_provider = FakeFundBenchmarkProvider()
        service = FundNavSyncService(
            repository=self.repository,
            provider=FakeFundNavProvider(),
            fixture_dir=DEFAULT_FIXTURE_DIR,
            benchmark_repository=benchmark_repository,
            benchmark_provider=benchmark_provider,
        )

        result = service.sync(fund_codes=["110020"])
        benchmark_status = service.status()["benchmarks"]

        self.assertEqual(benchmark_provider.batch_count, 1)
        self.assertEqual(
            result["results"][0]["benchmark"]["benchmark_status"],
            "matched",
        )
        self.assertEqual(benchmark_status["record_count"], 2)
        self.assertEqual(benchmark_status["matched_fund_count"], 1)
        self.assertEqual(self.repository.status()["record_count"], 2)

    def test_benchmark_failure_does_not_discard_successful_nav_sync(self) -> None:
        """指数网站故障时保留已同步净值，并把失败状态留给下一次刷新重试。"""

        service = FundNavSyncService(
            repository=self.repository,
            provider=FakeFundNavProvider(),
            fixture_dir=DEFAULT_FIXTURE_DIR,
            benchmark_repository=FundBenchmarkRepository(self.database),
            benchmark_provider=FakeFundBenchmarkProvider(should_fail=True),
        )

        result = service.sync(fund_codes=["110020"])

        self.assertEqual(result["successful_funds"], 1)
        self.assertEqual(result["results"][0]["benchmark"]["benchmark_status"], "failed")
        self.assertEqual(self.repository.status()["record_count"], 2)

    def test_daily_schedule_is_durable_and_idempotent(self) -> None:
        """FastAPI 重启多次只保留一条系统净值 Cron。"""

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
            self.assertEqual(schedules["schedules"][0]["job_type"], "fund_nav_sync")
        finally:
            scheduler.stop(wait=True)
            background_jobs.shutdown(wait=True)

    def test_search_import_and_arbitrary_fund_sync_form_one_catalog(self) -> None:
        """任意目录基金完成导入后，应进入列表、详情和后续组合可用范围。"""

        provider = FakeFundCatalogProvider()
        directory = FundDirectoryService(
            repository=self.catalog_repository,
            provider=provider,
            fixture_dir=DEFAULT_FIXTURE_DIR,
        )
        importer = FundCatalogImportService(
            repository=self.catalog_repository,
            provider=provider,
        )

        searched = directory.search(query="华夏", limit=10)
        self.assertEqual(searched["funds"][0]["import_status"], "discovered")

        fake_nav_result = {
            "summary": "净值同步完成",
            "workflow": "fund_nav_sync",
        }
        with patch(
            "app.services.fund_data_sync.fund_nav_sync_service.sync",
            return_value=fake_nav_result,
        ):
            imported = importer.import_fund(fund_code="000001")
        self.assertEqual(imported["workflow"], "fund_catalog_import")
        self.assertEqual(
            self.catalog_repository.get_many(["000001"])["000001"][
                "import_status"
            ],
            "ready",
        )

        # 导入 Service 负责状态；真实净值同步仍由独立 SyncService 负责事实入库。
        self.service.sync(fund_codes=["000001"])
        catalog = FundCatalogService(
            fixture_dir=DEFAULT_FIXTURE_DIR,
            snapshot_repository=self.repository,
            catalog_repository=self.catalog_repository,
        )
        listed = catalog.list_funds()
        detail = catalog.get_fund("000001")

        self.assertEqual(listed["count"], 4)
        self.assertEqual(detail["profile"]["short_name"], "华夏成长混合")
        self.assertEqual(detail["latest_nav_date"], "2026-08-26")


class FundDataSyncJobTests(unittest.TestCase):
    def test_background_job_runs_sync_without_calling_a_model(self) -> None:
        """fund_nav_sync 应由 JobRunner 直接调用同步服务，不经过 Agent Loop。"""

        service = BackgroundJobService()
        fake_result = {
            "summary": "已同步 3/3 只基金，数据库净值更新至 2026-08-26。",
            "workflow": "fund_nav_sync",
            "successful_funds": 3,
            "failed_funds": 0,
            "results": [],
            "failures": [],
        }
        try:
            with patch(
                "app.services.fund_data_sync.fund_nav_sync_service.sync",
                return_value=fake_result,
            ) as sync_mock:
                started = service.start_job(
                    user_id="chen",
                    job_id="fund-sync-test",
                    title="同步基金净值",
                    job_type="fund_nav_sync",
                )
                deadline = time.monotonic() + 2.0
                job = started["job"]
                while time.monotonic() < deadline:
                    job = service.get_job(
                        user_id="chen",
                        job_id="fund-sync-test",
                    )["job"]
                    if job["status"] in {"completed", "failed", "cancelled"}:
                        break
                    time.sleep(0.01)

            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["result"]["workflow"], "fund_nav_sync")
            sync_mock.assert_called_once()
        finally:
            service.shutdown(wait=True)

    def test_background_job_keeps_fund_code_in_import_payload(self) -> None:
        """目录导入必须由 Worker 读取持久化 fund_code，而不是依赖 HTTP 内存。"""

        service = BackgroundJobService()
        fake_result = {
            "summary": "华夏成长混合（000001）已导入。",
            "workflow": "fund_catalog_import",
            "fund_code": "000001",
        }
        try:
            with patch(
                "app.services.fund_catalog.fund_catalog_import_service.import_fund",
                return_value=fake_result,
            ) as import_mock:
                service.start_job(
                    user_id="chen",
                    job_id="fund-import-test",
                    title="导入基金 000001",
                    job_type="fund_catalog_import",
                    fund_code="000001",
                )
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    job = service.get_job(
                        user_id="chen",
                        job_id="fund-import-test",
                    )["job"]
                    if job["status"] in {"completed", "failed", "cancelled"}:
                        break
                    time.sleep(0.01)

            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["result"]["fund_code"], "000001")
            import_mock.assert_called_once()
            self.assertEqual(import_mock.call_args.kwargs["fund_code"], "000001")
        finally:
            service.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()

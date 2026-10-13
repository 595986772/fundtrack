"""基金持仓披露 Provider 边界、SQL 幂等写入和 REST 响应测试。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.database import Database
from app.main import app
from app.services.fund_disclosures import (
    FundDisclosureBatch,
    FundDisclosureRepository,
    FundDisclosureService,
    FundDisclosureSyncService,
    FundIndustryAllocation,
    FundSecurityHolding,
)


class FakeDisclosureProvider:
    """用一份固定季报替代单元测试中的财经网络请求。"""

    source = "Fake Disclosure Provider"

    def fetch(self, *, fund_code: str, as_of: date | None = None):
        report_date = date(2026, 6, 30)
        return FundDisclosureBatch(
            fund_code=fund_code,
            security_holdings=(
                FundSecurityHolding(
                    fund_code=fund_code,
                    report_date=report_date,
                    asset_type="stock",
                    security_code="600000",
                    security_name="测试银行",
                    weight_pct=12.5,
                    market_value_10k=1250.0,
                    rank=1,
                    source_url="https://example.com/stocks",
                ),
                FundSecurityHolding(
                    fund_code=fund_code,
                    report_date=report_date,
                    asset_type="bond",
                    security_code="240001",
                    security_name="测试国债",
                    weight_pct=25.0,
                    market_value_10k=2500.0,
                    rank=1,
                    source_url="https://example.com/bonds",
                ),
            ),
            industry_allocations=(
                FundIndustryAllocation(
                    fund_code=fund_code,
                    report_date=report_date,
                    industry_name="金融业",
                    weight_pct=18.0,
                    market_value_10k=1800.0,
                    rank=1,
                    source_url="https://example.com/industries",
                ),
            ),
            stock_report_date=report_date,
            bond_report_date=report_date,
            industry_report_date=report_date,
        )


class FundDisclosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database = Database(
            f"sqlite:///{Path(self.temporary_directory.name) / 'disclosures.db'}"
        )
        self.database = database
        self.repository = FundDisclosureRepository(database)
        self.query = FundDisclosureService(
            repository=self.repository,
            fixture_path=Path(self.temporary_directory.name) / "missing.json",
            today=lambda: date(2026, 8, 30),
        )
        self.sync = FundDisclosureSyncService(
            repository=self.repository,
            provider=FakeDisclosureProvider(),
            query_service=self.query,
        )

    def tearDown(self) -> None:
        self.database.engine.dispose()
        self.temporary_directory.cleanup()

    def test_sync_replaces_same_report_without_duplicate_rows(self) -> None:
        first = self.sync.sync(fund_codes=["000001"])
        second = self.sync.sync(fund_codes=["000001"])
        detail = self.query.get_fund(fund_code="000001")
        status = self.query.status()

        self.assertEqual(first["successful_funds"], 1)
        self.assertEqual(second["successful_funds"], 1)
        self.assertEqual(detail["data_mode"], "database")
        self.assertEqual(detail["freshness_status"], "fresh")
        self.assertEqual(detail["security_count"], 2)
        self.assertEqual(detail["industry_count"], 1)
        self.assertEqual(detail["disclosed_bond_weight_pct"], 25.0)
        self.assertEqual(status["record_count"], 3)

    def test_missing_fund_is_explicitly_unavailable(self) -> None:
        detail = self.query.get_fund(fund_code="999999")

        self.assertEqual(detail["data_mode"], "unavailable")
        self.assertEqual(detail["freshness_status"], "unavailable")
        self.assertIn("尚未同步", detail["warnings"][0])

    def test_rest_endpoint_uses_same_query_service(self) -> None:
        self.sync.sync(fund_codes=["000001"])
        from app.routers import funds as fund_router

        with patch.object(fund_router, "fund_disclosure_service", self.query):
            response = TestClient(app).get("/api/v1/funds/000001/disclosures")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["report_dates"]["stock"], "2026-06-30")
        self.assertEqual(payload["security_holdings"][0]["security_code"], "600000")


if __name__ == "__main__":
    unittest.main()

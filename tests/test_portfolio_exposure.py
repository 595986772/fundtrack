"""组合底层持仓穿透、REST 接口和 LangChain Tool 的确定性测试。"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.agent_tools import get_agent_tools
from app.services.portfolio_agent_tools import get_portfolio_exposure_tool
from app.services.portfolio_exposure import PortfolioExposureService
from app.services.tool_permissions import check_tool_permission


PORTFOLIO_ID = "portfolio_0123456789abcdef"
REPORT_DATE = "2026-06-30"


class FakePortfolioReader:
    def get(self, *, user_id: str, portfolio_id: str):
        return {
            "id": portfolio_id,
            "user_id": user_id,
            "name": "穿透测试组合",
            "holdings": [
                {
                    "fund_code": "000001",
                    "short_name": "基金 A",
                    "weight_pct": 40.0,
                },
                {
                    "fund_code": "000002",
                    "short_name": "基金 B",
                    "weight_pct": 60.0,
                },
            ],
        }


class FakeDisclosureReader:
    def list_for_funds(self, fund_codes: list[str]):
        return {
            "000001": self._disclosure(
                "000001",
                securities=[
                    self._security("stock", "600001", "共同股票", 20),
                    self._security("stock", "600002", "基金A股票", 10),
                ],
                industries=[
                    self._industry("制造业", 30),
                    self._industry("金融业", 20),
                ],
            ),
            "000002": self._disclosure(
                "000002",
                securities=[
                    self._security("stock", "600001", "共同股票", 30),
                    self._security("stock", "600003", "基金B股票", 20),
                    self._security("bond", "240001", "基金B债券", 10),
                ],
                industries=[self._industry("制造业", 40)],
            ),
        }

    @staticmethod
    def _security(asset_type, code, name, weight):
        return {
            "report_date": REPORT_DATE,
            "asset_type": asset_type,
            "security_code": code,
            "security_name": name,
            "weight_pct": weight,
            "source": "Fake",
            "source_url": "https://example.com/security",
        }

    @staticmethod
    def _industry(name, weight):
        return {
            "report_date": REPORT_DATE,
            "industry_name": name,
            "weight_pct": weight,
            "source": "Fake",
            "source_url": "https://example.com/industry",
        }

    @staticmethod
    def _disclosure(code, *, securities, industries):
        return {
            "fund_code": code,
            "data_mode": "database",
            "source": "Fake",
            "source_url": "https://example.com/fund",
            "report_dates": {
                "stock": REPORT_DATE,
                "bond": REPORT_DATE,
                "industry": REPORT_DATE,
            },
            "latest_report_date": REPORT_DATE,
            "report_age_days": 61,
            "freshness_status": "fresh",
            "warnings": [],
            "security_holdings": securities,
            "industry_allocations": industries,
        }


class PortfolioExposureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PortfolioExposureService(
            portfolios=FakePortfolioReader(),
            disclosures=FakeDisclosureReader(),
        )

    def test_analyze_multiplies_weights_and_detects_overlap(self) -> None:
        result = self.service.analyze(user_id="chen", portfolio_id=PORTFOLIO_ID)

        self.assertEqual(result["data_kind"], "deterministic_portfolio_lookthrough")
        self.assertEqual(result["data_quality"]["security_coverage_pct"], 48.0)
        self.assertEqual(result["data_quality"]["industry_asset_coverage_pct"], 50.0)
        self.assertEqual(result["data_quality"]["status"], "partial")

        # 共同股票：40% × 20% + 60% × 30% = 26%。
        top = result["security_exposures"][0]
        self.assertEqual(top["security_code"], "600001")
        self.assertEqual(top["portfolio_exposure_pct"], 26.0)
        self.assertEqual(top["source_fund_count"], 2)

        # 制造业：40% × 30% + 60% × 40% = 36%。
        self.assertEqual(result["industry_exposures"][0]["industry_name"], "制造业")
        self.assertEqual(
            result["industry_exposures"][0]["portfolio_exposure_pct"],
            36.0,
        )
        overlap = result["fund_overlaps"][0]
        self.assertEqual(overlap["common_security_count"], 1)
        self.assertEqual(overlap["fund_overlap_pct"], 20.0)
        self.assertEqual(overlap["combined_portfolio_exposure_pct"], 26.0)

    def test_api_and_tool_reuse_the_same_service(self) -> None:
        from app.routers import portfolios as portfolio_router
        from app.services import portfolio_agent_tools as tool_module

        with patch.object(portfolio_router, "portfolio_exposure_service", self.service):
            response = TestClient(app).get(
                f"/api/v1/portfolios/{PORTFOLIO_ID}/exposure?user_id=chen"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["security_exposures"][0]["security_code"], "600001")

        with patch.object(tool_module, "portfolio_exposure_service", self.service):
            raw = get_portfolio_exposure_tool.invoke(
                {"user_id": "chen", "portfolio_id": PORTFOLIO_ID}
            )
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tool_result_scope"], "bounded_agent_summary")
        self.assertLess(len(raw), 8_000)
        self.assertIn(
            "get_portfolio_exposure_tool",
            {tool.name for tool in get_agent_tools()},
        )
        self.assertTrue(
            check_tool_permission("get_portfolio_exposure_tool", {}).allowed
        )


if __name__ == "__main__":
    unittest.main()

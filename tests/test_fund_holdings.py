"""用临时 SQL 数据和假档案验证单基金口径；不联网、不调用付费模型。"""
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.database import Database
from app.main import app
from app.schemas import AgentChatRequest, FundHoldingsAnalysisResponse
from app.services.agent_routing import AgentRequestRouter
from app.services.agent_tool_executor import AgentToolExecutor
from app.services.agent_execution_context import AgentExecutionContext
from app.services.fund_agent_tools import get_fund_holdings_tool
from app.services.fund_disclosures import (
    FundDisclosureRepository, FundDisclosureService, FundDisclosureBatch,
    FundSecurityHolding, FundIndustryAllocation,
)
from app.services.fund_holdings import FundHoldingsService


class FundHoldingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.db = Database(f"sqlite:///{Path(self.temp.name) / 'holdings.db'}")
        self.repo = FundDisclosureRepository(self.db)
        self.disclosures = FundDisclosureService(repository=self.repo, fixture_path=Path(self.temp.name) / "absent.json", today=lambda: date(2026, 9, 8))
        self.catalog = SimpleNamespace(get_fund=lambda code: {"profile":{"short_name":"测试基金", "fund_type":"混合型"}})
        self.service = FundHoldingsService(disclosures=self.disclosures, catalog=self.catalog)

    def tearDown(self):
        self.db.engine.dispose()
        self.temp.cleanup()

    def save(self, day="2026-06-30", *, stocks=None, bond=20, bond_day=None, source="Fixture Provider"):
        report = date.fromisoformat(day)
        bond_report = date.fromisoformat(bond_day or day)
        rows = [FundSecurityHolding("000001", report, "stock", code, f"证券{code}", weight, None, index+1, "https://example.com/stock") for index,(code,weight) in enumerate(stocks if stocks is not None else [("600001",10),("600002",5)])]
        if bond is not None:
            rows.append(FundSecurityHolding("000001", bond_report, "bond", "240001", "国债", bond, None, 1, "https://example.com/bond"))
        batch = FundDisclosureBatch("000001", tuple(rows), (FundIndustryAllocation("000001", report, "制造业", 60, None, 1, "https://example.com/industry"),), report, bond_report if bond is not None else None, report)
        self.repo.replace_latest(batch=batch, source=source, fetched_at=datetime.now(timezone.utc))

    def test_single_fund_percent_and_unknown_cash(self):
        self.save()
        result = self.service.analyze(fund_code="000001")
        FundHoldingsAnalysisResponse.model_validate(result)
        self.assertEqual(result["security_coverage_pct"], 35)
        self.assertEqual(result["unresolved_weight_pct"], 65)
        self.assertIsNone(result["asset_evidence"][2]["weight_pct"])
        self.assertEqual(result["concentration"]["top_five_disclosed_stocks_pct"], 15)
        self.assertEqual(result["concentration"]["top_industry_weight_pct"], 60)
        self.assertTrue(all(group["status"] == "unavailable" for group in result["comparisons"]))

    def test_different_dates_do_not_form_false_total(self):
        self.save(bond_day="2026-03-31")
        result = self.service.analyze(fund_code="000001")
        self.assertIsNone(result["security_coverage_pct"])
        self.assertIsNone(result["unresolved_weight_pct"])
        self.assertEqual(result["asset_evidence"][1]["weight_pct"],20)

    def test_gross_leverage_is_not_discarded(self):
        self.save(stocks=[], bond=130)
        result = self.service.analyze(fund_code="000001")
        self.assertEqual(result["gross_disclosed_weight_pct"],130)
        self.assertEqual(result["security_coverage_pct"],100)
        self.assertEqual(result["asset_evidence"][1]["weight_pct"],130)
        self.assertIn("超过 100%", " ".join(result["limitations"]))

    def test_comparison_never_treats_missing_position_as_zero_or_trade(self):
        self.save("2025-12-31",stocks=[("600001",8),("600003",12)])
        self.save()
        result = self.service.analyze(fund_code="000001")
        group = result["comparisons"][0]
        self.assertEqual(group["previous_report_date"],"2025-12-31")
        rows = {row["key"]:row for row in group["rows"]}
        self.assertEqual(rows["600001"]["change_pp"],2)
        self.assertEqual(rows["600002"]["status"],"newly_disclosed")
        self.assertIsNone(rows["600002"]["previous_weight_pct"])
        self.assertEqual(rows["600003"]["status"],"not_disclosed")
        self.assertIsNone(rows["600003"]["weight_pct"])
        self.assertIsNone(rows["600003"]["change_pp"])
        self.assertEqual(rows["600003"]["previous_source_url"],"https://example.com/stock")

    def test_history_must_be_older_and_same_provider(self):
        self.save("2026-03-31",source="Other Provider")
        self.save()
        result = self.service.analyze(fund_code="000001")
        self.assertTrue(all(group["status"] == "unavailable" for group in result["comparisons"]))

    def test_no_disclosure_and_missing_etf_mapping_are_explicit(self):
        self.catalog.get_fund = lambda code: {"profile":{"short_name":"测试ETF联接C","fund_type":"指数型"}}
        result = self.service.analyze(fund_code="000001")
        self.assertEqual(result["disclosure"]["data_mode"],"unavailable")
        self.assertEqual(result["lookthrough_status"],"target_etf_unavailable")
        self.assertIsNone(result["security_coverage_pct"])
        self.assertIsNone(result["concentration"]["top_industry_weight_pct"])

    def test_api_and_tool_use_same_service_and_get_is_read_only(self):
        self.save()
        before = self.repo.status()["record_count"]
        with patch("app.routers.funds.fund_holdings_service", self.service), patch("app.services.fund_agent_tools.fund_holdings_service",self.service):
            response = TestClient(app).get("/api/v1/funds/000001/holdings-analysis")
            tool = json.loads(get_fund_holdings_tool.invoke({"fund_code":"000001"}))
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()["concentration"],tool["concentration"])
        self.assertEqual(before,self.repo.status()["record_count"])
        self.assertEqual(TestClient(app).get("/api/v1/funds/bad/holdings-analysis").status_code,422)

    def test_tool_context_is_bounded_and_reports_omissions(self):
        self.save(stocks=[(f"{600000+i}",1) for i in range(24)])
        with patch("app.services.fund_agent_tools.fund_holdings_service",self.service):
            result = json.loads(get_fund_holdings_tool.invoke({"fund_code":"000001"}))
        self.assertEqual(len(result["disclosure"]["security_holdings"]),11)
        self.assertEqual(result["omitted_counts"]["securities"],14)
        self.assertEqual(self.service.analyze(fund_code="000001")["disclosure"]["security_count"],25)

    def test_fund_concentration_routes_to_fund_not_portfolio(self):
        router = AgentRequestRouter()
        request = AgentChatRequest(user_id="test", fund_code="000001", message="请分析单只基金000001的已披露持仓、行业集中度，再结合市场事件解释影响。")
        route = router.route(request)
        self.assertIn("get_fund_holdings_tool",route.tool_names)
        self.assertIn("search_market_events_tool",route.tool_names)
        self.assertNotIn("get_portfolio_exposure_tool",route.tool_names)
        portfolio = router.route(AgentChatRequest(user_id="test",portfolio_id="portfolio_0123456789abcdef",message="我的组合底层持仓穿透"))
        self.assertIn("get_portfolio_exposure_tool",portfolio.tool_names)
        mixed = router.route(AgentChatRequest(user_id="test",fund_code="000001",portfolio_id="portfolio_0123456789abcdef",message="组合中基金000001的底层持仓穿透"))
        self.assertIn("get_portfolio_exposure_tool",mixed.tool_names)

    def test_current_fund_holdings_and_multiple_explicit_facts(self):
        router = AgentRequestRouter()
        for message in ("分析这只基金的当前持仓", "这只基金的持仓权重是多少"):
            route = router.route(AgentChatRequest(user_id="test",fund_code="000001",message=message))
            self.assertIn("get_fund_holdings_tool",route.tool_names)
            self.assertNotIn("get_portfolio_assessment_tool",route.tool_names)
        route = router.route(AgentChatRequest(user_id="test",fund_code="000001",message="这只基金的持仓、净值表现和管理费是多少"))
        self.assertTrue({"get_fund_holdings_tool","get_fund_nav_summary_tool","get_fund_profile_tool"}.issubset(route.tool_names))

    def test_executor_binds_current_fund_instead_of_model_supplied_code(self):
        arguments = {"fund_code":"999999"}
        AgentToolExecutor()._inject_trusted_arguments(
            tool_name="get_fund_holdings_tool", tool_args=arguments,
            request=AgentChatRequest(user_id="test",fund_code="000001",message="分析持仓"),
            execution_context=AgentExecutionContext.product(user_id="test"),
        )
        self.assertEqual(arguments["fund_code"],"000001")


if __name__ == "__main__":
    unittest.main()

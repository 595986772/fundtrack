"""终端数据适配测试：验证收益口径、短历史和只读 API，而不是测试模型文案。"""

from datetime import date
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.fund_chart_data import build_fund_chart_data, chart_period_start, fund_period_summary
from tests.test_fund_returns import detail_for


def chart_detail():
    """除息后单位净值下降，但经济收益不变，适合检测前端图表是否混淆口径。"""
    detail = detail_for(values=[2.0, 1.8, 1.8], actions=[{
        "action_date": "2026-01-02", "action_type": "cash_dividend",
        "cash_dividend_per_unit": 0.2, "split_ratio": None,
    }])
    detail.update(profile={"fund_code": "110020", "short_name": "测试基金"}, source="test", data_mode="fixture")
    return detail


class FundChartDataTests(unittest.TestCase):
    def test_calendar_month_end_and_natural_days(self):
        self.assertEqual(chart_period_start(date(2024, 5, 31), "3m"), date(2024, 2, 29))
        self.assertEqual(chart_period_start(date(2026, 3, 31), "30d"), date(2026, 3, 1))

    def test_dividend_does_not_become_loss_on_return_curve(self):
        result = build_fund_chart_data(chart_detail())
        self.assertEqual([point["value"] for point in result["points"]], [1, 1, 1])
        self.assertEqual([point["nav"] for point in result["points"]], [2, 1.8, 1.8])
        self.assertEqual(result["return_basis_status"], "verified")

    def test_missing_benchmark_is_not_fabricated(self):
        result = build_fund_chart_data(chart_detail())
        self.assertEqual(result["benchmark_points"], [])
        self.assertEqual(result["benchmark_status"], "unavailable")

    def test_benchmark_compounds_and_retains_warning(self):
        result = build_fund_chart_data(chart_detail(), {
            "benchmark_status": "proxy", "benchmark_name": "测试基准",
            "warnings": ["固定利率代理"], "points": [
                {"benchmark_date": "2026-01-01", "daily_return": 0.1},
                {"benchmark_date": "2026-01-03", "daily_return": -0.1},
            ],
        })
        self.assertAlmostEqual(result["benchmark_points"][-1]["value"], 0.99)
        self.assertEqual(result["benchmark_warnings"], ["固定利率代理"])
        self.assertEqual(len(result["benchmark_points"]), 2)

    def test_insufficient_period_is_none_not_zero(self):
        result = fund_period_summary(chart_detail())
        self.assertIsNone(result["return_30d_pct"])
        self.assertIsNone(result["return_3m_pct"])

    def test_summary_uses_last_observation_before_cutoff(self):
        detail = chart_detail()
        detail["nav_points"] = [
            {"nav_date": "2026-04-30", "unit_nav": 1.0},
            {"nav_date": "2026-07-30", "unit_nav": 1.5},
            {"nav_date": "2026-08-31", "unit_nav": 1.65},
        ]
        detail["corporate_actions"] = []
        detail["corporate_action_coverage"] = {"intervals": []}
        result = fund_period_summary(detail)
        self.assertEqual(result["return_30d_pct"], 10)
        self.assertEqual(result["return_3m_pct"], 65)

    def test_api_serializes_chart_and_preserves_frozen_isolation(self):
        with patch("app.routers.funds.fund_catalog_service.get_fund", return_value=chart_detail()), patch("app.routers.funds.frozen_eval_data_enabled", return_value=True), patch("app.routers.funds.fund_benchmark_repository.get_context") as benchmark:
            response = TestClient(app).get("/api/v1/funds/110020/chart")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["points"][0]["date"], "2026-01-01")
        benchmark.assert_not_called()

    def test_api_validates_code_and_reports_unknown_fund(self):
        client = TestClient(app)
        self.assertEqual(client.get("/api/v1/funds/not-a-code/chart").status_code, 422)
        self.assertEqual(client.get("/api/v1/funds/999999/chart").status_code, 404)

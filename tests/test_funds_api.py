"""FundScope 列表和详情 API 的确定性测试。"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class FundApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_list_always_includes_three_baseline_funds(self) -> None:
        response = self.client.get("/api/v1/funds")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        fund_codes = {item["fund_code"] for item in payload["funds"]}
        # 三只 fixture 是稳定基线；用户从在线目录导入的基金可以继续追加。
        self.assertEqual(payload["count"], len(payload["funds"]))
        self.assertTrue(
            {"110020", "161725", "110007"}.issubset(fund_codes),
        )
        # 未同步时至少是冻结基线日期；执行每日同步后允许自然向后推进。
        self.assertGreaterEqual(payload["snapshot_end"], "2026-08-25")
        self.assertIn(payload["data_mode"], {"fixture", "database"})

    def test_detail_returns_sorted_nav_points(self) -> None:
        response = self.client.get("/api/v1/funds/110020")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        nav_points = payload["nav_points"]
        self.assertEqual(payload["profile"]["fund_code"], "110020")
        self.assertEqual(payload["latest_nav_date"], nav_points[-1]["nav_date"])
        self.assertIn("corporate_actions", payload)
        self.assertIn("intervals", payload["corporate_action_coverage"])
        self.assertGreater(len(nav_points), 500)
        self.assertEqual(
            [item["nav_date"] for item in nav_points],
            sorted(item["nav_date"] for item in nav_points),
        )

    def test_unknown_fund_returns_not_found(self) -> None:
        response = self.client.get("/api/v1/funds/999999")

        self.assertEqual(response.status_code, 404)
        self.assertIn("尚未导入", response.json()["detail"])

    def test_search_endpoint_returns_import_state(self) -> None:
        fake_result = {
            "query": "华夏",
            "count": 1,
            "source": "Fake Fund Directory",
            "data_mode": "live",
            "catalog_updated_at": "2026-08-29T00:00:00Z",
            "message": "",
            "funds": [
                {
                    "fund_code": "000001",
                    "short_name": "华夏成长混合",
                    "fund_type": "混合型-灵活",
                    "source_url": "https://fund.eastmoney.com/000001.html",
                    "import_status": "discovered",
                    "is_imported": False,
                }
            ],
        }
        with patch(
            "app.routers.funds.fund_directory_service.search",
            return_value=fake_result,
        ):
            response = self.client.get("/api/v1/funds/search?q=华夏")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["funds"][0]["is_imported"])

    def test_risk_endpoint_returns_deterministic_metrics(self) -> None:
        response = self.client.get("/api/v1/funds/161725/risk?months=12")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["fund_code"], "161725")
        self.assertEqual(payload["period_months"], 12)
        self.assertGreaterEqual(payload["period_end"], "2026-08-25")
        self.assertGreater(payload["max_drawdown"]["drawdown_pct"], 0)
        self.assertGreater(payload["annualized_volatility_pct"], 0)
        self.assertIn(
            payload["return_basis"],
            {
                "total_return_reinvested",
                "provider_reported_daily_return",
                "unit_nav_unadjusted",
            },
        )
        self.assertIn(payload["return_basis_status"], {"verified", "provider_reported", "limited"})
        self.assertIn("252", payload["methodology"])

    def test_risk_endpoint_rejects_unsupported_period(self) -> None:
        response = self.client.get("/api/v1/funds/161725/risk?months=7")

        self.assertEqual(response.status_code, 422)

    def test_home_page_exposes_fund_workspace(self) -> None:
        """首页应提供新版研究工作区，同时保留基金与 Agent 业务节点。"""
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="fund-shell"', response.text)
        self.assertIn('id="agent-fund-context"', response.text)
        # 页面已收敛为五个产品入口，内部基金和组合节点仍保留。
        for destination in ("assets", "intelligence", "community", "sectors", "research"):
            self.assertIn(f'data-product-view="{destination}"', response.text)
        self.assertIn('id="overview-dashboard"', response.text)
        self.assertIn('id="portfolio-workbench"', response.text)
        self.assertIn('id="workspace-outline"', response.text)
        self.assertIn('class="mobile-product-nav"', response.text)
        self.assertIn("/static/design-system.css?v=product", response.text)
        self.assertIn("/static/styles.css?v=product", response.text)
        self.assertIn("/static/app.js?v=product", response.text)
        self.assertIn("/static/holdings-quotes.js?v=", response.text)

    def test_frontend_sends_selected_fund_to_agent(self) -> None:
        """基金分析按钮和请求体必须共用当前选中的基金代码。"""
        response = self.client.get("/static/app.js")

        self.assertEqual(response.status_code, 200)
        self.assertIn("data-analyze-fund", response.text)
        # 指数研究不能意外夹带侧栏中之前选中的基金，单基金仍传当前代码。
        self.assertIn('fund_code: state.agentContextMode === "market" ? null : state.selectedFundCode', response.text)

    def test_frontend_fund_chart_supports_short_periods(self) -> None:
        """单基金净值图应支持精确的近 30 日和近 3 月区间。"""
        script = self.client.get("/static/app.js")
        styles = self.client.get("/static/styles.css")

        self.assertEqual(script.status_code, 200)
        self.assertIn('fundPeriodButton("30d", "近 30 日")', script.text)
        self.assertIn('fundPeriodButton("3m", "近 3 月")', script.text)
        self.assertIn("FundScopeTerminalData.cutoffDate", script.text)
        self.assertIn(".fund-chart-header .period-control", styles.text)

    def test_agent_chat_renders_and_tracks_portfolio_research_jobs(self) -> None:
        """聊天消息应从持久化元数据恢复 Job，并通过 SSE 渲染八阶段进度。"""

        script = self.client.get("/static/app.js")
        styles = self.client.get("/static/styles.css")

        self.assertEqual(script.status_code, 200)
        self.assertIn("portfolio_research_jobs", script.text)
        self.assertIn("watchChatPortfolioResearchJob", script.text)
        self.assertIn("new EventSource", script.text)
        self.assertIn("renderMessagePortfolioResearch", script.text)
        self.assertIn("renderResearchSnapshotAudit", script.text)
        self.assertIn("研究输入已冻结", script.text)
        self.assertIn("loadPortfolioResearchHistory", script.text)
        self.assertIn("comparePortfolioResearch", script.text)
        self.assertIn("renderPortfolioResearchComparison", script.text)
        self.assertIn("与上次对比", script.text)
        self.assertIn("AI 综合", script.text)
        self.assertIn(".message-research-job", styles.text)
        self.assertIn(".message-research-steps", styles.text)
        self.assertIn(".research-snapshot-audit", styles.text)
        self.assertIn(".portfolio-research-comparison", styles.text)
        self.assertIn(".portfolio-research-change-facts", styles.text)

    def test_frontend_can_start_and_observe_fund_data_sync(self) -> None:
        """同步按钮应创建 Background Job，并等待持久化状态完成后刷新页面。"""

        page = self.client.get("/")
        script = self.client.get("/static/app.js")

        self.assertIn('id="sync-fund-data"', page.text)
        self.assertIn('fundDataSync: "/api/v1/funds/data-sync"', script.text)
        self.assertIn("waitForBackgroundJob", script.text)
        self.assertIn("completedJob.result?.summary", script.text)

    def test_frontend_searches_and_imports_arbitrary_funds(self) -> None:
        page = self.client.get("/")
        script = self.client.get("/static/app.js")

        self.assertIn('id="fund-search-results"', page.text)
        self.assertIn('fundSearch: "/api/v1/funds/search"', script.text)
        self.assertIn('fundImport: "/api/v1/funds/import"', script.text)
        self.assertIn("importFundFromDirectory", script.text)

    def test_frontend_sanitizes_markdown_and_renders_evidence_cards(self) -> None:
        page = self.client.get("/")
        script = self.client.get("/static/app.js")

        self.assertIn("dompurify@3.2.6", page.text)
        self.assertIn("marked@15.0.12", page.text)
        self.assertIn("window.DOMPurify.sanitize", script.text)
        self.assertIn("renderMessageEvidence", script.text)

    def test_business_and_developer_experiences_are_separate(self) -> None:
        home = self.client.get("/")
        developer = self.client.get("/developer")
        script = self.client.get("/static/app.js")

        self.assertEqual(developer.status_code, 200)
        self.assertIn("data-developer-only", home.text)
        self.assertIn('window.location.pathname === "/developer"', script.text)
        self.assertIn("developerAgentChatStream", script.text)
        self.assertIn("分析当前基金的主要风险与支撑因素", script.text)
        self.assertIn("state.developerMode", script.text)

    def test_fund_data_status_endpoint_reports_pipeline_state(self) -> None:
        response = self.client.get("/api/v1/funds/data-status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn(payload["data_mode"], {"fixture", "database"})
        self.assertEqual(payload["schedule_id"], "fund-nav-daily-sync")
        self.assertIn("sync_cron", payload)

    def test_frontend_renders_risk_panel_and_period_endpoint(self) -> None:
        """基金详情页应调用风险接口，并提供 6/12/36 个月切换控件。"""
        response = self.client.get("/static/app.js")

        self.assertEqual(response.status_code, 200)
        self.assertIn("/risk?months=${months}", response.text)
        self.assertIn('class="fund-risk-section"', response.text)
        self.assertIn("data-fund-risk-period", response.text)
        self.assertIn("最大回撤", response.text)
        self.assertIn("risk.return_basis_label", response.text)
        self.assertIn("风险调整后表现", response.text)
        self.assertIn("夏普比率", response.text)
        self.assertIn("Sortino 比率", response.text)
        self.assertIn("benchmark.excess_return_pct", response.text)

    def test_frontend_renders_deterministic_assessment(self) -> None:
        """详情页应读取综合研判接口并展示三组可追溯证据。"""
        response = self.client.get("/static/app.js")

        self.assertEqual(response.status_code, 200)
        self.assertIn("/assessment?months=${months}", response.text)
        self.assertIn('class="fund-assessment-section"', response.text)
        self.assertIn("主要风险", response.text)
        self.assertIn("支撑因素", response.text)
        self.assertIn("evidence.evidence_id", response.text)

    def test_frontend_supports_portfolio_editor_and_agent_context(self) -> None:
        """组合工作台应支持真实持仓账本、旧权重兼容和 Agent 上下文。"""
        response = self.client.get("/static/app.js")

        self.assertEqual(response.status_code, 200)
        self.assertIn("data-portfolio-weight", response.text)
        self.assertIn("data-position-units", response.text)
        self.assertIn("data-portfolio-transaction-form", response.text)
        self.assertIn("renderPortfolioTransactions", response.text)
        self.assertIn("portfolioTransactionRetry", response.text)
        self.assertIn("payload.idempotency_key = idempotencyKey", response.text)
        self.assertIn("本次没有重复增加持仓", response.text)
        self.assertIn("loadPortfolioAssessment", response.text)
        self.assertIn('state.agentContextMode === "portfolio"', response.text)
        self.assertIn("交给 Agent 解读", response.text)

    def test_frontend_exposes_portfolio_monitoring_workflow(self) -> None:
        """组合页应能保存策略、运行监控并从预警重新发起研究。"""

        response = self.client.get("/static/app.js")

        self.assertEqual(response.status_code, 200)
        self.assertIn("data-portfolio-monitoring-form", response.text)
        self.assertIn("data-run-portfolio-monitor", response.text)
        self.assertIn("data-acknowledge-alert", response.text)
        self.assertIn("data-monitor-research", response.text)

    def test_frontend_renders_holding_level_event_impacts(self) -> None:
        """组合页应展示消息关联仓位、细分行业和独立刷新入口。"""

        response = self.client.get("/static/app.js")

        self.assertEqual(response.status_code, 200)
        self.assertIn("/event-impacts?${params}", response.text)
        self.assertIn("renderPortfolioEventImpacts", response.text)
        self.assertIn("哪些消息影响了我", response.text)
        self.assertIn("item.affected_exposure_pct", response.text)
        self.assertIn("data-reload-portfolio-event-impacts", response.text)

    def test_frontend_renders_validated_portfolio_scenarios(self) -> None:
        """调整结果应区分模型候选和 Python 同快照场景体检。"""

        script = self.client.get("/static/app.js")
        styles = self.client.get("/static/styles.css")

        self.assertEqual(script.status_code, 200)
        self.assertIn("renderPortfolioScenarioValidation", script.text)
        self.assertIn("同快照场景体检", script.text)
        self.assertIn("scenarioValidation.status", script.text)
        self.assertIn("费用与赎回限制：信息不足", script.text)
        self.assertIn("5 阶段 · 人工确认", script.text)
        self.assertIn('name="max_single_fund_weight_pct"', script.text)
        self.assertIn('name="max_turnover_pct"', script.text)
        self.assertIn(".portfolio-scenario-comparison", styles.text)
        self.assertIn(".portfolio-scenario-table", styles.text)


if __name__ == "__main__":
    unittest.main()

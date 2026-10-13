"""市场环境 REST API 与前端入口契约测试。"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class MarketApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_sentiment_endpoint_returns_structured_evidence(self) -> None:
        response = self.client.get("/api/v1/market/sentiment")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreaterEqual(payload["data_date"], "2026-08-25")
        self.assertIn(payload["data_mode"], {"fixture", "database"})
        self.assertTrue(payload["state_label"])
        self.assertEqual(len(payload["signals"]), 4)
        self.assertEqual(len(payload["sources"]), 3)
        self.assertIn("return_30d_pct", payload["trend"])
        self.assertIn("return_3m_pct", payload["trend"])
        self.assertIn("不构成投资建议", payload["disclaimer"])

    def test_api_index_exposes_market_sentiment(self) -> None:
        response = self.client.get("/api")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["market_sentiment"],
            "/api/v1/market/sentiment",
        )
        self.assertEqual(
            response.json()["market_events"],
            "/api/v1/market/events",
        )
        self.assertEqual(
            response.json()["market_hotspots"],
            "/api/v1/market/hotspots",
        )
        self.assertEqual(
            response.json()["market_indices"],
            "/api/v1/market/indices",
        )

    def test_market_indices_endpoint_and_frontend_contract(self) -> None:
        response = self.client.get("/api/v1/market/indices")
        script = self.client.get("/static/app.js")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["data_kind"], "market_index_comparison")
        self.assertIn(payload["data_mode"], {"database", "unavailable"})
        self.assertIn("renderMarketIndexOverview", script.text)
        self.assertIn("宽基指数分区", script.text)
        self.assertIn("selectedMarketIndexCode", script.text)
        self.assertIn("return_30d_pct", script.text)
        self.assertIn("return_3m_pct", script.text)
        self.assertIn("近 3 个月", script.text)
        if payload["data_mode"] == "database":
            # 旧数据库可能尚未手动同步新增指数，响应数量应以实际可用历史为准。
            self.assertEqual(len(payload["indices"]), payload["index_count"])
            self.assertGreater(payload["index_count"], 0)
            self.assertTrue(
                all("return_30d_pct" in item for item in payload["indices"])
            )
            self.assertTrue(
                all("return_3m_pct" in item for item in payload["indices"])
            )

    def test_events_endpoint_filters_by_fund_and_keyword(self) -> None:
        response = self.client.get(
            "/api/v1/market/events",
            params={"fund_code": "161725", "q": "白酒", "limit": 3},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreaterEqual(payload["count"], 1)
        self.assertLessEqual(payload["count"], 3)
        self.assertGreaterEqual(payload["total_matched"], 1)
        for event in payload["events"]:
            self.assertIn("161725", event["affected_fund_codes"])
            self.assertTrue(event["source_url"].startswith("https://"))
            self.assertTrue(event["content_hash"].startswith("sha256:"))
            self.assertIn(
                event["freshness_status"],
                {"fresh", "decaying", "expired"},
            )
            self.assertGreaterEqual(event["decay_factor"], 0)
            self.assertLessEqual(event["decay_factor"], 1)

    def test_events_endpoint_validates_date_range(self) -> None:
        response = self.client.get(
            "/api/v1/market/events",
            params={"date_from": "2026-08-20", "date_to": "2026-08-01"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("date_from", response.json()["detail"])

    def test_frontend_contains_market_environment_surface(self) -> None:
        page = self.client.get("/")
        script = self.client.get("/static/app.js")

        self.assertIn('id="market-sentiment"', page.text)
        self.assertIn("/api/v1/market/sentiment", script.text)
        self.assertIn("renderMarketSentiment", script.text)
        self.assertIn('id="market-events"', page.text)
        self.assertIn("/api/v1/market/events", script.text)
        self.assertIn("renderMarketEvents", script.text)
        self.assertIn('id="market-hotspots"', page.text)
        self.assertIn("/api/v1/market/hotspots", script.text)
        self.assertIn("renderMarketHotspots", script.text)

    def test_hotspot_endpoint_returns_stable_tracking_contract(self) -> None:
        response = self.client.get(
            "/api/v1/market/hotspots",
            params={"history_days": 30, "limit": 5},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["data_kind"], "market_hotspot_tracking")
        self.assertIn(payload["data_mode"], {"database", "unavailable"})
        self.assertIn("lifecycle_counts", payload)
        self.assertIn("hotspots", payload)
        self.assertIn("热点", payload["disclaimer"])

    def test_large_capital_api_and_frontend_contract(self) -> None:
        overview = self.client.get(
            "/api/v1/market/large-capital",
            params={"user_id": "chen", "fund_code": "161725"},
        )
        status = self.client.get("/api/v1/market/large-capital/status")
        api_index = self.client.get("/api")
        page = self.client.get("/")
        script = self.client.get("/static/app.js")

        self.assertEqual(overview.status_code, 200)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(overview.json()["data_kind"], "large_capital_signals")
        self.assertIn(overview.json()["data_mode"], {"database", "unavailable"})
        self.assertIn("identity_boundary", overview.json())
        self.assertIn("sector_analysis", overview.json())
        self.assertEqual(set(overview.json()["sector_analysis"]["windows"]), {"1d", "5d", "20d"})
        self.assertEqual(
            api_index.json()["large_capital"],
            "/api/v1/market/large-capital",
        )
        self.assertIn('id="large-capital-signals"', page.text)
        self.assertIn('id="sync-large-capital"', page.text)
        # 新首页改用终端页签承载资金摘要，完整市场面板与同步入口继续保留。
        terminal = self.client.get("/static/terminal.js")
        self.assertIn('id="overview-dashboard"', page.text)
        # 资金证据迁入板块工作区；基金详情不再复制市场资金表。
        self.assertNotIn('view.tab === "capital"', terminal.text)
        self.assertIn('id="sector-radar"', page.text)
        self.assertIn(
            'largeCapital: "/api/v1/market/large-capital"',
            script.text,
        )
        self.assertIn("loadLargeCapitalSignals", script.text)
        self.assertIn("renderLargeCapitalSignals", script.text)
        self.assertIn("renderCapitalSectorAnalysis", script.text)
        self.assertIn("formatCapitalFlow", script.text)

    def test_market_data_status_and_frontend_sync_contract(self) -> None:
        status = self.client.get("/api/v1/market/data-status")
        page = self.client.get("/static/app.js")

        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["schedule_id"], "market-snapshot-daily-sync")
        self.assertIn('marketDataSync: "/api/v1/market/data-sync"', page.text)
        self.assertIn("data-sync-market", page.text)
        self.assertIn("syncMarketData", page.text)

    def test_market_event_status_and_frontend_sync_contract(self) -> None:
        status = self.client.get("/api/v1/market/events/data-status")
        api_index = self.client.get("/api")
        page = self.client.get("/")
        script = self.client.get("/static/app.js")

        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["schedule_id"], "market-event-daily-sync")
        self.assertEqual(
            api_index.json()["market_intelligence_pipeline"],
            "/api/v1/market/events/pipeline",
        )
        self.assertIn('id="sync-market-events"', page.text)
        self.assertIn('id="market-pipeline-status"', page.text)
        self.assertIn('aria-label="获取最新市场情报"', page.text)
        self.assertIn('aria-label="重新读取已保存的市场事件"', page.text)
        self.assertIn(
            'marketEventDataSync: "/api/v1/market/events/data-sync"',
            script.text,
        )
        self.assertIn(
            'marketIntelligencePipeline: "/api/v1/market/events/pipeline"',
            script.text,
        )
        self.assertIn("syncMarketEvents", script.text)
        self.assertIn("renderMarketPipelineStatus", script.text)
        self.assertIn("更新于 ${formatDateTime(result.data_updated_at)}", script.text)

    def test_market_topic_catalog_and_frontend_collection_scope(self) -> None:
        catalog = self.client.get("/api/v1/market/events/topics")
        page = self.client.get("/")
        script = self.client.get("/static/app.js")

        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(catalog.json()["max_selected_topics"], 3)
        self.assertIn(
            "半导体",
            [item["topic"] for item in catalog.json()["topics"]],
        )
        self.assertIn('id="market-topic-config"', page.text)
        self.assertIn('id="market-topic-popover"', page.text)
        self.assertIn(
            'marketEventTopics: "/api/v1/market/events/topics"',
            script.text,
        )
        self.assertIn("use_model_topic_expansion", script.text)
        self.assertIn("marketSelectedTopics", script.text)

        invalid = self.client.post(
            "/api/v1/market/events/pipeline",
            json={
                "user_id": "chen",
                "topics": ["半导体", "新能源", "医药", "消费"],
            },
        )
        self.assertEqual(invalid.status_code, 422)

    def test_market_event_analysis_frontend_contract(self) -> None:
        api_index = self.client.get("/api")
        page = self.client.get("/")
        script = self.client.get("/static/app.js")

        self.assertEqual(api_index.status_code, 200)
        self.assertEqual(
            api_index.json()["market_event_analysis"],
            "/api/v1/market/events/analyze",
        )
        self.assertIn('id="analyze-market-events"', page.text)
        self.assertIn(
            'marketEventAnalysis: "/api/v1/market/events/analyze"',
            script.text,
        )
        self.assertIn("analyzeMarketEvents", script.text)
        self.assertIn("formatEventFreshness", script.text)

    def test_market_event_clustering_frontend_contract(self) -> None:
        api_index = self.client.get("/api")
        page = self.client.get("/")
        script = self.client.get("/static/app.js")

        self.assertEqual(api_index.status_code, 200)
        self.assertEqual(
            api_index.json()["market_event_clustering"],
            "/api/v1/market/events/cluster",
        )
        self.assertIn('id="cluster-market-events"', page.text)
        self.assertIn(
            'marketEventClustering: "/api/v1/market/events/cluster"',
            script.text,
        )
        self.assertIn("clusterMarketEvents", script.text)
        self.assertIn("heat_score", script.text)

    def test_social_signals_api_and_frontend_contract(self) -> None:
        status = self.client.get("/api/v1/market/social-signals/status")
        overview = self.client.get("/api/v1/market/social-signals")
        api_index = self.client.get("/api")
        page = self.client.get("/")
        script = self.client.get("/static/app.js")

        self.assertEqual(status.status_code, 200)
        self.assertIn("configured", status.json())
        self.assertEqual(status.json()["operation_window"]["cutoff_time"], "15:00")
        self.assertEqual(overview.status_code, 200)
        self.assertIn("sample_count", overview.json())
        self.assertIn("accepted_sample_count", overview.json())
        self.assertEqual(
            overview.json()["data_kind"],
            "social_investment_signals",
        )
        self.assertEqual(
            api_index.json()["social_signals"],
            "/api/v1/market/social-signals",
        )
        self.assertIn('id="social-signals"', page.text)
        self.assertIn('id="connect-xhs-session"', page.text)
        self.assertIn('id="sync-social-signals"', page.text)
        self.assertIn("社区话题", page.text)
        self.assertIn('data-workspace-page="social-posts"', page.text)
        self.assertIn(
            'socialSignals: "/api/v1/market/social-signals"',
            script.text,
        )
        self.assertIn(
            'socialSignalBrowserSession: "/api/v1/market/social-signals/browser-session"',
            script.text,
        )
        self.assertIn("connectXhsSession", script.text)
        self.assertIn("小红书已经连接，可直接点击右侧按钮刷新社区观点", script.text)
        self.assertIn("renderSocialSignals", script.text)
        self.assertIn("renderSocialSignalSamples", script.text)
        self.assertIn("最近采集样本", script.text)
        self.assertIn("当前窗口已保存", script.text)
        self.assertIn("social-signals/analysis", script.text)
        self.assertIn("renderSocialOperationWindow", script.text)
        self.assertIn("social_positioning", script.text)

    def test_social_signal_browser_login_job_has_valid_public_schema(self) -> None:
        """登录 Job 已启动后，REST 响应不能因漏写 job_type 而返回 500。"""

        fake_started_job = {
            "ok": True,
            "accepted": True,
            "message": "登录任务已提交",
            "job": {
                "id": "xhs-login-test",
                "user_id": "chen",
                "title": "连接 FundScope 小红书公开页面",
                "job_type": "social_signal_browser_login",
                "status": "running",
                "execution_backend": "local",
                "current_step": 1,
                "total_steps": 2,
                "progress_percent": 50.0,
                "progress_message": "正在等待用户完成小红书登录。",
                "result": None,
                "error": None,
                "cancel_requested": False,
                "attempt_count": 1,
                "max_retries": 0,
                "timeout_seconds": 360,
                "idempotency_key": None,
                "celery_task_id": "local-xhs-login-test",
                "created_at": "2026-09-03T13:20:11+00:00",
                "updated_at": "2026-09-03T13:20:12+00:00",
                "started_at": "2026-09-03T13:20:11+00:00",
                "completed_at": None,
            },
        }
        with patch(
            "app.routers.market.background_job_service.start_job",
            return_value=fake_started_job,
        ):
            response = self.client.post(
                "/api/v1/market/social-signals/browser-session",
                json={"user_id": "chen"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["job"]["job_type"],
            "social_signal_browser_login",
        )

    def test_social_collection_does_not_request_automatic_job_retries(self) -> None:
        # 在创建边界拦截，不启动真实采集线程，也不消耗用户的平台会话。
        with patch("app.routers.market.background_job_service.start_job", side_effect=RuntimeError("test offline")) as start:
            response = self.client.post("/api/v1/market/social-signals/data-sync", json={"user_id": "chen"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(start.call_args.kwargs["max_retries"], 0)

    def test_saved_analysis_creates_separate_non_browser_job(self) -> None:
        with patch("app.routers.market.background_job_service.start_job", side_effect=RuntimeError("test offline")) as start:
            response = self.client.post("/api/v1/market/social-signals/analysis", json={"user_id": "chen"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(start.call_args.kwargs["job_type"], "social_signal_analysis")
        self.assertEqual(start.call_args.kwargs["max_retries"], 0)


if __name__ == "__main__":
    unittest.main()

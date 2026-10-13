"""小程序兼容路由不改变可信身份；所有服务用替身，不联网也不改真实账本。"""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.auth import CurrentPrincipal, get_current_principal
from app.services.portfolio_decisions import PortfolioDecisionError


class MiniProgramWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.dependency_overrides)
        app.dependency_overrides[get_current_principal] = lambda: CurrentPrincipal(
            user_id="alice", display_name="Alice", role="user", authenticated=True
        )
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.previous)

    def test_native_review_alias_uses_same_service_and_trusted_user(self):
        path = "/api/v1/portfolios/portfolio_1234567890abcdef/decisions/decision_1234567890abcdef"
        with patch("app.routers.portfolios.portfolio_decision_service.review", side_effect=PortfolioDecisionError("测试记录不存在")) as service:
            for method, suffix in (("patch", ""), ("post", "/review")):
                response = getattr(self.client, method)(path + suffix, json={"user_id": "bob", "review_note": "核对历史假设"})
                self.assertEqual(response.status_code, 404)
                self.assertEqual(service.call_args.kwargs["user_id"], "alice")
                self.assertEqual(service.call_args.kwargs["review_note"], "核对历史假设")

    def test_market_jobs_forward_idempotency_without_changing_user(self):
        paths = ["events/pipeline", "events/analyze", "events/cluster", "social-signals/data-sync", "social-signals/analysis", "social-signals/browser-session", "large-capital/data-sync"]
        with patch("app.routers.market.background_job_service.start_job", side_effect=ValueError("test stop before job creation")) as start:
            for path in paths:
                response = self.client.post(f"/api/v1/market/{path}", json={"user_id": "bob", "idempotency_key": "mini-safe-retry-001"})
                self.assertEqual(response.status_code, 400, path)
                self.assertEqual(start.call_args.kwargs["user_id"], "alice")
                self.assertEqual(start.call_args.kwargs["idempotency_key"], "mini-safe-retry-001")

    def test_portfolio_research_job_keeps_idempotency(self):
        with patch("app.routers.portfolios.portfolio_service.get", return_value={"name": "测试组合"}), patch("app.routers.portfolios.background_job_service.start_job", side_effect=ValueError("test stop")) as start:
            response = self.client.post("/api/v1/portfolios/portfolio_1234567890abcdef/research-runs", json={"user_id": "bob", "idempotency_key": "mini-research-retry-001"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(start.call_args.kwargs["user_id"], "alice")
            self.assertEqual(start.call_args.kwargs["idempotency_key"], "mini-research-retry-001")


if __name__ == "__main__":
    unittest.main()

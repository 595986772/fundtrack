"""小程序和网页共用 Session；所有凭证、交易都只进入临时测试数据库。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.database import Database
from app.main import app
from app.services.auth import AuthService
from app.services.portfolios import PortfolioService


class MiniProgramAuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.database = Database(f"sqlite:///{Path(self.directory.name) / 'mini.db'}")
        self.auth = AuthService(self.database)
        self.portfolios = PortfolioService(self.database)
        self.auth.register(user_id="alice", display_name="Alice", password="test-password-alice")
        _, self.bob_token = self.auth.register(user_id="bob", display_name="Bob", password="test-password-bob")
        self.patches = [
            patch("app.services.auth.auth_service", self.auth),
            patch("app.routers.auth.auth_service", self.auth),
            patch("app.routers.portfolios.portfolio_service", self.portfolios),
            patch.object(settings, "auth_required", True),
        ]
        for item in self.patches:
            item.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.database.engine.dispose()
        self.directory.cleanup()

    def login(self):
        response = self.client.post("/api/v1/auth/token", json={"user_id": "alice", "password": "test-password-alice"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(response.json()["token_type"], "bearer")
        return response.json()["access_token"]

    def test_token_auth_overrides_spoofed_user_and_reuses_existing_ledger(self):
        token = self.login()
        header = {"Authorization": f"Bearer {token}"}
        response = self.client.post("/api/v1/portfolios", headers=header, json={
            "user_id": "bob", "name": "Mobile ledger",
            "holdings": [{"fund_code": "110020", "units": 100, "cost_amount": 200, "purchase_date": "2025-01-02"}],
        })
        self.assertEqual(response.status_code, 201, response.text)
        portfolio = response.json()
        self.assertEqual(portfolio["user_id"], "alice")
        self.assertEqual(portfolio["position_mode"], "actual")
        self.assertEqual(portfolio["total_cost_amount"], 200)
        # 换成 Bob 的令牌，不能读取 Alice 的真实账本。
        denied = self.client.get(f"/api/v1/portfolios/{portfolio['id']}?user_id=alice", headers={"Authorization": f"Bearer {self.bob_token}"})
        self.assertEqual(denied.status_code, 404)
        trade = {"user_id": "bob", "idempotency_key": "mini-test-buy-0001", "fund_code": "110020", "transaction_type": "buy", "trade_date": "2025-01-03", "units": 10, "unit_price": 2, "fee": 1}
        url = f"/api/v1/portfolios/{portfolio['id']}/transactions"
        first = self.client.post(url, headers=header, json=trade)
        second = self.client.post(url, headers=header, json=trade)
        self.assertIn(first.status_code, (200, 201), first.text)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertTrue(second.json()["idempotency_replayed"])
        current = self.client.get(f"/api/v1/portfolios/{portfolio['id']}", headers=header).json()
        self.assertEqual(current["holdings"][0]["units"], 110)
        self.assertEqual(current["total_cost_amount"], 221)

    def test_logout_revokes_token(self):
        header = {"Authorization": f"Bearer {self.login()}"}
        self.assertTrue(self.client.get("/api/v1/auth/me", headers=header).json()["authenticated"])
        self.assertEqual(self.client.post("/api/v1/auth/logout", headers=header).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/portfolios", headers=header).status_code, 401)

    def test_invalid_explicit_bearer_never_falls_back_to_cookie_or_legacy(self):
        self.client.cookies.set(settings.auth_cookie_name, self.bob_token)
        with patch.object(settings, "auth_required", False):
            for value in ("Bearer revoked-token", "Basic abcd", "Bearer ", "Bearer a b"):
                response = self.client.get("/api/v1/portfolios?user_id=alice", headers={"Authorization": value})
                self.assertEqual(response.status_code, 401)

    def test_explicit_token_has_priority_over_browser_cookie(self):
        self.client.cookies.set(settings.auth_cookie_name, self.bob_token)
        result = self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {self.login()}"})
        self.assertEqual(result.json()["user"]["user_id"], "alice")

    def test_existing_cookie_login_still_works(self):
        response = self.client.post("/api/v1/auth/login", json={"user_id": "alice", "password": "test-password-alice"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("httponly", response.headers["set-cookie"].lower())
        self.assertEqual(self.client.get("/api/v1/auth/me").json()["user"]["user_id"], "alice")

    def test_bad_password_returns_401_without_token(self):
        response = self.client.post("/api/v1/auth/token", json={"user_id": "alice", "password": "wrong"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("access_token", response.json())

    def test_preview_and_desktop_are_separate(self):
        self.assertIn("Agent", self.client.get("/").text)
        self.assertIn("非微信模拟器", self.client.get("/mini").text)


if __name__ == "__main__":
    unittest.main()

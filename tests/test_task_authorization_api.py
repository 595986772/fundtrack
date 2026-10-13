"""授权控制面只允许当前登录用户查看/撤销自己的授权，不能自行授予能力。"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.auth import CurrentPrincipal, get_current_principal
from tests.task_authorization_fixture import AuthorizationFixture, A


class AuthorizationApiTests(unittest.TestCase):
    def setUp(self):
        self.fx = AuthorizationFixture()
        self.addCleanup(self.fx.close)
        self.scope = self.fx.grant()
        self.client = TestClient(app)
        self.service_patch = patch("app.routers.agent_authorizations.get_task_authorization_service", return_value=self.fx.service)
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)
        self.previous_overrides = dict(app.dependency_overrides)
        self.addCleanup(self.restore_overrides)
        self.login_as("chen")

    def restore_overrides(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.previous_overrides)

    def login_as(self, user_id):
        app.dependency_overrides[get_current_principal] = lambda: CurrentPrincipal(
            user_id=user_id, display_name=user_id, role="user", authenticated=True)

    def test_list_and_detail_only_return_my_scopes(self):
        result = self.client.get("/api/v1/agent/authorizations?user_id=alice")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["grants"][0]["portfolio_ids"], [A])
        detail = self.client.get(f"/api/v1/agent/authorizations/{self.scope.task_grant_id}")
        self.assertTrue(detail.json()["effective"])
        self.assertGreater(len(detail.json()["audit"]), 0)

    def test_other_user_cannot_read_or_revoke_by_guessing_id(self):
        self.login_as("alice")
        path = f"/api/v1/agent/authorizations/{self.scope.task_grant_id}"
        self.assertEqual(self.client.get(path + "?user_id=chen").status_code, 404)
        self.assertEqual(self.client.post(path + "/revoke", json={"user_id": "chen"}).status_code, 404)
        self.fx.service.check_context(self.scope)

    def test_revoke_is_idempotent_and_does_not_delete_audit(self):
        path = f"/api/v1/agent/authorizations/{self.scope.task_grant_id}"
        for _ in range(2):
            result = self.client.post(path + "/revoke", json={"user_id": "chen"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["status"], "revoked")
        self.assertFalse(self.client.get(path).json()["effective"])
        self.assertTrue(any(row["action"] == "grant:revoke" for row in self.client.get(path).json()["audit"]))

    def test_no_public_endpoint_can_mint_arbitrary_grants(self):
        result = self.client.post("/api/v1/agent/authorizations", json={"portfolio_ids": [A], "capabilities": ["developer:tools"]})
        self.assertEqual(result.status_code, 405)


if __name__ == "__main__":
    unittest.main()

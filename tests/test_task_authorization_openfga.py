"""真实 OpenFGA 端到端验证：仅本机权限 HTTP，不运行模型、不接触真实持仓。"""

import os
import unittest

from app.services.agent_authorization import AgentAuthorizationError, _configured_policy
from app.services.openfga_authorization import OpenFGAPolicy
from tests.task_authorization_fixture import AuthorizationFixture, A, B


@unittest.skipUnless(os.getenv("RUN_OPENFGA_INTEGRATION") == "1", "需要已启动并初始化的本机 OpenFGA")
class RealOpenFGAIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fx = AuthorizationFixture()
        self.policy = _configured_policy()
        self.fx.service._policy = self.policy

    def tearDown(self):
        # 集成测试使用随机 grant ID 与临时业务数据库，清理它们自己的关系，不碰用户授权。
        for row in self.fx.service.list(user_id="chen"):
            self.policy.revoke(row["id"], row["portfolio_ids"], ["portfolio:read", "portfolio:research-run"])
        self.fx.close()

    def test_real_resource_check_delegation_and_revoke(self):
        scope = self.fx.grant()
        self.assertTrue(self.policy.check(scope.task_grant_id, "portfolio:read", A))
        self.assertFalse(self.policy.check(scope.task_grant_id, "portfolio:read", B))
        self.fx.service.authorize(scope, action="portfolio:read", portfolio_id=A)
        with self.assertRaises(AgentAuthorizationError):
            self.fx.service.authorize(scope, action="portfolio:read", portfolio_id=B)
        child = self.fx.service.create(scope, run_id="child-real", agent_id="risk", portfolio_ids=[A], capabilities=["portfolio:read"])
        self.fx.service.authorize(child, action="portfolio:read", portfolio_id=A)
        self.fx.service.revoke(user_id="chen", grant_id=scope.task_grant_id)
        # 撤销由 SQL 生命周期立即控制；即使关系清理稍后做，也不能继续使用子授权。
        with self.assertRaises(AgentAuthorizationError):
            self.fx.service.authorize(child, action="portfolio:read", portfolio_id=A)

    def test_real_client_connection_failure_fails_closed(self):
        scope = self.fx.grant()
        self.fx.service._policy = OpenFGAPolicy("http://127.0.0.1:1", self.policy.store_id, self.policy.model_id, timeout_seconds=0.2)
        with self.assertRaises(AgentAuthorizationError) as error:
            self.fx.service.authorize(scope, action="portfolio:read", portfolio_id=A)
        self.assertEqual(error.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()

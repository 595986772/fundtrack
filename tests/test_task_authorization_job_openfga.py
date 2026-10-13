"""显式开启的真实 OpenFGA Job 测试：临时 SQL、假工作流、禁止真实模型调用。

本机 FGA 已初始化后执行：
    RUN_OPENFGA_INTEGRATION=1 python -m unittest tests.test_task_authorization_job_openfga -v

只创建随机 grant 的关系，结束后删除这些测试关系，不修改用户持仓或授权。
"""

from contextlib import ExitStack
import os
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from app.config import settings
from app.services import agent_authorization
from app.services.agent_authorization import AgentAuthorizationError
from app.services.agent_execution_context import current_agent_execution_context
from app.services.ai_governance_context import current_ai_scope
from app.services.background_jobs import BackgroundJobService
from app.services.governed_model import GovernedChatOpenAI
from app.services.job_executor import LocalJobExecutor
from app.services.job_runtime import JobRunner
from app.services.job_store import SqlJobStore
from tests.task_authorization_fixture import AuthorizationFixture, A, B
from tests.test_task_authorization_jobs import QueuedExecutor


@unittest.skipUnless(os.getenv("RUN_OPENFGA_INTEGRATION") == "1", "需要显式开启本机 OpenFGA 集成测试")
class RealOpenFGAJobIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (
            ("database_auto_create_schema", True), ("agent_task_authorization_enabled", True),
            ("ai_governance_enabled", False), ("reliable_research_jobs_enabled", False),
        ):
            self.stack.enter_context(patch.object(settings, name, value))
        self.fx = AuthorizationFixture()
        self.stack.callback(self.fx.close)
        self.policy = agent_authorization._configured_policy()
        self.assertIn(urlsplit(self.policy._client.url).hostname, {"localhost", "127.0.0.1", "::1"},
            "本测试只允许访问本机 OpenFGA，不能对远程授权服务写入关系。")
        self.fx.service._policy = self.policy
        self.stack.callback(self._clear_test_relations)
        self.stack.enter_context(patch.object(agent_authorization, "get_task_authorization_service", return_value=self.fx.service))
        self.store = SqlJobStore(self.fx.database)
        self.jobs = BackgroundJobService(store=self.store, executor=QueuedExecutor())
        self.stack.callback(self.jobs.shutdown)
        self.worker = LocalJobExecutor(self.store)
        self.stack.callback(self.worker.shutdown, wait=True, timeout=2)
        # 即使未来误接了真实业务路径，也必须在模型请求发出前失败，不能产生费用。
        for method in ("_generate", "_agenerate", "_stream", "_astream"):
            call = self.stack.enter_context(patch.object(GovernedChatOpenAI, method,
                side_effect=AssertionError("本测试禁止调用真实模型")))
            self.stack.callback(call.assert_not_called)

    def _clear_test_relations(self):
        # 只枚举临时数据库中的随机 grant；某一次清理失败也继续清理其余测试关系。
        with ExitStack() as cleanup:
            for grant in self.fx.service.list(user_id="chen", limit=100):
                capabilities = [cap for cap in grant["capabilities"] if cap in {"portfolio:read", "portfolio:research-run"}]
                cleanup.callback(self.policy.revoke, grant["id"], grant["portfolio_ids"], capabilities)

    def create_job(self, job_id):
        return self.jobs.start_job(user_id="chen", job_id=job_id, title="isolated FGA test",
            job_type="portfolio_monitor", portfolio_id=A, max_retries=3)["job"]

    def execute(self, job_id):
        with patch("app.services.job_executor.logger"):
            self.worker._execute_with_retry(user_id="chen", job_id=job_id)
        return self.store.get(user_id="chen", job_id=job_id)

    def test_real_job_execution_and_sql_revocation_stop_callbacks_and_delivery(self):
        executed = []

        def fake_workflow(*, record, started_at):
            scope = current_agent_execution_context()
            self.assertEqual(scope.task_grant_id, record.payload["task_grant_id"])
            self.assertEqual(scope.task_run_id, record.payload["task_run_id"])
            self.assertEqual(scope.task_agent_id, record.payload["task_agent_id"])
            self.assertTrue(self.policy.check(scope.task_grant_id, "portfolio:research-run", A))
            self.assertFalse(self.policy.check(scope.task_grant_id, "portfolio:read", B))
            agent_authorization.guard_agent_portfolio("chen", A)
            with self.assertRaises(AgentAuthorizationError):
                agent_authorization.guard_agent_portfolio("chen", B)
            executed.append(record.id)
            if record.id != "real-success":
                self.fx.service.revoke(user_id="chen", grant_id=scope.task_grant_id)
                # FGA 关系仍在，SQL 撤销必须立即阻止后续回调和结果落库。
                self.assertTrue(self.policy.check(scope.task_grant_id, "portfolio:read", A))
                if record.id == "real-revoked-callback":
                    current_ai_scope().check_cancelled()
            return {"summary": "fake workflow result", "portfolio_id": A}

        with patch.object(JobRunner, "_run_portfolio_monitor", side_effect=fake_workflow):
            success = self.create_job("real-success")
            completed = self.execute(success["id"])
            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.result["summary"], "fake workflow result")
            self.assertEqual(completed.attempt_count, 1)
            for job_id in ("real-revoked-callback", "real-revoked-delivery"):
                with self.subTest(job_id=job_id):
                    self.create_job(job_id)
                    failed = self.execute(job_id)
                    self.assertEqual(failed.status, "failed")
                    self.assertEqual(failed.attempt_count, 1)
                    self.assertIsNone(failed.result)
                    self.assertIn("撤销", failed.error)
        self.assertEqual(executed, ["real-success", "real-revoked-callback", "real-revoked-delivery"])
        self.assertEqual(len(self.fx.service.list(user_id="chen")), 3)
        self.assertIsNone(current_agent_execution_context().task_grant_id)

    def test_real_policy_relation_removal_blocks_worker_without_reissuing_grant(self):
        created = self.create_job("real-policy-denied")
        grant_id = created["payload"]["task_grant_id"]
        self.assertTrue(self.policy.check(grant_id, "portfolio:research-run", A))
        self.policy.revoke(grant_id, [A], ["portfolio:research-run"])
        self.assertFalse(self.policy.check(grant_id, "portfolio:research-run", A))
        self.assertTrue(self.policy.check(grant_id, "portfolio:read", A))
        self.assertEqual(self.fx.service.detail(user_id="chen", grant_id=grant_id)["status"], "active")

        with patch.object(JobRunner, "_run_portfolio_monitor") as workflow:
            failed = self.execute(created["id"])
        workflow.assert_not_called()
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.attempt_count, 1)
        self.assertIsNone(failed.result)
        self.assertIn("OpenFGA", failed.error)
        self.assertEqual(failed.payload["task_grant_id"], grant_id)
        self.assertEqual(len(self.fx.service.list(user_id="chen")), 1)


if __name__ == "__main__":
    unittest.main()

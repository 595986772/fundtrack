"""Job API 的范围选择与公开字段测试，授权开启且不调用网络或模型。"""

from contextlib import ExitStack
from copy import deepcopy
import asyncio
from threading import get_ident
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.routers import background_jobs as job_router
from app.services import agent_authorization
from app.services.auth import CurrentPrincipal, get_current_principal
from app.services.background_jobs import BackgroundJobService
from app.services.job_runtime import restore_job_execution_context
from app.services.job_store import SqlJobStore
from tests.task_authorization_fixture import AuthorizationFixture, A, B, C
from tests.test_task_authorization_jobs import QueuedExecutor


BASE = "/background-jobs"
PUBLIC_FIELDS = {"portfolio_id", "portfolio_ids", "task_grant_id", "task_run_id", "task_agent_id"}


class TaskAuthorizationJobApiTests(unittest.TestCase):
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
        self.store = SqlJobStore(self.fx.database)
        self.executor = QueuedExecutor()
        self.jobs = BackgroundJobService(store=self.store, executor=self.executor)
        self.stack.callback(self.jobs.shutdown)
        self.stack.enter_context(patch.object(agent_authorization, "get_task_authorization_service", return_value=self.fx.service))
        self.stack.enter_context(patch.object(job_router, "background_job_service", self.jobs))
        principal = CurrentPrincipal(user_id="chen", display_name="test", role="user", authenticated=True)
        self.stack.enter_context(patch.dict(app.dependency_overrides, {get_current_principal: lambda: principal}))
        # 不启动生产 lifespan，避免启动真实调度器或后台投递循环。
        self.client = TestClient(app)
        self.stack.callback(self.client.close)

    def start(self, **overrides):
        return self.client.post(BASE, json={
            "job_id": "api-scope-job", "title": "API scope test", "job_type": "agent_prompt",
            "portfolio_id": A, "prompt": "private prompt must stay internal", **overrides,
        })

    def test_selection_reaches_service_and_authenticated_owner_creates_grant(self):
        with patch.object(self.jobs, "start_job", wraps=self.jobs.start_job) as start_job:
            response = self.start(user_id="alice", task_grant_id="forged", task_run_id="forged", task_agent_id="forged")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(start_job.call_args.kwargs["portfolio_id"], A)
        self.assertEqual(start_job.call_args.kwargs["user_id"], "chen")
        for key in ("task_grant_id", "task_run_id", "task_agent_id"):
            self.assertNotIn(key, start_job.call_args.kwargs)
        record = self.store.get(user_id="chen", job_id="api-scope-job")
        scope = restore_job_execution_context(record)
        detail = self.fx.service.detail(user_id="chen", grant_id=scope.task_grant_id)
        self.assertEqual(detail["portfolio_ids"], [A])
        self.assertNotIn(B, detail["portfolio_ids"])
        self.assertEqual(record.payload["prompt"], "private prompt must stay internal")
        public = response.json()["job"]["payload"]
        self.assertEqual(set(public), PUBLIC_FIELDS)
        self.assertEqual(public["portfolio_ids"], [A])
        for key in ("task_grant_id", "task_run_id", "task_agent_id"):
            self.assertEqual(public[key], getattr(scope, key))
            self.assertNotEqual(public[key], "forged")

    def test_job_authorization_network_work_is_outside_api_event_loop(self):
        event_threads, policy_threads = [], []

        async def record_api_thread():
            event_threads.append(get_ident())
            return CurrentPrincipal(user_id="chen", display_name="test", role="user", authenticated=True)

        original = self.fx.policy.grant

        def grant(*args, **kwargs):
            policy_threads.append(get_ident())
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return original(*args, **kwargs)

        with patch.dict(app.dependency_overrides, {get_current_principal: record_api_thread}), \
                patch.object(self.fx.policy, "grant", side_effect=grant):
            response = self.start()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(policy_threads), 1)
        self.assertNotEqual(event_threads[0], policy_threads[0])

    def test_start_get_list_and_cancel_preserve_only_public_scope_fields(self):
        created = self.start()
        self.assertEqual(created.status_code, 200, created.text)
        expected = created.json()["job"]["payload"]
        detail = self.client.get(f"{BASE}/api-scope-job")
        listing = self.client.get(BASE)
        cancelled = self.client.post(f"{BASE}/api-scope-job/cancel", json={})
        for response in (detail, listing, cancelled):
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("private prompt must stay internal", response.text)
        jobs = [detail.json()["job"], *listing.json()["jobs"], cancelled.json()["job"]]
        for job in jobs:
            self.assertEqual(job["payload"], expected)
            self.assertEqual(set(job["payload"]), PUBLIC_FIELDS)
        self.assertEqual(cancelled.json()["job"]["status"], "cancelled")

    def test_response_model_strips_internal_fields_even_if_service_returns_them(self):
        self.assertEqual(self.start().status_code, 200)
        record = self.store.get(user_id="chen", job_id="api-scope-job")
        response = self.jobs.get_job(user_id="chen", job_id=record.id)
        # 故意模拟旧适配器直接返回完整输入，响应模型仍不能公开 Prompt。
        response["job"]["payload"] = {**record.payload, "human_review": {"private": "constraint"}}
        with patch.object(self.jobs, "get_job", return_value=response):
            result = self.client.get(f"{BASE}/{record.id}")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(set(result.json()["job"]["payload"]), PUBLIC_FIELDS)
        self.assertNotIn("private prompt must stay internal", result.text)
        self.assertNotIn("human_review", result.text)

    def test_legacy_missing_null_and_empty_payloads_remain_readable(self):
        base = self.jobs.start_job(user_id="chen", title="legacy", job_id="legacy", job_type="demo_steps")
        for shape in ("missing", "null", "empty", "nullable-fields"):
            with self.subTest(shape=shape):
                response = deepcopy(base)
                if shape == "missing":
                    response["job"].pop("payload")
                elif shape == "null":
                    response["job"]["payload"] = None
                elif shape == "empty":
                    response["job"]["payload"] = {}
                else:
                    response["job"]["payload"] = dict.fromkeys(PUBLIC_FIELDS)
                with patch.object(self.jobs, "get_job", return_value=response):
                    result = self.client.get(f"{BASE}/legacy")
                self.assertEqual(result.status_code, 200, result.text)
                expected = None if shape in {"missing", "null"} else dict.fromkeys(PUBLIC_FIELDS)
                self.assertEqual(result.json()["job"]["payload"], expected)

    def test_no_selection_stays_empty_instead_of_authorizing_all_portfolios(self):
        response = self.start(portfolio_id=None)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["job"]["payload"]["portfolio_id"])
        self.assertEqual(response.json()["job"]["payload"]["portfolio_ids"], [])
        scope = restore_job_execution_context(self.store.get(user_id="chen", job_id="api-scope-job"))
        self.assertNotIn("portfolio:read", scope.capabilities)
        self.assertNotIn("portfolio:research-run", scope.capabilities)

    def test_foreign_selection_is_rejected_before_dispatch(self):
        response = self.start(user_id="alice", portfolio_id=C)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["code"], "resource_denied")
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.store.count(user_id="chen"), 0)

    def test_malformed_selection_is_rejected_by_request_schema(self):
        for portfolio_id in ("all", [A, B], "portfolio_*", ""):
            with self.subTest(portfolio_id=portfolio_id), patch.object(self.jobs, "start_job") as start_job:
                response = self.start(portfolio_id=portfolio_id)
                self.assertEqual(response.status_code, 422, response.text)
                start_job.assert_not_called()

    def test_human_can_still_read_public_refs_after_revocation(self):
        response = self.start()
        self.assertEqual(response.status_code, 200, response.text)
        scope = response.json()["job"]["payload"]
        self.fx.service.revoke(user_id="chen", grant_id=scope["task_grant_id"])
        detail = self.client.get(f"{BASE}/api-scope-job")
        listing = self.client.get(BASE)
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(detail.json()["job"]["payload"], scope)
        self.assertEqual(listing.json()["jobs"][0]["payload"], scope)


if __name__ == "__main__":
    unittest.main()

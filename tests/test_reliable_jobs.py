"""故障注入只使用临时数据库和确定性模型，不调用收费 API。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import func, select

from app.config import settings
from app.database import Database
from app.models import BackgroundJobModel, JobOutboxModel, ReliableJobStateModel
from app.services.ai_governance_context import AIControlError, current_ai_scope, stable_run_id
from app.services.background_jobs import BackgroundJobService
from app.services.job_delivery import JobDeliveryService
from app.services.job_runtime import JobRunner
from app.services.job_store import SqlJobStore, utc_now
from app.services.reliable_job_runtime import run_reliable_job
from app.services.reliable_jobs import JobLeaseLostError, ReliableJobRepository


class CapturingReliableExecutor:
    backend_name = "local"

    def __init__(self):
        self.calls = []

    def submit(self, **kwargs):
        self.calls.append(kwargs)
        return "fake-task"

    def cancel(self, **kwargs):
        pass

    def shutdown(self, **kwargs):
        pass


class ReliableJobTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(TemporaryDirectory())
        self.db = Database(f"sqlite:///{Path(directory) / 'jobs.db'}")
        self.stack.callback(self.db.engine.dispose)
        self.stack.enter_context(patch.object(settings, "reliable_research_jobs_enabled", True))
        self.stack.enter_context(patch.object(settings, "ai_governance_enabled", False))
        # This fixture tests delivery/leases with a synthetic portfolio ID. Resource
        # authorization has separate tests backed by actual portfolio records.
        self.stack.enter_context(patch.object(settings, "agent_task_authorization_enabled", False))
        self.store = SqlJobStore(self.db)
        self.executor = CapturingReliableExecutor()
        self.service = BackgroundJobService(store=self.store, executor=self.executor)
        self.stack.callback(self.service.shutdown)
        self.now = utc_now() + timedelta(seconds=1)
        self.repo = ReliableJobRepository(self.store, clock=lambda: self.now, lease_seconds=9)

    def create(self, job_id="research-test", **kwargs):
        # 入队和后续租约使用同一个测试时钟，不受机器负载或测试运行速度影响。
        with patch("app.services.reliable_jobs.database_now", return_value=self.now):
            return self.service.start_job(user_id="alice", title="test", job_id=job_id,
                job_type="portfolio_research", portfolio_id="portfolio_0123456789abcdef", **kwargs)

    def reserve(self):
        rows = self.repo.due_deliveries("local")
        self.assertEqual(len(rows), 1)
        return self.repo.reserve_delivery(*rows[0])

    def claim(self, delivery):
        return self.repo.claim(delivery.user_id, delivery.job_id, delivery.id)

    def test_job_and_outbox_commit_together_and_rollback_together(self):
        # 故意在写完事件后抛错，验证业务 Job 也没有单独提交。
        from app.services.reliable_jobs import add_delivery
        def broken(*args, **kwargs):
            add_delivery(*args, **kwargs)
            raise RuntimeError("simulated transaction crash")
        with patch("app.services.reliable_jobs.add_delivery", side_effect=broken):
            with self.assertRaises(RuntimeError):
                self.create()
        with self.db.session() as session:
            for model in (BackgroundJobModel, ReliableJobStateModel, JobOutboxModel):
                self.assertEqual(session.scalar(select(func.count()).select_from(model)), 0)
        response = self.create()
        self.assertTrue(response["accepted"])
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(len(self.repo.due_deliveries("local")), 1)

    def test_idempotent_submission_and_legacy_claim_cannot_bypass_lease(self):
        first = self.create(idempotency_key="one")
        second = self.create(job_id="different-id", idempotency_key="one")
        self.assertEqual(first["job"]["id"], second["job"]["id"])
        self.assertFalse(second["accepted"])
        self.assertEqual(len(self.repo.due_deliveries("local")), 1)
        self.assertIsNone(self.store.claim(user_id="alice", job_id=first["job"]["id"]))

    def test_publish_failure_retry_and_lost_ack_are_safe(self):
        self.create()
        delivery = self.reserve()
        self.repo.acknowledge_delivery(delivery, error=ConnectionError("secret redis url"))
        self.assertEqual(self.repo.due_deliveries("local"), [])
        self.now += timedelta(seconds=3)
        retry = self.reserve()
        self.assertEqual(retry.id, delivery.id)
        record, lease = self.claim(retry)
        # Worker 已领取后，迟到发布者确认不能复活 Outbox。
        self.repo.acknowledge_delivery(delivery, task_id="late")
        self.repo.acknowledge_delivery(retry, task_id="also-late")
        self.assertIsNone(self.claim(delivery))
        with self.db.session() as session:
            self.assertEqual(session.get(JobOutboxModel, retry.id).status, "consumed")
        self.assertEqual(record.attempt_count, 1)

    def test_sender_crash_and_broker_message_loss_are_republished(self):
        self.create()
        first = self.reserve()
        self.now += timedelta(seconds=31)
        second = self.reserve()
        self.assertEqual(second.id, first.id)
        self.repo.acknowledge_delivery(first, task_id="late-ack")
        with self.db.session() as session:
            self.assertEqual(session.get(JobOutboxModel, first.id).sender_token, second.sender_token)
        self.repo.acknowledge_delivery(second, task_id="sent")
        self.now += timedelta(seconds=settings.job_delivery_ack_seconds + 1)
        third = self.reserve()
        self.assertEqual(third.id, first.id)
        self.assertIsNotNone(self.claim(third))

    def test_concurrent_dispatch_and_duplicate_delivery_have_one_winner(self):
        self.create()
        row = self.repo.due_deliveries("local")[0]
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.repo.reserve_delivery(*row), range(6)))
            self.assertEqual(sum(value is not None for value in results), 1)
            delivery = next(value for value in results if value)
            claims = list(pool.map(lambda _: self.claim(delivery), range(6)))
        self.assertEqual(sum(value is not None for value in claims), 1)

    def test_heartbeat_prevents_takeover_and_expiry_fences_old_results(self):
        self.create()
        delivery = self.reserve()
        _, old = self.claim(delivery)
        self.now += timedelta(seconds=8)
        self.repo.check(old, heartbeat=True)
        self.now += timedelta(seconds=2)
        self.assertEqual(self.repo.recover_expired("local"), 0)
        self.now += timedelta(seconds=8)
        self.assertEqual(self.repo.recover_expired("local"), 1)
        _, new = self.claim(self.reserve())
        self.assertEqual(new.version, 2)
        self.assertEqual(new.previous_threads, (old.checkpoint_thread_id,))
        self.assertIsNone(self.claim(delivery))
        for changes in ({"current_step": 7}, {"status": "completed", "result": {"wrong": True}}):
            with self.assertRaises(JobLeaseLostError):
                self.repo.check(old, changes=changes)
        self.repo.execution_failed(old, RuntimeError("late"))
        self.repo.check(new, changes={"status": "completed", "result": {"correct": True}})
        self.assertEqual(self.store.get(user_id="alice", job_id=new.job_id).result, {"correct": True})

    def test_cancellation_and_recovery_limits(self):
        self.create()
        _, lease = self.claim(self.reserve())
        self.service.cancel_job(user_id="alice", job_id=lease.job_id)
        self.now += timedelta(seconds=10)
        self.assertEqual(self.repo.recover_expired("local"), 0)
        with self.assertRaises(JobLeaseLostError):
            self.repo.check(lease, changes={"status": "completed"})
        self.create(job_id="bounded")
        for attempt in range(settings.job_recovery_max_attempts + 1):
            self.claim(self.reserve())
            self.now += timedelta(seconds=10)
            self.repo.recover_expired("local")
        self.assertEqual(self.store.get(user_id="alice", job_id="bounded").status, "failed")

    def test_total_deadline_is_not_reset_by_takeover(self):
        self.create(timeout_seconds=30)
        _, first = self.claim(self.reserve())
        original = self.repo.summary("alice", first.job_id)["deadline_at"]
        self.now += timedelta(seconds=10)
        self.repo.recover_expired("local")
        _, second = self.claim(self.reserve())
        self.assertEqual(self.repo.summary("alice", first.job_id)["deadline_at"], original)
        self.now += timedelta(seconds=21)
        self.repo.recover_expired("local")
        self.assertEqual(self.store.get(user_id="alice", job_id=second.job_id).status, "failed")

    def test_failures_use_bounded_outbox_retry_but_quota_error_does_not(self):
        self.create(max_retries=1)
        _, lease = self.claim(self.reserve())
        self.repo.execution_failed(lease, RuntimeError("transient"))
        self.assertEqual(self.repo.due_deliveries("local"), [])
        self.now += timedelta(seconds=3)
        _, retry = self.claim(self.reserve())
        self.repo.execution_failed(retry, RuntimeError("again"))
        self.assertEqual(self.store.get(user_id="alice", job_id=lease.job_id).status, "failed")
        self.create(job_id="budget")
        _, budget = self.claim(self.reserve())
        self.repo.execution_failed(budget, AIControlError("limit", "budget exhausted"))
        self.assertEqual(self.store.get(user_id="alice", job_id="budget").status, "failed")
        self.assertEqual(self.repo.summary("alice", "budget")["failure_retry_count"], 0)

    def test_disabling_enrollment_does_not_disable_existing_fences(self):
        self.create()
        with patch.object(settings, "reliable_research_jobs_enabled", False):
            self.assertTrue(self.repo.is_managed("alice", "research-test"))
            self.assertIsNone(self.store.claim(user_id="alice", job_id="research-test"))

    def test_runtime_preserves_budget_run_id_and_public_status(self):
        self.create()
        delivery = self.reserve()
        observed = []
        def execute(_runner, record, started_at):
            scope = current_ai_scope()
            scope.check_cancelled()
            observed.append((scope.user_id, scope.run_id))
            return {"report": "fake"}
        with patch.object(JobRunner, "_run_portfolio_research", execute):
            result = run_reliable_job(self.store, user_id="alice", job_id=delivery.job_id, dispatch_id=delivery.id)
        self.assertEqual(result.status, "completed")
        self.assertEqual(observed, [("alice", stable_run_id("job", delivery.job_id))])
        self.assertNotIn("owner_token", self.service.get_job(user_id="alice", job_id=delivery.job_id)["job"]["recovery"])

    def test_delivery_tick_handles_executor_failure_without_losing_job(self):
        self.create()
        delivery = JobDeliveryService(self.store, self.executor)
        delivery.repository = self.repo
        with patch.object(self.executor, "submit", side_effect=ConnectionError("down")):
            delivery.tick()
        job = self.store.get(user_id="alice", job_id="research-test")
        self.assertEqual(job.status, "queued")
        self.assertIn("自动补投", job.progress_message)
        self.now += timedelta(seconds=3)
        delivery.tick()
        self.assertEqual(len(self.executor.calls), 1)
        self.assertIn("dispatch_id", self.executor.calls[0])

    def test_takeover_does_not_erase_unknown_tokens_or_reset_job_budget(self):
        from app.services.ai_usage import AIUsageService
        from app.services.ai_governance_context import AIExecutionScope
        from app.models import AIModelCallModel, AIUsageBucketModel
        # PostgreSQL 版用例只建相关表，所以在此补齐本用例自己的账本表。
        for model in (AIModelCallModel, AIUsageBucketModel):
            model.__table__.create(self.db.engine, checkfirst=True)
        self.create()
        _, lease = self.claim(self.reserve())
        ledger = AIUsageService(self.db)
        scope = AIExecutionScope("alice", stable_run_id("job", lease.job_id), "background_job")
        with patch.object(settings, "ai_run_token_limit", 100), patch.object(settings, "ai_daily_token_limit", 1000):
            ledger.reserve(call_id="first-call", scope=scope, provider="fake", model="fake", tokens=80, ttl_seconds=60)
            ledger.mark_started(user_id="alice", call_id="first-call")
            ledger.settle(user_id="alice", call_id="first-call", usage=None, reason="worker_lost")
            self.now += timedelta(seconds=10)
            self.repo.recover_expired("local")
            self.claim(self.reserve())
            self.assertEqual(ledger.summary(user_id="alice")["uncertain_tokens"], 80)
            with self.assertRaises(AIControlError):
                ledger.reserve(call_id="second-call", scope=scope, provider="fake", model="fake", tokens=30, ttl_seconds=60)

    def test_memory_checkpoint_cannot_claim_durable_recovery(self):
        with patch.object(settings, "langgraph_checkpoint_backend", "memory"):
            with self.assertRaisesRegex(ValueError, "持久化"):
                self.create()
        self.assertEqual(self.store.count(user_id="alice"), 0)

    def test_rest_creation_polling_and_local_delivery_use_same_recovery_contract(self):
        import time
        from fastapi.testclient import TestClient
        from app.main import app
        from app.routers import portfolios, background_jobs
        from app.services.job_executor import LocalJobExecutor
        from app.services.portfolio_research_checkpoints import create_portfolio_research_checkpoint_runtime
        from tests.test_reliable_research_checkpoints import build_durable_graph, FakePortfolioService, PORTFOLIO_ID

        runtime = create_portfolio_research_checkpoint_runtime(backend="auto", database_url=self.db.url)
        self.stack.callback(runtime.close)
        graph = build_durable_graph(self.db, runtime)
        self.service.executor = LocalJobExecutor(self.store)
        self.service.delivery.executor = self.service.executor
        # 不进入全局 app 的 lifespan，避免测试启动用户真实环境中的 Cron。
        client = TestClient(app)
        with patch.object(portfolios, "portfolio_service", FakePortfolioService()), \
                patch.object(portfolios, "background_job_service", self.service), \
                patch.object(background_jobs, "background_job_service", self.service), \
                patch("app.services.portfolio_research_graph.portfolio_research_graph_service", graph):
            response = client.post(f"/api/v1/portfolios/{PORTFOLIO_ID}/research-runs", json={"user_id": "chen", "months": 12})
            self.assertEqual(response.status_code, 202)
            payload = response.json()["job"]
            self.assertTrue(payload["recovery"]["enabled"])
            self.assertEqual(payload["status"], "queued")
            self.service.start_reliable_delivery()
            deadline = time.monotonic() + 10
            while True:
                poll = client.get(f"/background-jobs/{payload['id']}", params={"user_id": "chen"})
                self.assertEqual(poll.status_code, 200)
                job = poll.json()["job"]
                if job["status"] in {"completed", "failed"} or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
            self.service.shutdown()
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(job["recovery"]["execution_version"], 1)
            self.assertEqual(job["result"]["workflow"], "portfolio_research")


if __name__ == "__main__":
    unittest.main()

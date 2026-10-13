"""真实 PostgreSQL 验证。只创建/删除随机测试 schema，绝不清空现有业务表。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text

from app.config import settings
from app.database import Database
from app.models import AIUsageBucketModel, AIModelCallModel, BackgroundJobModel
from app.services.ai_governance_context import AIControlError, AIExecutionScope
from app.services.ai_usage import AIUsageService
from app.services.background_jobs import BackgroundJobService
from app.services.job_store import SqlJobStore
from tests.test_ai_governance_integration import CapturingExecutor


@unittest.skipUnless(os.getenv("TEST_AI_POSTGRES_URL"), "需要 TEST_AI_POSTGRES_URL 才运行 PostgreSQL 测试")
class PostgresGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.db = Database(os.environ["TEST_AI_POSTGRES_URL"])
        original = self.db.engine
        self.stack.callback(original.dispose)
        schema = "test_ai_" + uuid4().hex
        with original.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        def cleanup():
            with original.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        self.stack.callback(cleanup)
        # 测试独占 schema；SQLAlchemy 把无 schema 的 ORM 查询统一映射到这里。
        self.db.engine = original.execution_options(schema_translate_map={None: schema})
        self.db.session_factory.configure(bind=self.db.engine)
        for model in (AIUsageBucketModel, AIModelCallModel, BackgroundJobModel):
            model.__table__.create(self.db.engine)
        self.stack.enter_context(patch.object(self.db, "create_schema"))
        self.ledger = AIUsageService(self.db)
        for key, value in {"ai_governance_enabled": True, "ai_daily_token_limit": 1000,
            "ai_run_token_limit": 500, "ai_global_pending_jobs": 3, "ai_user_pending_jobs": 2}.items():
            self.stack.enter_context(patch.object(settings, key, value))

    def test_concurrent_budget_reservations_are_atomic(self):
        def reserve(i):
            try:
                self.ledger.reserve(call_id=str(i), scope=AIExecutionScope("alice", "run:1", "test"),
                    provider="fake", model="fake", tokens=100, ttl_seconds=30)
                return True
            except AIControlError:
                return False
        with ThreadPoolExecutor(max_workers=10) as pool:
            self.assertEqual(sum(pool.map(reserve, range(12))), 5)
        self.assertEqual(self.ledger.summary(user_id="alice")["reserved_tokens"], 500)

    def test_concurrent_job_admission_and_claim_are_atomic(self):
        store = SqlJobStore(self.db)
        service = BackgroundJobService(store=store, executor=CapturingExecutor())
        def submit(i):
            try:
                return service.start_job(user_id="alice", title="test", job_id=f"job-{i}")["job"]["id"]
            except AIControlError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = [value for value in pool.map(submit, range(8)) if value]
            self.assertEqual(len(accepted), 2)
            claims = list(pool.map(lambda _: store.claim(user_id="alice", job_id=accepted[0]), range(8)))
        self.assertEqual(sum(row is not None for row in claims), 1)

    def test_unknown_cost_reconciliation_is_idempotent(self):
        self.ledger.reserve(call_id="c", scope=AIExecutionScope("alice", "run:1", "test"),
            provider="fake", model="fake", tokens=100, ttl_seconds=30)
        self.ledger.mark_started(user_id="alice", call_id="c")
        self.ledger.settle(user_id="alice", call_id="c", usage=None, reason="timeout")
        usage = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
        for _ in range(2):
            self.ledger.reconcile(user_id="alice", call_id="c", usage=usage, note="verified offline")
        summary = self.ledger.summary(user_id="alice")
        self.assertEqual((summary["used_tokens"], summary["uncertain_tokens"]), (30, 0))

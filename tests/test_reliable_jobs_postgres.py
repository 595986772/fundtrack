"""复用可靠性用例验证真实 PostgreSQL，每个用例独占随机 schema。"""

from contextlib import ExitStack
from datetime import timedelta
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import Database
from app.models import BackgroundJobModel, JobOutboxModel, ReliableJobStateModel, PortfolioResearchInputSnapshotModel
from app.services.background_jobs import BackgroundJobService
from app.services.job_store import SqlJobStore, utc_now
from app.services.portfolio_research_checkpoints import create_portfolio_research_checkpoint_runtime
from app.services.reliable_jobs import ReliableJobRepository
from tests import test_reliable_jobs as job_tests
from tests.test_reliable_research_checkpoints import build_durable_graph, FailOncePortfolioInterpreter, PORTFOLIO_ID


@unittest.skipUnless(os.getenv("TEST_RELIABILITY_POSTGRES_URL"), "需要 TEST_RELIABILITY_POSTGRES_URL")
class PostgresReliableJobTests(job_tests.ReliableJobTests):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        admin = Database(os.environ["TEST_RELIABILITY_POSTGRES_URL"])
        self.stack.callback(admin.engine.dispose)
        schema = "reliable_test_" + uuid4().hex
        with admin.engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        def cleanup():
            with admin.engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        self.stack.callback(cleanup)
        self.url = make_url(admin.url).update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
        self.db = Database(self.url)
        self.stack.callback(self.db.engine.dispose)
        self.stack.enter_context(patch.object(settings, "reliable_research_jobs_enabled", True))
        self.stack.enter_context(patch.object(settings, "ai_governance_enabled", False))
        for model in (BackgroundJobModel, JobOutboxModel, ReliableJobStateModel, PortfolioResearchInputSnapshotModel):
            model.__table__.create(self.db.engine)
        self.stack.enter_context(patch.object(self.db, "create_schema"))
        self.store = SqlJobStore(self.db)
        self.executor = job_tests.CapturingReliableExecutor()
        self.service = BackgroundJobService(store=self.store, executor=self.executor)
        self.stack.callback(self.service.shutdown)
        self.now = utc_now() + timedelta(seconds=1)
        self.repo = ReliableJobRepository(self.store, clock=lambda: self.now, lease_seconds=9)

    def test_postgres_channel_blobs_and_pending_writes_can_be_recovered(self):
        runtime = create_portfolio_research_checkpoint_runtime(backend="postgres", database_url=self.url)
        self.stack.callback(runtime.close)
        graph = build_durable_graph(self.db, runtime, FailOncePortfolioInterpreter())
        options = {"run_id": "stable-job", "user_id": "chen", "portfolio_id": PORTFOLIO_ID}
        with self.assertRaises(RuntimeError):
            graph.run(**options, checkpoint_thread_id="v1")
        graph.prepare_attempt_checkpoint(target_thread_id="v2", source_thread_ids=("v1",))
        report = graph.run(**options, checkpoint_thread_id="v2")
        self.assertTrue(report["graph_trace"]["resumed_from_checkpoint"])
        self.assertEqual(report["graph_trace"]["checkpoint_backend"], "postgres")

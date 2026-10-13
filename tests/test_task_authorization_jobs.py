"""真实 SQL 授权服务配合 FakePolicy，验证 Job 和图不会丢失任务范围。"""

from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from langgraph.runtime import Runtime

from app.config import settings
from app.database import Database
from app.models import FundPortfolioModel
from app.services import agent_authorization
from app.services.agent_authorization import AgentAuthorizationError, TaskAuthorizationService
from app.services.agent_execution_context import (
    AgentExecutionContext, agent_execution_scope, current_agent_execution_context,
)
from app.services.ai_governance_context import current_ai_scope
from app.services.background_jobs import BackgroundJobService
from app.services.job_executor import LocalJobExecutor
from app.services.job_runtime import (
    JobRunner, JobRunPause, TASK_GRANT_FIELDS, job_authorization_identity,
    restore_job_execution_context,
)
from app.services.job_store import SqlJobStore, utc_now
from app.services.portfolio_research_graph import PortfolioResearchContext
from app.services.portfolio_adjustment_graph import (
    PortfolioAdjustmentContext, PortfolioAdjustmentGraphService,
)
from tests.test_portfolio_research_graph import PORTFOLIO_ID, build_graph_service
from tests.test_portfolio_adjustment_graph import (
    FakeAdjustmentGenerator, FakeScenarioValidator, FakeScenarioExplainer,
    source_research_report, human_review, SOURCE_JOB_ID,
)


OTHER_PORTFOLIO = "portfolio_1111111111111111"
FOREIGN_PORTFOLIO = "portfolio_2222222222222222"
USER = "chen"


class FakePolicy:
    """只替换 FGA 网络层，建单、归属和生命周期仍走真实授权实现。"""

    store_id = "test-policy-store"
    model_id = "test-policy-model"

    def __init__(self):
        self.relationships = set()
        self.grants = []
        self.unavailable = False
        self.lock = Lock()

    def grant(self, grant_id, portfolios, capabilities):
        with self.lock:
            self.grants.append(grant_id)
            self.relationships.update(
                (grant_id, action, portfolio)
                for action in capabilities for portfolio in portfolios
            )

    def check(self, grant_id, action, portfolio_id):
        if self.unavailable:
            raise ConnectionError("test policy unavailable")
        with self.lock:
            return (grant_id, action, portfolio_id) in self.relationships


class QueuedExecutor:
    backend_name = "local"

    def __init__(self):
        self.calls = []

    def submit(self, **kwargs):
        self.calls.append(kwargs)
        return "queued-test-task"

    def cancel(self, **kwargs):
        pass

    def shutdown(self, **kwargs):
        pass


class TaskAuthorizationJobTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(TemporaryDirectory())
        self.database = Database(f"sqlite:///{Path(directory) / 'authorization.db'}")
        self.stack.callback(self.database.engine.dispose)
        self.stack.enter_context(patch.object(settings, "database_auto_create_schema", True))
        self.stack.enter_context(patch.object(settings, "agent_task_authorization_enabled", True))
        self.stack.enter_context(patch.object(settings, "ai_governance_enabled", False))
        self.stack.enter_context(patch.object(settings, "reliable_research_jobs_enabled", False))
        self.now = utc_now()
        self.policy = FakePolicy()
        self.authorization = TaskAuthorizationService(
            self.database, policy=self.policy, clock=lambda: self.now,
        )
        self.stack.enter_context(patch.object(
            agent_authorization, "get_task_authorization_service", return_value=self.authorization,
        ))
        with self.database.session() as session, session.begin():
            for owner, portfolio in ((USER, PORTFOLIO_ID), (USER, OTHER_PORTFOLIO), ("other", FOREIGN_PORTFOLIO)):
                session.add(FundPortfolioModel(
                    user_id=owner, id=portfolio, name=portfolio,
                    created_at=self.now, updated_at=self.now,
                ))
        self.store = SqlJobStore(self.database)
        self.executor = QueuedExecutor()
        self.jobs = BackgroundJobService(store=self.store, executor=self.executor)
        self.stack.callback(self.jobs.shutdown)
        self.context = AgentExecutionContext.product(user_id=USER, source="authorization-test")

    def create_job(self, job_id="secure-job", job_type="portfolio_research", portfolio_id=PORTFOLIO_ID, **kwargs):
        if job_type == "portfolio_adjustment":
            kwargs.setdefault("source_research_job_id", SOURCE_JOB_ID)
        return self.jobs.start_job(
            user_id=USER, title=f"Task {job_id}", job_id=job_id, job_type=job_type,
            portfolio_id=portfolio_id, **kwargs,
        )["job"]

    def record(self, job_id="secure-job"):
        return self.store.get(user_id=USER, job_id=job_id)

    def parent(self, portfolios=None, **kwargs):
        return self.authorization.create(
            self.context, run_id="parent-run", agent_id="parent-agent",
            portfolio_ids=[PORTFOLIO_ID] if portfolios is None else portfolios, **kwargs,
        )

    def revoke(self, context):
        self.authorization.revoke(user_id=USER, grant_id=context.task_grant_id)

    def test_all_protected_jobs_persist_owned_grants_and_public_references(self):
        for job_type in ("portfolio_research", "portfolio_adjustment", "portfolio_monitor", "agent_prompt"):
            with self.subTest(job_type=job_type):
                public = self.create_job(job_type, job_type)
                record = self.record(job_type)
                context = restore_job_execution_context(record)
                self.assertEqual((context.task_run_id, context.task_agent_id), job_authorization_identity(job_type, job_type))
                self.assertEqual(public["payload"]["portfolio_id"], PORTFOLIO_ID)
                self.assertNotIn("prompt", public["payload"])
                for key in TASK_GRANT_FIELDS:
                    self.assertEqual(public["payload"][key], getattr(context, key))
                self.assertEqual(self.authorization.detail(user_id=USER, grant_id=context.task_grant_id)["portfolio_ids"], [PORTFOLIO_ID])
        self.assertEqual(len(self.executor.calls), 4)

    def test_foreign_or_missing_portfolio_cannot_create_or_dispatch_job(self):
        for portfolio in (FOREIGN_PORTFOLIO, "portfolio_3333333333333333"):
            with self.subTest(portfolio=portfolio), self.assertRaises(AgentAuthorizationError):
                self.create_job(portfolio_id=portfolio)
        self.assertEqual(self.store.count(user_id=USER), 0)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.policy.grants, [])

    def test_prompt_without_selection_gets_empty_scope_not_all_user_portfolios(self):
        self.create_job(job_type="agent_prompt", portfolio_id=None)
        context = restore_job_execution_context(self.record())
        self.assertNotIn("portfolio:read", context.capabilities)
        self.assertNotIn("portfolio:research-run", context.capabilities)
        self.assertEqual(self.record().payload["portfolio_ids"], [])

    def test_parent_is_preserved_even_when_caller_passes_plain_product(self):
        parent = self.parent()
        with agent_execution_scope(parent):
            self.create_job(execution_context=self.context)
            with self.assertRaises(AgentAuthorizationError):
                self.create_job("outside", portfolio_id=OTHER_PORTFOLIO, execution_context=self.context)
        child = restore_job_execution_context(self.record())
        detail = self.authorization.detail(user_id=USER, grant_id=child.task_grant_id)
        self.assertEqual(detail["parent_grant_id"], parent.task_grant_id)
        self.revoke(parent)
        with self.assertRaises(AgentAuthorizationError):
            restore_job_execution_context(self.record())

    def test_read_only_parent_cannot_queue_research(self):
        parent = self.parent(capabilities=["portfolio:read"])
        with agent_execution_scope(parent), self.assertRaises(AgentAuthorizationError):
            self.create_job()
        self.assertEqual(self.executor.calls, [])

    def test_idempotency_checks_existing_portfolio_and_parent_scope(self):
        self.create_job("existing-b", portfolio_id=OTHER_PORTFOLIO, idempotency_key="same-key")
        parent = self.parent()
        with agent_execution_scope(parent):
            for requested in (PORTFOLIO_ID, OTHER_PORTFOLIO):
                with self.subTest(requested=requested), self.assertRaises(AgentAuthorizationError):
                    self.create_job("new-a", portfolio_id=requested, idempotency_key="same-key")
        self.assertEqual(len(self.executor.calls), 1)

    def test_idempotent_revoked_and_expired_jobs_never_mint_replacements(self):
        for suffix, expire in (("revoked", False), ("expired", True)):
            with self.subTest(suffix=suffix):
                self.create_job(suffix, idempotency_key=suffix)
                context = restore_job_execution_context(self.record(suffix))
                if expire:
                    self.now += timedelta(days=2)
                else:
                    self.revoke(context)
                count = len(self.policy.grants)
                with self.assertRaises(AgentAuthorizationError):
                    self.create_job(f"new-{suffix}", idempotency_key=suffix)
                self.assertEqual(len(self.policy.grants), count)

    def test_idempotency_race_checks_the_winning_record(self):
        self.create_job("winner-b", portfolio_id=OTHER_PORTFOLIO, idempotency_key="race-key")
        winner = self.record("winner-b")
        parent = self.parent()
        with agent_execution_scope(parent), patch.object(
            self.store, "find_by_idempotency_key", side_effect=[None, winner],
        ), patch.object(self.store, "create", side_effect=ValueError("concurrent duplicate")):
            with self.assertRaises(AgentAuthorizationError):
                self.create_job("loser-a", idempotency_key="race-key")
        self.assertEqual(len(self.executor.calls), 1)

    def test_task_job_queries_filter_actual_payload_but_human_queries_stay_unfiltered(self):
        self.create_job("visible-a")
        self.create_job("secret-b", portfolio_id=OTHER_PORTFOLIO)
        self.store.update(user_id=USER, job_id="secret-b", changes={
            "result": {"portfolio_id": PORTFOLIO_ID, "summary": "hidden B result"},
        })
        parent = self.parent()
        with agent_execution_scope(parent):
            listing = self.jobs.list_jobs(user_id=USER, max_results=1)
            self.assertEqual([row["id"] for row in listing["jobs"]], ["visible-a"])
            self.assertEqual(listing["total_count"], 1)
            self.assertEqual(self.jobs.get_job(user_id=USER, job_id="visible-a")["job"]["id"], "visible-a")
            with self.assertRaises(AgentAuthorizationError):
                self.jobs.get_job(user_id=USER, job_id="secret-b")
            with self.assertRaises(AgentAuthorizationError):
                self.jobs.list_jobs(user_id="other")
            summary = self.jobs.index_for_prompt(user_id=USER)
            self.assertNotIn("secret-b", summary)
            self.assertNotIn("hidden B result", summary)
        self.assertEqual(self.jobs.list_jobs(user_id=USER)["total_count"], 2)
        self.assertEqual(self.jobs.get_job(user_id=USER, job_id="secret-b")["job"]["result"]["summary"], "hidden B result")

    def test_list_does_not_swallow_policy_outage_or_revoked_parent(self):
        self.create_job()
        parent = self.parent()
        with agent_execution_scope(parent):
            self.policy.unavailable = True
            with self.assertRaises(AgentAuthorizationError) as raised:
                self.jobs.list_jobs(user_id=USER)
            self.assertEqual(raised.exception.status_code, 503)
            self.policy.unavailable = False
            self.revoke(parent)
            with self.assertRaises(AgentAuthorizationError):
                self.jobs.list_jobs(user_id=USER)

    def test_worker_restores_real_grant_and_forwards_it_to_agent_loop(self):
        self.create_job(job_type="agent_prompt")
        expected = restore_job_execution_context(self.record())

        async def chat(request, *, execution_context):
            self.assertEqual(execution_context.task_grant_id, expected.task_grant_id)
            self.assertEqual(current_agent_execution_context(), execution_context)
            return SimpleNamespace(answer="done", stop_reason="completed", tool_calls=[], model_dump=lambda **kwargs: {})

        with patch("app.services.tool_agent.tool_calling_agent_service.chat", new=AsyncMock(side_effect=chat)), patch.object(
            self.authorization, "create", side_effect=AssertionError("Worker must not create a grant"),
        ):
            result = JobRunner(self.store).run(user_id=USER, job_id="secure-job")
        self.assertEqual(result.status, "completed")
        self.assertIsNone(current_agent_execution_context().task_grant_id)

    def test_legacy_protected_jobs_fail_without_minting_and_are_not_retried(self):
        for job_type in ("portfolio_research", "portfolio_adjustment", "portfolio_monitor", "agent_prompt"):
            with self.subTest(job_type=job_type):
                self.create_job(job_type, job_type, max_retries=3)
                record = self.record(job_type)
                payload = {key: value for key, value in record.payload.items() if key not in TASK_GRANT_FIELDS}
                self.store.update(user_id=USER, job_id=job_type, changes={"payload": payload})
                with patch.object(self.authorization, "create", side_effect=AssertionError("no mint")), patch("app.services.job_executor.logger"):
                    LocalJobExecutor(self.store)._execute_with_retry(user_id=USER, job_id=job_type)
                failed = self.record(job_type)
                self.assertEqual(failed.status, "failed")
                self.assertEqual(failed.attempt_count, 1)
                self.assertIn("重新创建", failed.error)

    def test_worker_rejects_grant_substitution_and_wrong_run_or_agent(self):
        self.create_job("secure-a", job_type="portfolio_monitor")
        self.create_job("other-b", job_type="portfolio_monitor", portfolio_id=OTHER_PORTFOLIO)
        original = self.record("secure-a")
        other = self.record("other-b")
        for key in TASK_GRANT_FIELDS:
            with self.subTest(key=key):
                payload = {**original.payload, key: other.payload[key] if key != "task_agent_id" else "other-agent"}
                self.store.update(user_id=USER, job_id="secure-a", changes={"status": "queued", "payload": payload})
                with patch.object(JobRunner, "_run_portfolio_monitor") as work, self.assertRaises(AgentAuthorizationError):
                    JobRunner(self.store).run(user_id=USER, job_id="secure-a")
                work.assert_not_called()

    def test_revocation_before_worker_claim_blocks_business(self):
        self.create_job(job_type="portfolio_monitor")
        self.revoke(restore_job_execution_context(self.record()))
        with patch.object(JobRunner, "_run_portfolio_monitor") as work, self.assertRaises(AgentAuthorizationError):
            JobRunner(self.store).run(user_id=USER, job_id="secure-job")
        work.assert_not_called()

    def test_revocation_at_cancel_callback_and_result_delivery_stops_job(self):
        for stage in ("cancel", "complete", "pause"):
            with self.subTest(stage=stage):
                self.create_job(stage, job_type="portfolio_monitor")
                context = restore_job_execution_context(self.record(stage))

                def work(**kwargs):
                    self.assertEqual(current_agent_execution_context().task_grant_id, context.task_grant_id)
                    self.revoke(context)
                    if stage == "cancel":
                        current_ai_scope().check_cancelled()
                    if stage == "pause":
                        return JobRunPause({"summary": "must not leak"}, 1, "waiting")
                    return {"summary": "must not leak"}

                with patch.object(JobRunner, "_run_portfolio_monitor", side_effect=work), patch("app.services.job_executor.logger"):
                    LocalJobExecutor(self.store)._execute_with_retry(user_id=USER, job_id=stage)
                failed = self.record(stage)
                self.assertEqual(failed.status, "failed")
                self.assertEqual(failed.attempt_count, 1)
                self.assertIsNone(failed.result)

    def test_policy_denial_and_outage_are_nonretryable(self):
        for stage in ("deny", "outage"):
            with self.subTest(stage=stage):
                self.create_job(stage, job_type="portfolio_monitor", max_retries=3)
                if stage == "deny":
                    self.policy.relationships.clear()
                else:
                    self.policy.unavailable = True
                with patch.object(JobRunner, "_run_portfolio_monitor") as work, patch("app.services.job_executor.logger"):
                    LocalJobExecutor(self.store)._execute_with_retry(user_id=USER, job_id=stage)
                work.assert_not_called()
                self.assertEqual(self.record(stage).status, "failed")
                self.assertEqual(self.record(stage).attempt_count, 1)
                self.policy.unavailable = False

    def test_research_snapshot_threads_and_graph_nodes_keep_the_grant(self):
        graph = build_graph_service()
        parent = self.parent()
        barrier = Barrier(5)
        observed = []
        lock = Lock()

        def wrap(operation):
            def call(*args, **kwargs):
                barrier.wait(timeout=10)
                with lock:
                    observed.append(current_agent_execution_context().task_grant_id)
                agent_authorization.guard_agent_portfolio(USER, PORTFOLIO_ID)
                with self.assertRaises(AgentAuthorizationError):
                    agent_authorization.guard_agent_portfolio(USER, OTHER_PORTFOLIO)
                return operation(*args, **kwargs)
            return call

        for owner, name in ((graph.assessments, "assess"), (graph.quant_risk, "analyze"),
                            (graph.performance, "analyze"), (graph, "_capture_market_context"),
                            (graph.decisions, "list")):
            self.stack.enter_context(patch.object(owner, name, side_effect=wrap(getattr(owner, name))))
        interpreter = graph.interpreter.interpret

        def interpret(**kwargs):
            self.assertEqual(current_agent_execution_context().task_grant_id, parent.task_grant_id)
            return interpreter(**kwargs)

        with agent_execution_scope(parent), patch.object(graph.interpreter, "interpret", side_effect=interpret):
            result = graph.run(run_id="threaded-graph", user_id=USER, portfolio_id=PORTFOLIO_ID)
        self.assertEqual(result["portfolio_id"], PORTFOLIO_ID)
        self.assertEqual(observed, [parent.task_grant_id] * 5)

    def test_node_runtime_explicitly_binds_scope_in_an_unrelated_thread(self):
        context = self.parent()
        for graph, runtime_context in (
            (build_graph_service(), PortfolioResearchContext(execution_context=context)),
            (PortfolioAdjustmentGraphService(), PortfolioAdjustmentContext(execution_context=context)),
        ):
            with self.subTest(graph=type(graph).__name__):
                def node(state, runtime):
                    self.assertEqual(current_agent_execution_context().task_grant_id, context.task_grant_id)
                    return {"ok": True}
                wrapped = graph._authorized_node(node)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(wrapped, {"user_id": USER, "portfolio_id": PORTFOLIO_ID}, Runtime(context=runtime_context)).result()
                self.assertEqual(result, {"ok": True})
        self.assertIsNone(current_agent_execution_context().task_grant_id)

    def test_revocation_in_snapshot_capture_prevents_snapshot_commit(self):
        graph = build_graph_service()
        parent = self.parent()
        original = graph.assessments.assess

        def revoke_during_capture(**kwargs):
            result = original(**kwargs)
            self.revoke(parent)
            return result

        with agent_execution_scope(parent), patch.object(graph.assessments, "assess", side_effect=revoke_during_capture):
            with self.assertRaises(AgentAuthorizationError):
                graph.run(run_id="capture-revoked", user_id=USER, portfolio_id=PORTFOLIO_ID)
        self.assertIsNone(graph.snapshots.get_for_run(user_id=USER, run_id="capture-revoked"))

    def test_revocation_after_model_prevents_report_node_and_checkpoint_delivery(self):
        graph = build_graph_service()
        parent = self.parent()
        original = graph.interpreter.interpret

        def revoke_after_model(**kwargs):
            result = original(**kwargs)
            self.revoke(parent)
            return result

        with agent_execution_scope(parent), patch.object(graph.interpreter, "interpret", side_effect=revoke_after_model):
            with self.assertRaises(AgentAuthorizationError):
                graph.run(run_id="model-revoked", user_id=USER, portfolio_id=PORTFOLIO_ID)
        self.assertNotIn("report", graph.checkpoint_state(run_id="model-revoked"))

    def test_completed_research_checkpoint_rechecks_after_loading_even_without_snapshot(self):
        graph = build_graph_service()
        graph.run(run_id="completed-run", user_id=USER, portfolio_id=PORTFOLIO_ID)
        parent = self.parent()
        original = graph.checkpoint_info

        def checkpoint(**kwargs):
            result = deepcopy(original(**kwargs))
            result["state"].pop("input_snapshot", None)
            self.revoke(parent)
            return result

        with agent_execution_scope(parent), patch.object(graph, "checkpoint_info", side_effect=checkpoint):
            with self.assertRaises(AgentAuthorizationError):
                graph.run(run_id="completed-run", user_id=USER, portfolio_id=PORTFOLIO_ID)

    def test_adjustment_waiting_and_completed_checkpoints_are_reauthorized(self):
        for finish in (False, True):
            with self.subTest(finish=finish):
                graph = PortfolioAdjustmentGraphService(generator=FakeAdjustmentGenerator(),
                    scenario_validator=FakeScenarioValidator(), scenario_explainer=FakeScenarioExplainer())
                args = dict(run_id=f"adjustment-{finish}", user_id=USER, portfolio_id=PORTFOLIO_ID, source_research_job_id=SOURCE_JOB_ID)
                graph.run(**args, source_report=source_research_report())
                if finish:
                    graph.run(**args, resume_payload=human_review())
                parent = self.authorization.create(self.context, run_id=f"parent-{finish}", agent_id="parent", portfolio_ids=[PORTFOLIO_ID])
                original = graph.checkpoint_info

                def checkpoint(**kwargs):
                    result = original(**kwargs)
                    self.revoke(parent)
                    return result

                with agent_execution_scope(parent), patch.object(graph, "checkpoint_info", side_effect=checkpoint):
                    with self.assertRaises(AgentAuthorizationError):
                        graph.run(**args)

    def test_checkpoint_copy_rechecks_even_if_target_already_exists(self):
        graph = build_graph_service()
        parent = self.parent()

        def existing_target(*args, **kwargs):
            self.revoke(parent)
            return object()

        with agent_execution_scope(parent), patch.object(graph.checkpointer, "get_tuple", side_effect=existing_target):
            with self.assertRaises(AgentAuthorizationError):
                graph.prepare_attempt_checkpoint(target_thread_id="target", source_thread_ids=[])

    def test_reliable_checkpoint_copy_runs_inside_restored_scope_and_authorization_failure_is_terminal(self):
        from app.services.reliable_jobs import ReliableJobRepository
        from app.services.reliable_job_runtime import run_reliable_job

        with patch.object(settings, "reliable_research_jobs_enabled", True):
            self.create_job(max_retries=3)
        repository = ReliableJobRepository(self.store)
        delivery = repository.reserve_delivery(*repository.due_deliveries("local")[0])
        expected = restore_job_execution_context(self.record())
        graph = Mock()

        def prepare(**kwargs):
            self.assertEqual(current_agent_execution_context().task_grant_id, expected.task_grant_id)
            self.assertTrue(kwargs["target_thread_id"])
            self.revoke(expected)

        graph.prepare_attempt_checkpoint.side_effect = prepare
        graph.checkpoint_info.return_value = {"status": "empty", "completed_node_count": 0}
        with patch("app.services.portfolio_research_graph.portfolio_research_graph_service", graph), patch("app.services.reliable_job_runtime.logger"):
            run_reliable_job(self.store, user_id=USER, job_id="secure-job", dispatch_id=delivery.id)
        graph.prepare_attempt_checkpoint.assert_called_once()
        graph.run.assert_not_called()
        self.assertEqual(self.record().status, "failed")
        self.assertEqual(self.record().attempt_count, 1)
        self.assertEqual(repository.due_deliveries("local"), [])

    def test_worker_revalidates_sql_ownership_not_fake_business_service(self):
        self.create_job(job_type="portfolio_monitor")
        with self.database.session() as session, session.begin():
            session.delete(session.get(FundPortfolioModel, (USER, PORTFOLIO_ID)))
        with patch.object(JobRunner, "_run_portfolio_monitor") as work, self.assertRaises(AgentAuthorizationError):
            JobRunner(self.store).run(user_id=USER, job_id="secure-job")
        work.assert_not_called()

    def test_authorized_research_job_completes_through_real_graph(self):
        self.create_job()
        graph = build_graph_service()
        with patch("app.services.portfolio_research_graph.portfolio_research_graph_service", graph):
            completed = JobRunner(self.store).run(user_id=USER, job_id="secure-job")
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.result["portfolio_id"], PORTFOLIO_ID)
        self.assertEqual(completed.current_step, completed.total_steps)
        self.assertEqual(graph.checkpoint_info(run_id="secure-job")["status"], "completed")
        self.assertEqual(len(self.policy.grants), 1)

    def test_authorized_reliable_job_completes_through_real_graph(self):
        from app.services.reliable_jobs import ReliableJobRepository
        from app.services.reliable_job_runtime import run_reliable_job

        with patch.object(settings, "reliable_research_jobs_enabled", True):
            self.create_job()
        repository = ReliableJobRepository(self.store)
        delivery = repository.reserve_delivery(*repository.due_deliveries("local")[0])
        graph = build_graph_service()
        with patch("app.services.portfolio_research_graph.portfolio_research_graph_service", graph):
            completed = run_reliable_job(self.store, user_id=USER, job_id="secure-job", dispatch_id=delivery.id)
        self.assertIsNotNone(completed)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.result["portfolio_id"], PORTFOLIO_ID)
        self.assertEqual(len(self.policy.grants), 1)

    def test_adjustment_pause_resume_restores_same_grant(self):
        self.create_job(SOURCE_JOB_ID)
        self.store.update(user_id=USER, job_id=SOURCE_JOB_ID, changes={
            "status": "completed", "result": source_research_report(),
        })
        self.create_job(job_type="portfolio_adjustment")
        original_grant = self.record().payload["task_grant_id"]
        graph = PortfolioAdjustmentGraphService(generator=FakeAdjustmentGenerator(),
            scenario_validator=FakeScenarioValidator(), scenario_explainer=FakeScenarioExplainer())
        with patch("app.services.portfolio_adjustment_graph.portfolio_adjustment_graph_service", graph):
            paused = JobRunner(self.store).run(user_id=USER, job_id="secure-job")
            self.assertEqual(paused.status, "waiting_input")
            self.jobs.resume_portfolio_adjustment(user_id=USER, job_id="secure-job", **human_review())
            completed = JobRunner(self.store).run(user_id=USER, job_id="secure-job")
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.payload["task_grant_id"], original_grant)
        self.assertEqual(len(self.policy.grants), 2)

    def test_adjustment_source_payload_cannot_be_hidden_by_forged_result(self):
        self.create_job(SOURCE_JOB_ID, portfolio_id=OTHER_PORTFOLIO)
        self.store.update(user_id=USER, job_id=SOURCE_JOB_ID, changes={
            "status": "completed", "result": source_research_report(),
        })
        self.create_job(job_type="portfolio_adjustment")
        with patch("app.services.portfolio_adjustment_graph.portfolio_adjustment_graph_service.run") as graph:
            with self.assertRaisesRegex(AgentAuthorizationError, "研究报告与当前组合不匹配"):
                JobRunner(self.store).run(user_id=USER, job_id="secure-job")
        graph.assert_not_called()

    def test_expired_job_cannot_be_restored_or_reissued(self):
        self.create_job(job_type="portfolio_monitor", max_retries=3)
        self.now += timedelta(days=2)
        with patch.object(JobRunner, "_run_portfolio_monitor") as work, patch("app.services.job_executor.logger"):
            LocalJobExecutor(self.store)._execute_with_retry(user_id=USER, job_id="secure-job")
        work.assert_not_called()
        self.assertEqual(self.record().status, "failed")
        self.assertEqual(self.record().attempt_count, 1)
        with self.assertRaises(AgentAuthorizationError):
            self.create_job(job_type="portfolio_monitor")
        self.assertEqual(len(self.policy.grants), 1)

    def test_celery_entry_does_not_retry_authorization_errors(self):
        from app.worker_tasks import execute_background_job

        self.create_job(job_type="portfolio_monitor", max_retries=3)
        self.revoke(restore_job_execution_context(self.record()))
        with patch("app.worker_tasks.runtime_database", self.database), patch("app.worker_tasks.logger"), patch.object(
            execute_background_job, "retry", side_effect=AssertionError("authorization must not retry"),
        ) as retry, patch.object(JobRunner, "_run_portfolio_monitor") as work:
            result = execute_background_job.run(USER, "secure-job")
        retry.assert_not_called()
        work.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.record().attempt_count, 1)


if __name__ == "__main__":
    unittest.main()

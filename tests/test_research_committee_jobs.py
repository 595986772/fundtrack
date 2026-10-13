"""Research mode boundaries, with fake graphs and no paid model calls."""

from datetime import datetime
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import settings
from app.routers import portfolios as api
from app.schemas import PortfolioResearchStartRequest
from app.services.ai_governance_context import current_ai_scope, stable_run_id
from app.services.auth import CurrentPrincipal, get_current_principal
from app.services.background_jobs import BackgroundJobService
from app.services.daily_briefings import DailyBriefingService
from app.services.fund_transaction_timing import CHINA_TIMEZONE
from app.services.job_runtime import JobRunner
from app.services.job_store import InMemoryJobStore
from tests.task_authorization_fixture import install_research_authorization


PORTFOLIO_ID = "portfolio_0123456789abcdef"
RESEARCH_URL = f"/api/v1/portfolios/{PORTFOLIO_ID}/research-runs"


class CapturingExecutor:
    backend_name = "local"

    def __init__(self):
        self.calls = []

    def submit(self, **kwargs):
        self.calls.append(kwargs)
        return "fake-task"

    def shutdown(self, **kwargs):
        pass


class LegacyGraph:
    """Intentionally does not accept research_mode, like existing FakeGraphs."""

    node_count = 8

    def __init__(self, jobs, *, completed_nodes=0):
        self.jobs = jobs
        self.completed_nodes = completed_nodes
        self.calls = []
        self.progress = []
        self.checkpoint_calls = []
        self.prepare_attempt_checkpoint = Mock()
        self.snapshots = SimpleNamespace(get_for_run=Mock(return_value=None))

    def checkpoint_info(self, *, run_id, checkpoint_thread_id=None):
        self.checkpoint_calls.append((run_id, checkpoint_thread_id))
        return {
            "status": "resumable" if self.completed_nodes else "empty",
            "completed_node_count": self.completed_nodes,
            "backend": "memory",
        }

    def run(self, *, run_id, user_id, portfolio_id, months, progress_callback,
            checkpoint_thread_id=None, daily_briefing=None, research_plan=None, repair_previous=None, repair_checkpoint=None):
        return self._complete(
            run_id=run_id, user_id=user_id, portfolio_id=portfolio_id, months=months,
            progress_callback=progress_callback, checkpoint_thread_id=checkpoint_thread_id,
            daily_briefing=daily_briefing,
            research_plan=research_plan,
        )

    def _complete(self, *, progress_callback, **kwargs):
        self.calls.append({**kwargs, "scope": current_ai_scope()})
        for step in range(self.completed_nodes + 1, self.node_count + 1):
            progress_callback(f"node_{step}", "Completed fake node")
            self.progress.append(self.jobs.get_job(
                user_id=kwargs["user_id"], job_id=kwargs["run_id"],
            )["job"])
        return {"workflow": "portfolio_research", "portfolio_id": kwargs["portfolio_id"],
                "summary": "Fake research report"}


class CommitteeGraph(LegacyGraph):
    node_count = 13

    def run(self, *, run_id, user_id, portfolio_id, months, progress_callback,
            research_mode, checkpoint_thread_id=None, research_plan=None, repair_previous=None, repair_checkpoint=None):
        return self._complete(
            run_id=run_id, user_id=user_id, portfolio_id=portfolio_id, months=months,
            research_mode=research_mode, progress_callback=progress_callback,
            checkpoint_thread_id=checkpoint_thread_id,
            research_plan=research_plan,
        )


class ResearchCommitteeJobTests(unittest.TestCase):
    def setUp(self):
        install_research_authorization(self, PORTFOLIO_ID)
        self.enterContext(patch.object(settings, "job_default_timeout_seconds", 310))
        self.enterContext(patch.object(settings, "job_default_max_retries", 2))
        self.model_factory = self.enterContext(patch(
            "app.services.llm.get_chat_model",
            side_effect=AssertionError("Tests must not request a real model"),
        ))
        self.store = InMemoryJobStore()
        self.executor = CapturingExecutor()
        self.jobs = BackgroundJobService(store=self.store, executor=self.executor)
        self.addCleanup(self.jobs.shutdown)

    def start(self, **kwargs):
        options = {"user_id": "chen", "title": "Research test",
                   "job_type": "portfolio_research", "portfolio_id": PORTFOLIO_ID}
        result = self.jobs.start_job(**{**options, **kwargs})
        return self.store.get(user_id="chen", job_id=result["job"]["id"])

    def run_job(self, record, graph):
        # Exercise JobRunner without importing or executing the production graph.
        module = SimpleNamespace(portfolio_research_graph_service=graph)
        with patch.dict(sys.modules, {"app.services.portfolio_research_graph": module}):
            return JobRunner(self.store).run(user_id=record.user_id, job_id=record.id)

    def client(self):
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[get_current_principal] = lambda: CurrentPrincipal(
            user_id="chen", display_name="Test", role="user", authenticated=True,
        )
        self.portfolios = self.enterContext(patch.object(api, "portfolio_service"))
        self.portfolios.get.return_value = {"name": "Test portfolio"}
        self.enterContext(patch.object(api, "background_job_service", self.jobs))
        return self.enterContext(TestClient(app))

    def test_request_schema_defaults_and_rejects_invalid_modes(self):
        self.assertEqual(PortfolioResearchStartRequest().research_mode, "standard")
        self.assertEqual(
            PortfolioResearchStartRequest(research_mode="committee").research_mode,
            "committee",
        )
        for value in ("", "light", "multi_role", "Committee", None, []):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                PortfolioResearchStartRequest(research_mode=value)

    def test_committee_defaults_are_persisted_and_submitted(self):
        record = self.start(research_mode="committee")
        self.assertEqual(record.job_type, "portfolio_research")
        self.assertEqual(record.payload["research_mode"], "committee")
        self.assertEqual((record.total_steps, record.timeout_seconds, record.max_retries),
                         (13, 900, 0))
        self.assertEqual(self.executor.calls, [{
            "user_id": "chen", "job_id": record.id, "timeout_seconds": 900,
        }])

    def test_standard_defaults_and_explicit_committee_limits(self):
        for options in ({}, {"research_mode": "standard"}):
            with self.subTest(options=options):
                record = self.start(**options)
                self.assertEqual(record.payload["research_mode"], "standard")
                self.assertEqual(
                    (record.total_steps, record.timeout_seconds, record.max_retries),
                    (8, 310, 2),
                )
        record = self.start(research_mode="committee", timeout_seconds=600, max_retries=1)
        self.assertEqual((record.timeout_seconds, record.max_retries), (600, 1))

    def test_service_rejects_invalid_modes_before_submission(self):
        for value in ("", "light", "multi_role", "Committee", None, []):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "research_mode"):
                self.start(research_mode=value)
        self.assertEqual(self.store.count(user_id="chen"), 0)
        self.assertEqual(self.executor.calls, [])

    def test_non_research_jobs_cannot_select_committee(self):
        for job_type in ("demo_steps", "agent_prompt", "portfolio_monitor", "portfolio_adjustment"):
            with self.subTest(job_type=job_type), self.assertRaisesRegex(ValueError, "portfolio_research"):
                self.start(job_type=job_type, research_mode="committee")
        self.assertEqual(self.store.count(user_id="chen"), 0)
        self.assertEqual(self.executor.calls, [])

    def test_other_jobs_keep_their_existing_defaults(self):
        record = self.start(job_type="agent_prompt")
        self.assertNotIn("research_mode", record.payload)
        self.assertEqual((record.total_steps, record.timeout_seconds, record.max_retries),
                         (3, 310, 2))
        self.model_factory.assert_not_called()

    def test_daily_briefing_cannot_select_committee(self):
        for briefing in ({}, {"trade_date": "2026-09-17", "trigger": "manual"}):
            with self.subTest(briefing=briefing), self.assertRaisesRegex(ValueError, "standard"):
                self.start(research_mode="committee", daily_briefing=briefing)
        self.assertEqual(self.store.count(user_id="chen"), 0)
        self.assertEqual(self.executor.calls, [])

    def test_api_committee_reaches_payload_graph_and_thirteen_step_progress(self):
        client = self.client()
        response = client.post(RESEARCH_URL, json={
            "user_id": "forged-user", "months": 6, "research_mode": "committee",
            "idempotency_key": "committee-api",
        })
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()["job"]
        self.assertEqual((job["user_id"], job["total_steps"], job["timeout_seconds"], job["max_retries"]),
                         ("chen", 13, 900, 0))
        record = self.store.get(user_id="chen", job_id=job["id"])
        self.assertEqual(record.payload["research_mode"], "committee")
        graph = CommitteeGraph(self.jobs)
        completed = self.run_job(record, graph)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.current_step, 13)
        self.assertEqual(graph.calls[0]["research_mode"], "committee")
        self.assertEqual(graph.calls[0]["months"], 6)
        self.assertEqual(graph.calls[0]["scope"].run_id, stable_run_id("job", record.id))
        self.assertEqual([item["current_step"] for item in graph.progress], list(range(1, 14)))
        self.assertLess(graph.progress[7]["progress_percent"], 100)
        self.assertEqual(graph.progress[-1]["progress_percent"], 100)
        self.portfolios.get.assert_called_once_with(user_id="chen", portfolio_id=PORTFOLIO_ID)
        self.model_factory.assert_not_called()

    def test_api_default_uses_legacy_graph_signature_and_eight_steps(self):
        response = self.client().post(RESEARCH_URL, json={})
        self.assertEqual(response.status_code, 202, response.text)
        record = self.store.get(user_id="chen", job_id=response.json()["job"]["id"])
        self.assertEqual(record.payload["research_mode"], "standard")
        graph = LegacyGraph(self.jobs)
        completed = self.run_job(record, graph)
        self.assertEqual((completed.status, completed.current_step), ("completed", 8))
        self.assertNotIn("research_mode", graph.calls[0])
        self.assertEqual([item["current_step"] for item in graph.progress], list(range(1, 9)))

    def test_api_invalid_mode_is_rejected_before_portfolio_or_job_work(self):
        client = self.client()
        for value in ("light", "multi_role", "Committee", None, []):
            with self.subTest(value=value):
                response = client.post(RESEARCH_URL, json={"research_mode": value})
                self.assertEqual(response.status_code, 422, response.text)
        self.portfolios.get.assert_not_called()
        self.assertEqual(self.store.count(user_id="chen"), 0)
        self.assertEqual(self.executor.calls, [])

    def test_old_payload_without_mode_still_runs_standard(self):
        record = self.start()
        payload = dict(record.payload)
        payload.pop("research_mode")
        record = self.store.update(user_id="chen", job_id=record.id, changes={"payload": payload})
        graph = LegacyGraph(self.jobs)
        completed = self.run_job(record, graph)
        self.assertEqual((completed.status, completed.current_step), ("completed", 8))
        self.assertNotIn("research_mode", graph.calls[0])

    def test_worker_revalidates_invalid_and_daily_committee_payloads(self):
        invalid_payloads = [
            {"research_mode": "multi_role"},
            {"research_mode": None},
            {"research_mode": []},
            {"research_mode": "committee", "daily_briefing": {}},
            {"research_mode": "committee", "daily_briefing": {"trade_date": "2026-09-17"}},
        ]
        with patch("app.services.daily_briefing_data.prepare_daily_briefing") as prepare:
            for changes in invalid_payloads:
                with self.subTest(changes=changes):
                    record = self.start()
                    record = self.store.update(user_id="chen", job_id=record.id,
                        changes={"payload": {**record.payload, **changes}})
                    graph = LegacyGraph(self.jobs)
                    with self.assertRaises(ValueError):
                        self.run_job(record, graph)
                    self.assertEqual(graph.calls, [])
                    self.assertEqual(graph.checkpoint_calls, [])
            prepare.assert_not_called()
        self.model_factory.assert_not_called()

    def test_committee_resume_keeps_partition_mode_budget_and_progress(self):
        record = self.start(research_mode="committee")
        record = self.store.update(user_id="chen", job_id=record.id, changes={"current_step": 8})
        self.store.lease = SimpleNamespace(
            checkpoint_thread_id="research:v2", previous_threads=("research:v1",), version=2,
        )
        graph = CommitteeGraph(self.jobs, completed_nodes=8)
        completed = self.run_job(record, graph)
        graph.prepare_attempt_checkpoint.assert_called_once_with(
            target_thread_id="research:v2", source_thread_ids=("research:v1",),
        )
        self.assertEqual(graph.checkpoint_calls, [(record.id, "research:v2")])
        self.assertEqual(graph.calls[0]["checkpoint_thread_id"], "research:v2")
        self.assertEqual(graph.calls[0]["research_mode"], "committee")
        self.assertEqual(graph.calls[0]["scope"].run_id, stable_run_id("job", record.id))
        self.assertEqual([item["current_step"] for item in graph.progress], list(range(9, 14)))
        self.assertEqual((completed.status, completed.current_step, completed.max_retries),
                         ("completed", 13, 0))

    def test_daily_service_keeps_lightweight_mode_and_one_job(self):
        portfolios = Mock()
        portfolios.get.return_value = {"name": "Daily portfolio", "holdings": [{"fund_code": "110020"}]}
        calendar = Mock()
        calendar.is_session.return_value = True
        daily = DailyBriefingService(
            database=Mock(), portfolios=portfolios, jobs=self.jobs, calendar=calendar,
            clock=lambda: datetime(2026, 9, 17, 14, 30, tzinfo=CHINA_TIMEZONE),
        )
        first = daily.run_today(user_id="chen", portfolio_id=PORTFOLIO_ID)
        second = daily.run_today(user_id="chen", portfolio_id=PORTFOLIO_ID)
        self.assertFalse(second["accepted"])
        self.assertEqual(first["job"]["id"], second["job"]["id"])
        record = self.store.get(user_id="chen", job_id=first["job"]["id"])
        self.assertEqual(record.payload["research_mode"], "standard")
        self.assertEqual((record.total_steps, record.timeout_seconds, record.max_retries), (8, 900, 0))
        graph = LegacyGraph(self.jobs)
        prepared = {"trade_date": "2026-09-17", "gate": {"status": "ready"}}
        with patch("app.services.daily_briefing_data.prepare_daily_briefing", return_value=prepared) as prepare:
            completed = self.run_job(record, graph)
            prepare.assert_called_once()
        self.assertEqual(completed.status, "completed")
        self.assertEqual(len(graph.calls), 1)
        self.assertEqual(graph.calls[0]["daily_briefing"], prepared)
        self.assertNotIn("research_mode", graph.calls[0])
        self.model_factory.assert_not_called()

    def test_frozen_daily_input_is_not_prepared_again(self):
        record = self.start(daily_briefing={"trade_date": "2026-09-17", "trigger": "manual"})
        graph = LegacyGraph(self.jobs)
        graph.snapshots.get_for_run.return_value = {"snapshot_id": "frozen"}
        with patch("app.services.daily_briefing_data.prepare_daily_briefing") as prepare:
            completed = self.run_job(record, graph)
            prepare.assert_not_called()
        self.assertEqual((completed.status, completed.current_step), ("completed", 8))
        self.assertNotIn("research_mode", graph.calls[0])


if __name__ == "__main__":
    unittest.main()

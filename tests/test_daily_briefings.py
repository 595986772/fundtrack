"""自动简报的确定性测试：临时 SQL、假行情和假模型，不花模型额度、不采集真实新闻。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import settings
from app.database import Database
from app.models import BackgroundJobModel, PortfolioDailyBriefingPolicyModel
from app.routers import daily_briefings as api
from app.services.auth import CurrentPrincipal, get_current_principal
from app.services.background_jobs import BackgroundJobService
from app.services.daily_briefings import DailyBriefingService
from app.services.daily_briefing_data import DailyBriefingData, BriefingDataUnavailable, check_inputs
from app.services.fund_transaction_timing import CHINA_TIMEZONE
from app.services.job_store import SqlJobStore
from app.services.portfolios import PortfolioNotFoundError
from app.services.vector_rag import DashScopeEmbeddingClient
from tests.test_portfolio_research_graph import build_graph_service, FakePortfolioInterpreter
from tests.test_reliable_jobs import CapturingReliableExecutor
from tests.task_authorization_fixture import install_research_authorization


PID = "portfolio_0123456789abcdef"
NOW = datetime(2026, 9, 17, 14, 30, tzinfo=CHINA_TIMEZONE)
FUNDS = [{"fund_code": "110020", "short_name": "测试基金", "latest_nav": 1.2,
          "latest_nav_date": "2026-09-16", "daily_growth_rate_pct": 0.5, "source": "假数据"}]
QUOTE = {"index_code": "000001", "index_name": "测试指数", "last": 3000, "change_pct": 0.1,
         "quote_at": NOW.isoformat(), "source_url": "https://example.com/index"}


class PortfolioReader:
    def get(self, *, user_id, portfolio_id):
        if user_id != "chen" or portfolio_id != PID:
            raise PortfolioNotFoundError("not found")
        return {"name": "测试组合", "holdings": deepcopy(FUNDS)}


class DailyBriefingTests(unittest.TestCase):
    def setUp(self):
        install_research_authorization(self, PID)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(f"sqlite:///{Path(directory.name) / 'briefing.db'}")
        self.addCleanup(self.db.engine.dispose)
        for name, value in [("ai_governance_enabled", False), ("reliable_research_jobs_enabled", True)]:
            patcher = patch.object(settings, name, value)
            patcher.start(); self.addCleanup(patcher.stop)
        self.store = SqlJobStore(self.db)
        self.jobs = BackgroundJobService(store=self.store, executor=CapturingReliableExecutor())
        self.addCleanup(self.jobs.shutdown)
        self.service = DailyBriefingService(database=self.db, portfolios=PortfolioReader(), jobs=self.jobs, clock=lambda: NOW)

    def rows(self):
        with self.db.session() as session:
            return session.scalar(select(func.count()).select_from(BackgroundJobModel))

    def start(self):
        result = self.service.run_today(user_id="chen", portfolio_id=PID)
        return self.store.get(user_id="chen", job_id=result["job"]["id"])

    def data_service(self, *, quote=None, extra=None, clock=lambda: NOW):
        fetch = Mock(return_value={"items": [deepcopy(QUOTE if quote is None else quote)], "source_name": "假行情"})
        stages = [("indices", "指数", fetch)]
        if extra:
            stages.insert(0, extra)
        return DailyBriefingData(stages=stages, funds=lambda: deepcopy(FUNDS), clock=clock), fetch

    def test_default_off_and_read_never_create_policy_or_job(self):
        result = self.service.overview(user_id="chen", portfolio_id=PID)
        self.assertFalse(result["policy"]["enabled"])
        self.service.tick(NOW)
        self.assertEqual(self.rows(), 0)
        with self.db.session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(PortfolioDailyBriefingPolicyModel)), 0)

    def test_time_zone_calendar_catchup_and_restart_idempotency(self):
        self.service.configure(user_id="chen", portfolio_id=PID, enabled=True)
        for moment in (NOW.replace(hour=14, minute=29), NOW.replace(minute=40),
                       NOW.replace(month=10, day=1), NOW.replace(day=19)):
            self.service.tick(moment)
        self.assertEqual(self.rows(), 0)
        self.service.tick(NOW.astimezone(timezone.utc))
        second = DailyBriefingService(database=self.db, portfolios=PortfolioReader(), jobs=self.jobs)
        second.tick(NOW + timedelta(minutes=8))
        manual = self.service.run_today(user_id="chen", portfolio_id=PID)
        self.assertFalse(manual["accepted"])
        self.assertEqual(self.rows(), 1)
        self.assertEqual(manual["job"]["job_type"], "portfolio_research")
        self.assertEqual(self.service.overview(user_id="chen", portfolio_id=PID)["next_run_at"], "2026-09-18T14:30:00+08:00")

    def test_parallel_submit_has_one_sql_winner(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.service.run_today(user_id="chen", portfolio_id=PID), range(4)))
        self.assertEqual(sum(item["accepted"] for item in results), 1)
        self.assertEqual(self.rows(), 1)

    def test_unknown_calendar_blocks_and_records_safe_error(self):
        self.service.configure(user_id="chen", portfolio_id=PID, enabled=True)
        self.service.calendar = Mock()
        self.service.calendar.is_session.side_effect = ValueError("private provider detail")
        self.service.tick(NOW)
        overview = self.service.overview(user_id="chen", portfolio_id=PID)
        self.assertEqual(self.rows(), 0)
        self.assertEqual(overview["calendar_status"], "unavailable")
        self.assertNotIn("private", overview["policy"]["last_error"])

    def test_no_manual_work_before_open_or_on_closed_day(self):
        for now in (NOW.replace(hour=9, minute=29), NOW.replace(month=10, day=1)):
            with self.assertRaises(ValueError):
                self.service.run_today(user_id="chen", portfolio_id=PID, now=now)
        self.assertEqual(self.rows(), 0)

    def test_prepare_saves_partial_failure_and_reuses_frozen_input(self):
        record = self.start()
        reader, fetch = self.data_service(extra=("news", "新闻", Mock(side_effect=TimeoutError("secret"))))
        prepared = reader.prepare(record=record, store=self.store, check=lambda: None)
        self.assertEqual(prepared["gate"]["status"], "ready")
        self.assertEqual(prepared["sources"][0]["status"], "failed")
        self.assertNotIn("secret", str(prepared))
        self.assertEqual(prepared["funds"][0]["latest_nav_date"], "2026-09-16")
        reader.clock = lambda: NOW + timedelta(days=1)
        self.assertEqual(prepared, reader.prepare(record=record, store=self.store, check=lambda: None))
        fetch.assert_called_once()

    def test_old_quote_blocks_and_persists_reason_without_model(self):
        record = self.start()
        reader, _ = self.data_service(quote={**QUOTE, "quote_at": (NOW - timedelta(days=1)).isoformat()})
        with self.assertRaises(BriefingDataUnavailable):
            reader.prepare(record=record, store=self.store, check=lambda: None)
        result = self.store.get(user_id="chen", job_id=record.id).result["daily_briefing_input"]
        self.assertEqual(result["gate"]["status"], "blocked")
        self.assertEqual(result["indices"]["items"], [])
        self.assertIn("停止", " ".join(result["warnings"]))

    def test_future_quotes_and_invalid_fund_values_are_not_valid_data(self):
        for quote in ({**QUOTE, "quote_at": (NOW + timedelta(hours=1)).isoformat()}, {**QUOTE, "change_pct": None}):
            gate, usable = check_inputs(funds=FUNDS, indices=[quote], trade_date="2026-09-17", captured_at=NOW)
            self.assertEqual((gate["status"], usable), ("blocked", []))
        for nav in (None, 0, float("nan")):
            gate, _ = check_inputs(funds=[{**FUNDS[0], "latest_nav": nav}], indices=[QUOTE], trade_date="2026-09-17", captured_at=NOW)
            self.assertEqual(gate["status"], "blocked")

    def test_queued_auto_task_expired_and_cancelled_task_do_not_fetch(self):
        self.service.configure(user_id="chen", portfolio_id=PID, enabled=True)
        self.service.tick(NOW)
        record = self.start()
        reader, fetch = self.data_service(clock=lambda: NOW.replace(minute=41))
        with self.assertRaises(BriefingDataUnavailable):
            reader.prepare(record=record, store=self.store, check=lambda: None)
        fetch.assert_not_called()
        reader.clock = lambda: NOW
        with self.assertRaises(TimeoutError):
            reader.prepare(record=record, store=self.store, check=Mock(side_effect=TimeoutError()))
        fetch.assert_not_called()

    def test_resume_after_stage_does_not_refetch_successful_source(self):
        record = self.start()
        old = {"trade_date": "2026-09-17", "sources": [], "warnings": [],
               "indices": {"items": [QUOTE], "source_name": "已保存行情"}}
        self.store.update(user_id="chen", job_id=record.id, changes={"result": {"daily_briefing_input": old}})
        reader, fetch = self.data_service()
        reader.prepare(record=record, store=self.store, check=lambda: None)
        fetch.assert_not_called()

    def test_api_principal_overrides_forged_user_and_other_portfolio_is_hidden(self):
        app = FastAPI(); app.include_router(api.router)
        app.dependency_overrides[get_current_principal] = lambda: CurrentPrincipal("chen", "test", "user", True)
        with patch.object(api, "daily_briefing_service", self.service):
            client = TestClient(app)
            path = f"/api/v1/portfolios/{PID}/daily-briefing"
            self.assertEqual(client.put(path, json={"user_id": "bob", "enabled": True}).status_code, 200)
            self.assertEqual(client.get(path + "?user_id=bob").json()["policy"]["enabled"], True)
            self.assertEqual(client.get(path.replace(PID, "portfolio_1111111111111111") + "?user_id=chen").status_code, 404)
            self.assertEqual(client.post(path + "/runs?user_id=bob").status_code, 200)
            self.assertEqual(self.rows(), 1)

    def test_json_policy_toggle_persists_without_starting_job(self):
        app = FastAPI(); app.include_router(api.router)
        app.dependency_overrides[get_current_principal] = lambda: CurrentPrincipal("chen", "test", "user", True)
        with patch.object(api, "daily_briefing_service", self.service), TestClient(app) as client:
            path = f"/api/v1/portfolios/{PID}/daily-briefing"
            # 模拟浏览器的错误 Content-Type，确认失败请求没有写入策略或创建任务。
            invalid = client.put(path, content='{"user_id":"chen","enabled":true}',
                                 headers={"Content-Type": "text/plain;charset=UTF-8"})
            self.assertEqual(invalid.status_code, 422)
            self.assertFalse(client.get(path + "?user_id=chen").json()["policy"]["enabled"])
            for enabled in (True, False):
                response = client.put(path, json={"user_id": "chen", "enabled": enabled})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["policy"]["enabled"], enabled)
                self.assertEqual(client.get(path + "?user_id=chen").json()["policy"]["enabled"], enabled)
                self.assertEqual(self.rows(), 0)

    def test_complete_job_prepares_data_then_freezes_graph_and_saves_readable_history(self):
        from app.services.ai_governance_context import current_ai_scope, stable_run_id
        from app.services.job_runtime import JobRunner
        from app.services.portfolio_research_snapshots import PortfolioResearchSnapshotService
        # 本测试同步执行真正的 JobRunner 和 LangGraph，只有采集与模型是替身。
        with patch.object(settings, "reliable_research_jobs_enabled", False):
            record = self.start()
        reader, fetch = self.data_service()
        reader.funds = lambda: [*deepcopy(FUNDS), {**FUNDS[0], "fund_code": "110007"}]
        graph = build_graph_service(snapshots=PortfolioResearchSnapshotService(self.db))
        def prepare(**kwargs):
            self.assertEqual(current_ai_scope().user_id, "chen")
            self.assertEqual(current_ai_scope().run_id, stable_run_id("job", record.id))
            return reader.prepare(**kwargs)
        with patch("app.services.daily_briefing_data.prepare_daily_briefing", side_effect=prepare), patch(
            "app.services.portfolio_research_graph.portfolio_research_graph_service", graph):
            JobRunner(self.store).run(user_id="chen", job_id=record.id)
        saved = self.store.get(user_id="chen", job_id=record.id)
        self.assertEqual(saved.status, "completed")
        self.assertEqual(saved.result["market_snapshot"]["daily_briefing"]["gate"]["status"], "ready")
        history = self.service.overview(user_id="chen", portfolio_id=PID)["runs"]
        self.assertTrue(history[0]["model_used"])
        self.assertEqual(history[0]["captured_at"], NOW.isoformat())
        fetch.assert_called_once()
        from app.services.portfolio_agent_tools import _bounded_research_report
        self.assertEqual(_bounded_research_report(saved.result)["market_snapshot"]["daily_briefing"]["trade_date"], "2026-09-17")


class DailyBriefingGraphTests(unittest.TestCase):
    def test_briefing_evidence_reaches_model_and_original_snapshot_is_reused(self):
        interpreter = Mock(wraps=FakePortfolioInterpreter())
        service = build_graph_service(interpreter=interpreter)
        briefing = {"trade_date": "2026-09-17", "captured_at": NOW.isoformat(),
                    "funds": [{"fund_code": "110020"}, {"fund_code": "110007"}], "warnings": ["测试缺口"],
                    "evidence": [{"evidence_id": "briefing:index:test", "title": "测试行情"}]}
        report = service.run(run_id="daily-test", user_id="chen", portfolio_id=PID, months=12, daily_briefing=briefing)
        self.assertEqual(report["market_snapshot"]["daily_briefing"], briefing)
        self.assertEqual(interpreter.interpret.call_args.kwargs["market_context"]["daily_briefing"], briefing)
        replay = service.run(run_id="daily-test", user_id="chen", portfolio_id=PID, months=12, daily_briefing={"changed": True})
        self.assertEqual(replay["market_snapshot"]["daily_briefing"], briefing)
        interpreter.interpret.assert_called_once()
        from app.services.portfolio_research_interpretation import PortfolioResearchInterpretationService
        catalog = PortfolioResearchInterpretationService._evidence_catalog(assessment={}, market_context=report["market_snapshot"])
        self.assertIn("briefing:index:test", catalog)

    def test_changed_portfolio_funds_abort_before_interpretation(self):
        interpreter = Mock(wraps=FakePortfolioInterpreter())
        with self.assertRaisesRegex(ValueError, "持仓基金发生变化"):
            build_graph_service(interpreter=interpreter).run(run_id="changed-holdings", user_id="chen", portfolio_id=PID,
                months=12, daily_briefing={"funds": [{"fund_code": "other"}]})
        interpreter.interpret.assert_not_called()


class GovernedEmbeddingTests(unittest.TestCase):
    def test_embedding_batches_settle_usage_and_missing_usage_stays_unknown(self):
        client = object.__new__(DashScopeEmbeddingClient)
        client._governed, client._timeout = True, 30
        client._client = Mock()
        client._client.embeddings.create.side_effect = [
            SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[float(i)]) for i in range(10)],
                            usage=SimpleNamespace(prompt_tokens=40, total_tokens=40)),
            SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[10.0])], usage=None)]
        permits = []
        @contextmanager
        def permit(**kwargs):
            self.assertEqual(kwargs["payload"]["max_tokens"], 0)
            item = SimpleNamespace(usage=None); permits.append(item); yield item
        with patch("app.services.governed_model.model_permit", permit):
            result = client.embed_texts(["测试"] * 11)
        self.assertEqual(len(result), 11)
        self.assertEqual(permits[0].usage["total_tokens"], 40)
        self.assertIsNone(permits[1].usage)


if __name__ == "__main__":
    unittest.main()

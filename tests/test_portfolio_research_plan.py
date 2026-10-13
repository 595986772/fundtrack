"""按需工作流端到端契约：API、持久化任务、真实图、模型输入与断点恢复。"""

from copy import deepcopy
from unittest import TestCase
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import portfolios as api
from app.services.auth import CurrentPrincipal, get_current_principal
from app.services.background_jobs import BackgroundJobService
from app.services.job_runtime import JobRunner
from app.services.portfolio_report_evidence import PortfolioReportEvidenceService
from app.services.portfolio_research_plan import build_research_plan
from app.services.portfolio_research_interpretation import PortfolioResearchInterpretationService
from tests.task_authorization_fixture import install_research_authorization
from tests.test_portfolio_research_graph import PORTFOLIO_ID, build_graph_service, FailOncePortfolioInterpreter
from tests.test_portfolio_report_evidence import reader_for, report_pair
from tests.test_research_committee_jobs import CapturingExecutor


class ResearchPlanTests(TestCase):
    def test_auto_routing_and_explicit_override(self):
        for question, expected in (("解释我的配置", "explain"), ("比较两期季报", "strategy"),
                ("深入分析反对理由", "committee"), ("当前组合如何", "standard")):
            with self.subTest(question=question):
                self.assertEqual(build_research_plan(question, "auto")["mode"], expected)
        self.assertEqual(build_research_plan("比较季报", "explain")["mode"], "explain")

    def test_focused_path_skips_optional_sources_and_freezes_question(self):
        for mode in ("explain", "strategy"):
            graph = build_graph_service()
            reader = reader_for(report_pair())
            graph.report_evidence = PortfolioReportEvidenceService(reader, max_funds=1)
            graph.market_sentiment.analyze = Mock(side_effect=AssertionError("不应读取市场"))
            graph.decisions.list = Mock(side_effect=AssertionError("不应读取历史"))
            plan = build_research_plan("110020 的策略是什么？", mode)
            report = graph.run(run_id=mode, user_id="chen", portfolio_id=PORTFOLIO_ID, research_plan=plan)
            self.assertEqual(report["research_plan"], plan)
            self.assertEqual(report["graph_trace"]["completed_nodes"],
                ["load_portfolio", "load_focused_inputs", "interpret_with_llm", "compose_report"])
            self.assertEqual(reader.research.call_count, 0 if mode == "explain" else 1)
            if mode == "strategy":
                self.assertEqual(reader.research.call_args.args[1], plan["question"])
            frozen = graph.snapshots.get_for_run(user_id="chen", run_id=mode)
            self.assertEqual(frozen["payload"]["research_plan"], plan)
            metrics = PortfolioResearchInterpretationService._metric_catalog(
                assessment=frozen["payload"]["assessment"], quant_risk=frozen["payload"]["quant_risk"],
                market_context=frozen["payload"]["market_context"])
            self.assertNotIn("market.relevant_event_count", metrics)

    def test_checkpoint_cannot_change_question_or_refresh_permission(self):
        graph = build_graph_service(interpreter=FailOncePortfolioInterpreter())
        plan = build_research_plan("解释当前配置", "auto")
        args = dict(run_id="resume-plan", user_id="chen", portfolio_id=PORTFOLIO_ID)
        with self.assertRaises(RuntimeError):
            graph.run(**args, research_plan=plan)
        for changed in (build_research_plan("解释其他问题", "auto"),
                        build_research_plan("解释当前配置", "auto", allow_public_refresh=True)):
            with self.assertRaisesRegex(ValueError, "计划"):
                graph.run(**args, research_plan=changed)
        report = graph.run(**args, research_plan=plan)
        self.assertTrue(report["graph_trace"]["resumed_from_checkpoint"])
        self.assertEqual(report["research_plan"], plan)

    def test_personal_questions_do_not_invent_conditions(self):
        plan = build_research_plan("这只基金适合我加仓吗？", "auto", investor_context={"horizon": "三年不用"})
        self.assertEqual(len(plan["clarification_questions"]), 2)
        self.assertEqual(plan["investor_context"], {"horizon": "三年不用"})

    def test_real_interpreter_initialization_failure_still_completes_focused_graph(self):
        graph = build_graph_service(interpreter=PortfolioResearchInterpretationService())
        with patch("app.services.portfolio_research_interpretation.get_chat_model", side_effect=RuntimeError("offline")):
            report = graph.run(run_id="focused-fallback", user_id="chen", portfolio_id=PORTFOLIO_ID,
                research_plan=build_research_plan("解释集中度", "explain"))
        self.assertEqual(report["generation"]["mode"], "deterministic_fallback")
        self.assertNotIn("市场环境", [row["title"] for row in report["key_findings"]])
        self.assertEqual(len(report["graph_trace"]["completed_nodes"]), 4)

    def test_api_to_real_job_graph_and_idempotency(self):
        install_research_authorization(self, PORTFOLIO_ID)
        jobs = BackgroundJobService(executor=CapturingExecutor())
        self.addCleanup(jobs.shutdown)
        graph = build_graph_service()
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[get_current_principal] = lambda: CurrentPrincipal(
            user_id="chen", display_name="Test", role="user", authenticated=True)
        self.enterContext(patch.object(api, "portfolio_service", graph.portfolios))
        self.enterContext(patch.object(api, "background_job_service", jobs))
        self.enterContext(patch("app.services.portfolio_research_graph.portfolio_research_graph_service", graph))
        with TestClient(app) as client:
            body = {"question": "为什么组合有这个风险？", "research_mode": "auto", "idempotency_key": "plan-api"}
            path = f"/api/v1/portfolios/{PORTFOLIO_ID}/research-runs"
            response = client.post(path, json=body)
            self.assertEqual(response.status_code, 202, response.text)
            job = response.json()["job"]
            self.assertEqual(job["total_steps"], 4)
            completed = JobRunner(jobs.store).run(user_id="chen", job_id=job["id"])
            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.result["research_plan"]["question"], body["question"])
            retry = client.post(path, json=body)
            self.assertEqual(retry.json()["job"]["id"], job["id"])
            for change in ({"question": "比较季报"}, {"allow_public_refresh": True}, {"months": 6}):
                response = client.post(path, json={**body, **change})
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(len(jobs.executor.calls), 1)

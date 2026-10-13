"""季报评测器的离线测试；假模型只验证框架，不作为真实模型成绩。"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
from threading import Event

from fastapi.testclient import TestClient

from app.database import Database
from app.schemas import AgentToolCall, FundReportAnswerReport
from app.services.agent_evals import AgentEvalError
from app.services.fund_report_evals import FundReportEvalService
from app.services.fund_report_eval_grading import grade_report_case, known_regression_signals, TOOL, GRADER_VERSION
from app.services.fund_reports import get_fund_report_reader, fund_report_reader_scope, fund_report_service
from app.services.observability import AgentObservabilityService
from app.services.auth import CurrentPrincipal
from app.services.background_jobs import BackgroundJobService
from app.services.job_runtime import JobRunner
from app.services.agent_execution_context import AgentExecutionContext
from app.main import app
from tests.test_agent_evals import make_run


class GradingTests(unittest.TestCase):
    def setUp(self):
        self.service = FundReportEvalService()
        self.docs = self.service.validate_fixture()
        self.case = self.service._load_suite()["scenarios"][0]
        self.docs = [d for d in self.docs if d["fund_code"] == self.case["fund_code"]]
        self.payload = {"ok": True, "fund_code": self.case["fund_code"], "documents": self.docs,
            "evidence": [], "retrieval": [{"mode":"keyword"}],
            "allocation_changes": [{"label":"基金投资", "denominator":"基金总资产",
                "before":{"ratio_percent":"92.05"}, "after":{"ratio_percent":"92.12"},
                "change_percentage_points":"0.07"}]}

    def grade(self, payload=None, answer=None, *, status="consistent", unanswered=False, empty=False):
        run = make_run(answer=answer or f"92.05%至92.12%，增加0.07个百分点。[原文]({self.docs[0]['source_url']}#page=12)")
        run.tool_calls = [AgentToolCall(tool_name=TOOL, tool_args={}, tool_result=json.dumps(payload or self.payload))]
        # 此处只测评分消费校验记录的行为，记录本身的真实性由answer/loop测试覆盖。
        run.report_answer_check = FundReportAnswerReport(fund_code=self.case["fund_code"], status=status,
            allocations=[] if empty else [{"label": "基金投资"}], unanswered=unanswered) if status else None
        return grade_report_case(self.case, run, self.docs, {"retrieval_mode":"keyword"})

    def test_limited_missing_and_link_only_responses_cannot_pass(self):
        for args in ({"status": "limited"}, {"status": None}, {"empty": True, "unanswered": True}):
            checks, quality = self.grade(**args)
            self.assertTrue(next(c.passed for c in checks if c.name == "引用可定位"))
            self.assertFalse(next(c.passed for c in checks if c.name == "逐轮回答完成"))
            self.assertEqual(quality["metrics"]["answer_delivery"]["value"], 0)

    def test_legitimate_unanswerable_is_distinct_from_failed_generation(self):
        self.case = next(case for case in self.service._load_suite()["scenarios"] if case["category"] == "unanswerable")
        checks, _ = self.grade(empty=True, unanswered=True)
        self.assertTrue(next(c.passed for c in checks if c.name == "逐轮回答完成"))
        checks, _ = self.grade(status="limited", empty=True, unanswered=True)
        self.assertFalse(next(c.passed for c in checks if c.name == "逐轮回答完成"))

    def test_first_turn_limited_cannot_be_hidden_by_successful_followup(self):
        runs = [make_run(answer="材料入口"), make_run(answer="完成")]
        for run, status in zip(runs, ("limited", "consistent")):
            run.report_answer_check = FundReportAnswerReport(fund_code=self.case["fund_code"], status=status, allocations=[{"label": "基金投资"}])
        runs[0].report_answer_check.issues = ["statements[0].before引用不存在、未送入模型或季度不匹配。"]
        combined = SimpleNamespace(**dict(runs[-1]), eval_turns=[{"run": run} for run in runs])
        checks, quality = grade_report_case(self.case, combined, self.docs, {"retrieval_mode":"keyword"})
        self.assertFalse(next(c.passed for c in checks if c.name == "逐轮回答完成"))
        self.assertEqual(quality["metrics"]["answer_delivery"]["value"], .5)
        self.assertEqual(quality["turns"][0]["answer_alignment"]["issues"], runs[0].report_answer_check.issues)
        self.assertFalse(quality["turns"][1]["answer_alignment"]["issues"])
        self.assertEqual(quality["turns"][0]["usage"]["total_tokens"], 150)

    def test_known_regressions_inspect_interpretation_not_incidental_quote_text(self):
        report = FundReportAnswerReport(fund_code="015789", status="consistent", statements=[{
            "topic": "火箭", "interpretation": "两期关注火箭。", "before": [{"quote": "四是IPO进度。"}], "after": []}])
        self.assertFalse(known_regression_signals("report-active-theme", report)[0]["passed"])
        report.statements[0]["interpretation"] = "两期关注火箭；前期还关注IPO进度，后期章节未识别到对应重点。"
        self.assertTrue(known_regression_signals("report-active-theme", report)[0]["passed"])
        report.statements[0]["interpretation"] = "二季度在利率下行后拉长久期。"
        self.assertFalse(known_regression_signals("report-bond-duration", report)[0]["passed"])
        report.statements[0]["interpretation"] = "一季度跟随利率下行小幅降低久期；二季度在经济数据下行后拉长至超配基准。"
        self.assertTrue(known_regression_signals("report-bond-duration", report)[0]["passed"])

    def test_terminal_detail_requires_displayed_support_not_only_title(self):
        from app.schemas import FundReportAnswerReport
        report = FundReportAnswerReport(fund_code="015789", status="consistent", statements=[{
            "topic": "终端应用", "interpretation": "后期展望具体到手机直连和车载通信。",
            "after": [{"quote": "终端应用场景值得关注。"}]}])
        self.assertFalse(known_regression_signals("report-active-theme", report)[1]["passed"])
        ref = report.statements[0]["after"][0]
        ref["context_after"] = [{"quote": "手机直连卫星、车载卫星通信有望进展。"}]
        self.assertTrue(known_regression_signals("report-active-theme", report)[1]["passed"])
        self.assertTrue(known_regression_signals("report-active-theme", report)[1]["manual_criteria"])

    def test_new_grader_has_separate_comparison_identity_but_same_input_cache(self):
        self.assertTrue(self.service._data_fingerprint().endswith(GRADER_VERSION))
        self.assertNotIn(GRADER_VERSION, self.service._input_fingerprint())

    def test_six_verified_documents_and_twenty_cases(self):
        self.assertEqual(len(self.service.validate_fixture()), 6)
        self.assertEqual(len(self.service.suite_info().scenarios), 20)

    def test_auto_checks_do_not_approve_semantic_quality(self):
        checks, quality = self.grade(answer=f"这证明明天一定涨。[来源]({self.docs[0]['source_url']}#page=12)")
        self.assertTrue(all(c.passed for c in checks))
        self.assertEqual(quality["manual_review"]["status"], "pending")
        self.assertNotIn("answer_accuracy", quality["metrics"])

    def test_independent_numeric_gold_rejects_self_consistent_wrong_tool(self):
        payload = deepcopy(self.payload)
        payload["allocation_changes"][0]["after"]["ratio_percent"] = "99.00"
        payload["allocation_changes"][0]["change_percentage_points"] = "6.95"
        checks, quality = self.grade(payload)
        self.assertFalse(next(c.passed for c in checks if c.dimension == "numeric"))
        self.assertEqual(quality["metrics"]["disclosure_numeric"]["value"], 0)

    def test_invented_page_and_cross_fund_documents_fail(self):
        payload = deepcopy(self.payload);payload["documents"][0]["id"] = "other-fund"
        checks, quality = self.grade(payload, answer=f"[来源]({self.docs[1]['source_url']}#page=999)")
        self.assertFalse(next(c.passed for c in checks if c.dimension == "conversation"))
        self.assertFalse(next(c.passed for c in checks if c.dimension == "citations"))

    def test_unanswerable_has_no_fake_perfect_retrieval_score(self):
        self.case = self.service._load_suite()["scenarios"][4]
        _, quality = self.grade(answer="这些季报不能确定今日交易。")
        self.assertIsNone(quality["metrics"]["evidence_recall"]["value"])
        self.assertEqual(quality["manual_review"]["status"], "pending")

    def test_marked_missing_is_not_zero(self):
        self.case = self.service._load_suite()["scenarios"][9]
        self.docs = [d for d in self.service._manifest() if d["fund_code"] == "015789"]
        self.payload.update(fund_code="015789", documents=self.docs, allocation_changes=[{
            "label":"固定收益投资", "before":{"ratio_percent":"0.69"}, "after":{"ratio_percent":"0"},
            "change_percentage_points":"-0.69", "denominator":"基金总资产"}])
        checks, _ = self.grade()
        self.assertFalse(next(c.passed for c in checks if c.dimension == "numeric"))


class FixtureAgent:
    def __init__(self):
        self.requests, self.histories = [], []

    async def chat(self, request, *, history, **kwargs):
        self.requests.append(request);self.histories.append(history)
        # 使用与产品相同的真实Service，只有LLM输出由固定文本替代。
        payload = await asyncio.to_thread(get_fund_report_reader().research,
            request.fund_code, request.message, request.fund_report_ids)
        run = make_run(answer="示例回答仅用于离线框架测试。")
        run.run_id = f"fake-report-{len(self.requests)}"
        run.user_id = request.user_id;run.question=request.message
        run.tool_calls = [AgentToolCall(tool_name=TOOL, tool_args={"fund_code":request.fund_code}, tool_result=json.dumps(payload))]
        return run


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The fixture agent exercises scoring and persistence, not an external
        # authorization service. Resource delegation is covered separately.
        from app.config import settings
        authorization = patch.object(settings, "agent_task_authorization_enabled", False)
        authorization.start()
        self.addCleanup(authorization.stop)

    async def test_followup_timeout_preserves_first_turn_and_known_usage(self):
        from app.services.fund_report_runtime import ReportExecution, ReportExecutionError
        class FailingFollowup(FixtureAgent):
            async def chat(self, request, **kwargs):
                if self.requests:
                    execution = ReportExecution(run_id="timeout-second")
                    execution.record_attempt({"usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}})
                    execution.record_attempt({"usage": None})
                    raise ReportExecutionError(execution, "synthesis", timed_out=True)
                return await super().chat(request, **kwargs)
        with TemporaryDirectory() as directory:
            db = Database(f"sqlite:///{directory}/observations.db")
            try:
                obs = AgentObservabilityService(db)
                service = FundReportEvalService(agent=FailingFollowup(), observability=obs)
                result = await service.run_suite(user_id="reviewer", case_ids=["report-index-followup"], retrieval_mode="keyword")
                row = obs.get_eval_run(user_id="reviewer", run_id=result.id).results[0]
                self.assertFalse(row.passed)
                self.assertFalse(row.report_quality)
                self.assertEqual(row.usage.total_tokens, 180)
                self.assertEqual(len(row.execution_diagnostics["turns"]), 2)
                self.assertEqual(row.execution_diagnostics["turns"][0]["answer"], "示例回答仅用于离线框架测试。")
                self.assertEqual(row.execution_diagnostics["turns"][1]["answer_status"], "timeout")
                self.assertFalse(row.execution_diagnostics["usage_complete"])
                self.assertEqual(row.execution_diagnostics["run_config"]["retrieval_mode"], "keyword")
            finally:
                db.engine.dispose()

    async def test_keyword_two_turn_run_isolated_and_review_persists(self):
        with TemporaryDirectory() as directory:
            db = Database(f"sqlite:///{directory}/observations.db")
            obs = AgentObservabilityService(db);agent = FixtureAgent()
            service = FundReportEvalService(agent=agent, observability=obs)
            with patch("app.services.fund_reports.FundReportService._embedder", side_effect=AssertionError("关键词模式不能调用embedding")):
                result = await service.run_suite(user_id="reviewer", case_ids=["report-index-followup"], retrieval_mode="keyword")
            self.assertIsNone(result.results[0].error)
            self.assertEqual(len(agent.requests), 2)
            self.assertEqual(agent.requests[0].fund_report_ids, agent.requests[1].fund_report_ids)
            self.assertTrue(agent.histories[1])
            self.assertIn(":keyword", result.data_fingerprint)
            self.assertIsNone(service.dashboard(user_id="reviewer").latest_run)
            self.assertEqual(service.dashboard(user_id="reviewer", retrieval_mode="keyword").latest_run.id, result.id)
            self.assertIs(get_fund_report_reader(), fund_report_service)
            old_score = result.score
            reviewed = obs.review_report_case(user_id="reviewer", run_id=result.id,
                case_id="report-index-followup", status="needs_fix", note="离线假答案没有引用原文")
            self.assertEqual(reviewed.score, old_score)
            self.assertEqual(reviewed.report_metrics["manual_review"]["needs_fix"], 1)
            self.assertEqual(obs.get_eval_run(user_id="reviewer", run_id=result.id).results[0].report_quality["manual_review"]["history"][0]["reviewed_by"], "reviewer")
            with self.assertRaises(LookupError):
                obs.get_eval_run(user_id="other", run_id=result.id)
            with patch("app.routers.evals.agent_observability_service", obs):
                principal = CurrentPrincipal("reviewer", "Reviewer", "admin", True)
                auth = patch("app.services.auth.auth_service.authenticate", side_effect=lambda _: principal)
                auth.start()
                try:
                    client = TestClient(app)
                    response = client.put(f"/api/v1/evals/runs/{result.id}/cases/report-index-followup/review",
                        json={"user_id":"other", "status":"accepted", "note":"已对照原文"})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["user_id"], "reviewer")
                    principal = CurrentPrincipal("other", "Other", "admin", True)
                    self.assertEqual(client.get(f"/api/v1/evals/runs/{result.id}").status_code, 404)
                finally:
                    auth.stop()
            db.engine.dispose()

    async def test_contextvar_isolation_and_error_reset(self):
        reader = object()
        async def evaluation():
            with fund_report_reader_scope(reader):
                await asyncio.sleep(0)
                self.assertIs(await asyncio.to_thread(get_fund_report_reader), reader)
            self.assertIs(get_fund_report_reader(), fund_report_service)
        async def product():
            await asyncio.sleep(0)
            self.assertIs(get_fund_report_reader(), fund_report_service)
        await asyncio.gather(evaluation(), product())

    async def test_invalid_case_fails_before_index_cost(self):
        service = FundReportEvalService()
        with patch.object(service, "_prepare_reader", side_effect=AssertionError("不应建索引")):
            with self.assertRaises(AgentEvalError):
                await service.run_suite(user_id="reviewer", case_ids=["invalid-case"])

    async def test_cancel_waits_for_index_thread_before_removing_database(self):
        service = FundReportEvalService()
        entered, release, exited = Event(), Event(), Event()
        paths = []
        def prepare(database, docs, progress, mode):
            path = Path(database.engine.url.database); paths.append(path.parent)
            entered.set()
            try:
                release.wait(2)
                self.assertTrue(path.parent.exists())
                progress("检查取消信号")
            finally:
                exited.set()
        with patch.object(service, "_prepare_reader", side_effect=prepare):
            task = asyncio.create_task(service.run_suite(user_id="test", case_ids=["report-index-allocation"]))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(exited.is_set())
        self.assertFalse(paths[0].exists())

    async def test_complete_hybrid_index_is_reused_but_partial_index_is_rebuilt(self):
        service = FundReportEvalService()
        docs = service._manifest()[:1]
        from app.services.fund_report_source import page_chunks
        from app.services.fund_reports import FundReportService
        ids = [row["evidence_id"] for row in page_chunks(docs[0])]
        for stored, expected_calls in ((ids, 0), (ids[:1], 1)):
            with TemporaryDirectory() as directory:
                database = Database(f"sqlite:///{directory}/eval.db")
                collection = Mock(); collection.get.return_value = {"ids":stored}
                with patch.object(FundReportService, "_collection", return_value=collection), patch.object(FundReportService, "index_document") as index:
                    await asyncio.to_thread(service._prepare_reader, database, docs, lambda _:None, "hybrid")
                    self.assertEqual(index.call_count, expected_calls)
                database.engine.dispose()


class JobApiTests(unittest.TestCase):
    def setUp(self):
        self.executor = Mock(backend_name="local")
        self.executor.submit.return_value = "local-report-test"
        self.jobs = BackgroundJobService(executor=self.executor)
        self.patcher = patch("app.routers.evals.background_job_service", self.jobs)
        self.patcher.start()
        self.principal = CurrentPrincipal("reviewer", "Reviewer", "admin", True)
        self.auth = patch("app.services.auth.auth_service.authenticate", side_effect=lambda _: self.principal)
        self.auth.start()
        self.client = TestClient(app)
        self.body = {"user_id":"spoofed", "case_ids":["report-index-allocation"], "idempotency_key":"report-test-key", "retrieval_mode":"keyword"}

    def tearDown(self):
        self.patcher.stop()
        self.auth.stop()

    def test_admin_owner_and_idempotent_job_acceptance(self):
        first = self.client.post("/api/v1/evals/fund-reports/jobs", json=self.body)
        second = self.client.post("/api/v1/evals/fund-reports/jobs", json=self.body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["job"]["id"], second.json()["job"]["id"])
        self.assertEqual(first.json()["job"]["user_id"], "reviewer")
        self.assertEqual(first.json()["job"]["max_retries"], 0)
        self.assertEqual(self.executor.submit.call_count, 1)
        changed = self.client.post("/api/v1/evals/fund-reports/jobs", json={**self.body, "retrieval_mode":"hybrid"})
        self.assertNotEqual(changed.json()["job"]["id"], first.json()["job"]["id"])

    def test_regular_user_and_invalid_cases_cannot_start_jobs(self):
        self.principal = CurrentPrincipal("ordinary", "Ordinary", "user", True)
        self.assertEqual(self.client.post("/api/v1/evals/fund-reports/jobs", json=self.body).status_code, 403)
        self.principal = CurrentPrincipal("reviewer", "Reviewer", "admin", True)
        self.assertEqual(self.client.post("/api/v1/evals/fund-reports/jobs", json={**self.body,"case_ids":["invalid"]}).status_code, 400)
        self.assertEqual(self.executor.submit.call_count, 0)

    def test_runner_dispatches_exact_cases_and_saves_only_result_reference(self):
        response = self.client.post("/api/v1/evals/fund-reports/jobs", json=self.body)
        job_id = response.json()["job"]["id"]
        received = {}
        async def execute(**kwargs):
            received.update(kwargs)
            kwargs["progress_callback"](1, 1, "完成")
            return SimpleNamespace(id="eval_test", suite_id="fund_report_rag_v1", passed_cases=1,total_cases=1,score=100)
        with patch("app.services.fund_report_evals.fund_report_eval_service.run_suite", side_effect=execute):
            JobRunner(self.jobs.store).run(user_id="reviewer", job_id=job_id)
        job = self.jobs.get_job(user_id="reviewer", job_id=job_id)["job"]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["eval_run_id"], "eval_test")
        self.assertEqual(received["case_ids"], self.body["case_ids"])
        self.assertEqual(received["retrieval_mode"], "keyword")

    def test_job_type_cannot_be_started_from_untrusted_product_tool(self):
        with self.assertRaises(ValueError):
            self.jobs.start_job(user_id="reviewer", title="bad", job_type="fund_report_eval",
                execution_context=AgentExecutionContext.product(user_id="reviewer"))


if __name__ == "__main__":
    unittest.main()

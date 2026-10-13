"""用真实 LangGraph + 假模型验证多角色投研，不访问网络、不消耗 API 额度。"""

from collections import Counter
from contextlib import ExitStack
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock
from types import SimpleNamespace
from typing import TypedDict, get_type_hints
import unittest
from unittest.mock import Mock, patch

import httpx
from langgraph.graph import START, END, StateGraph

from app.database import Database
from app.config import settings
from app.schemas import AgentChatRequest
from app.services.ai_governance_context import AIControlError, ai_execution_scope, current_ai_scope
from app.services.ai_capacity import MemoryAICapacity
from app.services.ai_usage import AIUsageService
from app.services.governed_model import GovernedChatOpenAI
from app.services.portfolio_report_context import resolve_report_context
from app.services.portfolio_research_checkpoints import create_portfolio_research_checkpoint_runtime
from app.services.portfolio_research_committee import PortfolioResearchCommitteeService
from app.services.portfolio_research_graph import PortfolioResearchState, PortfolioResearchContext
from tests.test_portfolio_research_graph import PORTFOLIO_ID, build_graph_service, FailOncePortfolioInterpreter
from tests.test_portfolio_research_interpretation import research_inputs
from tests.test_reliable_research_checkpoints import build_durable_graph
from tests.test_ai_governance import model_response


def findings():
    return [
        {"tone": "supportive", "title": "历史业绩有支撑", "detail": "已有收益记录提供了历史观察依据。",
         "metric_keys": ["risk.period_return_pct"], "evidence_ids": [], "fund_codes": []},
        {"tone": "caution", "title": "配置仍需关注集中度", "detail": "集中暴露的风险不能被历史收益掩盖。",
         "metric_keys": ["concentration.max_weight_pct"], "evidence_ids": [], "fund_codes": []},
    ]


def legacy_v7_graph(interpreter=None):
    """真实旧八节点拓扑和旧 State，没有 research_mode 或任何 committee channel。"""
    service = build_graph_service(interpreter=interpreter)
    service.GRAPH_VERSION = "portfolio-research-v7"
    fields = {key: value for key, value in get_type_hints(PortfolioResearchState, include_extras=True).items()
        if key != "research_mode" and not key.startswith("committee")}
    legacy_state = TypedDict("LegacyResearchState", fields, total=False)
    builder = StateGraph(legacy_state, context_schema=PortfolioResearchContext)
    branches = ["calculate_assessment", "calculate_quant_risk", "calculate_performance",
        "load_market_context", "load_decision_history"]
    for node in ["load_portfolio", *branches, "interpret_with_llm", "compose_report"]:
        builder.add_node(node, getattr(service, f"_{node}"))
    builder.add_edge(START, "load_portfolio")
    for node in branches:
        builder.add_edge("load_portfolio", node)
    builder.add_edge(branches, "interpret_with_llm")
    builder.add_edge("interpret_with_llm", "compose_report")
    builder.add_edge("compose_report", END)
    service.graph = builder.compile(checkpointer=service.checkpointer)
    return service


class CommitteeModel:
    """根据阶段返回固定 JSON，记录提示词、预算参数和传播到线程的可信身份。"""
    def __init__(self, edits=None, fail_once=None, parallel=False):
        self.edits = edits or {}
        self.fail_once = fail_once
        self.calls = []
        self.options = []
        self.lock = Lock()
        self.barrier = Barrier(2) if parallel else None

    def create(self, **kwargs):
        self.options.append(kwargs)
        return self

    def with_structured_output(self, schema, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        value = json.loads(messages[-1].content)
        phase = value["task"]
        scope = current_ai_scope()
        with self.lock:
            self.calls.append({"phase": phase, "input": value, "user": scope.user_id,
                "run_id": scope.run_id, "kwargs": kwargs, "system": messages[0].content})
            if self.fail_once == phase:
                self.fail_once = None
                raise AIControlError("test_budget", "模拟额度拒绝")
        if self.barrier and phase.endswith("_analysis"):
            self.barrier.wait(timeout=3)
        if phase.endswith("_analysis"):
            result = {"headline": "支撑与风险需要同时看", "summary": "结合历史证据观察，保留数据缺口。",
                "key_findings": findings(), "uncertainties": ["尚不清楚用户的持有期限。"]}
        elif phase.endswith("_review"):
            result = {"responses": [{"target_claim_id": row["claim_id"], "verdict": "qualify",
                "finding": findings()[index]} for index, row in enumerate(value["discussion"]["opposing_position"]["key_findings"])],
                "uncertainties": ["仍需观察后续披露。"]}
        else:
            result = {"current_state": "cautious", "confidence": 0.6, "headline": "支持依据与集中风险并存",
                "summary": "可保留已有观察依据，但不能忽略集中暴露。", "key_findings": findings(),
                "conflicts": ["对配置合理性的判断仍有条件差异。"], "uncertainties": [],
                "conditional_view": "需要结合个人期限与风险承受能力。", "follow_up_items": ["关注后续披露。"]}
        edit = self.edits.get(phase)
        if isinstance(edit, Exception):
            raise edit
        return edit(result) if edit else result


class CommitteeTests(unittest.TestCase):
    def run_graph(self, graph, model, **kwargs):
        with patch("app.services.portfolio_research_committee.get_chat_model", side_effect=model.create):
            with ai_execution_scope("chen", "test-research", "test"):
                return graph.run(run_id="committee-test", user_id="chen", portfolio_id=PORTFOLIO_ID,
                    research_mode="committee", **kwargs)

    def test_independent_parallel_roles_one_review_and_synthesis(self):
        graph = build_graph_service()
        model = CommitteeModel(parallel=True)
        nodes = []
        report = self.run_graph(graph, model, progress_callback=lambda name, _: nodes.append(name))
        committee = report["committee"]
        self.assertEqual(committee["status"], "completed")
        self.assertEqual(committee["snapshot_id"], report["research_snapshot"]["snapshot_id"])
        self.assertEqual(len(nodes), 13)
        self.assertEqual(committee["model_calls"], 5)
        self.assertEqual(Counter(row["phase"] for row in model.calls), Counter({
            "opportunity_analysis": 1, "risk_analysis": 1, "opportunity_review": 1,
            "risk_review": 1, "committee_synthesis": 1,
        }))
        for call in model.calls:
            self.assertEqual(call["user"], "chen")
            self.assertEqual(call["run_id"], "test-research")
            self.assertEqual(call["kwargs"]["max_tokens"], 2200)
            self.assertIn("观点", call["system"])
            if call["phase"].endswith("_analysis"):
                self.assertNotIn("discussion", call["input"])
        self.assertTrue(all(row["max_retries"] == 0 for row in model.options))
        self.assertEqual(report["generation"]["mode"], "multi_agent_committee")
        self.assertIn("尚不清楚用户的持有期限。", report["model_interpretation"]["uncertainties"])
        self.assertEqual(committee["roles"][0]["key_findings"][0]["metric_references"][0]["value"], 4.2)
        # 完成后再打开/重试直接读检查点，不重复消耗模型。
        self.run_graph(graph, model)
        self.assertEqual(len(model.calls), 5)

    def test_standard_mode_never_calls_committee(self):
        graph = build_graph_service()
        with patch("app.services.portfolio_research_committee.get_chat_model") as create:
            result = graph.run(run_id="standard", user_id="chen", portfolio_id=PORTFOLIO_ID)
        create.assert_not_called()
        self.assertNotIn("committee", result)
        self.assertEqual(result["research_mode"], "standard")

    def test_all_five_structured_calls_use_the_same_governed_ledger(self):
        # 使用真实 LangChain JSON 解析和额度账本，只在 HTTP 传输层替换供应商。
        # 这能发现 invoke 的输出上限没有实际传到 SDK 等单纯 FakeModel 看不到的问题。
        with ExitStack() as stack:
            directory = stack.enter_context(TemporaryDirectory())
            db = Database(f"sqlite:///{Path(directory) / 'usage.db'}")
            stack.callback(db.engine.dispose)
            ledger, capacity = AIUsageService(db), MemoryAICapacity()
            stack.enter_context(patch("app.services.governed_model.get_ai_capacity", return_value=capacity))
            stack.enter_context(patch("app.services.governed_model.get_ai_usage_service", return_value=ledger))
            for name, value in {"ai_governance_enabled": True, "ai_max_output_tokens": 2500,
                "ai_daily_token_limit": 500000, "ai_run_token_limit": 500000,
                "ai_model_requests_per_minute": 100, "ai_user_concurrency": 2}.items():
                stack.enter_context(patch.object(settings, name, value))
            supplier, bodies = CommitteeModel(), []
            def handler(request):
                body = json.loads(request.content)
                bodies.append(body)
                value = supplier.invoke([SimpleNamespace(content=row["content"]) for row in body["messages"]])
                return model_response(json.dumps(value, ensure_ascii=False))
            client = stack.enter_context(httpx.Client(transport=httpx.MockTransport(handler)))
            model = GovernedChatOpenAI(model="test-model", api_key="offline-placeholder",
                base_url="https://model.invalid/v1", http_client=client,
                governance_provider="deepseek-test", governance_retries=0)
            stack.enter_context(patch("app.services.portfolio_research_committee.get_chat_model", return_value=model))
            stack.enter_context(ai_execution_scope("chen", "shared-committee-budget", "test"))
            report = build_graph_service().run(run_id="real-sdk-test", user_id="chen",
                portfolio_id=PORTFOLIO_ID, research_mode="committee")
            self.assertEqual(report["committee"]["status"], "completed")
            self.assertEqual(len(bodies), 5)
            self.assertTrue(all(body["max_tokens"] == 2200 for body in bodies))
            summary = ledger.summary(user_id="chen")
            self.assertEqual(summary["used_tokens"], 75)
            self.assertEqual(len(summary["calls"]), 5)
            self.assertTrue(all(row["run_id"] == "shared-committee-budget" for row in summary["calls"]))
            self.assertEqual(capacity.snapshot("chen")["running"], 0)

    def test_invalid_numbers_references_and_fund_scope_are_not_persisted(self):
        def unknown(data):
            data["key_findings"][0]["metric_keys"] = ["invented.future_return"]
            return data
        def scope(data):
            data["key_findings"][0]["fund_codes"] = ["110020"]
            return data
        def number(data):
            data["summary"] = "未来收益为 987654.321%。"
            return data
        def borrowed_number(data):
            # 60.00 确实在集中度里存在，但不能拿来冒充实际只有 4.2 的区间收益。
            data["key_findings"][0]["detail"] = "区间收益为60.00%。"
            return data
        for edit in (unknown, scope, number, borrowed_number):
            with self.subTest(edit=edit.__name__):
                model = CommitteeModel(edits={"risk_analysis": edit})
                result = self.run_graph(build_graph_service(), model)
                self.assertEqual(result["committee"]["status"], "degraded")
                self.assertFalse(result["generation"]["model_used"])
                self.assertEqual(result["committee"]["roles"][1]["key_findings"], [])
                self.assertEqual(len(model.calls), 2)
                self.assertNotIn("987654", json.dumps(result))

    def test_review_must_cover_existing_opposing_claims_exactly_once(self):
        def tamper(data):
            data["responses"][0]["target_claim_id"] = "nonexistent"
            return data
        result = self.run_graph(build_graph_service(), CommitteeModel(edits={"risk_review": tamper}))
        self.assertEqual(result["committee"]["reviews"][1]["status"], "failed")
        self.assertEqual(result["committee"]["synthesis"]["model_calls"], 0)

    def test_chair_cannot_hide_disagreements_or_invent_evidence(self):
        for conflicts in ([], ["   ", "\t"]):
            with self.subTest(conflicts=conflicts):
                def erase(data):
                    data["conflicts"] = conflicts
                    return data
                result = self.run_graph(build_graph_service(), CommitteeModel(edits={"committee_synthesis": erase}))
                self.assertEqual(result["committee"]["status"], "degraded")
                self.assertEqual(result["generation"]["mode"], "deterministic_fallback")
                self.assertEqual(result["committee"]["reviews"][0]["responses"][0]["verdict"], "qualify")

    def test_verified_instrument_names_remain_valid_in_prose(self):
        def mention_name(data):
            data["summary"] = "沪深300联接提供的历史支撑仍需结合风险理解。"
            return data
        result = self.run_graph(build_graph_service(), CommitteeModel(edits={"opportunity_analysis": mention_name}))
        self.assertEqual(result["committee"]["status"], "completed")

    def test_provider_failure_has_no_hidden_retry_and_exposes_no_error_body(self):
        model = CommitteeModel(edits={"risk_analysis": RuntimeError("sensitive-provider-body")})
        result = self.run_graph(build_graph_service(), model)
        self.assertEqual(len(model.calls), 2)
        self.assertNotIn("sensitive-provider-body", json.dumps(result))
        self.assertTrue(result["committee"]["warnings"])

    def test_daily_briefing_and_changed_checkpoint_mode_are_rejected(self):
        graph = build_graph_service()
        model = CommitteeModel()
        with self.assertRaisesRegex(ValueError, "每日简报"):
            self.run_graph(graph, model, daily_briefing={})
        self.assertFalse(model.calls)
        self.run_graph(graph, model)
        with self.assertRaisesRegex(ValueError, "研究模式"):
            graph.run(run_id="committee-test", user_id="chen", portfolio_id=PORTFOLIO_ID)

    def test_old_v7_standard_graph_can_resume_after_upgrade(self):
        old = legacy_v7_graph(FailOncePortfolioInterpreter())
        args = dict(run_id="v7-interrupted", user_id="chen", portfolio_id=PORTFOLIO_ID)
        with self.assertRaises(RuntimeError):
            old.run(**args)
        self.assertNotIn("research_mode", old.checkpoint_state(run_id=args["run_id"]))
        new = build_graph_service(snapshots=old.snapshots)
        new.checkpointer = old.checkpointer
        new.graph = new._build_graph()
        new._get_or_capture_snapshot = Mock(side_effect=AssertionError("恢复旧任务不能重抓输入"))
        result = new.run(**args)
        self.assertEqual(result["research_snapshot"]["graph_version"], "portfolio-research-v7")
        self.assertEqual(result["research_mode"], "standard")
        self.assertNotIn("committee", result)
        with self.assertRaisesRegex(ValueError, "研究模式"):
            new.run(**args, research_mode="committee")

    def test_completed_v7_checkpoint_can_be_read_without_regenerating(self):
        old = legacy_v7_graph()
        args = dict(run_id="v7-completed", user_id="chen", portfolio_id=PORTFOLIO_ID)
        previous = old.run(**args)
        new = build_graph_service(snapshots=old.snapshots)
        new.checkpointer = old.checkpointer
        new.graph = new._build_graph()
        new.interpreter.interpret = Mock(side_effect=AssertionError("已完成旧图不能重调模型"))
        result = new.run(**args)
        self.assertEqual(result["summary"], previous["summary"])
        self.assertEqual(result["graph_version"], "portfolio-research-v7")

    def test_followup_reuses_role_evidence_without_model_calls(self):
        model = CommitteeModel()
        report = self.run_graph(build_graph_service(), model)
        reader = Mock()
        reader.read_report.return_value = report
        _, context = resolve_report_context(AgentChatRequest(user_id="chen", message="他们为什么不同意？",
            portfolio_id=PORTFOLIO_ID, research_job_id=report["run_id"]), {}, reader=reader)
        self.assertIn("risk:initial:1", context["content"])
        self.assertIn("metric_references", context["content"])
        self.assertEqual(len(model.calls), 5)

    def test_budget_rejection_propagates_and_parallel_result_survives_sqlite_takeover(self):
        # 真实 SQLite saver 和快照，模拟 Worker 重启后复制到新的租约分区。
        with TemporaryDirectory() as directory:
            db = Database(f"sqlite:///{Path(directory) / 'data.db'}")
            url = f"sqlite:///{Path(directory) / 'checkpoints.db'}"
            runtime = create_portfolio_research_checkpoint_runtime(backend="sqlite", database_url=url)
            graph = build_durable_graph(db, runtime)
            model = CommitteeModel(fail_once="risk_analysis")
            try:
                with self.assertRaises(AIControlError):
                    self.run_graph(graph, model, checkpoint_thread_id="first-worker")
                self.assertEqual(graph.checkpoint_info(run_id="committee-test", checkpoint_thread_id="first-worker")["status"], "resumable")
            finally:
                runtime.close()
            runtime = create_portfolio_research_checkpoint_runtime(backend="sqlite", database_url=url)
            graph = build_durable_graph(db, runtime)
            try:
                graph.prepare_attempt_checkpoint(target_thread_id="second-worker", source_thread_ids=["first-worker"])
                graph._get_or_capture_snapshot = Mock(side_effect=AssertionError("不可重新抓数据"))
                result = self.run_graph(graph, model, checkpoint_thread_id="second-worker")
                counts = Counter(row["phase"] for row in model.calls)
                self.assertEqual(counts["opportunity_analysis"], 1)
                self.assertEqual(counts["risk_analysis"], 2)
                self.assertEqual(result["committee"]["status"], "completed")
            finally:
                runtime.close()
                db.engine.dispose()

    def test_cancellation_is_not_converted_to_a_model_fallback(self):
        service = PortfolioResearchCommitteeService()
        evidence = service.prepare(research_inputs())
        def cancelled():
            raise RuntimeError("job-cancelled")
        with ai_execution_scope("chen", "cancel", "test", check_cancelled=cancelled):
            with self.assertRaisesRegex(RuntimeError, "job-cancelled"):
                service.analyze("opportunity", evidence)


if __name__ == "__main__":
    unittest.main()

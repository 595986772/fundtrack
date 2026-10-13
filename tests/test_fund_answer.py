"""事实核对的离线测试：故意给错答案，不依赖真实大模型或在线数据源。"""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.messages import AIMessage

from app.schemas import AgentChatRequest, AgentToolCall, FundAnswerReport
from app.services.agent_routing import AgentRequestRoute
from app.services.fund_answer import FundAnswerCheck, build_answer_facts, render_answer_text
from app.services.investment_context import InvestmentContext, InvestmentFact, investment_context_scope
from app.services.recovery import ModelCallResult
from tests.test_financial_research_brief import build_service


def brief():
    return build_service().build_fund(fund_code="012345", question="全面分析这只基金的风险")


def valid_draft(payload):
    fact = payload["answer_facts"][0]
    return {
        "facts": [{key: fact[key] for key in ("evidence_id", "fund_code", "value", "unit", "period")}],
        "judgments": [{
            "text": "这只基金更适合作为有波动承受能力的长期配置候选，暂不能确认适合你。",
            "supporting_evidence": [{"evidence_id": fact["evidence_id"], "explanation": "区间历史收益提供了表现参考，但不能保证后续收益。"}],
            "counter_evidence": [], "supporting_evidence_gap": "",
            "counter_evidence_gap": "本轮未找到适用的反向证据，不代表不存在反面情况。",
            "conditions": [{"basis": "assumption", "text": "需要能承受市场波动，并且没有近期用款安排。", "background_fields": []}],
            "change_triggers": ["资金用途改变或最新披露的持仓结构发生明显变化时，需要重新评估。"],
        }],
        "uncertainties": ["后续市场走势仍不确定。"],
    }


class FactCheckTests(unittest.TestCase):
    def setUp(self):
        self.payload = brief()
        self.checker = FundAnswerCheck(self.payload)

    def test_valid_draft_uses_canonical_values_and_preserves_gaps(self):
        self.checker.gaps = ["投资期限：尚未提供"]
        report = self.checker.finish(json.dumps(valid_draft(self.payload)), attempts=0, initial_issues=[])
        self.assertEqual(report.status, "consistent")
        self.assertEqual(report.version, "fund-answer-v2")
        self.assertEqual(report.facts[0].value, "8.50")
        self.assertIn("投资期限：尚未提供", report.uncertainties)
        self.assertIn(report.facts[0].evidence_id, render_answer_text(report))

    def test_wrong_fund_value_unit_period_and_unknown_reference(self):
        for key, value in (("fund_code", "999999"), ("value", "85"), ("unit", "元"),
                           ("period", "未来一年"), ("evidence_id", "fact:fake")):
            with self.subTest(key=key):
                draft = valid_draft(self.payload)
                draft["facts"][0][key] = value
                _, issues = self.checker.check(json.dumps(draft))
                self.assertTrue(issues)
                self.assertIn(key, " ".join(issues))

    def test_equivalent_decimal_allowed_but_sign_and_scale_not_allowed(self):
        for value, valid in (("8.5", True), ("-8.50", False), ("0.085", False), ("8.5%", False), ("NaN", False)):
            draft = valid_draft(self.payload)
            draft["facts"][0]["value"] = value
            self.assertEqual(not self.checker.check(json.dumps(draft))[1], valid)

    def test_malformed_json_and_free_factual_field_rejected(self):
        self.assertTrue(self.checker.check("输出被截断 { ")[1])
        draft = valid_draft(self.payload)
        draft["unchecked_facts"] = "预计一定上涨"
        self.assertTrue(self.checker.check(json.dumps(draft))[1])

    def test_unknown_judgment_reference_rejected(self):
        draft = valid_draft(self.payload)
        draft["judgments"][0]["supporting_evidence"][0]["evidence_id"] = "risk:fake"
        self.assertIn("不存在", " ".join(self.checker.check(json.dumps(draft))[1]))

    def test_remaining_conflict_removes_whole_draft_not_just_bad_fact(self):
        draft = valid_draft(self.payload)
        draft["facts"][0]["value"] = "999"
        draft["judgments"][0]["text"] = "因为错误的收益而加仓"
        report = self.checker.finish(json.dumps(draft), attempts=1, initial_issues=["初始错误"])
        self.assertEqual(report.status, "limited")
        self.assertEqual(report.judgments, [])
        self.assertNotIn("999", render_answer_text(report))
        self.assertNotIn("因为错误的收益", render_answer_text(report))

    def test_judgment_only_citation_also_gets_expandable_fact(self):
        draft = valid_draft(self.payload)
        other = self.payload["answer_facts"][1]["evidence_id"]
        draft["judgments"][0]["supporting_evidence"][0]["evidence_id"] = other
        report = self.checker.finish(json.dumps(draft), attempts=0, initial_issues=[])
        self.assertIn(other, [f.evidence_id for f in report.facts])

    def test_conditional_investment_opinion_is_not_blocked(self):
        draft = valid_draft(self.payload)
        draft["judgments"][0]["text"] = "若可承受波动，可考虑分批买入；仓位取决于你的资金用途。"
        self.assertEqual(self.checker.check(json.dumps(draft))[1], [])

    def test_missing_dates_or_values_do_not_create_fake_zero(self):
        summary = {"historical_risk": {"period_return_pct": 0}, "disclosure": {}}
        self.assertEqual(build_answer_facts("012345", summary), [])
        summary["historical_risk"].update(period_start="2026-01-01", period_end="2026-02-01")
        self.assertEqual(build_answer_facts("012345", summary)[0]["value"], "0.00")

    def test_empty_catalog_is_not_labelled_verified(self):
        self.payload["answer_facts"] = []
        report = FundAnswerCheck(self.payload).finish(
            '{"facts":[],"judgments":[],"uncertainties":[]}', attempts=0, initial_issues=[],
        )
        self.assertEqual(report.status, "limited")

    def test_only_successful_single_fund_is_in_scope(self):
        call = AgentToolCall(tool_name="get_fund_research_brief_tool", tool_args={}, tool_result=json.dumps(self.payload))
        self.assertIsNotNone(FundAnswerCheck.from_tool_calls([call]))
        self.assertIsNone(FundAnswerCheck.from_tool_calls([call.model_copy(update={"is_error": True})]))
        other = json.loads(call.tool_result)
        other["scope"]["fund_code"] = "999999"
        self.assertIsNone(FundAnswerCheck.from_tool_calls([call, call.model_copy(update={"tool_result": json.dumps(other)})]))

    def test_fact_ids_are_real_tool_citations_and_change_with_data(self):
        from app.services.evidence import extract_answer_evidence_ids, extract_tool_evidence_ids

        call = AgentToolCall(tool_name="get_fund_research_brief_tool", tool_args={}, tool_result=json.dumps(self.payload))
        original_id = self.payload["answer_facts"][0]["evidence_id"]
        self.assertIn(original_id, extract_tool_evidence_ids([call]))
        self.assertEqual(extract_answer_evidence_ids(f"`{original_id}`"), [original_id])
        self.payload["evidence_summary"]["historical_risk"]["period_return_pct"] = 10.0
        changed = build_answer_facts("012345", self.payload["evidence_summary"])
        self.assertNotEqual(original_id, changed[0]["evidence_id"])

    def test_copied_text_retains_source_meaning_and_judgment_reference(self):
        draft = valid_draft(self.payload)
        source_fact = next(row for row in self.payload["answer_facts"] if row["source_url"])
        draft["facts"] = [{key: source_fact[key] for key in ("evidence_id", "fund_code", "value", "unit", "period")}]
        draft["judgments"][0]["supporting_evidence"][0]["evidence_id"] = source_fact["evidence_id"]
        report = self.checker.finish(json.dumps(draft), attempts=0, initial_issues=[])
        text = render_answer_text(report)
        self.assertIn(source_fact["source_url"], text)
        self.assertIn(source_fact["source_path"], text)
        self.assertIn(source_fact["meaning"], text)
        self.assertGreaterEqual(text.count(source_fact["evidence_id"]), 2)

    def test_support_and_counter_keep_their_own_scope_and_reason(self):
        draft = valid_draft(self.payload)
        # 反向依据选披露日不同的持仓事实，不能被改写成风险指标的统计期。
        fact = next(row for row in self.payload["answer_facts"] if ".top_securities." in row["source_path"])
        judgment = draft["judgments"][0]
        judgment["counter_evidence"] = [{"evidence_id": fact["evidence_id"], "explanation": "持仓暴露较集中，削弱将它作为分散配置的理由。"}]
        judgment["counter_evidence_gap"] = ""
        report = self.checker.finish(json.dumps(draft), attempts=0, initial_issues=[])
        self.assertEqual(report.status, "consistent")
        self.assertEqual(report.judgments[0].evidence_ids, [draft["facts"][0]["evidence_id"], fact["evidence_id"]])
        self.assertIn(fact["period"], [row.period for row in report.facts])
        self.assertNotEqual(report.facts[0].period, fact["period"])
        copied = render_answer_text(report)
        for value in ["支持依据：", "反向依据：", judgment["counter_evidence"][0]["explanation"],
                      "适用条件（模型假设）", judgment["change_triggers"][0]]:
            self.assertIn(value, copied)

    def test_missing_evidence_needs_explicit_gap_not_invented_balance(self):
        for category in ("supporting_evidence", "counter_evidence"):
            draft = valid_draft(self.payload)
            draft["judgments"][0][category] = []
            draft["judgments"][0][category + "_gap"] = "   "
            self.assertIn(category + "_gap", " ".join(self.checker.check(json.dumps(draft))[1]))
            draft["judgments"][0][category + "_gap"] = "本轮缺少适用依据，该观点暂作为假设。"
            self.assertEqual(self.checker.check(json.dumps(draft))[1], [])

    def test_blank_explanations_conditions_and_triggers_rejected(self):
        for path in ("explanation", "condition", "trigger", "text"):
            with self.subTest(path=path):
                draft = valid_draft(self.payload)
                row = draft["judgments"][0]
                if path == "explanation":
                    row["supporting_evidence"][0][path] = "   "
                elif path == "condition":
                    row["conditions"][0]["text"] = "   "
                elif path == "trigger":
                    row["change_triggers"][0] = "   "
                else:
                    row[path] = "   "
                self.assertTrue(self.checker.check(json.dumps(draft))[1])
        for key in ("conditions", "change_triggers"):
            draft = valid_draft(self.payload)
            draft["judgments"][0][key] = []
            self.assertIn(key, " ".join(self.checker.check(json.dumps(draft))[1]))

    def test_same_fact_can_have_two_sides_but_missing_counter_ref_is_rejected(self):
        draft = valid_draft(self.payload)
        row = draft["judgments"][0]
        row["counter_evidence"] = [{"evidence_id": draft["facts"][0]["evidence_id"], "explanation": "同一历史区间不能代表所有市场环境。"}]
        row["counter_evidence_gap"] = ""
        self.assertEqual(self.checker.check(json.dumps(draft))[1], [])
        row["counter_evidence"][0]["evidence_id"] = "fact:invented"
        self.assertIn("不存在", " ".join(self.checker.check(json.dumps(draft))[1]))

    def test_cross_fund_fact_cannot_be_used_as_support_or_counter(self):
        foreign = dict(self.payload["answer_facts"][1], fund_code="999999", evidence_id="fact:fund:999999:other")
        self.payload["answer_facts"].insert(1, foreign)
        checker = FundAnswerCheck(self.payload)
        for category in ("supporting_evidence", "counter_evidence"):
            draft = valid_draft(self.payload)
            draft["judgments"][0][category] = [{"evidence_id": foreign["evidence_id"], "explanation": "测试错误引用"}]
            self.assertIn("其他基金", " ".join(checker.check(json.dumps(draft))[1]))
            report = checker.finish(json.dumps(draft), attempts=1, initial_issues=[])
            self.assertEqual(report.judgments, [])
            self.assertTrue(all(row.fund_code == "012345" for row in report.facts))

    def test_confirmed_background_is_copied_and_isolated_not_invented(self):
        context = InvestmentContext(facts={"horizon": InvestmentFact(
            text="这笔钱三年不用", source_turn="user-turn-1", updated_at="2026-09-06T01:00:00Z",
        )})
        with investment_context_scope(context):
            checker = FundAnswerCheck(self.payload)
        # 离开作用域仍保留当轮快照；另一个会话则不可见。
        self.assertNotIn("这笔钱三年不用", FundAnswerCheck(self.payload).instruction())
        self.assertIn("这笔钱三年不用", checker.instruction())
        context.facts["horizon"].text = "下个月需要用"
        draft = valid_draft(self.payload)
        draft["judgments"][0]["conditions"] = [
            {"basis": "user_context", "text": "你的持有安排为承受短期波动提供了一定空间。", "background_fields": ["horizon"]},
            {"basis": "missing_information", "text": "还需要了解你的亏损承受能力。", "background_fields": ["loss_tolerance"]},
            {"basis": "assumption", "text": "假设资金用途没有变化。", "background_fields": []},
        ]
        report = checker.finish(json.dumps(draft), attempts=0, initial_issues=[])
        self.assertEqual(report.status, "consistent")
        self.assertEqual(report.investment_background["horizon"].text, "这笔钱三年不用")
        self.assertEqual(report.investment_background["horizon"].source_turn, "user-turn-1")
        self.assertIn("用户原话 · 投资期限：这笔钱三年不用", render_answer_text(report))
        self.assertIn("待你补充", render_answer_text(report))
        self.assertTrue(self.checker.check(json.dumps(draft))[1])
        # 模型不能通过附加 quote 字段伪造用户原话。
        draft["judgments"][0]["conditions"][0]["quote"] = "可以接受所有亏损"
        self.assertTrue(checker.check(json.dumps(draft))[1])

    def test_unconfirmed_background_and_wrong_condition_basis_are_rejected(self):
        fact = InvestmentFact(text="三年不用", source_turn="turn-1", updated_at="2026-09-06")
        for pending in (False, True):
            with investment_context_scope(InvestmentContext(facts={"horizon": fact}, needs_confirmation=pending)):
                checker = FundAnswerCheck(self.payload)
            for basis, fields, valid in (
                ("user_context", ["horizon"], not pending),
                ("user_context", [], False),
                ("assumption", ["horizon"], False),
                ("missing_information", ["horizon"], pending),
                ("missing_information", [], True),
            ):
                draft = valid_draft(self.payload)
                draft["judgments"][0]["conditions"] = [{"basis": basis, "text": "条件说明", "background_fields": fields}]
                self.assertEqual(not checker.check(json.dumps(draft))[1], valid, (pending, basis))

    def test_legacy_v1_report_still_loads_without_manufactured_explanations(self):
        report = FundAnswerReport.model_validate({
            "fund_code": "012345", "fund_name": "旧报告", "status": "consistent",
            "judgments": [{"text": "过去的结论", "evidence_ids": ["old-reference"]}],
        })
        self.assertEqual(report.version, "fund-answer-v1")
        self.assertIn("old-reference", render_answer_text(report))
        self.assertNotIn("反向依据", render_answer_text(report))


class FundAnswerLoopTests(unittest.IsolatedAsyncioTestCase):
    async def run_loop(self, *, broken=False, repair="valid", max_rounds=3, reread=False, missing_reason=False):
        from app.services import tool_agent as module

        payload = brief()
        service = build_service()
        latest = json.loads(json.dumps(payload))
        if reread:
            latest["evidence_summary"]["historical_risk"]["period_start"] = "2026-03-01"
            latest["answer_facts"] = build_answer_facts("012345", latest["evidence_summary"])
            service.build_fund = Mock(side_effect=[payload, payload, latest])
        good = valid_draft(latest)
        phases = []

        class Model:
            def bind_tools(self, tools):
                return self

        async def invoke(**kwargs):
            phases.append(kwargs["phase"])
            if len(phases) == 1 or reread and len(phases) == 2:
                call = {
                    "id": "brief-1", "name": "get_fund_research_brief_tool",
                    "args": {"fund_code": "012345", "question": "全面分析"}, "type": "tool_call",
                }
                calls = [call]
                if reread and len(phases) == 1:
                    calls.append({**call, "id": "brief-2"})
                return ModelCallResult(response=AIMessage(content="", tool_calls=calls))
            draft = json.loads(json.dumps(good))
            if kwargs["phase"] == "fund_answer_repair":
                self.assertEqual(kwargs["max_attempts"], 1)
                self.assertFalse(kwargs["allow_continuation"])
                if repair == "failure":
                    raise RuntimeError("模拟超时")
                if repair == "cancel":
                    raise asyncio.CancelledError()
                if repair == "invalid":
                    draft["facts"][0]["period"] = "未来一年"
            elif broken:
                draft["facts"][0]["value"] = "999"
            elif missing_reason:
                draft["judgments"][0]["counter_evidence_gap"] = ""
            self.assertTrue(any("单基金研究最终回答" in str(msg.content) for msg in kwargs["messages"]))
            return ModelCallResult(response=AIMessage(
                content=json.dumps(draft, ensure_ascii=False),
                usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            ))

        route = AgentRequestRoute(name="fund_research_brief", domains=("fund_research_brief",),
                                  tool_names=("get_fund_research_brief_tool",), reason="offline test", include_mcp_tools=False)
        # 模型、路由器、工具数据和运行态都隔离，测试既不收费也不更改用户任务。
        with (
            patch.object(module, "get_chat_model", return_value=Model()),
            patch.object(module.agent_request_router, "route_hybrid", new=AsyncMock(return_value=route)),
            patch.object(module.model_recovery_service, "invoke", new=AsyncMock(side_effect=invoke)),
            patch.object(module.agent_runtime_state_service, "collect_response_fields", return_value={}),
            patch("app.services.financial_research_agent_tools.financial_research_brief_service", service),
        ):
            result = await module.ToolCallingAgentService().chat(AgentChatRequest(
                user_id="fact-check-test", message="全面分析这只基金", fund_code="012345", max_tool_rounds=max_rounds,
            ))
        return result, phases

    async def test_valid_answer_needs_no_additional_model_call(self):
        result, phases = await self.run_loop()
        self.assertEqual(phases, ["agent_round", "agent_round"])
        self.assertEqual(result.answer_check.status, "consistent")
        self.assertEqual(result.usage.total_tokens, 30)
        self.assertNotIn('"judgments"', result.answer)

    async def test_conflict_corrected_once_and_tokens_accounted(self):
        result, phases = await self.run_loop(broken=True)
        self.assertEqual(phases.count("fund_answer_repair"), 1)
        self.assertEqual(result.answer_check.status, "corrected")
        self.assertEqual(result.usage.total_tokens, 60)
        self.assertEqual(len(result.tool_calls), 1)
        self.assertNotIn("999", result.answer)

    async def test_missing_explanation_uses_existing_single_repair_not_new_agent(self):
        result, phases = await self.run_loop(missing_reason=True)
        self.assertEqual(phases, ["agent_round", "agent_round", "fund_answer_repair"])
        self.assertEqual(result.answer_check.status, "corrected")
        self.assertEqual(len(result.tool_calls), 1)
        self.assertTrue(result.answer_check.judgments[0].counter_evidence_gap)

    async def test_invalid_repair_or_timeout_degrades_without_leaking_draft(self):
        for repair in ("invalid", "failure"):
            result, phases = await self.run_loop(broken=True, repair=repair)
            self.assertEqual(phases.count("fund_answer_repair"), 1)
            self.assertEqual(result.answer_check.status, "limited")
            self.assertFalse(result.answer_check.judgments)
            self.assertNotIn("999", result.answer)

    async def test_forced_final_path_also_checks_facts(self):
        result, phases = await self.run_loop(broken=True, max_rounds=1)
        self.assertIn("forced_final_answer", phases)
        self.assertEqual(result.answer_check.status, "corrected")

    async def test_stop_during_repair_propagates_cancellation(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.run_loop(broken=True, repair="cancel")

    async def test_later_tool_result_replaces_old_check_context(self):
        result, phases = await self.run_loop(reread=True)
        self.assertEqual(len(result.tool_calls), 3)
        self.assertEqual(result.answer_check.status, "consistent")
        self.assertNotIn("fund_answer_repair", phases)
        self.assertIn("2026-03-01", result.answer_check.facts[0].period)

    async def test_report_persists_with_history_in_temporary_database(self):
        from app.services.comprehensive_agent import ComprehensiveAgentService
        from app.services.conversations import ConversationService
        from app.services.observability import AgentObservabilityService
        from app.database import Database
        from tests.test_conversations import FakeAgent, FakeRuntimeState

        payload = brief()
        report = FundAnswerCheck(payload).finish(json.dumps(valid_draft(payload)), attempts=0, initial_issues=[])

        class Agent(FakeAgent):
            async def chat(self, request, **kwargs):
                result = await super().chat(request, **kwargs)
                return result.model_copy(update={"answer_check": report, "answer": render_answer_text(report)})

        with TemporaryDirectory() as directory:
            database = Database(f"sqlite:///{Path(directory) / 'history.db'}")
            conversations = ConversationService(database)
            service = ComprehensiveAgentService(conversations=conversations, agent=Agent(),
                runtime_state=FakeRuntimeState(), observability=AgentObservabilityService(database))
            result = await service.chat(AgentChatRequest(user_id="fact-test", message="你好"))
            saved = conversations.detail(conversation_id=result.conversation.id, user_id="fact-test")
            self.assertEqual(saved["messages"][-1]["run_metadata"]["answer_check"]["facts"][0]["value"], "8.50")
            self.assertEqual(saved["messages"][-1]["run_metadata"]["answer_check"], report.model_dump(mode="json"))

            # HTTP 的流式接口只切分最终正文，同时把可展开事实放在最终 result 事件。
            from fastapi.testclient import TestClient
            from app.main import app

            with patch("app.routers.workspace.comprehensive_agent_service.chat", new=AsyncMock(return_value=result)):
                with TestClient(app).stream("POST", "/api/v1/agent/chat/stream",
                                          json={"user_id": "fact-test", "message": "你好"}) as response:
                    frames = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]
            self.assertEqual(response.status_code, 200)
            self.assertEqual("".join(frame["delta"] for frame in frames if "delta" in frame), result.run.answer)
            result_frame = next(frame for frame in frames if "assistant_message" in frame)
            self.assertEqual(result_frame["assistant_message"]["run_metadata"]["answer_check"]["status"], "consistent")
            self.assertEqual(result_frame["assistant_message"]["run_metadata"]["answer_check"], report.model_dump(mode="json"))


if __name__ == "__main__":
    unittest.main()

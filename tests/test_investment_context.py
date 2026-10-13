"""投资背景的增量提取、跨轮衔接及隔离测试，不调用收费模型或行情接口。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from app.database import Database
from app.schemas import AgentChatRequest, AgentToolCall
from app.services.agent_execution_context import AgentExecutionContext
from app.services.agent_routing import AgentRequestRouter
from app.services.agent_tool_executor import AgentToolExecutor
from app.services.comprehensive_agent import ComprehensiveAgentService
from app.services.conversations import ConversationService, ConversationNotFoundError, ConversationChangedError
from app.services.financial_research_planner import FinancialEvidenceGate, FinancialResearchPlanner
from app.services.financial_research_agent_tools import get_fund_research_brief_tool
from app.services.investment_context import (
    BackgroundPatch, InvestmentContext, InvestmentContextService,
    current_investment_context, investment_context_scope,
)
from app.services.observability import AgentObservabilityService
from app.services.prompt_builder import SystemPromptBuilderService
from tests.test_conversations import FakeAgent, FakeRuntimeState
from tests.test_financial_research_planner import good_fund_tasks, good_portfolio_tasks


INTRO = "这笔钱三年不用，最多接受亏损15%，用于养老。"
INTRO_PATCH = {
    "changes": [
        {"field": "horizon", "operation": "set", "quote": "这笔钱三年不用"},
        {"field": "liquidity", "operation": "set", "quote": "这笔钱三年不用"},
        {"field": "loss_tolerance", "operation": "set", "quote": "最多接受亏损15%"},
        {"field": "purpose", "operation": "set", "quote": "用于养老"},
    ],
    "background_reply": True,
}


def known_background() -> InvestmentContext:
    state = InvestmentContext()
    InvestmentContextService._apply_patch(state, BackgroundPatch.model_validate(INTRO_PATCH), INTRO)
    return state


def research_result(question: str, *, scope: str = "fund") -> dict:
    """真实闸门搭配固定行情样本，检查的仍是本轮真实 ContextVar。"""

    plan = FinancialResearchPlanner().plan(
        question=question, scope=scope, profiles=[{"sample_role": "industry_theme"}],
    )
    gate = FinancialEvidenceGate().evaluate(
        plan=plan, question=question,
        scope_data={"profile": {"fund_code": "161725"}, "holdings": [{"fund_code": "161725"}]},
        task_results=good_fund_tasks() if scope == "fund" else good_portfolio_tasks(),
    )
    return {"ok": True, "question": question, "research_plan": plan, "evidence_gate": gate}


def investor_row(result: dict) -> dict:
    return next(item for item in result["evidence_gate"]["requirements"] if item["key"] == "investor_context")


class InvestmentExtractionTests(IsolatedAsyncioTestCase):
    def setUp(self):
        # 除被明确替换的提取器以外，任何真实模型创建都视为测试隔离失败。
        self.model_guard = patch("app.services.investment_context.get_chat_model", side_effect=AssertionError("unexpected model call"))
        self.mock_model = self.model_guard.start()
        self.addCleanup(self.model_guard.stop)

    def tearDown(self):
        self.mock_model.assert_not_called()

    async def test_incremental_update_replaces_only_current_field(self):
        service = InvestmentContextService()
        with patch.object(service, "_extract", AsyncMock(return_value=INTRO_PATCH)):
            first = await service.prepare(message=INTRO, previous={})
        with patch.object(service, "_extract", AsyncMock(return_value={
            "changes": [{"field": "loss_tolerance", "operation": "set", "quote": "最多只接受亏损5%"}],
            "background_reply": True,
        })):
            second = await service.prepare(
                message="更正一下，最多只接受亏损5%", previous=first.model_dump(),
            )
        self.assertEqual(second.facts["horizon"], first.facts["horizon"])
        self.assertEqual(second.facts["loss_tolerance"].text, "最多只接受亏损5%")
        self.assertNotIn("15%", service.prompt_data(second))

    async def test_clear_is_local_and_prevents_old_values_reappearing(self):
        service = InvestmentContextService()
        with patch.object(service, "_extract", AsyncMock()) as extract:
            cleared = await service.prepare(message="清除本次投资背景", previous=known_background().model_dump())
            next_turn = await service.prepare(message="现在适合加仓吗", previous=cleared.model_dump())
        extract.assert_not_called()
        self.assertEqual(next_turn.facts, {})
        self.assertEqual(cleared.update_status, "cleared")
        self.assertTrue(cleared.background_only)

    async def test_explicit_withdrawal_removes_one_field(self):
        service = InvestmentContextService()
        with patch.object(service, "_extract", AsyncMock(return_value={
            "changes": [{"field": "loss_tolerance", "operation": "clear", "quote": "忘记之前的亏损承受条件"}],
        })):
            result = await service.prepare(message="忘记之前的亏损承受条件", previous=known_background().model_dump())
        self.assertNotIn("loss_tolerance", result.facts)
        self.assertIn("horizon", result.facts)

    async def test_new_money_resets_previous_purpose_and_risk(self):
        service = InvestmentContextService()
        with patch.object(service, "_extract", AsyncMock(return_value={
            "reset_quote": "换另一笔资金", "changes": [
                {"field": "purpose", "operation": "set", "quote": "用于买房"},
            ],
        })):
            result = await service.prepare(message="换另一笔资金，用于买房", previous=known_background().model_dump())
        self.assertEqual(set(result.facts), {"purpose"})

    async def test_hypothetical_and_statistic_questions_do_not_become_facts(self):
        service = InvestmentContextService()
        for message in ("假设一个人能承受30%亏损呢？", "这只基金三年最大回撤15%算高吗？"):
            with patch.object(service, "_extract", AsyncMock(return_value={"changes": []})):
                result = await service.prepare(message=message, previous={})
            self.assertFalse(result.facts)

    async def test_fabricated_quote_or_model_failure_marks_prior_state_unconfirmed(self):
        service = InvestmentContextService()
        for extract in (
            AsyncMock(return_value={"changes": [{"field": "horizon", "operation": "set", "quote": "持有十年"}]}),
            AsyncMock(side_effect=TimeoutError("do not expose provider credentials")),
        ):
            with patch.object(service, "_extract", extract):
                result = await service.prepare(message="我只能接受5%", previous=known_background().model_dump())
            self.assertTrue(result.needs_confirmation)
            self.assertEqual(len(result.missing_fields()), 3)
            self.assertNotIn("credentials", result.model_dump_json())
            # 下一轮无关问题也不能自动重新启用旧的 15%。
            next_turn = await service.prepare(message="解释夏普比率", previous=result.model_dump())
            self.assertTrue(next_turn.needs_confirmation)

    async def test_background_reply_resumes_only_same_research_scope(self):
        service = InvestmentContextService()
        previous = InvestmentContext(
            fund_code="161725", research_question="这只基金适合我吗？", pending_fields=["horizon"],
        )
        with patch.object(service, "_extract", AsyncMock(return_value={
            "changes": [{"field": "horizon", "operation": "set", "quote": "三年"}],
            "background_reply": True,
        })):
            resumed = await service.prepare(message="三年", previous=previous.model_dump())
            switched = await service.prepare(message="三年", previous=previous.model_dump(), fund_code="110020")
        self.assertEqual(resumed.effective_question("三年"), previous.research_question)
        self.assertFalse(switched.resume_research)
        self.assertEqual(switched.fund_code, "110020")

    async def test_greeting_and_normal_lookup_do_not_add_extraction_calls(self):
        service = InvestmentContextService()
        with patch.object(service, "_extract", AsyncMock()) as extract:
            await service.prepare(message="你好", previous={})
            await service.prepare(message="帮我查一下这只基金的净值", previous={})
        extract.assert_not_called()

    async def test_chinese_percentage_update_is_not_skipped(self):
        service = InvestmentContextService()
        with patch.object(service, "_extract", AsyncMock(return_value={
            "changes": [{"field": "loss_tolerance", "operation": "set", "quote": "最多亏损五个点"}],
            "background_reply": True,
        })) as extract:
            result = await service.prepare(message="最多亏损五个点", previous=known_background().model_dump())
        extract.assert_awaited_once()
        self.assertEqual(result.facts["loss_tolerance"].text, "最多亏损五个点")

    async def test_resume_tracks_actual_successful_research_subject(self):
        service = InvestmentContextService()
        state = InvestmentContext(fund_code="161725")
        service.finish(state, [AgentToolCall(
            tool_name="get_fund_research_brief_tool", tool_args={"fund_code": "110020"},
            tool_result=json.dumps({
                "ok": True, "scope": {"kind": "fund", "fund_code": "110020"},
                "question": "110020适合我吗？", "research_plan": {"intent": "decision_support"},
            }),
        )])
        with patch.object(service, "_extract", AsyncMock(return_value={
            "changes": [{"field": "horizon", "operation": "set", "quote": "三年"}],
            "background_reply": True,
        })):
            resumed = await service.prepare(message="三年", previous=state.model_dump())
        self.assertEqual(resumed.fund_code, "110020")
        self.assertEqual(resumed.effective_question("三年"), "110020适合我吗？")

    async def test_context_isolated_between_tasks_and_propagates_into_tool_thread(self):
        async def read(value):
            state = known_background()
            state.facts["horizon"].text = value
            with investment_context_scope(state):
                await asyncio.sleep(0)
                return await asyncio.to_thread(lambda: current_investment_context().facts["horizon"].text)
        self.assertEqual(await asyncio.gather(read("一年"), read("三年")), ["一年", "三年"])
        self.assertIsNone(current_investment_context())


class InvestmentGateTests(TestCase):
    def test_previous_context_satisfies_fund_and_portfolio_gate(self):
        for scope in ("fund", "portfolio"):
            with investment_context_scope(known_background()):
                result = research_result("这只基金适合我吗？", scope=scope)
            row = investor_row(result)
            self.assertEqual(row["status"], "satisfied")
            self.assertIn("15%", row["facts"]["loss_tolerance"]["text"])
            self.assertFalse(any("持有多久" in question for question in result["evidence_gate"]["clarification_questions"]))

    def test_only_missing_field_is_asked_and_empty_state_does_not_use_keyword_guess(self):
        state = known_background()
        del state.facts["liquidity"]
        with investment_context_scope(state):
            result = research_result("请全面分析加仓的风险")
        self.assertEqual(investor_row(result)["missing_fields"], ["liquidity"])
        self.assertEqual(result["evidence_gate"]["clarification_questions"], ["近期是否可能需要赎回这笔资金？"])
        with investment_context_scope(InvestmentContext()):
            result = research_result("长期投资时应该如何理解最大回撤，适合我吗？")
        self.assertEqual(investor_row(result)["status"], "missing")

    def test_prompt_and_tool_share_same_facts_and_clear_boundary(self):
        state = known_background()
        with investment_context_scope(state):
            prompt = SystemPromptBuilderService().build(user_id="chen", tool_names=[], domains=())
        self.assertIn("最多接受亏损15%", prompt.system_prompt)
        self.assertIn("不得从旧回答或旧摘要补猜", prompt.system_prompt)

    def test_personal_suitability_selects_complete_research_tool(self):
        for message, target, tool in (
            ("这只半导体基金适合我吗？", {"fund_code": "161725"}, "get_fund_research_brief_tool"),
            ("适合我吗？", {"fund_code": "161725"}, "get_fund_research_brief_tool"),
            ("适合我吗？", {"portfolio_id": "portfolio_" + "a" * 16}, "get_portfolio_research_brief_tool"),
        ):
            route = AgentRequestRouter().route(
                AgentChatRequest(message=message, **target),
                execution_context=AgentExecutionContext.product(user_id="guest"),
            )
            self.assertEqual(route.tool_names, (tool,))


class InvestmentExtractorContractTests(IsolatedAsyncioTestCase):
    async def test_json_mode_has_explicit_schema_and_only_current_user_text(self):
        from unittest.mock import Mock

        structured = Mock(ainvoke=AsyncMock(return_value=BackgroundPatch()))
        model = Mock(with_structured_output=Mock(return_value=structured))
        with patch("app.services.investment_context.get_chat_model", return_value=model) as factory:
            await InvestmentContextService()._extract(INTRO, InvestmentContext())
        factory.assert_called_once_with(max_retries=0, temperature=0, thinking=False)
        model.with_structured_output.assert_called_once_with(BackgroundPatch, method="json_mode")
        messages = structured.ainvoke.call_args.args[0]
        self.assertIn('"changes"', messages[0].content)
        self.assertIn('"additionalProperties": false', messages[0].content)
        self.assertEqual(json.loads(messages[1].content)["message"], INTRO)


class ResearchFakeAgent(FakeAgent):
    """让应用层测试通过真正的工具参数注入与 LangChain 同步工具执行。"""

    async def chat(self, request, *, history, progress_callback=None):
        response = await super().chat(request, history=history, progress_callback=progress_callback)
        background = current_investment_context()
        if background.background_only:
            return response
        args = {"fund_code": request.fund_code or "161725", "question": "模型删掉的原问题"}
        AgentToolExecutor()._inject_trusted_arguments(
            tool_name="get_fund_research_brief_tool", tool_args=args, request=request,
            execution_context=AgentExecutionContext.product(user_id=request.user_id),
        )
        with patch("app.services.financial_research_agent_tools.financial_research_brief_service.build_fund",
                   side_effect=lambda **kwargs: research_result(kwargs["question"])):
            raw = await get_fund_research_brief_tool.ainvoke(args)
        response.tool_calls = [AgentToolCall(tool_name="get_fund_research_brief_tool", tool_args=args, tool_result=raw)]
        return response


class InvestmentConversationTests(IsolatedAsyncioTestCase):
    async def test_late_old_response_cannot_restore_cleared_background(self):
        """不用同进程协调锁模拟两个 Worker；数据库仍必须拒绝过期提交。"""

        with TemporaryDirectory() as directory:
            db = Database(f"sqlite:///{Path(directory) / 'concurrent.db'}")
            conversations = ConversationService(db)
            conversation = conversations.create(user_id="chen")
            conversations.append_exchange(
                user_id="chen", conversation_id=conversation["id"], user_content=INTRO,
                assistant_content="已记录", run_id="seed",
                run_metadata={"investment_context": known_background().model_dump()},
            )
            entered, release = asyncio.Event(), asyncio.Event()

            class SlowAgent(FakeAgent):
                async def chat(self, request, *, history, progress_callback=None):
                    entered.set()
                    await release.wait()
                    return await super().chat(request, history=history, progress_callback=progress_callback)

            def make_service(agent):
                return ComprehensiveAgentService(
                    conversations=ConversationService(db), agent=agent, runtime_state=FakeRuntimeState(),
                    observability=AgentObservabilityService(db),
                )

            slow, fast = make_service(SlowAgent()), make_service(FakeAgent())
            old = asyncio.create_task(slow.chat(AgentChatRequest(
                user_id="chen", conversation_id=conversation["id"], message="解释净值",
            )))
            await entered.wait()
            await fast.chat(AgentChatRequest(
                user_id="chen", conversation_id=conversation["id"], message="清除本次投资背景",
            ))
            release.set()
            with self.assertRaises(ConversationChangedError):
                await old
            state = conversations.investment_context(user_id="chen", conversation_id=conversation["id"])
            self.assertEqual(state["facts"], {})
            self.assertEqual(state["update_status"], "cleared")
            self.assertEqual(conversations.get(user_id="chen", conversation_id=conversation["id"])["message_count"], 4)
            db.engine.dispose()

    async def test_complete_multi_turn_flow_survives_service_restart_and_deletion(self):
        with TemporaryDirectory() as directory:
            db = Database(f"sqlite:///{Path(directory) / 'chat.db'}")
            conversations = ConversationService(db, summary_trigger_messages=4, summary_recent_messages=2)
            extractor = InvestmentContextService()
            service = ComprehensiveAgentService(
                conversations=conversations, agent=ResearchFakeAgent(), runtime_state=FakeRuntimeState(),
                observability=AgentObservabilityService(db), investment_context=extractor,
            )
            with patch.object(extractor, "_extract", AsyncMock(return_value=INTRO_PATCH)):
                first = await service.chat(AgentChatRequest(user_id="chen", message=INTRO, fund_code="161725"))
            context = first.assistant_message.run_metadata["investment_context"]
            self.assertEqual(context["facts"]["horizon"]["source_turn"], first.user_message.id)
            second = await service.chat(AgentChatRequest(
                user_id="chen", conversation_id=first.conversation.id, message="这只基金适合我吗？", fund_code="161725",
            ))
            self.assertEqual(investor_row(json.loads(second.run.tool_calls[0].tool_result))["status"], "satisfied")
            with patch.object(extractor, "_extract", AsyncMock(return_value={
                "changes": [{"field": "loss_tolerance", "operation": "set", "quote": "最多接受亏损5%"}],
                "background_reply": True,
            })):
                third = await service.chat(AgentChatRequest(
                    user_id="chen", conversation_id=first.conversation.id, message="更正，最多接受亏损5%",
                ))
            restarted = ConversationService(Database(db.url))
            restored = restarted.investment_context(user_id="chen", conversation_id=first.conversation.id)
            self.assertEqual(restored["facts"]["loss_tolerance"]["text"], "最多接受亏损5%")
            self.assertEqual(restored["facts"]["horizon"]["source_turn"], first.user_message.id)
            self.assertEqual(restored["facts"]["loss_tolerance"]["source_turn"], third.user_message.id)
            with self.assertRaises(ConversationNotFoundError):
                restarted.investment_context(user_id="other", conversation_id=first.conversation.id)
            fresh = restarted.create(user_id="chen")
            self.assertEqual(restarted.investment_context(user_id="chen", conversation_id=fresh["id"]), {})
            await service.chat(AgentChatRequest(user_id="chen", conversation_id=first.conversation.id, message="清除本次投资背景"))
            self.assertEqual(restarted.investment_context(user_id="chen", conversation_id=first.conversation.id)["facts"], {})
            restarted.delete(user_id="chen", conversation_id=first.conversation.id)
            with self.assertRaises(ConversationNotFoundError):
                restarted.investment_context(user_id="chen", conversation_id=first.conversation.id)
            db.engine.dispose()

    async def test_short_reply_continues_previous_research_without_reasking_known_fields(self):
        with TemporaryDirectory() as directory:
            db = Database(f"sqlite:///{Path(directory) / 'followup.db'}")
            extractor = InvestmentContextService()
            service = ComprehensiveAgentService(
                conversations=ConversationService(db), agent=ResearchFakeAgent(), runtime_state=FakeRuntimeState(),
                observability=AgentObservabilityService(db), investment_context=extractor,
            )
            first = await service.chat(AgentChatRequest(user_id="chen", message="这只基金适合我吗？", fund_code="161725"))
            self.assertEqual(len(first.assistant_message.run_metadata["investment_context"]["pending_fields"]), 3)
            with patch.object(extractor, "_extract", AsyncMock(return_value={**INTRO_PATCH, "background_reply": True})):
                second = await service.chat(AgentChatRequest(user_id="chen", conversation_id=first.conversation.id, message=INTRO))
            result = json.loads(second.run.tool_calls[0].tool_result)
            self.assertEqual(result["question"], "这只基金适合我吗？")
            self.assertEqual(investor_row(result)["status"], "satisfied")
            self.assertEqual(second.assistant_message.run_metadata["investment_context"]["pending_fields"], [])
            db.engine.dispose()

    async def test_agent_failure_does_not_commit_updated_background(self):
        with TemporaryDirectory() as directory:
            db = Database(f"sqlite:///{Path(directory) / 'failed.db'}")
            conversations = ConversationService(db)
            conversation = conversations.create(user_id="chen")
            extractor = InvestmentContextService()
            agent = FakeAgent()
            agent.chat = AsyncMock(side_effect=RuntimeError("model failed"))
            service = ComprehensiveAgentService(
                conversations=conversations, agent=agent, runtime_state=FakeRuntimeState(),
                observability=AgentObservabilityService(db), investment_context=extractor,
            )
            with patch.object(extractor, "_extract", AsyncMock(return_value=INTRO_PATCH)):
                with self.assertRaises(RuntimeError):
                    await service.chat(AgentChatRequest(user_id="chen", conversation_id=conversation["id"], message=INTRO))
            self.assertEqual(conversations.investment_context(user_id="chen", conversation_id=conversation["id"]), {})
            self.assertEqual(conversations.detail(user_id="chen", conversation_id=conversation["id"])["messages"], [])
            self.assertIsNone(current_investment_context())
            db.engine.dispose()

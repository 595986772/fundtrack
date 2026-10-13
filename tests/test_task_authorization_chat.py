"""聊天任务授权的离线集成测试：临时 SQL、Fake Policy，不访问模型或外部数据。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.config import settings
from app.database import Database
from app.models import AgentChatExecutionModel, AgentTaskGrantModel, FundPortfolioModel
from app.schemas import AgentChatRequest, AgentChatResponse, AgentPromptTrace, AgentWorkspaceRuntimeInfo, AgentWorkspaceSnapshot
from app.services import agent_authorization as task_auth
from app.services.agent_chat_runtime import AgentChatCoordinator, AgentChatExecutionStore, _scoped_idempotency_key
from app.services.agent_execution_context import (
    AgentExecutionContext, PORTFOLIO_READ, PORTFOLIO_RESEARCH_RUN,
    agent_execution_scope, current_agent_execution_context,
)
from app.services.ai_governance_context import AIControlError, current_ai_scope, stable_run_id
from app.services.comprehensive_agent import ComprehensiveAgentService
from app.services.conversations import ConversationNotFoundError, ConversationService
from app.services.investment_context import InvestmentContext, InvestmentContextService, current_investment_context
from app.services.observability import AgentObservabilityService
from app.services.portfolio_report_context import resolve_report_context, resolve_report_reference
from tests.task_authorization_fixture import A as PORT_A, B as PORT_B, C as PORT_OTHER, FakePolicy


class FakeBackground(InvestmentContextService):
    def __init__(self):
        self.calls = []
        self.override = None

    async def prepare(self, **kwargs):
        self.calls.append((deepcopy(kwargs), current_agent_execution_context()))
        if self.override is not None:
            return self.override.model_copy(deep=True)
        return InvestmentContext(fund_code=kwargs["fund_code"], portfolio_id=kwargs["portfolio_id"])


class FakeAgent:
    def __init__(self):
        self.calls = []
        self.action = None

    async def chat(self, request, *, history, execution_context=None, progress_callback=None):
        self.calls.append({"request": request, "history": deepcopy(history),
            "context": current_agent_execution_context(), "passed_context": execution_context,
            "ai_scope": current_ai_scope(), "background": current_investment_context().model_copy(deep=True)})
        if self.action is not None:
            await self.action(progress_callback)
        return AgentChatResponse(run_id=f"model-run-{len(self.calls)}", user_id=request.user_id,
            question=request.message, answer=f"answer:{request.message}", used_model="fake",
            max_tool_rounds=request.max_tool_rounds, stop_reason="model_finished",
            tools_available=[], tool_calls=[], rounds=[], pipeline_steps=[],
            prompt_trace=AgentPromptTrace(section_count=0, total_chars=0, sections=[]))


class FakeRuntime:
    def __init__(self):
        self.contexts = []

    def workspace_snapshot(self, *, user_id, tool_names):
        self.contexts.append(current_agent_execution_context())
        return AgentWorkspaceSnapshot(user_id=user_id, runtime=AgentWorkspaceRuntimeInfo(
            app_name="offline", model="fake", execution_backend="local", database_backend="sqlite",
            tool_count=len(tool_names), mcp_tool_count=0))


class FakeReportReader:
    def __init__(self):
        self.contexts = []

    def read_report(self, *, user_id, portfolio_id, job_id):
        self.contexts.append(current_agent_execution_context())
        return {"portfolio_id": portfolio_id, "portfolio_name": "Selected portfolio",
            "run_id": job_id, "headline": "Frozen report evidence", "key_findings": [],
            "research_snapshot": {"snapshot_id": "snapshot-frozen"}}


class TaskAuthorizationChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Database(f"sqlite:///{Path(temporary.name) / 'chat-authorization.db'}")
        self.database.create_schema()
        self.addCleanup(self.database.engine.dispose)
        self.now = datetime(2026, 9, 21, tzinfo=timezone.utc)
        with self.database.session() as session, session.begin():
            for user, portfolio in (("alice", PORT_A), ("alice", PORT_B), ("bob", PORT_OTHER)):
                session.add(FundPortfolioModel(user_id=user, id=portfolio, name=portfolio,
                    created_at=self.now, updated_at=self.now))
        self.policy = FakePolicy()
        self.authorization = task_auth.TaskAuthorizationService(
            self.database, policy=self.policy, clock=lambda: self.now)
        for patcher in (
            patch.object(settings, "agent_task_authorization_enabled", True),
            patch.object(settings, "agent_task_grant_seconds", 1800),
            patch.object(task_auth, "get_task_authorization_service", return_value=self.authorization),
            patch("app.routers.workspace.get_task_authorization_service", return_value=self.authorization),
            patch("app.services.investment_context.get_chat_model", side_effect=AssertionError("Real model forbidden")),
            patch("app.services.tool_agent.get_chat_model", side_effect=AssertionError("Real model forbidden")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.context = AgentExecutionContext.product(user_id="alice", authenticated=True)
        self.conversations = ConversationService(self.database,
            summary_trigger_messages=4, summary_recent_messages=2)
        self.agent, self.background, self.runtime = FakeAgent(), FakeBackground(), FakeRuntime()
        self.service = ComprehensiveAgentService(conversations=self.conversations, agent=self.agent,
            investment_context=self.background, runtime_state=self.runtime,
            observability=AgentObservabilityService(self.database))
        self.store = AgentChatExecutionStore(self.database)
        self.coordinator = AgentChatCoordinator(agent=self.service, store=self.store)

    def request(self, **updates):
        return AgentChatRequest(**{"user_id": "alice", "message": "inspect selected",
            "portfolio_id": PORT_A, "idempotency_key": "chat-test-0001", **updates})

    async def chat(self, request=None, *, callback=None):
        return await self.coordinator.chat(request or self.request(),
            execution_context=self.context, progress_callback=callback)

    def grants(self):
        with self.database.session() as session:
            return list(session.scalars(select(AgentTaskGrantModel)))

    def record(self, request=None):
        request = request or self.request()
        with self.database.session() as session:
            return session.get(AgentChatExecutionModel, (request.user_id,
                _scoped_idempotency_key(request.idempotency_key, self.context)))

    def seed_reference(self, portfolio_id=PORT_A):
        conversation = self.conversations.create(user_id="alice")
        self.conversations.append_exchange(user_id="alice", conversation_id=conversation["id"],
            user_content="old question", assistant_content="old report text", run_id="seed",
            run_metadata={"research_reference": {"job_id": "job_frozen", "portfolio_id": portfolio_id}})
        return conversation["id"]

    def report_patch(self, reader):
        return patch("app.services.comprehensive_agent.resolve_report_context",
            side_effect=lambda request, previous: resolve_report_context(request, previous, reader))

    async def test_product_grants_selected_portfolio_and_scopes_entire_run(self):
        events, scopes = [], []

        async def collect(event):
            events.append(event)
            scopes.append(current_agent_execution_context())

        with patch.object(self.conversations, "append_exchange", wraps=self.conversations.append_exchange) as persist:
            result = await self.chat(callback=collect)
        metadata = result.response.assistant_message.run_metadata["task_authorization"]
        self.assertEqual(metadata["portfolio_ids"], [PORT_A])
        self.assertEqual(metadata["agent_id"], "main")
        self.assertEqual(metadata["run_id"], stable_run_id("chat", _scoped_idempotency_key("chat-test-0001", self.context)))
        self.assertNotEqual(metadata["run_id"], result.response.run.run_id)
        self.assertEqual(len(self.grants()), 1)
        self.assertEqual(self.grants()[0].expires_at - self.grants()[0].created_at, timedelta(minutes=30))
        contexts = [*scopes, self.agent.calls[0]["context"], self.agent.calls[0]["passed_context"],
            self.background.calls[0][1], self.runtime.contexts[0]]
        self.assertTrue(all(context.task_grant_id == metadata["task_grant_id"] for context in contexts))
        self.assertTrue(self.agent.calls[0]["context"].authenticated)
        self.assertIsNone(current_agent_execution_context().task_grant_id)
        self.assertEqual(persist.call_args.kwargs["run_metadata"]["task_authorization"], metadata)
        event = next(event for event in events if event.get("phase") == "authorization")
        self.assertEqual(event["authorization"]["id"], metadata["task_grant_id"])
        self.assertEqual(event["authorization"]["scope"]["portfolio_ids"], [PORT_A])
        self.assertNotIn(PORT_B, str(events))

    async def test_unselected_request_has_empty_scope_and_no_portfolio_tools(self):
        result = await self.chat(self.request(portfolio_id=None))
        metadata = result.response.assistant_message.run_metadata["task_authorization"]
        context = self.agent.calls[0]["context"]
        self.assertEqual(metadata["portfolio_ids"], [])
        self.assertNotIn(PORTFOLIO_READ, context.capabilities)
        self.assertNotIn(PORTFOLIO_RESEARCH_RUN, context.capabilities)
        self.assertFalse(context.can_expose_tool("get_portfolio_research_brief_tool"))

    async def test_foreign_selection_is_rejected_before_any_data_or_agent_work(self):
        with self.assertRaises(task_auth.AgentAuthorizationError):
            await self.chat(self.request(portfolio_id=PORT_OTHER))
        self.assertEqual(self.agent.calls, [])
        self.assertEqual(self.background.calls, [])
        self.assertEqual(self.grants(), [])

    async def test_failure_retry_reuses_grant_without_renewing_expiry(self):
        async def fail(_):
            raise RuntimeError("offline model failure")

        self.agent.action = fail
        with self.assertRaises(RuntimeError):
            await self.chat()
        original = self.grants()[0]
        self.now += timedelta(minutes=4)
        self.agent.action = None
        result = await self.chat()
        self.assertEqual(result.response.assistant_message.run_metadata["task_authorization"]["task_grant_id"], original.id)
        self.assertEqual(self.grants()[0].expires_at, original.expires_at)
        self.assertEqual(len(self.grants()), 1)

    async def test_active_cache_is_revalidated_without_new_grant_or_model_call(self):
        first = await self.chat()
        self.now += timedelta(minutes=2)
        events = []

        async def collect(event):
            events.append(event)

        second = await self.chat(callback=collect)
        self.assertTrue(second.replayed)
        self.assertEqual(second.response, first.response)
        self.assertEqual(len(self.agent.calls), 1)
        self.assertEqual(len(self.grants()), 1)
        self.assertEqual([event["phase"] for event in events], ["authorization", "cache"])

    async def test_revoked_and_expired_cache_never_reaches_callback(self):
        for expired in (False, True):
            with self.subTest(expired=expired):
                request = self.request(idempotency_key=f"chat-expiry-{expired}")
                await self.chat(request)
                grant_id = self.agent.calls[-1]["context"].task_grant_id
                if expired:
                    self.now += timedelta(minutes=31)
                else:
                    self.authorization.revoke(user_id="alice", grant_id=grant_id)
                events = []

                async def collect(event):
                    events.append(event)

                with self.assertRaises(task_auth.AgentAuthorizationError):
                    await self.chat(request, callback=collect)
                self.assertEqual(events, [])
        self.assertEqual(len(self.agent.calls), 2)

    async def test_cache_rechecks_policy_and_current_ownership(self):
        await self.chat()
        relations = self.policy.relations.copy()
        self.policy.relations.clear()
        with self.assertRaises(task_auth.AgentAuthorizationError) as rejected:
            await self.chat()
        self.assertEqual(rejected.exception.code, "policy_denied")
        self.policy.relations = relations
        with self.database.session() as session, session.begin():
            session.delete(session.get(FundPortfolioModel, ("alice", PORT_A)))
        with self.assertRaises(task_auth.AgentAuthorizationError) as rejected:
            await self.chat()
        self.assertEqual(rejected.exception.code, "resource_denied")
        self.assertEqual(len(self.agent.calls), 1)

    async def test_same_key_cannot_change_selection_after_success_or_failure(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                request = self.request(idempotency_key=f"chat-switch-{failed}")
                async def fail(_):
                    raise RuntimeError("offline failure")
                self.agent.action = fail if failed else None
                if failed:
                    with self.assertRaises(RuntimeError):
                        await self.chat(request)
                else:
                    await self.chat(request)
                with self.assertRaises(task_auth.AgentAuthorizationError) as rejected:
                    await self.chat(request.model_copy(update={"portfolio_id": PORT_B}))
                self.assertEqual(rejected.exception.status_code, 409)
        self.assertEqual(len(self.agent.calls), 2)
        self.assertEqual(len(self.grants()), 2)

    async def test_legacy_cache_without_fingerprint_still_checks_original_scope(self):
        await self.chat()
        with self.database.session() as session, session.begin():
            row = session.get(AgentChatExecutionModel, ("alice", self.record().idempotency_key))
            row.response_payload = {key: value for key, value in row.response_payload.items()
                if key != "_request_fingerprint"}
        with self.assertRaises(task_auth.AgentAuthorizationError) as rejected:
            await self.chat(self.request(portfolio_id=PORT_B))
        self.assertEqual(rejected.exception.code, "grant_scope_conflict")

    async def test_cache_cannot_restore_another_users_grant(self):
        await self.chat()
        with self.database.session() as session, session.begin():
            row = session.get(AgentChatExecutionModel, ("alice", self.record().idempotency_key))
            payload = deepcopy(row.response_payload)
            for section in ("conversation", "run", "workspace"):
                payload[section]["user_id"] = "bob"
            payload.pop("_request_fingerprint", None)
            session.add(AgentChatExecutionModel(user_id="bob", idempotency_key=row.idempotency_key,
                conversation_id=row.conversation_id, status="completed", response_payload=payload,
                error="", created_at=self.now, updated_at=self.now))
        with self.assertRaises(task_auth.AgentAuthorizationError) as rejected:
            await self.coordinator.chat(self.request(user_id="bob"),
                execution_context=AgentExecutionContext.product(user_id="bob"))
        self.assertEqual(rejected.exception.code, "grant_identity_mismatch")

    async def test_report_followup_resolves_scope_before_threaded_report_read(self):
        conversation_id = self.seed_reference()
        reader = FakeReportReader()
        request = self.request(conversation_id=conversation_id, portfolio_id=None)
        with self.report_patch(reader):
            result = await self.chat(request)
            replay = await self.chat(request)
        self.assertTrue(replay.replayed)
        self.assertEqual(len(reader.contexts), 1)
        metadata = result.response.assistant_message.run_metadata
        self.assertEqual(metadata["task_authorization"]["portfolio_ids"], [PORT_A])
        self.assertEqual(reader.contexts[0].task_grant_id, metadata["task_authorization"]["task_grant_id"])
        self.assertEqual(self.agent.calls[0]["request"].portfolio_id, PORT_A)
        self.assertIn("Frozen report evidence", str(self.agent.calls[0]["history"]))
        self.assertNotIn("old report text", str(self.agent.calls[0]["history"]))

    async def test_report_scope_denial_happens_before_loading_body(self):
        conversation_id = self.seed_reference(PORT_OTHER)
        reader = FakeReportReader()
        with self.report_patch(reader), self.assertRaises(task_auth.AgentAuthorizationError):
            await self.chat(self.request(conversation_id=conversation_id, portfolio_id=None))
        self.assertEqual(reader.contexts, [])
        self.assertEqual(self.agent.calls, [])

    async def test_explicit_report_clear_does_not_restore_portfolio(self):
        conversation_id = self.seed_reference()
        reader = FakeReportReader()
        with self.report_patch(reader):
            result = await self.chat(self.request(conversation_id=conversation_id,
                portfolio_id=None, research_job_id=""))
        self.assertEqual(reader.contexts, [])
        self.assertEqual(result.response.assistant_message.run_metadata["task_authorization"]["portfolio_ids"], [])

    async def test_cached_followup_compares_current_server_reference(self):
        conversation_id = self.seed_reference()
        request = self.request(conversation_id=conversation_id, portfolio_id=None)
        with self.report_patch(FakeReportReader()):
            await self.chat(request)
        self.conversations.append_exchange(user_id="alice", conversation_id=conversation_id,
            user_content="switch", assistant_content="switched", run_id="changed",
            run_metadata={"research_reference": {"job_id": "job_other", "portfolio_id": PORT_B}})
        with self.assertRaises(task_auth.AgentAuthorizationError):
            await self.chat(request)
        self.assertEqual(len(self.agent.calls), 1)

    async def test_switching_scope_never_reintroduces_old_text_or_rolling_summary(self):
        result = await self.chat(self.request(message="secret-A"))
        conversation_id = result.response.conversation.id
        with patch.object(self.conversations, "context_messages", side_effect=AssertionError("Unscoped history read")):
            for index in range(1, 5):
                await self.chat(self.request(conversation_id=conversation_id, portfolio_id=PORT_B,
                    idempotency_key=f"chat-new-scope-{index}", message=f"question-B-{index}"))
                self.assertNotIn("secret-A", str(self.agent.calls[-1]["history"]))
                self.assertFalse(any(item["role"] == "system" for item in self.agent.calls[-1]["history"]))
        self.assertEqual(self.agent.calls[1]["history"], [])
        self.assertIn("question-B-1", str(self.agent.calls[2]["history"]))
        self.assertEqual(self.background.calls[1][0]["previous"], {})
        await self.chat(self.request(conversation_id=conversation_id, portfolio_id=None,
            idempotency_key="chat-cleared-scope"))
        self.assertEqual(self.agent.calls[-1]["history"], [])

    async def test_scoped_history_keeps_existing_message_and_character_limits(self):
        self.conversations.context_message_limit = 2
        self.conversations.context_char_limit = 30
        result = await self.chat(self.request(message="first " + "x" * 50))
        await self.chat(self.request(conversation_id=result.response.conversation.id,
            idempotency_key="chat-history-budget", message="next"))
        history = self.agent.calls[-1]["history"]
        self.assertLessEqual(len(history), 2)
        self.assertLessEqual(sum(len(item["content"]) for item in history), 30)

    async def test_background_extraction_cannot_expand_portfolio_scope(self):
        self.background.override = InvestmentContext(portfolio_id=PORT_B,
            resume_research=True, research_question="read B", pending_fields=["horizon"])
        for index, portfolio_id in enumerate((None, PORT_A)):
            result = await self.chat(self.request(portfolio_id=portfolio_id,
                idempotency_key=f"chat-extraction-{index}"))
            call = self.agent.calls[-1]
            self.assertEqual(call["request"].portfolio_id, portfolio_id)
            self.assertEqual(call["background"].portfolio_id, portfolio_id)
            self.assertFalse(call["background"].resume_research)
            self.assertEqual(result.response.assistant_message.run_metadata["task_authorization"]["portfolio_ids"],
                [portfolio_id] if portfolio_id else [])

    async def test_legacy_service_and_disabled_or_developer_context_do_not_create_grants(self):
        await self.service.chat(self.request())
        with patch.object(settings, "agent_task_authorization_enabled", False):
            await self.service.chat(self.request(), execution_context=self.context)
        await self.service.chat(self.request(), execution_context=AgentExecutionContext.developer(
            user_id="alice", role="admin"))
        self.assertEqual(self.grants(), [])

    async def test_inherited_worker_scope_is_not_replaced_or_renewed(self):
        bound = self.authorization.create(self.context, run_id="job-root", agent_id="worker", portfolio_ids=[PORT_A])
        with agent_execution_scope(bound):
            result = await self.service.chat(self.request())
        self.assertEqual(result.assistant_message.run_metadata["task_authorization"]["task_grant_id"], bound.task_grant_id)
        self.assertEqual(len(self.grants()), 1)
        self.authorization.revoke(user_id="alice", grant_id=bound.task_grant_id)
        with agent_execution_scope(bound), patch.object(self.authorization, "create", wraps=self.authorization.create) as create:
            with self.assertRaises(task_auth.AgentAuthorizationError):
                await self.service.chat(self.request())
            create.assert_not_called()
        self.assertEqual(len(self.agent.calls), 1)

    async def test_revocation_just_after_creation_stops_all_preloading(self):
        original = self.authorization.create
        def revoke_new(*args, **kwargs):
            bound = original(*args, **kwargs)
            self.authorization.revoke(user_id=bound.user_id, grant_id=bound.task_grant_id)
            return bound
        with patch.object(self.authorization, "create", side_effect=revoke_new):
            with self.assertRaises(task_auth.AgentAuthorizationError):
                await self.chat()
        self.assertEqual(self.agent.calls, [])
        self.assertEqual(self.background.calls, [])

    async def test_revoked_token_is_never_passed_to_progress_callback(self):
        events = []
        async def revoke_and_emit(callback):
            bound = current_agent_execution_context()
            self.authorization.revoke(user_id=bound.user_id, grant_id=bound.task_grant_id)
            await callback({"type": "answer_delta", "delta": "protected text"})
        async def collect(event):
            events.append(event)
        self.agent.action = revoke_and_emit
        with self.assertRaises(AIControlError):
            await self.chat(callback=collect)
        self.assertFalse(any(event["type"] == "answer_delta" for event in events))
        self.assertEqual(self.record().status, "failed")
        self.assertEqual(len(self.agent.calls), 1)
        self.assertEqual(self.conversations.list(user_id="alice")[0]["message_count"], 0)

    async def test_revocation_after_model_without_streaming_prevents_persistence(self):
        async def revoke(_):
            bound = current_agent_execution_context()
            self.authorization.revoke(user_id=bound.user_id, grant_id=bound.task_grant_id)
        self.agent.action = revoke
        with self.assertRaises(AIControlError):
            await self.chat()
        self.assertEqual(self.conversations.list(user_id="alice")[0]["message_count"], 0)

    async def test_revocation_at_persist_progress_prevents_storage(self):
        async def collect(event):
            if event.get("phase") == "persist":
                bound = current_agent_execution_context()
                self.authorization.revoke(user_id=bound.user_id, grant_id=bound.task_grant_id)
        with self.assertRaises(AIControlError):
            await self.chat(callback=collect)
        self.assertEqual(self.conversations.list(user_id="alice")[0]["message_count"], 0)

    async def test_revocation_on_complete_prevents_coordinator_result_storage(self):
        async def collect(event):
            if event.get("phase") == "complete":
                bound = current_agent_execution_context()
                self.authorization.revoke(user_id=bound.user_id, grant_id=bound.task_grant_id)
        with self.assertRaises(AIControlError):
            await self.chat(callback=collect)
        self.assertEqual(self.record().status, "failed")
        self.assertNotIn("assistant_message", self.record().response_payload)

    async def test_coordinator_checks_authorization_after_cache_storage_before_return(self):
        original = self.store.complete
        def complete_and_revoke(**kwargs):
            original(**kwargs)
            bound = current_agent_execution_context()
            self.authorization.revoke(user_id=bound.user_id, grant_id=bound.task_grant_id)
        with patch.object(self.store, "complete", side_effect=complete_and_revoke):
            with self.assertRaises(AIControlError):
                await self.chat()
        self.assertEqual(self.record().status, "failed")

    async def test_cache_callback_revocation_blocks_remaining_events_and_return(self):
        await self.chat()
        events = []
        async def collect(event):
            events.append(event)
            self.authorization.revoke(user_id="alice", grant_id=event["authorization"]["id"])
        with self.assertRaises(AIControlError):
            await self.chat(callback=collect)
        self.assertEqual([event["phase"] for event in events], ["authorization"])

    async def test_authorization_denial_is_not_retried_or_converted_to_model_error(self):
        denied = task_auth.AgentAuthorizationError("resource_denied", "denied")
        async def fail(_):
            raise denied
        self.agent.action = fail
        with self.assertRaises(AIControlError) as rejected:
            await self.chat()
        self.assertIs(rejected.exception, denied)
        self.assertEqual(len(self.agent.calls), 1)

    async def test_cancellation_leaves_root_grant_active_for_children(self):
        async def cancel(_):
            raise asyncio.CancelledError()
        self.agent.action = cancel
        with self.assertRaises(asyncio.CancelledError):
            await self.chat()
        bound = self.agent.calls[0]["context"]
        self.authorization.check_context(bound)
        self.assertEqual(self.record().status, "failed")
        self.assertIsNone(current_agent_execution_context().task_grant_id)

    async def test_foreign_conversation_cannot_supply_report_reference(self):
        conversation = self.conversations.create(user_id="bob")
        with self.assertRaises(ConversationNotFoundError):
            await self.chat(self.request(conversation_id=conversation["id"], portfolio_id=None))
        self.assertEqual(self.grants(), [])

    async def test_fund_report_priority_does_not_restore_stale_portfolio_reference(self):
        previous = {"research_reference": {"job_id": "job_old", "portfolio_id": PORT_A},
            "fund_report_reference": {"fund_code": "000001", "document_ids": ["doc1", "doc2"]}}
        self.assertIsNone(resolve_report_reference(self.request(portfolio_id=None), previous))

    async def test_expired_failed_request_cannot_mint_a_replacement_grant(self):
        async def fail(_):
            raise RuntimeError("offline failure")
        self.agent.action = fail
        with self.assertRaises(RuntimeError):
            await self.chat()
        self.now += timedelta(minutes=31)
        with self.assertRaises(task_auth.AgentAuthorizationError) as rejected:
            await self.chat()
        self.assertEqual(rejected.exception.code, "grant_expired")
        self.assertEqual(len(self.grants()), 1)
        self.assertEqual(len(self.agent.calls), 1)

    async def test_cache_without_original_grant_is_not_replayed(self):
        await self.chat()
        with self.database.session() as session, session.begin():
            row = session.get(AgentChatExecutionModel, ("alice", self.record().idempotency_key))
            payload = deepcopy(row.response_payload)
            payload["assistant_message"]["run_metadata"].pop("task_authorization")
            row.response_payload = payload
        with self.assertRaises(task_auth.AgentAuthorizationError) as rejected:
            await self.chat()
        self.assertEqual(rejected.exception.code, "grant_required")
        self.assertEqual(len(self.agent.calls), 1)

    async def test_product_result_without_authorization_is_rejected_before_cache_storage(self):
        # 老测试直接 mock 整个应用层时也要提供授权回执，不能因此放宽产品结果边界。
        legacy = await self.service.chat(self.request())
        with patch.object(self.service, "chat", AsyncMock(return_value=legacy)), \
                patch.object(self.store, "complete", wraps=self.store.complete) as complete:
            with self.assertRaises(task_auth.AgentAuthorizationError) as rejected:
                await self.chat()
            complete.assert_not_called()
        self.assertEqual(rejected.exception.code, "grant_required")
        self.assertEqual(self.record().status, "failed")

    async def test_openfga_create_and_authorize_do_not_block_the_async_event_loop(self):
        loop, loop_thread = asyncio.get_running_loop(), get_ident()
        calls = []
        def checked(operation):
            def call(*args, **kwargs):
                calls.append(get_ident())
                tick = Event()
                loop.call_soon_threadsafe(tick.set)
                self.assertTrue(tick.wait(1), "OpenFGA blocked the event loop")
                return operation(*args, **kwargs)
            return call
        with patch.object(self.policy, "grant", side_effect=checked(self.policy.grant)), \
                patch.object(self.policy, "check", side_effect=checked(self.policy.check)):
            await self.chat()
            await self.chat()
        self.assertGreaterEqual(len(calls), 4)
        self.assertTrue(all(thread != loop_thread for thread in calls))

    async def test_simultaneous_same_key_requests_share_one_grant_and_one_model_call(self):
        first, second = await asyncio.gather(self.chat(), self.chat())
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(len(self.grants()), 1)
        self.assertEqual(len(self.agent.calls), 1)

    async def test_cross_scope_report_is_denied_before_reader_even_with_valid_grant(self):
        bound = self.authorization.create(self.context, run_id="report-read", agent_id="main", portfolio_ids=[PORT_A])
        reader = FakeReportReader()
        with agent_execution_scope(bound), self.assertRaises(task_auth.AgentAuthorizationError):
            resolve_report_context(self.request(portfolio_id=PORT_B, research_job_id="job_wrong"), {}, reader)
        self.assertEqual(reader.contexts, [])

    async def test_policy_outage_stops_report_preload_without_model_fallback(self):
        conversation_id = self.seed_reference()
        reader = FakeReportReader()
        self.policy.down = True
        with self.report_patch(reader), self.assertRaises(task_auth.AgentAuthorizationError) as rejected:
            await self.chat(self.request(conversation_id=conversation_id, portfolio_id=None))
        self.assertEqual(rejected.exception.status_code, 503)
        self.assertEqual(reader.contexts, [])
        self.assertEqual(self.agent.calls, [])

    def http_client(self):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.services.auth import CurrentPrincipal, get_current_principal
        from app.services.rate_limits import enforce_chat_rate_limit

        previous = dict(app.dependency_overrides)
        def restore():
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)
        self.addCleanup(restore)
        principal = CurrentPrincipal(user_id="alice", display_name="Alice", role="user", authenticated=True)
        app.dependency_overrides[get_current_principal] = lambda: principal
        app.dependency_overrides[enforce_chat_rate_limit] = lambda: principal
        patcher = patch("app.routers.workspace.agent_chat_coordinator", self.coordinator)
        patcher.start()
        self.addCleanup(patcher.stop)
        return TestClient(app)

    def test_http_uses_trusted_user_and_returns_conflict_for_same_key_changed_portfolio(self):
        client = self.http_client()
        payload = self.request(user_id="spoofed").model_dump(mode="json")
        first = client.post("/api/v1/agent/chat", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["conversation"]["user_id"], "alice")
        metadata = first.json()["assistant_message"]["run_metadata"]["task_authorization"]
        self.assertEqual(metadata["portfolio_ids"], [PORT_A])
        conflict = client.post("/api/v1/agent/chat", json={**payload, "portfolio_id": PORT_B})
        self.assertEqual(conflict.status_code, 409)
        self.authorization.revoke(user_id="alice", grant_id=metadata["task_grant_id"])
        replay = client.post("/api/v1/agent/chat", json=payload)
        self.assertEqual(replay.status_code, 403)
        self.assertEqual(len(self.agent.calls), 1)

    def test_http_sse_rejects_revoked_token_and_never_emits_result(self):
        import json
        client = self.http_client()
        async def revoke_and_emit(callback):
            bound = current_agent_execution_context()
            self.authorization.revoke(user_id=bound.user_id, grant_id=bound.task_grant_id)
            await callback({"type": "answer_delta", "delta": "protected text"})
        self.agent.action = revoke_and_emit
        response = client.post("/api/v1/agent/chat/stream", json=self.request().model_dump(mode="json"))
        events = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines()
            if line.startswith("data: ")]
        # 授权事件也可能尚在发送队列内；撤销后无需为了展示旧状态而继续发送。
        self.assertEqual(self.grants()[0].portfolio_ids, [PORT_A])
        self.assertTrue(any(event.get("code") == "grant_inactive" and event.get("status") == 403 for event in events))
        self.assertNotIn("event: answer_delta", response.text)
        self.assertNotIn("event: result", response.text)
        self.assertNotIn("protected text", response.text)

    def test_http_sse_rechecks_completed_response_before_delivery(self):
        client = self.http_client()
        original = self.coordinator.chat

        async def revoke_after_completion(*args, **kwargs):
            completed = await original(*args, **kwargs)
            scope = completed.execution_context
            self.authorization.revoke(user_id=scope.user_id, grant_id=scope.task_grant_id)
            return completed

        with patch.object(self.coordinator, "chat", side_effect=revoke_after_completion):
            response = client.post("/api/v1/agent/chat/stream", json=self.request().model_dump(mode="json"))
        self.assertIn("grant_inactive", response.text)
        self.assertNotIn("event: result", response.text)
        self.assertNotIn("event: answer_delta", response.text)
        self.assertIn("event: done", response.text)

    def test_http_sse_stops_between_final_answer_chunks(self):
        from app.routers import workspace
        client = self.http_client()
        frame = workspace._sse_frame
        sent_chunks = []

        def revoke_after_first_chunk(event, payload):
            text = frame(event, payload)
            if event == "answer_delta":
                sent_chunks.append(payload["delta"])
                grant = self.grants()[0]
                self.authorization.revoke(user_id=grant.user_id, grant_id=grant.id)
            return text

        question = "selected portfolio evidence " * 8
        with patch.object(workspace, "_sse_frame", side_effect=revoke_after_first_chunk):
            response = client.post("/api/v1/agent/chat/stream", json=self.request(message=question).model_dump(mode="json"))
        self.assertEqual(len(sent_chunks), 1)
        self.assertIn("grant_inactive", response.text)
        self.assertNotIn("event: result", response.text)
        self.assertIn("event: done", response.text)


if __name__ == "__main__":
    unittest.main()

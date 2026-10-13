"""认证、聊天幂等、并发锁、限流、Request ID 与模型 fallback 的本地测试。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.database import Database
from app.main import app
from app.models import AppUserModel, AuthSessionModel
from app.schemas import (
    AgentChatRequest,
    AgentChatResponse,
    AgentConversationMessageInfo,
    AgentConversationSummary,
    AgentPromptTrace,
    AgentWorkspaceRuntimeInfo,
    AgentWorkspaceSnapshot,
    ComprehensiveAgentChatResponse,
)
from app.services.agent_chat_runtime import (
    AgentChatCoordinator,
    AgentChatExecutionStore,
    ChatLockRegistry,
)
from app.services.auth import (
    AuthService,
    CurrentPrincipal,
    InvalidCredentialsError,
    get_current_principal,
)
from app.services.llm import ProviderFailoverRunnable
from app.services.rate_limits import SlidingWindowRateLimiter


class AuthServiceTests(unittest.TestCase):
    def test_register_hashes_secrets_and_session_authenticates(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database = Database(
                f"sqlite:///{Path(temporary_directory) / 'auth.db'}"
            )
            service = AuthService(database)
            principal, token = service.register(
                user_id="Alice",
                display_name="Alice Chen",
                password="correct-password",
            )

            with database.session() as session:
                user = session.get(AppUserModel, "alice")
                stored_session = session.get(
                    AuthSessionModel,
                    hashlib.sha256(token.encode("utf-8")).hexdigest(),
                )

            self.assertEqual(principal.role, "admin")
            self.assertNotIn("correct-password", user.password_hash)
            self.assertIsNotNone(stored_session)
            self.assertNotEqual(stored_session.token_hash, token)
            self.assertEqual(service.authenticate(token).user_id, "alice")
            with self.assertRaises(InvalidCredentialsError):
                service.login(user_id="alice", password="wrong-password")

    def test_authenticated_dependency_overrides_spoofed_body_user(self) -> None:
        principal = CurrentPrincipal(
            user_id="alice",
            display_name="Alice",
            role="user",
            authenticated=True,
        )
        app.dependency_overrides[get_current_principal] = lambda: principal
        client = TestClient(app)
        try:
            with patch(
                "app.routers.workspace.conversation_service.create",
                return_value={
                    "id": "conv_auth_test",
                    "user_id": "alice",
                    "title": "认证测试",
                    "message_count": 0,
                    "created_at": "2026-08-30T00:00:00+00:00",
                    "updated_at": "2026-08-30T00:00:00+00:00",
                },
            ) as create:
                response = client.post(
                    "/api/v1/conversations",
                    json={"user_id": "victim", "title": "认证测试"},
                )
        finally:
            app.dependency_overrides.clear()

        self.assertEqual(response.status_code, 201)
        create.assert_called_once_with(user_id="alice", title="认证测试")

    def test_strict_auth_rejects_missing_session(self) -> None:
        client = TestClient(app)
        with patch.object(settings, "auth_required", True):
            response = client.get("/api/v1/workspace?user_id=chen")

        self.assertEqual(response.status_code, 401)


class FakeComprehensiveAgent:
    def __init__(self) -> None:
        self.call_count = 0
        self.active = 0
        self.max_active = 0
        self.modes: list[str | None] = []

    async def chat(
        self,
        request,
        *,
        execution_context=None,
        progress_callback=None,
    ):
        self.call_count += 1
        self.modes.append(
            execution_context.mode if execution_context is not None else None
        )
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return build_comprehensive_response(request, sequence=self.call_count)


def build_comprehensive_response(
    request: AgentChatRequest,
    *,
    sequence: int,
) -> ComprehensiveAgentChatResponse:
    now = "2026-08-30T00:00:00+00:00"
    conversation_id = request.conversation_id or "conv_backend_test"
    run = AgentChatResponse(
        run_id=f"run-backend-{sequence}",
        user_id=request.user_id,
        question=request.message,
        answer=f"回答 {sequence}",
        used_model="fake-model",
        max_tool_rounds=request.max_tool_rounds,
        stop_reason="model_finished",
        tools_available=[],
        tool_calls=[],
        prompt_trace=AgentPromptTrace(section_count=0, total_chars=0, sections=[]),
        rounds=[],
        pipeline_steps=[],
    )
    summary = AgentConversationSummary(
        id=conversation_id,
        user_id=request.user_id,
        title="后端测试",
        message_count=2,
        created_at=now,
        updated_at=now,
    )
    return ComprehensiveAgentChatResponse(
        conversation=summary,
        user_message=AgentConversationMessageInfo(
            id=f"msg-user-{sequence}",
            conversation_id=conversation_id,
            role="user",
            content=request.message,
            created_at=now,
        ),
        assistant_message=AgentConversationMessageInfo(
            id=f"msg-assistant-{sequence}",
            conversation_id=conversation_id,
            role="assistant",
            content=run.answer,
            run_id=run.run_id,
            created_at=now,
        ),
        run=run,
        workspace=AgentWorkspaceSnapshot(
            user_id=request.user_id,
            runtime=AgentWorkspaceRuntimeInfo(
                app_name="FundScope",
                model="fake-model",
                execution_backend="local",
                database_backend="sqlite",
                tool_count=0,
                mcp_tool_count=0,
            ),
        ),
    )


class ChatCoordinationTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_idempotency_key_replays_without_second_agent_call(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database = Database(
                f"sqlite:///{Path(temporary_directory) / 'idempotency.db'}"
            )
            fake_agent = FakeComprehensiveAgent()
            coordinator = AgentChatCoordinator(
                agent=fake_agent,
                store=AgentChatExecutionStore(database),
                locks=ChatLockRegistry(),
            )
            request = AgentChatRequest(
                user_id="alice",
                message="分析当前基金",
                idempotency_key="chat-request-0001",
            )

            first = await coordinator.chat(request)
            second = await coordinator.chat(request)

        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(fake_agent.call_count, 1)
        self.assertEqual(first.response.run.run_id, second.response.run.run_id)

    async def test_same_conversation_is_serialized(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database = Database(
                f"sqlite:///{Path(temporary_directory) / 'concurrency.db'}"
            )
            fake_agent = FakeComprehensiveAgent()
            coordinator = AgentChatCoordinator(
                agent=fake_agent,
                store=AgentChatExecutionStore(database),
                locks=ChatLockRegistry(),
            )
            requests = [
                AgentChatRequest(
                    user_id="alice",
                    conversation_id="conv_serialized",
                    message=f"问题 {index}",
                )
                for index in range(2)
            ]
            await asyncio.gather(*(coordinator.chat(item) for item in requests))

        self.assertEqual(fake_agent.max_active, 1)

    # 此旧单元用例的 FakeComprehensiveAgent 不签发授权；任务缓存授权另有完整集成测试。
    @patch.object(settings, "agent_task_authorization_enabled", False)
    async def test_idempotency_cache_is_isolated_by_server_agent_mode(self) -> None:
        """同一管理员复用客户端 key 时，产品响应不能回放开发者响应。"""

        from app.services.agent_execution_context import AgentExecutionContext

        with TemporaryDirectory() as temporary_directory:
            database = Database(
                f"sqlite:///{Path(temporary_directory) / 'mode-cache.db'}"
            )
            fake_agent = FakeComprehensiveAgent()
            coordinator = AgentChatCoordinator(
                agent=fake_agent,
                store=AgentChatExecutionStore(database),
                locks=ChatLockRegistry(),
            )
            request = AgentChatRequest(
                user_id="alice",
                message="分析项目",
                idempotency_key="shared-mode-key",
            )

            product = await coordinator.chat(
                request,
                execution_context=AgentExecutionContext.product(user_id="alice"),
            )
            developer = await coordinator.chat(
                request,
                execution_context=AgentExecutionContext.developer(
                    user_id="alice",
                    role="admin",
                ),
            )

        self.assertFalse(product.replayed)
        self.assertFalse(developer.replayed)
        self.assertEqual(fake_agent.call_count, 2)
        self.assertEqual(fake_agent.modes, ["product", "developer"])


class FakeModelRunnable:
    def __init__(self, *, error: Exception | None = None, answer: str = "") -> None:
        self.error = error
        self.answer = answer
        self.response_metadata: dict = {}

    async def ainvoke(self, _input, **_kwargs):
        if self.error:
            raise self.error
        return self

    def invoke(self, _input, **_kwargs):
        if self.error:
            raise self.error
        return self

    def bind_tools(self, _tools):
        return self


class InfrastructureUtilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_provider_falls_back_only_after_primary_error(self) -> None:
        primary = FakeModelRunnable(error=TimeoutError("primary unavailable"))
        fallback = FakeModelRunnable(answer="fallback answer")
        runnable = ProviderFailoverRunnable(
            [("primary", primary), ("fallback", fallback)]
        ).bind_tools([])

        result = await runnable.ainvoke([])

        self.assertEqual(result.answer, "fallback answer")
        self.assertEqual(result.response_metadata["fundscope_provider"], "fallback")

    async def test_rate_limiter_returns_retry_time_at_limit(self) -> None:
        limiter = SlidingWindowRateLimiter()
        self.assertTrue(limiter.consume(key="alice", limit=2).allowed)
        self.assertTrue(limiter.consume(key="alice", limit=2).allowed)
        blocked = limiter.consume(key="alice", limit=2)
        self.assertFalse(blocked.allowed)
        self.assertGreaterEqual(blocked.retry_after_seconds, 1)

    async def test_request_id_is_returned_in_response_headers(self) -> None:
        response = TestClient(app).get(
            "/health",
            headers={"X-Request-ID": "request-test-001"},
        )

        self.assertEqual(response.headers["X-Request-ID"], "request-test-001")
        self.assertIn("app;dur=", response.headers["Server-Timing"])


if __name__ == "__main__":
    unittest.main()

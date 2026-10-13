"""s20 会话持久化与综合 Agent 用例测试。

这些测试只使用临时 SQLite 和 Fake Agent，不调用 DeepSeek、Embedding、Redis 或 Celery。
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.config import settings
from app.database import Database
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
from app.services.comprehensive_agent import ComprehensiveAgentService
from app.services.conversations import ConversationNotFoundError, ConversationService
from app.services.observability import AgentObservabilityService


class ConversationServiceTests(unittest.TestCase):
    def test_exchange_is_persistent_and_user_isolated(self) -> None:
        """一轮消息应持久化，另一个用户不能读取同一个 conversation_id。"""

        with TemporaryDirectory() as temporary_directory:
            database = Database(
                f"sqlite:///{Path(temporary_directory) / 'conversations.db'}"
            )
            service = ConversationService(database)
            conversation = service.create(user_id="chen")

            service.append_exchange(
                user_id="chen",
                conversation_id=conversation["id"],
                user_content="请分析 Agent Loop",
                assistant_content="Agent Loop 会反复执行工具并回填结果。",
                run_id="run-1",
                run_metadata={"stop_reason": "model_finished"},
            )

            recreated = ConversationService(Database(database.url))
            detail = recreated.detail(
                user_id="chen",
                conversation_id=conversation["id"],
            )

            self.assertEqual(detail["conversation"]["message_count"], 2)
            self.assertEqual(detail["conversation"]["title"], "请分析 Agent Loop")
            self.assertEqual(
                [message["role"] for message in detail["messages"]],
                ["user", "assistant"],
            )
            with self.assertRaises(ConversationNotFoundError):
                recreated.detail(
                    user_id="other-user",
                    conversation_id=conversation["id"],
                )

    def test_context_window_keeps_recent_messages_within_budget(self) -> None:
        """多轮历史进入模型前必须同时受消息数和字符数限制。"""

        with TemporaryDirectory() as temporary_directory:
            service = ConversationService(
                Database(f"sqlite:///{Path(temporary_directory) / 'context.db'}"),
                context_message_limit=4,
                context_char_limit=80,
            )
            conversation = service.create(user_id="chen")
            for index in range(4):
                service.append_exchange(
                    user_id="chen",
                    conversation_id=conversation["id"],
                    user_content=f"问题 {index} " + "x" * 24,
                    assistant_content=f"回答 {index} " + "y" * 24,
                    run_id=f"run-{index}",
                    run_metadata={},
                )

            context = service.context_messages(
                user_id="chen",
                conversation_id=conversation["id"],
            )

            self.assertLessEqual(len(context), 4)
            self.assertIn("3", context[-1]["content"])
            self.assertLessEqual(
                sum(len(message["content"]) for message in context),
                100,
            )

    def test_long_conversation_uses_rolling_summary_and_keeps_recent_text(self) -> None:
        """较早消息进入摘要后，最近两轮仍应以完整 user/assistant 原文进入模型。"""

        with TemporaryDirectory() as temporary_directory:
            service = ConversationService(
                Database(f"sqlite:///{Path(temporary_directory) / 'summary.db'}"),
                context_message_limit=4,
                context_char_limit=10_000,
                summary_trigger_messages=6,
                summary_recent_messages=4,
            )
            conversation = service.create(user_id="chen")
            for index in range(5):
                service.append_exchange(
                    user_id="chen",
                    conversation_id=conversation["id"],
                    user_content=f"早期事实 {index}：我的关注点是风险",
                    assistant_content=f"第 {index} 轮回答",
                    run_id=f"run-summary-{index}",
                    run_metadata={},
                )

            context = service.context_messages(
                user_id="chen",
                conversation_id=conversation["id"],
            )

            self.assertEqual(context[0]["role"], "system")
            self.assertIn("早期事实 0", context[0]["content"])
            self.assertEqual(len(context), 5)
            recent_text = " ".join(item["content"] for item in context[1:])
            self.assertIn("早期事实 3", recent_text)
            self.assertIn("早期事实 4", recent_text)


class FakeAgent:
    """记录综合应用层传入的 history，并返回一个最小合法 Agent 响应。"""

    def __init__(self) -> None:
        self.histories: list[list[dict[str, str]]] = []
        self.call_count = 0

    async def chat(self, request, *, history, progress_callback=None):
        self.histories.append(history)
        self.call_count += 1
        if progress_callback is not None:
            await progress_callback(
                {
                    "type": "phase",
                    "phase": "model",
                    "label": "Fake Agent 正在回答",
                }
            )
        return AgentChatResponse(
            run_id=f"run-{self.call_count}",
            user_id=request.user_id,
            question=request.message,
            answer=f"第 {self.call_count} 次回答",
            used_model="fake-model",
            max_tool_rounds=request.max_tool_rounds,
            stop_reason="model_finished",
            tools_available=["calculator_tool"],
            tool_calls=[],
            prompt_trace=AgentPromptTrace(
                section_count=1,
                total_chars=10,
                sections=[],
            ),
            rounds=[],
            pipeline_steps=["Receive user question"],
        )


class FakeRuntimeState:
    """避免综合测试读取全局任务、Job 和团队状态。"""

    def workspace_snapshot(self, *, user_id, tool_names):
        return AgentWorkspaceSnapshot(
            user_id=user_id,
            runtime=AgentWorkspaceRuntimeInfo(
                app_name="Agent Workspace",
                model="fake-model",
                execution_backend="local",
                database_backend="sqlite",
                tool_count=len(tool_names),
                mcp_tool_count=0,
            ),
        )


class ComprehensiveAgentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_turn_receives_first_persisted_exchange(self) -> None:
        """产品聊天第二轮必须真正把第一轮用户和助手消息传给 Agent。"""

        with TemporaryDirectory() as temporary_directory:
            database = Database(f"sqlite:///{Path(temporary_directory) / 'product.db'}")
            conversations = ConversationService(database)
            observability = AgentObservabilityService(database)
            fake_agent = FakeAgent()
            service = ComprehensiveAgentService(
                conversations=conversations,
                agent=fake_agent,
                runtime_state=FakeRuntimeState(),
                observability=observability,
            )

            first = await service.chat(
                AgentChatRequest(user_id="chen", message="第一轮问题")
            )
            second = await service.chat(
                AgentChatRequest(
                    user_id="chen",
                    conversation_id=first.conversation.id,
                    message="第二轮问题",
                )
            )

            self.assertEqual(fake_agent.histories[0], [])
            self.assertEqual(
                [item["content"] for item in fake_agent.histories[1]],
                ["第一轮问题", "第 1 次回答"],
            )
            self.assertEqual(second.conversation.message_count, 4)
            self.assertEqual(second.assistant_message.run_id, "run-2")
            self.assertEqual(
                second.assistant_message.run_metadata["stop_reason"],
                "model_finished",
            )
            self.assertEqual(
                observability.summary(user_id="chen").chat_run_count,
                2,
            )

    async def test_progress_callback_reports_service_and_agent_phases(self) -> None:
        """SSE 所需阶段事件应来自真实服务边界，而不是前端猜测计时。"""

        with TemporaryDirectory() as temporary_directory:
            database = Database(f"sqlite:///{Path(temporary_directory) / 'stream.db'}")
            service = ComprehensiveAgentService(
                conversations=ConversationService(database),
                agent=FakeAgent(),
                runtime_state=FakeRuntimeState(),
                observability=AgentObservabilityService(database),
            )
            events = []

            async def collect(event):
                events.append(event)

            await service.chat(
                AgentChatRequest(user_id="chen", message="测试流式阶段"),
                progress_callback=collect,
            )

            self.assertEqual(events[0]["phase"], "prepare")
            self.assertIn("model", [event["phase"] for event in events])
            self.assertEqual(events[-1]["phase"], "complete")


class AgentChatStreamApiTests(unittest.TestCase):
    # 仅验证 SSE 帧协议，模拟 Agent 不生成 grant；授权交付由专项集成测试覆盖。
    @patch.object(settings, "agent_task_authorization_enabled", False)
    @patch.object(settings, "ai_governance_enabled", False)
    def test_stream_emits_phase_deltas_result_and_done(self) -> None:
        """POST SSE 应保留完整最终响应，同时提供可逐步渲染的 answer_delta。"""

        now = "2026-08-29T00:00:00+00:00"
        run = AgentChatResponse(
            run_id="run-stream-test",
            user_id="chen",
            question="你好",
            answer="这是一个流式回答。",
            used_model="fake-model",
            max_tool_rounds=10,
            stop_reason="model_finished",
            tools_available=[],
            tool_calls=[],
            prompt_trace=AgentPromptTrace(
                section_count=1,
                total_chars=10,
                sections=[],
            ),
            rounds=[],
            pipeline_steps=[],
        )
        response_model = ComprehensiveAgentChatResponse(
            conversation=AgentConversationSummary(
                id="conv-stream-test",
                user_id="chen",
                title="你好",
                message_count=2,
                created_at=now,
                updated_at=now,
            ),
            user_message=AgentConversationMessageInfo(
                id="msg-user",
                conversation_id="conv-stream-test",
                role="user",
                content="你好",
                created_at=now,
            ),
            assistant_message=AgentConversationMessageInfo(
                id="msg-assistant",
                conversation_id="conv-stream-test",
                role="assistant",
                content=run.answer,
                run_id=run.run_id,
                created_at=now,
            ),
            run=run,
            workspace=AgentWorkspaceSnapshot(
                user_id="chen",
                runtime=AgentWorkspaceRuntimeInfo(
                    app_name="Agent Workspace",
                    model="fake-model",
                    execution_backend="local",
                    database_backend="sqlite",
                    tool_count=0,
                    mcp_tool_count=0,
                ),
            ),
        )

        async def fake_chat(request, *, execution_context, progress_callback):
            self.assertEqual(execution_context.mode, "product")
            await progress_callback(
                {"type": "phase", "phase": "model", "label": "模型正在回答"}
            )
            return response_model

        client = TestClient(app)
        with patch(
            "app.routers.workspace.comprehensive_agent_service.chat",
            side_effect=fake_chat,
        ):
            with client.stream(
                "POST",
                "/api/v1/agent/chat/stream",
                json={"user_id": "chen", "message": "你好"},
            ) as response:
                body = "".join(response.iter_text())

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: phase", body)
        self.assertIn("event: answer_delta", body)
        self.assertIn("event: result", body)
        self.assertIn("event: done", body)
        self.assertIn("这是一个流式回答。", body)


if __name__ == "__main__":
    unittest.main()

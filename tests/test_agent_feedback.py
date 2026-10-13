"""真实回答反馈闭环的确定性测试。

测试只使用临时 SQLite 和手工构造的 Agent 响应，不会调用 DeepSeek、Redis 或 Celery。
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.database import Database
from app.schemas import (
    AgentChatResponse,
    AgentPromptTrace,
    AgentTokenUsage,
    AgentToolCall,
)
from app.services.agent_feedback import (
    AgentFeedbackNotFoundError,
    AgentFeedbackService,
    AgentFeedbackValidationError,
)
from app.services.conversations import ConversationService
from app.services.observability import AgentObservabilityService


def make_observed_run(run_id: str) -> AgentChatResponse:
    """构造一条带工具与 Token 的运行，让反馈能够读取真实观测摘要。"""

    return AgentChatResponse(
        run_id=run_id,
        user_id="chen",
        question="这只基金现在有什么风险？",
        answer="主要风险是回撤。",
        used_model="fake-model",
        max_tool_rounds=4,
        stop_reason="model_finished",
        tools_available=["get_fund_assessment_tool"],
        tool_calls=[
            AgentToolCall(
                tool_name="get_fund_assessment_tool",
                tool_args={"fund_code": "110020"},
                tool_result='{"evidence_id":"risk:max_drawdown:110020:12m"}',
            )
        ],
        prompt_trace=AgentPromptTrace(section_count=1, total_chars=20),
        rounds=[],
        pipeline_steps=["Receive user question"],
        usage=AgentTokenUsage(
            input_tokens=120,
            output_tokens=30,
            total_tokens=150,
        ),
    )


class AgentFeedbackServiceTests(unittest.TestCase):
    def test_negative_feedback_becomes_traceable_candidate(self) -> None:
        """负向反馈应保存问答快照，并通过 run_id 读取工具与 Token。"""

        with TemporaryDirectory() as temporary_directory:
            database = Database(
                f"sqlite:///{Path(temporary_directory) / 'feedback.db'}"
            )
            conversations = ConversationService(database)
            feedbacks = AgentFeedbackService(database)
            observability = AgentObservabilityService(database)
            conversation = conversations.create(user_id="chen")
            _, _, assistant = conversations.append_exchange(
                user_id="chen",
                conversation_id=conversation["id"],
                user_content="这只基金现在有什么风险？",
                assistant_content="主要风险是回撤。",
                run_id="run-feedback-1",
                run_metadata={},
            )
            observability.record_run(
                run=make_observed_run("run-feedback-1"),
                user_id="chen",
                duration_ms=280.5,
                source="chat",
                conversation_id=conversation["id"],
            )

            saved = feedbacks.submit(
                user_id="chen",
                conversation_id=conversation["id"],
                assistant_message_id=assistant["id"],
                rating="unhelpful",
                reason_code="incomplete",
                comment="没有说明数据依据。",
                correction="补充证据编号和不确定性。",
            )
            dashboard = feedbacks.dashboard(user_id="chen")

            self.assertEqual(saved.question, "这只基金现在有什么风险？")
            self.assertEqual(saved.answer, "主要风险是回撤。")
            self.assertEqual(dashboard.pending_count, 1)
            self.assertEqual(dashboard.candidates[0].run.usage.total_tokens, 150)
            self.assertEqual(
                dashboard.candidates[0].run.tool_calls[0]["name"],
                "get_fund_assessment_tool",
            )
            detail = conversations.detail(
                user_id="chen",
                conversation_id=conversation["id"],
            )
            self.assertEqual(
                detail["messages"][-1]["feedback"]["rating"],
                "unhelpful",
            )

    def test_feedback_is_upserted_reviewed_and_user_isolated(self) -> None:
        """重复点击应更新同一记录，候选审核也不能跨用户操作。"""

        with TemporaryDirectory() as temporary_directory:
            database = Database(
                f"sqlite:///{Path(temporary_directory) / 'review.db'}"
            )
            conversations = ConversationService(database)
            feedbacks = AgentFeedbackService(database)
            conversation = conversations.create(user_id="chen")
            _, user_message, assistant = conversations.append_exchange(
                user_id="chen",
                conversation_id=conversation["id"],
                user_content="问题",
                assistant_content="回答",
                run_id="run-feedback-2",
                run_metadata={},
            )

            positive = feedbacks.submit(
                user_id="chen",
                conversation_id=conversation["id"],
                assistant_message_id=assistant["id"],
                rating="helpful",
            )
            negative = feedbacks.submit(
                user_id="chen",
                conversation_id=conversation["id"],
                assistant_message_id=assistant["id"],
                rating="unhelpful",
                reason_code="inaccurate",
            )

            self.assertEqual(positive.id, negative.id)
            self.assertEqual(feedbacks.dashboard(user_id="chen").total_count, 1)
            reviewed = feedbacks.review(
                user_id="chen",
                feedback_id=negative.id,
                status="approved",
                review_note="值得补成事实准确性用例。",
            )
            self.assertEqual(reviewed.review_status, "approved")
            reviewed_dashboard = feedbacks.dashboard(user_id="chen")
            self.assertEqual(reviewed_dashboard.approved_count, 1)
            self.assertEqual(reviewed_dashboard.pending_count, 0)
            # 已保留案例不能从界面消失，它还要继续进入人工评测草稿整理流程。
            self.assertEqual(reviewed_dashboard.approved_candidates[0].id, negative.id)

            with self.assertRaises(AgentFeedbackNotFoundError):
                feedbacks.review(
                    user_id="other-user",
                    feedback_id=negative.id,
                    status="dismissed",
                )
            with self.assertRaises(AgentFeedbackValidationError):
                feedbacks.submit(
                    user_id="chen",
                    conversation_id=conversation["id"],
                    assistant_message_id=user_message["id"],
                    rating="helpful",
                )

            # 删除会话时同步移除反馈，符合用户对聊天数据删除的直觉。
            conversations.delete(
                user_id="chen",
                conversation_id=conversation["id"],
            )
            self.assertEqual(feedbacks.dashboard(user_id="chen").total_count, 0)


if __name__ == "__main__":
    unittest.main()

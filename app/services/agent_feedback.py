"""Agent 回答反馈与评测候选服务。

这个模块不调用大模型，也不自动修改 Prompt 或评测 JSON。它只把用户对某条回答的
评价保存下来，并把负向反馈组织成待人工审核的真实案例。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.database import Database, runtime_database
from app.models import (
    AgentConversationMessageModel,
    AgentResponseFeedbackModel,
    AgentRunObservationModel,
)
from app.schemas import (
    AgentFeedbackCandidate,
    AgentFeedbackDashboard,
    AgentFeedbackRunSummary,
    AgentResponseFeedbackInfo,
    AgentTokenUsage,
)


class AgentFeedbackNotFoundError(LookupError):
    """目标回答或反馈不存在，或者不属于当前用户。"""


class AgentFeedbackValidationError(ValueError):
    """反馈目标或状态不符合反馈流程。"""


class AgentFeedbackService:
    """持久化回答反馈，并关联既有 Agent 运行观测。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def submit(
        self,
        *,
        user_id: str,
        conversation_id: str,
        assistant_message_id: str,
        rating: str,
        reason_code: str | None = None,
        comment: str = "",
        correction: str = "",
    ) -> AgentFeedbackCandidate:
        """为一条助手回答创建或覆盖反馈。

        helpful 反馈只进入统计；unhelpful 反馈会重置为 pending，等待用户在评测面板
        人工判断是否值得整理成正式回归场景。
        """

        now = datetime.now(timezone.utc)
        with self.database.session() as session, session.begin():
            assistant_message = session.get(
                AgentConversationMessageModel,
                {"user_id": user_id, "id": assistant_message_id},
            )
            if (
                assistant_message is None
                or assistant_message.conversation_id != conversation_id
            ):
                raise AgentFeedbackNotFoundError(
                    f"回答不存在：{assistant_message_id}"
                )
            if assistant_message.role != "assistant":
                raise AgentFeedbackValidationError("只能评价 Agent 的回答。")

            # 当前助手消息之前最近的一条 user 消息，就是这次回答对应的问题。
            question_message = session.scalar(
                select(AgentConversationMessageModel)
                .where(
                    AgentConversationMessageModel.user_id == user_id,
                    AgentConversationMessageModel.conversation_id == conversation_id,
                    AgentConversationMessageModel.role == "user",
                    AgentConversationMessageModel.created_at
                    <= assistant_message.created_at,
                )
                .order_by(AgentConversationMessageModel.created_at.desc())
                .limit(1)
            )

            feedback = session.scalar(
                select(AgentResponseFeedbackModel).where(
                    AgentResponseFeedbackModel.user_id == user_id,
                    AgentResponseFeedbackModel.assistant_message_id
                    == assistant_message_id,
                )
            )
            if feedback is None:
                feedback = AgentResponseFeedbackModel(
                    user_id=user_id,
                    id=f"feedback_{uuid4().hex[:16]}",
                    conversation_id=conversation_id,
                    assistant_message_id=assistant_message_id,
                    run_id=assistant_message.run_id,
                    rating=rating,
                    question_snapshot=self._bounded_text(
                        question_message.content if question_message else "",
                        4_000,
                    ),
                    answer_snapshot=self._bounded_text(
                        assistant_message.content,
                        12_000,
                    ),
                    created_at=now,
                    updated_at=now,
                )
                session.add(feedback)

            feedback.rating = rating
            feedback.reason_code = reason_code if rating == "unhelpful" else None
            feedback.comment = self._bounded_text(comment, 2_000)
            feedback.correction = (
                self._bounded_text(correction, 4_000)
                if rating == "unhelpful"
                else ""
            )
            feedback.review_status = (
                "pending" if rating == "unhelpful" else "recorded"
            )
            feedback.review_note = ""
            feedback.run_id = assistant_message.run_id
            feedback.question_snapshot = self._bounded_text(
                question_message.content if question_message else "",
                4_000,
            )
            feedback.answer_snapshot = self._bounded_text(
                assistant_message.content,
                12_000,
            )
            feedback.updated_at = now

            observation = self._get_observation(
                session,
                user_id=user_id,
                run_id=feedback.run_id,
            )
            session.flush()

        return self._candidate_to_schema(feedback, observation)

    def review(
        self,
        *,
        user_id: str,
        feedback_id: str,
        status: str,
        review_note: str = "",
    ) -> AgentFeedbackCandidate:
        """把负向反馈标记为保留或忽略，但不写入正式评测集。"""

        with self.database.session() as session, session.begin():
            feedback = session.get(
                AgentResponseFeedbackModel,
                {"user_id": user_id, "id": feedback_id},
            )
            if feedback is None:
                raise AgentFeedbackNotFoundError(f"反馈不存在：{feedback_id}")
            if feedback.rating != "unhelpful":
                raise AgentFeedbackValidationError("正向反馈不需要候选审核。")

            feedback.review_status = status
            feedback.review_note = self._bounded_text(review_note, 2_000)
            feedback.updated_at = datetime.now(timezone.utc)
            observation = self._get_observation(
                session,
                user_id=user_id,
                run_id=feedback.run_id,
            )

        return self._candidate_to_schema(feedback, observation)

    def dashboard(
        self,
        *,
        user_id: str,
        candidate_limit: int = 40,
    ) -> AgentFeedbackDashboard:
        """返回反馈统计以及尚未处理的负向案例。"""

        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(AgentResponseFeedbackModel)
                    .where(AgentResponseFeedbackModel.user_id == user_id)
                    .order_by(AgentResponseFeedbackModel.updated_at.desc())
                )
            )
            pending_rows = [
                item
                for item in rows
                if item.rating == "unhelpful"
                and item.review_status == "pending"
            ][:candidate_limit]
            approved_rows = [
                item
                for item in rows
                if item.rating == "unhelpful"
                and item.review_status == "approved"
            ][:candidate_limit]

            visible_rows = [*pending_rows, *approved_rows]
            run_ids = {item.run_id for item in visible_rows if item.run_id}
            observations: dict[str, AgentRunObservationModel] = {}
            if run_ids:
                observation_rows = list(
                    session.scalars(
                        select(AgentRunObservationModel).where(
                            AgentRunObservationModel.user_id == user_id,
                            AgentRunObservationModel.run_id.in_(run_ids),
                        )
                    )
                )
                observations = {item.run_id: item for item in observation_rows}

        return AgentFeedbackDashboard(
            user_id=user_id,
            total_count=len(rows),
            helpful_count=sum(item.rating == "helpful" for item in rows),
            unhelpful_count=sum(item.rating == "unhelpful" for item in rows),
            pending_count=sum(item.review_status == "pending" for item in rows),
            approved_count=sum(item.review_status == "approved" for item in rows),
            dismissed_count=sum(item.review_status == "dismissed" for item in rows),
            candidates=[
                self._candidate_to_schema(item, observations.get(item.run_id))
                for item in pending_rows
            ],
            approved_candidates=[
                self._candidate_to_schema(item, observations.get(item.run_id))
                for item in approved_rows
            ],
        )

    def get_candidate(
        self,
        *,
        user_id: str,
        feedback_id: str,
    ) -> AgentFeedbackCandidate:
        """按用户读取一个完整候选，供评测草稿服务复用。"""

        with self.database.session() as session:
            feedback = session.get(
                AgentResponseFeedbackModel,
                {"user_id": user_id, "id": feedback_id},
            )
            if feedback is None:
                raise AgentFeedbackNotFoundError(f"反馈不存在：{feedback_id}")
            observation = self._get_observation(
                session,
                user_id=user_id,
                run_id=feedback.run_id,
            )
            return self._candidate_to_schema(feedback, observation)

    def brief_for_messages(
        self,
        *,
        user_id: str,
        message_ids: list[str],
    ) -> dict[str, AgentResponseFeedbackInfo]:
        """批量读取消息反馈，避免会话详情逐条查询数据库。"""

        if not message_ids:
            return {}
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(AgentResponseFeedbackModel).where(
                        AgentResponseFeedbackModel.user_id == user_id,
                        AgentResponseFeedbackModel.assistant_message_id.in_(
                            message_ids
                        ),
                    )
                )
            )
        return {
            item.assistant_message_id: self._feedback_to_schema(item)
            for item in rows
        }

    def _get_observation(
        self,
        session: Any,
        *,
        user_id: str,
        run_id: str | None,
    ) -> AgentRunObservationModel | None:
        if not run_id:
            return None
        return session.get(
            AgentRunObservationModel,
            {"user_id": user_id, "run_id": run_id},
        )

    def _candidate_to_schema(
        self,
        feedback: AgentResponseFeedbackModel,
        observation: AgentRunObservationModel | None,
    ) -> AgentFeedbackCandidate:
        brief = self._feedback_to_schema(feedback)
        run_summary = None
        if observation is not None:
            run_summary = AgentFeedbackRunSummary(
                model=observation.model,
                stop_reason=observation.stop_reason,
                duration_ms=observation.duration_ms,
                round_count=observation.round_count,
                tool_calls=list(observation.tool_calls or []),
                evidence_ids=list(observation.evidence_ids or []),
                usage=AgentTokenUsage(
                    input_tokens=observation.input_tokens,
                    output_tokens=observation.output_tokens,
                    total_tokens=observation.total_tokens,
                ),
                metrics=dict(observation.metrics or {}),
            )
        return AgentFeedbackCandidate(
            **brief.model_dump(),
            user_id=feedback.user_id,
            conversation_id=feedback.conversation_id,
            assistant_message_id=feedback.assistant_message_id,
            run_id=feedback.run_id,
            question=feedback.question_snapshot,
            answer=feedback.answer_snapshot,
            run=run_summary,
        )

    def _feedback_to_schema(
        self,
        feedback: AgentResponseFeedbackModel,
    ) -> AgentResponseFeedbackInfo:
        return AgentResponseFeedbackInfo(
            id=feedback.id,
            rating=feedback.rating,
            reason_code=feedback.reason_code,
            comment=feedback.comment or "",
            correction=feedback.correction or "",
            review_status=feedback.review_status,
            review_note=feedback.review_note or "",
            created_at=self._isoformat(feedback.created_at),
            updated_at=self._isoformat(feedback.updated_at),
        )

    @staticmethod
    def _bounded_text(value: str, limit: int) -> str:
        return str(value or "").strip()[:limit]

    @staticmethod
    def _isoformat(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()


agent_feedback_service = AgentFeedbackService()

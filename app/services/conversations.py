"""主 Agent 的持久化会话服务。

这个模块只管理聊天记录，不调用模型，也不执行工具。把会话持久化从 Agent Loop 中
独立出来后，HTTP 接口、后台任务和测试都可以按需决定是否携带多轮上下文。
"""

from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select, update

from app.database import Database, runtime_database
from app.models import (
    AgentConversationMessageModel,
    AgentConversationModel,
    AgentResponseFeedbackModel,
)
from app.services.agent_feedback import AgentFeedbackService


class ConversationNotFoundError(LookupError):
    """会话不存在，或者不属于当前用户。"""


class ConversationChangedError(RuntimeError):
    """生成回答期间会话已有更新，旧请求不能覆盖新消息或已清除的投资背景。"""


class ConversationService:
    """通过 SQLAlchemy 管理会话、消息和模型上下文窗口。"""

    def __init__(
        self,
        database: Database = runtime_database,
        *,
        context_message_limit: int = 16,
        context_char_limit: int = 16_000,
        summary_trigger_messages: int = 18,
        summary_recent_messages: int = 10,
        summary_char_limit: int = 6_000,
        summary_message_char_limit: int = 600,
    ) -> None:
        self.database = database
        self.context_message_limit = context_message_limit
        self.context_char_limit = context_char_limit
        self.summary_trigger_messages = max(4, summary_trigger_messages)
        self.summary_recent_messages = max(2, summary_recent_messages)
        self.summary_char_limit = max(1_000, summary_char_limit)
        self.summary_message_char_limit = max(120, summary_message_char_limit)
        self.database.create_schema()
        # 会话详情和反馈使用同一个 Database，临时测试库也不会误读全局运行库。
        self.feedbacks = AgentFeedbackService(database)

    def create(self, *, user_id: str, title: str | None = None) -> dict[str, Any]:
        """创建空会话；第一轮成功回答后会自动生成更有意义的标题。"""

        now = datetime.now(timezone.utc)
        conversation = AgentConversationModel(
            user_id=user_id,
            id=f"conv_{uuid4().hex[:16]}",
            title=self._clean_title(title) if title else "新对话",
            created_at=now,
            updated_at=now,
        )

        with self.database.session() as session, session.begin():
            session.add(conversation)

        return self._conversation_to_dict(conversation, message_count=0)

    def ensure(
        self,
        *,
        user_id: str,
        conversation_id: str | None,
    ) -> dict[str, Any]:
        """复用已有会话；前端第一次发送消息时没有 ID，就自动创建。"""

        if conversation_id:
            return self.get(user_id=user_id, conversation_id=conversation_id)
        return self.create(user_id=user_id)

    def get(self, *, user_id: str, conversation_id: str) -> dict[str, Any]:
        """读取一个属于当前用户的会话摘要。"""

        with self.database.session() as session:
            conversation = self._get_model(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            message_count = session.scalar(
                select(func.count())
                .select_from(AgentConversationMessageModel)
                .where(
                    AgentConversationMessageModel.user_id == user_id,
                    AgentConversationMessageModel.conversation_id == conversation_id,
                )
            ) or 0
            return self._conversation_to_dict(
                conversation,
                message_count=int(message_count),
            )

    def list(self, *, user_id: str, limit: int = 40) -> list[dict[str, Any]]:
        """按最近更新时间返回侧边栏会话列表。"""

        with self.database.session() as session:
            conversations = list(
                session.scalars(
                    select(AgentConversationModel)
                    .where(AgentConversationModel.user_id == user_id)
                    .order_by(AgentConversationModel.updated_at.desc())
                    .limit(limit)
                )
            )
            count_rows = session.execute(
                select(
                    AgentConversationMessageModel.conversation_id,
                    func.count(),
                )
                .where(AgentConversationMessageModel.user_id == user_id)
                .group_by(AgentConversationMessageModel.conversation_id)
            ).all()

        counts = {conversation_id: int(count) for conversation_id, count in count_rows}
        return [
            self._conversation_to_dict(
                conversation,
                message_count=counts.get(conversation.id, 0),
            )
            for conversation in conversations
        ]

    def detail(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        """读取会话以及按时间排列的全部可见消息。"""

        with self.database.session() as session:
            conversation = self._get_model(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            messages = list(
                session.scalars(
                    select(AgentConversationMessageModel)
                    .where(
                        AgentConversationMessageModel.user_id == user_id,
                        AgentConversationMessageModel.conversation_id
                        == conversation_id,
                    )
                    .order_by(AgentConversationMessageModel.created_at.asc())
                )
            )

        feedback_by_message = self.feedbacks.brief_for_messages(
            user_id=user_id,
            message_ids=[message.id for message in messages],
        )

        return {
            "conversation": self._conversation_to_dict(
                conversation,
                message_count=len(messages),
            ),
            "messages": [
                self._message_to_dict(
                    message,
                    feedback=feedback_by_message.get(message.id),
                )
                for message in messages
            ],
        }

    def context_messages(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> list[dict[str, str]]:
        """为模型读取一个有明确上限的最近对话窗口。

        Context Compact 负责当前 AgentRun 中的大型工具结果；这里负责跨请求的聊天历史。
        两道边界一起工作，避免一个长期会话无限扩大模型输入。
        """

        with self.database.session() as session:
            conversation = self._get_model(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            total_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(AgentConversationMessageModel)
                    .where(
                        AgentConversationMessageModel.user_id == user_id,
                        AgentConversationMessageModel.conversation_id
                        == conversation_id,
                    )
                )
                or 0
            )
            summarized_count = min(
                int(conversation.summary_message_count or 0),
                total_count,
            )
            # 摘要覆盖过的消息不再重复放进近期原文窗口。SQLite 查询按最新消息倒序，
            # 最后仍会在 Python 中翻回正常对话顺序。
            unsummarized_count = max(0, total_count - summarized_count)
            recent_limit = min(self.context_message_limit, unsummarized_count)
            newest_first = list(
                session.scalars(
                    select(AgentConversationMessageModel)
                    .where(
                        AgentConversationMessageModel.user_id == user_id,
                        AgentConversationMessageModel.conversation_id
                        == conversation_id,
                    )
                    .order_by(AgentConversationMessageModel.created_at.desc())
                    .limit(recent_limit)
                )
            )
            rolling_summary = str(conversation.rolling_summary or "").strip()

        # 摘要以独立 SystemMessage 放在旧历史最前面；主 Agent 的系统提示词仍由
        # PromptBuilder 生成。摘要也占字符预算，避免“做了摘要反而无限增长”。
        summary_item: dict[str, str] | None = None
        used_chars = 0
        if rolling_summary:
            summary_content = f"以下是较早对话的滚动摘要：\n{rolling_summary}"
            if len(summary_content) > self.context_char_limit:
                summary_content = summary_content[-self.context_char_limit :]
            summary_item = {"role": "system", "content": summary_content}
            used_chars = len(summary_content)

        # 从最新消息向前装填剩余字符预算，再反转为正常对话顺序。
        selected: list[dict[str, str]] = []
        for message in newest_first:
            remaining = self.context_char_limit - used_chars
            if remaining <= 0:
                break

            content = message.content
            if len(content) > remaining:
                content = f"{content[:remaining]}\n[较早消息已截断]"

            selected.append({"role": message.role, "content": content})
            used_chars += len(content)

        selected.reverse()
        if summary_item is not None:
            selected.insert(0, summary_item)
        return selected

    def investment_context(self, *, user_id: str, conversation_id: str) -> dict[str, Any]:
        """读取最近一次成功回答携带的背景快照，不扫描或猜测旧聊天原文。

        每次新回答都会保存一份完整的小状态，因此它不受滚动摘要和上下文截断影响。
        旧会话没有该字段时从空状态开始；删除会话会连同消息一起删除这些背景。
        """

        return dict(self.latest_run_metadata(user_id=user_id, conversation_id=conversation_id).get("investment_context") or {})

    def latest_run_metadata(self, *, user_id: str, conversation_id: str) -> dict[str, Any]:
        """业务背景与报告引用都读取最后一次成功回答，不受聊天摘要截断影响。"""
        with self.database.session() as session:
            self._get_model(session, user_id=user_id, conversation_id=conversation_id)
            latest = session.scalar(
                select(AgentConversationMessageModel)
                .where(
                    AgentConversationMessageModel.user_id == user_id,
                    AgentConversationMessageModel.conversation_id == conversation_id,
                    AgentConversationMessageModel.role == "assistant",
                )
                .order_by(AgentConversationMessageModel.created_at.desc())
                .limit(1)
            )
            return dict(latest.run_metadata or {}) if latest else {}

    def append_exchange(
        self,
        *,
        user_id: str,
        conversation_id: str,
        user_content: str,
        assistant_content: str,
        run_id: str,
        run_metadata: dict[str, Any],
        expected_updated_at: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """在一个事务里保存一轮用户消息和 Agent 最终回答。"""

        user_message = AgentConversationMessageModel(
            user_id=user_id,
            id=f"msg_{uuid4().hex[:16]}",
            conversation_id=conversation_id,
            role="user",
            content=user_content,
            run_id=None,
            run_metadata={},
            created_at=datetime.now(timezone.utc),
        )
        assistant_message = AgentConversationMessageModel(
            user_id=user_id,
            id=f"msg_{uuid4().hex[:16]}",
            conversation_id=conversation_id,
            role="assistant",
            content=assistant_content,
            run_id=run_id,
            run_metadata=run_metadata,
            created_at=datetime.now(timezone.utc),
        )
        # 把本次新提取条件的临时出处绑定到真正的用户消息 ID。历史字段已经绑定过，
        # 不会因新一轮回答而失去最初出处；复制后再改，避免影响调用方的运行态。
        background = run_metadata.get("investment_context")
        if isinstance(background, dict):
            metadata = deepcopy(run_metadata)
            for fact in metadata["investment_context"].get("facts", {}).values():
                if str(fact.get("source_turn", "")).startswith("context_"):
                    fact["source_turn"] = user_message.id
            assistant_message.run_metadata = metadata

        with self.database.session() as session, session.begin():
            if expected_updated_at is not None:
                # 同进程已有聊天锁；这里是数据库层的乐观并发校验，覆盖其他进程或
                # 直接 Service 调用。比较与写入是同一条 UPDATE，不能先查再无条件写。
                changed = session.execute(
                    update(AgentConversationModel)
                    .where(
                        AgentConversationModel.user_id == user_id,
                        AgentConversationModel.id == conversation_id,
                        AgentConversationModel.updated_at == datetime.fromisoformat(expected_updated_at),
                    )
                    .values(updated_at=assistant_message.created_at)
                )
                if changed.rowcount != 1:
                    raise ConversationChangedError("会话已有新消息或背景更新，请重新发送本次问题；旧回答未保存。")
            conversation = self._get_model(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            existing_count = session.scalar(
                select(func.count())
                .select_from(AgentConversationMessageModel)
                .where(
                    AgentConversationMessageModel.user_id == user_id,
                    AgentConversationMessageModel.conversation_id == conversation_id,
                )
            ) or 0

            # 只有默认标题会自动改名，用户手动设置的标题保持不变。
            if conversation.title == "新对话":
                conversation.title = self._title_from_message(user_content)
            conversation.updated_at = assistant_message.created_at
            session.add_all([user_message, assistant_message])

        # 只在一整轮消息成功提交后推进摘要水位。摘要是下一轮的上下文优化，不应让
        # 当前回答落库事务变成一个包含大量历史扫描的长事务。
        self._roll_summary_if_needed(
            user_id=user_id,
            conversation_id=conversation_id,
        )

        summary = self._conversation_to_dict(
            conversation,
            message_count=int(existing_count) + 2,
        )
        return (
            summary,
            self._message_to_dict(user_message),
            self._message_to_dict(assistant_message),
        )

    def _roll_summary_if_needed(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> None:
        """分批把较早原文提取成滚动摘要，并始终保留最近几条完整消息。

        这里采用确定性的“角色 + 原文摘录”，没有额外调用大模型，因此不会引入摘要
        幻觉，也不会让每轮对话额外消耗一次模型请求。它牺牲少量语言精炼，换来事实
        可追溯和稳定成本，适合当前 FundScope 的业务底座。
        """

        with self.database.session() as session, session.begin():
            conversation = self._get_model(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            total_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(AgentConversationMessageModel)
                    .where(
                        AgentConversationMessageModel.user_id == user_id,
                        AgentConversationMessageModel.conversation_id
                        == conversation_id,
                    )
                )
                or 0
            )
            covered_count = min(
                int(conversation.summary_message_count or 0),
                total_count,
            )
            if total_count - covered_count <= self.summary_trigger_messages:
                return

            target_covered_count = max(
                covered_count,
                total_count - self.summary_recent_messages,
            )
            batch_size = target_covered_count - covered_count
            if batch_size <= 0:
                return

            messages = list(
                session.scalars(
                    select(AgentConversationMessageModel)
                    .where(
                        AgentConversationMessageModel.user_id == user_id,
                        AgentConversationMessageModel.conversation_id
                        == conversation_id,
                    )
                    .order_by(
                        AgentConversationMessageModel.created_at.asc(),
                        AgentConversationMessageModel.id.asc(),
                    )
                    .offset(covered_count)
                    .limit(batch_size)
                )
            )
            if not messages:
                return

            summary_lines = [self._summary_line(message) for message in messages]
            existing_summary = str(conversation.rolling_summary or "").strip()
            combined = "\n".join(
                part
                for part in (existing_summary, *summary_lines)
                if part
            )
            conversation.rolling_summary = self._bound_summary(combined)
            conversation.summary_message_count = covered_count + len(messages)
            conversation.summary_updated_at = datetime.now(timezone.utc)

    def _summary_line(self, message: AgentConversationMessageModel) -> str:
        """把一条历史消息压成一行尽量忠实的摘录。"""

        role_label = "用户" if message.role == "user" else "助手"
        content = re.sub(r"\s+", " ", message.content).strip()
        if len(content) > self.summary_message_char_limit:
            content = f"{content[: self.summary_message_char_limit]}..."
        return f"{role_label}：{content}"

    def _bound_summary(self, summary: str) -> str:
        """摘要达到上限时保留最近的累计内容，并明确标记较早部分已省略。"""

        if len(summary) <= self.summary_char_limit:
            return summary
        marker = "[更早的摘要内容已省略]\n"
        tail = summary[-(self.summary_char_limit - len(marker)) :]
        # 尽量从完整行开始，避免第一行出现半个句子。
        if "\n" in tail:
            tail = tail.split("\n", 1)[1]
        return f"{marker}{tail}"

    def delete(self, *, user_id: str, conversation_id: str) -> None:
        """删除一个会话及其消息，不影响长期 Memory 和任务数据。"""

        with self.database.session() as session, session.begin():
            conversation = self._get_model(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            # 删除聊天时同步删除其反馈，避免用户以为数据已清除但候选仍被保留。
            session.execute(
                delete(AgentResponseFeedbackModel).where(
                    AgentResponseFeedbackModel.user_id == user_id,
                    AgentResponseFeedbackModel.conversation_id == conversation_id,
                )
            )
            session.execute(
                delete(AgentConversationMessageModel).where(
                    AgentConversationMessageModel.user_id == user_id,
                    AgentConversationMessageModel.conversation_id == conversation_id,
                )
            )
            session.delete(conversation)

    def _get_model(
        self,
        session: Any,
        *,
        user_id: str,
        conversation_id: str,
    ) -> AgentConversationModel:
        conversation = session.get(
            AgentConversationModel,
            {"user_id": user_id, "id": conversation_id},
        )
        if conversation is None:
            raise ConversationNotFoundError(f"会话不存在：{conversation_id}")
        return conversation

    def _conversation_to_dict(
        self,
        conversation: AgentConversationModel,
        *,
        message_count: int,
    ) -> dict[str, Any]:
        return {
            "id": conversation.id,
            "user_id": conversation.user_id,
            "title": conversation.title,
            "message_count": message_count,
            "created_at": conversation.created_at.isoformat(),
            "updated_at": conversation.updated_at.isoformat(),
        }

    def _message_to_dict(
        self,
        message: AgentConversationMessageModel,
        *,
        feedback: Any | None = None,
    ) -> dict[str, Any]:
        return {
            "id": message.id,
            "conversation_id": message.conversation_id,
            "role": message.role,
            "content": message.content,
            "run_id": message.run_id,
            "run_metadata": dict(message.run_metadata or {}),
            "feedback": (
                feedback.model_dump(mode="json")
                if feedback is not None
                else None
            ),
            "created_at": message.created_at.isoformat(),
        }

    def _title_from_message(self, message: str) -> str:
        collapsed = re.sub(r"\s+", " ", message).strip()
        if len(collapsed) <= 32:
            return collapsed
        return f"{collapsed[:32]}..."

    def _clean_title(self, title: str) -> str:
        collapsed = re.sub(r"\s+", " ", title).strip()
        return collapsed[:120] or "新对话"


conversation_service = ConversationService()

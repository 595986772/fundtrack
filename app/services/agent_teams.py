"""s15 Agent Teams 的持久化成员注册表与消息总线。

这一章学习的不是“再调用一次 Subagent”，而是让多个 Agent 拥有稳定身份，并通过
可查询的 inbox/outbox 通信：

main-agent -> Message Bus -> teammate 独立 Agent Loop -> Message Bus -> main-agent

成员和消息保存在 SQLAlchemy 数据库中。Docker 模式下 API 与 Celery Worker 连接同一个
PostgreSQL；本地模式下则使用同一 SQLite 文件，所以消息不会只存在某个进程的内存里。

阅读顺序建议：先看 spawn_teammate，再看 send_message，接着沿着 Background Job 找到
execute_teammate_message，最后看 _save_reply 和 _conversation_context。
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.database import Database, runtime_database
from app.models import (
    AgentAutonomySessionModel,
    AgentTeammateModel,
    AgentTeamMessageModel,
)


# main-agent 是逻辑上的团队负责人，不需要占用 agent_teammates 表中的一行。
MAIN_AGENT_NAME = "main-agent"
# 状态与能力都使用有限集合，避免把模型随意生成的字符串直接写入控制面。
TEAMMATE_STATUSES = {"idle", "queued", "working", "failed", "offline"}
ALLOWED_CAPABILITIES = {
    "calculate",
    "rag_search",
    "read_project",
    "search_project",
    "write_workspace",
}
# 兼容 s15-s17：不显式配置能力时，仍只授予原来的四种只读能力。
# Worktree 写权限必须在创建 teammate 时明确选择，不能因“空列表=默认”而意外获得。
DEFAULT_CAPABILITIES = {
    "calculate",
    "rag_search",
    "read_project",
    "search_project",
}

MAX_TEAMMATES_PER_USER = 12
MAX_MESSAGES_PER_INBOX = 100
MAX_MESSAGE_CHARS = 4_000
MAX_INSTRUCTIONS_CHARS = 2_000
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,49}$")


def utc_now() -> datetime:
    """统一生成带时区的 UTC 时间。"""

    return datetime.now(timezone.utc)


class AgentTeamService:
    """管理长期 Teammate、普通文本消息和 teammate AgentRun。

    这个 service 同时被 REST、主 Agent tools 和 Celery Worker 调用。三种入口共享同一套
    校验与状态迁移，避免“前端能做、模型工具却走另一套逻辑”。
    """

    def __init__(self, database: Database = runtime_database) -> None:
        # 默认连接应用级数据库；测试可以注入临时 SQLite Database，做到完全隔离。
        self.database = database
        self.database.create_schema()

    def spawn_teammate(
        self,
        *,
        user_id: str,
        name: str,
        role: str,
        instructions: str = "",
        capabilities: list[str] | None = None,
        teammate_id: str | None = None,
    ) -> dict[str, Any]:
        """创建一个有稳定身份的 teammate，但不会自动给它分配任务。"""

        # 第一步只做输入规范化。数据库中始终保存清洗后的稳定身份。
        normalized_user_id = self._normalize_user_id(user_id)
        normalized_name = self._normalize_participant(name, allow_main=False)
        normalized_role = self._normalize_role(role)
        normalized_instructions = self._normalize_instructions(instructions)
        normalized_capabilities = self._normalize_capabilities(capabilities)
        normalized_id = self._normalize_teammate_id(
            teammate_id or f"mate-{uuid4().hex[:10]}"
        )

        # 数量上限保护教学项目不被模型无限扩员；唯一性最终仍由数据库约束保证。
        with self.database.session() as session:
            current_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(AgentTeammateModel)
                    .where(AgentTeammateModel.user_id == normalized_user_id)
                )
                or 0
            )
        if current_count >= MAX_TEAMMATES_PER_USER:
            raise ValueError(f"每个用户最多创建 {MAX_TEAMMATES_PER_USER} 个 teammate。")

        # 新成员只完成“注册”，初始状态是 idle，不会在创建时自动调用模型。
        now = utc_now()
        model = AgentTeammateModel(
            user_id=normalized_user_id,
            id=normalized_id,
            name=normalized_name,
            role=normalized_role,
            instructions=normalized_instructions,
            capabilities=normalized_capabilities,
            status="idle",
            current_job_id=None,
            last_result=None,
            last_error=None,
            created_at=now,
            updated_at=now,
        )

        # 一个短事务完成插入；同名/同 ID 冲突在这里统一翻译成可读业务错误。
        try:
            with self.database.session() as session, session.begin():
                session.add(model)
        except IntegrityError as exc:
            raise ValueError(
                f"teammate 名称或 ID 已存在：{normalized_name}"
            ) from exc

        return {
            "ok": True,
            "teammate": self._teammate_to_dict(model),
            "message": "Teammate 已注册，现在可以通过 Message Bus 给它发送工作。",
        }

    def get_teammate(self, *, user_id: str, name: str) -> dict[str, Any]:
        """按当前用户和稳定名称读取一个 teammate。"""

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_name = self._normalize_participant(name, allow_main=False)
        with self.database.session() as session:
            model = self._require_teammate_model(
                session,
                user_id=normalized_user_id,
                name=normalized_name,
            )
            return {"ok": True, "teammate": self._teammate_to_dict(model)}

    def list_teammates(
        self,
        *,
        user_id: str,
        max_results: int = 30,
    ) -> dict[str, Any]:
        """列出一个用户的长期 teammate，按最近更新时间倒序。"""

        normalized_user_id = self._normalize_user_id(user_id)
        safe_limit = max(1, min(int(max_results), 100))
        statement = (
            select(AgentTeammateModel)
            .where(AgentTeammateModel.user_id == normalized_user_id)
            .order_by(AgentTeammateModel.updated_at.desc())
            .limit(safe_limit)
        )
        with self.database.session() as session:
            models = list(session.scalars(statement))
        return {
            "ok": True,
            "user_id": normalized_user_id,
            "count": len(models),
            "teammates": [self._teammate_to_dict(model) for model in models],
        }

    def send_message(
        self,
        *,
        user_id: str,
        recipient: str,
        content: str,
        sender: str = MAIN_AGENT_NAME,
        dispatch: bool = True,
        max_rounds: int = 3,
        max_retries: int = 1,
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        """把消息写入 inbox，并可选择立即创建一个 teammate 后台 AgentRun。

        dispatch=False 只体现 Message Bus；dispatch=True 才继续进入 Background Job。
        无论是否执行模型，消息都会先持久化，所以发送行为本身可追踪。
        """

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_sender = self._normalize_participant(sender, allow_main=True)
        normalized_recipient = self._normalize_participant(recipient, allow_main=True)
        normalized_content = self._normalize_message(content)
        safe_rounds = max(1, min(int(max_rounds), 5))
        safe_retries = max(0, min(int(max_retries), 3))
        safe_timeout = max(1, min(int(timeout_seconds), 1800))

        # 自己给自己发消息在当前教学协议里没有明确意义，直接阻止。
        if normalized_sender == normalized_recipient:
            raise ValueError("sender 和 recipient 不能是同一个成员。")

        # main-agent 是虚拟的团队负责人，不需要出现在 teammate 表中；其他参与者必须存在。
        with self.database.session() as session:
            if normalized_sender != MAIN_AGENT_NAME:
                sender_model = self._require_teammate_model(
                    session,
                    user_id=normalized_user_id,
                    name=normalized_sender,
                )
                if sender_model.status == "offline":
                    raise ValueError(f"teammate 已离线：{normalized_sender}")
            if normalized_recipient != MAIN_AGENT_NAME:
                recipient_model = self._require_teammate_model(
                    session,
                    user_id=normalized_user_id,
                    name=normalized_recipient,
                )
                if recipient_model.status == "offline":
                    raise ValueError(f"teammate 已离线：{normalized_recipient}")

        # s17：目标已有自治 Worker 时，消息只需进入 inbox。现有 IDLE/WORK 循环会在
        # 下一个检查点读取它，不应该再为同一 teammate 启动第二个并发 Worker。
        autonomy_session: dict[str, Any] | None = None
        if dispatch and normalized_recipient != MAIN_AGENT_NAME:
            autonomy_session = self._active_autonomy_session(
                user_id=normalized_user_id,
                teammate_name=normalized_recipient,
            )

        # 先把普通消息作为独立业务事实保存。自治唤醒模式下关联现有 Job，便于审计。
        message_id = f"msg-{uuid4().hex[:12]}"
        message_model = AgentTeamMessageModel(
            user_id=normalized_user_id,
            id=message_id,
            sender=normalized_sender,
            recipient=normalized_recipient,
            content=normalized_content,
            job_id=(autonomy_session or {}).get("job_id"),
            in_reply_to=None,
            read_at=None,
            created_at=utc_now(),
        )
        with self.database.session() as session, session.begin():
            session.add(message_model)

        # 发给 main-agent 的消息只是回信/通知，不再递归派发一个主 Agent Job。
        job_result: dict[str, Any] | None = None
        if (
            dispatch
            and normalized_recipient != MAIN_AGENT_NAME
            and autonomy_session is None
        ):
            # job_id 可以由 message_id 提前确定。先把消息关联和 queued 状态写入数据库，
            # 再向 Redis 投递，避免一个极快的 Worker 已经完成后，API 才把 idle 覆盖回 queued。
            expected_job_id = f"team-{message_id}"
            with self.database.session() as session, session.begin():
                message = self._require_message_model(
                    session,
                    user_id=normalized_user_id,
                    message_id=message_id,
                )
                teammate = self._require_teammate_model(
                    session,
                    user_id=normalized_user_id,
                    name=normalized_recipient,
                )
                message.job_id = expected_job_id
                teammate.status = "queued"
                teammate.current_job_id = expected_job_id
                teammate.last_error = None
                teammate.updated_at = utc_now()
                session.flush()
                message_model = message

            try:
                # 到这里才接触 Executor。函数会立即拿回 Job 快照，不等待 teammate 回答。
                job_result = self._dispatch_teammate_job(
                    user_id=normalized_user_id,
                    teammate_name=normalized_recipient,
                    message_id=message_id,
                    content=normalized_content,
                    max_rounds=safe_rounds,
                    max_retries=safe_retries,
                    timeout_seconds=safe_timeout,
                )
            except Exception as exc:
                # 消息已经是有效历史，提交失败时不删除它；只把成员状态标为 failed。
                self._set_teammate_failure(
                    user_id=normalized_user_id,
                    name=normalized_recipient,
                    error=str(exc),
                    clear_job=True,
                )
                raise RuntimeError(f"消息已保存，但 teammate AgentRun 提交失败：{exc}") from exc

        return {
            "ok": True,
            "message": self._message_to_dict(message_model),
            "job": job_result["job"] if job_result is not None else None,
            "dispatched": bool(job_result) or autonomy_session is not None,
            "delivery_mode": (
                "autonomy_wakeup"
                if autonomy_session is not None
                else "background_job"
                if job_result is not None
                else "message_only"
            ),
            "autonomy_session_id": (
                autonomy_session["id"] if autonomy_session is not None else None
            ),
        }

    def _active_autonomy_session(
        self,
        *,
        user_id: str,
        teammate_name: str,
    ) -> dict[str, str] | None:
        """用当前 Service 的数据库查询 s17 会话，测试和生产都保持同一依赖边界。"""

        statement = (
            select(AgentAutonomySessionModel)
            .where(
                AgentAutonomySessionModel.user_id == user_id,
                AgentAutonomySessionModel.teammate_name == teammate_name,
                AgentAutonomySessionModel.status.in_(
                    {"queued", "running", "idle", "working", "stopping"}
                ),
            )
            .order_by(AgentAutonomySessionModel.updated_at.desc())
            .limit(1)
        )
        with self.database.session() as session:
            model = session.scalar(statement)
            if model is None:
                return None
            return {"id": model.id, "job_id": model.job_id}

    def check_inbox(
        self,
        *,
        user_id: str,
        recipient: str = MAIN_AGENT_NAME,
        unread_only: bool = False,
        mark_read: bool = False,
        max_results: int = 30,
    ) -> dict[str, Any]:
        """读取一个成员的 inbox；是否标记已读由调用方明确决定。"""

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_recipient = self._normalize_participant(recipient, allow_main=True)
        safe_limit = max(1, min(int(max_results), MAX_MESSAGES_PER_INBOX))

        # main-agent 是虚拟收件人；查看 teammate inbox 前则先确认成员真实存在。
        with self.database.session() as session:
            if normalized_recipient != MAIN_AGENT_NAME:
                self._require_teammate_model(
                    session,
                    user_id=normalized_user_id,
                    name=normalized_recipient,
                )

        # 查询条件始终带 user_id + recipient，这就是当前 demo 的租户隔离边界。
        filters = [
            AgentTeamMessageModel.user_id == normalized_user_id,
            AgentTeamMessageModel.recipient == normalized_recipient,
        ]
        if unread_only:
            filters.append(AgentTeamMessageModel.read_at.is_(None))

        statement = (
            select(AgentTeamMessageModel)
            .where(*filters)
            .order_by(AgentTeamMessageModel.created_at.desc())
            .limit(safe_limit)
        )
        with self.database.session() as session, session.begin():
            models = list(session.scalars(statement))
            # “读取”和“消费”分开：默认查看不会改变状态，只有显式 mark_read 才写 read_at。
            if mark_read:
                now = utc_now()
                for model in models:
                    if model.read_at is None:
                        model.read_at = now
                session.flush()

            # 数据库为了 limit 先倒序查询；返回时恢复时间正序，阅读对话更自然。
            messages = [self._message_to_dict(model) for model in reversed(models)]

        unread_statement = (
            select(func.count())
            .select_from(AgentTeamMessageModel)
            .where(
                AgentTeamMessageModel.user_id == normalized_user_id,
                AgentTeamMessageModel.recipient == normalized_recipient,
                AgentTeamMessageModel.read_at.is_(None),
            )
        )
        with self.database.session() as session:
            unread_count = int(session.scalar(unread_statement) or 0)

        return {
            "ok": True,
            "user_id": normalized_user_id,
            "recipient": normalized_recipient,
            "count": len(messages),
            "unread_count": unread_count,
            "messages": messages,
        }

    def recent_messages(
        self,
        *,
        user_id: str,
        max_results: int = 12,
    ) -> list[dict[str, Any]]:
        """返回当前用户最近的团队消息，供接口 trace 和前端观察。"""

        normalized_user_id = self._normalize_user_id(user_id)
        safe_limit = max(1, min(int(max_results), 50))
        statement = (
            select(AgentTeamMessageModel)
            .where(AgentTeamMessageModel.user_id == normalized_user_id)
            .order_by(AgentTeamMessageModel.created_at.desc())
            .limit(safe_limit)
        )
        with self.database.session() as session:
            models = list(session.scalars(statement))
        return [self._message_to_dict(model) for model in reversed(models)]

    def index_for_prompt(self, *, user_id: str, max_items: int = 8) -> str:
        """生成轻量 Team 索引，让主 Agent 发现成员和新回复。

        这里故意不标记已读，也不把完整历史塞进 system prompt。模型只先看到成员状态和
        未读摘要，需要正文时再调用 check_team_inbox_tool。
        """

        teammates = self.list_teammates(user_id=user_id, max_results=max_items)["teammates"]
        inbox = self.check_inbox(
            user_id=user_id,
            recipient=MAIN_AGENT_NAME,
            unread_only=True,
            mark_read=False,
            max_results=max_items,
        )

        if not teammates:
            return "当前还没有 Agent Team teammate。"

        lines = ["当前 Agent Team 成员："]
        for teammate in teammates:
            lines.append(
                f"- [{teammate['status']}] {teammate['name']} / {teammate['role']} "
                f"(job={teammate.get('current_job_id') or 'none'})"
            )
        if inbox["messages"]:
            lines.append("main-agent 未读 inbox：")
            for message in inbox["messages"]:
                preview = message["content"][:160].replace("\n", " ")
                lines.append(f"- {message['sender']}: {preview}")
        else:
            lines.append("main-agent 当前没有未读团队消息。")
        return "\n".join(lines)

    async def execute_teammate_message(
        self,
        *,
        user_id: str,
        teammate_name: str,
        message_id: str,
        max_rounds: int = 3,
    ) -> dict[str, Any]:
        """在 Worker 中让指定 teammate 处理 inbox 消息并写回一条回复。

        这是 s15 真正执行模型的入口，通常由 JobRunner 在 local 线程或 Celery Worker 中
        调用，而不是由发送消息的 HTTP 请求直接调用。
        """

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_name = self._normalize_participant(teammate_name, allow_main=False)
        normalized_message_id = self._normalize_message_id(message_id)

        # Worker 异常发生在“回复已写入、Job 还没完成”之间时，Celery 可能重新投递。
        # 先找 in_reply_to 可以避免第二次调用模型和写入重复回复。
        existing_reply = self._find_reply(
            user_id=normalized_user_id,
            message_id=normalized_message_id,
        )
        if existing_reply is not None:
            self._set_teammate_idle(
                user_id=normalized_user_id,
                name=normalized_name,
                last_result=existing_reply["content"],
            )
            return {
                "summary": existing_reply["content"],
                "workflow": "teammate_message",
                "teammate": normalized_name,
                "reply": existing_reply,
                "deduplicated": True,
            }

        # 在调用模型前，把“谁在处理哪条消息”校验清楚并把状态切到 working。
        with self.database.session() as session, session.begin():
            teammate = self._require_teammate_model(
                session,
                user_id=normalized_user_id,
                name=normalized_name,
            )
            message = self._require_message_model(
                session,
                user_id=normalized_user_id,
                message_id=normalized_message_id,
            )
            if message.recipient != normalized_name:
                raise ValueError("这条消息不属于指定 teammate 的 inbox。")
            teammate.status = "working"
            teammate.last_error = None
            teammate.updated_at = utc_now()
            session.flush()
            teammate_data = self._teammate_to_dict(teammate)
            message_content = message.content

        # 只取 main-agent 与当前 teammate 的对话，不泄露其他 teammate 的消息。
        history = self._conversation_context(
            user_id=normalized_user_id,
            teammate_name=normalized_name,
            exclude_message_id=normalized_message_id,
            max_items=12,
        )
        # s16 协议事件与普通聊天分表保存，但在 teammate 独立上下文中统一呈现。
        from app.services.team_protocols import team_protocol_service

        protocol_context = team_protocol_service.context_for_teammate(
            user_id=normalized_user_id,
            teammate_name=normalized_name,
            max_items=10,
        )
        history = f"{history}\n\n{protocol_context}"

        try:
            # 复用 s06 的受限 Agent Loop，但传入长期 teammate 身份和 Message Bus 历史。
            # 它拥有干净的独立 messages，不会继承主 Agent 的临时上下文。
            from app.services.subagent import subagent_service

            response = await subagent_service.run(
                task=message_content,
                max_rounds=max(1, min(int(max_rounds), 5)),
                agent_name=normalized_name,
                role=str(teammate_data["role"]),
                instructions=str(teammate_data["instructions"]),
                conversation_context=history,
                capabilities=list(teammate_data["capabilities"]),
                user_id=normalized_user_id,
            )
            answer = str(response.get("answer") or "").strip()
            if not answer:
                raise RuntimeError("Teammate Agent 没有返回可保存的回答。")

            reply = self._save_reply(
                user_id=normalized_user_id,
                teammate_name=normalized_name,
                original_message_id=normalized_message_id,
                content=answer,
            )
            # 先保存回复，再清空 current_job_id。即使后续 Job 完成写入失败，回复仍可去重。
            self._set_teammate_idle(
                user_id=normalized_user_id,
                name=normalized_name,
                last_result=answer,
            )
            return {
                "summary": answer,
                "workflow": "teammate_message",
                "teammate": normalized_name,
                "reply": reply,
                "agent_response": response,
                "deduplicated": False,
            }
        except Exception as exc:
            # 错误既会由 Job Runtime 保存到 Job，也会留在 teammate.last_error 方便成员列表观察。
            self._set_teammate_failure(
                user_id=normalized_user_id,
                name=normalized_name,
                error=str(exc),
            )
            raise

    async def execute_autonomous_task(
        self,
        *,
        user_id: str,
        teammate_name: str,
        task: dict[str, Any],
        max_rounds: int = 3,
        workspace_root: str | None = None,
        worktree_name: str | None = None,
    ) -> dict[str, Any]:
        """让 teammate 在 s17 WORK 阶段执行一条已被 Harness 认领的 Task。

        这里不负责扫描、claim 或 completed 状态；这些确定性动作属于
        ``AutonomousAgentService``。本方法只负责构建 teammate 独立上下文、调用受限
        Agent Loop，并把最终结果写回 main-agent inbox。
        """

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_name = self._normalize_participant(teammate_name, allow_main=False)
        task_id = str(task.get("id") or "").strip()
        subject = str(task.get("subject") or "").strip()
        description = str(task.get("description") or "").strip()
        if not task_id or not subject:
            raise ValueError("自治 Task 缺少 id 或 subject。")

        with self.database.session() as session:
            teammate = self._require_teammate_model(
                session,
                user_id=normalized_user_id,
                name=normalized_name,
            )
            if teammate.status == "offline":
                raise ValueError(f"teammate 已离线：{normalized_name}")
            teammate_data = self._teammate_to_dict(teammate)

        history = self._conversation_context(
            user_id=normalized_user_id,
            teammate_name=normalized_name,
            max_items=12,
        )
        from app.services.team_protocols import team_protocol_service

        protocol_context = team_protocol_service.context_for_teammate(
            user_id=normalized_user_id,
            teammate_name=normalized_name,
            max_items=10,
        )
        conversation_context = f"{history}\n\n{protocol_context}"
        task_prompt = (
            f"你已由 Harness 自动认领任务 {task_id}。\n"
            f"任务标题：{subject}\n"
            f"任务说明：{description or '无额外说明'}\n"
            f"隔离工作区：{worktree_name or '未绑定，仅允许只读分析'}。\n"
            "请使用现有受限工具完成工作，并给出可交付的结果。"
        )

        try:
            from app.services.subagent import subagent_service

            response = await subagent_service.run(
                task=task_prompt,
                max_rounds=max(1, min(int(max_rounds), 5)),
                agent_name=normalized_name,
                role=str(teammate_data["role"]),
                instructions=str(teammate_data["instructions"]),
                conversation_context=conversation_context,
                capabilities=list(teammate_data["capabilities"]),
                user_id=normalized_user_id,
                workspace_root=workspace_root,
                worktree_name=worktree_name,
            )
            answer = str(response.get("answer") or "").strip()
            if not answer:
                raise RuntimeError("Autonomous teammate 没有返回可保存的任务结果。")

            # 自治任务没有一条 main-agent 原消息可作为 in_reply_to，因此发送独立 result 通知。
            notification = self.send_message(
                user_id=normalized_user_id,
                sender=normalized_name,
                recipient=MAIN_AGENT_NAME,
                content=f"[Autonomous Task {task_id}] {answer}",
                dispatch=False,
            )["message"]
            return {
                "summary": answer,
                "workflow": "autonomous_task",
                "teammate": normalized_name,
                "task_id": task_id,
                "worktree_name": worktree_name,
                "notification": notification,
                "agent_response": response,
            }
        except Exception as exc:
            self._set_teammate_failure(
                user_id=normalized_user_id,
                name=normalized_name,
                error=str(exc),
            )
            raise

    def _dispatch_teammate_job(
        self,
        *,
        user_id: str,
        teammate_name: str,
        message_id: str,
        content: str,
        max_rounds: int,
        max_retries: int,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """延迟导入 Job Service，避免 Agent Team 与 Job Runtime 循环导入。

        message_id 同时生成确定性 job_id 和 idempotency_key；重复提交同一条消息只会得到
        原 Job，不会再创建第二次 teammate AgentRun。
        """

        from app.services.background_jobs import background_job_service

        return background_job_service.start_job(
            user_id=user_id,
            job_id=f"team-{message_id}",
            title=f"{MAIN_AGENT_NAME} -> {teammate_name}",
            job_type="teammate_message",
            prompt=content,
            max_tool_rounds=max_rounds,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
            idempotency_key=f"team-message:{message_id}",
            teammate_name=teammate_name,
            team_message_id=message_id,
        )

    def _save_reply(
        self,
        *,
        user_id: str,
        teammate_name: str,
        original_message_id: str,
        content: str,
    ) -> dict[str, Any]:
        """把 teammate 的回答写进 main-agent inbox，并把原消息标记为已读。"""

        # “查重、标记原消息已读、插入回复”放在同一个事务里，保持消息关系一致。
        with self.database.session() as session, session.begin():
            existing = session.scalar(
                select(AgentTeamMessageModel).where(
                    AgentTeamMessageModel.user_id == user_id,
                    AgentTeamMessageModel.in_reply_to == original_message_id,
                )
            )
            if existing is not None:
                return self._message_to_dict(existing)

            original = self._require_message_model(
                session,
                user_id=user_id,
                message_id=original_message_id,
            )
            # teammate 已消费原消息，所以原消息在它的 inbox 中变成已读。
            original.read_at = original.read_at or utc_now()
            reply = AgentTeamMessageModel(
                user_id=user_id,
                id=f"msg-{uuid4().hex[:12]}",
                sender=teammate_name,
                recipient=MAIN_AGENT_NAME,
                content=self._normalize_message(content),
                job_id=original.job_id,
                in_reply_to=original_message_id,
                read_at=None,
                created_at=utc_now(),
            )
            session.add(reply)
            session.flush()
            return self._message_to_dict(reply)

    def _find_reply(self, *, user_id: str, message_id: str) -> dict[str, Any] | None:
        """按 in_reply_to 查找既有回复，是 Worker 重投时的业务幂等检查。"""

        statement = select(AgentTeamMessageModel).where(
            AgentTeamMessageModel.user_id == user_id,
            AgentTeamMessageModel.in_reply_to == message_id,
        )
        with self.database.session() as session:
            model = session.scalar(statement)
            return self._message_to_dict(model) if model is not None else None

    def _conversation_context(
        self,
        *,
        user_id: str,
        teammate_name: str,
        exclude_message_id: str,
        max_items: int,
    ) -> str:
        """只加载该 teammate 与 main-agent 的最近消息，形成独立团队上下文。"""

        # SQL 条件明确限定双向的一对一会话，不把团队全局消息混入该 teammate 上下文。
        pair_filter = or_(
            and_(
                AgentTeamMessageModel.sender == MAIN_AGENT_NAME,
                AgentTeamMessageModel.recipient == teammate_name,
            ),
            and_(
                AgentTeamMessageModel.sender == teammate_name,
                AgentTeamMessageModel.recipient == MAIN_AGENT_NAME,
            ),
        )
        statement = (
            select(AgentTeamMessageModel)
            .where(
                AgentTeamMessageModel.user_id == user_id,
                AgentTeamMessageModel.id != exclude_message_id,
                pair_filter,
            )
            .order_by(AgentTeamMessageModel.created_at.desc())
            .limit(max(1, min(max_items, 30)))
        )
        with self.database.session() as session:
            models = list(session.scalars(statement))
        if not models:
            return "这是该 teammate 收到的第一条团队消息。"

        lines = ["该 teammate 与 main-agent 的最近 Message Bus 历史："]
        for model in reversed(models):
            lines.append(f"{model.sender} -> {model.recipient}: {model.content}")
        return "\n".join(lines)

    def _set_teammate_idle(self, *, user_id: str, name: str, last_result: str) -> None:
        """成功后恢复可接单状态，并保留最近结果摘要供列表观察。"""

        with self.database.session() as session, session.begin():
            teammate = self._require_teammate_model(
                session,
                user_id=user_id,
                name=name,
            )
            teammate.status = "idle"
            teammate.current_job_id = None
            teammate.last_result = last_result[:4_000]
            teammate.last_error = None
            teammate.updated_at = utc_now()

    def _set_teammate_failure(
        self,
        *,
        user_id: str,
        name: str,
        error: str,
        clear_job: bool = False,
    ) -> None:
        """失败时保存可观察错误；提交阶段失败可额外清除尚未运行的 job 引用。"""

        with self.database.session() as session, session.begin():
            teammate = self._require_teammate_model(
                session,
                user_id=user_id,
                name=name,
            )
            teammate.status = "failed"
            if clear_job:
                teammate.current_job_id = None
            teammate.last_error = error[:4_000]
            teammate.updated_at = utc_now()

    def _require_teammate_model(
        self,
        session,
        *,
        user_id: str,
        name: str,
    ) -> AgentTeammateModel:
        """在当前 Session 中按 user_id + name 查成员，找不到就统一抛 KeyError。"""

        statement = select(AgentTeammateModel).where(
            AgentTeammateModel.user_id == user_id,
            AgentTeammateModel.name == name,
        )
        model = session.scalar(statement)
        if model is None:
            raise KeyError(f"找不到 teammate：{name}")
        return model

    def _require_message_model(
        self,
        session,
        *,
        user_id: str,
        message_id: str,
    ) -> AgentTeamMessageModel:
        """利用联合主键读取消息，确保同 ID 也不能跨 user_id 查询。"""

        model = session.get(AgentTeamMessageModel, (user_id, message_id))
        if model is None:
            raise KeyError(f"找不到团队消息：{message_id}")
        return model

    def _teammate_to_dict(self, model: AgentTeammateModel) -> dict[str, Any]:
        return {
            "id": model.id,
            "user_id": model.user_id,
            "name": model.name,
            "role": model.role,
            "instructions": model.instructions,
            "capabilities": list(model.capabilities or []),
            "status": model.status,
            "current_job_id": model.current_job_id,
            "last_result": model.last_result,
            "last_error": model.last_error,
            "created_at": self._iso(model.created_at),
            "updated_at": self._iso(model.updated_at),
        }

    def _message_to_dict(self, model: AgentTeamMessageModel) -> dict[str, Any]:
        return {
            "id": model.id,
            "user_id": model.user_id,
            "sender": model.sender,
            "recipient": model.recipient,
            "content": model.content,
            "job_id": model.job_id,
            "in_reply_to": model.in_reply_to,
            "is_read": model.read_at is not None,
            "read_at": self._iso(model.read_at),
            "created_at": self._iso(model.created_at),
        }

    def _normalize_user_id(self, value: str) -> str:
        normalized = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}", normalized):
            raise ValueError("user_id 只能包含字母、数字、下划线和连字符，最长 50 位。")
        return normalized

    def _normalize_participant(self, value: str, *, allow_main: bool) -> str:
        normalized = str(value or "").strip()
        if allow_main and normalized == MAIN_AGENT_NAME:
            return normalized
        if not IDENTIFIER_PATTERN.fullmatch(normalized):
            raise ValueError("成员名称必须以字母开头，只能包含字母、数字、下划线和连字符。")
        if normalized == MAIN_AGENT_NAME:
            raise ValueError(f"{MAIN_AGENT_NAME} 是保留名称。")
        return normalized

    def _normalize_teammate_id(self, value: str) -> str:
        normalized = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", normalized):
            raise ValueError("teammate_id 格式非法。")
        return normalized

    def _normalize_message_id(self, value: str) -> str:
        normalized = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", normalized):
            raise ValueError("message_id 格式非法。")
        return normalized

    def _normalize_role(self, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > 100:
            raise ValueError("role 不能为空且最多 100 个字符。")
        return normalized

    def _normalize_instructions(self, value: str) -> str:
        normalized = str(value or "").strip()
        if len(normalized) > MAX_INSTRUCTIONS_CHARS:
            raise ValueError(f"instructions 最多 {MAX_INSTRUCTIONS_CHARS} 个字符。")
        return normalized

    def _normalize_capabilities(self, values: list[str] | None) -> list[str]:
        # 空列表表示 s15-s17 的默认只读能力。s18 的 write_workspace 必须显式传入。
        raw_values = values or sorted(DEFAULT_CAPABILITIES)
        normalized = sorted({str(value).strip() for value in raw_values if str(value).strip()})
        unknown = [value for value in normalized if value not in ALLOWED_CAPABILITIES]
        if unknown:
            raise ValueError(f"不支持的 teammate capabilities：{', '.join(unknown)}")
        return normalized

    def _normalize_message(self, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("团队消息不能为空。")
        if len(normalized) > MAX_MESSAGE_CHARS:
            raise ValueError(f"团队消息最多 {MAX_MESSAGE_CHARS} 个字符。")
        return normalized

    def _iso(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat(timespec="microseconds")


# REST、主 Agent tools、Prompt Builder 和 Worker 都通过这个 service 访问同一团队数据。
agent_team_service = AgentTeamService()

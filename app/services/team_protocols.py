"""s16 Team Protocols：持久化请求状态、类型分发和响应匹配。

s15 的普通 Message Bus 只能表达“某人发了一段文本”。s16 在它旁边增加一条结构化
协议通道，专门表达需要握手的协作：

    request(pending) -> response(approved | rejected)

每个请求都有 request_id。ProtocolState 表保存当前状态，ProtocolEvent 表保存请求与
响应事件。当前 FastAPI 项目没有常驻 teammate 线程，所以 shutdown_request 会进入已有
Background Job/Celery Runtime，由 Worker 完成确定性的协议分发；这不是 s17 的自主轮询。
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.database import Database, runtime_database
from app.models import (
    AgentAutonomySessionModel,
    AgentTeamProtocolEventModel,
    AgentTeamProtocolModel,
    AgentTeammateModel,
)
from app.services.agent_teams import MAIN_AGENT_NAME


PROTOCOL_TYPES = {"shutdown", "plan_approval"}
PROTOCOL_STATUSES = {"pending", "approved", "rejected"}
TERMINAL_PROTOCOL_STATUSES = {"approved", "rejected"}

REQUEST_MESSAGE_TYPES = {
    "shutdown": "shutdown_request",
    "plan_approval": "plan_approval_request",
}
RESPONSE_MESSAGE_TYPES = {
    "shutdown": "shutdown_response",
    "plan_approval": "plan_approval_response",
}

MAX_PROTOCOLS_PER_USER = 200
MAX_PAYLOAD_CHARS = 4_000
MAX_FEEDBACK_CHARS = 2_000
PARTICIPANT_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,49}$")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def utc_now() -> datetime:
    """统一生成带时区的 UTC 时间。"""

    return datetime.now(timezone.utc)


class TeamProtocolService:
    """管理 s16 的 ProtocolState、typed events 和合法状态迁移。

    REST、Agent tools 和 Worker 都调用这个 service。模型只能提出协议工具调用，真正的
    request_id 生成、参与者校验、类型匹配和状态变化始终由后端完成。
    """

    def __init__(self, database: Database = runtime_database) -> None:
        # 测试可以注入临时 SQLite；Docker 中默认连接 API/Worker 共用的 PostgreSQL。
        self.database = database
        self.database.create_schema()

    def submit_plan(
        self,
        *,
        user_id: str,
        teammate_name: str,
        plan: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """由 teammate 向 main-agent 提交一个待审批计划。

        这里只创建 pending 请求，不自动审批，也不启动新 Job。主 Agent 必须在后续请求中
        明确调用 review_team_plan_tool，才能进入 approved/rejected。
        """

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_teammate = self._normalize_participant(
            teammate_name,
            allow_main=False,
        )
        normalized_plan = self._normalize_payload(plan, field_name="plan")
        normalized_request_id = self._normalize_request_id(
            request_id or f"req-{uuid4().hex[:12]}"
        )

        result = self._create_request(
            user_id=normalized_user_id,
            request_id=normalized_request_id,
            protocol_type="plan_approval",
            sender=normalized_teammate,
            target=MAIN_AGENT_NAME,
            payload=normalized_plan,
            job_id=None,
            queue_target=False,
        )
        result["message"] = "计划审批请求已提交，等待 main-agent 明确批准或拒绝。"
        return result

    def request_shutdown(
        self,
        *,
        user_id: str,
        teammate_name: str,
        reason: str = "",
        dispatch: bool = True,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """main-agent 向 teammate 发起结构化关闭握手。

        dispatch=True 时先持久化 pending 请求和 typed event，再创建内部协议 Job。Worker
        会按 message_type 分发 shutdown_request，并写入匹配 request_id 的 response。
        """

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_teammate = self._normalize_participant(
            teammate_name,
            allow_main=False,
        )
        normalized_reason = self._normalize_payload(
            reason or "请完成当前收尾并安全停止。",
            field_name="reason",
        )
        normalized_request_id = self._normalize_request_id(
            request_id or f"req-{uuid4().hex[:12]}"
        )
        # s17 自治 teammate 已经有常驻 Worker。此时只需写 pending request，让它自己的
        # IDLE/WORK 检查点处理；再创建协议 Job 会形成两个 Worker 同时改同一成员状态。
        autonomous_session = self._active_autonomy_session(
            user_id=normalized_user_id,
            teammate_name=normalized_teammate,
        )
        use_protocol_job = dispatch and autonomous_session is None
        expected_job_id = (
            f"protocol-{normalized_request_id}" if use_protocol_job else None
        )

        result = self._create_request(
            user_id=normalized_user_id,
            request_id=normalized_request_id,
            protocol_type="shutdown",
            sender=MAIN_AGENT_NAME,
            target=normalized_teammate,
            payload=normalized_reason,
            job_id=expected_job_id,
            queue_target=use_protocol_job,
        )

        job_result: dict[str, Any] | None = None
        if use_protocol_job:
            try:
                job_result = self._dispatch_protocol_job(
                    user_id=normalized_user_id,
                    request_id=normalized_request_id,
                    teammate_name=normalized_teammate,
                    reason=normalized_reason,
                )
            except Exception as exc:
                # 请求事件仍是有效审计记录；基础设施失败不等于 teammate 拒绝请求。
                self._mark_dispatch_failure(
                    user_id=normalized_user_id,
                    request_id=normalized_request_id,
                    teammate_name=normalized_teammate,
                    error=str(exc),
                )
                raise RuntimeError(
                    f"关闭请求已保存，但协议 Job 提交失败：{exc}"
                ) from exc

        result["job"] = job_result["job"] if job_result is not None else None
        result["dispatched"] = bool(job_result) or autonomous_session is not None
        if autonomous_session is not None:
            result["message"] = (
                "shutdown_request 已保存，运行中的自治 Worker 会在下一个检查点优先处理。"
            )
        elif dispatch:
            result["message"] = (
                "shutdown_request 已进入 Worker，稍后通过同一 request_id 匹配响应。"
            )
        else:
            result["message"] = "shutdown_request 已保存，但尚未分发。"
        return result

    def review_plan(
        self,
        *,
        user_id: str,
        request_id: str,
        approve: bool,
        feedback: str = "",
    ) -> dict[str, Any]:
        """由 main-agent 审批 plan_approval 请求，并写入结构化 response。"""

        return self._respond_protocol(
            user_id=self._normalize_user_id(user_id),
            request_id=self._normalize_request_id(request_id),
            actor=MAIN_AGENT_NAME,
            approve=bool(approve),
            feedback=self._normalize_feedback(feedback),
            expected_protocol_type="plan_approval",
            allow_duplicate=False,
        )

    def dispatch_protocol_request(
        self,
        *,
        user_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Worker 按协议类型分发 pending 请求；当前只处理 shutdown_request。

        这里对应参考源码的 dispatch_message。重复投递时，如果请求已经 resolved，会返回
        原 response，而不会再次改变状态或写重复事件。
        """

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_request_id = self._normalize_request_id(request_id)

        with self.database.session() as session, session.begin():
            protocol = self._require_protocol_model(
                session,
                user_id=normalized_user_id,
                request_id=normalized_request_id,
                for_update=True,
            )
            if protocol.protocol_type != "shutdown":
                raise ValueError(
                    f"Worker 不支持自动分发协议：{protocol.protocol_type}"
                )
            teammate = self._require_teammate_model(
                session,
                user_id=normalized_user_id,
                name=protocol.target,
            )
            if protocol.status == "pending":
                teammate.status = "working"
                teammate.last_error = None
                teammate.updated_at = utc_now()

        try:
            # 教学版 teammate 没有常驻线程；Worker 代表其协议 dispatcher 完成安全收尾确认。
            return self._respond_protocol(
                user_id=normalized_user_id,
                request_id=normalized_request_id,
                actor=self.get_protocol(
                    user_id=normalized_user_id,
                    request_id=normalized_request_id,
                )["request"]["target"],
                approve=True,
                feedback="Teammate 已完成收尾并确认停止。",
                expected_protocol_type="shutdown",
                allow_duplicate=True,
            )
        except Exception as exc:
            self._set_teammate_error_for_protocol(
                user_id=normalized_user_id,
                request_id=normalized_request_id,
                error=str(exc),
            )
            raise

    def get_protocol(self, *, user_id: str, request_id: str) -> dict[str, Any]:
        """查询一条 ProtocolState，并附带同 request_id 的完整事件。"""

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_request_id = self._normalize_request_id(request_id)
        with self.database.session() as session:
            protocol = self._require_protocol_model(
                session,
                user_id=normalized_user_id,
                request_id=normalized_request_id,
            )
            events = self._events_for_request(
                session,
                user_id=normalized_user_id,
                request_id=normalized_request_id,
            )
            return {
                "ok": True,
                "request": self._protocol_to_dict(protocol),
                "events": [self._event_to_dict(event) for event in events],
            }

    def list_protocols(
        self,
        *,
        user_id: str,
        status: str | None = None,
        max_results: int = 30,
    ) -> dict[str, Any]:
        """列出请求状态和最近 typed events，供 REST、工具和前端统一观察。"""

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_status = self._normalize_status(status) if status else None
        safe_limit = max(1, min(int(max_results), 100))

        filters = [AgentTeamProtocolModel.user_id == normalized_user_id]
        if normalized_status is not None:
            filters.append(AgentTeamProtocolModel.status == normalized_status)

        protocol_statement = (
            select(AgentTeamProtocolModel)
            .where(*filters)
            .order_by(AgentTeamProtocolModel.updated_at.desc())
            .limit(safe_limit)
        )
        event_statement = (
            select(AgentTeamProtocolEventModel)
            .where(AgentTeamProtocolEventModel.user_id == normalized_user_id)
            .order_by(AgentTeamProtocolEventModel.created_at.desc())
            .limit(safe_limit * 2)
        )
        with self.database.session() as session:
            protocols = list(session.scalars(protocol_statement))
            events = list(session.scalars(event_statement))

        return {
            "ok": True,
            "user_id": normalized_user_id,
            "count": len(protocols),
            "protocols": [self._protocol_to_dict(item) for item in protocols],
            # 数据库倒序取 limit，接口恢复时间正序，观察握手流程更自然。
            "recent_events": [
                self._event_to_dict(item) for item in reversed(events)
            ],
        }

    def recent_index(
        self,
        *,
        user_id: str,
        max_items: int = 20,
    ) -> list[dict[str, Any]]:
        """给 `/agent/chat` 响应返回轻量 ProtocolState 列表。"""

        return list(
            self.list_protocols(
                user_id=user_id,
                max_results=max(1, min(max_items, 50)),
            )["protocols"]
        )

    def recent_events(
        self,
        *,
        user_id: str,
        max_items: int = 20,
    ) -> list[dict[str, Any]]:
        """给 `/agent/chat` 响应返回最近协议事件。"""

        return list(
            self.list_protocols(
                user_id=user_id,
                max_results=max(1, min(max_items, 50)),
            )["recent_events"]
        )

    def index_for_prompt(self, *, user_id: str, max_items: int = 8) -> str:
        """把 pending 协议和最近结果压成 system prompt 中的轻量索引。"""

        protocols = self.list_protocols(
            user_id=user_id,
            max_results=max_items,
        )["protocols"]
        if not protocols:
            return "当前没有 Team Protocol 请求。"

        lines = ["当前 Team Protocol 请求（最近更新优先）："]
        for protocol in protocols:
            payload_preview = protocol["payload"][:140].replace("\n", " ")
            lines.append(
                "- "
                f"[{protocol['status']}] {protocol['request_id']} "
                f"{protocol['protocol_type']}: {protocol['sender']} -> "
                f"{protocol['target']}, payload={payload_preview}"
            )
        return "\n".join(lines)

    def context_for_teammate(
        self,
        *,
        user_id: str,
        teammate_name: str,
        max_items: int = 10,
    ) -> str:
        """把与指定 teammate 有关的协议事件加入它自己的独立上下文。"""

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_name = self._normalize_participant(
            teammate_name,
            allow_main=False,
        )
        statement = (
            select(AgentTeamProtocolEventModel)
            .where(
                AgentTeamProtocolEventModel.user_id == normalized_user_id,
                or_(
                    AgentTeamProtocolEventModel.sender == normalized_name,
                    AgentTeamProtocolEventModel.recipient == normalized_name,
                ),
            )
            .order_by(AgentTeamProtocolEventModel.created_at.desc())
            .limit(max(1, min(int(max_items), 30)))
        )
        with self.database.session() as session:
            events = list(session.scalars(statement))
        if not events:
            return "该 teammate 当前没有 Team Protocol 事件。"

        lines = ["该 teammate 最近的结构化 Team Protocol 事件："]
        for event in reversed(events):
            decision = ""
            if event.approve is not None:
                decision = f", approve={event.approve}"
            lines.append(
                f"- {event.message_type} request_id={event.request_id}{decision}: "
                f"{event.content}"
            )
        return "\n".join(lines)

    def _create_request(
        self,
        *,
        user_id: str,
        request_id: str,
        protocol_type: str,
        sender: str,
        target: str,
        payload: str,
        job_id: str | None,
        queue_target: bool,
    ) -> dict[str, Any]:
        """在一个事务中创建 ProtocolState、request event 和可选 queued 状态。"""

        if protocol_type not in PROTOCOL_TYPES:
            raise ValueError(f"不支持的 protocol_type：{protocol_type}")

        with self.database.session() as session:
            current_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(AgentTeamProtocolModel)
                    .where(AgentTeamProtocolModel.user_id == user_id)
                )
                or 0
            )
        if current_count >= MAX_PROTOCOLS_PER_USER:
            raise ValueError(
                f"每个用户最多保留 {MAX_PROTOCOLS_PER_USER} 条 Team Protocol。"
            )

        now = utc_now()
        event_id = f"pev-{uuid4().hex[:12]}"
        protocol = AgentTeamProtocolModel(
            user_id=user_id,
            request_id=request_id,
            protocol_type=protocol_type,
            sender=sender,
            target=target,
            status="pending",
            payload=payload,
            feedback="",
            request_event_id=event_id,
            response_event_id=None,
            job_id=job_id,
            created_at=now,
            updated_at=now,
            resolved_at=None,
        )
        event = AgentTeamProtocolEventModel(
            user_id=user_id,
            id=event_id,
            request_id=request_id,
            message_type=REQUEST_MESSAGE_TYPES[protocol_type],
            sender=sender,
            recipient=target,
            content=payload,
            approve=None,
            created_at=now,
        )

        try:
            with self.database.session() as session, session.begin():
                # main-agent 是虚拟身份；其他 sender/target 必须是当前用户的真实 teammate。
                sender_model = None
                target_model = None
                if sender != MAIN_AGENT_NAME:
                    sender_model = self._require_teammate_model(
                        session,
                        user_id=user_id,
                        name=sender,
                    )
                if target != MAIN_AGENT_NAME:
                    target_model = self._require_teammate_model(
                        session,
                        user_id=user_id,
                        name=target,
                    )

                for teammate in (sender_model, target_model):
                    if teammate is not None and teammate.status == "offline":
                        raise ValueError(f"teammate 已离线：{teammate.name}")

                if queue_target and target_model is not None:
                    # 当前没有常驻 idle loop，避免关闭请求覆盖一个正在执行的普通工作。
                    if target_model.status in {"queued", "working"}:
                        raise ValueError(
                            f"teammate 正在执行其他 Job，暂不能关闭：{target_model.name}"
                        )
                    target_model.status = "queued"
                    target_model.current_job_id = job_id
                    target_model.last_error = None
                    target_model.updated_at = now

                session.add(protocol)
                session.add(event)
        except IntegrityError as exc:
            raise ValueError(f"request_id 已存在：{request_id}") from exc

        return {
            "ok": True,
            "request": self._protocol_to_dict(protocol),
            "request_event": self._event_to_dict(event),
            "job": None,
            "dispatched": False,
        }

    def _respond_protocol(
        self,
        *,
        user_id: str,
        request_id: str,
        actor: str,
        approve: bool,
        feedback: str,
        expected_protocol_type: str,
        allow_duplicate: bool,
    ) -> dict[str, Any]:
        """按 request_id 匹配响应，并校验类型、接收者和 pending 状态。

        这对应参考源码的 match_response。任何一项不匹配都不能修改 ProtocolState。
        """

        normalized_actor = self._normalize_participant(actor, allow_main=True)
        with self.database.session() as session, session.begin():
            protocol = self._require_protocol_model(
                session,
                user_id=user_id,
                request_id=request_id,
                for_update=True,
            )
            if protocol.protocol_type != expected_protocol_type:
                raise ValueError(
                    "协议响应类型不匹配："
                    f"expected={expected_protocol_type}, actual={protocol.protocol_type}"
                )
            if protocol.target != normalized_actor:
                raise ValueError(
                    f"只有协议目标 {protocol.target} 可以响应该请求。"
                )

            if protocol.status in TERMINAL_PROTOCOL_STATUSES:
                if not allow_duplicate:
                    raise ValueError(
                        f"请求 {request_id} 已经是 {protocol.status}，不能重复响应。"
                    )
                response = self._require_event_model(
                    session,
                    user_id=user_id,
                    event_id=str(protocol.response_event_id),
                )
                return {
                    "ok": True,
                    "request": self._protocol_to_dict(protocol),
                    "response_event": self._event_to_dict(response),
                    "deduplicated": True,
                    "message": "重复投递已返回原协议响应。",
                }

            now = utc_now()
            response_event = AgentTeamProtocolEventModel(
                user_id=user_id,
                id=f"pev-{uuid4().hex[:12]}",
                request_id=request_id,
                message_type=RESPONSE_MESSAGE_TYPES[protocol.protocol_type],
                sender=normalized_actor,
                recipient=protocol.sender,
                content=feedback or ("Approved" if approve else "Rejected"),
                approve=approve,
                created_at=now,
            )
            protocol.status = "approved" if approve else "rejected"
            protocol.feedback = response_event.content
            protocol.response_event_id = response_event.id
            protocol.updated_at = now
            protocol.resolved_at = now
            session.add(response_event)

            if protocol.protocol_type == "shutdown":
                teammate = self._require_teammate_model(
                    session,
                    user_id=user_id,
                    name=normalized_actor,
                )
                teammate.status = "offline" if approve else "idle"
                teammate.current_job_id = None
                teammate.last_result = response_event.content[:4_000]
                teammate.last_error = None
                teammate.updated_at = now

            session.flush()
            return {
                "ok": True,
                "request": self._protocol_to_dict(protocol),
                "response_event": self._event_to_dict(response_event),
                "deduplicated": False,
                "message": (
                    f"{protocol.protocol_type} 已"
                    f"{'批准' if approve else '拒绝'}。"
                ),
            }

    def _dispatch_protocol_job(
        self,
        *,
        user_id: str,
        request_id: str,
        teammate_name: str,
        reason: str,
    ) -> dict[str, Any]:
        """把 shutdown 协议交给已有 Background Job/Celery Runtime。"""

        # 延迟导入避免 background_jobs -> job_runtime -> team_protocols 的循环依赖。
        from app.services.background_jobs import background_job_service

        return background_job_service.start_job(
            user_id=user_id,
            job_id=f"protocol-{request_id}",
            title=f"shutdown protocol: {teammate_name}",
            job_type="teammate_protocol",
            prompt=reason,
            max_retries=1,
            timeout_seconds=120,
            idempotency_key=f"team-protocol:{request_id}",
            teammate_name=teammate_name,
            team_protocol_request_id=request_id,
        )

    def _active_autonomy_session(
        self,
        *,
        user_id: str,
        teammate_name: str,
    ) -> dict[str, str] | None:
        """查询目标是否已有 s17 自治 Worker，避免重复分发 shutdown。"""

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

    def _mark_dispatch_failure(
        self,
        *,
        user_id: str,
        request_id: str,
        teammate_name: str,
        error: str,
    ) -> None:
        """队列提交失败时保留 pending 请求，同时恢复可观察成员状态。"""

        with self.database.session() as session, session.begin():
            protocol = self._require_protocol_model(
                session,
                user_id=user_id,
                request_id=request_id,
                for_update=True,
            )
            teammate = self._require_teammate_model(
                session,
                user_id=user_id,
                name=teammate_name,
            )
            protocol.job_id = None
            protocol.updated_at = utc_now()
            teammate.status = "failed"
            teammate.current_job_id = None
            teammate.last_error = error[:4_000]
            teammate.updated_at = utc_now()

    def _set_teammate_error_for_protocol(
        self,
        *,
        user_id: str,
        request_id: str,
        error: str,
    ) -> None:
        """Worker 分发异常时把错误同步到协议目标 teammate。"""

        with self.database.session() as session, session.begin():
            protocol = self._require_protocol_model(
                session,
                user_id=user_id,
                request_id=request_id,
            )
            if protocol.target == MAIN_AGENT_NAME:
                return
            teammate = self._require_teammate_model(
                session,
                user_id=user_id,
                name=protocol.target,
            )
            teammate.status = "failed"
            teammate.current_job_id = None
            teammate.last_error = error[:4_000]
            teammate.updated_at = utc_now()

    def _events_for_request(
        self,
        session,
        *,
        user_id: str,
        request_id: str,
    ) -> list[AgentTeamProtocolEventModel]:
        statement = (
            select(AgentTeamProtocolEventModel)
            .where(
                AgentTeamProtocolEventModel.user_id == user_id,
                AgentTeamProtocolEventModel.request_id == request_id,
            )
            .order_by(AgentTeamProtocolEventModel.created_at.asc())
        )
        return list(session.scalars(statement))

    def _require_protocol_model(
        self,
        session,
        *,
        user_id: str,
        request_id: str,
        for_update: bool = False,
    ) -> AgentTeamProtocolModel:
        statement = select(AgentTeamProtocolModel).where(
            AgentTeamProtocolModel.user_id == user_id,
            AgentTeamProtocolModel.request_id == request_id,
        )
        if for_update:
            statement = statement.with_for_update()
        model = session.scalar(statement)
        if model is None:
            raise KeyError(f"找不到 Team Protocol 请求：{request_id}")
        return model

    def _require_event_model(
        self,
        session,
        *,
        user_id: str,
        event_id: str,
    ) -> AgentTeamProtocolEventModel:
        model = session.get(AgentTeamProtocolEventModel, (user_id, event_id))
        if model is None:
            raise KeyError(f"找不到 Team Protocol 事件：{event_id}")
        return model

    def _require_teammate_model(
        self,
        session,
        *,
        user_id: str,
        name: str,
    ) -> AgentTeammateModel:
        model = session.scalar(
            select(AgentTeammateModel).where(
                AgentTeammateModel.user_id == user_id,
                AgentTeammateModel.name == name,
            )
        )
        if model is None:
            raise KeyError(f"找不到 teammate：{name}")
        return model

    def _protocol_to_dict(self, model: AgentTeamProtocolModel) -> dict[str, Any]:
        return {
            "request_id": model.request_id,
            "user_id": model.user_id,
            "protocol_type": model.protocol_type,
            "sender": model.sender,
            "target": model.target,
            "status": model.status,
            "payload": model.payload,
            "feedback": model.feedback,
            "request_event_id": model.request_event_id,
            "response_event_id": model.response_event_id,
            "job_id": model.job_id,
            "created_at": self._iso(model.created_at),
            "updated_at": self._iso(model.updated_at),
            "resolved_at": self._iso(model.resolved_at),
        }

    def _event_to_dict(
        self,
        model: AgentTeamProtocolEventModel,
    ) -> dict[str, Any]:
        return {
            "id": model.id,
            "user_id": model.user_id,
            "request_id": model.request_id,
            "message_type": model.message_type,
            "sender": model.sender,
            "recipient": model.recipient,
            "content": model.content,
            "approve": model.approve,
            "created_at": self._iso(model.created_at),
        }

    def _normalize_user_id(self, value: str) -> str:
        normalized = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}", normalized):
            raise ValueError("user_id 格式非法。")
        return normalized

    def _normalize_participant(self, value: str, *, allow_main: bool) -> str:
        normalized = str(value or "").strip()
        if allow_main and normalized == MAIN_AGENT_NAME:
            return normalized
        if not PARTICIPANT_PATTERN.fullmatch(normalized):
            raise ValueError("协议参与者名称格式非法。")
        if normalized == MAIN_AGENT_NAME:
            raise ValueError(f"{MAIN_AGENT_NAME} 是保留名称。")
        return normalized

    def _normalize_request_id(self, value: str) -> str:
        normalized = str(value or "").strip()
        if not REQUEST_ID_PATTERN.fullmatch(normalized):
            raise ValueError("request_id 格式非法。")
        return normalized

    def _normalize_status(self, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in PROTOCOL_STATUSES:
            raise ValueError(
                f"protocol status 只能是：{', '.join(sorted(PROTOCOL_STATUSES))}"
            )
        return normalized

    def _normalize_payload(self, value: str, *, field_name: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{field_name} 不能为空。")
        if len(normalized) > MAX_PAYLOAD_CHARS:
            raise ValueError(f"{field_name} 最多 {MAX_PAYLOAD_CHARS} 个字符。")
        return normalized

    def _normalize_feedback(self, value: str) -> str:
        normalized = str(value or "").strip()
        if len(normalized) > MAX_FEEDBACK_CHARS:
            raise ValueError(f"feedback 最多 {MAX_FEEDBACK_CHARS} 个字符。")
        return normalized

    def _iso(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat(timespec="microseconds")


team_protocol_service = TeamProtocolService()

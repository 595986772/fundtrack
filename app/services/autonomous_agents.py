"""s17 Autonomous Agents：受控空闲轮询、任务发现和自动认领。

本模块实现 teammate 的外层生命周期：

    IDLE -> 发现消息/任务 -> WORK -> IDLE -> 超时或停止

模型仍然只负责完成某一条具体工作。是否轮询、认领哪条任务、最多做几条、何时停止，
都由 Harness 的确定性代码控制，避免把资源与并发边界交给模型自行决定。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
import time
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import func, select

from app.database import Database, runtime_database
from app.models import (
    AgentAutonomySessionModel,
    AgentTeamProtocolModel,
    AgentTeammateModel,
)
from app.services.agent_teams import AgentTeamService, agent_team_service
from app.services.tasks import TaskStoreService, task_store_service
from app.services.team_protocols import TeamProtocolService, team_protocol_service
from app.services.worktrees import WorktreeService, worktree_service


ACTIVE_SESSION_STATUSES = {"queued", "running", "idle", "working", "stopping"}
TERMINAL_SESSION_STATUSES = {"completed", "failed", "cancelled"}
SESSION_STATUSES = ACTIVE_SESSION_STATUSES | TERMINAL_SESSION_STATUSES
SIMPLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PARTICIPANT_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,49}$")
MAX_SESSIONS_PER_USER = 100


def utc_now() -> datetime:
    """统一使用带时区的 UTC 时间。"""

    return datetime.now(timezone.utc)


class AutonomousAgentService:
    """管理自治会话状态，并在 Worker 中运行 IDLE/WORK 外层循环。"""

    def __init__(
        self,
        database: Database = runtime_database,
        *,
        tasks: TaskStoreService = task_store_service,
        teams: AgentTeamService = agent_team_service,
        protocols: TeamProtocolService = team_protocol_service,
        worktrees: WorktreeService = worktree_service,
    ) -> None:
        # 测试可注入临时 SQLite、临时任务目录和对应的 Team/Protocol Service。
        self.database = database
        self.tasks = tasks
        self.teams = teams
        self.protocols = protocols
        self.worktrees = worktrees
        self.database.create_schema()

    def start_session(
        self,
        *,
        user_id: str,
        teammate_name: str,
        poll_interval_seconds: float = 2.0,
        max_idle_polls: int = 5,
        max_tasks: int = 5,
        max_rounds_per_task: int = 3,
        timeout_seconds: int = 300,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """创建自治会话并立即提交 ``autonomous_teammate`` Background Job。"""

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_name = self._normalize_teammate_name(teammate_name)
        normalized_session_id = self._normalize_session_id(
            session_id or f"auto-{uuid4().hex[:12]}"
        )
        poll_interval = max(0.05, min(float(poll_interval_seconds), 30.0))
        idle_limit = max(1, min(int(max_idle_polls), 60))
        task_limit = max(1, min(int(max_tasks), 20))
        round_limit = max(1, min(int(max_rounds_per_task), 5))
        safe_timeout = max(5, min(int(timeout_seconds), 3600))
        job_id = f"autonomy-{normalized_session_id}"
        now = utc_now()

        with self.database.session() as session, session.begin():
            teammate = self._require_teammate(
                session,
                user_id=normalized_user_id,
                teammate_name=normalized_name,
            )
            if teammate.status == "offline":
                raise ValueError(f"teammate 已离线：{normalized_name}")

            # 一名 teammate 同时只能有一个自治外层循环；否则会自己和自己抢任务。
            active = session.scalar(
                select(AgentAutonomySessionModel).where(
                    AgentAutonomySessionModel.user_id == normalized_user_id,
                    AgentAutonomySessionModel.teammate_name == normalized_name,
                    AgentAutonomySessionModel.status.in_(ACTIVE_SESSION_STATUSES),
                )
            )
            if active is not None:
                raise ValueError(
                    f"teammate 已有运行中的自治会话：{active.id}"
                )

            count = int(
                session.scalar(
                    select(func.count())
                    .select_from(AgentAutonomySessionModel)
                    .where(AgentAutonomySessionModel.user_id == normalized_user_id)
                )
                or 0
            )
            if count >= MAX_SESSIONS_PER_USER:
                raise ValueError(
                    f"每个用户最多保留 {MAX_SESSIONS_PER_USER} 条自治会话。"
                )

            model = AgentAutonomySessionModel(
                user_id=normalized_user_id,
                id=normalized_session_id,
                teammate_name=normalized_name,
                job_id=job_id,
                status="queued",
                current_task_id=None,
                completed_task_ids=[],
                poll_interval_seconds=poll_interval,
                max_idle_polls=idle_limit,
                idle_poll_count=0,
                max_tasks=task_limit,
                tasks_completed=0,
                max_rounds_per_task=round_limit,
                stop_requested=False,
                stop_reason=None,
                last_event="自治会话已创建，等待 Worker 领取。",
                last_error=None,
                created_at=now,
                updated_at=now,
                started_at=None,
                completed_at=None,
            )
            session.add(model)
            teammate.status = "queued"
            teammate.current_job_id = job_id
            teammate.last_error = None
            teammate.updated_at = now

        try:
            job_result = self._dispatch_job(
                user_id=normalized_user_id,
                teammate_name=normalized_name,
                session_id=normalized_session_id,
                job_id=job_id,
                timeout_seconds=safe_timeout,
            )
        except Exception as exc:
            self.mark_failed(
                user_id=normalized_user_id,
                session_id=normalized_session_id,
                error=f"自治 Job 提交失败：{exc}",
            )
            raise RuntimeError(f"自治会话已保存，但 Job 提交失败：{exc}") from exc

        return {
            "ok": True,
            "session": self.get_session(
                user_id=normalized_user_id,
                session_id=normalized_session_id,
            )["session"],
            "job": job_result["job"],
            "message": "自治会话已提交，teammate 将自行检查 inbox 和任务板。",
        }

    def run_session(
        self,
        *,
        user_id: str,
        session_id: str,
        check_runtime: Callable[[], None] | None = None,
        update_progress: Callable[[str], None] | None = None,
        wait: Callable[[float], None] | None = None,
    ) -> dict[str, Any]:
        """由 Worker 执行一次自治生命周期，直到停止条件成立。

        每轮顺序固定为 shutdown protocol -> 普通 inbox -> Task Board。协议优先可以保证
        teammate 不会因为不断有普通任务而无法停止。
        """

        user = self._normalize_user_id(user_id)
        autonomy_id = self._normalize_session_id(session_id)
        runtime_check = check_runtime or (lambda: None)
        progress = update_progress or (lambda _message: None)
        sleeper = wait or time.sleep
        processed_messages: list[str] = []
        task_results: list[dict[str, Any]] = []

        # Job 重试时会复用同一 autonomy session。保留上次已认领的 current_task_id，
        # 下一轮才能恢复它，而不是把 in_progress 任务遗留在任务板上。
        initial = self.get_session(user_id=user, session_id=autonomy_id)["session"]
        self._set_phase(
            user_id=user,
            session_id=autonomy_id,
            status="running",
            event="Worker 已领取自治会话，开始检查工作。",
            current_task_id=initial["current_task_id"],
            started=True,
        )

        while True:
            runtime_check()
            current = self.get_session(user_id=user, session_id=autonomy_id)["session"]

            if current["stop_requested"]:
                return self._finish(
                    user_id=user,
                    session_id=autonomy_id,
                    stop_reason="manual_stop",
                    processed_messages=processed_messages,
                    task_results=task_results,
                )

            shutdown_request = self._pending_shutdown_request(
                user_id=user,
                teammate_name=current["teammate_name"],
            )
            if shutdown_request is not None:
                self._set_phase(
                    user_id=user,
                    session_id=autonomy_id,
                    status="stopping",
                    event=(
                        "IDLE 检测到 shutdown_request："
                        f"{shutdown_request['request_id']}"
                    ),
                )
                self.protocols.dispatch_protocol_request(
                    user_id=user,
                    request_id=shutdown_request["request_id"],
                )
                return self._finish(
                    user_id=user,
                    session_id=autonomy_id,
                    stop_reason="shutdown_protocol",
                    processed_messages=processed_messages,
                    task_results=task_results,
                    preserve_offline=True,
                )

            # 普通 inbox 优先于任务板；这让用户的新指令能尽快唤醒空闲 teammate。
            inbox = self.teams.check_inbox(
                user_id=user,
                recipient=current["teammate_name"],
                unread_only=True,
                mark_read=False,
                max_results=1,
            )
            if inbox["messages"]:
                message = inbox["messages"][0]
                self._set_phase(
                    user_id=user,
                    session_id=autonomy_id,
                    status="working",
                    event=f"正在处理 inbox 消息 {message['id']}。",
                    reset_idle=True,
                )
                progress(f"{current['teammate_name']} 正在处理自主 inbox 消息。")
                asyncio.run(
                    self.teams.execute_teammate_message(
                        user_id=user,
                        teammate_name=current["teammate_name"],
                        message_id=message["id"],
                        max_rounds=current["max_rounds_per_task"],
                    )
                )
                processed_messages.append(message["id"])
                self._set_phase(
                    user_id=user,
                    session_id=autonomy_id,
                    status="idle",
                    event="inbox 消息已处理，返回 IDLE。",
                    reset_idle=True,
                )
                continue

            if current["tasks_completed"] >= current["max_tasks"]:
                return self._finish(
                    user_id=user,
                    session_id=autonomy_id,
                    stop_reason="max_tasks",
                    processed_messages=processed_messages,
                    task_results=task_results,
                )

            # 上一次执行若在模型调用中失败，Task 已经是 in_progress，不能再次 claim。
            # 先恢复当前 owner 的任务；没有待恢复任务时才扫描新的 pending Task。
            claimed = self._resumable_task(
                user_id=user,
                task_id=current["current_task_id"],
                teammate_name=current["teammate_name"],
            )
            if claimed is None and current["current_task_id"] is not None:
                self._set_phase(
                    user_id=user,
                    session_id=autonomy_id,
                    status="idle",
                    event="上次任务已不需要恢复，重新扫描 Task Board。",
                    current_task_id=None,
                )
                continue
            if claimed is None:
                claimed = self.claim_next_task(
                    user_id=user,
                    teammate_name=current["teammate_name"],
                )
            if claimed is not None:
                task = claimed["task"]
                # Task JSON 只保存安全 worktree_name。真正路径必须再次通过 SQL 状态和
                # Git 目录校验后才能进入 teammate 文件工具，不能信任 Task 自己给路径。
                workspace = self.worktrees.resolve_task_workspace(
                    user_id=user,
                    task=task,
                )
                self._set_phase(
                    user_id=user,
                    session_id=autonomy_id,
                    status="working",
                    event=f"已自动认领任务 {task['id']}，进入 WORK。",
                    current_task_id=task["id"],
                    reset_idle=True,
                )
                progress(
                    f"{current['teammate_name']} 已自动认领 {task['id']}，正在执行。"
                )
                response = asyncio.run(
                    self.teams.execute_autonomous_task(
                        user_id=user,
                        teammate_name=current["teammate_name"],
                        task=task,
                        max_rounds=current["max_rounds_per_task"],
                        workspace_root=(
                            str(workspace["path"]) if workspace else None
                        ),
                        worktree_name=(
                            str(workspace["name"]) if workspace else None
                        ),
                    )
                )
                completed = self.tasks.complete_task(
                    user_id=user,
                    task_id=task["id"],
                    owner=current["teammate_name"],
                )
                worktree_result: dict[str, Any] | None = None
                worktree_error: str | None = None
                if workspace:
                    try:
                        # 自动完成只“保留待 review”，不擅自删除、合并或丢弃修改。
                        worktree_result = self.worktrees.keep_worktree(
                            user_id=user,
                            name=str(workspace["name"]),
                        )["worktree"]
                    except Exception as exc:
                        # Task 的模型工作已经成功，生命周期标记失败不应把它伪装成未完成。
                        # 错误留在运行结果中，用户可以通过 Worktree 接口继续处理。
                        worktree_error = str(exc)
                task_results.append(
                    {
                        "task_id": task["id"],
                        "subject": task["subject"],
                        "answer": response["summary"],
                        "newly_unblocked_task_ids": completed[
                            "newly_unblocked_task_ids"
                        ],
                        "worktree": worktree_result,
                        "worktree_error": worktree_error,
                    }
                )
                self._record_task_completion(
                    user_id=user,
                    session_id=autonomy_id,
                    task_id=task["id"],
                )
                continue

            # 没有消息也没有可认领任务，才真正进入一次 IDLE poll。
            idle_count = self._record_idle_poll(
                user_id=user,
                session_id=autonomy_id,
            )
            current = self.get_session(user_id=user, session_id=autonomy_id)["session"]
            progress(
                f"IDLE poll {idle_count}/{current['max_idle_polls']}：暂无可认领任务。"
            )
            if idle_count >= current["max_idle_polls"]:
                return self._finish(
                    user_id=user,
                    session_id=autonomy_id,
                    stop_reason="idle_timeout",
                    processed_messages=processed_messages,
                    task_results=task_results,
                )
            sleeper(float(current["poll_interval_seconds"]))

    def claim_next_task(
        self,
        *,
        user_id: str,
        teammate_name: str,
    ) -> dict[str, Any] | None:
        """扫描候选任务，并逐个尝试在文件锁内 claim。

        扫描与认领之间可能有另一个 Worker 抢先，因此 claim 失败是正常竞争，不是故障。
        """

        for task in self.tasks.list_claimable_tasks(user_id=user_id, max_results=50):
            try:
                return self.tasks.claim_task(
                    user_id=user_id,
                    task_id=task["id"],
                    owner=teammate_name,
                )
            except ValueError:
                continue
        return None

    def _resumable_task(
        self,
        *,
        user_id: str,
        task_id: str | None,
        teammate_name: str,
    ) -> dict[str, Any] | None:
        """找回 Job 重试前已经由同一 teammate 认领的任务。"""

        if not task_id:
            return None
        tasks = self.tasks.list_tasks(user_id=user_id, max_results=100)["tasks"]
        task = next((item for item in tasks if item["id"] == task_id), None)
        if (
            task is None
            or task["status"] != "in_progress"
            or task["owner"] != teammate_name
        ):
            return None
        return {"ok": True, "task": task, "message": "恢复已认领任务。"}

    def request_stop(self, *, user_id: str, session_id: str) -> dict[str, Any]:
        """写入优雅停止信号；Worker 会在下一个 Harness 检查点退出。"""

        user = self._normalize_user_id(user_id)
        autonomy_id = self._normalize_session_id(session_id)
        with self.database.session() as session, session.begin():
            model = self._require_session(session, user_id=user, session_id=autonomy_id)
            if model.status in TERMINAL_SESSION_STATUSES:
                return {
                    "ok": False,
                    "session": self._session_to_dict(model),
                    "message": f"自治会话已处于终态 {model.status}。",
                }
            model.stop_requested = True
            model.status = "stopping"
            model.last_event = "已收到优雅停止请求，等待 Worker 检查点。"
            model.updated_at = utc_now()
            session.flush()
            return {
                "ok": True,
                "session": self._session_to_dict(model),
                "message": "停止信号已持久化。",
            }

    def get_session(self, *, user_id: str, session_id: str) -> dict[str, Any]:
        user = self._normalize_user_id(user_id)
        autonomy_id = self._normalize_session_id(session_id)
        with self.database.session() as session:
            model = self._require_session(session, user_id=user, session_id=autonomy_id)
            return {"ok": True, "session": self._session_to_dict(model)}

    def list_sessions(
        self,
        *,
        user_id: str,
        status: str | None = None,
        max_results: int = 30,
    ) -> dict[str, Any]:
        user = self._normalize_user_id(user_id)
        normalized_status = str(status or "").strip() or None
        if normalized_status and normalized_status not in SESSION_STATUSES:
            raise ValueError(f"不支持的自治状态：{normalized_status}")
        filters = [AgentAutonomySessionModel.user_id == user]
        if normalized_status:
            filters.append(AgentAutonomySessionModel.status == normalized_status)
        statement = (
            select(AgentAutonomySessionModel)
            .where(*filters)
            .order_by(AgentAutonomySessionModel.updated_at.desc())
            .limit(max(1, min(int(max_results), 100)))
        )
        with self.database.session() as session:
            models = list(session.scalars(statement))
        return {
            "ok": True,
            "user_id": user,
            "count": len(models),
            "sessions": [self._session_to_dict(model) for model in models],
        }

    def recent_index(self, *, user_id: str, max_items: int = 12) -> list[dict[str, Any]]:
        return list(
            self.list_sessions(user_id=user_id, max_results=max_items)["sessions"]
        )

    def index_for_prompt(self, *, user_id: str, max_items: int = 6) -> str:
        sessions = self.recent_index(user_id=user_id, max_items=max_items)
        if not sessions:
            return "当前没有 Autonomous Agent 会话。"
        lines = ["当前 Autonomous Agent 会话："]
        for item in sessions:
            lines.append(
                f"- [{item['status']}] {item['id']} teammate={item['teammate_name']} "
                f"task={item['current_task_id'] or 'none'} "
                f"completed={item['tasks_completed']}/{item['max_tasks']} "
                f"idle={item['idle_poll_count']}/{item['max_idle_polls']}"
            )
        return "\n".join(lines)

    def active_session_for_teammate(
        self,
        *,
        user_id: str,
        teammate_name: str,
    ) -> dict[str, Any] | None:
        """供 Message Bus/Team Protocol 判断是否应唤醒已有自治 Worker。"""

        with self.database.session() as session:
            model = session.scalar(
                select(AgentAutonomySessionModel)
                .where(
                    AgentAutonomySessionModel.user_id == self._normalize_user_id(user_id),
                    AgentAutonomySessionModel.teammate_name
                    == self._normalize_teammate_name(teammate_name),
                    AgentAutonomySessionModel.status.in_(ACTIVE_SESSION_STATUSES),
                )
                .order_by(AgentAutonomySessionModel.updated_at.desc())
            )
            return self._session_to_dict(model) if model is not None else None

    def mark_failed(self, *, user_id: str, session_id: str, error: str) -> None:
        """Worker 异常时同时保存会话错误和 teammate 可观察状态。"""

        user = self._normalize_user_id(user_id)
        autonomy_id = self._normalize_session_id(session_id)
        now = utc_now()
        with self.database.session() as session, session.begin():
            model = self._require_session(session, user_id=user, session_id=autonomy_id)
            model.status = "failed"
            model.stop_reason = "error"
            model.last_error = str(error)[:4_000]
            model.last_event = "自治 Worker 执行失败。"
            model.updated_at = now
            model.completed_at = now
            teammate = self._require_teammate(
                session,
                user_id=user,
                teammate_name=model.teammate_name,
            )
            if teammate.status != "offline":
                teammate.status = "failed"
                teammate.current_job_id = None
                teammate.last_error = str(error)[:4_000]
                teammate.updated_at = now

    def mark_retrying(self, *, user_id: str, session_id: str, error: str) -> None:
        """一次 Worker 尝试失败但仍有重试预算时，保留 current_task_id。"""

        user = self._normalize_user_id(user_id)
        autonomy_id = self._normalize_session_id(session_id)
        now = utc_now()
        with self.database.session() as session, session.begin():
            model = self._require_session(session, user_id=user, session_id=autonomy_id)
            model.status = "queued"
            model.last_error = str(error)[:4_000]
            model.last_event = "本次执行失败，等待 Background Job 有界重试。"
            model.updated_at = now
            teammate = self._require_teammate(
                session,
                user_id=user,
                teammate_name=model.teammate_name,
            )
            if teammate.status != "offline":
                teammate.status = "queued"
                teammate.current_job_id = model.job_id
                teammate.last_error = str(error)[:4_000]
                teammate.updated_at = now

    def mark_cancelled(self, *, user_id: str, session_id: str) -> None:
        """Background Job 被取消时结束对应自治业务状态。"""

        user = self._normalize_user_id(user_id)
        autonomy_id = self._normalize_session_id(session_id)
        now = utc_now()
        with self.database.session() as session, session.begin():
            model = self._require_session(session, user_id=user, session_id=autonomy_id)
            model.status = "cancelled"
            model.stop_reason = "job_cancelled"
            model.last_event = "Background Job 已取消。"
            model.updated_at = now
            model.completed_at = now
            teammate = self._require_teammate(
                session,
                user_id=user,
                teammate_name=model.teammate_name,
            )
            if teammate.status != "offline":
                teammate.status = "idle"
                teammate.current_job_id = None
                teammate.updated_at = now

    def _dispatch_job(
        self,
        *,
        user_id: str,
        teammate_name: str,
        session_id: str,
        job_id: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        from app.services.background_jobs import background_job_service

        return background_job_service.start_job(
            user_id=user_id,
            job_id=job_id,
            title=f"autonomous teammate: {teammate_name}",
            job_type="autonomous_teammate",
            prompt=f"Autonomous session {session_id}",
            max_retries=1,
            timeout_seconds=timeout_seconds,
            idempotency_key=f"autonomy-session:{session_id}",
            teammate_name=teammate_name,
            autonomy_session_id=session_id,
        )

    def _pending_shutdown_request(
        self,
        *,
        user_id: str,
        teammate_name: str,
    ) -> dict[str, str] | None:
        statement = (
            select(AgentTeamProtocolModel)
            .where(
                AgentTeamProtocolModel.user_id == user_id,
                AgentTeamProtocolModel.target == teammate_name,
                AgentTeamProtocolModel.protocol_type == "shutdown",
                AgentTeamProtocolModel.status == "pending",
            )
            .order_by(AgentTeamProtocolModel.created_at.asc())
            .limit(1)
        )
        with self.database.session() as session:
            model = session.scalar(statement)
            if model is None:
                return None
            return {"request_id": model.request_id}

    def _set_phase(
        self,
        *,
        user_id: str,
        session_id: str,
        status: str,
        event: str,
        current_task_id: str | None = None,
        reset_idle: bool = False,
        started: bool = False,
    ) -> None:
        now = utc_now()
        with self.database.session() as session, session.begin():
            model = self._require_session(session, user_id=user_id, session_id=session_id)
            model.status = status
            model.current_task_id = current_task_id
            model.last_event = event[:4_000]
            model.last_error = None
            model.updated_at = now
            if reset_idle:
                model.idle_poll_count = 0
            if started and model.started_at is None:
                model.started_at = now
            teammate = self._require_teammate(
                session,
                user_id=user_id,
                teammate_name=model.teammate_name,
            )
            if teammate.status != "offline":
                teammate.status = "working" if status == "working" else "idle"
                teammate.current_job_id = model.job_id
                teammate.updated_at = now

    def _record_idle_poll(self, *, user_id: str, session_id: str) -> int:
        with self.database.session() as session, session.begin():
            model = self._require_session(session, user_id=user_id, session_id=session_id)
            model.status = "idle"
            model.current_task_id = None
            model.idle_poll_count += 1
            model.last_event = (
                f"IDLE poll {model.idle_poll_count}/{model.max_idle_polls}：暂无工作。"
            )
            model.updated_at = utc_now()
            session.flush()
            return model.idle_poll_count

    def _record_task_completion(
        self,
        *,
        user_id: str,
        session_id: str,
        task_id: str,
    ) -> None:
        with self.database.session() as session, session.begin():
            model = self._require_session(session, user_id=user_id, session_id=session_id)
            completed_ids = list(model.completed_task_ids or [])
            if task_id not in completed_ids:
                completed_ids.append(task_id)
                model.tasks_completed += 1
            model.completed_task_ids = completed_ids
            model.current_task_id = None
            model.status = "idle"
            model.idle_poll_count = 0
            model.last_event = f"任务 {task_id} 已完成，返回 IDLE。"
            model.updated_at = utc_now()
            teammate = self._require_teammate(
                session,
                user_id=user_id,
                teammate_name=model.teammate_name,
            )
            if teammate.status != "offline":
                teammate.status = "idle"
                teammate.current_job_id = model.job_id
                teammate.updated_at = utc_now()

    def _finish(
        self,
        *,
        user_id: str,
        session_id: str,
        stop_reason: str,
        processed_messages: list[str],
        task_results: list[dict[str, Any]],
        preserve_offline: bool = False,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.database.session() as session, session.begin():
            model = self._require_session(session, user_id=user_id, session_id=session_id)
            model.status = "completed"
            model.current_task_id = None
            model.stop_reason = stop_reason
            model.last_event = f"自治会话结束：{stop_reason}。"
            model.updated_at = now
            model.completed_at = now
            teammate = self._require_teammate(
                session,
                user_id=user_id,
                teammate_name=model.teammate_name,
            )
            if not preserve_offline and teammate.status != "offline":
                teammate.status = "idle"
                teammate.current_job_id = None
                teammate.last_result = (
                    f"自治会话完成，处理 {model.tasks_completed} 条任务。"
                )
                teammate.updated_at = now
            session.flush()
            session_data = self._session_to_dict(model)
        return {
            "summary": (
                f"自治会话 {session_id} 已结束，原因={stop_reason}，"
                f"完成任务={len(task_results)}。"
            ),
            "workflow": "autonomous_teammate",
            "session": session_data,
            "processed_message_ids": processed_messages,
            "task_results": task_results,
        }

    def _require_session(self, session, *, user_id: str, session_id: str):
        model = session.get(AgentAutonomySessionModel, (user_id, session_id))
        if model is None:
            raise KeyError(f"找不到自治会话：{session_id}")
        return model

    def _require_teammate(self, session, *, user_id: str, teammate_name: str):
        model = session.scalar(
            select(AgentTeammateModel).where(
                AgentTeammateModel.user_id == user_id,
                AgentTeammateModel.name == teammate_name,
            )
        )
        if model is None:
            raise KeyError(f"找不到 teammate：{teammate_name}")
        return model

    def _session_to_dict(self, model: AgentAutonomySessionModel) -> dict[str, Any]:
        return {
            "id": model.id,
            "user_id": model.user_id,
            "teammate_name": model.teammate_name,
            "job_id": model.job_id,
            "status": model.status,
            "current_task_id": model.current_task_id,
            "completed_task_ids": list(model.completed_task_ids or []),
            "poll_interval_seconds": model.poll_interval_seconds,
            "max_idle_polls": model.max_idle_polls,
            "idle_poll_count": model.idle_poll_count,
            "max_tasks": model.max_tasks,
            "tasks_completed": model.tasks_completed,
            "max_rounds_per_task": model.max_rounds_per_task,
            "stop_requested": model.stop_requested,
            "stop_reason": model.stop_reason,
            "last_event": model.last_event,
            "last_error": model.last_error,
            "created_at": model.created_at.isoformat(),
            "updated_at": model.updated_at.isoformat(),
            "started_at": model.started_at.isoformat() if model.started_at else None,
            "completed_at": (
                model.completed_at.isoformat() if model.completed_at else None
            ),
        }

    def _normalize_user_id(self, value: str) -> str:
        normalized = str(value or "guest").strip()
        if not PARTICIPANT_PATTERN.fullmatch(normalized):
            raise ValueError("user_id 只能包含字母、数字、下划线和短横线。")
        return normalized

    def _normalize_teammate_name(self, value: str) -> str:
        normalized = str(value or "").strip()
        if not PARTICIPANT_PATTERN.fullmatch(normalized):
            raise ValueError("teammate_name 格式非法。")
        return normalized

    def _normalize_session_id(self, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not SIMPLE_ID_PATTERN.fullmatch(normalized):
            raise ValueError("session_id 格式非法。")
        return normalized


autonomous_agent_service = AutonomousAgentService()

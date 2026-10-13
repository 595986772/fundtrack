"""聊天请求的会话并发锁与持久化幂等协调。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from threading import Lock
from typing import Any, AsyncIterator, Awaitable, Callable
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.database import Database, runtime_database
from app.models import AgentChatExecutionModel
from app.schemas import AgentChatRequest, ComprehensiveAgentChatResponse
from app.services import agent_authorization as task_auth
from app.services.agent_execution_context import (
    AgentExecutionContext, agent_execution_scope, current_agent_execution_context,
)
from app.services.ai_governance_context import ai_execution_scope, current_ai_scope, stable_run_id
from app.services.comprehensive_agent import (
    ComprehensiveAgentService,
    chat_authorization_event,
    chat_task_authorization_metadata,
    comprehensive_agent_service,
    requires_chat_task_authorization,
)
from app.services.financial_research_repair import research_repair_scope
from app.services.portfolio_report_context import selected_chat_portfolio_ids
from app.services.chat_execution_budget import bounded_product_chat, current_chat_budget, chat_stage


class ChatAlreadyRunningError(RuntimeError):
    """同一幂等键正在另一个 API 进程执行。"""


def _scoped_idempotency_key(
    raw_key: str,
    execution_context: AgentExecutionContext | None,
) -> str:
    """把客户端幂等键绑定到服务端模式，避免产品/开发响应互相复用。

    数据库字段最多 64 字符，因此这里使用 SHA-256。客户端仍只需要保存原始 key，
    服务端会在每次请求中用同样的可信模式稳定地产生同一个数据库 key。
    """

    if execution_context is None:
        # 保留无显式上下文的内部旧调用语义；HTTP 入口都会传入可信上下文。
        return raw_key
    material = f"{execution_context.mode}\0{raw_key}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class ChatCoordinationResult:
    response: ComprehensiveAgentChatResponse
    replayed: bool = False
    # 仅供 HTTP 交付层检查，不能序列化给客户端后再当作权限凭据传回来。
    execution_context: AgentExecutionContext | None = None


class ChatLockRegistry:
    """同一进程内按会话和幂等键串行化聊天，避免消息顺序交叉。"""

    def __init__(self) -> None:
        self._entries: dict[tuple[int, str], tuple[asyncio.Lock, int]] = {}
        self._guard = Lock()

    @asynccontextmanager
    async def acquire(self, keys: list[str]) -> AsyncIterator[None]:
        loop_key = id(asyncio.get_running_loop())
        normalized = sorted(set(key for key in keys if key))
        entries: list[tuple[tuple[int, str], asyncio.Lock]] = []
        with self._guard:
            for key in normalized:
                registry_key = (loop_key, key)
                lock, references = self._entries.get(
                    registry_key,
                    (asyncio.Lock(), 0),
                )
                self._entries[registry_key] = (lock, references + 1)
                entries.append((registry_key, lock))

        acquired = []
        try:
            # 等锁也可能超时或被用户取消，只释放实际拿到的锁并回收所有引用。
            for _, lock in entries:
                async def acquire_one():
                    await lock.acquire()
                    acquired.append(lock)
                await chat_stage("conversation_queue", acquire_one)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()
            with self._guard:
                for registry_key, lock in entries:
                    current_lock, references = self._entries[registry_key]
                    if current_lock is lock and references <= 1:
                        self._entries.pop(registry_key, None)
                    else:
                        self._entries[registry_key] = (
                            current_lock,
                            references - 1,
                        )


class AgentChatExecutionStore:
    """数据库中的幂等状态机：running -> completed/failed。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def begin(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        conversation_id: str | None,
        request_fingerprint: str | None = None,
    ) -> ComprehensiveAgentChatResponse | None:
        """返回已完成响应；首次或上次失败则把记录置为 running。"""

        now = datetime.now(timezone.utc)
        try:
            with self.database.session() as session, session.begin():
                record = session.get(
                    AgentChatExecutionModel,
                    {
                        "user_id": user_id,
                        "idempotency_key": idempotency_key,
                    },
                )
                if request_fingerprint and record is not None:
                    old_fingerprint = (record.response_payload or {}).get("_request_fingerprint")
                    if old_fingerprint and old_fingerprint != request_fingerprint:
                        # 在读缓存或重启失败任务前校验；同键换组合不能取得旧结果或新授权。
                        raise task_auth.AgentAuthorizationError("idempotency_conflict",
                            "相同请求标识不能更换研究对象或请求内容，请重新发送。", status_code=409)
                if record is not None and record.status == "completed":
                    return ComprehensiveAgentChatResponse.model_validate(
                        record.response_payload
                    )
                if record is not None and record.status == "running":
                    raise ChatAlreadyRunningError(
                        "相同请求正在执行，请等待首次请求完成"
                    )
                if record is None:
                    session.add(
                        AgentChatExecutionModel(
                            user_id=user_id,
                            idempotency_key=idempotency_key,
                            conversation_id=conversation_id,
                            status="running",
                            response_payload=({"_request_fingerprint": request_fingerprint}
                                if request_fingerprint else {}),
                            error="",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:
                    record.status = "running"
                    record.conversation_id = conversation_id
                    record.error = ""
                    # 保留补查预算，但旧执行的超时诊断不能冒充这次重试的状态。
                    record.response_payload = {key: value for key, value in (record.response_payload or {}).items()
                        if key != "report_execution"}
                    if request_fingerprint:
                        record.response_payload = {**record.response_payload, "_request_fingerprint": request_fingerprint}
                    record.updated_at = now
            return None
        except IntegrityError as exc:
            # 多 API 进程同时插入相同 key 时，数据库联合主键是最终仲裁者。
            raise ChatAlreadyRunningError(
                "相同请求正在另一个进程执行"
            ) from exc

    def complete(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        response: ComprehensiveAgentChatResponse,
    ) -> None:
        with self.database.session() as session, session.begin():
            record = session.get(
                AgentChatExecutionModel,
                {"user_id": user_id, "idempotency_key": idempotency_key},
            )
            if record is None:
                return
            record.status = "completed"
            record.conversation_id = response.conversation.id
            fingerprint = (record.response_payload or {}).get("_request_fingerprint")
            record.response_payload = {**response.model_dump(mode="json"),
                **({"_request_fingerprint": fingerprint} if fingerprint else {})}
            record.error = ""
            record.updated_at = datetime.now(timezone.utc)

    def research_repair_state(self, *, user_id: str, idempotency_key: str) -> dict[str, Any]:
        """失败请求的中间预算留在已有 JSON 字段，服务重启后也能恢复。"""

        with self.database.session() as session:
            record = session.get(AgentChatExecutionModel, {"user_id": user_id, "idempotency_key": idempotency_key})
            return dict((record.response_payload or {}).get("research_repair_budget") or {}) if record else {}

    def checkpoint_research_repair(self, *, user_id: str, idempotency_key: str, budget: dict) -> None:
        """在公开源操作前保存预算；写库失败则不应继续抓取。"""

        with self.database.session() as session, session.begin():
            record = session.get(AgentChatExecutionModel, {"user_id": user_id, "idempotency_key": idempotency_key})
            if record is None or record.status != "running":
                raise RuntimeError("当前请求已不在执行状态，停止补查")
            record.response_payload = {**(record.response_payload or {}), "research_repair_budget": budget}

    def fail(self, *, user_id: str, idempotency_key: str, error: str, execution_diagnostics: dict | None = None) -> None:
        """失败记录可由同一个 key 再次执行，不缓存错误响应。"""

        with self.database.session() as session, session.begin():
            record = session.get(
                AgentChatExecutionModel,
                {"user_id": user_id, "idempotency_key": idempotency_key},
            )
            if record is None:
                return
            record.status = "failed"
            record.error = str(error)[:1000]
            if execution_diagnostics:
                # 使用现有幂等请求的JSON保存失败时间账本，不把未验证草稿写入会话。
                record.response_payload = {**(record.response_payload or {}), "report_execution": execution_diagnostics}
            record.updated_at = datetime.now(timezone.utc)


class AgentChatCoordinator:
    """在产品 Agent Service 外围补充并发顺序和幂等语义。"""

    def __init__(
        self,
        *,
        agent: ComprehensiveAgentService = comprehensive_agent_service,
        store: AgentChatExecutionStore | None = None,
        locks: ChatLockRegistry | None = None,
    ) -> None:
        self.agent = agent
        self.store = store or AgentChatExecutionStore()
        self.locks = locks or ChatLockRegistry()

    def _response_authorization(self, request, response, execution_context, *, replayed):
        """缓存不是权限凭据：恢复原票，验证本次对象，绝不为重放重新签发授权。"""
        metadata = response.assistant_message.run_metadata.get("task_authorization") or {}
        if not metadata:
            if (requires_chat_task_authorization(execution_context)
                    or (execution_context is not None and execution_context.task_grant_id)):
                raise task_auth.AgentAuthorizationError("grant_required", "结果缺少任务授权，不能存储或返回，请重新发起请求。")
            return execution_context, None
        if (response.conversation.user_id != request.user_id or response.run.user_id != request.user_id
                or response.workspace.user_id != request.user_id):
            raise task_auth.AgentAuthorizationError("grant_identity_mismatch", "缓存不属于当前用户。")
        expected_run = (execution_context.task_run_id if execution_context and execution_context.task_grant_id
            else current_ai_scope().run_id)
        expected_agent = (execution_context.task_agent_id if execution_context and execution_context.task_grant_id
            else "main")
        if (metadata.get("run_id") != expected_run or metadata.get("agent_id") != expected_agent
                or not metadata.get("task_grant_id")
                or (execution_context and execution_context.task_grant_id
                    and execution_context.task_grant_id != metadata["task_grant_id"])):
            raise task_auth.AgentAuthorizationError("grant_identity_mismatch", "缓存与当前任务授权身份不一致。")
        context = task_auth.get_task_authorization_service().restore(user_id=request.user_id,
            grant_id=metadata["task_grant_id"], run_id=expected_run, agent_id=expected_agent)
        if execution_context is not None:
            context = replace(execution_context, capabilities=context.capabilities & execution_context.capabilities,
                task_grant_id=context.task_grant_id, task_run_id=context.task_run_id,
                task_agent_id=context.task_agent_id)
        with agent_execution_scope(context):
            task_auth.check_active_task_authorization()
            # 新结果已在应用层确认范围；只有重放才重新解析“现在”的服务端引用。
            # 不能用本轮刚写回的新报告引用反推本轮开始前的选择。
            portfolio_ids = metadata.get("portfolio_ids", [])
            if replayed:
                previous = self.agent.conversations.latest_run_metadata(
                    user_id=request.user_id, conversation_id=request.conversation_id,
                ) if request.conversation_id else {}
                portfolio_ids = selected_chat_portfolio_ids(request, previous)
            if sorted(metadata.get("portfolio_ids", [])) != sorted(portfolio_ids):
                raise task_auth.AgentAuthorizationError("grant_scope_conflict", "缓存与本次选择的组合不一致。")
            checked = chat_task_authorization_metadata(context, portfolio_ids)
        return context, checked

    @bounded_product_chat
    async def chat(
        self,
        request: AgentChatRequest,
        *,
        execution_context: AgentExecutionContext | None = None,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ChatCoordinationResult:
        task_auth.check_active_task_authorization()
        inherited = current_agent_execution_context()
        if inherited.task_grant_id:
            if execution_context is not None and (
                execution_context.user_id != inherited.user_id
                or execution_context.task_grant_id not in (None, inherited.task_grant_id)
            ):
                raise task_auth.AgentAuthorizationError("grant_identity_mismatch", "不能替换当前任务授权。")
            execution_context = inherited
        if (
            execution_context is not None
            and execution_context.user_id != request.user_id
        ):
            raise ValueError("Agent 请求用户与可信执行身份不一致")

        request_fingerprint = None
        if requires_chat_task_authorization(execution_context) or (execution_context and execution_context.task_grant_id):
            request_fingerprint = hashlib.sha256(json.dumps(request.model_dump(mode="json"),
                sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        effective_idempotency_key = None
        if request.idempotency_key:
            effective_idempotency_key = _scoped_idempotency_key(
                request.idempotency_key,
                execution_context,
            )

        lock_keys = []
        if request.conversation_id:
            lock_keys.append(
                f"conversation:{request.user_id}:{request.conversation_id}"
            )
        if effective_idempotency_key:
            lock_keys.append(
                f"idempotency:{request.user_id}:{effective_idempotency_key}"
            )

        async with self.locks.acquire(lock_keys):
            # 缓存、失败重试与首次执行共用稳定任务身份，不能绕过授权有效期。
            run_id = stable_run_id("chat", effective_idempotency_key or uuid4().hex)
            with ai_execution_scope(request.user_id, run_id, "agent_chat"):
                budget = current_chat_budget()
                if budget:
                    budget.run_id = run_id
                if effective_idempotency_key:
                    cached = self.store.begin(
                        user_id=request.user_id,
                        idempotency_key=effective_idempotency_key,
                        conversation_id=request.conversation_id,
                        request_fingerprint=request_fingerprint,
                    )
                    if cached is not None:
                        context, authorization = await asyncio.to_thread(self._response_authorization,
                            request, cached, execution_context, replayed=True)
                        with agent_execution_scope(context) if context is not None else nullcontext():
                            task_auth.check_active_task_authorization()
                            if progress_callback is not None:
                                if authorization:
                                    await progress_callback(chat_authorization_event(authorization))
                                    task_auth.check_active_task_authorization()
                                await progress_callback({"type": "phase", "phase": "cache",
                                    "label": "已返回首次请求的完整结果"})
                            task_auth.check_active_task_authorization()
                            return ChatCoordinationResult(cached, replayed=True, execution_context=context)

                try:
                    agent_kwargs: dict[str, Any] = {}
                    if execution_context is not None:
                        agent_kwargs["execution_context"] = execution_context
                    if progress_callback is not None:
                        agent_kwargs["progress_callback"] = progress_callback
                    previous_budget = self.store.research_repair_state(
                        user_id=request.user_id, idempotency_key=effective_idempotency_key,
                    ) if effective_idempotency_key else None
                    checkpoint = (lambda budget: self.store.checkpoint_research_repair(
                        user_id=request.user_id, idempotency_key=effective_idempotency_key, budget=budget,
                    )) if effective_idempotency_key else None
                    with research_repair_scope(
                        allow_public_refresh=request.research_refresh,
                        previous=previous_budget, checkpoint=checkpoint,
                    ):
                        response = await self.agent.chat(request, **agent_kwargs)
                    context, _ = await asyncio.to_thread(self._response_authorization,
                        request, response, execution_context, replayed=False)
                    with agent_execution_scope(context) if context is not None else nullcontext():
                        task_auth.check_active_task_authorization()
                        if effective_idempotency_key:
                            self.store.complete(user_id=request.user_id,
                                idempotency_key=effective_idempotency_key, response=response)
                        task_auth.check_active_task_authorization()
                        return ChatCoordinationResult(response, execution_context=context)
                except BaseException as exc:
                    if effective_idempotency_key:
                        self.store.fail(
                            user_id=request.user_id,
                            idempotency_key=effective_idempotency_key,
                            error=str(exc),
                            execution_diagnostics=getattr(exc, "execution_diagnostics", None),
                        )
                    raise


agent_chat_coordinator = AgentChatCoordinator()

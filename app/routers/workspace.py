"""Agent Workspace 的版本化产品 API。"""

import asyncio
from contextlib import suppress
import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.schemas import (
    AgentChatRequest,
    AgentConversationCreateRequest,
    AgentConversationDeleteResponse,
    AgentConversationDetailResponse,
    AgentConversationListResponse,
    AgentConversationSummary,
    AgentWorkspaceSnapshot,
    ComprehensiveAgentChatResponse,
)
from app.services.agent_runtime_state import agent_runtime_state_service
from app.services.agent_chat_runtime import (
    ChatAlreadyRunningError,
    agent_chat_coordinator,
)
from app.services.agent_streams import agent_stream_registry
from app.services.agent_execution_context import AgentExecutionContext, current_agent_execution_context
from app.services.agent_authorization import get_task_authorization_service
from app.services.auth import CurrentPrincipal, get_current_principal, require_admin
from app.services.comprehensive_agent import comprehensive_agent_service
from app.services.conversations import ConversationChangedError, ConversationNotFoundError, conversation_service
from app.services.errors import AgentConfigError, AgentRuntimeError
from app.services.rate_limits import enforce_chat_rate_limit
from app.services.ai_governance_context import AIControlError
from app.services.portfolio_research_audit import PortfolioResearchAuditNotFoundError, PortfolioResearchAuditValidationError


router = APIRouter(prefix="/api/v1", tags=["agent-workspace"])


@router.get("/workspace", response_model=AgentWorkspaceSnapshot)
async def get_workspace(
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """读取右侧资源面板使用的统一 Agent 状态快照。"""

    return agent_runtime_state_service.workspace_snapshot(
        user_id=principal.resolve_user_id(user_id)
    )


@router.get("/conversations", response_model=AgentConversationListResponse)
async def list_conversations(
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """返回当前用户最近使用的会话。"""

    user_id = principal.resolve_user_id(user_id)
    return AgentConversationListResponse(
        user_id=user_id,
        conversations=conversation_service.list(user_id=user_id),
    )


@router.post("/conversations", response_model=AgentConversationSummary, status_code=201)
async def create_conversation(
    request: AgentConversationCreateRequest,
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """显式创建一个空会话；首次发送消息时也支持自动创建。"""

    return conversation_service.create(
        user_id=principal.resolve_user_id(request.user_id),
        title=request.title,
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=AgentConversationDetailResponse,
)
async def get_conversation(
    conversation_id: str,
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """读取一条会话和全部可见消息。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        return conversation_service.detail(
            user_id=user_id,
            conversation_id=conversation_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete(
    "/conversations/{conversation_id}",
    response_model=AgentConversationDeleteResponse,
)
async def delete_conversation(
    conversation_id: str,
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """删除聊天记录，但不删除该用户的长期记忆和任务资源。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        conversation_service.delete(
            user_id=user_id,
            conversation_id=conversation_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return AgentConversationDeleteResponse(ok=True, conversation_id=conversation_id)


@router.post("/agent/chat", response_model=ComprehensiveAgentChatResponse)
@router.post(
    "/developer/agent/chat",
    response_model=ComprehensiveAgentChatResponse,
    dependencies=[Depends(require_admin)],
)
async def comprehensive_agent_chat(
    request: AgentChatRequest,
    http_request: Request,
    response: Response,
    principal: CurrentPrincipal = Depends(enforce_chat_rate_limit),
):
    """运行综合 Agent；具体工具模式由后端路由而不是请求体决定。"""

    try:
        trusted_user_id = principal.resolve_user_id(request.user_id)
        trusted_request = request.model_copy(
            update={"user_id": trusted_user_id}
        )
        execution_context = _execution_context_for_route(
            http_request=http_request,
            principal=principal,
            user_id=trusted_user_id,
        )
        coordinated = await agent_chat_coordinator.chat(
            trusted_request,
            execution_context=execution_context,
        )
        response.headers["Idempotency-Replayed"] = str(
            coordinated.replayed
        ).lower()
        return coordinated.response
    except (ConversationNotFoundError, PortfolioResearchAuditNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PortfolioResearchAuditValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (ChatAlreadyRunningError, ConversationChangedError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/agent/chat/stream")
@router.post(
    "/developer/agent/chat/stream",
    dependencies=[Depends(require_admin)],
)
async def stream_comprehensive_agent_chat(
    chat_request: AgentChatRequest,
    http_request: Request,
    principal: CurrentPrincipal = Depends(enforce_chat_rate_limit),
):
    """通过 SSE 推送 Agent 阶段、工具状态、回答增量和最终结构化结果。"""

    stream_id = f"stream_{uuid4().hex[:16]}"
    trusted_user_id = principal.resolve_user_id(chat_request.user_id)
    trusted_request = chat_request.model_copy(
        update={"user_id": trusted_user_id}
    )
    execution_context = _execution_context_for_route(
        http_request=http_request,
        principal=principal,
        user_id=trusted_user_id,
    )

    async def event_generator():
        # 生产事件和发送事件属于不同 asyncio Task。显式携带后端上下文，避免队列
        # 中已经完成的回答在用户撤销之后仍被当作普通文本发送出去。
        queue: asyncio.Queue[tuple[dict, AgentExecutionContext | None]] = asyncio.Queue()

        async def enqueue(event, context=None):
            await queue.put((event, context))

        async def report_progress(event: dict) -> None:
            context = current_agent_execution_context()
            await enqueue(event, context if context.task_grant_id else None)

        async def run_agent() -> None:
            try:
                coordinated = await agent_chat_coordinator.chat(
                    trusted_request,
                    execution_context=execution_context,
                    progress_callback=report_progress,
                )
                await enqueue(
                    {
                        "type": "result",
                        "data": coordinated.response.model_dump(mode="json"),
                        "replayed": coordinated.replayed,
                    }, coordinated.execution_context,
                )
            except asyncio.CancelledError:
                await enqueue(
                    {
                        "type": "cancelled",
                        "message": "本次 Agent 运行已停止。",
                    }
                )
            except AIControlError as exc:
                await enqueue({"type": "error", "status": exc.status_code,
                    "code": exc.code, "retry_after": exc.retry_after, "message": str(exc)})
            except ConversationNotFoundError as exc:
                await enqueue(
                    {"type": "error", "status": 404, "message": str(exc)}
                )
            except AgentConfigError as exc:
                await enqueue(
                    {"type": "error", "status": 500, "message": str(exc)}
                )
            except PortfolioResearchAuditNotFoundError as exc:
                await enqueue({"type":"error", "status":404, "message":str(exc)})
            except PortfolioResearchAuditValidationError as exc:
                await enqueue({"type":"error", "status":409, "message":str(exc)})
            except AgentRuntimeError as exc:
                await enqueue(
                    {"type": "error", "status": 502, "message": str(exc)}
                )
            except (ChatAlreadyRunningError, ConversationChangedError) as exc:
                await enqueue(
                    {"type": "error", "status": 409, "message": str(exc)}
                )
            except Exception as exc:
                await enqueue(
                    {
                        "type": "error",
                        "status": 500,
                        "message": f"Agent 流式运行失败：{exc}",
                    }
                )
            finally:
                await enqueue({"type": "done"})

        runner_task = asyncio.create_task(run_agent())
        agent_stream_registry.register(
            stream_id=stream_id,
            task=runner_task,
            user_id=trusted_request.user_id,
        )
        yield _sse_frame(
            "stream_started",
            {"stream_id": stream_id, "message": "Agent 运行已创建。"},
        )

        try:
            while True:
                if await http_request.is_disconnected():
                    break
                try:
                    event, delivery_context = await asyncio.wait_for(queue.get(), timeout=10.0)
                except TimeoutError:
                    # 注释帧不会触发前端业务事件，只用于让代理层保持连接活跃。
                    yield ": keep-alive\n\n"
                    continue

                event_type = str(event.get("type") or "message")
                if delivery_context and delivery_context.task_grant_id:
                    await asyncio.to_thread(get_task_authorization_service().check_context, delivery_context)
                if event_type == "result":
                    data = event["data"]
                    answer = str(data["assistant_message"]["content"])
                    yield _sse_frame(
                        "phase",
                        {
                            "type": "phase",
                            "phase": "stream",
                            "label": "正在输出最终回答",
                        },
                    )
                    # 核心 Agent 已完成工具闭环；这里按小段传输最终文本，不引入人为延迟。
                    for index in range(0, len(answer), 24):
                        if delivery_context and delivery_context.task_grant_id:
                            await asyncio.to_thread(get_task_authorization_service().check_context, delivery_context)
                        yield _sse_frame(
                            "answer_delta",
                            {"delta": answer[index : index + 24]},
                        )
                        await asyncio.sleep(0)
                    if delivery_context and delivery_context.task_grant_id:
                        await asyncio.to_thread(get_task_authorization_service().check_context, delivery_context)
                    yield _sse_frame("result", data)
                    continue

                if event_type == "done":
                    yield _sse_frame("done", {"stream_id": stream_id})
                    break
                yield _sse_frame(event_type, event)
        except AIControlError as exc:
            yield _sse_frame("error", {"type": "error", "status": exc.status_code,
                "code": exc.code, "retry_after": exc.retry_after, "message": str(exc)})
            yield _sse_frame("done", {"stream_id": stream_id})
        finally:
            if not runner_task.done():
                runner_task.cancel()
                with suppress(asyncio.CancelledError):
                    await runner_task
            agent_stream_registry.unregister(stream_id=stream_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/agent/chat/stream/{stream_id}")
async def cancel_comprehensive_agent_chat(
    stream_id: str,
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """停止一条仍在当前 API 进程执行的流式聊天。"""

    accepted = agent_stream_registry.cancel(
        stream_id=stream_id,
        # 本地迁移模式保留旧停止按钮；严格认证时必须是流的创建者。
        user_id=principal.user_id if principal.authenticated else None,
    )
    return {
        "ok": accepted,
        "stream_id": stream_id,
        "message": "停止信号已发送。" if accepted else "该运行已结束或不存在。",
    }


def _sse_frame(event: str, payload: dict) -> str:
    """把结构化事件编码成浏览器可解析的标准 SSE 帧。"""

    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


def _execution_context_for_route(
    *,
    http_request: Request,
    principal: CurrentPrincipal,
    user_id: str,
) -> AgentExecutionContext:
    """根据服务端命中的 API 路由构造模式，客户端不能通过 JSON 字段自行切换。"""

    if http_request.url.path.startswith("/api/v1/developer/"):
        return AgentExecutionContext.developer(
            user_id=user_id,
            role=principal.role,
            authenticated=principal.authenticated,
            source="developer-api",
        )
    return AgentExecutionContext.product(
        user_id=user_id,
        role=principal.role,
        authenticated=principal.authenticated,
        source="product-api",
    )

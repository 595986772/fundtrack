"""工具清单和无状态 Agent 内核路由。

产品前端使用 `/api/v1/agent/chat`。这里保留两个稳定的底层入口，供 Worker、
自动化测试和外部集成直接复用。
"""

from fastapi import APIRouter, Depends, HTTPException

from app.schemas import AgentChatRequest, AgentChatResponse, AgentToolsResponse
from app.services.agent_execution_context import AgentExecutionContext
from app.services.auth import CurrentPrincipal, require_admin
from app.services.errors import AgentConfigError, AgentRuntimeError
from app.services.tool_agent import tool_calling_agent_service

router = APIRouter(tags=["tools"])


@router.get("/tools", response_model=AgentToolsResponse)
async def list_tools(
    principal: CurrentPrincipal = Depends(require_admin),
):
    """查看开发者 Agent 可以调用的完整工具池。"""

    user_id = principal.resolve_user_id("guest")
    context = AgentExecutionContext.developer(
        user_id=user_id,
        role=principal.role,
        authenticated=principal.authenticated,
        source="stateless-tools-api",
    )
    return AgentToolsResponse(
        tools=tool_calling_agent_service.list_tools(execution_context=context)
    )


@router.post("/agent/chat", response_model=AgentChatResponse)
async def agent_chat(
    request: AgentChatRequest,
    principal: CurrentPrincipal = Depends(require_admin),
):
    """执行一次不保存会话历史的 Tool Calling Agent 对话。"""

    try:
        user_id = principal.resolve_user_id(request.user_id)
        trusted_request = request.model_copy(update={"user_id": user_id})
        context = AgentExecutionContext.developer(
            user_id=user_id,
            role=principal.role,
            authenticated=principal.authenticated,
            source="stateless-developer-api",
        )
        return await tool_calling_agent_service.chat(
            trusted_request,
            execution_context=context,
        )
    except AgentConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

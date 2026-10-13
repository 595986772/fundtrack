"""s19 MCP Tools 的直接测试接口。

这些接口故意不经过大模型，方便先分别验证：
- Server 能否连接；
- tools/list 是否发现工具；
- tools/call 是否返回结果。

确认协议层正常后，再用 `/agent/chat` 测试“模型选择 MCP 工具”的完整链路。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from app.schemas import (
    McpCallRequest,
    McpCallResponse,
    McpServerActionResponse,
    McpStateResponse,
)
from app.services.mcp_tools import mcp_tool_service


router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("", response_model=McpStateResponse)
async def get_mcp_state():
    """查看白名单 Server、当前连接、动态工具和最近调用。"""

    return McpStateResponse(
        servers=mcp_tool_service.list_servers(),
        tools=mcp_tool_service.list_tools(),
        recent_calls=mcp_tool_service.recent_calls(),
    )


@router.post(
    "/servers/{server_name}/connect",
    response_model=McpServerActionResponse,
)
async def connect_mcp_server(server_name: str):
    """启动 stdio Server，并执行 initialize + tools/list。"""

    try:
        server = await mcp_tool_service.connect(server_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return McpServerActionResponse(
        action="connect",
        server=server,
        tools=mcp_tool_service.list_tools(),
    )


@router.post(
    "/servers/{server_name}/refresh",
    response_model=McpServerActionResponse,
)
async def refresh_mcp_server(server_name: str):
    """对已连接 Server 重新执行 tools/list。"""

    try:
        server = await mcp_tool_service.refresh(server_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return McpServerActionResponse(
        action="refresh",
        server=server,
        tools=mcp_tool_service.list_tools(),
    )


@router.post(
    "/servers/{server_name}/disconnect",
    response_model=McpServerActionResponse,
)
async def disconnect_mcp_server(server_name: str):
    """关闭 MCP Session 和子进程，并从 Agent 工具池移除其工具。"""

    try:
        server = await mcp_tool_service.disconnect(server_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return McpServerActionResponse(
        action="disconnect",
        server=server,
        tools=mcp_tool_service.list_tools(),
    )


@router.post("/call", response_model=McpCallResponse)
async def call_mcp_tool(request: McpCallRequest):
    """直接执行 MCP tools/call，不消耗大模型 token。"""

    try:
        raw_result = await mcp_tool_service.call_tool(
            request.tool_name,
            request.arguments,
        )
        parsed_result = json.loads(raw_result)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return McpCallResponse(
        result=parsed_result,
        recent_calls=mcp_tool_service.recent_calls(),
    )

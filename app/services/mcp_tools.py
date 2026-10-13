"""s19 MCP Client、工具发现与 LangChain 适配层。

这个文件处在两套接口之间：

1. 向下使用 MCP SDK 与独立 Server 通信；
2. 向上把远程工具转换成 LangChain ``StructuredTool``，交给主 Agent。

数据流：
``connect`` -> MCP initialize -> ``tools/list`` -> 加命名空间
-> LangChain ``bind_tools`` -> 模型产生 tool_call -> ``tools/call`` -> 结果回填模型
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import re
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def normalize_mcp_name(value: str) -> str:
    """把 Server/Tool 名称转换成模型工具名允许的安全字符。

    MCP 约定最终名称形如 ``mcp__server__tool``。不同 Server 即使都声明
    ``search``，加前缀后也不会互相覆盖。
    """

    normalized = _UNSAFE_NAME_CHARS.sub("_", str(value or "").strip())
    return normalized or "unnamed"


@dataclass(frozen=True)
class McpServerConfig:
    """一个受信任 MCP Server 的启动配置。

    command/args 来自后端白名单，不接受模型直接传入，所以模型无法借 MCP
    启动任意 shell 命令。
    """

    name: str
    description: str
    command: str
    args: tuple[str, ...]
    cwd: Path
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class McpToolDescriptor:
    """从 MCP ``tools/list`` 得到的一条工具定义。"""

    server_name: str
    original_name: str
    namespaced_name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]

    @property
    def is_read_only(self) -> bool:
        """权限层只自动放行 Server 明确声明的非破坏性只读工具。"""

        return (
            self.annotations.get("readOnlyHint") is True
            and self.annotations.get("destructiveHint") is not True
        )

    def public_dict(self) -> dict[str, Any]:
        """返回适合 REST、Prompt 和调试响应展示的安全字段。"""

        return {
            "server_name": self.server_name,
            "original_name": self.original_name,
            "name": self.namespaced_name,
            "description": self.description,
            "input_schema": self.input_schema,
            "annotations": self.annotations,
            "read_only": self.is_read_only,
        }


@dataclass
class McpCommand:
    """HTTP/Agent 任务发给 MCP supervisor 的一条内部命令。"""

    action: str
    payload: dict[str, Any]
    future: asyncio.Future[Any]


@dataclass
class McpConnection:
    """一个 MCP supervisor task 及其公开运行状态。"""

    config: McpServerConfig
    status: str = "disconnected"
    protocol_version: str | None = None
    server_info: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, McpToolDescriptor] = field(default_factory=dict)
    error: str | None = None
    connected_at: str | None = None
    command_queue: asyncio.Queue[McpCommand] | None = None
    worker_task: asyncio.Task[None] | None = None


class McpToolService:
    """管理受信任 Server，并把动态发现的工具合并到 Agent 工具池。"""

    def __init__(
        self,
        configs: list[McpServerConfig] | None = None,
    ) -> None:
        default_configs = [
            McpServerConfig(
                name="agent-learning",
                description="本地只读 Agent Harness 课程资料服务",
                command=sys.executable,
                args=("-m", "app.mcp_servers.learning_server"),
                cwd=PROJECT_ROOT,
            )
        ]
        selected_configs = configs if configs is not None else default_configs
        self._configs = {config.name: config for config in selected_configs}
        self._connections = {
            name: McpConnection(config=config)
            for name, config in self._configs.items()
        }
        self._lifecycle_lock = asyncio.Lock()
        self._recent_calls: list[dict[str, Any]] = []

    def configured_server_names(self) -> list[str]:
        """列出后端白名单里的 Server 名称。"""

        return sorted(self._configs)

    async def connect(self, server_name: str) -> dict[str, Any]:
        """启动本地 Server，完成握手并执行第一次工具发现。"""

        normalized_name = str(server_name or "").strip()
        connection = self._connections.get(normalized_name)
        if connection is None:
            available = ", ".join(self.configured_server_names()) or "(none)"
            raise ValueError(f"未知 MCP Server：{normalized_name}；可用：{available}")

        async with self._lifecycle_lock:
            if (
                connection.status == "connected"
                and connection.worker_task is not None
                and not connection.worker_task.done()
            ):
                return self._connection_dict(connection)

            # MCP SDK 的 stdio context 必须由“进入它的同一个 asyncio task”关闭。
            # 因此这里启动专属 supervisor；不同 HTTP 请求只向它的 Queue 发命令，
            # 不直接持有或关闭 ClientSession。
            loop = asyncio.get_running_loop()
            ready = loop.create_future()
            connection.command_queue = asyncio.Queue()
            connection.status = "connecting"
            connection.error = None
            worker = asyncio.create_task(
                self._connection_worker(connection, ready),
                name=f"mcp-supervisor-{normalize_mcp_name(connection.config.name)}",
            )
            connection.worker_task = worker
            try:
                await ready
            except Exception as exc:
                # Worker 自己负责退出 stdio context；这里等待它完成清理。
                await asyncio.gather(worker, return_exceptions=True)
                connection.worker_task = None
                connection.command_queue = None
                raise RuntimeError(
                    f"连接 MCP Server {normalized_name} 失败：{exc}"
                ) from exc

        return self._connection_dict(connection)

    async def disconnect(self, server_name: str) -> dict[str, Any]:
        """关闭 Session 和 stdio 子进程，并从动态工具池移除其工具。"""

        connection = self._connections.get(str(server_name or "").strip())
        if connection is None:
            raise ValueError(f"未知 MCP Server：{server_name}")

        async with self._lifecycle_lock:
            worker = connection.worker_task
            queue = connection.command_queue
            if worker is not None and not worker.done() and queue is not None:
                connection.status = "disconnecting"
                future = asyncio.get_running_loop().create_future()
                await queue.put(McpCommand("stop", {}, future))
                await future
                # Session 和 stdio context 会在 supervisor 自己的 finally 中退出。
                await worker

            connection.worker_task = None
            connection.command_queue = None
            connection.tools = {}
            connection.protocol_version = None
            connection.server_info = {}
            connection.connected_at = None
            connection.error = None
            connection.status = "disconnected"

        return self._connection_dict(connection)

    async def shutdown(self) -> None:
        """FastAPI 关闭时清理全部 MCP 子进程。"""

        for server_name in list(self._connections):
            connection = self._connections[server_name]
            if connection.worker_task is not None:
                await self.disconnect(server_name)

    async def refresh(self, server_name: str) -> dict[str, Any]:
        """重新执行 ``tools/list``，观察 Server 的工具清单是否变化。"""

        connection = self._connected_connection(server_name)
        await self._submit_command(connection, "refresh", {})
        return self._connection_dict(connection)

    async def _connection_worker(
        self,
        connection: McpConnection,
        ready: asyncio.Future[None],
    ) -> None:
        """在唯一 owner task 中管理 stdio、Session 和所有协议请求。

        这是长连接的关键：FastAPI 的 connect/call/disconnect 可能来自不同请求 task，
        但 MCP SDK 的 context manager 只在本 supervisor task 中进入和退出。
        """

        parameters = StdioServerParameters(
            command=connection.config.command,
            args=list(connection.config.args),
            cwd=connection.config.cwd,
        )
        normal_stop = False

        try:
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(
                        seconds=connection.config.timeout_seconds
                    ),
                ) as session:
                    initialized = await session.initialize()
                    connection.protocol_version = str(initialized.protocolVersion)
                    connection.server_info = initialized.serverInfo.model_dump(
                        mode="json",
                        by_alias=True,
                    )
                    connection.tools = await self._discover_session(
                        session,
                        connection.config,
                    )
                    connection.status = "connected"
                    connection.connected_at = datetime.now(timezone.utc).isoformat()
                    ready.set_result(None)

                    while True:
                        if connection.command_queue is None:
                            raise RuntimeError("MCP supervisor command queue missing")
                        command = await connection.command_queue.get()

                        if command.action == "stop":
                            normal_stop = True
                            command.future.set_result(None)
                            break

                        try:
                            if command.action == "refresh":
                                connection.tools = await self._discover_session(
                                    session,
                                    connection.config,
                                )
                                command.future.set_result(connection.tools)
                            elif command.action == "call":
                                result = await session.call_tool(
                                    command.payload["tool_name"],
                                    arguments=command.payload.get("arguments") or {},
                                    read_timeout_seconds=timedelta(
                                        seconds=connection.config.timeout_seconds
                                    ),
                                )
                                command.future.set_result(result)
                            else:
                                raise RuntimeError(
                                    f"未知 MCP supervisor 命令：{command.action}"
                                )
                        except Exception as exc:
                            command.future.set_exception(exc)
        except BaseException as exc:
            connection.error = str(exc)
            connection.status = "error"
            connection.tools = {}
            if not ready.done():
                ready.set_exception(exc)
            self._fail_queued_commands(connection, exc)
        finally:
            # 正常 stop 的公开状态由 disconnect() 在 worker 完全退出后统一清空。
            if not normal_stop and connection.status != "error":
                connection.status = "error"
                connection.error = "MCP supervisor 意外停止"
                connection.tools = {}

    def _fail_queued_commands(
        self,
        connection: McpConnection,
        error: BaseException,
    ) -> None:
        """Server 意外退出时，唤醒 Queue 中等待的 HTTP/Agent 请求。"""

        queue = connection.command_queue
        if queue is None:
            return
        while not queue.empty():
            command = queue.get_nowait()
            if not command.future.done():
                command.future.set_exception(
                    RuntimeError(f"MCP supervisor 已停止：{error}")
                )

    async def _submit_command(
        self,
        connection: McpConnection,
        action: str,
        payload: dict[str, Any],
    ) -> Any:
        """把协议操作排入 supervisor Queue，并等待对应 request 的结果。"""

        queue = connection.command_queue
        worker = connection.worker_task
        if queue is None or worker is None or worker.done():
            raise RuntimeError(f"MCP Server {connection.config.name} 连接已断开")

        future = asyncio.get_running_loop().create_future()
        await queue.put(McpCommand(action=action, payload=payload, future=future))
        return await future

    async def _discover_session(
        self,
        session: ClientSession,
        config: McpServerConfig,
    ) -> dict[str, McpToolDescriptor]:
        """读取可能分页的 MCP tool definitions，并建立命名空间索引。"""

        discovered: dict[str, McpToolDescriptor] = {}
        cursor: str | None = None

        while True:
            response = await session.list_tools(cursor=cursor)
            for remote_tool in response.tools:
                namespaced_name = (
                    f"mcp__{normalize_mcp_name(config.name)}"
                    f"__{normalize_mcp_name(remote_tool.name)}"
                )
                if namespaced_name in discovered:
                    raise RuntimeError(f"MCP 工具名称规范化后冲突：{namespaced_name}")

                annotations = (
                    remote_tool.annotations.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=True,
                    )
                    if remote_tool.annotations is not None
                    else {}
                )
                discovered[namespaced_name] = McpToolDescriptor(
                    server_name=config.name,
                    original_name=remote_tool.name,
                    namespaced_name=namespaced_name,
                    description=remote_tool.description or "",
                    input_schema=dict(remote_tool.inputSchema or {}),
                    annotations=annotations,
                )

            cursor = response.nextCursor
            if not cursor:
                break

        return discovered

    def list_servers(self) -> list[dict[str, Any]]:
        """返回所有配置和当前连接状态，不泄露 command/env。"""

        return [
            self._connection_dict(self._connections[name])
            for name in sorted(self._connections)
        ]

    def list_tools(self) -> list[dict[str, Any]]:
        """返回所有已连接 Server 动态发现出的工具。"""

        return [
            descriptor.public_dict()
            for descriptor in self._descriptors()
        ]

    def recent_calls(self, max_results: int = 20) -> list[dict[str, Any]]:
        """返回最近调用轨迹，帮助从前端观察 tools/call。"""

        bounded = max(1, min(int(max_results), 100))
        return list(reversed(self._recent_calls[-bounded:]))

    def index_for_prompt(self) -> str:
        """生成轻量 MCP 状态，供 System Prompt 告诉模型当前有哪些连接。"""

        connected = [
            server
            for server in self.list_servers()
            if server["connected"]
        ]
        if not connected:
            return (
                "当前没有已连接的 MCP Server。"
                f"后端允许连接：{', '.join(self.configured_server_names()) or '(none)'}。"
            )

        lines = ["当前已连接的 MCP Server："]
        for server in connected:
            tool_names = ", ".join(server["tool_names"]) or "(no tools)"
            lines.append(f"- {server['name']}: {tool_names}")
        return "\n".join(lines)

    def permission_for_tool(self, tool_name: str) -> tuple[bool, str]:
        """把 MCP annotations 转成现有 Permission 层可用的决定。"""

        descriptor = self._descriptor_map().get(tool_name)
        if descriptor is None:
            return False, f"未知或未连接的 MCP 工具：{tool_name}"
        if descriptor.is_read_only:
            return True, "MCP Server 声明该工具为 readOnly 且非 destructive"
        return False, "当前教学版不会自动执行未声明为安全只读的 MCP 工具"

    def langchain_tools(self) -> list[Any]:
        """把 MCP JSON Schema 动态包装成 LangChain StructuredTool。"""

        try:
            from langchain_core.tools import StructuredTool
        except ModuleNotFoundError as exc:
            raise RuntimeError("请先安装 LangChain 依赖") from exc

        return [
            self._to_langchain_tool(descriptor, StructuredTool)
            for descriptor in self._descriptors()
        ]

    def assemble_tool_pool(self, built_in_tools: list[Any]) -> list[Any]:
        """组装固定内置工具和当前已发现的 MCP 工具。

        内置工具放在前面；MCP 名称都带 ``mcp__`` 前缀，因此不会静默覆盖
        本项目手写工具。
        """

        dynamic_tools = self.langchain_tools()
        all_tools = [*built_in_tools, *dynamic_tools]
        names = [tool.name for tool in all_tools]
        if len(names) != len(set(names)):
            raise RuntimeError("组装后的 Agent 工具池存在重名工具")
        return all_tools

    def _to_langchain_tool(
        self,
        descriptor: McpToolDescriptor,
        structured_tool_type: Any,
    ) -> Any:
        """为一条动态 descriptor 创建真正调用 MCP Client 的协程工具。"""

        async def call_remote_tool(**arguments: Any) -> str:
            return await self.call_tool(descriptor.namespaced_name, arguments)

        safety = "readOnly" if descriptor.is_read_only else "requires approval"
        return structured_tool_type.from_function(
            coroutine=call_remote_tool,
            name=descriptor.namespaced_name,
            description=(
                f"[MCP server={descriptor.server_name}; {safety}] "
                f"{descriptor.description}"
            ),
            args_schema=descriptor.input_schema,
            infer_schema=False,
        )

    async def call_tool(
        self,
        namespaced_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """把模型的命名空间工具名还原为 Server 原名并发送 ``tools/call``。"""

        descriptor = self._descriptor_map().get(namespaced_name)
        if descriptor is None:
            raise ValueError(f"未知或未连接的 MCP 工具：{namespaced_name}")
        connection = self._connected_connection(descriptor.server_name)

        started_at = datetime.now(timezone.utc)
        safe_arguments = dict(arguments or {})

        try:
            # 不直接触碰 Session，而是把 tools/call 交给它的唯一 owner task。
            response = await self._submit_command(
                connection,
                "call",
                {
                    "tool_name": descriptor.original_name,
                    "arguments": safe_arguments,
                },
            )

            payload = self._call_result_payload(
                descriptor=descriptor,
                response=response,
            )
            if response.isError:
                raise RuntimeError(
                    f"MCP Server 返回工具错误："
                    f"{json.dumps(payload, ensure_ascii=False)}"
                )
        except Exception as exc:
            self._record_call(
                descriptor=descriptor,
                arguments=safe_arguments,
                started_at=started_at,
                ok=False,
                error=str(exc),
            )
            raise

        self._record_call(
            descriptor=descriptor,
            arguments=safe_arguments,
            started_at=started_at,
            ok=True,
            error=None,
        )
        return json.dumps(payload, ensure_ascii=False)

    def _call_result_payload(
        self,
        *,
        descriptor: McpToolDescriptor,
        response: Any,
    ) -> dict[str, Any]:
        """把 MCP Content blocks 转成容易交给模型和前端查看的 JSON。"""

        content: list[dict[str, Any]] = []
        for block in response.content:
            if hasattr(block, "model_dump"):
                content.append(
                    block.model_dump(mode="json", by_alias=True, exclude_none=True)
                )
            else:
                content.append({"type": "unknown", "value": str(block)})

        return {
            "ok": not bool(response.isError),
            "server_name": descriptor.server_name,
            "tool_name": descriptor.original_name,
            "namespaced_name": descriptor.namespaced_name,
            "content": content,
            "structured_content": response.structuredContent,
        }

    def _record_call(
        self,
        *,
        descriptor: McpToolDescriptor,
        arguments: dict[str, Any],
        started_at: datetime,
        ok: bool,
        error: str | None,
    ) -> None:
        """保留有上限的内存轨迹；不在日志里记录环境变量或 API Key。"""

        finished_at = datetime.now(timezone.utc)
        self._recent_calls.append(
            {
                "server_name": descriptor.server_name,
                "tool_name": descriptor.namespaced_name,
                "arguments": arguments,
                "ok": ok,
                "error": error,
                "started_at": started_at.isoformat(),
                "duration_ms": round(
                    (finished_at - started_at).total_seconds() * 1000,
                    2,
                ),
            }
        )
        if len(self._recent_calls) > 100:
            del self._recent_calls[:-100]

    def _descriptors(self) -> list[McpToolDescriptor]:
        """以稳定顺序返回全部动态工具。"""

        return sorted(
            (
                descriptor
                for connection in self._connections.values()
                if connection.status == "connected"
                for descriptor in connection.tools.values()
            ),
            key=lambda item: item.namespaced_name,
        )

    def _descriptor_map(self) -> dict[str, McpToolDescriptor]:
        """按给模型看的完整名称建立调用索引。"""

        return {
            descriptor.namespaced_name: descriptor
            for descriptor in self._descriptors()
        }

    def _connected_connection(self, server_name: str) -> McpConnection:
        """获取已连接 Server；未连接时给出适合接口展示的错误。"""

        connection = self._connections.get(str(server_name or "").strip())
        if connection is None:
            raise ValueError(f"未知 MCP Server：{server_name}")
        if (
            connection.status != "connected"
            or connection.worker_task is None
            or connection.worker_task.done()
        ):
            raise RuntimeError(
                f"MCP Server {connection.config.name} 尚未连接，"
                "请先调用 connect_mcp_tool 或 /mcp/servers/.../connect"
            )
        return connection

    def _connection_dict(self, connection: McpConnection) -> dict[str, Any]:
        """把内部 Session/进程对象裁剪成公开状态。"""

        return {
            "name": connection.config.name,
            "description": connection.config.description,
            "transport": "stdio",
            "status": connection.status,
            "connected": connection.status == "connected",
            "protocol_version": connection.protocol_version,
            "server_info": connection.server_info,
            "tool_count": len(connection.tools),
            "tool_names": sorted(connection.tools),
            "connected_at": connection.connected_at,
            "error": connection.error,
        }


mcp_tool_service = McpToolService()

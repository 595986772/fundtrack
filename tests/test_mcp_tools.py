"""s19 MCP Tools 的本地确定性测试。

运行：
    conda run -n dl python -m unittest tests.test_mcp_tools -v

测试会启动真实的本地 stdio MCP 子进程，但不会调用 DeepSeek、Redis、Docker，
也不会访问网络或修改项目文件。
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from app.schemas import AgentChatRequest
from app.services.mcp_tools import McpToolService, normalize_mcp_name
from app.services.tool_permissions import check_tool_permission
from tests.agent_test_context import developer_context


class McpToolServiceTests(unittest.IsolatedAsyncioTestCase):
    """验证 Client -> Server -> 动态 LangChain Tool 的核心数据流。"""

    async def asyncSetUp(self) -> None:
        self.service = McpToolService()

    async def asyncTearDown(self) -> None:
        await self.service.shutdown()

    def test_normalize_mcp_name_replaces_only_unsafe_characters(self) -> None:
        """空格、斜线等字符必须规范化，字母、数字、横线和下划线保留。"""

        self.assertEqual(normalize_mcp_name("docs server/v2"), "docs_server_v2")
        self.assertEqual(normalize_mcp_name("agent-learning"), "agent-learning")

    async def test_connect_discovers_namespaced_tools_and_annotations(self) -> None:
        """initialize + tools/list 应建立 mcp__server__tool 索引。"""

        server = await self.service.connect("agent-learning")
        tools = self.service.list_tools()

        self.assertTrue(server["connected"])
        self.assertEqual(server["transport"], "stdio")
        self.assertEqual(server["tool_count"], 2)
        self.assertTrue(server["protocol_version"])
        self.assertEqual(
            {tool["name"] for tool in tools},
            {
                "mcp__agent-learning__get_chapter_outline",
                "mcp__agent-learning__search_course_notes",
            },
        )
        self.assertTrue(all(tool["read_only"] for tool in tools))

    async def test_call_tool_returns_structured_server_result(self) -> None:
        """tools/call 应把命名空间名称还原成 Server 原始工具名。"""

        await self.service.connect("agent-learning")
        raw_result = await self.service.call_tool(
            "mcp__agent-learning__get_chapter_outline",
            {"chapter_id": "s19"},
        )
        result = json.loads(raw_result)

        self.assertTrue(result["ok"])
        self.assertEqual(result["tool_name"], "get_chapter_outline")
        self.assertEqual(result["structured_content"]["chapter_id"], "s19")
        self.assertIn("tools/list", result["structured_content"]["concepts"])
        self.assertTrue(self.service.recent_calls()[0]["ok"])

    async def test_langchain_adapter_uses_discovered_json_schema(self) -> None:
        """动态工具无需在 agent_tools.py 手写 Pydantic schema 也能 ainvoke。"""

        await self.service.connect("agent-learning")
        tool = next(
            item
            for item in self.service.langchain_tools()
            if item.name.endswith("search_course_notes")
        )
        raw_result = await tool.ainvoke({"query": "MCP", "max_results": 2})
        result = json.loads(raw_result)

        self.assertIn("query", tool.args)
        self.assertEqual(result["structured_content"]["count"], 2)

    async def test_mcp_tool_dispatch_reuses_permission_and_hooks(self) -> None:
        """MCP 动态工具也必须走主 Agent 的 PreToolUse/PostToolUse。"""

        from langchain_core.messages import ToolMessage

        from app.services import tool_agent as tool_agent_module
        from app.services import tool_permissions as permission_module

        await self.service.connect("agent-learning")
        tool_map = {
            tool.name: tool
            for tool in self.service.langchain_tools()
        }
        tool_name = "mcp__agent-learning__get_chapter_outline"

        # Permission 模块改用本测试的 Client，避免依赖全局 FastAPI MCP 状态。
        with patch.object(
            permission_module,
            "mcp_tool_service",
            self.service,
        ):
            executed, _, traces = (
                await tool_agent_module.tool_calling_agent_service._execute_tool_call(
                    tool_call={
                        "id": "mcp-call-1",
                        "name": tool_name,
                        "args": {"chapter_id": "s19"},
                    },
                    tool_map=tool_map,
                    request=AgentChatRequest(
                        user_id="chen",
                        message="通过 MCP 查询 s19",
                    ),
                    execution_context=developer_context(),
                    tool_index=1,
                    ToolMessage=ToolMessage,
                )
            )

        self.assertFalse(executed.is_error)
        self.assertEqual(json.loads(executed.tool_result)["tool_name"], "get_chapter_outline")
        self.assertTrue(
            any(
                trace.hook_name == "permission_pre_tool_use"
                and trace.status == "passed"
                for trace in traces
            )
        )
        self.assertTrue(
            any(trace.event == "PostToolUse" for trace in traces)
        )

    async def test_agent_rebuilds_tool_pool_after_connect_in_same_request(self) -> None:
        """第一轮连接后，第二轮模型必须能调用刚发现的 MCP 工具。"""

        from langchain_core.messages import AIMessage

        from app.services import agent_tools as agent_tools_module
        from app.services import prompt_builder as prompt_builder_module
        from app.services import tool_agent as tool_agent_module
        from app.services import tool_permissions as permission_module
        from app.services.recovery import ModelCallResult

        class FakeChatModel:
            """只记录每次 bind_tools 的名称，不发送任何网络请求。"""

            def __init__(self) -> None:
                self.bound_tool_names: list[list[str]] = []

            def bind_tools(self, tools):
                self.bound_tool_names.append([tool.name for tool in tools])
                return self

        fake_model = FakeChatModel()

        async def fake_recovery_invoke(**kwargs):
            round_index = kwargs["round_index"]
            if round_index == 1:
                return ModelCallResult(
                    response=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "connect-1",
                                "name": "connect_mcp_tool",
                                "args": {"server_name": "agent-learning"},
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            if round_index == 2:
                return ModelCallResult(
                    response=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "remote-1",
                                "name": "mcp__agent-learning__get_chapter_outline",
                                "args": {"chapter_id": "s19"},
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            return ModelCallResult(response=AIMessage(content="MCP 查询完成"))

        # 四个模块都指向本测试 Service，完整走 connect tool、Permission、
        # Prompt 重建和动态分发，同时避免污染正在运行的 FastAPI 全局连接。
        with (
            patch.object(
                tool_agent_module,
                "mcp_tool_service",
                self.service,
            ),
            patch.object(
                agent_tools_module,
                "mcp_tool_service",
                self.service,
            ),
            patch.object(
                permission_module,
                "mcp_tool_service",
                self.service,
            ),
            patch.object(
                prompt_builder_module,
                "mcp_tool_service",
                self.service,
            ),
            patch.object(
                tool_agent_module,
                "get_chat_model",
                return_value=fake_model,
            ),
            patch.object(
                tool_agent_module.model_recovery_service,
                "invoke",
                new=AsyncMock(side_effect=fake_recovery_invoke),
            ),
        ):
            response = await tool_agent_module.tool_calling_agent_service.chat(
                AgentChatRequest(
                    user_id="chen",
                    message="连接课程 MCP 并查询 s19",
                    max_tool_rounds=4,
                ),
                execution_context=developer_context(),
            )

        self.assertEqual(response.answer, "MCP 查询完成")
        self.assertEqual(
            [call.tool_name for call in response.tool_calls],
            [
                "connect_mcp_tool",
                "mcp__agent-learning__get_chapter_outline",
            ],
        )
        self.assertNotIn(
            "mcp__agent-learning__get_chapter_outline",
            fake_model.bound_tool_names[0],
        )
        self.assertIn(
            "mcp__agent-learning__get_chapter_outline",
            fake_model.bound_tool_names[1],
        )
        self.assertTrue(response.current_mcp_servers[0].connected)


class McpPermissionTests(unittest.TestCase):
    """验证 Server 启动配置仍由后端控制。"""

    def test_connect_tool_rejects_server_outside_allowlist(self) -> None:
        decision = check_tool_permission(
            "connect_mcp_tool",
            {"server_name": "arbitrary-shell-server"},
            execution_context=developer_context(),
        )

        self.assertFalse(decision.allowed)
        self.assertIn("未授权", decision.reason)

    def test_unknown_mcp_tool_is_denied_by_default(self) -> None:
        decision = check_tool_permission(
            "mcp__unknown__dangerous",
            {},
            execution_context=developer_context(),
        )

        self.assertFalse(decision.allowed)


class McpRouterTests(unittest.TestCase):
    """从 FastAPI 接口验证连接、发现、调用、断开的完整控制面。"""

    def test_rest_flow_without_model(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            disconnected = client.get("/mcp")
            self.assertEqual(disconnected.status_code, 200)
            self.assertFalse(disconnected.json()["servers"][0]["connected"])

            connected = client.post("/mcp/servers/agent-learning/connect")
            self.assertEqual(connected.status_code, 200)
            self.assertEqual(connected.json()["server"]["tool_count"], 2)

            listed_tools = client.get("/tools")
            listed_names = {
                tool["name"]
                for tool in listed_tools.json()["tools"]
            }
            self.assertIn(
                "mcp__agent-learning__search_course_notes",
                listed_names,
            )

            called = client.post(
                "/mcp/call",
                json={
                    "tool_name": "mcp__agent-learning__search_course_notes",
                    "arguments": {"query": "MCP", "max_results": 3},
                },
            )
            self.assertEqual(called.status_code, 200)
            self.assertGreaterEqual(
                called.json()["result"]["structured_content"]["count"],
                1,
            )

            disconnected_again = client.post(
                "/mcp/servers/agent-learning/disconnect"
            )
            self.assertEqual(disconnected_again.status_code, 200)
            self.assertFalse(disconnected_again.json()["server"]["connected"])


if __name__ == "__main__":
    unittest.main()

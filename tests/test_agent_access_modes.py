"""产品 Agent 与开发者 Agent 权限边界的确定性回归测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.schemas import AgentChatRequest
from app.services.agent_execution_context import AgentExecutionContext
from app.services.auth import CurrentPrincipal
from app.services.agent_tool_executor import agent_tool_executor
from app.services.agent_tools import get_agent_tools, get_product_agent_tools
from app.services.tool_agent import tool_calling_agent_service
from app.services.tool_permissions import check_tool_permission
from app.routers.workspace import _execution_context_for_route


class FakeToolMessage:
    """避免测试依赖 LangChain 消息内部实现，只保留执行器需要的两个字段。"""

    def __init__(self, *, content: str, tool_call_id: str) -> None:
        self.content = content
        self.tool_call_id = tool_call_id


class RecordingTool:
    """记录底层函数是否真的执行，用来证明拒绝发生在调用之前。"""

    def __init__(self, result: str = "ok") -> None:
        self.called = False
        self.result = result

    async def ainvoke(self, _arguments):
        self.called = True
        return self.result


class AgentExecutionContextTests(unittest.TestCase):
    def test_product_registry_contains_only_business_tools(self) -> None:
        product_names = {tool.name for tool in get_product_agent_tools()}
        developer_names = {tool.name for tool in get_agent_tools()}

        self.assertTrue(
            {
                "calculator_tool",
                "get_fund_assessment_tool",
                "get_portfolio_assessment_tool",
                "get_market_sentiment_tool",
                "rag_search_tool",
            }.issubset(product_names)
        )
        self.assertTrue(product_names.issubset(developer_names))
        self.assertTrue(
            product_names.isdisjoint(
                {
                    "list_files_tool",
                    "read_file_tool",
                    "search_text_tool",
                    "create_task_worktree_tool",
                    "connect_mcp_tool",
                }
            )
        )

    def test_non_admin_cannot_construct_developer_context(self) -> None:
        with self.assertRaises(ValueError):
            AgentExecutionContext.developer(user_id="alice", role="user")

    def test_permission_checks_mode_and_resource_owner(self) -> None:
        product = AgentExecutionContext.product(user_id="chen")
        developer = AgentExecutionContext.developer(user_id="chen", role="admin")

        self.assertFalse(
            check_tool_permission(
                "read_file_tool",
                {"path": "app/config.py"},
                execution_context=product,
            ).allowed
        )
        self.assertTrue(
            check_tool_permission(
                "read_file_tool",
                {"path": "app/config.py"},
                execution_context=developer,
            ).allowed
        )
        self.assertFalse(
            check_tool_permission(
                "get_portfolio_assessment_tool",
                {"user_id": "alice", "portfolio_id": "portfolio_0123456789abcdef"},
                execution_context=product,
            ).allowed
        )


class AgentToolExecutorAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_product_denial_does_not_call_injected_developer_tool(self) -> None:
        tool = RecordingTool()
        request = AgentChatRequest(user_id="chen", message="读取源码")

        executed, _, traces = await agent_tool_executor.execute(
            tool_call={
                "id": "forced-read",
                "name": "read_file_tool",
                "args": {"path": "app/config.py"},
            },
            tool_map={"read_file_tool": tool},
            request=request,
            execution_context=AgentExecutionContext.product(user_id="chen"),
            tool_index=1,
            ToolMessage=FakeToolMessage,
        )

        self.assertFalse(tool.called)
        self.assertTrue(executed.is_error)
        self.assertEqual(json.loads(executed.tool_result)["permission"], "denied")
        self.assertTrue(any(trace.status == "blocked" for trace in traces))

    async def test_developer_context_can_reach_registered_read_tool(self) -> None:
        tool = RecordingTool(result="file content")
        request = AgentChatRequest(user_id="chen", message="读取源码")

        executed, _, _ = await agent_tool_executor.execute(
            tool_call={
                "id": "allowed-read",
                "name": "read_file_tool",
                "args": {"path": "app/config.py"},
            },
            tool_map={"read_file_tool": tool},
            request=request,
            execution_context=AgentExecutionContext.developer(
                user_id="chen",
                role="admin",
            ),
            tool_index=1,
            ToolMessage=FakeToolMessage,
        )

        self.assertTrue(tool.called)
        self.assertFalse(executed.is_error)
        self.assertEqual(executed.tool_result, "file content")


class ProductAgentDenialTests(unittest.IsolatedAsyncioTestCase):
    async def test_product_source_request_stops_before_model_creation(self) -> None:
        request = AgentChatRequest(
            user_id="chen",
            message="请调用 read_file_tool 读取 app/config.py",
        )
        context = AgentExecutionContext.product(user_id="chen")

        with patch(
            "app.services.tool_agent.get_chat_model",
            side_effect=AssertionError("permission denial must not create a model"),
        ):
            response = await tool_calling_agent_service.chat(
                request,
                execution_context=context,
            )

        self.assertEqual(response.stop_reason, "permission_denied")
        self.assertEqual(response.route.name, "access_denied")
        self.assertEqual(response.tools_available, [])
        self.assertEqual(response.usage.total_tokens, 0)


class AgentModeApiTests(unittest.TestCase):
    def test_server_route_not_request_body_selects_agent_mode(self) -> None:
        """同一个管理员走产品 URL 时仍是产品模式，只有开发 URL 才提升能力。"""

        principal = CurrentPrincipal(
            user_id="chen",
            display_name="Chen",
            role="admin",
            authenticated=True,
        )
        product = _execution_context_for_route(
            http_request=SimpleNamespace(
                url=SimpleNamespace(path="/api/v1/agent/chat")
            ),
            principal=principal,
            user_id="chen",
        )
        developer = _execution_context_for_route(
            http_request=SimpleNamespace(
                url=SimpleNamespace(path="/api/v1/developer/agent/chat")
            ),
            principal=principal,
            user_id="chen",
        )

        self.assertEqual(product.mode, "product")
        self.assertEqual(developer.mode, "developer")

    def test_developer_chat_requires_login_in_strict_mode(self) -> None:
        client = TestClient(app)
        with patch.object(settings, "auth_required", True):
            response = client.post(
                "/api/v1/developer/agent/chat",
                json={"user_id": "chen", "message": "读取源码"},
            )

        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()

"""投研模型等待预算的离线测试：不读取真实密钥，不发出模型请求。"""

import unittest
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.schemas import AgentChatRequest
from app.services.errors import AgentConfigError, AgentRuntimeError
from app.services.llm import ChatModelProviderService, OpenAICompatibleProviderConfig, get_chat_model
from app.services.recovery import ModelRecoveryError


class ModelTimeoutTests(unittest.TestCase):
    def test_report_can_request_low_thinking_for_each_provider(self):
        configs = [OpenAICompatibleProviderConfig(name, "test-model", "test-key", "https://example.invalid")
                   for name in ("primary", "fallback")]
        with patch.object(ChatModelProviderService, "_provider_configs", return_value=configs), patch("app.services.governed_model.GovernedChatOpenAI") as model:
            get_chat_model(max_retries=0, thinking=True, reasoning_effort="low")
        for call in model.call_args_list:
            self.assertEqual(call.kwargs["reasoning_effort"], "low")
            self.assertEqual(call.kwargs["extra_body"]["thinking"]["type"], "enabled")

    def test_timeout_reaches_each_provider_without_sdk_retries(self):
        # 主备供应商必须使用相同等待预算；恢复层负责重试，SDK 不能再套一层。
        configs = [OpenAICompatibleProviderConfig(name, "test-model", "test-key", "https://example.invalid")
                   for name in ("primary", "fallback")]
        with (
            patch.object(ChatModelProviderService, "_provider_configs", return_value=configs),
            patch("app.services.governed_model.GovernedChatOpenAI") as model,
        ):
            get_chat_model(max_retries=0, timeout_seconds=90)
            self.assertEqual(model.call_count, 2)
            for call in model.call_args_list:
                self.assertEqual(call.kwargs["timeout"], 90)
                self.assertEqual(call.kwargs["max_retries"], 0)

    def test_lightweight_calls_keep_thirty_second_default(self):
        with patch("app.services.llm.chat_model_provider_service.create") as create:
            get_chat_model()
        self.assertEqual(create.call_args.kwargs["timeout_seconds"], 30)

    def test_invalid_timeout_fails_before_provider_configuration(self):
        with patch.object(ChatModelProviderService, "_provider_configs") as configs:
            for value in (0, -1, float("inf"), float("nan")):
                with self.subTest(value=value), self.assertRaises(AgentConfigError):
                    get_chat_model(timeout_seconds=value)
        configs.assert_not_called()


class AgentTimeoutMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_forwards_budget_and_explains_exhausted_timeout(self):
        from app.services import tool_agent as module
        from app.services.agent_routing import AgentRequestRoute

        failure = ModelRecoveryError(
            phase="agent_round", round_index=1, original_error=TimeoutError("Request timed out."),
            traces=[], compact_traces=[],
        )
        route = AgentRequestRoute(name="direct", domains=(), tool_names=(),
                                  reason="offline test", include_mcp_tools=False)
        # 注入异常验证真实 Agent 的错误分支，既不执行工具也不写入用户会话。
        with (
            patch.object(settings, "agent_model_timeout_seconds", 75),
            patch.object(settings, "agent_model_max_attempts", 2),
            patch.object(module, "get_chat_model") as create,
            patch.object(module.agent_request_router, "route_hybrid", new=AsyncMock(return_value=route)),
            patch.object(module.model_recovery_service, "invoke", new=AsyncMock(side_effect=failure)) as invoke,
        ):
            with self.assertRaises(AgentRuntimeError) as caught:
                await module.ToolCallingAgentService().chat(AgentChatRequest(
                    user_id="timeout-offline-test", message="你好",
                ))
        create.assert_called_once_with(max_retries=0, timeout_seconds=75)
        self.assertEqual(invoke.call_args.kwargs["max_attempts"], 2)
        self.assertIn("响应超时", str(caught.exception))
        self.assertIn("不是工具调用轮数用完", str(caught.exception))

"""基金业务工具与现有 Agent Loop 的确定性集成测试。

测试通过 Fake 模型主动返回 tool_calls，不会访问 DeepSeek。这样既能证明工具确实经过
主循环、Permission 和 Hooks，又不会让自动测试依赖网络或模型临时输出。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.schemas import AgentChatRequest, AgentToolCall
from app.services.agent_tools import get_agent_tools
from app.services.fund_agent_tools import (
    get_fund_assessment_tool,
    get_fund_nav_summary_tool,
    get_fund_profile_tool,
    get_fund_risk_tool,
)
from app.services.eval_context import use_frozen_eval_data
from app.services.market_agent_tools import get_market_sentiment_tool
from app.services.prompt_builder import prompt_builder_service
from app.services.tool_permissions import check_tool_permission


class FundAgentToolTests(unittest.TestCase):
    """先验证三个工具本身返回的是模型可用的确定性数据。"""

    def test_profile_tool_uses_same_catalog_as_fund_api(self) -> None:
        payload = json.loads(
            get_fund_profile_tool.invoke({"fund_code": "161725"})
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["short_name"], "招商中证白酒指数(LOF)A")
        self.assertEqual(payload["fund_type"], "指数型-股票")
        self.assertGreaterEqual(payload["snapshot_end"], "2026-08-25")
        self.assertIn("Eastmoney", payload["source"])

    def test_nav_summary_returns_bounded_period_instead_of_all_rows(self) -> None:
        payload = json.loads(
            get_fund_nav_summary_tool.invoke(
                {"fund_code": "161725", "months": 12}
            )
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["period_months"], 12)
        self.assertGreaterEqual(payload["period_end"], "2026-08-25")
        self.assertLess(payload["record_count"], 300)
        self.assertLessEqual(
            payload["period_low"]["unit_nav"],
            payload["latest_unit_nav"],
        )
        self.assertGreaterEqual(
            payload["period_high"]["unit_nav"],
            payload["latest_unit_nav"],
        )

    def test_risk_tool_reuses_deterministic_risk_service(self) -> None:
        payload = json.loads(
            get_fund_risk_tool.invoke(
                {"fund_code": "161725", "months": 12}
            )
        )

        self.assertTrue(payload["ok"])
        self.assertIn(
            payload["data_kind"],
            {"frozen_snapshot_risk", "synced_database_snapshot_risk"},
        )
        self.assertGreater(payload["max_drawdown"]["drawdown_pct"], 0)
        self.assertGreater(payload["annualized_volatility_pct"], 0)

    def test_assessment_tool_returns_same_traceable_business_result(self) -> None:
        # 使用评测固定快照覆盖内容最多的主题基金，确保产品优化不会再次越过 4k。
        with use_frozen_eval_data():
            raw_result = get_fund_assessment_tool.invoke(
                {"fund_code": "161725", "months": 12}
            )
        payload = json.loads(raw_result)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data_kind"], "deterministic_fund_assessment")
        self.assertEqual(payload["tool_result_scope"], "bounded_agent_summary")
        self.assertEqual(payload["state_code"], "high_attention")
        self.assertTrue(payload["risk_evidence"][0]["evidence_id"])
        self.assertIn("sharpe_ratio", payload["risk_adjusted"])
        self.assertIn("excess_return_pct", payload["benchmark_comparison"])
        self.assertLess(len(raw_result), 4_000)
        self.assertGreater(payload["omitted_evidence_counts"]["risk"], 0)

    def test_risk_evidence_matches_values_and_separates_funds_and_periods(self) -> None:
        # 不用模型自证正确：直接核对证据与风险服务结果，并验证编号不会跨区间串用。
        ids = set()
        with use_frozen_eval_data():
            for code, months in (("110020", 12), ("110020", 6), ("110007", 12)):
                raw = get_fund_risk_tool.invoke({"fund_code": code, "months": months})
                payload = json.loads(raw)
                volatility, drawdown = payload["evidence"]
                self.assertEqual(volatility["value_pct"], payload["annualized_volatility_pct"])
                self.assertEqual(drawdown["value_pct"], payload["max_drawdown"]["drawdown_pct"])
                self.assertLess(len(raw), 4000)
                for item in payload["evidence"]:
                    self.assertNotIn(item["evidence_id"], ids)
                    ids.add(item["evidence_id"])
                    self.assertIn(f":{code}:{payload['period_start']}-{payload['period_end']}", item["evidence_id"])
                    self.assertEqual(item["data_date"], payload["period_end"])
                    self.assertEqual(item["source_name"], payload["source"])

    def test_fund_tools_are_registered_and_allowed_as_read_only(self) -> None:
        tool_names = {tool.name for tool in get_agent_tools()}

        self.assertIn("get_fund_profile_tool", tool_names)
        self.assertIn("get_fund_nav_summary_tool", tool_names)
        self.assertIn("get_fund_risk_tool", tool_names)
        self.assertIn("get_fund_assessment_tool", tool_names)
        for tool_name in (
            "get_fund_profile_tool",
            "get_fund_nav_summary_tool",
            "get_fund_risk_tool",
            "get_fund_assessment_tool",
        ):
            decision = check_tool_permission(tool_name, {"fund_code": "161725"})
            self.assertTrue(decision.allowed)

    def test_selected_fund_is_added_to_system_prompt(self) -> None:
        result = prompt_builder_service.build(
            user_id="chen",
            tool_names=[
                "get_fund_profile_tool",
                "get_fund_nav_summary_tool",
                "get_fund_risk_tool",
                "get_fund_assessment_tool",
                "get_market_sentiment_tool",
            ],
            active_fund_code="161725",
        )
        fund_section = next(
            section for section in result.sections if section.name == "fund_context"
        )

        self.assertIn("161725", fund_section.content)
        self.assertIn("必须调用", fund_section.content)
        self.assertIn("get_fund_assessment_tool", result.system_prompt)
        self.assertIn("evidence_id", result.system_prompt)
        self.assertIn("逐字符复制", result.system_prompt)
        self.assertIn(
            "整体状态、风险与支撑因素、消息面和条件式投资观点使用综合评估工具",
            result.system_prompt,
        )


class FundAgentLoopTests(unittest.IsolatedAsyncioTestCase):
    """验证基金工具真正穿过主 Agent Loop，而不是只在列表里显示。"""

    async def test_agent_combines_fund_and_market_tools_then_returns_answer(self) -> None:
        from langchain_core.messages import AIMessage, ToolMessage

        from app.services import tool_agent as tool_agent_module
        from app.services.recovery import ModelCallResult

        class FakeChatModel:
            """保留 bind_tools 接口，但不进行任何网络请求。"""

            def __init__(self) -> None:
                self.bound_tool_names: list[str] = []

            def bind_tools(self, tools):
                self.bound_tool_names = [tool.name for tool in tools]
                return self

        fake_model = FakeChatModel()
        observed_tool_results: list[str] = []

        async def fake_recovery_invoke(**kwargs):
            if kwargs["round_index"] == 1:
                return ModelCallResult(
                    response=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "fund-profile-1",
                                "name": "get_fund_profile_tool",
                                "args": {"fund_code": "161725"},
                                "type": "tool_call",
                            },
                            {
                                "id": "fund-nav-1",
                                "name": "get_fund_nav_summary_tool",
                                "args": {"fund_code": "161725", "months": 12},
                                "type": "tool_call",
                            },
                            {
                                "id": "fund-risk-1",
                                "name": "get_fund_risk_tool",
                                "args": {"fund_code": "161725", "months": 12},
                                "type": "tool_call",
                            },
                            {
                                "id": "market-state-1",
                                "name": "get_market_sentiment_tool",
                                "args": {},
                                "type": "tool_call",
                            },
                        ],
                    )
                )

            observed_tool_results.extend(
                message.content
                for message in kwargs["messages"]
                if isinstance(message, ToolMessage)
            )
            return ModelCallResult(
                response=AIMessage(
                    content="已根据基金档案、净值、风险指标和市场环境完成分析。"
                )
            )

        with (
            # 联合查询测试应固定市场样本，不能因用户本地尚未同步行情而随机失败。
            use_frozen_eval_data(),
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
                    fund_code="161725",
                    message=(
                        "请查询当前基金的档案、近一年净值表现、最大回撤和市场情绪"
                    ),
                    max_tool_rounds=3,
                )
            )

        self.assertEqual(
            [call.tool_name for call in response.tool_calls],
            [
                "get_fund_profile_tool",
                "get_fund_nav_summary_tool",
                "get_fund_risk_tool",
                "get_market_sentiment_tool",
            ],
        )
        self.assertTrue(all(not call.is_error for call in response.tool_calls))
        # 模型遗漏编号时，最终答案仍应保留工具真实返回的风险证据入口。
        self.assertIn("risk:volatility:161725:", response.answer)
        self.assertIn("get_fund_profile_tool", fake_model.bound_tool_names)
        self.assertIn("get_market_sentiment_tool", fake_model.bound_tool_names)
        self.assertTrue(any('"fund_code": "161725"' in item for item in observed_tool_results))
        self.assertTrue(
            any(section.name == "fund_context" for section in response.prompt_trace.sections)
        )
        self.assertTrue(any('"state_label":' in item for item in observed_tool_results))
        self.assertGreaterEqual(len(response.hooks_trace), 8)
        self.assertEqual(response.stop_reason, "model_finished")

    async def test_single_read_route_disables_tool_after_success(self) -> None:
        """窄业务路由成功一次后，第二轮应使用未绑定工具的模型完成回答。"""

        from langchain_core.messages import AIMessage

        from app.services import tool_agent as tool_agent_module
        from app.services.recovery import ModelCallResult

        class FakeChatModel:
            def __init__(self) -> None:
                self.bound_model = object()

            def bind_tools(self, tools):
                return self.bound_model

        fake_model = FakeChatModel()
        invoked_runnables: list[object] = []

        async def fake_recovery_invoke(**kwargs):
            invoked_runnables.append(kwargs["runnable"])
            if kwargs["round_index"] == 1:
                return ModelCallResult(
                    response=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "fund-profile-once",
                                "name": "get_fund_profile_tool",
                                "args": {"fund_code": "110020"},
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            return ModelCallResult(response=AIMessage(content="档案查询完成。"))

        with (
            patch.object(tool_agent_module, "get_chat_model", return_value=fake_model),
            patch.object(
                tool_agent_module.model_recovery_service,
                "invoke",
                new=AsyncMock(side_effect=fake_recovery_invoke),
            ),
        ):
            response = await tool_agent_module.tool_calling_agent_service.chat(
                AgentChatRequest(
                    user_id="chen",
                    fund_code="110020",
                    message="当前基金的名称和类型是什么？",
                    max_tool_rounds=3,
                )
            )

        self.assertIs(invoked_runnables[0], fake_model.bound_model)
        self.assertIs(invoked_runnables[1], fake_model)
        self.assertEqual(len(response.tool_calls), 1)
        self.assertTrue(
            any("single-read route completed" in step for step in response.pipeline_steps)
        )

    async def test_same_round_read_tools_execute_concurrently_in_original_order(self) -> None:
        """两个纯读取调用应重叠等待，但回填结果仍保持模型给出的顺序。"""

        from langchain_core.messages import ToolMessage

        from app.services import tool_agent as tool_agent_module

        active = 0
        max_active = 0

        async def fake_execute(**kwargs):
            nonlocal active, max_active
            tool_call = kwargs["tool_call"]
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            tool_name = tool_call["name"]
            return (
                AgentToolCall(
                    tool_name=tool_name,
                    tool_args={},
                    tool_result=f"result:{tool_name}",
                ),
                ToolMessage(
                    content=f"result:{tool_name}",
                    tool_call_id=tool_call["id"],
                ),
                [],
            )

        calls = [
            {"id": "profile", "name": "get_fund_profile_tool", "args": {}},
            {"id": "risk", "name": "get_fund_risk_tool", "args": {}},
        ]
        service = tool_agent_module.tool_calling_agent_service
        with patch.object(service, "_execute_tool_call", side_effect=fake_execute):
            results = await service._execute_tool_round(
                tool_calls=calls,
                tool_map={},
                request=AgentChatRequest(
                    user_id="chen",
                    fund_code="110020",
                    message="查询档案和风险",
                ),
                ToolMessage=ToolMessage,
                progress_callback=None,
                round_index=1,
            )

        self.assertEqual(max_active, 2)
        self.assertEqual(
            [item[1].tool_name for item in results],
            ["get_fund_profile_tool", "get_fund_risk_tool"],
        )

    async def test_round_with_write_tool_remains_sequential(self) -> None:
        """读取与写入混在同轮时整体串行，确保写入顺序稳定。"""

        from langchain_core.messages import ToolMessage

        from app.services import tool_agent as tool_agent_module

        active = 0
        max_active = 0

        async def fake_execute(**kwargs):
            nonlocal active, max_active
            tool_call = kwargs["tool_call"]
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return (
                AgentToolCall(
                    tool_name=tool_call["name"],
                    tool_args={},
                    tool_result="ok",
                ),
                ToolMessage(content="ok", tool_call_id=tool_call["id"]),
                [],
            )

        calls = [
            {"id": "profile", "name": "get_fund_profile_tool", "args": {}},
            {"id": "note", "name": "write_note_tool", "args": {}},
        ]
        service = tool_agent_module.tool_calling_agent_service
        with patch.object(service, "_execute_tool_call", side_effect=fake_execute):
            await service._execute_tool_round(
                tool_calls=calls,
                tool_map={},
                request=AgentChatRequest(user_id="chen", message="读取后写入"),
                ToolMessage=ToolMessage,
                progress_callback=None,
                round_index=1,
            )

        self.assertEqual(max_active, 1)


if __name__ == "__main__":
    unittest.main()

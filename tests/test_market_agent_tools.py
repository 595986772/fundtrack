"""市场状态 Tool 的数据、注册、权限与 Prompt 规则测试。"""

import json
import unittest

from app.services.agent_tools import get_agent_tools
from app.services.market_agent_tools import (
    get_large_capital_signals_tool,
    get_market_hotspots_tool,
    get_market_sentiment_tool,
    search_market_events_tool,
)
from app.services.eval_context import use_frozen_eval_data
from app.services.market_indices import MARKET_INDEX_DEFINITIONS
from app.services.prompt_builder import prompt_builder_service
from app.services.tool_permissions import check_tool_permission


class MarketAgentToolTests(unittest.TestCase):
    def test_market_tool_returns_traceable_snapshot(self) -> None:
        raw = get_market_sentiment_tool.invoke({})
        payload = json.loads(raw)

        # 新增科创50仍保留全部指标；多一个指数只增加一份紧凑记录。
        self.assertLess(len(raw), 4_500)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["state_label"])
        self.assertGreaterEqual(payload["data_date"], "2026-08-25")
        self.assertIn(payload["data_mode"], {"fixture", "database"})
        self.assertEqual(len(payload["evidence_ids"]), 3)
        self.assertEqual(len(payload["sources"]), 3)
        if payload["index_overview"]["data_mode"] == "database":
            indices = payload["index_overview"]["indices"]
            self.assertTrue(indices)
            self.assertTrue(
                {item["index_code"] for item in indices}
                <= {item.index_code for item in MARKET_INDEX_DEFINITIONS}
            )
            for index in payload["index_overview"]["indices"]:
                self.assertIn("return_30d_pct", index)
                self.assertIn("return_3m_pct", index)
                self.assertIn("relative_strength_rank_30d", index)
                self.assertIn("relative_strength_rank_3m", index)

    def test_market_tool_is_registered_and_read_only(self) -> None:
        tool_names = {tool.name for tool in get_agent_tools()}
        decision = check_tool_permission("get_market_sentiment_tool", {})
        capital_decision = check_tool_permission("get_large_capital_signals_tool", {})
        event_decision = check_tool_permission("search_market_events_tool", {})
        hotspot_decision = check_tool_permission("get_market_hotspots_tool", {})

        self.assertIn("get_market_sentiment_tool", tool_names)
        self.assertIn("get_large_capital_signals_tool", tool_names)
        self.assertIn("search_market_events_tool", tool_names)
        self.assertIn("get_market_hotspots_tool", tool_names)
        self.assertTrue(decision.allowed)
        self.assertTrue(capital_decision.allowed)
        self.assertTrue(event_decision.allowed)
        self.assertTrue(hotspot_decision.allowed)

    def test_hotspot_tool_returns_bounded_trend_contract(self) -> None:
        """即使尚无历史，Agent 也能收到稳定结构和明确的建立基线提示。"""

        raw = get_market_hotspots_tool.invoke({"query": "半导体", "limit": 6})
        payload = json.loads(raw)

        self.assertLess(len(raw), 8_000)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["tool_result_scope"],
            "bounded_hotspot_tracking",
        )
        self.assertIn(payload["data_mode"], {"database", "unavailable"})
        self.assertIn("lifecycle_counts", payload)
        self.assertIn("hotspots", payload)

    def test_large_capital_tool_returns_bounded_identity_aware_evidence(self) -> None:
        """Agent 看到的是紧凑证据摘要，不是整张 ETF/龙虎榜原始表。"""

        raw = get_large_capital_signals_tool.invoke({"fund_code": "161725"})
        payload = json.loads(raw)

        self.assertLess(len(raw), 4_000)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["tool_result_scope"],
            "bounded_large_capital_summary",
        )
        self.assertIn("identity_boundary", payload)
        self.assertIn("topic_signals", payload)
        self.assertIn("institutional_signal", payload)

    def test_event_tool_returns_traceable_fund_announcements(self) -> None:
        payload = json.loads(
            search_market_events_tool.invoke(
                {
                    "query": "",
                    "fund_code": "161725",
                    "event_type": "fund_announcement",
                    "limit": 5,
                }
            )
        )

        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["count"], 1)
        event = payload["events"][0]
        self.assertEqual(event["event_type"], "fund_announcement")
        self.assertIn("161725", event["affected_fund_codes"])
        self.assertIn(event["source_level"], {"A", "B"})
        self.assertIn("source_url", event)

    def test_broad_event_tool_stays_below_context_compact_threshold(self) -> None:
        """多类型事件要一次返回完整关键字段，不能被 4k ToolMessage 阈值压缩。"""

        with use_frozen_eval_data():
            raw = search_market_events_tool.invoke(
                {
                    "query": "",
                    "fund_code": "161725",
                    "event_type": ["policy", "regulation", "fund_announcement"],
                    "limit": 8,
                }
            )
        payload = json.loads(raw)

        self.assertLess(len(raw), 4_000)
        self.assertEqual(payload["tool_result_scope"], "bounded_agent_summary")
        self.assertEqual(payload["total_matched"], 6)
        self.assertEqual(len(payload["events"]), 6)
        self.assertTrue(all(item["event_id"] for item in payload["events"]))

    def test_event_tool_normalizes_common_multi_value_model_arguments(self) -> None:
        """模型生成数组或中文枚举时，工具应宽查询而不是因 schema 细节失败。"""

        with use_frozen_eval_data():
            raw = search_market_events_tool.invoke(
                {
                    "query": ["政策", "监管"],
                    "fund_code": "110020",
                    "event_type": "政策和监管",
                    "limit": 8,
                }
            )
        payload = json.loads(raw)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total_matched"], 3)

    def test_prompt_distinguishes_market_state_from_news(self) -> None:
        result = prompt_builder_service.build(
            user_id="chen",
            tool_names=[
                "get_market_sentiment_tool",
                "get_large_capital_signals_tool",
                "get_market_hotspots_tool",
                "search_market_events_tool",
            ],
            active_fund_code="161725",
        )
        tools_section = next(
            section for section in result.sections if section.name == "tools"
        )

        self.assertIn("get_market_sentiment_tool", tools_section.content)
        self.assertIn("get_large_capital_signals_tool", tools_section.content)
        self.assertIn("get_market_hotspots_tool", tools_section.content)
        self.assertIn("search_market_events_tool", tools_section.content)
        self.assertIn("event_id", tools_section.content)
        self.assertIn("不是收益预测", tools_section.content)
        self.assertIn("只调用一次", tools_section.content)
        self.assertIn("身份不可见", tools_section.content)


if __name__ == "__main__":
    unittest.main()

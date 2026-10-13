"""证据解析与最终答案 Grounding Guard 的确定性测试。"""

from __future__ import annotations

import json
import unittest

from app.schemas import AgentToolCall
from app.services.evidence import (
    ensure_answer_evidence_index,
    extract_tool_evidence_records,
)


class EvidenceGroundingTests(unittest.TestCase):
    def make_call(self) -> AgentToolCall:
        return AgentToolCall(
            tool_name="search_market_events_tool",
            tool_args={"fund_code": "161725"},
            tool_result=json.dumps(
                {
                    "ok": True,
                    "events": [
                        {"event_id": "evt-20260820-mofcom-consumption"},
                        {"event_id": "evt-20260702-consumption-plan"},
                    ],
                }
            ),
            is_error=False,
        )

    def test_missing_citation_gets_real_tool_evidence_index(self) -> None:
        answer, appended = ensure_answer_evidence_index(
            answer="商务部披露了最新消费数据。",
            tool_calls=[self.make_call()],
        )

        self.assertEqual(len(appended), 2)
        self.assertIn("证据索引（来自本轮工具）", answer)
        self.assertIn("evt-20260820-mofcom-consumption", answer)

    def test_existing_valid_citation_keeps_answer_unchanged(self) -> None:
        original = "依据 evt-20260820-mofcom-consumption，消费数据仍需观察。"
        answer, appended = ensure_answer_evidence_index(
            answer=original,
            tool_calls=[self.make_call()],
        )

        self.assertEqual(answer, original)
        self.assertEqual(appended, [])

    def test_invented_citation_is_not_hidden_by_appending_real_ids(self) -> None:
        original = "依据 evt-invented-example，消费数据已经明显改善。"
        answer, appended = ensure_answer_evidence_index(
            answer=original,
            tool_calls=[self.make_call()],
        )

        self.assertEqual(answer, original)
        self.assertEqual(appended, [])

    def test_evidence_records_keep_source_for_frontend_cards(self) -> None:
        call = AgentToolCall(
            tool_name="get_fund_assessment_tool",
            tool_args={"fund_code": "161725"},
            tool_result=json.dumps(
                {
                    "ok": True,
                    "risk_evidence": [
                        {
                            "evidence_id": "risk:max_drawdown:161725:12m",
                            "title": "历史最大回撤",
                            "source_name": "AKShare / Eastmoney Fund",
                            "source_url": "https://fund.eastmoney.com/161725.html",
                            "data_date": "2026-08-28",
                            "direction": "caution",
                        }
                    ],
                }
            ),
        )

        records = extract_tool_evidence_records([call])

        self.assertEqual(records[0]["evidence_id"], "risk:max_drawdown:161725:12m")
        self.assertEqual(records[0]["source_name"], "AKShare / Eastmoney Fund")
        self.assertEqual(records[0]["tool_name"], "get_fund_assessment_tool")

    def test_source_only_tool_gets_trace_reference(self) -> None:
        call = AgentToolCall(
            tool_name="get_fund_profile_tool",
            tool_args={"fund_code": "110020"},
            tool_result=json.dumps(
                {
                    "ok": True,
                    "fund_code": "110020",
                    "short_name": "易方达沪深300ETF联接A",
                    "snapshot_end": "2026-08-28",
                    "source": "AKShare / Eastmoney Fund",
                    "source_url": "https://fund.eastmoney.com/110020.html",
                }
            ),
        )

        records = extract_tool_evidence_records([call])

        self.assertTrue(records[0]["evidence_id"].startswith("tool:get_fund_profile_tool"))
        self.assertEqual(records[0]["source_url"], "https://fund.eastmoney.com/110020.html")

    def test_portfolio_impact_id_is_preserved_as_primary_evidence(self) -> None:
        call = AgentToolCall(
            tool_name="get_portfolio_event_impacts_tool",
            tool_args={"portfolio_id": "portfolio_aaaaaaaaaaaaaaaa"},
            tool_result=json.dumps(
                {
                    "ok": True,
                    "impacts": [
                        {
                            "impact_evidence_id": (
                                "portfolio-impact:portfolio_aaaaaaaaaaaaaaaa:evt-policy"
                            ),
                            "event_id": "evt-policy",
                            "title": "政策事件",
                            "event_date": "2026-08-30",
                            "source_name": "测试来源",
                            "source_url": "https://example.com/policy",
                            "direction": "supportive",
                        }
                    ],
                }
            ),
        )

        records = extract_tool_evidence_records([call])
        answer, appended = ensure_answer_evidence_index(
            answer="这条政策与当前持仓存在关联。",
            tool_calls=[call],
        )

        self.assertEqual(
            records[0]["evidence_id"],
            "portfolio-impact:portfolio_aaaaaaaaaaaaaaaa:evt-policy",
        )
        self.assertIn(records[0]["evidence_id"], appended)
        self.assertIn("证据索引", answer)


if __name__ == "__main__":
    unittest.main()

"""市场事件 Service 的清洗、去重、排序和查询测试。"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.services.market_events import MarketEventError, MarketEventService


class MarketEventServiceTests(unittest.TestCase):
    def test_default_search_returns_unique_events_newest_first(self) -> None:
        result = MarketEventService().search()

        self.assertEqual(result["count"], 8)
        self.assertEqual(result["total_matched"], 8)
        self.assertGreaterEqual(result["source_count"], 1)
        self.assertEqual(result["source_count"], len(result["source_names"]))
        self.assertEqual(result["events"][0]["event_id"], "evt-20260820-mofcom-consumption")
        self.assertEqual(result["events"][0]["published_date"], "2026-08-20")
        self.assertTrue(result["events"][0]["content_hash"].startswith("sha256:"))
        self.assertEqual(
            result["fresh_event_count"]
            + result["decaying_event_count"]
            + result["expired_event_count"],
            result["total_matched"],
        )
        self.assertIn(
            result["events"][0]["freshness_status"],
            {"fresh", "decaying", "expired"},
        )

    def test_search_can_combine_keyword_fund_and_type(self) -> None:
        result = MarketEventService().search(
            query="消费",
            fund_code="161725",
            event_type="policy",
        )

        self.assertEqual(result["total_matched"], 3)
        self.assertTrue(
            all("161725" in event["affected_fund_codes"] for event in result["events"])
        )
        self.assertTrue(all(event["event_type"] == "policy" for event in result["events"]))

    def test_search_supports_date_and_source_filters(self) -> None:
        result = MarketEventService().search(
            source_level="A",
            date_from=date(2026, 7, 1),
            date_to=date(2026, 8, 31),
        )

        self.assertEqual(result["total_matched"], 4)
        self.assertTrue(all(event["source_level"] == "A" for event in result["events"]))

    def test_source_text_hash_deduplicates_whitespace_and_case(self) -> None:
        base_event = {
            "event_id": "evt-one",
            "published_at": "2026-08-20T08:48:00+08:00",
            "event_date": "2026-08-20",
            "event_type": "policy",
            "title": "测试事件一",
            "summary": "第一条",
            "source_text": "Same   SOURCE text",
            "impact_direction": "neutral",
            "impact_horizon": "short_term",
            "impact_reason": "仅用于测试",
            "affected_fund_codes": ["110020"],
            "tags": ["测试"],
            "source_name": "测试来源",
            "source_level": "A",
            "source_grade_reason": "官方来源",
            "source_url": "https://example.com/one",
        }
        duplicate = {
            **base_event,
            "event_id": "evt-two",
            "title": "测试事件二",
            "source_text": " same source TEXT ",
            "source_url": "https://example.com/two",
        }

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "events.json"
            fixture.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-08-26T10:00:00+08:00",
                        "data_cutoff": "2026-08-20",
                        "events": [base_event, duplicate],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = MarketEventService(fixture).search()

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["events"][0]["event_id"], "evt-one")

    def test_invalid_date_range_is_rejected(self) -> None:
        with self.assertRaises(MarketEventError):
            MarketEventService().search(
                date_from=date(2026, 8, 20),
                date_to=date(2026, 8, 1),
            )


if __name__ == "__main__":
    unittest.main()

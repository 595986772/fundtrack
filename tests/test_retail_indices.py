"""民间指数适配器测试：全部使用明确的合成输入，不连接发布站或社媒。"""
import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.database import Database
from app.models import RetailIndexCacheModel
from app.schemas import AgentChatRequest, RetailIndexOverviewResponse
from app.services.agent_routing import agent_request_router
from app.services.market_agent_tools import get_retail_indices_tool
from app.services.retail_indices import RetailIndexError, RetailIndexService, parse_bagholder_series
from app.services.tool_permissions import check_tool_permission


def sample_data():
    # 每个工作日一条合成点位；不是展示给用户的行情 fixture。
    days = [date(2026, 4, 1) + timedelta(days=n) for n in range(157)]
    days = [day for day in days if day.weekday() < 5]
    return {
        "meta": {"internal_code": "bagholder50_index", "data_end_date": "2026-09-04",
                 "post_publication_start_date": "2026-07-13", "base_date": "2016-01-04", "base_point": 1000},
        "ohlc": [{"time": day.isoformat(), "close": 100 + n} for n, day in enumerate(days)],
        "avoid_tomorrow": [{"name": "不应输出个股名单"}],
    }


class RetailIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"sqlite:///{Path(self.temp.name) / 'test.db'}")
        self.service = RetailIndexService(self.db)
        self.raw = json.dumps(sample_data()).encode()

    def tearDown(self):
        self.db.engine.dispose()
        self.temp.cleanup()

    def test_calendar_and_trading_windows_and_precision(self):
        parsed = parse_bagholder_series(self.raw, today=date(2026, 9, 5))
        self.assertEqual(parsed["returns"]["30d"]["start_date"], "2026-08-05")
        self.assertEqual(parsed["returns"]["3m"]["start_date"], "2026-06-04")
        self.assertEqual(parsed["returns"]["5d"]["start_date"], "2026-08-28")
        self.assertAlmostEqual(parsed["returns"]["1d"]["value_pct"], (212 / 211 - 1) * 100, places=4)
        self.assertNotIn("不应输出个股名单", json.dumps(parsed, ensure_ascii=False))
        self.assertEqual(parsed["history"][0]["date"], "2026-06-04")

    def test_invalid_data_cannot_become_valid_snapshot(self):
        for mutation in ("nan", "bool", "duplicate", "future", "wrong_index", "mismatch", "unordered", "empty"):
            data = sample_data()
            if mutation == "nan": data["ohlc"][-1]["close"] = float("nan")
            if mutation == "bool": data["ohlc"][-1]["close"] = True
            if mutation == "duplicate": data["ohlc"].append(copy.deepcopy(data["ohlc"][-1]))
            if mutation == "future": data["meta"]["data_end_date"] = data["ohlc"][-1]["time"] = "2027-09-04"
            if mutation == "wrong_index": data["meta"]["internal_code"] = "another_index"
            if mutation == "mismatch": data["meta"]["data_end_date"] = "2026-09-03"
            if mutation == "unordered": data["ohlc"].reverse()
            if mutation == "empty": data["ohlc"] = []
            with self.subTest(mutation=mutation), self.assertRaises(RetailIndexError):
                parse_bagholder_series(json.dumps(data).encode(), today=date(2026, 9, 5))

    def test_insufficient_history_is_null_not_zero(self):
        data = sample_data()
        data["ohlc"] = data["ohlc"][-2:]
        parsed = parse_bagholder_series(json.dumps(data).encode(), today=date(2026, 9, 5))
        self.assertIsNone(parsed["returns"]["30d"]["value_pct"])
        self.assertIsNone(parsed["returns"]["5d"]["value_pct"])
        self.assertIsNotNone(parsed["returns"]["1d"]["value_pct"])

    def test_read_never_fetches_or_fabricates_mom(self):
        with patch("app.services.retail_indices.fetch_bagholder_series") as fetch:
            result = self.service.overview()
        fetch.assert_not_called()
        self.assertEqual([item["indicator_id"] for item in result["indicators"]], ["mom", "bagholder50"])
        mom, bagholder = result["indicators"]
        self.assertEqual(mom["status"], "source_unavailable")
        self.assertIsNone(mom["value"])
        self.assertEqual(bagholder["status"], "not_synced")
        RetailIndexOverviewResponse.model_validate(result)

    def test_refresh_persists_and_repeat_does_not_fetch(self):
        with patch("app.services.retail_indices.fetch_bagholder_series", return_value=self.raw) as fetch:
            result = self.service.refresh()
            self.service.refresh()
            self.assertEqual(fetch.call_count, 1)
        persisted = RetailIndexService(self.db).overview()
        self.assertEqual(result["indicators"][1]["value"], persisted["indicators"][1]["value"])
        self.assertTrue(persisted["indicators"][1]["source_hash"])
        self.assertIsNotNone(persisted["indicators"][1]["fetched_at"])

    def _expire_cooldown(self):
        with self.db.session() as session, session.begin():
            session.get(RetailIndexCacheModel, "bagholder50").attempted_at = datetime.now(timezone.utc) - timedelta(minutes=2)

    def test_error_keeps_old_value_date_and_fetch_time(self):
        with patch("app.services.retail_indices.fetch_bagholder_series", return_value=self.raw):
            before = self.service.refresh()["indicators"][1]
        self._expire_cooldown()
        with patch("app.services.retail_indices.fetch_bagholder_series", side_effect=RetailIndexError("来源不可用")):
            after = self.service.refresh()["indicators"][1]
        self.assertEqual(after["status"], "cached_error")
        for key in ("value", "data_date", "fetched_at", "source_hash"):
            self.assertEqual(before[key], after[key])
        self.assertTrue(after["last_error"])

    def test_source_rollback_cannot_replace_newer_data(self):
        with patch("app.services.retail_indices.fetch_bagholder_series", return_value=self.raw):
            self.service.refresh()
        self._expire_cooldown()
        data = sample_data()
        data["ohlc"].pop()
        data["meta"]["data_end_date"] = "2026-09-03"
        with patch("app.services.retail_indices.fetch_bagholder_series", return_value=json.dumps(data).encode()):
            item = self.service.refresh()["indicators"][1]
        self.assertEqual(item["data_date"], "2026-09-04")
        self.assertEqual(item["status"], "cached_error")

    def test_staleness_is_based_on_data_date_not_download_time(self):
        with patch("app.services.retail_indices.fetch_bagholder_series", return_value=self.raw):
            self.service.refresh()
        result = self.service.overview(now=datetime(2026, 9, 10, tzinfo=timezone.utc))
        self.assertEqual(result["indicators"][1]["status"], "stale")

    def test_agent_uses_same_read_only_snapshot_and_bounded_output(self):
        with patch("app.services.retail_indices.fetch_bagholder_series", return_value=self.raw):
            self.service.refresh()
        with patch("app.services.market_agent_tools.retail_index_service", self.service):
            raw = get_retail_indices_tool.invoke({})
        self.assertLess(len(raw), 4000)
        self.assertNotIn('"history":[{', raw)
        self.assertTrue(json.loads(raw)["indicators"][1]["evidence_id"])
        self.assertTrue(check_tool_permission("get_retail_indices_tool", {}).allowed)
        for code in (None, "161725"):
            route = agent_request_router.route(AgentChatRequest(user_id="chen", message="看看宝妈指数和韭菜指数", fund_code=code))
            self.assertIn("get_retail_indices_tool", route.tool_names)

    def test_rest_read_and_refresh_contract(self):
        from app.main import app
        # 不启动应用 lifespan，防止测试顺带启动项目中已有的定时任务。
        client = TestClient(app)
        self.addCleanup(client.close)
        with patch("app.routers.market.retail_index_service", self.service):
            with patch("app.services.retail_indices.fetch_bagholder_series", return_value=self.raw) as fetch:
                self.assertEqual(client.get("/api/v1/market/retail-indices").status_code, 200)
                fetch.assert_not_called()
                response = client.post("/api/v1/market/retail-indices/refresh")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()["indicators"]), 2)
                self.assertTrue(response.json()["indicators"][1]["history"])


if __name__ == "__main__":
    unittest.main()

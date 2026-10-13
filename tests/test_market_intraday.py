"""盘中行情的离线回归测试，所有外部响应为合成输入，不触发真实采集。"""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from app.database import Database
from app.schemas import MarketIntradayOverviewResponse
from app.services.market_indices import MARKET_INDEX_DEFINITIONS
from app.services.market_intraday import (
    SHANGHAI, MarketIntradayError, MarketIntradayService,
    TencentIntradayProvider, parse_quotes, quote_freshness,
)

NOW = datetime(2026, 9, 9, 14, 30, tzinfo=SHANGHAI)


def sample(*, quote_time="20260909142930", edits=None, missing=()):
    lines = []
    for item in MARKET_INDEX_DEFINITIONS:
        if item.provider_symbol in missing:
            continue
        fields = [""] * 38
        fields[1], fields[2] = item.index_name, item.index_code
        fields[3], fields[4], fields[30] = "4000", "3900", quote_time
        fields[36], fields[37] = "1234567", "123456.78"
        if edits:
            for index, value in edits.items():
                fields[index] = value
        lines.append(f'v_{item.provider_symbol}={json.dumps("~".join(fields), ensure_ascii=False)};')
    return "\n".join(lines)


class IntradayParserTests(unittest.TestCase):
    def test_units_timestamp_and_recomputed_return(self):
        quotes, errors = parse_quotes(sample(), now=NOW)
        self.assertFalse(errors)
        self.assertEqual(len(quotes), 7)
        self.assertIn("sh000688", quotes)
        item = quotes["sh000001"]
        self.assertAlmostEqual(item["amount_cny"], 1_234_567_800)
        self.assertEqual(item["volume_lots"], 1_234_567)
        self.assertEqual(item["quote_at"], "2026-09-09T14:29:30+08:00")
        self.assertAlmostEqual(item["change_pct"], 2.5641)

    def test_invalid_values_never_become_zero_or_today(self):
        for edits in ({36: ""}, {36: "NaN"}, {37: "inf"}, {37: "-1"},
                      {37: "1e308"}, {30: "20270909143000"}, {30: ""}, {4: "0"}, {2: "bad"}):
            with self.subTest(edits=edits):
                quotes, errors = parse_quotes(sample(edits=edits), now=NOW)
                self.assertFalse(quotes)
                self.assertEqual(len(errors), 7)

    def test_zero_volume_valid_partial_data_and_script_not_executed(self):
        quotes, errors = parse_quotes(sample(edits={36: "0", 37: "0"}, missing=("sh000852",)) + '\nalert("bad");', now=NOW)
        self.assertEqual(quotes["sh000001"]["amount_cny"], 0)
        self.assertEqual(set(errors), {"sh000852"})
        quotes, errors = parse_quotes('<html>blocked</html>', now=NOW)
        self.assertFalse(quotes)
        self.assertEqual(len(errors), 7)

    def test_intraday_staleness_lunch_close_and_old_date(self):
        cases = [
            ("2026-09-09T14:29:30", "2026-09-09T14:30:00", "recent"),
            ("2026-09-09T10:00:00", "2026-09-09T14:30:00", "stale"),
            ("2026-09-09T11:30:00", "2026-09-09T12:30:00", "recent"),
            ("2026-09-09T15:00:00", "2026-09-09T22:00:00", "recent"),
            ("2026-09-09T14:30:00", "2026-09-09T22:00:00", "stale"),
            ("2026-09-08T15:00:00", "2026-09-09T08:00:00", "previous_date"),
            ("2026-09-11T15:00:00", "2026-09-12T12:00:00", "previous_date"),
        ]
        for quote, now, expected in cases:
            with self.subTest(quote=quote, now=now):
                self.assertEqual(quote_freshness(datetime.fromisoformat(quote).replace(tzinfo=SHANGHAI), datetime.fromisoformat(now).replace(tzinfo=SHANGHAI)), expected)

    def test_provider_is_one_fixed_batch_request_with_timeout(self):
        response = httpx.Response(200, content=sample().encode("gb18030"), request=httpx.Request("GET", "https://qt.gtimg.cn/"))
        with patch("app.services.market_intraday.httpx.Client") as client:
            client.return_value.__enter__.return_value.get.return_value = response
            self.assertEqual(len(TencentIntradayProvider().fetch(now=NOW)[0]), 7)
            client.assert_called_once_with(timeout=12, follow_redirects=False)
            request = client.return_value.__enter__.return_value.get
            request.assert_called_once()
            self.assertEqual(len(request.call_args.kwargs["params"]["q"].split(",")), 7)
            self.assertIn("sh000688", request.call_args.kwargs["params"]["q"].split(","))


class IntradayServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"sqlite:///{Path(self.temp.name) / 'intraday.db'}")
        self.provider = Mock()
        self.provider.fetch.return_value = parse_quotes(sample(), now=NOW)
        self.service = MarketIntradayService(self.db, self.provider)

    def tearDown(self):
        self.db.engine.dispose()
        self.temp.cleanup()

    def test_empty_read_is_readonly_and_nullable(self):
        result = MarketIntradayOverviewResponse.model_validate(self.service.overview(now=NOW))
        self.provider.fetch.assert_not_called()
        self.assertEqual(len(result.indices), 7)
        self.assertTrue(all(item.amount_cny is None and item.status == "not_synced" for item in result.indices))

    def test_refresh_persists_across_service_instances_and_throttles(self):
        before = self.service.refresh(now=NOW)
        another = MarketIntradayService(self.db, self.provider)
        after = another.refresh(now=NOW + timedelta(seconds=5))
        self.provider.fetch.assert_called_once()
        self.assertEqual(before["indices"], after["indices"])
        MarketIntradayOverviewResponse.model_validate(after)
        self.assertNotIn("total_amount_cny", after)

    def test_existing_six_index_cache_does_not_invent_star50_quote(self):
        # 模拟升级前只保存六个指数，新增科创50应显示缺失，手动刷新后才有真实值。
        self.provider.fetch.return_value = parse_quotes(sample(missing=("sh000688",)), now=NOW)
        before = self.service.refresh(now=NOW)
        star50 = next(item for item in before["indices"] if item["index_code"] == "000688")
        self.assertIsNone(star50.get("amount_cny"))
        self.assertEqual(before["indices"][0]["status"], "available")
        self.provider.fetch.return_value = parse_quotes(sample(), now=NOW)
        after = self.service.refresh(now=NOW + timedelta(seconds=31))
        star50 = next(item for item in after["indices"] if item["index_code"] == "000688")
        self.assertEqual(star50["status"], "available")
        self.assertGreater(star50["amount_cny"], 0)

    def test_failure_preserves_old_data_and_retries_after_cooldown(self):
        before = self.service.refresh(now=NOW)["indices"][0]
        self.provider.fetch.side_effect = MarketIntradayError("暂不可用")
        after = self.service.refresh(now=NOW + timedelta(seconds=31))["indices"][0]
        for key in ("amount_cny", "volume_lots", "quote_at", "fetched_at"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(after["status"], "cached_error")
        self.provider.fetch.side_effect = None
        self.assertEqual(self.service.refresh(now=NOW + timedelta(seconds=62))["indices"][0]["status"], "available")

    def test_partial_failure_and_timestamp_rollback(self):
        self.service.refresh(now=NOW)
        self.provider.fetch.return_value = parse_quotes(sample(quote_time="20260909143030", missing=("sh000852",)), now=NOW + timedelta(seconds=31))
        result = self.service.refresh(now=NOW + timedelta(seconds=31))
        self.assertEqual(result["indices"][0]["quote_at"], "2026-09-09T14:30:30+08:00")
        self.assertEqual(result["indices"][4]["status"], "cached_error")
        self.provider.fetch.return_value = parse_quotes(sample(), now=NOW)
        result = self.service.refresh(now=NOW + timedelta(seconds=62))
        self.assertIn("回退", result["indices"][0]["error"])
        self.assertEqual(result["indices"][0]["quote_at"], "2026-09-09T14:30:30+08:00")

    def test_parallel_service_instances_share_refresh_claim(self):
        entered, release = Event(), Event()
        def fetch(**kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return parse_quotes(sample(), now=NOW)
        self.provider.fetch.side_effect = fetch
        another = MarketIntradayService(self.db, self.provider)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.service.refresh, now=NOW)
            try:
                self.assertTrue(entered.wait(5))
                second = another.refresh(now=NOW + timedelta(seconds=1))
                self.assertTrue(second["refreshing"])
                self.provider.fetch.assert_called_once()
            finally:
                release.set()
            self.assertEqual(first.result()["indices"][0]["status"], "available")

    def test_get_and_post_routes_are_separate(self):
        from app.main import app
        with patch("app.routers.market.market_intraday_service", self.service):
            client = TestClient(app)
            self.addCleanup(client.close)
            self.assertEqual(client.get("/api/v1/market/intraday").status_code, 200)
            self.provider.fetch.assert_not_called()
            response = client.post("/api/v1/market/intraday/refresh")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["indices"]), 7)
            self.provider.fetch.assert_called_once()

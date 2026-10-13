"""全市场汇总回归：合成证券/临时数据库，禁止访问真实数据源。"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from app.database import Database
from app.schemas import MarketTurnoverOverviewResponse
from app.services.market_turnover import (
    BASE_URL, MarketTurnoverError, MarketTurnoverService,
    SinaMarketTurnoverProvider, aggregate_turnover,
)

NOW = datetime(2026, 9, 9, 7, 0, tzinfo=timezone.utc)


def rows():
    return [{"symbol": "sh600000", "amount": 100.25},
            {"symbol": "sh688001", "amount": 200},
            {"symbol": "sz000001", "amount": 300},
            {"symbol": "sz300001", "amount": 400},
            {"symbol": "bj920001", "amount": 50}]


def aggregate(data=None, expected_count=5, reference=NOW):
    return aggregate_turnover(data if data is not None else rows(), expected_count=expected_count,
                              reference_at=reference, started_at=NOW, fetched_at=NOW)


class MarketTurnoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"sqlite:///{Path(self.temp.name) / 'test.db'}")
        self.provider = Mock()
        self.provider.fetch.return_value = aggregate()
        self.service = MarketTurnoverService(self.db, self.provider)

    def tearDown(self):
        self.db.engine.dispose()
        self.temp.cleanup()

    def test_a_share_groups_and_totals_do_not_double_count(self):
        result = aggregate()
        self.assertEqual(result["sh_sz_amount_cny"], 1000.25)
        self.assertEqual(result["sh_sz_bj_amount_cny"], 1050.25)
        self.assertEqual([x["security_count"] for x in result["exchanges"]], [2, 2, 1])
        # 完全相同的重复记录只计一次；若它占用了缺失证券的位置，下面的覆盖校验会拒绝。
        self.assertEqual(aggregate(rows() + [rows()[0]])["sh_sz_bj_amount_cny"], 1050.25)

    def test_missing_pages_duplicates_and_unknown_assets_are_rejected(self):
        for data in (rows()[:-1], rows()[:-1] + [rows()[0]],
                     rows() + [{"symbol": "sh510300", "amount": 1}],
                     rows() + [{"symbol": "sh900001", "amount": 1}],
                     rows() + [{"symbol": "hk00700", "amount": 1}],
                     rows() + [{"symbol": "sh600000", "amount": 999}]):
            with self.subTest(data=data), self.assertRaises(MarketTurnoverError):
                aggregate(data)
        with self.assertRaises(MarketTurnoverError):
            aggregate(rows()[:-1], expected_count=4)

    def test_null_invalid_or_overflow_is_not_zero_but_real_zero_is_valid(self):
        for value in (None, "-", float("nan"), float("inf"), -1, True):
            data = rows()
            data[0]["amount"] = value
            with self.subTest(value=value), self.assertRaises(MarketTurnoverError):
                aggregate(data)
        data = rows()
        data[0]["amount"] = 0
        self.assertEqual(aggregate(data)["sh_sz_bj_amount_cny"], 950)
        for row in data: row["amount"] = 1e308
        with self.assertRaises(MarketTurnoverError): aggregate(data)

    def test_read_is_empty_nullable_and_never_collects(self):
        result = self.service.overview(now=NOW)
        self.assertEqual(result["status"], "not_synced")
        self.assertIsNone(result["snapshot"])
        self.provider.fetch.assert_not_called()
        MarketTurnoverOverviewResponse.model_validate(result)

    def test_refresh_cache_is_persistent_and_shared_across_instances(self):
        before = self.service.refresh(now=NOW)
        service = MarketTurnoverService(self.db, self.provider)
        after = service.refresh(now=NOW + timedelta(seconds=60))
        self.provider.fetch.assert_called_once()
        self.assertEqual(before["snapshot"], after["snapshot"])
        self.assertEqual(after["next_refresh_at"], NOW + timedelta(seconds=180))
        MarketTurnoverOverviewResponse.model_validate(after)

    def test_failure_preserves_entire_batch_and_source_date(self):
        before = self.service.refresh(now=NOW)
        self.provider.fetch.side_effect = MarketTurnoverError("缺页")
        after = self.service.refresh(now=NOW + timedelta(seconds=181))
        self.assertEqual(after["status"], "cached_error")
        self.assertEqual(after["snapshot"], before["snapshot"])
        self.assertIn("缺页", after["error"])
        self.provider.fetch.side_effect = None
        self.assertEqual(self.service.refresh(now=NOW + timedelta(seconds=362))["status"], "available")

    def test_source_rollback_and_old_date_are_not_today(self):
        self.service.refresh(now=NOW)
        self.provider.fetch.return_value = aggregate(reference=NOW - timedelta(days=1))
        result = self.service.refresh(now=NOW + timedelta(days=1))
        self.assertEqual(result["status"], "cached_error")
        self.assertEqual(result["freshness"], "previous_date")
        self.assertIn("回退", result["error"])

    def test_provider_checks_all_pages_and_stable_count(self):
        # 小分页替身验证最后一页和首尾数量复核，不需要真的抓几千条行情。
        def check(changed=False, truncated=False):
            clock_calls, count_calls = [], []
            def get(url, params=None, **kwargs):
                request = httpx.Request("GET", url)
                if "hq.sinajs" in url:
                    clock_calls.append(url)
                    fields = ["0"] * 32
                    fields[30], fields[31] = "2026-09-09", "15:00:00"
                    return httpx.Response(200, content=('var hq_str_sh000001="' + ','.join(fields) + '";').encode(), request=request)
                if url.endswith("StockCount"):
                    count_calls.append(url)
                    return httpx.Response(200, json=str(6 if changed and len(count_calls) > 1 else 5), request=request)
                self.assertEqual(url, BASE_URL + "Market_Center.getHQNodeData")
                self.assertEqual(params["node"], "hs_a")
                index = (params["page"] - 1) * 2
                data = rows()[index:index + 2]
                return httpx.Response(200, json=[] if truncated else data, request=request)
            with patch("app.services.market_turnover.PAGE_SIZE", 2), patch("app.services.market_turnover.httpx.Client") as client:
                client.return_value.__enter__.return_value.get.side_effect = get
                return SinaMarketTurnoverProvider().fetch(now=NOW)
        self.assertEqual(check()["security_count"], 5)
        with self.assertRaises(MarketTurnoverError): check(changed=True)
        with self.assertRaises(MarketTurnoverError): check(truncated=True)

    def test_api_read_and_manual_refresh_are_isolated(self):
        from app.main import app
        client = TestClient(app)
        self.addCleanup(client.close)
        with patch("app.routers.market.market_turnover_service", self.service):
            self.assertEqual(client.get("/api/v1/market/turnover").status_code, 200)
            self.provider.fetch.assert_not_called()
            result = client.post("/api/v1/market/turnover/refresh")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["snapshot"]["sh_sz_bj_amount_cny"], 1050.25)
            self.provider.fetch.assert_called_once()

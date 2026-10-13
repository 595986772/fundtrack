"""日成交额离线测试：核对金额列、独立日线缓存、周期与手动刷新边界。"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import unittest

import httpx
from fastapi.testclient import TestClient

from app.database import Database
from app.schemas import MarketDailyTurnoverResponse
from app.services.market_daily_turnover import (
    DailyTurnoverError, MarketDailyTurnoverService, TencentDailyTurnoverProvider,
    definition_for, normalize_daily,
)

NOW = datetime(2026, 9, 9, 8, tzinfo=timezone.utc)
START, END = date(2026, 6, 1), date(2026, 9, 8)


def raw_row(day="2026-09-08", amount="7871391.45"):
    # 第5列6759192.00是手数；第8列才是万元，不能因为字段英文叫amount就猜单位。
    return [day, "1614.19", "1591.00", "1624.69", "1585.12", "6759192.00", {}, "0.96", amount, "0", "0"]


def payload(symbol="sh000688", rows=None):
    return {"code": 0, "data": {symbol: {"day": rows if rows is not None else [raw_row()]}}}


class DailyParserTests(unittest.TestCase):
    def test_daily_amount_is_not_volume_and_today_is_excluded(self):
        rows = [raw_row(), raw_row("2026-09-09", "999999")]
        data = normalize_daily(payload(rows=rows), "sh000688", START, END)
        self.assertEqual(len(data), 1)
        self.assertAlmostEqual(data[0]["amount_cny"], 78713914500)
        self.assertEqual(data[0]["date"], "2026-09-08")

    def test_bad_missing_and_conflicting_rows_fail_instead_of_fake_zero(self):
        for value in (None, "", "NaN", "inf", "-1", "1e308", True):
            with self.subTest(value=value), self.assertRaises(DailyTurnoverError):
                normalize_daily(payload(rows=[raw_row(amount=value)]), "sh000688", START, END)
        with self.assertRaises(DailyTurnoverError):
            normalize_daily(payload(rows=[raw_row(),raw_row(amount="5")]), "sh000688", START, END)
        for value in ([], None, {"code":0,"data":[]}):
            with self.subTest(value=value), self.assertRaises(DailyTurnoverError):
                normalize_daily(value, "sh000688", START, END)
        self.assertEqual(normalize_daily(payload(rows=[raw_row(amount="0")]), "sh000688", START, END)[0]["amount_cny"], 0)

    def test_duplicate_dates_are_unique_and_sorted(self):
        rows = [raw_row(), raw_row("2026-09-07"), raw_row()]
        result = normalize_daily(payload(rows=rows), "sh000688", START, END)
        self.assertEqual([p["date"] for p in result], ["2026-09-07","2026-09-08"])

    def test_reference_sum_requires_same_date_and_never_fills_missing_side(self):
        def get(url, params):
            symbol=params["param"].split(",")[0]
            rows=[raw_row(amount="100"), raw_row("2026-09-07", "200")] if symbol=="sh000001" else [raw_row(amount="300")]
            return httpx.Response(200,json=payload(symbol,rows),request=httpx.Request("GET",url))
        with patch("app.services.market_daily_turnover.httpx.Client") as client:
            client.return_value.__enter__.return_value.get.side_effect=get
            result=TencentDailyTurnoverProvider().fetch(definition_for("reference:sh_sz"), end=END)
            self.assertEqual(client.return_value.__enter__.return_value.get.call_count,2)
        self.assertIsNone(result[0]["amount_cny"])
        self.assertEqual(result[1]["amount_cny"],4000000)


class DailyServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.db=Database(f"sqlite:///{Path(self.temp.name)/'daily.db'}")
        self.provider=Mock()
        self.provider.fetch.return_value=[
            {"date": (END-timedelta(days=i)).isoformat(), "amount_cny": 100e8+i}
            for i in reversed(range(100)) if (END-timedelta(days=i)).weekday()<5
        ]
        self.service=MarketDailyTurnoverService(self.db,self.provider)

    def tearDown(self):
        self.db.engine.dispose()
        self.temp.cleanup()

    def test_readonly_empty_state_does_not_fetch(self):
        result=self.service.overview(now=NOW)
        self.provider.fetch.assert_not_called()
        self.assertEqual(result["points"],[])
        self.assertEqual(len(result["series"]),8)
        MarketDailyTurnoverResponse.model_validate(result)

    def test_calendar_period_and_restart_preserve_daily_points(self):
        three=self.service.refresh("index:sh000688",now=NOW)
        thirty=MarketDailyTurnoverService(self.db,self.provider).overview("index:sh000688","30d",now=NOW)
        self.assertGreater(len(three["points"]),len(thirty["points"]))
        self.assertEqual(thirty["range_start"],END-timedelta(days=29))
        self.assertTrue(all(p["date"]<NOW.date().isoformat() for p in three["points"]))
        self.assertEqual(three["data_date"],"2026-09-08")
        self.provider.fetch.assert_called_once()
        self.assertIn("不等于",self.service.overview(now=NOW)["scope"])

    def test_cooldown_failure_and_rollback_keep_old_daily_data(self):
        before=self.service.refresh(now=NOW)
        self.service.refresh(now=NOW+timedelta(seconds=10))
        self.provider.fetch.assert_called_once()
        self.provider.fetch.side_effect=DailyTurnoverError("来源不可用")
        after=self.service.refresh(now=NOW+timedelta(seconds=181))
        self.assertEqual(after["points"],before["points"])
        self.assertEqual(after["status"],"cached_error")
        self.provider.fetch.side_effect=None
        self.provider.fetch.return_value=[{"date":"2026-09-07","amount_cny":20}]
        after=self.service.refresh(now=NOW+timedelta(seconds=362))
        self.assertEqual(after["points"],before["points"])
        self.assertIn("回退",after["error"])

    def test_invalid_selection_has_no_collection_side_effect(self):
        for series,period in (("market:sh_sz","3m"),("index:sh000688","1d")):
            with self.assertRaises(ValueError): self.service.refresh(series,period,now=NOW)
        self.provider.fetch.assert_not_called()

    def test_daily_get_and_post_endpoints_are_separate(self):
        from app.main import app
        with TestClient(app) as client, patch("app.routers.market.market_daily_turnover_service",self.service):
            self.assertEqual(client.get("/api/v1/market/turnover/daily").status_code,200)
            self.provider.fetch.assert_not_called()
            response=client.post("/api/v1/market/turnover/daily/refresh?series_id=index:sh000688&period=30d")
            self.assertEqual(response.status_code,200)
            self.assertGreater(len(response.json()["points"]),1)
            self.provider.fetch.assert_called_once()


if __name__ == "__main__": unittest.main()

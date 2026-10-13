"""成交额采样历史回归：不访问外网，验证真实点、跨日和最新快照的一致性。"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import Database
from app.models import MarketIntradayCacheModel, MarketTurnoverPointModel
from app.schemas import MarketTurnoverHistoryResponse
from app.services.market_intraday import MarketIntradayService, parse_quotes
from app.services.market_turnover import MarketTurnoverError, MarketTurnoverService
from app.services.market_turnover_history import MarketTurnoverHistoryService, save_points
from tests.test_market_intraday import NOW, sample
from tests.test_market_turnover import aggregate


class TurnoverHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"sqlite:///{Path(self.temp.name) / 'history.db'}")
        self.service = MarketTurnoverHistoryService(self.db)

    def tearDown(self):
        self.db.engine.dispose()
        self.temp.cleanup()

    def count(self):
        with self.db.session() as session:
            return session.scalar(select(func.count()).select_from(MarketTurnoverPointModel))

    def test_empty_get_and_legacy_cache_get_never_write_history(self):
        self.assertEqual(self.service.history(now=NOW)["points"], [])
        with self.db.session() as session, session.begin():
            session.add(MarketIntradayCacheModel(id="all_a_turnover", payload={"snapshot": aggregate()}))
        result = self.service.history(now=NOW)
        self.assertEqual(len(result["points"]), 1)
        self.assertEqual(result["points"][0]["amount_cny"], 1000.25)
        self.assertEqual(self.count(), 0)
        self.assertEqual(len(result["series"]), 12)
        MarketTurnoverHistoryResponse.model_validate(result)

    def test_refresh_preserves_legacy_start_and_new_points_survive_restart(self):
        old = parse_quotes(sample(), now=NOW)[0]
        with self.db.session() as session, session.begin():
            session.add(MarketIntradayCacheModel(id="major_indices", payload={"quotes": old}))
        provider = Mock()
        provider.fetch.return_value = parse_quotes(sample(quote_time="20260909143100", edits={37: "223456.78"}), now=NOW + timedelta(minutes=1))
        MarketIntradayService(self.db, provider).refresh(now=NOW + timedelta(minutes=1))
        result = MarketTurnoverHistoryService(self.db).history("index:sh000688", now=NOW)
        self.assertEqual(len(result["points"]), 2)
        self.assertEqual(result["points"][0]["amount_cny"], old["sh000688"]["amount_cny"])
        self.assertGreater(result["points"][1]["amount_cny"], result["points"][0]["amount_cny"])

    def test_same_source_time_is_one_point_even_after_repeated_refresh(self):
        provider = Mock()
        provider.fetch.return_value = parse_quotes(sample(), now=NOW)
        service = MarketIntradayService(self.db, provider)
        service.refresh(now=NOW)
        service.refresh(now=NOW + timedelta(seconds=10))  # 节流，不应访问来源。
        service.refresh(now=NOW + timedelta(seconds=31))
        self.assertEqual(provider.fetch.call_count, 2)
        self.assertEqual(self.count(), 7)
        self.assertEqual(len(self.service.history("index:sh000688", now=NOW)["points"]), 1)

    def test_partial_failure_and_source_time_rollback_never_add_fake_points(self):
        provider = Mock()
        provider.fetch.return_value = parse_quotes(sample(), now=NOW)
        service = MarketIntradayService(self.db, provider)
        service.refresh(now=NOW)
        provider.fetch.return_value = parse_quotes(sample(quote_time="20260909143100", missing=("sh000688",)), now=NOW + timedelta(minutes=1))
        service.refresh(now=NOW + timedelta(minutes=1))
        self.assertEqual(len(self.service.history("index:sh000688", now=NOW)["points"]), 1)
        self.assertEqual(len(self.service.history("index:sh000001", now=NOW)["points"]), 2)
        provider.fetch.return_value = parse_quotes(sample(quote_time="20260909140000"), now=NOW)
        service.refresh(now=NOW + timedelta(minutes=2))
        self.assertEqual(len(self.service.history("index:sh000001", now=NOW)["points"]), 2)

    def test_failed_full_market_refresh_does_not_extend_curve(self):
        provider = Mock()
        provider.fetch.return_value = aggregate()
        service = MarketTurnoverService(self.db, provider)
        service.refresh(now=NOW)
        before = self.service.history(now=NOW)["points"]
        provider.fetch.side_effect = MarketTurnoverError("缺页")
        service.refresh(now=NOW + timedelta(minutes=4))
        self.assertEqual(self.service.history(now=NOW)["points"], before)
        # 五种完整市场口径各存一点，没有指数混入或部分批次。
        self.assertEqual(self.count(), 5)

    def test_dates_use_shanghai_and_never_join_different_days(self):
        now = datetime(2026, 9, 10, 0, 5, tzinfo=NOW.tzinfo)
        with self.db.session() as session, session.begin():
            save_points(session, [
                {"series_id": "market:sh_sz", "quote_at": "2026-09-09T15:59:00Z",
                 "fetched_at": "2026-09-09T16:00:00Z", "amount_cny": 500, "security_count": 5},
                {"series_id": "market:sh_sz", "quote_at": "2026-09-09T16:01:00Z",
                 "fetched_at": "2026-09-09T16:02:00Z", "amount_cny": 0, "security_count": 5},
            ], now=now)
        latest = self.service.history(now=now)
        self.assertEqual(latest["data_date"], now.date())
        self.assertEqual(len(latest["points"]), 1)
        self.assertEqual(latest["points"][0]["amount_cny"], 0)
        previous = self.service.history(data_date=(now - timedelta(days=1)).date(), now=now)
        self.assertFalse(previous["is_today"])
        self.assertEqual(previous["points"][0]["amount_cny"], 500)
        self.assertEqual(len(previous["available_dates"]), 2)

    def test_corrections_are_not_forced_up_and_coverage_changes_warn(self):
        with self.db.session() as session, session.begin():
            save_points(session, [
                {"series_id": "market:sh_sz", "quote_at": NOW, "fetched_at": NOW,
                 "amount_cny": 500, "security_count": 5},
                {"series_id": "market:sh_sz", "quote_at": NOW + timedelta(minutes=3), "fetched_at": NOW + timedelta(minutes=3),
                 "amount_cny": 490, "security_count": 4},
            ], now=NOW)
        result = self.service.history(now=NOW)
        self.assertEqual([p["amount_cny"] for p in result["points"]], [500, 490])
        self.assertEqual(len(result["warnings"]), 2)

    def test_history_retention_discards_old_points(self):
        with self.db.session() as session, session.begin():
            save_points(session, [{"series_id": "market:sh_sz", "quote_at": NOW - timedelta(days=90),
                                  "fetched_at": NOW, "amount_cny": 20, "security_count": 5}], now=NOW)
        self.assertEqual(self.count(), 0)

    def test_history_api_is_readonly_and_validates_selection(self):
        from app.main import app
        with TestClient(app) as client, patch("app.routers.market.market_turnover_history_service", self.service):
            self.assertEqual(client.get("/api/v1/market/turnover/history").status_code, 200)
            self.assertEqual(client.get("/api/v1/market/turnover/history?series_id=wrong").status_code, 400)
            self.assertEqual(client.get("/api/v1/market/turnover/history?data_date=not-a-date").status_code, 422)
            self.assertEqual(client.post("/api/v1/market/turnover/history").status_code, 405)
            self.assertEqual(self.count(), 0)


if __name__ == "__main__":
    unittest.main()

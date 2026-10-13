"""验证交易日/北京时间边界及时间快照；交易写入只发生在临时数据库。"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import Database
from app.main import app
from app.services.fund_transaction_timing import calculate_transaction_timing
from app.services.portfolios import PortfolioService, PortfolioValidationError, PortfolioIdempotencyConflictError


PROFILE = {"fund_code": "110020", "short_name": "沪深300ETF联接A", "fund_type": "指数型-股票"}


def estimate(at, direction="buy", **kwargs):
    return calculate_transaction_timing(profile=PROFILE, submitted_at=datetime.fromisoformat(at),
                                        transaction_type=direction, **kwargs)


class TransactionCalendarTests(unittest.TestCase):
    def test_cutoff_and_timezone(self):
        for at, expected in [
            ("2026-09-10T00:30:00+08:00", "2026-09-10"),
            ("2026-09-10T12:30:00+08:00", "2026-09-10"),
            ("2026-09-10T14:59:59+08:00", "2026-09-10"),
            ("2026-09-10T06:59:59Z", "2026-09-10"),
            ("2026-09-10T15:00:00+08:00", "2026-09-11"),
            ("2026-09-10T07:01:00Z", "2026-09-11"),
        ]:
            with self.subTest(at=at):
                self.assertEqual(estimate(at)["nav_date"], expected)
        self.assertTrue(any("边界" in s for s in estimate("2026-09-10T15:00:00+08:00")["warnings"]))

    def test_weekend_holidays_and_makeup_workday(self):
        for at, nav, first in [
            ("2026-09-11T14:00:00+08:00", "2026-09-11", "2026-09-14"),
            ("2026-09-11T16:00:00+08:00", "2026-09-14", "2026-09-15"),
            ("2026-09-12T10:00:00+08:00", "2026-09-14", "2026-09-15"),
            ("2026-09-24T16:00:00+08:00", "2026-09-28", "2026-09-29"),
            ("2026-09-30T15:01:00+08:00", "2026-10-08", "2026-10-09"),
            ("2026-10-01T10:00:00+08:00", "2026-10-08", "2026-10-09"),
            # 调休上班不等于交易所开市。
            ("2026-10-10T10:00:00+08:00", "2026-10-12", "2026-10-13"),
        ]:
            with self.subTest(at=at):
                value = estimate(at)
                self.assertEqual(value["nav_date"], nav)
                self.assertEqual(value["first_return_nav_date"], first)
                self.assertEqual(value["confirmation_date"], first)
                self.assertIsNone(value["last_return_nav_date"])

    def test_sell_keeps_nav_day_return_but_not_later(self):
        value = estimate("2026-09-11T14:00:00+08:00", "sell")
        self.assertEqual(value["last_return_nav_date"], "2026-09-11")
        self.assertEqual(value["confirmation_date"], "2026-09-14")
        self.assertIsNone(value["first_return_nav_date"])
        self.assertIn("到账日", "".join(value["warnings"]))

    def test_special_funds_and_channel_require_product_rules(self):
        for name, kind in [("纳斯达克", "QDII"), ("养老", "FOF"), ("现金", "货币"),
                           ("港股通", "股票型"), ("一年持有期", "混合型"), ("50ETF", "指数型"), ("未知", "")]:
            with self.subTest(kind=kind):
                result = calculate_transaction_timing(
                    profile={**PROFILE, "short_name": name, "fund_type": kind},
                    submitted_at=datetime.fromisoformat("2026-09-10T14:00:00+08:00"), transaction_type="buy")
                self.assertEqual(result["status"], "manual_required")
                self.assertIsNone(result["nav_date"])
        self.assertEqual(estimate("2026-09-10T14:00:00+08:00", channel="exchange")["status"], "manual_required")

    def test_calendar_out_of_bounds_does_not_invent_weekdays(self):
        self.assertEqual(estimate("2099-01-01T14:00:00+08:00")["status"], "calendar_unavailable")
        with patch("app.services.fund_transaction_timing._calendar", side_effect=ValueError("unavailable")):
            self.assertIsNone(estimate("2026-09-10T14:00:00+08:00")["nav_date"])

    def test_naive_datetime_rejected(self):
        with self.assertRaisesRegex(ValueError, "时区"):
            estimate("2026-09-10T14:00:00")

    def test_readonly_api_and_validation(self):
        client = TestClient(app)
        # 替换档案查询，整个估算不访问外部行情/LLM，不写用户资产。
        with patch("app.services.fund_transaction_timing.fund_catalog_service.get_fund", return_value={"profile": PROFILE}):
            response = client.post("/api/v1/funds/110020/transaction-timing", json={
                "submitted_at": "2026-09-30T15:01:00+08:00", "transaction_type": "buy"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["first_return_nav_date"], "2026-10-09")
        for at in ("2026-09-10T14:00:00", "invalid"):
            self.assertEqual(client.post("/api/v1/funds/110020/transaction-timing", json={
                "submitted_at": at, "transaction_type": "buy"}).status_code, 422)
        self.assertEqual(client.post("/api/v1/funds/999999/transaction-timing", json={
            "submitted_at": "2026-09-10T14:00:00+08:00", "transaction_type": "buy"}).status_code, 404)


class TransactionTimingLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"sqlite:///{Path(self.temp.name) / 'ledger.db'}")
        self.service = PortfolioService(self.db)
        self.portfolio = self.service.create(user_id="timing-test", name="临时测试账本", holdings=[{
            "fund_code": "110020", "units": 100, "cost_amount": 100, "purchase_date": date(2025, 1, 2)}])
        self.input = dict(user_id="timing-test", portfolio_id=self.portfolio["id"], fund_code="110020",
                          transaction_type="buy", trade_date=date(2025, 1, 6), units=10, unit_price=2,
                          idempotency_key="timing-test-buy", submitted_at=datetime.fromisoformat("2025-01-03T16:00:00+08:00"))

    def tearDown(self):
        self.db.engine.dispose()
        self.temp.cleanup()

    def test_snapshot_persists_and_legacy_records_remain_unknown(self):
        result = self.service.add_transaction(**self.input)
        self.assertEqual(result["timing"]["nav_date"], "2025-01-06")
        self.assertEqual(result["timing"]["first_return_nav_date"], "2025-01-07")
        records = self.service.list_transactions(user_id="timing-test", portfolio_id=self.portfolio["id"])
        initial = next(t for t in records if t["trade_date"] == date(2025, 1, 2))
        saved = next(t for t in records if t["id"] == result["id"])
        self.assertIsNone(initial["timing"])
        self.assertEqual(saved["timing"], result["timing"])
        held = self.service.get(user_id="timing-test", portfolio_id=self.portfolio["id"])["holdings"][0]
        self.assertEqual(held["units"], 110)
        self.assertEqual(held["cost_amount"], 120)

    def test_idempotency_uses_original_inputs_not_recalculated_dates(self):
        first = self.service.add_transaction(**self.input)
        changed = {**first["timing"], "confirmation_date": "2025-01-08"}
        with patch("app.services.portfolios.preview_transaction_timing", return_value=changed):
            retry = self.service.add_transaction(**self.input)
        self.assertTrue(retry["idempotency_replayed"])
        self.assertEqual(retry["timing"], first["timing"])
        # UTC 与北京时间表达同一时间，应识别为同一笔重试。
        same = self.service.add_transaction(**{**self.input, "submitted_at": self.input["submitted_at"].astimezone(timezone.utc)})
        self.assertEqual(same["id"], first["id"])
        with self.assertRaises(PortfolioIdempotencyConflictError):
            self.service.add_transaction(**{**self.input, "submitted_at": self.input["submitted_at"] + timedelta(minutes=1)})
        with self.assertRaises(PortfolioIdempotencyConflictError):
            self.service.add_transaction(**{**self.input, "channel": "exchange"})

    def test_actual_receipt_can_override_estimate_without_silent_rewrite(self):
        result = self.service.add_transaction(**{**self.input, "trade_date": date(2025, 1, 7)})
        self.assertEqual(result["trade_date"], date(2025, 1, 7))
        self.assertEqual(result["timing"]["nav_date"], "2025-01-06")
        self.assertEqual(result["timing"]["recorded_nav_date"], "2025-01-07")
        self.assertIn("回单", "".join(result["timing"]["warnings"]))

    def test_transaction_api_forwards_and_returns_timing_snapshot(self):
        client = TestClient(app)
        payload = {**self.input, "trade_date": "2025-01-06", "submitted_at": "2025-01-03T16:00:00+08:00"}
        payload.pop("portfolio_id")
        path = f"/api/v1/portfolios/{self.portfolio['id']}/transactions"
        with patch("app.routers.portfolios.portfolio_service", self.service):
            response = client.post(path, json=payload)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["timing"]["first_return_nav_date"], "2025-01-07")
            replay = client.post(path, json=payload)
            self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
            records = client.get(path, params={"user_id": "timing-test"}).json()["transactions"]
            self.assertEqual(next(t for t in records if t["id"] == response.json()["id"])["timing"], response.json()["timing"])

    def test_impossible_and_future_trades_do_not_write(self):
        for overrides in ({"trade_date": date(2025, 1, 2)},
                          {"submitted_at": datetime.now(timezone.utc) + timedelta(days=10)},
                          {"submitted_at": datetime(2025, 1, 3, 16)}):
            with self.subTest(overrides=overrides), self.assertRaises(PortfolioValidationError):
                self.service.add_transaction(**{**self.input, **overrides})
        self.assertEqual(len(self.service.list_transactions(user_id="timing-test", portfolio_id=self.portfolio["id"])), 1)

    def test_additive_migration_is_repeatable_and_preserves_old_rows(self):
        other = Database(f"sqlite:///{Path(self.temp.name) / 'old.db'}")
        try:
            with other.engine.begin() as conn:
                conn.execute(text("CREATE TABLE fund_portfolio_transactions (user_id TEXT, portfolio_id TEXT, idempotency_key TEXT, id TEXT)"))
                conn.execute(text("INSERT INTO fund_portfolio_transactions (id) VALUES ('old')"))
            other._apply_additive_migrations()
            other._apply_additive_migrations()
            with other.engine.connect() as conn:
                self.assertEqual(conn.execute(text("SELECT id, timing FROM fund_portfolio_transactions")).one(), ("old", None))
        finally:
            other.engine.dispose()

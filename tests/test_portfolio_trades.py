"""申请、撤单结果与账务更正回归，全部写入临时库，不连接真实交易平台。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import FundPortfolioTransactionModel, PortfolioTransactionCorrectionModel
from app.services.portfolio_trades import PortfolioTradeService
from app.services.portfolios import PortfolioValidationError, PortfolioIdempotencyConflictError, PortfolioNotFoundError
from tests.test_portfolios import TemporaryPortfolioTestCase


class PortfolioTradeTests(TemporaryPortfolioTestCase):
    def setUp(self):
        super().setUp()
        self.trades = PortfolioTradeService(self.portfolios)
        self.portfolio = self.portfolios.create(user_id="chen", name="测试账本", holdings=[{
            "fund_code": "110020", "units": 1000, "cost_amount": 1000,
            "purchase_date": date(2025, 1, 2),
        }])
        self.scope = {"user_id": "chen", "portfolio_id": self.portfolio["id"]}

    def order(self, direction="sell", key="test-order-key", **overrides):
        values = dict(**self.scope, idempotency_key=key, fund_code="110020",
                      transaction_type=direction, submitted_at=datetime(2025, 2, 3, 6, tzinfo=timezone.utc))
        values.update(requested_units="300" if direction == "sell" else None,
                      requested_amount="360.50" if direction == "buy" else None)
        values.update(overrides)
        return self.trades.create_order(**values)

    def action(self, order, action, key=None, **values):
        return self.trades.apply_action(**self.scope, order_id=order["id"], action=action,
            reason="已核对外部平台回执", idempotency_key=key or f"test-action-{action}", **values)

    def confirm(self, order, **values):
        fields = dict(trade_date=date(2025, 2, 3), confirmation_date=date(2025, 2, 4),
                      units=Decimal("300"), unit_price=Decimal("1.2"), fee=Decimal("0.50"))
        fields.update(values)
        return self.action(order, "confirm", **fields)

    def current(self):
        return self.portfolios.get(**self.scope)

    def rows(self):
        return self.portfolios.list_transactions(**self.scope)

    def test_buy_is_pending_until_confirmed_and_retry_posts_once(self):
        order = self.order("buy")
        self.assertEqual(self.current()["pending_buy_amount"], "360.50")
        self.assertEqual(self.current()["holdings"][0]["units"], 1000)
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(self.order("buy")["idempotency_replayed"])
        confirmed = self.confirm(order)
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(self.current()["holdings"][0]["units"], 1300)
        self.assertEqual(self.current()["total_cost_amount"], 1360.5)
        self.assertTrue(self.confirm(order)["idempotency_replayed"])
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.current()["pending_order_count"], 0)
        with self.assertRaises(PortfolioValidationError):
            self.action(order, "settle", settlement_date=date(2025, 2, 5))

    def test_cancel_request_keeps_reserve_until_success(self):
        order = self.order()
        holding = self.current()["holdings"][0]
        self.assertEqual((holding["units"], holding["reserved_units"], holding["available_units"]), (1000, 300, 700))
        self.assertEqual(self.action(order, "cancel_request")["status"], "cancel_requested")
        self.assertEqual(self.current()["holdings"][0]["reserved_units"], 300)
        self.assertEqual(self.action(order, "cancel_success")["status"], "cancelled")
        self.assertEqual(self.current()["holdings"][0]["available_units"], 1000)
        self.assertEqual(len(self.rows()), 1)
        with self.assertRaises(PortfolioValidationError):
            self.confirm(order)

    def test_cancel_rejected_returns_to_pending(self):
        order = self.order()
        self.action(order, "cancel_request")
        self.assertEqual(self.action(order, "cancel_rejected")["status"], "pending")
        self.assertEqual(self.current()["holdings"][0]["available_units"], 700)
        self.action(order, "reject")
        self.assertEqual(self.current()["holdings"][0]["available_units"], 1000)

    def test_confirm_can_win_cancel_race_and_settlement_is_separate(self):
        order = self.order()
        self.action(order, "cancel_request")
        confirmed = self.confirm(order)
        self.assertEqual(confirmed["confirmed_amount"], "359.50")
        self.assertEqual(self.current()["unsettled_redemption_amount"], "359.50")
        self.assertEqual(self.current()["holdings"][0]["units"], 700)
        self.assertEqual(self.current()["realized_pnl_amount"], 59.5)
        with self.assertRaises(PortfolioValidationError):
            self.action(order, "cancel_success")
        with self.assertRaises(PortfolioValidationError):
            self.action(order, "settle", settlement_date=date(2025, 2, 3))
        self.action(order, "settle", settlement_date=date(2025, 2, 6))
        self.assertEqual(self.current()["unsettled_redemption_amount"], "0.00")
        self.assertTrue(self.action(order, "settle", settlement_date=date(2025, 2, 6))["idempotency_replayed"])
        self.assertEqual(len(self.rows()), 2)

    def test_pending_orders_and_direct_sales_share_available_units(self):
        self.order(requested_units="700")
        with self.assertRaises(PortfolioValidationError):
            self.order(key="second-order-key", requested_units="301")
        with self.assertRaises(PortfolioValidationError):
            self.portfolios.add_transaction(**self.scope, fund_code="110020", transaction_type="sell",
                trade_date=date(2025, 2, 3), units=301, unit_price=1, idempotency_key="direct-sale-key")
        self.assertEqual(len(self.trades.list_orders(**self.scope)), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_concurrent_orders_cannot_oversell(self):
        barrier = Barrier(2)
        def submit(index):
            barrier.wait()
            try:
                self.order(key=f"concurrent-{index}", requested_units="600")
                return "ok"
            except PortfolioValidationError:
                return "blocked"
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertCountEqual(executor.map(submit, (1, 2)), ["ok", "blocked"])
        self.assertEqual(self.current()["holdings"][0]["available_units"], 400)

    def test_idempotency_rejects_changed_order_action_and_correction(self):
        order = self.order()
        with self.assertRaises(PortfolioIdempotencyConflictError):
            self.order(requested_units="301")
        self.action(order, "cancel_request", key="same-action-key")
        with self.assertRaises(PortfolioIdempotencyConflictError):
            self.action(order, "cancel_success", key="same-action-key")
        self.action(order, "cancel_success")
        tx = self.rows()[0]
        params = dict(**self.scope, transaction_id=tx["id"], idempotency_key="correct-same-key", reason="误录")
        self.trades.correct_transaction(**params)
        self.assertTrue(self.trades.correct_transaction(**params)["idempotency_replayed"])
        with self.assertRaises(PortfolioIdempotencyConflictError):
            self.trades.correct_transaction(**{**params, "reason": "不同理由"})

    def test_confirmation_invalid_dates_and_partial_values_roll_back(self):
        order = self.order()
        for invalid in [dict(units=299), dict(confirmation_date=date(2025, 2, 2)),
                        dict(trade_date=date(2025, 1, 31)), dict(fee=999)]:
            with self.assertRaises(PortfolioValidationError):
                self.confirm(order, **invalid)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.trades.get_order(**self.scope, order_id=order["id"])["status"], "pending")
        self.assertEqual(self.current()["holdings"][0]["reserved_units"], 300)

    def test_buy_confirmation_must_match_application_amount(self):
        order = self.order("buy")
        with self.assertRaises(PortfolioValidationError):
            self.confirm(order, units=100)
        self.assertEqual(len(self.rows()), 1)

    def test_correction_keeps_original_and_updates_unsettled_projection(self):
        confirmed = self.confirm(self.order())
        original = next(row for row in self.rows() if row["id"] == confirmed["transaction_id"])
        replacement = dict(fund_code="110020", transaction_type="sell", trade_date=date(2025, 2, 3),
                           units="300", unit_price="1.200001", fee="0.10", note="按确认单更正")
        corrected = self.trades.correct_transaction(**self.scope, transaction_id=original["id"],
            idempotency_key="correct-sale-key", reason="手续费录错", replacement=replacement)
        rows = self.rows()
        old = next(row for row in rows if row["id"] == original["id"])
        new = next(row for row in rows if row["id"] == corrected["replacement_transaction_id"])
        self.assertEqual(old["status"], "corrected")
        self.assertFalse(old["effective"])
        self.assertEqual(old["fee"], 0.5)
        self.assertEqual(new["exact_values"]["unit_price"], "1.200001")
        self.assertEqual(self.current()["unsettled_redemption_amount"], "359.90")
        self.assertEqual(self.current()["holdings"][0]["units"], 700)
        self.trades.correct_transaction(**self.scope, transaction_id=new["id"],
            idempotency_key="reverse-sale-key", reason="这不是本账户的成交")
        self.assertEqual(self.current()["holdings"][0]["units"], 1000)
        self.assertEqual(self.current()["unsettled_redemption_amount"], "0.00")
        self.assertEqual(self.trades.get_order(**self.scope, order_id=confirmed["id"])["status"], "reversed")
        self.assertTrue(self.confirm(confirmed)["idempotency_replayed"])
        self.assertEqual(len(self.rows()), 3)

    def test_reversing_buy_that_would_create_historical_oversell_rolls_back(self):
        opening = self.rows()[0]
        self.confirm(self.order())
        with self.assertRaises(PortfolioValidationError):
            self.trades.correct_transaction(**self.scope, transaction_id=opening["id"],
                idempotency_key="reverse-opening-key", reason="误录")
        self.assertTrue(next(row for row in self.rows() if row["id"] == opening["id"])["effective"])
        with self.database.session() as session:
            self.assertIsNone(session.scalar(select(PortfolioTransactionCorrectionModel)))

    def test_correction_must_not_invalidate_reservations(self):
        self.order(requested_units="900")
        opening = self.rows()[0]
        with self.assertRaises(PortfolioValidationError):
            self.trades.correct_transaction(**self.scope, transaction_id=opening["id"],
                idempotency_key="reduce-opening-key", reason="修正份额", replacement={
                    "fund_code": "110020", "transaction_type": "buy", "trade_date": date(2025, 1, 2),
                    "units": "800", "unit_price": "1", "fee": "0"})
        self.assertEqual(self.current()["holdings"][0]["units"], 1000)

    def test_correction_preserves_business_order_but_records_actual_write_time(self):
        opening = self.rows()[0]
        self.portfolios.add_transaction(**self.scope, fund_code="110020", transaction_type="sell",
            trade_date=date(2025, 1, 2), units=1000, unit_price=1.2, idempotency_key="same-day-sale-key")
        corrected = self.trades.correct_transaction(**self.scope, transaction_id=opening["id"],
            idempotency_key="same-day-fix-key", reason="初始成本修正", replacement={
                "fund_code":"110020", "transaction_type":"buy", "trade_date":date(2025,1,2),
                "units":"1000", "unit_price":"1.05", "fee":"0"})
        current = self.current()
        self.assertEqual(current["holdings"], [])
        self.assertEqual(current["realized_pnl_amount"], 150)
        new = next(row for row in self.rows() if row["id"] == corrected["replacement_transaction_id"])
        self.assertGreater(new["created_at"], opening["created_at"])
        self.assertEqual(new["exact_values"]["ledger_id"], opening["id"])
        # 返回接口的真实时间与业务重放时间不同，业绩服务仍须先买后卖。
        from app.services.portfolio_performance import PortfolioPerformanceService
        performance = PortfolioPerformanceService(portfolios=self.portfolios)
        rows, _ = performance._actual_rows(portfolio=current, transactions=[row for row in self.rows() if row["effective"]])
        self.assertTrue(rows)

    def test_legacy_nav_is_not_truncated_by_new_input_precision(self):
        from app.services.portfolio_ledger_values import transaction_values
        row = FundPortfolioTransactionModel(units=1000000, unit_price=1.23456789, fee=0, exact_values=None)
        self.assertEqual(transaction_values(row)[1], Decimal("1.23456789"))

    def test_linked_correction_cannot_bypass_full_confirmation_rule(self):
        confirmed = self.confirm(self.order())
        with self.assertRaises(PortfolioValidationError):
            self.trades.correct_transaction(**self.scope, transaction_id=confirmed["transaction_id"],
                idempotency_key="partial-fix-key", reason="不能变成部分确认", replacement={
                    "fund_code":"110020", "transaction_type":"sell", "trade_date":date(2025,2,3),
                    "units":"100", "unit_price":"1.2", "fee":"0"})

    def test_full_reversal_keeps_zero_ledger_and_prevents_overwrite(self):
        self.trades.correct_transaction(**self.scope, transaction_id=self.rows()[0]["id"],
            idempotency_key="reverse-whole-key", reason="重复导入")
        self.assertEqual(self.current()["position_mode"], "actual")
        self.assertEqual(self.current()["total_market_value"], 0)
        self.assertEqual(self.current()["holdings"], [])
        with self.assertRaises(PortfolioValidationError):
            self.portfolios.delete(**self.scope)
        with self.assertRaises(PortfolioValidationError):
            self.portfolios.update(**self.scope, name="覆盖", holdings=[{"fund_code": "110020", "weight_pct": 100}])

    def test_users_and_portfolios_are_isolated(self):
        order = self.order()
        with self.assertRaises(PortfolioNotFoundError):
            self.trades.list_orders(user_id="someone-else", portfolio_id=self.portfolio["id"])
        other = self.create_sample()
        with self.assertRaises(PortfolioNotFoundError):
            self.trades.apply_action(user_id="chen", portfolio_id=other["id"], order_id=order["id"],
                action="cancel_success", reason="无权操作", idempotency_key="other-scope-key")
        with self.assertRaises(PortfolioNotFoundError):
            self.trades.correct_transaction(user_id="chen", portfolio_id=other["id"], transaction_id=self.rows()[0]["id"],
                reason="无权操作", idempotency_key="other-reverse-key")

    def test_decimal_input_is_stored_exactly_and_final_sale_releases_cost(self):
        small = self.portfolios.create(user_id="chen", name="小数账本", holdings=[{
            "fund_code": "110020", "units": 3, "cost_amount": "0.10", "purchase_date": date(2025, 1, 2)}])
        for index in range(3):
            self.portfolios.add_transaction(user_id="chen", portfolio_id=small["id"],
                fund_code="110020", transaction_type="sell", trade_date=date(2025, 2, index + 3),
                units="1.0000", unit_price="0.050001", fee="0", idempotency_key=f"decimal-sell-{index}")
        current = self.portfolios.get(user_id="chen", portfolio_id=small["id"])
        self.assertEqual(current["total_cost_amount"], 0)
        self.assertEqual(current["realized_pnl_amount"], 0.05)
        with self.database.session() as session:
            row = session.scalar(select(FundPortfolioTransactionModel).where(
                FundPortfolioTransactionModel.idempotency_key == "decimal-sell-0"))
            self.assertEqual(row.exact_values["unit_price"], "0.050001")

    def test_api_contract_and_history_guard_do_not_clear_other_records(self):
        from app.routers import portfolios as router
        with patch.object(router, "portfolio_service", self.portfolios), patch.object(
            router.portfolio_monitoring_service, "delete_monitoring_data") as cleanup:
            client = TestClient(app)
            base = f"/api/v1/portfolios/{self.portfolio['id']}"
            payload = {"user_id": "chen", "idempotency_key": "api-order-key", "fund_code": "110020",
                       "transaction_type": "sell", "requested_units": "30.1234", "submitted_at": "2025-02-03T14:00:00+08:00"}
            created = client.post(base + "/trade-orders", json=payload)
            self.assertEqual(created.status_code, 201, created.text)
            self.assertEqual(client.post(base + "/trade-orders", json=payload).status_code, 201)
            self.assertEqual(client.post(base + "/trade-orders", json={**payload, "requested_units": "31"}).status_code, 409)
            self.assertEqual(client.post(base + "/trade-orders", json={**payload, "submitted_at": "2025-02-03T14:00:00"}).status_code, 422)
            self.assertEqual(client.post(base + f"/trade-orders/{created.json()['id']}/actions", json={
                "user_id": "chen", "idempotency_key": "invalid-action-key", "action": "confirm", "reason": "缺字段"}).status_code, 422)
            self.assertEqual(client.delete(base + "?user_id=chen").status_code, 400)
            cleanup.assert_not_called()
            self.assertEqual(client.get(base + "/trade-orders?user_id=someone").status_code, 404)

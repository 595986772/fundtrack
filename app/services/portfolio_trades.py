"""基金申请、确认、撤单结果和账务更正的状态机。

这是一份外部账户的本地镜像。日期估算不会推动状态，模型也没有这些写入工具。
只有用户明确登记外部结果才改变状态；状态、事件与持仓投影必须一起提交或一起回滚。
"""

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from uuid import uuid4

from sqlalchemy import select

from app.models import (FundPortfolioHoldingModel, FundPortfolioTransactionModel,
    PortfolioTradeOrderEventModel, PortfolioTradeOrderModel, PortfolioTransactionCorrectionModel)
from app.services.agent_authorization import guard_agent_portfolio, guard_agent_write
from app.services.fund_transaction_timing import CHINA_TIMEZONE
from app.services.portfolio_ledger_values import MONEY, UNITS, ZERO, ledger_decimal, money, transaction_values
from app.services.portfolios import (PortfolioIdempotencyConflictError, PortfolioNotFoundError,
    PortfolioValidationError, portfolio_service)


def _json_values(value):
    """事件里的日期/Decimal 用字符串保存，重试比较不经过浮点转换。"""
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


class PortfolioTradeService:
    def __init__(self, portfolios=portfolio_service):
        self.portfolios = portfolios
        self.database = portfolios.database

    def create_order(self, *, user_id, portfolio_id, idempotency_key, fund_code,
                     transaction_type, submitted_at, requested_amount=None,
                     requested_units=None, channel="off_exchange", note=""):
        # 申请会占用可用份额，即便尚未成交也不属于 Agent 的只读研究能力。
        guard_agent_write()
        key = self.portfolios._normalize_transaction_idempotency_key(idempotency_key)
        self.portfolios._ensure_fund_exists(fund_code)
        if transaction_type not in {"buy", "sell"} or channel not in {"off_exchange", "exchange"}:
            raise PortfolioValidationError("申请方向或渠道无效")
        if not isinstance(submitted_at, datetime) or submitted_at.utcoffset() is None or submitted_at > datetime.now(timezone.utc):
            raise PortfolioValidationError("申请时间须包含时区，且不能晚于当前时间")
        try:
            amount = str(ledger_decimal(requested_amount, MONEY, positive=True, label="申购金额")) if transaction_type == "buy" else None
            units = str(ledger_decimal(requested_units, UNITS, positive=True, label="赎回份额")) if transaction_type == "sell" else None
        except ValueError as exc:
            raise PortfolioValidationError(str(exc)) from exc
        if (transaction_type == "buy" and requested_units is not None) or (transaction_type == "sell" and requested_amount is not None):
            raise PortfolioValidationError("买入填金额，卖出填份额，不能混填")
        payload = dict(fund_code=fund_code, transaction_type=transaction_type,
            submitted_at=submitted_at.astimezone(timezone.utc).isoformat(), requested_amount=amount,
            requested_units=units, channel=channel, note=note.strip())
        now = datetime.now(timezone.utc)
        replayed = False
        with self.database.session() as session, session.begin():
            portfolio = self.portfolios._get_model(session, user_id=user_id, portfolio_id=portfolio_id, for_update=True)
            existing = session.scalar(select(PortfolioTradeOrderModel).where(
                PortfolioTradeOrderModel.user_id == user_id, PortfolioTradeOrderModel.portfolio_id == portfolio_id,
                PortfolioTradeOrderModel.idempotency_key == key))
            if existing:
                previous = self._creation_payload(existing)
                if previous != payload:
                    raise PortfolioIdempotencyConflictError("同一个申请幂等键不能用于不同内容")
                order_id, replayed = existing.id, True
            else:
                if session.scalar(select(FundPortfolioHoldingModel.fund_code).where(
                    FundPortfolioHoldingModel.user_id == user_id, FundPortfolioHoldingModel.portfolio_id == portfolio_id,
                    FundPortfolioHoldingModel.position_mode == "allocation").limit(1)):
                    raise PortfolioValidationError("请先把目标权重组合转换为真实份额账本")
                order_id = f"ord_{uuid4().hex[:16]}"
                order = PortfolioTradeOrderModel(user_id=user_id, portfolio_id=portfolio_id, id=order_id,
                    idempotency_key=key, **{**payload, "submitted_at": submitted_at.astimezone(timezone.utc)}, status="pending",
                    created_at=now, updated_at=now)
                session.add(order)
                session.flush()
                # 新申请与其他未决申请一起检查可用份额，锁内检查避免并发重复减仓。
                self.portfolios._rebuild_actual_holdings(session, user_id=user_id, portfolio_id=portfolio_id, now=now)
                self._event(session, order, "created", "", "pending", "登记外部交易申请", payload, f"create:{order_id}")
                portfolio.updated_at = now
        return {**self.get_order(user_id=user_id, portfolio_id=portfolio_id, order_id=order_id), "idempotency_replayed": replayed}

    def list_orders(self, *, user_id, portfolio_id):
        guard_agent_portfolio(user_id, portfolio_id)
        with self.database.session() as session:
            self.portfolios._get_model(session, user_id=user_id, portfolio_id=portfolio_id)
            orders = list(session.scalars(select(PortfolioTradeOrderModel).where(
                PortfolioTradeOrderModel.user_id == user_id, PortfolioTradeOrderModel.portfolio_id == portfolio_id
            ).order_by(PortfolioTradeOrderModel.created_at.desc(), PortfolioTradeOrderModel.id)))
            for owner_id, real_portfolio_id in {(order.user_id, order.portfolio_id) for order in orders}:
                guard_agent_portfolio(owner_id, real_portfolio_id)
            return [self._view(session, item) for item in orders]

    def get_order(self, *, user_id, portfolio_id, order_id):
        with self.database.session() as session:
            self.portfolios._get_model(session, user_id=user_id, portfolio_id=portfolio_id)
            order = self._order(session, user_id, portfolio_id, order_id)
            result = self._view(session, order)
        guard_agent_portfolio(order.user_id, order.portfolio_id)
        return result

    def apply_action(self, *, user_id, portfolio_id, order_id, idempotency_key, action, reason, **values):
        # 在取得记账写锁前拒绝 Agent；领域 guard 不另开审计写事务。
        guard_agent_write()
        key = self.portfolios._normalize_transaction_idempotency_key(idempotency_key)
        reason = self._reason(reason)
        payload = _json_values({"order_id": order_id, "action": action, "reason": reason, **values})
        now = datetime.now(timezone.utc)
        replayed = False
        with self.database.session() as session, session.begin():
            portfolio = self.portfolios._get_model(session, user_id=user_id, portfolio_id=portfolio_id, for_update=True)
            order = self._order(session, user_id, portfolio_id, order_id)
            previous = session.scalar(select(PortfolioTradeOrderEventModel).where(
                PortfolioTradeOrderEventModel.user_id == user_id,
                PortfolioTradeOrderEventModel.portfolio_id == portfolio_id,
                PortfolioTradeOrderEventModel.idempotency_key == key))
            if previous:
                if previous.payload != payload:
                    raise PortfolioIdempotencyConflictError("该动作幂等键已用于其他内容")
                replayed = True
            else:
                old_status = order.status
                # 撤单请求失败可以回到待确认；外部成交与撤单竞态以实际结果为依据。
                transitions = {
                    "cancel_request": ({"pending"}, "cancel_requested"),
                    "cancel_success": ({"pending", "cancel_requested"}, "cancelled"),
                    "cancel_rejected": ({"cancel_requested"}, "pending"),
                    "reject": ({"pending", "cancel_requested"}, "rejected"),
                    "confirm": ({"pending", "cancel_requested"}, "confirmed"),
                    "settle": ({"confirmed"}, "settled"),
                }
                if action not in transitions or old_status not in transitions[action][0]:
                    raise PortfolioValidationError("当前状态不能执行该操作，请刷新查看最新结果")
                if action == "confirm":
                    self._confirm(session, order, values, now)
                elif action == "settle":
                    if order.transaction_type != "sell":
                        raise PortfolioValidationError("这里只登记赎回款实际到账，买入确认不需要到账操作")
                    settlement = self._date(values.get("settlement_date"), "到账日期")
                    if settlement < order.confirmation_date:
                        raise PortfolioValidationError("到账日期不能早于确认日期")
                    order.settlement_date = settlement
                order.status = transitions[action][1]
                order.updated_at = now
                session.flush()
                self.portfolios._rebuild_actual_holdings(session, user_id=user_id, portfolio_id=portfolio_id, now=now)
                self._event(session, order, action, old_status, order.status, reason, payload, key)
                portfolio.updated_at = now
        return {**self.get_order(user_id=user_id, portfolio_id=portfolio_id, order_id=order_id), "idempotency_replayed": replayed}

    def _confirm(self, session, order, values, now):
        """确认是唯一把待处理申请转成持仓流水的动作，不使用估算净值自动成交。"""
        trade_day = self._date(values.get("trade_date"), "成交净值日")
        confirm_day = self._date(values.get("confirmation_date"), "确认日期")
        if confirm_day < trade_day:
            raise PortfolioValidationError("确认日期不能早于成交净值日")
        submitted = self._aware(order.submitted_at)
        if trade_day < submitted.astimezone(CHINA_TIMEZONE).date():
            raise PortfolioValidationError("成交净值日不能早于申请日期")
        normalized = self.portfolios._normalize_transaction(fund_code=order.fund_code,
            transaction_type=order.transaction_type, trade_date=trade_day,
            units=values.get("units"), unit_price=values.get("unit_price"), fee=values.get("fee") or 0,
            note=order.note, submitted_at=submitted, channel=order.channel)
        self._check_confirmed_size(order, normalized["exact_values"])
        transaction = FundPortfolioTransactionModel(user_id=order.user_id,
            portfolio_id=order.portfolio_id, id=f"txn_{uuid4().hex[:16]}",
            # 同一申请在数据库中也只能生成一笔确认流水。
            idempotency_key=f"confirm:{order.id}", created_at=now, **normalized)
        session.add(transaction)
        order.transaction_id = transaction.id
        order.confirmation_date = confirm_day

    @staticmethod
    def _check_confirmed_size(order, precise):
        """确认和更正共用整笔匹配规则，避免从更正入口绕过部分成交限制。"""
        if order.transaction_type == "sell" and Decimal(precise["units"]) != Decimal(order.requested_units):
            raise PortfolioValidationError("本版只登记整笔确认，确认份额须等于申请份额；部分确认请先核对拆单")
        if order.transaction_type == "buy":
            cost = money(Decimal(precise["units"]) * Decimal(precise["unit_price"])) + Decimal(precise["fee"])
            if abs(cost - Decimal(order.requested_amount)) > MONEY:
                raise PortfolioValidationError("确认金额与申请金额不符，请核对份额、净值及费用；本版不自动处理部分确认")

    def correct_transaction(self, *, user_id, portfolio_id, transaction_id, idempotency_key, reason, replacement=None):
        """追加更正链，而非删除原流水；历史超卖或占用冲突会回滚整个更正。"""
        guard_agent_write()
        key = self.portfolios._normalize_transaction_idempotency_key(idempotency_key)
        reason = self._reason(reason)
        normalized = self.portfolios._normalize_transaction(**replacement) if replacement else None
        payload = _json_values({"transaction_id": transaction_id, "reason": reason, "replacement": normalized})
        replayed = False
        now = datetime.now(timezone.utc)
        with self.database.session() as session, session.begin():
            portfolio = self.portfolios._get_model(session, user_id=user_id, portfolio_id=portfolio_id, for_update=True)
            original = session.get(FundPortfolioTransactionModel, {"user_id": user_id, "id": transaction_id})
            if original is None or original.portfolio_id != portfolio_id:
                raise PortfolioNotFoundError("交易不存在")
            previous = session.scalar(select(PortfolioTransactionCorrectionModel).where(
                PortfolioTransactionCorrectionModel.user_id == user_id,
                PortfolioTransactionCorrectionModel.portfolio_id == portfolio_id,
                PortfolioTransactionCorrectionModel.idempotency_key == key))
            if previous:
                if previous.payload != payload:
                    raise PortfolioIdempotencyConflictError("该更正幂等键已用于其他内容")
                correction, replayed = previous, True
            else:
                if session.scalar(select(PortfolioTransactionCorrectionModel.id).where(
                    PortfolioTransactionCorrectionModel.user_id == user_id,
                    PortfolioTransactionCorrectionModel.transaction_id == transaction_id)):
                    raise PortfolioValidationError("原流水已更正，请操作更正后的有效流水")
                linked_order = session.scalar(select(PortfolioTradeOrderModel).where(
                    PortfolioTradeOrderModel.user_id == user_id, PortfolioTradeOrderModel.portfolio_id == portfolio_id,
                    PortfolioTradeOrderModel.transaction_id == transaction_id))
                if linked_order and normalized:
                    if normalized["fund_code"] != linked_order.fund_code or normalized["transaction_type"] != linked_order.transaction_type:
                        raise PortfolioValidationError("关联申请的更正不能改变基金或方向，请先冲正错误记录后重新登记")
                    if normalized["trade_date"] < self._aware(linked_order.submitted_at).astimezone(CHINA_TIMEZONE).date() or normalized["trade_date"] > linked_order.confirmation_date:
                        raise PortfolioValidationError("更正后的净值日必须位于申请日和确认日之间")
                    self._check_confirmed_size(linked_order, normalized["exact_values"])
                corrected = None
                if normalized:
                    # 记录时间是真实的更正时间；成本重放顺序单独继承最初那笔的顺序键。
                    # 不能通过回填 created_at 把今天的更正伪装成历史写入。
                    original_values = original.exact_values or {}
                    normalized["exact_values"].update(
                        ledger_created_at=original_values.get("ledger_created_at") or self._aware(original.created_at).isoformat(),
                        ledger_id=original_values.get("ledger_id") or original.id,
                    )
                    corrected = FundPortfolioTransactionModel(user_id=user_id, portfolio_id=portfolio_id,
                        id=f"txn_{uuid4().hex[:16]}", idempotency_key=f"correction:{uuid4().hex}",
                        created_at=now, **normalized)
                    session.add(corrected)
                correction = PortfolioTransactionCorrectionModel(id=f"cor_{uuid4().hex[:16]}",
                    user_id=user_id, portfolio_id=portfolio_id, transaction_id=transaction_id,
                    replacement_transaction_id=corrected.id if corrected else None,
                    idempotency_key=key, reason=reason, payload=payload, created_at=now)
                session.add(correction)
                if linked_order:
                    old_status = linked_order.status
                    linked_order.transaction_id = corrected.id if corrected else transaction_id
                    if corrected is None:
                        linked_order.status = "reversed"
                    linked_order.updated_at = now
                    self._event(session, linked_order, "correction", old_status, linked_order.status,
                        reason, {"correction_id": correction.id}, f"correction:{correction.id}")
                session.flush()
                self.portfolios._rebuild_actual_holdings(session, user_id=user_id, portfolio_id=portfolio_id, now=now)
                portfolio.updated_at = now
        return {"id": correction.id, "transaction_id": transaction_id,
            "replacement_transaction_id": correction.replacement_transaction_id,
            "reason": correction.reason, "created_at": correction.created_at,
            "idempotency_replayed": replayed}

    @staticmethod
    def _reason(value):
        value = str(value or "").strip()
        if not value or len(value) > 300:
            raise PortfolioValidationError("请填写 1-300 字的外部结果依据或更正原因")
        return value

    @staticmethod
    def _date(value, label):
        try:
            result = value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise PortfolioValidationError(f"{label}无效") from exc
        if result > datetime.now(CHINA_TIMEZONE).date():
            raise PortfolioValidationError(f"{label}不能晚于今天")
        return result

    @staticmethod
    def _aware(value):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _order(session, user_id, portfolio_id, order_id):
        order = session.get(PortfolioTradeOrderModel, {"user_id": user_id, "id": order_id})
        if order is None or order.portfolio_id != portfolio_id:
            raise PortfolioNotFoundError("申请不存在")
        return order

    def _creation_payload(self, order):
        return {"fund_code": order.fund_code, "transaction_type": order.transaction_type,
            "submitted_at": self._aware(order.submitted_at).astimezone(timezone.utc).isoformat(),
            "requested_amount": order.requested_amount, "requested_units": order.requested_units,
            "channel": order.channel, "note": order.note}

    @staticmethod
    def _event(session, order, action, old, new, reason, payload, key):
        session.add(PortfolioTradeOrderEventModel(id=f"evt_{uuid4().hex[:16]}", user_id=order.user_id,
            portfolio_id=order.portfolio_id, order_id=order.id, action=action, from_status=old,
            to_status=new, reason=reason, payload=payload, idempotency_key=key,
            created_at=datetime.now(timezone.utc)))

    def _view(self, session, order):
        result = {**self._creation_payload(order), "id": order.id, "portfolio_id": order.portfolio_id,
            "status": order.status, "transaction_id": order.transaction_id,
            "confirmation_date": order.confirmation_date, "settlement_date": order.settlement_date,
            "created_at": order.created_at, "updated_at": order.updated_at,
            "idempotency_replayed": False}
        events = session.scalars(select(PortfolioTradeOrderEventModel).where(
            PortfolioTradeOrderEventModel.user_id == order.user_id,
            PortfolioTradeOrderEventModel.portfolio_id == order.portfolio_id,
            PortfolioTradeOrderEventModel.order_id == order.id
        ).order_by(PortfolioTradeOrderEventModel.created_at, PortfolioTradeOrderEventModel.id))
        result["events"] = [{"id": event.id, "action": event.action, "from_status": event.from_status,
            "to_status": event.to_status, "reason": event.reason, "created_at": event.created_at} for event in events]
        result["confirmed_amount"] = None
        if order.transaction_id and order.status != "reversed":
            transaction = session.get(FundPortfolioTransactionModel, {"user_id": order.user_id, "id": order.transaction_id})
            if transaction:
                # 成交关联也按真实流水归属检查，防止跨组合引用泄露成交金额。
                guard_agent_portfolio(transaction.user_id, transaction.portfolio_id)
                units, price, fee = transaction_values(transaction)
                result["confirmed_amount"] = str(money(units * price) + (fee if order.transaction_type == "buy" else -fee))
        return result


portfolio_trade_service = PortfolioTradeService()

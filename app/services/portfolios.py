"""FundScope 组合、真实持仓和交易流水持久化服务。

旧版 allocation 组合只保存目标权重；新版 actual 组合以交易流水为事实源。每次新增或
更正交易后，Service 按交易日期重放有效买卖，使用移动平均成本重建剩余份额、成本和当前
市值权重。风险研判继续读取同一个 ``weight_pct``，但实际组合的权重已由最新净值推导。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.database import Database, runtime_database
from app.models import (
    FundPortfolioHoldingModel,
    FundPortfolioModel,
    FundPortfolioTransactionModel,
    PortfolioTradeOrderModel,
    PortfolioTransactionCorrectionModel,
)
from app.services.agent_authorization import (
    allowed_agent_portfolio_ids, guard_agent_portfolio, guard_agent_write,
)
from app.services.portfolio_ledger_values import (
    MONEY, UNITS, ZERO, exact_values, ledger_decimal, money, transaction_values,
    replay_transactions, ledger_order_key,
)
from app.services.funds import FundCatalogError, FundNotFoundError, fund_catalog_service
from app.services.fund_transaction_timing import CHINA_TIMEZONE, preview_transaction_timing
from app.services.errors import ServiceError


POSITION_EPSILON = 1e-8
TRANSACTION_IDEMPOTENCY_KEY_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,99}$"
)


class PortfolioError(ServiceError):
    """组合存储或基金目录无法完成当前操作。"""


class PortfolioNotFoundError(PortfolioError):
    """组合不存在，或者不属于当前用户。"""

    code = "resource_not_found"


class PortfolioValidationError(PortfolioError):
    """持仓或交易不满足明确业务规则。"""


class PortfolioIdempotencyConflictError(PortfolioError):
    """同一个幂等键被用于两笔内容不同的交易。"""


class PortfolioService:
    """通过 SQLAlchemy 保存组合，并从交易流水计算当前持仓与盈亏。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def create(
        self,
        *,
        user_id: str,
        name: str,
        holdings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """创建目标权重组合，或用初始买入记录创建真实持仓组合。"""

        guard_agent_write()
        normalized_name = self._normalize_name(name)
        normalized_holdings = self._normalize_holdings(holdings)
        now = datetime.now(timezone.utc)
        portfolio = FundPortfolioModel(
            user_id=user_id,
            id=f"portfolio_{uuid4().hex[:16]}",
            name=normalized_name,
            created_at=now,
            updated_at=now,
        )

        with self.database.session() as session, session.begin():
            session.add(portfolio)
            session.add_all(
                self._holding_models(
                    user_id=user_id,
                    portfolio_id=portfolio.id,
                    holdings=normalized_holdings,
                    now=now,
                )
            )
            if normalized_holdings[0]["position_mode"] == "actual":
                session.add_all(
                    self._opening_transactions(
                        user_id=user_id,
                        portfolio_id=portfolio.id,
                        holdings=normalized_holdings,
                        now=now,
                    )
                )

        return self.get(user_id=user_id, portfolio_id=portfolio.id)

    def list(self, *, user_id: str) -> list[dict[str, Any]]:
        """按最近修改时间返回当前用户的组合。"""

        allowed_ids = allowed_agent_portfolio_ids(user_id)
        statement = select(FundPortfolioModel).where(FundPortfolioModel.user_id == user_id)
        if allowed_ids is not None:
            # 任务授权只缩小查询范围；没有任务作用域的人类请求仍读取全部组合。
            statement = statement.where(FundPortfolioModel.id.in_(allowed_ids))
        with self.database.session() as session:
            portfolios = list(
                session.scalars(
                    statement.order_by(FundPortfolioModel.updated_at.desc())
                )
            )
        return [
            self.get(user_id=user_id, portfolio_id=portfolio.id)
            for portfolio in portfolios
        ]

    def get(self, *, user_id: str, portfolio_id: str) -> dict[str, Any]:
        """读取组合，并补充最新净值、市值、成本和盈亏。"""

        with self.database.session() as session:
            portfolio = self._get_model(
                session,
                user_id=user_id,
                portfolio_id=portfolio_id,
            )
            holdings = list(
                session.scalars(
                    select(FundPortfolioHoldingModel)
                    .where(
                        FundPortfolioHoldingModel.user_id == user_id,
                        FundPortfolioHoldingModel.portfolio_id == portfolio_id,
                    )
                    .order_by(FundPortfolioHoldingModel.weight_pct.desc())
                )
            )
            transactions = self._transaction_models(
                session,
                user_id=user_id,
                portfolio_id=portfolio_id,
            )
            orders = list(session.scalars(select(PortfolioTradeOrderModel).where(
                PortfolioTradeOrderModel.user_id == user_id,
                PortfolioTradeOrderModel.portfolio_id == portfolio_id,
            )))
            # 所有有效流水被冲正后，账本仍保留历史，不能退回权重编辑模式。
            has_ledger = bool(session.scalar(select(FundPortfolioTransactionModel.id).where(
                FundPortfolioTransactionModel.user_id == user_id,
                FundPortfolioTransactionModel.portfolio_id == portfolio_id,
            ).limit(1)))
        # 使用数据库实际归属检查，不把调用参数当成资源授权依据。
        guard_agent_portfolio(portfolio.user_id, portfolio.id)
        result = self._to_dict(
            portfolio=portfolio,
            holdings=holdings,
            transactions=transactions,
        )
        reserved = self._reserved_units(orders)
        for holding in result["holdings"]:
            if holding["position_mode"] == "actual":
                amount = reserved.get(holding["fund_code"], ZERO)
                holding["reserved_units"] = float(amount)
                holding["available_units"] = float(Decimal(str(holding["units"])) - amount)
        if has_ledger or orders:
            result["position_mode"] = "actual"
            # 全部冲正后仍是一个有历史的空账本，金额为零而不是退回权重模式。
            for field in ("total_cost_amount", "total_market_value", "unrealized_pnl_amount", "total_pnl_amount"):
                if result[field] is None:
                    result[field] = 0.0
        by_id = {item.id: item for item in transactions}
        unsettled = ZERO
        for order in orders:
            if order.status == "confirmed" and order.transaction_type == "sell" and order.transaction_id in by_id:
                units, price, fee = transaction_values(by_id[order.transaction_id])
                unsettled += money(units * price) - fee
        result["pending_order_count"] = sum(item.status in {"pending", "cancel_requested"} for item in orders)
        result["pending_buy_amount"] = str(sum((Decimal(item.requested_amount or "0") for item in orders
            if item.transaction_type == "buy" and item.status in {"pending", "cancel_requested"}), ZERO))
        result["unsettled_redemption_amount"] = str(money(unsettled))
        return result

    def update(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        name: str,
        holdings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """整体替换旧版权重；有真实流水的组合必须通过交易接口调整。"""

        guard_agent_write()
        normalized_name = self._normalize_name(name)
        normalized_holdings = self._normalize_holdings(holdings)
        now = datetime.now(timezone.utc)

        with self.database.session() as session, session.begin():
            portfolio = self._get_model(
                session,
                user_id=user_id,
                portfolio_id=portfolio_id,
                for_update=True,
            )
            has_transactions = bool(
                session.scalar(
                    select(FundPortfolioTransactionModel.id)
                    .where(
                        FundPortfolioTransactionModel.user_id == user_id,
                        FundPortfolioTransactionModel.portfolio_id == portfolio_id,
                    )
                    .limit(1)
                )
            )
            has_orders = session.scalar(select(PortfolioTradeOrderModel.id).where(
                PortfolioTradeOrderModel.user_id == user_id,
                PortfolioTradeOrderModel.portfolio_id == portfolio_id,
            ).limit(1))
            if has_transactions or has_orders:
                raise PortfolioValidationError(
                    "真实持仓不能整体覆盖，请通过买入/卖出流水调整份额"
                )

            portfolio.name = normalized_name
            portfolio.updated_at = now
            session.execute(
                delete(FundPortfolioHoldingModel).where(
                    FundPortfolioHoldingModel.user_id == user_id,
                    FundPortfolioHoldingModel.portfolio_id == portfolio_id,
                )
            )
            session.add_all(
                self._holding_models(
                    user_id=user_id,
                    portfolio_id=portfolio_id,
                    holdings=normalized_holdings,
                    now=now,
                )
            )
            if normalized_holdings[0]["position_mode"] == "actual":
                session.add_all(
                    self._opening_transactions(
                        user_id=user_id,
                        portfolio_id=portfolio_id,
                        holdings=normalized_holdings,
                        now=now,
                    )
                )
        return self.get(user_id=user_id, portfolio_id=portfolio_id)

    def rename(self, *, user_id: str, portfolio_id: str, name: str) -> dict[str, Any]:
        """重命名不会触碰持仓或交易历史。"""

        guard_agent_write()
        with self.database.session() as session, session.begin():
            portfolio = self._get_model(
                session,
                user_id=user_id,
                portfolio_id=portfolio_id,
                for_update=True,
            )
            portfolio.name = self._normalize_name(name)
            portfolio.updated_at = datetime.now(timezone.utc)
        return self.get(user_id=user_id, portfolio_id=portfolio_id)

    def delete(self, *, user_id: str, portfolio_id: str) -> None:
        """只有没有记账历史的组合可删除，避免绕过单笔流水的不可删除约束。"""

        guard_agent_write()
        with self.database.session() as session, session.begin():
            portfolio = self._get_model(
                session,
                user_id=user_id,
                portfolio_id=portfolio_id,
                for_update=True,
            )
            if session.scalar(select(FundPortfolioTransactionModel.id).where(
                    FundPortfolioTransactionModel.user_id == user_id,
                    FundPortfolioTransactionModel.portfolio_id == portfolio_id,
                ).limit(1)) or session.scalar(select(PortfolioTradeOrderModel.id).where(
                    PortfolioTradeOrderModel.user_id == user_id,
                    PortfolioTradeOrderModel.portfolio_id == portfolio_id,
                ).limit(1)):
                raise PortfolioValidationError("已有记账或申请记录的组合不能删除，请保留历史并通过更正流程处理错误")
            session.execute(
                delete(FundPortfolioHoldingModel).where(
                    FundPortfolioHoldingModel.user_id == user_id,
                    FundPortfolioHoldingModel.portfolio_id == portfolio_id,
                )
            )
            session.delete(portfolio)

    def add_transaction(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        fund_code: str,
        transaction_type: str,
        trade_date: date,
        units: float,
        unit_price: float,
        idempotency_key: str,
        fee: float = 0,
        note: str = "",
        submitted_at: datetime | None = None,
        channel: str = "off_exchange",
    ) -> dict[str, Any]:
        """保存交易并在同一事务里重建持仓，重复请求只返回首次结果。"""

        guard_agent_write()
        normalized = self._normalize_transaction(
            fund_code=fund_code,
            transaction_type=transaction_type,
            trade_date=trade_date,
            units=units,
            unit_price=unit_price,
            fee=fee,
            note=note,
            submitted_at=submitted_at,
            channel=channel,
        )
        normalized_idempotency_key = self._normalize_transaction_idempotency_key(
            idempotency_key
        )
        now = datetime.now(timezone.utc)
        transaction = FundPortfolioTransactionModel(
            user_id=user_id,
            id=f"txn_{uuid4().hex[:16]}",
            portfolio_id=portfolio_id,
            idempotency_key=normalized_idempotency_key,
            created_at=now,
            **normalized,
        )
        transaction_id = transaction.id
        replayed = False

        try:
            with self.database.session() as session, session.begin():
                portfolio = self._get_model(
                    session,
                    user_id=user_id,
                    portfolio_id=portfolio_id,
                    for_update=True,
                )
                existing = self._transaction_by_idempotency_key(
                    session,
                    user_id=user_id,
                    portfolio_id=portfolio_id,
                    idempotency_key=normalized_idempotency_key,
                )
                if existing is not None:
                    # 相同 key 只有在业务字段也完全一致时才算网络重试。若内容变化，
                    # 返回原交易会掩盖客户端 bug，因此必须明确报 409。
                    self._assert_same_idempotent_transaction(
                        existing=existing,
                        normalized=normalized,
                    )
                    transaction_id = existing.id
                    replayed = True
                else:
                    existing_modes = set(
                        session.scalars(
                            select(FundPortfolioHoldingModel.position_mode).where(
                                FundPortfolioHoldingModel.user_id == user_id,
                                FundPortfolioHoldingModel.portfolio_id == portfolio_id,
                            )
                        )
                    )
                    if "allocation" in existing_modes:
                        raise PortfolioValidationError(
                            "旧版权重组合不能追加交易，请先新建真实持仓组合"
                        )
                    session.add(transaction)
                    # flush 让唯一索引在重建持仓前就完成仲裁；失败时整个事务回滚，
                    # 不会留下“交易没写成但持仓已经变化”的半成品。
                    session.flush()
                    self._rebuild_actual_holdings(
                        session,
                        user_id=user_id,
                        portfolio_id=portfolio_id,
                        now=now,
                    )
                    portfolio.updated_at = now
        except IntegrityError as exc:
            # PostgreSQL 的行锁通常会让第二个请求在上面的查询中直接看到原交易；
            # SQLite 或极端并发仍可能同时走到 INSERT，由唯一索引在这里最终裁决。
            with self.database.session() as session:
                existing = self._transaction_by_idempotency_key(
                    session,
                    user_id=user_id,
                    portfolio_id=portfolio_id,
                    idempotency_key=normalized_idempotency_key,
                )
            if existing is None:
                raise PortfolioError("交易写入失败，数据库唯一约束冲突") from exc
            self._assert_same_idempotent_transaction(
                existing=existing,
                normalized=normalized,
            )
            transaction_id = existing.id
            replayed = True

        result = next(
            item
            for item in self.list_transactions(
                user_id=user_id,
                portfolio_id=portfolio_id,
            )
            if item["id"] == transaction_id
        )
        result["idempotency_replayed"] = replayed
        return result

    def list_transactions(
        self,
        *,
        user_id: str,
        portfolio_id: str,
    ) -> list[dict[str, Any]]:
        """返回按交易日期倒序展示、但按正序计算成本的完整流水。"""

        with self.database.session() as session:
            portfolio = self._get_model(
                session,
                user_id=user_id,
                portfolio_id=portfolio_id,
            )
            transactions = self._transaction_models(
                session,
                user_id=user_id,
                portfolio_id=portfolio_id,
                include_inactive=True,
            )
            corrections = {item.transaction_id: item for item in session.scalars(
                select(PortfolioTransactionCorrectionModel).where(
                    PortfolioTransactionCorrectionModel.user_id == user_id,
                    PortfolioTransactionCorrectionModel.portfolio_id == portfolio_id,
                ))}
        guard_agent_portfolio(portfolio.user_id, portfolio.id)
        _, annotations = self._replay_transactions([item for item in transactions if item.id not in corrections])
        return [
            {**self._transaction_to_dict(
                transaction,
                annotations.get(transaction.id, {"running_units": ZERO, "realized_pnl_amount": None}),
            ), "effective": transaction.id not in corrections,
                "status": ("corrected" if corrections[transaction.id].replacement_transaction_id else "reversed")
                    if transaction.id in corrections else "posted",
                "correction": ({"id": corrections[transaction.id].id,
                    "reason": corrections[transaction.id].reason,
                    "replacement_transaction_id": corrections[transaction.id].replacement_transaction_id,
                    "created_at": corrections[transaction.id].created_at}
                    if transaction.id in corrections else None)}
            for transaction in reversed(transactions)
        ]

    def delete_transaction(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        transaction_id: str,
    ) -> None:
        """旧入口拒绝物理删除，客户端必须提交有原因的更正/冲正。"""
        guard_agent_write()
        with self.database.session() as session:
            self._get_model(session, user_id=user_id, portfolio_id=portfolio_id)
            transaction = session.get(FundPortfolioTransactionModel, {"user_id": user_id, "id": transaction_id})
            if transaction is None or transaction.portfolio_id != portfolio_id:
                raise PortfolioNotFoundError("交易不存在")
        raise PortfolioValidationError("已入账流水不能删除，请使用带原因的更正或冲正；这不会撤销外部真实交易")

    def _normalize_holdings(
        self,
        holdings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """识别目标权重或真实持仓模式，并拒绝两种模式混用。"""

        if not holdings:
            raise PortfolioValidationError("组合至少需要一只基金")

        actual_flags = [
            any(
                holding.get(field) is not None
                for field in ("units", "cost_amount", "purchase_date")
            )
            for holding in holdings
        ]
        if any(actual_flags) and not all(actual_flags):
            raise PortfolioValidationError("同一组合不能混用目标权重和真实持仓")
        actual_mode = all(actual_flags)
        if actual_mode and any(
            holding.get("weight_pct") is not None for holding in holdings
        ):
            raise PortfolioValidationError("真实持仓不需要填写目标权重")

        normalized: list[dict[str, Any]] = []
        seen_codes: set[str] = set()
        for holding in holdings:
            fund_code = str(holding.get("fund_code") or "").strip()
            if fund_code in seen_codes:
                raise PortfolioValidationError(f"基金 {fund_code} 在组合中重复出现")
            self._ensure_fund_exists(fund_code)
            seen_codes.add(fund_code)

            if actual_mode:
                try:
                    units = ledger_decimal(holding.get("units"), UNITS, positive=True, label="份额")
                    cost_amount = ledger_decimal(holding.get("cost_amount"), MONEY, positive=True, label="成本")
                    purchase_date = self._as_date(holding.get("purchase_date"))
                except (TypeError, ValueError) as exc:
                    raise PortfolioValidationError(
                        "真实持仓需要有效的份额、成本金额和买入日期"
                    ) from exc
                if units <= 0 or cost_amount <= 0:
                    raise PortfolioValidationError("持有份额和成本金额必须大于 0")
                if purchase_date > date.today():
                    raise PortfolioValidationError("买入日期不能晚于今天")
                normalized.append(
                    {
                        "fund_code": fund_code,
                        "position_mode": "actual",
                        "units": units,
                        "cost_amount": cost_amount,
                        "average_cost_nav": round(cost_amount / units, 6),
                        "first_purchase_date": purchase_date,
                        "weight_pct": 0.0,
                    }
                )
                continue

            try:
                weight_pct = round(float(holding.get("weight_pct")), 2)
            except (TypeError, ValueError) as exc:
                raise PortfolioValidationError("基金权重必须是有效数字") from exc
            if weight_pct <= 0 or weight_pct > 100:
                raise PortfolioValidationError("单只基金权重必须大于 0 且不超过 100%")
            normalized.append(
                {
                    "fund_code": fund_code,
                    "position_mode": "allocation",
                    "weight_pct": weight_pct,
                    "units": None,
                    "cost_amount": None,
                    "average_cost_nav": None,
                    "first_purchase_date": None,
                }
            )

        if actual_mode:
            self._apply_current_value_weights(normalized)
        else:
            total_weight = round(sum(item["weight_pct"] for item in normalized), 2)
            if abs(total_weight - 100.0) > 0.01:
                raise PortfolioValidationError(
                    f"组合权重合计必须为 100%，当前为 {total_weight:.2f}%"
                )
        return normalized

    def _normalize_transaction(self, **values: Any) -> dict[str, Any]:
        fund_code = str(values["fund_code"] or "").strip()
        self._ensure_fund_exists(fund_code)
        transaction_type = str(values["transaction_type"] or "").strip().lower()
        if transaction_type not in {"buy", "sell"}:
            raise PortfolioValidationError("交易类型只能是 buy 或 sell")
        try:
            trade_date = self._as_date(values["trade_date"])
            precise = exact_values(values["units"], values["unit_price"], values.get("fee") or 0)
            units, unit_price, fee = (float(precise[key]) for key in ("units", "unit_price", "fee"))
        except (TypeError, ValueError) as exc:
            raise PortfolioValidationError("交易份额、净值或费用格式不正确") from exc
        if trade_date > datetime.now(CHINA_TIMEZONE).date():
            raise PortfolioValidationError("交易日期不能晚于今天")
        if units <= 0 or unit_price <= 0 or fee < 0:
            raise PortfolioValidationError("交易份额和净值必须大于 0，费用不能为负")
        if transaction_type == "sell" and Decimal(precise["fee"]) > money(Decimal(precise["units"]) * Decimal(precise["unit_price"])):
            raise PortfolioValidationError("卖出手续费不能超过成交金额")
        timing = None
        submitted_at = values.get("submitted_at")
        if submitted_at is not None:
            # 成交回单仍是账本事实；时间估算另存，不自动创建未来持仓或修改成交价。
            if not isinstance(submitted_at, datetime) or submitted_at.utcoffset() is None:
                raise PortfolioValidationError("下单时间必须包含时区")
            if submitted_at > datetime.now(timezone.utc):
                raise PortfolioValidationError("未来下单可预览，但不能登记为已确认成交")
            if trade_date < submitted_at.astimezone(CHINA_TIMEZONE).date():
                raise PortfolioValidationError("成交净值日不能早于下单日期")
            try:
                timing = preview_transaction_timing(
                    fund_code=fund_code, submitted_at=submitted_at,
                    transaction_type=transaction_type, channel=values.get("channel", "off_exchange"),
                )
            except ValueError as exc:
                raise PortfolioValidationError(str(exc)) from exc
            timing["recorded_nav_date"] = trade_date.isoformat()
            if timing["nav_date"] and timing["nav_date"] != trade_date.isoformat():
                timing["warnings"].append("已记录净值日与通用估算不同，账本按您填写的实际成交回单计算。")
        return {
            "fund_code": fund_code,
            "transaction_type": transaction_type,
            "trade_date": trade_date,
            "units": units,
            "unit_price": unit_price,
            "fee": fee,
            "exact_values": precise,
            "note": " ".join(str(values.get("note") or "").split())[:200],
            "timing": timing,
        }

    @staticmethod
    def _normalize_transaction_idempotency_key(value: str) -> str:
        """Service 也独立校验 key，避免只有 FastAPI/Pydantic 调用时才安全。"""

        normalized = str(value or "").strip()
        if not TRANSACTION_IDEMPOTENCY_KEY_PATTERN.fullmatch(normalized):
            raise PortfolioValidationError(
                "idempotency_key 必须为 8-100 位字母、数字、点、冒号、下划线或连字符"
            )
        return normalized

    @staticmethod
    def _transaction_by_idempotency_key(
        session: Any,
        *,
        user_id: str,
        portfolio_id: str,
        idempotency_key: str,
    ) -> FundPortfolioTransactionModel | None:
        """幂等键只在当前用户和当前组合内有效，不跨租户查找。"""

        return session.scalar(
            select(FundPortfolioTransactionModel).where(
                FundPortfolioTransactionModel.user_id == user_id,
                FundPortfolioTransactionModel.portfolio_id == portfolio_id,
                FundPortfolioTransactionModel.idempotency_key == idempotency_key,
            )
        )

    @staticmethod
    def _assert_same_idempotent_transaction(
        *,
        existing: FundPortfolioTransactionModel,
        normalized: dict[str, Any],
    ) -> None:
        """区分“同一请求重试”和“错误复用 key 的另一笔交易”。"""

        existing_payload = {
            "fund_code": existing.fund_code,
            "transaction_type": existing.transaction_type,
            "trade_date": existing.trade_date,
            "units": round(float(existing.units), 4),
            "unit_price": round(float(existing.unit_price), 6),
            "fee": round(float(existing.fee), 2),
            "note": existing.note,
            "exact_values": existing.exact_values or exact_values(existing.units, existing.unit_price, existing.fee),
        }
        # 推算日期可能随日历版本更新；幂等性只比较用户原始输入，不能把更新后的
        # 推算结果视为另一笔交易，也不能覆盖首次保存的时间快照。
        def timing_input(value):
            return (value["submitted_at"], value["channel"]) if value else None

        existing_payload["timing"] = timing_input(existing.timing)
        requested_payload = {**normalized, "timing": timing_input(normalized["timing"])}
        if existing_payload != requested_payload:
            raise PortfolioIdempotencyConflictError(
                "该 idempotency_key 已用于另一笔交易，请为新交易生成新的 key"
            )

    def _rebuild_actual_holdings(
        self,
        session: Any,
        *,
        user_id: str,
        portfolio_id: str,
        now: datetime,
    ) -> None:
        transactions = self._transaction_models(
            session,
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
        states, _ = self._replay_transactions(transactions)
        active_states = {
            fund_code: state
            for fund_code, state in states.items()
            if state["units"] > POSITION_EPSILON
        }
        orders = list(session.scalars(select(PortfolioTradeOrderModel).where(
            PortfolioTradeOrderModel.user_id == user_id,
            PortfolioTradeOrderModel.portfolio_id == portfolio_id,
        )))
        # 申请占用只影响可用份额。任何记账/更正都不能透支其他未决申请。
        for code, reserved in self._reserved_units(orders).items():
            if reserved > states.get(code, {}).get("units", ZERO):
                raise PortfolioValidationError(
                    f"{code} 未决赎回合计 {reserved:.4f} 份，超过变更后持有的 "
                    f"{states.get(code, {}).get('units', ZERO):.4f} 份，请减少申请或先核对未决记录"
                )

        session.execute(
            delete(FundPortfolioHoldingModel).where(
                FundPortfolioHoldingModel.user_id == user_id,
                FundPortfolioHoldingModel.portfolio_id == portfolio_id,
            )
        )
        holdings = []
        for fund_code, state in active_states.items():
            holdings.append(
                {
                    "fund_code": fund_code,
                    "position_mode": "actual",
                    "units": state["units"],
                    "cost_amount": state["cost_amount"],
                    "average_cost_nav": state["cost_amount"] / state["units"],
                    "first_purchase_date": state["first_purchase_date"],
                    "weight_pct": 0.0,
                }
            )
        if holdings:
            self._apply_current_value_weights(holdings)
            session.add_all(
                self._holding_models(
                    user_id=user_id,
                    portfolio_id=portfolio_id,
                    holdings=holdings,
                    now=now,
                )
            )

    def _replay_transactions(
        self,
        transactions: list[FundPortfolioTransactionModel],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, float | None]]]:
        """按移动平均成本重放流水，并给每笔交易生成运行后状态。"""

        return replay_transactions(transactions, error_type=PortfolioValidationError)

    def _apply_current_value_weights(self, holdings: list[dict[str, Any]]) -> None:
        values = []
        for holding in holdings:
            detail = fund_catalog_service.get_fund(holding["fund_code"])
            values.append(float(holding["units"]) * float(detail["latest_nav"]))
        total_value = sum(values)
        if total_value <= 0:
            raise PortfolioValidationError("持仓当前市值必须大于 0")
        for holding, current_value in zip(holdings, values):
            holding["weight_pct"] = round(current_value / total_value * 100, 6)

    @staticmethod
    def _holding_models(
        *,
        user_id: str,
        portfolio_id: str,
        holdings: list[dict[str, Any]],
        now: datetime,
    ) -> list[FundPortfolioHoldingModel]:
        return [
            FundPortfolioHoldingModel(
                user_id=user_id,
                portfolio_id=portfolio_id,
                fund_code=item["fund_code"],
                weight_pct=item["weight_pct"],
                position_mode=item["position_mode"],
                units=item["units"],
                cost_amount=item["cost_amount"],
                average_cost_nav=item["average_cost_nav"],
                first_purchase_date=item["first_purchase_date"],
                created_at=now,
                updated_at=now,
            )
            for item in holdings
        ]

    @staticmethod
    def _opening_transactions(
        *,
        user_id: str,
        portfolio_id: str,
        holdings: list[dict[str, Any]],
        now: datetime,
    ) -> list[FundPortfolioTransactionModel]:
        return [
            FundPortfolioTransactionModel(
                user_id=user_id,
                id=f"txn_{uuid4().hex[:16]}",
                portfolio_id=portfolio_id,
                fund_code=item["fund_code"],
                transaction_type="buy",
                trade_date=item["first_purchase_date"],
                units=item["units"],
                unit_price=round(item["cost_amount"] / item["units"], 6),
                fee=0.0,
                note="初始持仓",
                # 初始持仓与组合主体在同一事务创建，不来自可独立重试的交易 API。
                idempotency_key=None,
                created_at=now,
                exact_values={**exact_values(item["units"], item["cost_amount"] / item["units"]),
                    "opening_cost": str(money(item["cost_amount"]))},
            )
            for item in holdings
        ]

    @staticmethod
    def _transaction_models(
        session: Any,
        *,
        user_id: str,
        portfolio_id: str,
        include_inactive: bool = False,
    ) -> list[FundPortfolioTransactionModel]:
        transactions = list(
            session.scalars(
                select(FundPortfolioTransactionModel)
                .where(
                    FundPortfolioTransactionModel.user_id == user_id,
                    FundPortfolioTransactionModel.portfolio_id == portfolio_id,
                    True if include_inactive else ~select(PortfolioTransactionCorrectionModel.id).where(
                        PortfolioTransactionCorrectionModel.user_id == user_id,
                        PortfolioTransactionCorrectionModel.transaction_id == FundPortfolioTransactionModel.id,
                    ).exists(),
                )
                .order_by(
                    FundPortfolioTransactionModel.trade_date.asc(),
                    FundPortfolioTransactionModel.created_at.asc(),
                    FundPortfolioTransactionModel.id.asc(),
                )
            )
        )
        # 更正链保留原业务顺序，同时保留真实写入时间；同日先买后卖不能因更正而倒置。
        transactions.sort(key=ledger_order_key)
        return transactions

    def _to_dict(
        self,
        *,
        portfolio: FundPortfolioModel,
        holdings: list[FundPortfolioHoldingModel],
        transactions: list[FundPortfolioTransactionModel],
    ) -> dict[str, Any]:
        states, _ = self._replay_transactions(transactions)
        enriched_holdings: list[dict[str, Any]] = []
        for holding in holdings:
            detail = fund_catalog_service.get_fund(holding.fund_code)
            profile = detail["profile"]
            actual = holding.position_mode == "actual"
            current_value = (
                float(holding.units or 0) * float(detail["latest_nav"])
                if actual
                else None
            )
            cost_amount = float(holding.cost_amount or 0) if actual else None
            unrealized = (
                float(current_value) - float(cost_amount)
                if actual and current_value is not None and cost_amount is not None
                else None
            )
            fund_state = states.get(holding.fund_code, {})
            realized = float(fund_state.get("realized_pnl_amount") or 0)
            enriched_holdings.append(
                {
                    "fund_code": holding.fund_code,
                    "short_name": profile["short_name"],
                    "fund_type": profile["fund_type"],
                    "sample_role": profile["sample_role"],
                    "weight_pct": round(holding.weight_pct, 2),
                    "position_mode": holding.position_mode,
                    "units": round(float(holding.units), 4) if actual else None,
                    "cost_amount": round(float(cost_amount), 2) if actual else None,
                    "average_cost_nav": (
                        round(float(holding.average_cost_nav), 4)
                        if actual
                        else None
                    ),
                    "first_purchase_date": holding.first_purchase_date,
                    "latest_nav": detail["latest_nav"],
                    "latest_nav_date": detail["latest_nav_date"],
                    "current_value": round(float(current_value), 2) if actual else None,
                    "unrealized_pnl_amount": (
                        round(float(unrealized), 2) if actual else None
                    ),
                    "unrealized_pnl_pct": (
                        round(float(unrealized) / float(cost_amount) * 100, 2)
                        if actual and cost_amount and unrealized is not None
                        else None
                    ),
                    "realized_pnl_amount": round(realized, 2),
                    "total_pnl_amount": (
                        round(float(unrealized) + realized, 2)
                        if actual and unrealized is not None
                        else None
                    ),
                }
            )

        # 交易后保存的权重只是当时快照。净值每日变化，因此读取时必须重新按当前市值
        # 计算；组合 assessment 和 Agent Tool 也会通过这里拿到相同的最新权重。
        actual_market_value = sum(
            float(item["current_value"] or 0)
            for item in enriched_holdings
            if item["position_mode"] == "actual"
        )
        if actual_market_value > 0:
            for item in enriched_holdings:
                if item["position_mode"] == "actual":
                    item["weight_pct"] = round(
                        float(item["current_value"] or 0)
                        / actual_market_value
                        * 100,
                        2,
                    )

        position_mode = (
            "actual"
            if any(item["position_mode"] == "actual" for item in enriched_holdings)
            or bool(transactions)
            else "allocation" if enriched_holdings else "empty"
        )
        total_cost = (
            sum(float(item["cost_amount"] or 0) for item in enriched_holdings)
            if position_mode == "actual"
            else None
        )
        total_market_value = (
            sum(float(item["current_value"] or 0) for item in enriched_holdings)
            if position_mode == "actual"
            else None
        )
        unrealized = (
            float(total_market_value) - float(total_cost)
            if total_cost is not None and total_market_value is not None
            else None
        )
        realized = sum(
            float(state.get("realized_pnl_amount") or 0)
            for state in states.values()
        )
        cumulative_buy_cost = sum(
            float(state.get("cumulative_buy_cost") or 0)
            for state in states.values()
        )
        total_pnl = (
            float(unrealized) + realized
            if unrealized is not None
            else None
        )

        return {
            "id": portfolio.id,
            "user_id": portfolio.user_id,
            "name": portfolio.name,
            "holding_count": len(enriched_holdings),
            "total_weight_pct": round(
                sum(item["weight_pct"] for item in enriched_holdings),
                2,
            ),
            "position_mode": position_mode,
            "total_cost_amount": round(total_cost, 2) if total_cost is not None else None,
            "total_market_value": (
                round(total_market_value, 2)
                if total_market_value is not None
                else None
            ),
            "unrealized_pnl_amount": (
                round(unrealized, 2) if unrealized is not None else None
            ),
            "realized_pnl_amount": round(realized, 2),
            "total_pnl_amount": round(total_pnl, 2) if total_pnl is not None else None,
            "total_return_pct": (
                round(total_pnl / cumulative_buy_cost * 100, 2)
                if total_pnl is not None and cumulative_buy_cost > 0
                else None
            ),
            "holdings": enriched_holdings,
            "created_at": portfolio.created_at,
            "updated_at": portfolio.updated_at,
        }

    def _transaction_to_dict(
        self,
        transaction: FundPortfolioTransactionModel,
        annotation: dict[str, float | None],
    ) -> dict[str, Any]:
        detail = fund_catalog_service.get_fund(transaction.fund_code)
        units, unit_price, fee = transaction_values(transaction)
        gross = money(units * unit_price)
        if (transaction.exact_values or {}).get("opening_cost"):
            gross = Decimal(transaction.exact_values["opening_cost"])
        cash_flow = (
            -(gross + fee)
            if transaction.transaction_type == "buy"
            else gross - fee
        )
        return {
            "id": transaction.id,
            "idempotency_key": transaction.idempotency_key,
            "idempotency_replayed": False,
            "portfolio_id": transaction.portfolio_id,
            "fund_code": transaction.fund_code,
            "short_name": detail["profile"]["short_name"],
            "transaction_type": transaction.transaction_type,
            "trade_date": transaction.trade_date,
            "units": float(units),
            "unit_price": float(unit_price),
            "gross_amount": float(gross),
            "fee": float(fee),
            "cash_flow": float(cash_flow),
            "exact_values": transaction.exact_values or {"units": str(units), "unit_price": str(unit_price), "fee": str(fee)},
            "realized_pnl_amount": (
                round(float(annotation["realized_pnl_amount"]), 2)
                if annotation["realized_pnl_amount"] is not None
                else None
            ),
            "running_units": round(float(annotation["running_units"] or 0), 4),
            "note": transaction.note,
            "created_at": transaction.created_at,
            "timing": transaction.timing,
        }

    def _ensure_fund_exists(self, fund_code: str) -> None:
        try:
            fund_catalog_service.get_fund(fund_code)
        except FundNotFoundError as exc:
            raise PortfolioValidationError(str(exc)) from exc
        except FundCatalogError as exc:
            raise PortfolioError(str(exc)) from exc

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = " ".join(str(name or "").split())
        if not normalized:
            raise PortfolioValidationError("组合名称不能为空")
        if len(normalized) > 80:
            raise PortfolioValidationError("组合名称不能超过 80 个字符")
        return normalized

    @staticmethod
    def _get_model(
        session: Any,
        *,
        user_id: str,
        portfolio_id: str,
        for_update: bool = False,
    ) -> FundPortfolioModel:
        if for_update:
            # 必须先拒绝 Agent，再取得 SQLite 写锁；领域 guard 不另写审计。
            guard_agent_write()
            # SQLite 不支持行锁。在首次查询前取得写锁，与 PostgreSQL 的组合行锁
            # 一样串行化同一本账上的检查和写入，避免不同幂等键并发超卖。
            if session.bind.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            # PostgreSQL 多 Worker 同时记账时，组合主记录充当这本账的互斥锁。
            # SQLite 会忽略 FOR UPDATE，但本地单文件模式仍保持同一套代码路径。
            portfolio = session.scalar(
                select(FundPortfolioModel)
                .where(
                    FundPortfolioModel.user_id == user_id,
                    FundPortfolioModel.id == portfolio_id,
                )
                .with_for_update()
            )
        else:
            portfolio = session.get(
                FundPortfolioModel,
                {"user_id": user_id, "id": portfolio_id},
            )
        if portfolio is None:
            raise PortfolioNotFoundError(f"组合不存在：{portfolio_id}")
        return portfolio

    @staticmethod
    def _reserved_units(orders) -> dict[str, Decimal]:
        reserved: dict[str, Decimal] = {}
        for order in orders:
            if order.transaction_type == "sell" and order.status in {"pending", "cancel_requested"}:
                reserved[order.fund_code] = reserved.get(order.fund_code, ZERO) + Decimal(order.requested_units)
        return reserved

    @staticmethod
    def _as_date(value: Any) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))


portfolio_service = PortfolioService()

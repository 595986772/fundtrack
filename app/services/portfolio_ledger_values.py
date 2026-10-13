"""账本的十进制数边界。

旧表的 FLOAT 列保留给兼容接口，新记账的权威值写进 exact_values 的十进制字符串。
所有成本重放先读取精确值；只有图表/旧接口的展示投影才转成 float。这样既不在启动时
破坏旧数据库，也不会让一次 JSON/SQLite 浮点往返改变新流水的份额、净值和费用。
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from datetime import datetime, timezone


ZERO = Decimal("0")
MONEY = Decimal("0.01")
UNITS = Decimal("0.0001")
NAV = Decimal("0.000001")


def ledger_decimal(value, quantum, *, positive=False, label="数值") -> Decimal:
    """拒绝 NaN、无穷大和超出账本范围的数据，按明确的精度统一舍入。"""
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > Decimal("1000000000000"):
            raise ValueError
        with localcontext() as context:
            context.prec = 38
            result = number.quantize(quantum, rounding=ROUND_HALF_UP)
        if positive and result <= 0:
            raise ValueError
        return result
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是范围内的有限{'正数' if positive else '非负数'}") from exc


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def exact_values(units, unit_price, fee=0) -> dict[str, str]:
    return {
        "units": str(ledger_decimal(units, UNITS, positive=True, label="份额")),
        "unit_price": str(ledger_decimal(unit_price, NAV, positive=True, label="成交净值")),
        "fee": str(ledger_decimal(fee, MONEY, label="手续费")),
    }


def transaction_values(transaction) -> tuple[Decimal, Decimal, Decimal]:
    """历史 FLOAT 流水只能恢复已保存的数值，不伪造已丢失的小数精度。"""
    values = getattr(transaction, "exact_values", None) or {
        # 旧流水保留当时已存下的小数，不能拿新输入的六位净值规则再次截断历史。
        key: str(getattr(transaction, key)) for key in ("units", "unit_price", "fee")
    }
    return tuple(Decimal(values[key]) for key in ("units", "unit_price", "fee"))


def ledger_order_key(item):
    """更正后的记录沿用原业务顺序，不能用更正时间把同日买卖颠倒。"""
    values = item.exact_values or {}
    created = datetime.fromisoformat(values.get("ledger_created_at") or item.created_at.isoformat())
    return item.trade_date, created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created, values.get("ledger_id") or item.id


def replay_transactions(transactions, *, error_type=ValueError):
    """纯计算的移动平均成本重放，业务记账和灾备核对复用同一套规则。

    不查询行情、不写数据库，所以可以在断网的恢复环境中验证份额、成本与已实现收益。
    调用方须先过滤被更正的流水，并按 ledger_order_key 排序。
    """
    states, annotations = {}, {}
    for transaction in transactions:
        state = states.setdefault(transaction.fund_code, {
            "units": ZERO, "cost_amount": ZERO, "realized_pnl_amount": ZERO,
            "cumulative_buy_cost": ZERO, "first_purchase_date": None,
        })
        units, unit_price, fee = transaction_values(transaction)
        gross = money(units * unit_price)
        realized = None
        if transaction.transaction_type == "buy":
            buy_cost = Decimal(transaction.exact_values["opening_cost"]) if (transaction.exact_values or {}).get("opening_cost") else gross + fee
            state["units"] += units
            state["cost_amount"] += buy_cost
            state["cumulative_buy_cost"] += buy_cost
            if state["first_purchase_date"] is None:
                state["first_purchase_date"] = transaction.trade_date
        else:
            if units > state["units"]:
                raise error_type(f"{transaction.trade_date} 卖出 {transaction.fund_code} {transaction.units:.4f} 份，超过当时持有份额 {state['units']:.4f}")
            average_cost = state["cost_amount"] / state["units"] if state["units"] > Decimal("0.00000001") else ZERO
            released_cost = state["cost_amount"] if units == state["units"] else money(average_cost * units)
            realized = gross - fee - released_cost
            state["units"] -= units
            state["cost_amount"] -= released_cost
            state["realized_pnl_amount"] += realized
            if state["units"] <= Decimal("0.00000001"):
                state["units"], state["cost_amount"] = ZERO, ZERO
        annotations[transaction.id] = {"realized_pnl_amount": realized, "running_units": round(state["units"], 4)}
    return states, annotations

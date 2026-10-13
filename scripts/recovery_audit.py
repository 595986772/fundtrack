"""离线账务核对：输入数据库导出的行，输出核对结果，不联网、不修改流水。"""

from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace

from app.services.portfolio_ledger_values import UNITS, ZERO, ledger_order_key, money, replay_transactions, transaction_values

LEDGER_TABLES = (
    "fund_portfolios", "fund_portfolio_holdings", "fund_portfolio_transactions",
    "portfolio_transaction_corrections", "portfolio_trade_orders", "portfolio_trade_order_events",
)


def audit_ledger(tables, navs):
    """更正链、待赎回占用、清仓后的已实现收益也在核对范围内。

    净值只取备份内最新一条，并保留日期；不能用恢复日的新行情比较两次收益。
    数据缺失记为 unavailable，不用零补齐。不含流水的旧权重组合单独列为不适用。
    """
    missing = set(LEDGER_TABLES) - tables.keys()
    if missing:
        raise ValueError("缺少账务表，不能生成通过报告")
    portfolios = {(r["user_id"], r["id"]) for r in tables["fund_portfolios"]}
    rows, issues = [], []
    # 没有数据库外键的旧表也检查孤儿记录，避免只检查仍然存在的组合而漏报。
    for name in LEDGER_TABLES[1:]:
        for row in tables[name]:
            if (row["user_id"], row["portfolio_id"]) not in portfolios:
                issues.append({"code": "orphan_record", "table": name})
    for user, portfolio in sorted(portfolios):
        account = sha256(f"{user}:{portfolio}".encode()).hexdigest()[:20]
        def selected(name):
            return [r for r in tables[name] if (r["user_id"], r["portfolio_id"]) == (user, portfolio)]
        txns = selected("fund_portfolio_transactions")
        holdings = selected("fund_portfolio_holdings")
        corrections = selected("portfolio_transaction_corrections")
        orders = selected("portfolio_trade_orders")
        local = []
        tx_by_id = {r["id"]: r for r in txns}
        inactive = {r["transaction_id"] for r in corrections}
        for correction in corrections:
            replacement = correction.get("replacement_transaction_id")
            if correction["transaction_id"] not in tx_by_id or (replacement and replacement not in tx_by_id):
                local.append("broken_correction_reference")
            if replacement == correction["transaction_id"]:
                local.append("correction_cycle")
        # A -> B -> A 也不能当作两笔合法冲正，必须指出链损坏。
        links = {r["transaction_id"]: r.get("replacement_transaction_id") for r in corrections}
        for origin in links:
            seen, cursor = set(), origin
            while cursor in links and cursor not in seen:
                seen.add(cursor)
                cursor = links[cursor]
            if cursor in seen:
                local.append("correction_cycle")
        active = [r for r in txns if r["id"] not in inactive]
        converted = []
        try:
            for row in active:
                item = SimpleNamespace(**{**row, "exact_values": row.get("exact_values"),
                    "trade_date": date.fromisoformat(row["trade_date"]),
                    "created_at": datetime.fromisoformat(row["created_at"])})
                values = transaction_values(item)
                if item.transaction_type not in {"buy", "sell"} or any(not v.is_finite() or v < 0 for v in values) or values[0] == 0 or values[1] == 0:
                    raise ValueError("invalid ledger values")
                if (item.exact_values or {}).get("opening_cost"):
                    opening = Decimal(item.exact_values["opening_cost"])
                    if not opening.is_finite() or opening < 0:
                        raise ValueError("invalid opening cost")
                converted.append(item)
            states, _ = replay_transactions(sorted(converted, key=ledger_order_key))
        except (ValueError, ArithmeticError, TypeError, KeyError):
            local.append("ledger_replay_failed")
            states = {}
        actual = {r["fund_code"]: r for r in holdings if r["position_mode"] == "actual"}
        allocation = any(r["position_mode"] != "actual" for r in holdings)
        if allocation and txns:
            local.append("mixed_allocation_ledger")
        expected = {code for code, value in states.items() if value["units"] > ZERO}
        if set(actual) != expected:
            local.append("holding_set_mismatch")
        funds = []
        for code, value in sorted(states.items()):
            holding = actual.get(code)
            if holding:
                # 持仓表是 FLOAT 兼容投影，按产品规定的四位份额、两位金额核对。
                if Decimal(str(holding["units"])).quantize(UNITS) != value["units"].quantize(UNITS):
                    local.append("units_mismatch")
                if money(holding["cost_amount"]) != money(value["cost_amount"]):
                    local.append("cost_mismatch")
            reserved = sum((Decimal(r["requested_units"]) for r in orders if r["fund_code"] == code
                and r["transaction_type"] == "sell" and r["status"] in {"pending", "cancel_requested"}), ZERO)
            if reserved > value["units"]:
                local.append("reserved_units_exceed_holdings")
            nav = navs.get(code)
            market_value = money(value["units"] * Decimal(str(nav["unit_nav"]))) if nav else (ZERO if value["units"] == 0 else None)
            unrealized = market_value - value["cost_amount"] if market_value is not None else None
            funds.append({"fund_code": code, "units": str(value["units"].quantize(UNITS)),
                "cost_amount": str(money(value["cost_amount"])), "reserved_units": str(reserved),
                "realized_pnl": str(money(value["realized_pnl_amount"])),
                "nav_date": nav["nav_date"] if nav else None,
                "unrealized_pnl": str(money(unrealized)) if unrealized is not None else None,
                "total_pnl": str(money(unrealized + value["realized_pnl_amount"])) if unrealized is not None else None})
        for order in orders:
            if order["status"] in {"confirmed", "settled"} and order.get("transaction_id") not in tx_by_id:
                local.append("confirmed_order_missing_transaction")
            if order["status"] in {"pending", "cancel_requested"} and order["transaction_type"] == "sell" and order["fund_code"] not in states:
                local.append("reserved_units_without_holdings")
        local = sorted(set(local))
        issues.extend({"account": account, "code": code} for code in local)
        rows.append({"account": account, "transactions": len(txns), "effective_transactions": len(active),
            "corrections": len(corrections), "order_states": dict(sorted(Counter(r["status"] for r in orders).items())),
            "status": "failed" if local else "allocation_not_applicable" if allocation else "passed", "funds": funds})
    return {"passed": not issues, "valuation_complete": all(f["total_pnl"] is not None for r in rows for f in r["funds"]),
        "portfolio_count": len(portfolios), "issues": issues, "portfolios": rows}

"""把组合持仓和交易流水转换成可审计的真实业绩序列。

这个模块刻意和 ``portfolio_risk_engine.py`` 分开：

* 真实持仓组合按每笔买入、卖出和手续费重放，回答“这个账户实际经历了什么”；
* 旧版目标配置组合按固定权重每日再平衡，回答“这套配置历史上可能怎样表现”。

两种结果都使用基金总回报序列，并在同一日期上拼接基金专属业绩基准。大模型不参与
任何公式计算，Agent 只负责在后续研究报告中解释这些确定性结果。
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
import math
import statistics
from typing import Any, Protocol

from app.services.fund_benchmarks import fund_benchmark_repository
from app.services.fund_returns import (
    FundReturnSeriesError,
    FundReturnSeriesService,
    fund_return_series_service,
)
from app.services.fund_risk_adjusted import (
    FundRiskAdjustedError,
    FundRiskAdjustedReturnService,
    fund_risk_adjusted_return_service,
)
from app.services.funds import fund_catalog_service
from app.services.portfolios import portfolio_service
from app.services.errors import ServiceError


SUPPORTED_PERFORMANCE_MONTHS = {6, 12, 36}
TRADING_DAYS_PER_YEAR = 252
MIN_RETURN_OBSERVATIONS = 2
BENCHMARK_MINIMUM_COVERAGE_PCT = 80.0
EPSILON = 1e-10


class PortfolioPerformanceError(ServiceError):
    """组合流水、收益序列或基准不足以生成可信业绩时抛出。"""


class PortfolioReader(Protocol):
    """业绩层依赖的最小组合接口，方便测试替换为 Fake。"""

    def get(self, *, user_id: str, portfolio_id: str) -> dict[str, Any]: ...

    def list_transactions(
        self,
        *,
        user_id: str,
        portfolio_id: str,
    ) -> list[dict[str, Any]]: ...


class FundReader(Protocol):
    def get_fund(self, fund_code: str) -> dict[str, Any]: ...


class BenchmarkReader(Protocol):
    def get_context(self, *, fund_code: str) -> dict[str, Any] | None: ...


class PortfolioPerformanceService:
    """计算交易调整收益、XIRR、风险调整指标、基准和基金归因。"""

    def __init__(
        self,
        *,
        portfolios: PortfolioReader = portfolio_service,
        funds: FundReader = fund_catalog_service,
        return_series: FundReturnSeriesService = fund_return_series_service,
        risk_adjusted: FundRiskAdjustedReturnService = (
            fund_risk_adjusted_return_service
        ),
        benchmarks: BenchmarkReader = fund_benchmark_repository,
    ) -> None:
        self.portfolios = portfolios
        self.funds = funds
        self.return_series = return_series
        self.risk_adjusted = risk_adjusted
        self.benchmarks = benchmarks

    def analyze(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        months: int = 12,
    ) -> dict[str, Any]:
        """返回一份前端可直接画图、Agent 可直接引用的组合业绩快照。"""

        if months not in SUPPORTED_PERFORMANCE_MONTHS:
            raise PortfolioPerformanceError("组合业绩区间只支持 6、12 或 36 个月", code="invalid_arguments")

        portfolio = self.portfolios.get(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
        mode = str(portfolio.get("position_mode") or "allocation")
        if mode == "empty":
            raise PortfolioPerformanceError("组合中没有可计算的持仓", code="data_unavailable")

        if mode == "actual":
            transactions = self.portfolios.list_transactions(
                user_id=user_id,
                portfolio_id=portfolio_id,
            )
            # 审计列表包含已冲正的原流水，业绩投影只使用当前有效的记账版本。
            transactions = [item for item in transactions if item.get("effective", True)]
            if not transactions:
                raise PortfolioPerformanceError("真实组合还没有交易流水", code="data_unavailable")
            rows, fund_context = self._actual_rows(
                portfolio=portfolio,
                transactions=transactions,
            )
            data_kind = "actual_portfolio_performance"
            performance_label = "交易流水调整后的真实持仓业绩"
        else:
            rows, fund_context = self._allocation_rows(portfolio=portfolio)
            data_kind = "simulated_allocation_performance"
            performance_label = "当前目标权重的历史模拟业绩"

        if not rows:
            raise PortfolioPerformanceError("组合没有可用的收益日期", code="data_unavailable")
        period_end = rows[-1]["date"]
        cutoff = self._subtract_months(period_end, months)
        period_rows = [row for row in rows if row["date"] >= cutoff]
        if len(period_rows) < MIN_RETURN_OBSERVATIONS:
            raise PortfolioPerformanceError(
                f"组合区间收益样本不足，至少需要 {MIN_RETURN_OBSERVATIONS} 条", code="data_unavailable"
            )

        daily_returns = [float(row["daily_return"]) for row in period_rows]
        if any(not math.isfinite(value) or value <= -1 for value in daily_returns):
            raise PortfolioPerformanceError("组合日收益包含非法数值")

        twr_value = math.prod(1 + value for value in daily_returns)
        twr_pct = (twr_value - 1) * 100
        annualized_volatility_pct = (
            statistics.stdev(daily_returns)
            * math.sqrt(TRADING_DAYS_PER_YEAR)
            * 100
        )
        try:
            adjusted = self.risk_adjusted.analyze(
                daily_returns=daily_returns,
            )
        except FundRiskAdjustedError as exc:
            raise PortfolioPerformanceError(str(exc)) from exc

        chart_series, max_drawdown = self._chart_series(period_rows)
        benchmark = self._benchmark_comparison(
            rows=period_rows,
            fund_context=fund_context,
            chart_series=chart_series,
        )
        attribution = self._attribution(
            rows=period_rows,
            fund_context=fund_context,
            actual=mode == "actual",
        )
        cash_flows = self._cash_flow_summary(
            rows=period_rows,
            actual=mode == "actual",
        )
        if mode == "actual":
            xirr_pct, xirr_warning = self._xirr_for_rows(period_rows)
        else:
            xirr_pct, xirr_warning = None, None

        warnings = list(
            dict.fromkeys(
                warning
                for context in fund_context.values()
                for warning in context["metadata"].get(
                    "data_quality_warnings",
                    [],
                )
            )
        )
        warnings.extend(benchmark["warnings"])
        warnings.extend(adjusted["warnings"])
        if xirr_warning:
            warnings.append(xirr_warning)
        if mode != "actual":
            warnings.append(
                "目标配置没有真实现金流水，因此不计算 XIRR 和金额盈亏归因。"
            )

        return {
            "data_kind": data_kind,
            "performance_label": performance_label,
            "portfolio_id": portfolio_id,
            "portfolio_name": portfolio["name"],
            "position_mode": mode,
            "period_months": months,
            "period_start": period_rows[0]["date"].isoformat(),
            "period_end": period_rows[-1]["date"].isoformat(),
            "observation_count": len(period_rows),
            "twr_pct": round(twr_pct, 2),
            "xirr_pct": round(xirr_pct, 2) if xirr_pct is not None else None,
            "annualized_return_pct": adjusted["annualized_return_pct"],
            "annualized_volatility_pct": round(annualized_volatility_pct, 2),
            "sharpe_ratio": adjusted["sharpe_ratio"],
            "sortino_ratio": adjusted["sortino_ratio"],
            "risk_free_rate_annual_pct": adjusted[
                "risk_free_rate_annual_pct"
            ],
            "risk_free_rate_source": adjusted["risk_free_rate_source"],
            "max_drawdown": max_drawdown,
            "benchmark": benchmark,
            "cash_flows": cash_flows,
            "attribution": attribution,
            "series": chart_series,
            "data_sources": [
                {
                    "fund_code": fund_code,
                    "short_name": context["short_name"],
                    **context["metadata"],
                }
                for fund_code, context in fund_context.items()
            ],
            "assumptions": self._assumptions(actual=mode == "actual"),
            "warnings": list(dict.fromkeys(warnings)),
            "methodology": (
                "基金先按公司行为和数据源日增长率重建总回报。真实持仓按交易日期"
                "重放买卖，以外部现金流剔除法计算每日 TWR，并用 XIRR 衡量投资者"
                "资金的年化体验；基金收益贡献使用几何链接，使各基金贡献百分点之和"
                "等于组合 TWR。组合基准由每只基金自己的业绩比较基准按持仓权重混合。"
            ),
            "disclaimer": "历史业绩和归因仅用于学习与研究，不构成投资建议。",
        }

    def _fund_context(
        self,
        *,
        fund_codes: list[str],
        fallback_names: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        """一次读取每只基金的总回报序列，并转换成按日期索引的日收益。"""

        result: dict[str, dict[str, Any]] = {}
        for fund_code in fund_codes:
            detail = self.funds.get_fund(fund_code)
            try:
                series = self.return_series.build(
                    detail=detail,
                    points=list(detail.get("nav_points") or []),
                )
            except FundReturnSeriesError as exc:
                raise PortfolioPerformanceError(
                    f"基金 {fund_code} 无法生成总回报：{exc}"
                ) from exc
            daily_by_date = {
                date.fromisoformat(str(point["nav_date"])): float(daily_return)
                for point, daily_return in zip(
                    series.index_points[1:],
                    series.daily_returns,
                )
            }
            profile = detail.get("profile") or {}
            result[fund_code] = {
                "short_name": str(
                    profile.get("short_name")
                    or fallback_names.get(fund_code)
                    or fund_code
                ),
                "daily_returns": daily_by_date,
                "metadata": series.metadata(),
            }
        return result

    def _actual_rows(
        self,
        *,
        portfolio: dict[str, Any],
        transactions: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """重放真实流水，把外部投入/取出与投资收益拆开。"""

        normalized_transactions = []
        fallback_names: dict[str, str] = {}
        for item in transactions:
            transaction = dict(item)
            transaction["date"] = date.fromisoformat(str(item["trade_date"]))
            recorded = (item.get("exact_values") or {}).get("ledger_created_at") or item.get("created_at")
            # 接口/内存夹具可能分别给出 ISO 字符串和 datetime，先统一再排序。
            recorded = datetime.fromisoformat(str(recorded)) if recorded else datetime.min
            transaction["replay_time"] = recorded.replace(tzinfo=timezone.utc) if recorded.tzinfo is None else recorded
            normalized_transactions.append(transaction)
            fallback_names[str(item["fund_code"])] = str(
                item.get("short_name") or item["fund_code"]
            )
        normalized_transactions.sort(
            key=lambda item: (
                item["date"],
                item["replay_time"],
                str((item.get("exact_values") or {}).get("ledger_id") or item.get("id") or ""),
            )
        )
        fund_codes = list(dict.fromkeys(
            str(item["fund_code"]) for item in normalized_transactions
        ))
        fund_context = self._fund_context(
            fund_codes=fund_codes,
            fallback_names=fallback_names,
        )

        transactions_by_date: dict[date, list[dict[str, Any]]] = {}
        for transaction in normalized_transactions:
            transactions_by_date.setdefault(transaction["date"], []).append(
                transaction
            )

        first_date = normalized_transactions[0]["date"]
        current_codes = {
            str(item["fund_code"])
            for item in portfolio.get("holdings") or []
        }
        # 已全部卖出的组合在最后一笔交易日结束；仍有仓位时延伸到最新净值日。
        if current_codes:
            end_date = max(
                normalized_transactions[-1]["date"],
                *(
                    max(context["daily_returns"], default=first_date)
                    for code, context in fund_context.items()
                    if code in current_codes
                ),
            )
        else:
            end_date = normalized_transactions[-1]["date"]

        calendar_dates = {
            transaction["date"]
            for transaction in normalized_transactions
            if transaction["date"] <= end_date
        }
        for context in fund_context.values():
            calendar_dates.update(
                point_date
                for point_date in context["daily_returns"]
                if first_date <= point_date <= end_date
            )

        values = {fund_code: 0.0 for fund_code in fund_codes}
        units = {fund_code: 0.0 for fund_code in fund_codes}
        rows: list[dict[str, Any]] = []
        for point_date in sorted(calendar_dates):
            opening = dict(values)
            opening_total = sum(opening.values())
            market_pnl: dict[str, float] = {}
            fund_return_dates: list[str] = []
            for fund_code in fund_codes:
                daily_return = fund_context[fund_code]["daily_returns"].get(
                    point_date
                )
                if daily_return is None or opening[fund_code] <= EPSILON:
                    market_pnl[fund_code] = 0.0
                    continue
                gain = opening[fund_code] * daily_return
                values[fund_code] += gain
                market_pnl[fund_code] = gain
                fund_return_dates.append(fund_code)

            pre_trade_total = sum(values.values())
            net_flow_by_fund = {fund_code: 0.0 for fund_code in fund_codes}
            trade_effect_by_fund = {fund_code: 0.0 for fund_code in fund_codes}
            fee_by_fund = {fund_code: 0.0 for fund_code in fund_codes}
            # TWR/XIRR 只关心当天净现金流，但页面需要分别展示“投入”和“取出”。
            # 因此这里保留两个毛额，避免同一天买入和卖出时互相抵消。
            contribution_flow = 0.0
            withdrawal_flow = 0.0

            for transaction in transactions_by_date.get(point_date, []):
                fund_code = str(transaction["fund_code"])
                trade_units = float(transaction["units"])
                # 曲线使用账本已按分舍入的成交金额；初始导入成本不再由舍入后的净值反推。
                precise = transaction.get("exact_values") or {}
                gross = float(precise.get("opening_cost") or transaction.get("gross_amount")
                              or trade_units * float(transaction["unit_price"]))
                fee = float(transaction.get("fee") or 0.0)
                fee_by_fund[fund_code] += fee
                if transaction["transaction_type"] == "buy":
                    # 用户实际付出 gross + fee，但只有 gross 进入基金资产；差额就是
                    # 当天立刻发生的负收益，因此手续费不会被现金流剔除掉。
                    units[fund_code] += trade_units
                    values[fund_code] += gross
                    net_flow_by_fund[fund_code] += gross + fee
                    contribution_flow += gross + fee
                    trade_effect_by_fund[fund_code] -= fee
                    continue

                current_units = units[fund_code]
                if trade_units > current_units + EPSILON:
                    raise PortfolioPerformanceError(
                        f"{point_date.isoformat()} 基金 {fund_code} 的流水发生历史超卖"
                    )
                ratio = min(trade_units / current_units, 1.0)
                removed_value = values[fund_code] * ratio
                values[fund_code] -= removed_value
                units[fund_code] -= trade_units
                if units[fund_code] <= EPSILON:
                    units[fund_code] = 0.0
                    values[fund_code] = 0.0
                net_proceeds = gross - fee
                net_flow_by_fund[fund_code] -= net_proceeds
                withdrawal_flow += net_proceeds
                # 成交收入与模型公允价值之差包含成交偏差和手续费，属于组合表现，
                # 不能当成外部资金流一起剔除。
                trade_effect_by_fund[fund_code] += (
                    net_proceeds - removed_value
                )

            ending = dict(values)
            ending_total = sum(ending.values())
            net_flow = sum(net_flow_by_fund.values())
            positive_flow = sum(
                max(value, 0.0) for value in net_flow_by_fund.values()
            )
            market_return = (
                sum(market_pnl.values()) / opening_total
                if opening_total > EPSILON
                else 0.0
            )
            trade_denominator = pre_trade_total + positive_flow
            trade_effect = ending_total - pre_trade_total - net_flow
            trade_return = (
                trade_effect / trade_denominator
                if trade_denominator > EPSILON
                else 0.0
            )
            daily_return = (1 + market_return) * (1 + trade_return) - 1

            # 每只基金的日贡献相加必须等于组合日收益。之后再做几何链接，最终各基金
            # 贡献百分点之和也会精确等于区间 TWR。
            contribution_by_fund: dict[str, float] = {}
            for fund_code in fund_codes:
                market_part = (
                    market_pnl[fund_code] / opening_total
                    if opening_total > EPSILON
                    else 0.0
                )
                trade_part = (
                    trade_effect_by_fund[fund_code]
                    / trade_denominator
                    * (1 + market_return)
                    if trade_denominator > EPSILON
                    else 0.0
                )
                contribution_by_fund[fund_code] = market_part + trade_part

            rows.append(
                {
                    "date": point_date,
                    "daily_return": daily_return,
                    "opening_by_fund": opening,
                    "ending_by_fund": ending,
                    "net_flow_by_fund": net_flow_by_fund,
                    "fee_by_fund": fee_by_fund,
                    "contribution_by_fund": contribution_by_fund,
                    "fund_return_dates": fund_return_dates,
                    "opening_value": opening_total,
                    "ending_value": ending_total,
                    "net_flow": net_flow,
                    "contributions": contribution_flow,
                    "withdrawals": withdrawal_flow,
                    "has_market_return": bool(fund_return_dates),
                }
            )
        return rows, fund_context

    def _allocation_rows(
        self,
        *,
        portfolio: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """为旧版目标配置生成固定权重每日再平衡的模拟序列。"""

        holdings = list(portfolio.get("holdings") or [])
        if not holdings:
            raise PortfolioPerformanceError("目标配置没有基金持仓")
        fund_codes = [str(item["fund_code"]) for item in holdings]
        fallback_names = {
            str(item["fund_code"]): str(item.get("short_name") or item["fund_code"])
            for item in holdings
        }
        fund_context = self._fund_context(
            fund_codes=fund_codes,
            fallback_names=fallback_names,
        )
        common_dates = set.intersection(
            *(
                set(context["daily_returns"])
                for context in fund_context.values()
            )
        )
        weights = {
            str(item["fund_code"]): float(item["weight_pct"]) / 100
            for item in holdings
        }
        if not math.isclose(sum(weights.values()), 1.0, abs_tol=0.0001):
            raise PortfolioPerformanceError("目标配置权重合计必须为 100%")

        rows: list[dict[str, Any]] = []
        for point_date in sorted(common_dates):
            contributions = {
                fund_code: weights[fund_code]
                * fund_context[fund_code]["daily_returns"][point_date]
                for fund_code in fund_codes
            }
            rows.append(
                {
                    "date": point_date,
                    "daily_return": sum(contributions.values()),
                    "opening_by_fund": {
                        fund_code: weight * 100
                        for fund_code, weight in weights.items()
                    },
                    "ending_by_fund": {
                        fund_code: weight * 100
                        for fund_code, weight in weights.items()
                    },
                    "net_flow_by_fund": {
                        fund_code: 0.0 for fund_code in fund_codes
                    },
                    "fee_by_fund": {
                        fund_code: 0.0 for fund_code in fund_codes
                    },
                    "contribution_by_fund": contributions,
                    "fund_return_dates": list(fund_codes),
                    "opening_value": 100.0,
                    "ending_value": 100.0,
                    "net_flow": 0.0,
                    "contributions": 0.0,
                    "withdrawals": 0.0,
                    "has_market_return": True,
                }
            )
        return rows, fund_context

    def _chart_series(
        self,
        rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """把日收益复利成 100 点指数，并同步计算水下回撤曲线。"""

        index_value = 100.0
        peak_value = 100.0
        trough: dict[str, Any] | None = None
        series: list[dict[str, Any]] = []
        for row in rows:
            index_value *= 1 + float(row["daily_return"])
            peak_value = max(peak_value, index_value)
            drawdown_pct = (index_value / peak_value - 1) * 100
            if trough is None or drawdown_pct < trough["drawdown_pct"]:
                trough = {
                    "drawdown_pct": drawdown_pct,
                    "trough_date": row["date"].isoformat(),
                }
            series.append(
                {
                    "date": row["date"].isoformat(),
                    "portfolio_index": round(index_value, 6),
                    "benchmark_index": None,
                    "drawdown_pct": round(drawdown_pct, 4),
                    "market_value": round(float(row["ending_value"]), 2),
                    "net_flow": round(float(row["net_flow"]), 2),
                    "daily_return_pct": round(
                        float(row["daily_return"]) * 100,
                        6,
                    ),
                }
            )
        max_drawdown = trough or {
            "drawdown_pct": 0.0,
            "trough_date": rows[0]["date"].isoformat(),
        }
        max_drawdown["drawdown_pct"] = round(
            float(max_drawdown["drawdown_pct"]),
            2,
        )
        return series, max_drawdown

    def _benchmark_comparison(
        self,
        *,
        rows: list[dict[str, Any]],
        fund_context: dict[str, dict[str, Any]],
        chart_series: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """按每天实际持仓权重混合每只基金自己的业绩比较基准。"""

        contexts: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []
        for fund_code in fund_context:
            context = self.benchmarks.get_context(fund_code=fund_code)
            if context is None or context.get("benchmark_status") == "unavailable":
                warnings.append(f"基金 {fund_code} 的业绩基准尚不可用。")
                continue
            contexts[fund_code] = {
                **context,
                "returns": {
                    date.fromisoformat(str(point["benchmark_date"])): float(
                        point["daily_return"]
                    )
                    for point in context.get("points") or []
                },
            }

        names = list(
            dict.fromkeys(
                str(context.get("benchmark_name") or "")
                for context in contexts.values()
                if context.get("benchmark_name")
            )
        )
        benchmark_name = (
            names[0]
            if len(names) == 1
            else "持仓对应业绩基准混合"
            if names
            else "组合业绩基准"
        )
        benchmark_value = 1.0
        portfolio_aligned_value = 1.0
        matched_count = 0
        eligible_count = 0
        first_date: date | None = None
        last_date: date | None = None
        benchmark_started = False

        for row, chart_point in zip(rows, chart_series):
            if not row["has_market_return"] or row["opening_value"] <= EPSILON:
                continue
            eligible_count += 1
            active = {
                fund_code: float(value)
                for fund_code, value in row["opening_by_fund"].items()
                if float(value) > EPSILON
            }
            total = sum(active.values())
            if total <= EPSILON or any(code not in contexts for code in active):
                if benchmark_started:
                    chart_point["benchmark_index"] = round(
                        benchmark_value * 100,
                        6,
                    )
                continue

            benchmark_return = 0.0
            valid = True
            fund_return_dates = set(row["fund_return_dates"])
            for fund_code, value in active.items():
                # 若基金当天没有发布净值，真实组合价值也保持不变，对应基准分量按 0
                # 处理；只有基金有收益而基准缺值时才拒绝这一天的比较。
                if fund_code not in fund_return_dates:
                    component_return = 0.0
                else:
                    component_return = contexts[fund_code]["returns"].get(
                        row["date"]
                    )
                    if component_return is None:
                        valid = False
                        break
                benchmark_return += value / total * component_return
            if not valid:
                if benchmark_started:
                    chart_point["benchmark_index"] = round(
                        benchmark_value * 100,
                        6,
                    )
                continue

            benchmark_started = True
            matched_count += 1
            first_date = first_date or row["date"]
            last_date = row["date"]
            benchmark_value *= 1 + benchmark_return
            portfolio_aligned_value *= 1 + float(row["daily_return"])
            chart_point["benchmark_index"] = round(benchmark_value * 100, 6)

        coverage_pct = (
            matched_count / eligible_count * 100 if eligible_count else 0.0
        )
        source_urls = list(
            dict.fromkeys(
                url
                for context in contexts.values()
                for url in context.get("source_urls") or []
            )
        )
        source_names = list(
            dict.fromkeys(
                str(context.get("source") or "")
                for context in contexts.values()
                if context.get("source")
            )
        )
        for context in contexts.values():
            warnings.extend(context.get("warnings") or [])

        if matched_count < 2 or coverage_pct < BENCHMARK_MINIMUM_COVERAGE_PCT:
            # 样本数量与覆盖率是两个门槛。新账本可能覆盖率100%，但只有一个收益日。
            if matched_count < 2:
                warnings.append(f"组合与基准仅有 {matched_count} 个共同有效收益日，少于 2 日，暂不比较超额收益。")
            if coverage_pct < BENCHMARK_MINIMUM_COVERAGE_PCT:
                warnings.append(
                    f"组合基准只覆盖 {coverage_pct:.2f}% 的有效收益日，低于 "
                    f"{BENCHMARK_MINIMUM_COVERAGE_PCT:.0f}% 比较门槛。"
                )
            status = "insufficient" if contexts else "unavailable"
            return {
                "benchmark_name": benchmark_name,
                "status": status,
                "period_return_pct": None,
                "portfolio_aligned_return_pct": None,
                "excess_return_pct": None,
                "observation_count": matched_count,
                "coverage_pct": round(coverage_pct, 2),
                "period_start": first_date.isoformat() if first_date else None,
                "period_end": last_date.isoformat() if last_date else None,
                "source": " + ".join(source_names) or None,
                "source_urls": source_urls,
                "methodology": "组合基准由各持仓基金披露的业绩比较基准按每日持仓价值加权。",
                "warnings": list(dict.fromkeys(warnings)),
            }

        benchmark_return_pct = (benchmark_value - 1) * 100
        portfolio_aligned_return_pct = (portfolio_aligned_value - 1) * 100
        proxy = any(
            context.get("benchmark_status") == "proxy"
            for context in contexts.values()
        )
        return {
            "benchmark_name": benchmark_name,
            "status": "proxy" if proxy else "matched",
            "period_return_pct": round(benchmark_return_pct, 2),
            "portfolio_aligned_return_pct": round(
                portfolio_aligned_return_pct,
                2,
            ),
            "excess_return_pct": round(
                portfolio_aligned_return_pct - benchmark_return_pct,
                2,
            ),
            "observation_count": matched_count,
            "coverage_pct": round(coverage_pct, 2),
            "period_start": first_date.isoformat() if first_date else None,
            "period_end": last_date.isoformat() if last_date else None,
            "source": " + ".join(source_names) or None,
            "source_urls": source_urls,
            "methodology": (
                "组合基准由各持仓基金披露的业绩比较基准按每日持仓价值加权；"
                "超额收益是组合与混合基准在共同有效日期上的收益百分点差。"
            ),
            "warnings": list(dict.fromkeys(warnings)),
        }

    def _attribution(
        self,
        *,
        rows: list[dict[str, Any]],
        fund_context: dict[str, dict[str, Any]],
        actual: bool,
    ) -> list[dict[str, Any]]:
        """使用几何链接把组合 TWR 精确拆回各基金贡献。"""

        linked = {fund_code: 0.0 for fund_code in fund_context}
        future_growth = 1.0
        for row in reversed(rows):
            for fund_code in linked:
                linked[fund_code] += (
                    float(row["contribution_by_fund"].get(fund_code, 0.0))
                    * future_growth
                )
            future_growth *= 1 + float(row["daily_return"])

        starting = rows[0]["opening_by_fund"]
        ending = rows[-1]["ending_by_fund"]
        net_flows = {
            fund_code: sum(
                float(row["net_flow_by_fund"].get(fund_code, 0.0))
                for row in rows
            )
            for fund_code in fund_context
        }
        ending_total = sum(float(value) for value in ending.values())
        result = []
        for fund_code, context in fund_context.items():
            ending_value = float(ending.get(fund_code, 0.0))
            result.append(
                {
                    "fund_code": fund_code,
                    "short_name": context["short_name"],
                    "ending_weight_pct": round(
                        ending_value / ending_total * 100
                        if actual and ending_total > EPSILON
                        else float(ending_value),
                        2,
                    ),
                    "pnl_contribution_amount": (
                        round(
                            ending_value
                            - float(starting.get(fund_code, 0.0))
                            - net_flows[fund_code],
                            2,
                        )
                        if actual
                        else None
                    ),
                    "return_contribution_pct_points": round(
                        linked[fund_code] * 100,
                        2,
                    ),
                    "return_basis": context["metadata"]["return_basis"],
                    "return_basis_label": context["metadata"][
                        "return_basis_label"
                    ],
                }
            )
        result = sorted(
            result,
            key=lambda item: abs(float(item["return_contribution_pct_points"])),
            reverse=True,
        )
        # 单项先保留两位小数后可能出现 0.01pp 的展示误差。把纯舍入残差放到最大
        # 贡献项，保证用户在前端相加时仍然精确得到展示口径的组合 TWR。
        rounded_total = round((future_growth - 1) * 100, 2)
        displayed_total = round(
            sum(item["return_contribution_pct_points"] for item in result),
            2,
        )
        if result and not math.isclose(
            rounded_total,
            displayed_total,
            abs_tol=0.001,
        ):
            result[0]["return_contribution_pct_points"] = round(
                result[0]["return_contribution_pct_points"]
                + rounded_total
                - displayed_total,
                2,
            )
        return result

    @staticmethod
    def _cash_flow_summary(
        *,
        rows: list[dict[str, Any]],
        actual: bool,
    ) -> dict[str, Any]:
        if not actual:
            return {
                "opening_value": None,
                "contributions": None,
                "withdrawals": None,
                "fees": None,
                "net_invested": None,
                "ending_value": None,
            }
        contributions = sum(float(row["contributions"]) for row in rows)
        withdrawals = sum(float(row["withdrawals"]) for row in rows)
        fees = sum(
            sum(float(value) for value in row["fee_by_fund"].values())
            for row in rows
        )
        opening_value = float(rows[0]["opening_value"])
        return {
            "opening_value": round(opening_value, 2),
            "contributions": round(contributions, 2),
            "withdrawals": round(withdrawals, 2),
            "fees": round(fees, 2),
            "net_invested": round(
                opening_value + contributions - withdrawals,
                2,
            ),
            "ending_value": round(float(rows[-1]["ending_value"]), 2),
        }

    def _xirr_for_rows(
        self,
        rows: list[dict[str, Any]],
    ) -> tuple[float | None, str | None]:
        """把投资者投入记为负、取出和期末资产记为正，求年化资金收益率。"""

        flows: list[tuple[date, float]] = []
        opening_value = float(rows[0]["opening_value"])
        if opening_value > EPSILON:
            flows.append((rows[0]["date"], -opening_value))
        for row in rows:
            net_flow = float(row["net_flow"])
            if abs(net_flow) > EPSILON:
                flows.append((row["date"], -net_flow))
        ending_value = float(rows[-1]["ending_value"])
        if ending_value > EPSILON:
            flows.append((rows[-1]["date"], ending_value))
        if not flows or not any(value < 0 for _, value in flows) or not any(
            value > 0 for _, value in flows
        ):
            return None, "当前区间现金流没有同时包含投入和回收价值，XIRR 无法定义。"
        if flows[-1][0] <= flows[0][0]:
            return None, "现金流时间跨度不足一天，XIRR 无法定义。"

        base_date = min(point_date for point_date, _ in flows)

        def npv(rate: float) -> float:
            return sum(
                value / (1 + rate) ** ((point_date - base_date).days / 365.0)
                for point_date, value in flows
            )

        low = -0.9999
        high = 1.0
        low_value = npv(low)
        high_value = npv(high)
        while low_value * high_value > 0 and high < 1_000_000:
            high = high * 2 + 1
            high_value = npv(high)
        if low_value * high_value > 0:
            return None, "当前现金流未找到稳定的 XIRR 数值根。"

        for _ in range(200):
            middle = (low + high) / 2
            middle_value = npv(middle)
            if abs(middle_value) < 1e-8:
                return middle * 100, None
            if low_value * middle_value <= 0:
                high = middle
                high_value = middle_value
            else:
                low = middle
                low_value = middle_value
        return (low + high) / 2 * 100, None

    @staticmethod
    def _subtract_months(value: date, months: int) -> date:
        total_months = value.year * 12 + value.month - 1 - months
        year, month_zero = divmod(total_months, 12)
        month = month_zero + 1
        day = min(value.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    @staticmethod
    def _assumptions(*, actual: bool) -> list[str]:
        if not actual:
            return [
                "组合始终使用当前目标权重，并在每个共同净值日恢复到该权重。",
                "该结果是配置历史模拟，不代表用户真实账户收益。",
                "各基金只保留共同净值日期，不做向前填充。",
            ]
        return [
            "买入在交易日收益计算后生效，卖出在交易日收益计算后退出。",
            "基金价值按分红再投资总回报推进；未单独记录现金分红账户。",
            "不同基金净值日不一致时，未更新基金沿用上一估值，仅用于账户曲线。",
            "XIRR 使用实际交易现金流和区间期末模型价值，不代表券商对账单口径。",
        ]


portfolio_performance_service = PortfolioPerformanceService()

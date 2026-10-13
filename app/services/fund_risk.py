"""基于基金总回报序列计算确定性风险指标。

这个 service 不调用大模型，也不读取前端状态。它只接收基金代码和统计区间，读取
``FundCatalogService`` 的净值后完成数学计算。REST API、Agent Tool 和前端因此可以
共享完全相同的风险口径。
"""

from __future__ import annotations

import calendar
import math
import statistics
from datetime import date
from typing import Any

from app.services.funds import FundCatalogService, fund_catalog_service
from app.services.fund_benchmarks import (
    FundBenchmarkPerformanceService,
    fund_benchmark_performance_service,
)
from app.services.fund_risk_adjusted import (
    FundRiskAdjustedError,
    FundRiskAdjustedReturnService,
    fund_risk_adjusted_return_service,
)
from app.services.fund_returns import (
    FundReturnSeriesError,
    FundReturnSeriesService,
    fund_return_series_service,
)


from app.services.errors import ServiceError


SUPPORTED_RISK_MONTHS = {6, 12, 36}
TRADING_DAYS_PER_YEAR = 252


class FundRiskError(ServiceError):
    """风险区间或净值数据不足，无法完成计算。"""


class FundRiskService:
    """把一段净值序列转换成前端和 Agent 都能使用的风险画像。"""

    def __init__(
        self,
        catalog: FundCatalogService = fund_catalog_service,
        return_series: FundReturnSeriesService = fund_return_series_service,
        risk_adjusted: FundRiskAdjustedReturnService = (
            fund_risk_adjusted_return_service
        ),
        benchmark_performance: FundBenchmarkPerformanceService = (
            fund_benchmark_performance_service
        ),
    ) -> None:
        # 测试可以注入一个只返回人工净值序列的 Fake Catalog，从而独立验证公式。
        self.catalog = catalog
        self.return_series = return_series
        self.risk_adjusted = risk_adjusted
        self.benchmark_performance = benchmark_performance

    def analyze(self, *, fund_code: str, months: int = 12) -> dict[str, Any]:
        """计算指定基金近 6、12 或 36 个月的核心风险指标。"""

        if months not in SUPPORTED_RISK_MONTHS:
            raise FundRiskError("风险统计区间只支持 6、12 或 36 个月", code="invalid_arguments")

        detail = self.catalog.get_fund(fund_code)
        points = self._select_period_points(detail["nav_points"], months)
        if len(points) < 3:
            raise FundRiskError("当前区间净值点不足，至少需要 3 条记录", code="data_unavailable")

        # 分红和份额折算必须先进入统一收益层；后续所有指标复用同一份日收益，避免
        # “区间收益已复权、最大回撤却仍按单位净值”的混合口径。
        try:
            return_series = self.return_series.build(detail=detail, points=points)
        except FundReturnSeriesError as exc:
            raise FundRiskError(f"无法构建基金收益序列：{exc}") from exc
        series_points = return_series.index_points
        daily_returns = return_series.daily_returns
        try:
            # 夏普和 Sortino 只读取已经复权的同一份日收益，不重新读取单位净值。
            risk_adjusted = self.risk_adjusted.analyze(
                daily_returns=daily_returns
            )
        except FundRiskAdjustedError as exc:
            raise FundRiskError(f"无法计算风险调整后收益：{exc}") from exc
        benchmark_comparison = self.benchmark_performance.compare(
            fund_code=fund_code,
            profile=detail["profile"],
            fund_index_points=series_points,
            fund_daily_returns=daily_returns,
        )
        annualized_volatility = (
            statistics.stdev(daily_returns)
            * math.sqrt(TRADING_DAYS_PER_YEAR)
            * 100
        )

        # 下行波动率把上涨日视为 0，只累计低于 0 的收益平方。
        # 它比普通波动率更贴近用户对“跌起来有多不稳定”的感受。
        downside_volatility = (
            math.sqrt(
                sum(min(daily_return, 0.0) ** 2 for daily_return in daily_returns)
                / len(daily_returns)
            )
            * math.sqrt(TRADING_DAYS_PER_YEAR)
            * 100
        )

        max_drawdown = self._max_drawdown(series_points)
        longest_drawdown_days = self._longest_drawdown_days(series_points)
        current_peak = max(series_points, key=lambda point: point["index_value"])
        current_drawdown = (
            1
            - series_points[-1]["index_value"] / current_peak["index_value"]
        ) * 100
        worst_index, worst_return = min(
            enumerate(daily_returns, start=1),
            key=lambda item: item[1],
        )
        period_return = return_series.period_return_pct
        data_mode = detail.get("data_mode", "fixture")
        data_disclaimer = (
            "每日同步数据库快照，不是盘中实时行情"
            if data_mode == "database"
            else "本地固定历史快照"
        )
        disclaimer_parts = [
            data_disclaimer,
            *(
                warning.rstrip("。")
                for warning in return_series.data_quality_warnings
            ),
            "仅用于学习和研究，不构成投资建议",
        ]

        return {
            "fund_code": fund_code,
            "short_name": detail["profile"]["short_name"],
            "period_months": months,
            "period_start": points[0]["nav_date"],
            "period_end": points[-1]["nav_date"],
            "observation_count": len(points),
            "period_return_pct": round(period_return, 2),
            "annualized_volatility_pct": round(annualized_volatility, 2),
            "downside_volatility_pct": round(downside_volatility, 2),
            "max_drawdown": max_drawdown,
            "current_drawdown_pct": round(max(current_drawdown, 0.0), 2),
            "current_peak_date": current_peak["nav_date"],
            "worst_daily_return_pct": round(worst_return * 100, 2),
            "worst_daily_return_date": series_points[worst_index]["nav_date"],
            "longest_drawdown_days": longest_drawdown_days,
            "annualization_trading_days": TRADING_DAYS_PER_YEAR,
            "risk_adjusted": risk_adjusted,
            "benchmark_comparison": benchmark_comparison,
            "source": detail["source"],
            "data_mode": data_mode,
            "data_updated_at": detail.get("data_updated_at"),
            "snapshot_end": detail["snapshot_end"],
            **return_series.metadata(),
            "methodology": (
                f"收益口径为{return_series.return_basis_label}；年化波动率使用日收益样本"
                "标准差乘 sqrt(252)；下行波动率使用低于 0 的日收益平方均值；"
                "回撤均相对总回报指数此前高点计算。"
                f" {risk_adjusted['methodology']} "
                f"{benchmark_comparison['methodology']}"
            ),
            "disclaimer": (
                "；".join(disclaimer_parts) + "。"
            ),
        }

    def _select_period_points(
        self,
        all_points: list[dict[str, Any]],
        months: int,
    ) -> list[dict[str, Any]]:
        """按最后一个净值日期向前截取自然月，而不是简单假设每月 30 天。"""

        if not all_points:
            raise FundRiskError("基金没有可用净值数据", code="data_unavailable")
        latest_date = date.fromisoformat(all_points[-1]["nav_date"])
        cutoff = self._subtract_months(latest_date, months)
        return [
            point
            for point in all_points
            if date.fromisoformat(point["nav_date"]) >= cutoff
        ]

    def _max_drawdown(self, points: list[dict[str, Any]]) -> dict[str, Any]:
        """寻找“先出现高点、之后出现低点”的最大跌幅及恢复日期。"""

        running_peak = points[0]
        worst_peak = points[0]
        worst_trough = points[0]
        worst_drawdown = 0.0

        for point in points:
            if point["index_value"] > running_peak["index_value"]:
                running_peak = point
            drawdown = point["index_value"] / running_peak["index_value"] - 1
            if drawdown < worst_drawdown:
                worst_drawdown = drawdown
                worst_peak = running_peak
                worst_trough = point

        # 恢复的定义是：低点之后，净值第一次重新达到最大回撤前的高点。
        recovery_point = next(
            (
                point
                for point in points
                if point["nav_date"] > worst_trough["nav_date"]
                and point["index_value"] >= worst_peak["index_value"]
            ),
            None,
        )
        recovery_days = None
        if recovery_point is not None:
            recovery_days = (
                date.fromisoformat(recovery_point["nav_date"])
                - date.fromisoformat(worst_trough["nav_date"])
            ).days

        return {
            "drawdown_pct": round(abs(worst_drawdown) * 100, 2),
            "peak_date": worst_peak["nav_date"],
            "trough_date": worst_trough["nav_date"],
            "recovery_date": (
                recovery_point["nav_date"] if recovery_point else None
            ),
            "recovery_days": recovery_days,
        }

    def _longest_drawdown_days(self, points: list[dict[str, Any]]) -> int:
        """计算净值从高点跌落到恢复新高（或区间结束）的最长日历天数。"""

        peak_nav = points[0]["index_value"]
        peak_date = date.fromisoformat(points[0]["nav_date"])
        drawdown_started_at: date | None = None
        longest_days = 0

        for point in points[1:]:
            point_date = date.fromisoformat(point["nav_date"])
            if point["index_value"] >= peak_nav:
                if drawdown_started_at is not None:
                    longest_days = max(
                        longest_days,
                        (point_date - drawdown_started_at).days,
                    )
                    drawdown_started_at = None
                peak_nav = point["index_value"]
                peak_date = point_date
            elif drawdown_started_at is None:
                drawdown_started_at = peak_date

        # 如果统计截止日仍未恢复，就把最后一个净值日当作当前持续终点。
        if drawdown_started_at is not None:
            last_date = date.fromisoformat(points[-1]["nav_date"])
            longest_days = max(longest_days, (last_date - drawdown_started_at).days)
        return longest_days

    @staticmethod
    def _subtract_months(value: date, months: int) -> date:
        """日期减自然月，并把 3 月 31 日之类的日期安全映射到短月份。"""

        month_index = value.year * 12 + value.month - 1 - months
        year, zero_based_month = divmod(month_index, 12)
        month = zero_based_month + 1
        day = min(value.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)


fund_risk_service = FundRiskService()

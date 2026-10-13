"""把单位净值和公司行为转换成统一的基金收益序列。

风险、组合和 Agent 摘要都应复用这一层，避免一处按单位净值、一处按日增长率计算。
优先级如下：
1. 公司行为覆盖完整时，根据分红和份额折算重建总回报；
2. 覆盖不完整但上游日增长率齐全时，复利使用上游公布收益；
3. 两者都不可用时才使用未复权单位净值，并显式返回质量警告。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
from typing import Any, Literal


ReturnBasis = Literal[
    "total_return_reinvested",
    "provider_reported_daily_return",
    "unit_nav_unadjusted",
]
ReturnBasisStatus = Literal["verified", "provider_reported", "limited"]

RETURN_BASIS_LABELS: dict[ReturnBasis, str] = {
    "total_return_reinvested": "分红再投资总回报",
    "provider_reported_daily_return": "数据源公布日收益",
    "unit_nav_unadjusted": "单位净值未复权",
}
# 单位净值通常只保留四位小数，和上游日增长率之间允许少量舍入差。
RECONCILIATION_TOLERANCE_PCT = 0.15


class FundReturnSeriesError(RuntimeError):
    """净值、公司行为或上游收益率无法组成合法的收益序列。"""


@dataclass(frozen=True)
class FundReturnSeries:
    """风险计算真正需要的指数点、日收益和口径元数据。"""

    index_points: list[dict[str, Any]]
    daily_returns: list[float]
    return_basis: ReturnBasis
    return_basis_label: str
    return_basis_status: ReturnBasisStatus
    corporate_action_count: int
    corporate_action_source: str | None
    corporate_action_coverage_start: str | None
    corporate_action_coverage_end: str | None
    data_quality_warnings: list[str]

    @property
    def period_return_pct(self) -> float:
        """指数从首日 1.0 到末日的复利收益率。"""

        return (self.index_points[-1]["index_value"] - 1) * 100

    def metadata(self) -> dict[str, Any]:
        """生成 REST、Tool 和组合数据源可直接复用的口径字段。"""

        return {
            "return_basis": self.return_basis,
            "return_basis_label": self.return_basis_label,
            "return_basis_status": self.return_basis_status,
            "corporate_action_count": self.corporate_action_count,
            "corporate_action_source": self.corporate_action_source,
            "corporate_action_coverage_start": self.corporate_action_coverage_start,
            "corporate_action_coverage_end": self.corporate_action_coverage_end,
            "data_quality_warnings": list(self.data_quality_warnings),
        }


class FundReturnSeriesService:
    """选择可信收益口径，并把日收益复利成从 1.0 开始的指数。"""

    def build(
        self,
        *,
        detail: dict[str, Any],
        points: list[dict[str, Any]] | None = None,
    ) -> FundReturnSeries:
        """为一只基金构建收益序列，``points`` 可传入风险层已截取的区间。"""

        selected = list(points if points is not None else detail.get("nav_points") or [])
        if len(selected) < 2:
            raise FundReturnSeriesError("收益序列至少需要 2 个净值点")

        normalized_points = self._normalize_points(selected)
        start = normalized_points[0]["date"]
        end = normalized_points[-1]["date"]
        coverage = detail.get("corporate_action_coverage") or {}
        coverage_intervals = list(coverage.get("intervals") or [coverage])
        parsed_intervals = [
            (interval_start, interval_end)
            for interval in coverage_intervals
            if (interval_start := self._optional_date(interval.get("coverage_start")))
            is not None
            and (interval_end := self._optional_date(interval.get("coverage_end")))
            is not None
        ]
        complete_interval = next(
            (
                (interval_start, interval_end)
                for interval_start, interval_end in parsed_intervals
                if interval_start <= start
                and interval_end >= end
            ),
            None,
        )
        coverage_complete = complete_interval is not None
        # 覆盖不完整时仍返回当前最长可用区间，调用方可以直接看到缺口在哪里。
        reported_interval = complete_interval or (
            max(
                parsed_intervals,
                key=lambda item: (item[1] - item[0]).days,
            )
            if parsed_intervals
            else None
        )
        coverage_start = reported_interval[0] if reported_interval else None
        coverage_end = reported_interval[1] if reported_interval else None

        actions = self._actions_in_period(
            detail.get("corporate_actions") or [],
            start=start,
            end=end,
        )
        warnings: list[str] = []
        if coverage_complete:
            daily_returns = self._corporate_action_adjusted_returns(
                points=normalized_points,
                actions=actions,
            )
            mismatch_count = self._reconciliation_mismatch_count(
                points=normalized_points,
                daily_returns=daily_returns,
            )
            if mismatch_count:
                warnings.append(
                    f"{mismatch_count} 个交易日的公司行为复权收益与数据源日增长率"
                    "存在超过 0.15 个百分点的差异，请核对原始数据。"
                )
            basis: ReturnBasis = "total_return_reinvested"
            status: ReturnBasisStatus = "verified"
        elif self._has_complete_provider_returns(normalized_points):
            daily_returns = [
                float(point["daily_growth_rate_pct"]) / 100
                for point in normalized_points[1:]
            ]
            basis = "provider_reported_daily_return"
            status = "provider_reported"
            warnings.append(
                "公司行为明细尚未完整覆盖该区间，当前使用数据源公布的日增长率复利。"
            )
        else:
            daily_returns = [
                current["unit_nav"] / previous["unit_nav"] - 1
                for previous, current in zip(
                    normalized_points,
                    normalized_points[1:],
                )
            ]
            basis = "unit_nav_unadjusted"
            status = "limited"
            warnings.append(
                "公司行为和数据源日增长率均不完整，当前结果使用未复权单位净值；"
                "分红或份额折算可能被误认为涨跌。"
            )

        self._validate_returns(daily_returns)
        index_points = self._compound_index(
            points=normalized_points,
            daily_returns=daily_returns,
        )
        return FundReturnSeries(
            index_points=index_points,
            daily_returns=daily_returns,
            return_basis=basis,
            return_basis_label=RETURN_BASIS_LABELS[basis],
            return_basis_status=status,
            corporate_action_count=len(actions),
            corporate_action_source=(str(coverage["source"]) if coverage.get("source") else None),
            corporate_action_coverage_start=(
                coverage_start.isoformat() if coverage_start else None
            ),
            corporate_action_coverage_end=(
                coverage_end.isoformat() if coverage_end else None
            ),
            data_quality_warnings=warnings,
        )

    @staticmethod
    def _normalize_points(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """排序、去重并校验净值，避免下游公式重复处理脏数据。"""

        by_date: dict[date, dict[str, Any]] = {}
        for point in points:
            try:
                point_date = date.fromisoformat(str(point["nav_date"]))
                unit_nav = float(point["unit_nav"])
            except (KeyError, TypeError, ValueError) as exc:
                raise FundReturnSeriesError("净值日期或单位净值无法解析") from exc
            if not math.isfinite(unit_nav) or unit_nav <= 0:
                raise FundReturnSeriesError(
                    f"{point_date.isoformat()} 的单位净值必须为正数"
                )
            growth = point.get("daily_growth_rate_pct")
            if growth is not None:
                try:
                    growth = float(growth)
                except (TypeError, ValueError) as exc:
                    raise FundReturnSeriesError("日增长率无法解析") from exc
                if not math.isfinite(growth):
                    growth = None
            by_date[point_date] = {
                "date": point_date,
                "nav_date": point_date.isoformat(),
                "unit_nav": unit_nav,
                "daily_growth_rate_pct": growth,
            }

        normalized = [by_date[key] for key in sorted(by_date)]
        if len(normalized) < 2:
            raise FundReturnSeriesError("去重后收益序列至少需要 2 个净值点")
        return normalized

    @staticmethod
    def _actions_in_period(
        actions: list[dict[str, Any]],
        *,
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for action in actions:
            try:
                action_date = date.fromisoformat(str(action["action_date"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise FundReturnSeriesError("公司行为日期无法解析") from exc
            # 首日收盘净值已经反映当天事件；只有首日之后的事件影响区间收益。
            if start < action_date <= end:
                normalized.append({**action, "date": action_date})
        return sorted(
            normalized,
            key=lambda item: (item["date"], str(item.get("action_type") or "")),
        )

    def _corporate_action_adjusted_returns(
        self,
        *,
        points: list[dict[str, Any]],
        actions: list[dict[str, Any]],
    ) -> list[float]:
        """按每个相邻净值区间应用分红和份额折算。

        对最常见的单一事件，公式就是：
        ``(当日单位净值 × 拆分比例 + 每份现金分红) / 前日单位净值 - 1``。
        事件落在非净值日时归入之后第一个净值点，避免因节假日丢失调整。
        """

        daily_returns: list[float] = []
        for previous, current in zip(points, points[1:]):
            interval_actions = [
                action
                for action in actions
                if previous["date"] < action["date"] <= current["date"]
            ]
            split_ratio = 1.0
            cash_dividend = 0.0
            for action in interval_actions:
                action_type = str(action.get("action_type") or "")
                if action_type == "cash_dividend":
                    value = float(action.get("cash_dividend_per_unit") or 0)
                    if not math.isfinite(value) or value < 0:
                        raise FundReturnSeriesError("每份现金分红必须是非负数")
                    cash_dividend += value
                elif action_type == "share_split":
                    value = float(action.get("split_ratio") or 0)
                    if not math.isfinite(value) or value <= 0:
                        raise FundReturnSeriesError("份额折算比例必须为正数")
                    split_ratio *= value
                else:
                    raise FundReturnSeriesError(f"不支持的公司行为类型：{action_type}")

            end_value_per_old_unit = (
                current["unit_nav"] * split_ratio + cash_dividend
            )
            daily_returns.append(
                end_value_per_old_unit / previous["unit_nav"] - 1
            )
        return daily_returns

    @staticmethod
    def _has_complete_provider_returns(points: list[dict[str, Any]]) -> bool:
        return all(
            point.get("daily_growth_rate_pct") is not None
            for point in points[1:]
        )

    @staticmethod
    def _reconciliation_mismatch_count(
        *,
        points: list[dict[str, Any]],
        daily_returns: list[float],
    ) -> int:
        mismatches = 0
        for point, calculated in zip(points[1:], daily_returns):
            reported = point.get("daily_growth_rate_pct")
            if reported is None:
                continue
            if abs(calculated * 100 - float(reported)) > RECONCILIATION_TOLERANCE_PCT:
                mismatches += 1
        return mismatches

    @staticmethod
    def _validate_returns(daily_returns: list[float]) -> None:
        for value in daily_returns:
            if not math.isfinite(value) or value <= -1:
                raise FundReturnSeriesError("日收益率必须是大于 -100% 的有限数值")

    @staticmethod
    def _compound_index(
        *,
        points: list[dict[str, Any]],
        daily_returns: list[float],
    ) -> list[dict[str, Any]]:
        index_points = [
            {
                "nav_date": points[0]["nav_date"],
                "index_value": 1.0,
                "unit_nav": points[0]["unit_nav"],
            }
        ]
        index_value = 1.0
        for point, daily_return in zip(points[1:], daily_returns):
            index_value *= 1 + daily_return
            index_points.append(
                {
                    "nav_date": point["nav_date"],
                    "index_value": index_value,
                    "unit_nav": point["unit_nav"],
                }
            )
        return index_points

    @staticmethod
    def _optional_date(value: Any) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise FundReturnSeriesError("公司行为覆盖日期无法解析") from exc


fund_return_series_service = FundReturnSeriesService()

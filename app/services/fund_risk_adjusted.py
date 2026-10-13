"""计算基金年化收益、夏普比率和 Sortino 比率。

这一层只做数学，不读取数据库也不调用模型。输入必须来自 FundReturnSeriesService，
这样分红和份额折算已经先被还原成真实总回报，不会污染风险调整后指标。
"""

from __future__ import annotations

import math
import statistics

from app.config import settings


TRADING_DAYS_PER_YEAR = 252
NEAR_ZERO = 1e-12


class FundRiskAdjustedError(RuntimeError):
    """日收益或无风险利率不满足风险调整计算要求。"""


class FundRiskAdjustedReturnService:
    """把总回报日收益转换成年化收益、夏普和 Sortino。"""

    def __init__(self, *, risk_free_rate_annual_pct: float | None = None) -> None:
        self.risk_free_rate_annual_pct = (
            settings.fund_risk_free_rate_annual_pct
            if risk_free_rate_annual_pct is None
            else float(risk_free_rate_annual_pct)
        )
        if (
            not math.isfinite(self.risk_free_rate_annual_pct)
            or self.risk_free_rate_annual_pct <= -100
        ):
            raise ValueError("无风险年利率必须是大于 -100% 的有限数值")

    def analyze(self, *, daily_returns: list[float]) -> dict:
        """使用日频超额收益计算三个风险调整后指标。

        夏普分母使用日收益样本标准差；Sortino 分母只计算低于每日无风险目标的
        负向偏差。两者最后都乘 sqrt(252) 转成年化比率。
        """

        if len(daily_returns) < 2:
            raise FundRiskAdjustedError("风险调整收益至少需要 2 个日收益样本")
        if any(
            not math.isfinite(value) or value <= -1
            for value in daily_returns
        ):
            raise FundRiskAdjustedError("日收益必须是大于 -100% 的有限数值")

        compounded = math.prod(1 + value for value in daily_returns)
        annualized_return = compounded ** (
            TRADING_DAYS_PER_YEAR / len(daily_returns)
        ) - 1
        annual_risk_free = self.risk_free_rate_annual_pct / 100
        daily_risk_free = (1 + annual_risk_free) ** (
            1 / TRADING_DAYS_PER_YEAR
        ) - 1
        excess_returns = [
            daily_return - daily_risk_free
            for daily_return in daily_returns
        ]
        mean_excess = statistics.fmean(excess_returns)

        warnings: list[str] = []
        volatility = statistics.stdev(daily_returns)
        if volatility <= NEAR_ZERO:
            sharpe_ratio = None
            warnings.append("日收益没有可测波动，夏普比率无法定义。")
        else:
            sharpe_ratio = (
                mean_excess / volatility * math.sqrt(TRADING_DAYS_PER_YEAR)
            )

        downside_deviation = math.sqrt(
            statistics.fmean(
                min(excess_return, 0.0) ** 2
                for excess_return in excess_returns
            )
        )
        if downside_deviation <= NEAR_ZERO:
            sortino_ratio = None
            warnings.append(
                "区间内没有低于每日无风险目标的有效下行偏差，Sortino 比率无法定义。"
            )
        else:
            sortino_ratio = (
                mean_excess
                / downside_deviation
                * math.sqrt(TRADING_DAYS_PER_YEAR)
            )

        return {
            "annualized_return_pct": round(annualized_return * 100, 2),
            "risk_free_rate_annual_pct": round(
                self.risk_free_rate_annual_pct,
                4,
            ),
            "risk_free_rate_source": (
                "FundScope configuration: FUND_RISK_FREE_RATE_ANNUAL_PCT"
            ),
            "sharpe_ratio": (
                round(sharpe_ratio, 3) if sharpe_ratio is not None else None
            ),
            "sortino_ratio": (
                round(sortino_ratio, 3) if sortino_ratio is not None else None
            ),
            "methodology": (
                "年化收益使用总回报复利并按 252 个交易日折算；夏普使用日超额收益"
                "均值/日收益样本标准差×sqrt(252)；Sortino 只累计低于每日无风险"
                "目标的负向偏差。"
            ),
            "warnings": warnings,
        }


fund_risk_adjusted_return_service = FundRiskAdjustedReturnService()

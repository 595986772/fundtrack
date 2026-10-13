"""计算市场事件的有效期和时间衰减。

DeepSeek 只负责把事件归为短期、中期或长期；具体多少天有效、今天还剩多少权重，
由 Python 使用公开常量计算。这样同一份输入总能得到同一结果，也方便后续用 Eval 调参。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any


# 每种影响周期对应“完全新鲜天数”和“总有效天数”。前一段不衰减，后一段线性衰减。
HORIZON_WINDOWS = {
    "short_term": {"fresh_days": 3, "valid_days": 14},
    "medium_term": {"fresh_days": 14, "valid_days": 60},
    "long_term": {"fresh_days": 30, "valid_days": 180},
}


class MarketEventFreshnessError(ValueError):
    """事件日期或影响周期不符合时效性规则。"""


class MarketEventFreshnessService:
    """把事件日期和影响周期转换成可解释的衰减结果。"""

    def evaluate(
        self,
        *,
        event_date: date | str,
        horizon: str,
        as_of: date,
    ) -> dict[str, Any]:
        """返回状态、有效期和 0-1 衰减系数。

        `as_of` 使用整份数据的截止日期，而不是服务器当前时间。这样冻结快照可以复现，
        每日数据库同步后又会自然向前推进。
        """

        try:
            normalized_event_date = (
                event_date
                if isinstance(event_date, date)
                else date.fromisoformat(str(event_date))
            )
        except ValueError as exc:
            raise MarketEventFreshnessError(f"无效事件日期：{event_date}") from exc

        window = HORIZON_WINDOWS.get(horizon)
        if window is None:
            raise MarketEventFreshnessError(f"不支持的影响周期：{horizon}")

        # 来源偶尔可能提前发布未来生效事件；这时年龄按 0 天处理，不产生大于 1 的系数。
        age_days = max(0, (as_of - normalized_event_date).days)
        fresh_days = window["fresh_days"]
        valid_days = window["valid_days"]
        valid_until = normalized_event_date + timedelta(days=valid_days)

        if age_days <= fresh_days:
            status = "fresh"
            decay_factor = 1.0
        elif age_days < valid_days:
            status = "decaying"
            decay_factor = (valid_days - age_days) / (valid_days - fresh_days)
        else:
            # 过期事件仍会显示和保留来源，但在证据评分中乘以 0。
            status = "expired"
            decay_factor = 0.0

        rounded_factor = round(decay_factor, 4)
        return {
            "freshness_status": status,
            "freshness_as_of": as_of.isoformat(),
            "age_days": age_days,
            "fresh_days": fresh_days,
            "valid_days": valid_days,
            "valid_until": valid_until.isoformat(),
            "decay_factor": rounded_factor,
            "freshness_score": round(rounded_factor * 100, 2),
        }


market_event_freshness_service = MarketEventFreshnessService()

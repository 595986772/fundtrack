"""计算社交观点服务于哪个基金操作窗口。

普通“最近 N 小时”无法表达基金 15:00 截止规则：周一上午的有效窗口跨过周末，
工作日 15:00 之后发布的观点又应该归到下一个交易日。本模块使用上交所 XSHG
日历确定真实交易日，再用北京时间和最大年龄限制生成唯一、可测试的筛选窗口。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo

import exchange_calendars as exchange_calendar_library
import pandas as pd

from app.config import settings


CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")
EXCHANGE_CALENDAR_NAME = "XSHG"
OperationPhase = Literal["before_cutoff", "after_cutoff", "market_closed"]
CalendarLoader = Callable[[], Any]


@lru_cache(maxsize=1)
def _load_xshg_calendar() -> Any:
    """交易日历对象较重，进程内只构造一次即可。"""

    return exchange_calendar_library.get_calendar(EXCHANGE_CALENDAR_NAME)


@dataclass(frozen=True)
class FundOperationWindow:
    """一次刷新使用的精确时间边界，内部统一保留带时区的 datetime。"""

    phase: OperationPhase
    target_trade_date: date
    window_start: datetime
    window_end: datetime
    cutoff_at: datetime
    max_age_hours: int
    label: str

    def as_dict(self) -> dict[str, Any]:
        """转换为 API、Background Job 和前端都能直接序列化的结构。"""

        effective_hours = max(
            0.0,
            (self.window_end - self.window_start).total_seconds() / 3600,
        )
        remaining_minutes = max(
            0,
            int((self.cutoff_at - self.window_end).total_seconds() // 60),
        )
        return {
            "phase": self.phase,
            "timezone": "Asia/Shanghai",
            "calendar": EXCHANGE_CALENDAR_NAME,
            "target_trade_date": self.target_trade_date.isoformat(),
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "cutoff_at": self.cutoff_at.isoformat(),
            "cutoff_time": self.cutoff_at.strftime("%H:%M"),
            "max_age_hours": self.max_age_hours,
            "effective_window_hours": round(effective_hours, 2),
            "minutes_until_cutoff": remaining_minutes,
            "label": self.label,
        }


class SocialSignalFreshnessService:
    """根据当前时间计算帖子采集、聚合和展示共同使用的操作窗口。"""

    def __init__(self, calendar_loader: CalendarLoader | None = None) -> None:
        self.calendar_loader = calendar_loader or _load_xshg_calendar

    def resolve(self, *, as_of: datetime | None = None) -> FundOperationWindow:
        """返回当前操作窗口。

        截止前服务于今天；交易日截止后服务于下一交易日；休市日也服务于下一交易日。
        窗口起点最后还会与“当前时间减最大年龄”取较晚值，确保长假内容不会过期复用。
        """

        current = self._aware(as_of or datetime.now(timezone.utc))
        local_now = current.astimezone(CHINA_TIMEZONE)
        calendar = self.calendar_loader()
        current_session = pd.Timestamp(local_now.date())
        cutoff_today = self._cutoff_on(local_now.date())

        if calendar.is_session(current_session) and local_now < cutoff_today:
            phase: OperationPhase = "before_cutoff"
            target_session = current_session
            previous_session = calendar.previous_session(current_session)
            cycle_start = self._cutoff_on(previous_session.date())
        elif calendar.is_session(current_session):
            phase = "after_cutoff"
            target_session = calendar.next_session(current_session)
            cycle_start = cutoff_today
        else:
            phase = "market_closed"
            target_session = calendar.date_to_session(
                current_session,
                direction="next",
            )
            previous_session = calendar.previous_session(target_session)
            cycle_start = self._cutoff_on(previous_session.date())

        target_date = target_session.date()
        cutoff_at = self._cutoff_on(target_date)
        max_age_hours = max(1, settings.xhs_social_signal_lookback_hours)
        absolute_start = local_now - timedelta(hours=max_age_hours)
        window_start = max(cycle_start, absolute_start)
        return FundOperationWindow(
            phase=phase,
            target_trade_date=target_date,
            window_start=window_start.astimezone(timezone.utc),
            window_end=local_now.astimezone(timezone.utc),
            cutoff_at=cutoff_at,
            max_age_hours=max_age_hours,
            label=self._label(
                phase=phase,
                target_date=target_date,
                cutoff_at=cutoff_at,
                local_now=local_now,
            ),
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _cutoff_on(value: date) -> datetime:
        hour = max(0, min(settings.xhs_social_signal_cutoff_hour, 23))
        minute = max(0, min(settings.xhs_social_signal_cutoff_minute, 59))
        return datetime.combine(
            value,
            time(hour=hour, minute=minute),
            tzinfo=CHINA_TIMEZONE,
        )

    @staticmethod
    def _label(
        *,
        phase: OperationPhase,
        target_date: date,
        cutoff_at: datetime,
        local_now: datetime,
    ) -> str:
        cutoff_text = cutoff_at.strftime("%H:%M")
        if phase == "before_cutoff":
            minutes = max(0, int((cutoff_at - local_now).total_seconds() // 60))
            hours, remainder = divmod(minutes, 60)
            return f"本交易日 {cutoff_text} 截止，剩余 {hours} 小时 {remainder} 分"
        if phase == "after_cutoff":
            return (
                f"今日 {cutoff_text} 已截止，当前观点归入 "
                f"{target_date.isoformat()} 交易窗口"
            )
        return (
            f"当前为休市日，观点归入 {target_date.isoformat()} "
            f"{cutoff_text} 前的交易窗口"
        )


social_signal_freshness_service = SocialSignalFreshnessService()

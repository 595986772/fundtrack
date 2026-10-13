"""基金交易的时间轴：申请时间 -> 成交净值日 -> 确认与收益可见条件。

这里只查本地基金档案和交易日历，不调用大模型、不下单、不猜未来净值。
普通场外净值型基金按 T 日净值买入，因此首个后续净值日的变化才属于新份额；
卖出则按 T 日净值结算，原有份额承担到 T 日为止的涨跌。这不是保证盈利。
"""

from datetime import datetime, time
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars
import pandas as pd

from app.schemas import FundTransactionTimingResponse
from app.services.funds import fund_catalog_service


CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")
RULE_VERSION = "cn-off-exchange-nav-v1"
SOURCES = [
    "https://m.chinaamc.com/cjwt/gmwt/5501301.shtml",
    "https://m.chinaamc.com/cjwt/gmwt/5501300.shtml",
    "https://www.sse.com.cn/disclosure/dealinstruc/closed/",
]


@lru_cache(maxsize=1)
def _calendar():
    """复用现有依赖的上交所交易日历，调休上班的周末仍然休市。"""
    return exchange_calendars.get_calendar("XSHG")


def calculate_transaction_timing(*, profile: dict, submitted_at: datetime,
                                 transaction_type: str, channel: str = "off_exchange") -> dict:
    """核心计算可以独立测试。统一转换为北京时间，不依赖服务器所在时区。"""
    if submitted_at.tzinfo is None or submitted_at.utcoffset() is None:
        raise ValueError("下单时间必须包含时区，例如 +08:00")
    if transaction_type not in {"buy", "sell"} or channel not in {"off_exchange", "exchange"}:
        raise ValueError("交易类型或渠道无效")
    local = submitted_at.astimezone(CHINA_TIMEZONE)
    result = FundTransactionTimingResponse(
        fund_code=profile["fund_code"], submitted_at=local,
        transaction_type=transaction_type, channel=channel,
        rule_version=RULE_VERSION, status="manual_required", reason="",
        visibility="待对应净值披露、销售平台完成确认并更新后可见，不承诺具体时刻。",
        sources=SOURCES,
    )
    description = f"{profile.get('fund_type', '')} {profile.get('short_name', '')}".upper()
    # 当前档案没有逐产品的开放日/暂停申赎公告。特殊产品明确交回产品规则，不能
    # 把境外休市、封闭期或货币基金的收益分配硬套成 A 股 T+1。
    special = ("QDII", "FOF", "货币", "REIT", "海外", "全球", "港股", "沪港", "深港",
               "恒生", "纳斯达克", "标普", "日经", "定开", "定期开放", "持有期", "封闭")
    plain_etf = "ETF" in description and "联接" not in description
    if channel == "exchange":
        result.reason = "场内买卖以实际成交价格和时间计盈亏，不适用场外 15:00 净值规则。"
        return result.model_dump(mode="json")
    if plain_etf or any(word in description for word in special) or not any(
        word in description for word in ("股票", "混合", "债券", "指数")
    ):
        result.reason = "该产品需核对专属开放日、确认周期和收益规则，暂不套用普通场外 T+1。"
        return result.model_dump(mode="json")

    try:
        calendar = _calendar()
        result.calendar_through = calendar.last_session.date()
        day = pd.Timestamp(local.date())
        is_session = calendar.is_session(day)
        after_cutoff = local.time() >= time(15, 0)
        if is_session:
            nav_day = calendar.next_session(day) if after_cutoff else day
        else:
            nav_day = calendar.date_to_session(day, direction="next")
        next_day = calendar.next_session(nav_day)
    except (ValueError, KeyError, IndexError):
        # 日历覆盖范围是有限的；跨到未知年份时不降级成简单的“周一到周五”。
        result.status = "calendar_unavailable"
        result.reason = "交易日历未覆盖完整计算区间，请核对交易平台或更新交易日历。"
        return result.model_dump(mode="json")

    result.status = "estimated"
    result.nav_date = nav_day.date()
    result.confirmation_date = next_day.date()
    result.reason = ("非交易日申请，顺延至下一交易日。" if not is_session else
                     "15:00 及之后申请，按下一交易日估算。" if after_cutoff else
                     "交易日 15:00 前申请，按当日收盘净值估算。")
    if transaction_type == "buy":
        result.first_return_nav_date = next_day.date()
        result.warnings.append("新买份额不享有成交净值日之前的涨跌；首个收益可能为负，手续费也会影响盈亏。")
    else:
        result.last_return_nav_date = nav_day.date()
        result.warnings.append("卖出份额仍计入成交净值日的涨跌，此后不再计入；确认日不是赎回款到账日。")
    result.warnings.append("仅估算正常开放的存续场外基金；暂停申赎、渠道截止时间和回单可能改变日期。")
    if local.hour == 15 and local.minute == 0:
        result.warnings.append("15:00 边界按下一交易日保守估算，实际以销售平台受理回执为准。")
    return result.model_dump(mode="json")


def preview_transaction_timing(*, fund_code: str, submitted_at: datetime,
                               transaction_type: str, channel: str = "off_exchange") -> dict:
    """只读入口；预览将来的下单时间也不会生成任何流水或待处理订单。"""
    profile = fund_catalog_service.get_fund(fund_code)["profile"]
    return calculate_transaction_timing(profile=profile, submitted_at=submitted_at,
                                        transaction_type=transaction_type, channel=channel)

"""手动盘中行情：外部 Provider -> 校验单位/时间 -> 独立缓存 -> 页面。

这里只获取配置指数的一次快照，不轮询、不采集全市场个股、不调用模型。
不将盘中累计成交额与日线均值直接比较，也不把重叠指数相加为全市场成交额。
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError

from app.database import Database, runtime_database
from app.models import MarketIntradayCacheModel
from app.services.market_indices import MARKET_INDEX_DEFINITIONS
from app.services.market_turnover_history import index_points, save_points

SHANGHAI = ZoneInfo("Asia/Shanghai")
QUOTE_URL = "https://qt.gtimg.cn/"
SOURCE_NAME = "腾讯财经公开行情"
CACHE_ID = "major_indices"
REFRESH_INTERVAL_SECONDS = 30
LIMITATIONS = [
    "各行是数据源对该指数返回的累计成交口径，覆盖范围有重叠，不相加为全A股成交额。",
    "成交量单位为手，成交额单位为人民币元；盘中累计值不与日线5日均值混算。",
    "公开行情可能延迟；行情时间来自上游，不是本机抓取时间，不保证逐笔实时。",
]


class MarketIntradayError(ValueError):
    """第三方网络或字段异常，供页面显示可理解的失败原因。"""


def _number(value: str, *, positive: bool = False) -> float:
    # 缺失/NaN/负数不能伪装为成交量零；但盘前合法的零成交必须保留。
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError("非法行情数值")
    return result


def parse_quotes(raw: str, *, now: datetime) -> tuple[dict, dict]:
    """仅解析固定的行情赋值字符串，不执行第三方 JavaScript。

    腾讯页面自身映射：3=点位，4=昨收，30=上海时区行情时间，36=成交量(手)，
    37=成交额(万元)。入库统一把成交额乘10000转换成元，页面再格式化为亿元。
    字段依据及验证日期记录在 docs/盘中市场行情.md。
    """
    rows = {}
    for match in re.finditer(r'v_((?:sh|sz)\d{6})=("[^"\r\n]*");', raw):
        rows[match[1]] = json.loads(match[2]).split("~")
    quotes, errors = {}, {}
    for definition in MARKET_INDEX_DEFINITIONS:
        symbol = definition.provider_symbol
        try:
            fields = rows[symbol]
            if len(fields) < 38 or fields[2] != definition.index_code:
                raise ValueError("指数代码或字段不完整")
            quote_at = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
            if quote_at > now + timedelta(minutes=2):
                raise ValueError("行情时间来自未来")
            last = _number(fields[3], positive=True)
            previous = _number(fields[4], positive=True)
            amount = _number(fields[37]) * 10_000
            change = (last / previous - 1) * 100
            if not math.isfinite(amount) or not math.isfinite(change):
                raise ValueError("行情换算溢出")
            quotes[symbol] = {
                "last": last, "previous_close": previous,
                "change_pct": round(change, 4),
                "volume_lots": _number(fields[36]),
                "amount_cny": amount,
                "quote_at": quote_at.isoformat(),
                "fetched_at": now.isoformat(),
            }
        except (KeyError, ValueError, IndexError, TypeError):
            errors[symbol] = f"{definition.index_name}行情缺失或字段异常，保留上次数据。"
    return quotes, errors


class TencentIntradayProvider:
    """一次批量请求配置指数，固定地址、有限超时，无登录态或自动重试。"""

    def fetch(self, *, now: datetime) -> tuple[dict, dict]:
        try:
            symbols = ",".join(item.provider_symbol for item in MARKET_INDEX_DEFINITIONS)
            with httpx.Client(timeout=12, follow_redirects=False) as client:
                response = client.get(QUOTE_URL, params={"q": symbols})
                response.raise_for_status()
            return parse_quotes(response.content.decode("gb18030"), now=now)
        except (httpx.HTTPError, UnicodeError) as exc:
            raise MarketIntradayError("行情来源暂不可用，请稍后手动重试。") from exc


def _aware(value: datetime | None) -> datetime | None:
    # SQLite 读出的时间没有时区，数据库中的这两个系统时间均以UTC写入。
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def claim_market_refresh(database: Database, cache_id: str, now: datetime, interval: int) -> bool:
    """指数和全市场缓存共享的原子刷新闸门；不同cache_id互不阻塞。"""
    with database.session() as session:
        if session.get(MarketIntradayCacheModel, cache_id) is None:
            session.add(MarketIntradayCacheModel(id=cache_id, payload={}))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
    with database.session() as session, session.begin():
        return session.execute(update(MarketIntradayCacheModel).where(
            MarketIntradayCacheModel.id == cache_id,
            or_(MarketIntradayCacheModel.attempted_at.is_(None),
                MarketIntradayCacheModel.attempted_at <= now - timedelta(seconds=interval)),
        ).values(attempted_at=now)).rowcount == 1


def quote_freshness(quote_at: datetime, now: datetime) -> str:
    """不猜节假日：旧日期明确标为非今日；同日按行情时间判断是否落后。

    午休和收盘不要求数据不断跳动。这里判断的是快照时效，不宣布交易所正在开市。
    """
    current, quote = now.astimezone(SHANGHAI), quote_at.astimezone(SHANGHAI)
    if quote.date() != current.date():
        return "previous_date"
    minute = current.hour * 60 + current.minute
    # 午休对齐11:30，收盘后对齐15:00；其他时段允许5分钟来源延迟。
    reference = current
    if 690 <= minute < 780:
        reference = current.replace(hour=11, minute=30, second=0, microsecond=0)
    elif minute >= 900:
        reference = current.replace(hour=15, minute=0, second=0, microsecond=0)
    return "stale" if (reference - quote).total_seconds() > 300 else "recent"


class MarketIntradayService:
    """GET只读，POST手动刷新；共享数据库节流，部分失败保留对应指数旧快照。"""

    def __init__(self, database: Database = runtime_database, provider=None):
        self.database = database
        self.provider = provider or TencentIntradayProvider()
        self.database.create_schema()

    def overview(self, *, now: datetime | None = None) -> dict:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self.database.session() as session:
            row = session.get(MarketIntradayCacheModel, CACHE_ID)
            payload = row.payload if row else {}
            attempted, completed = (_aware(row.attempted_at), _aware(row.completed_at)) if row else (None, None)
        quotes, errors = payload.get("quotes", {}), payload.get("errors", {})
        items = []
        for definition in MARKET_INDEX_DEFINITIONS:
            symbol = definition.provider_symbol
            quote = quotes.get(symbol, {})
            error = errors.get(symbol, "")
            items.append({
                "index_code": definition.index_code, "index_name": definition.index_name,
                "source_url": f"https://gu.qq.com/{symbol}/zs", **quote,
                "status": ("cached_error" if quote else "error") if error else ("available" if quote else "not_synced"),
                "freshness": quote_freshness(datetime.fromisoformat(quote["quote_at"]), now) if quote else "missing",
                "error": error,
            })
        next_refresh = attempted + timedelta(seconds=REFRESH_INTERVAL_SECONDS) if attempted else None
        return {
            "data_kind": "market_intraday", "source_name": SOURCE_NAME,
            "checked_at": now, "attempted_at": attempted, "completed_at": completed,
            "next_refresh_at": next_refresh,
            "refreshing": bool(attempted and (not completed or attempted > completed) and now < next_refresh),
            "indices": items, "limitations": LIMITATIONS,
        }

    def refresh(self, *, now: datetime | None = None) -> dict:
        fixed_now = now.astimezone(timezone.utc) if now else None
        now = fixed_now or datetime.now(timezone.utc)
        # 建立单行后，用条件UPDATE原子抢占刷新权；网络等待不持有数据库事务。
        # 即使多个标签页/服务进程同时点击，也只有一个调用外部Provider。
        claimed = claim_market_refresh(self.database, CACHE_ID, now, REFRESH_INTERVAL_SECONDS)
        if not claimed:
            return self.overview(now=now)

        try:
            quotes, errors = self.provider.fetch(now=now)
        except MarketIntradayError as exc:
            quotes = {}
            errors = {item.provider_symbol: str(exc) for item in MARKET_INDEX_DEFINITIONS}
        with self.database.session() as session, session.begin():
            row = session.get(MarketIntradayCacheModel, CACHE_ID)
            # 请求过慢被后续刷新接替时，旧响应不能覆盖新响应。
            if _aware(row.attempted_at) == now:
                saved = dict((row.payload or {}).get("quotes", {}))
                previous_points = index_points(saved)
                for symbol, quote in quotes.items():
                    previous = saved.get(symbol)
                    if previous and quote["quote_at"] < previous["quote_at"]:
                        errors[symbol] = "来源行情时间回退，已保留较新的快照。"
                    else:
                        saved[symbol] = quote
                row.payload = {"quotes": saved, "errors": errors}
                # 只记录通过原有时间回退检查的数据；失败的指数继续保持旧时间。
                save_points(session, previous_points + index_points(saved), now=now)
                row.completed_at = fixed_now or datetime.now(timezone.utc)
        return self.overview(now=fixed_now)


market_intraday_service = MarketIntradayService()

"""成交额曲线的数据层：保存真实采样点，按序列和北京时间日期读取。

这里只保存/读取其他行情服务已经验收的数据，不访问外网、不补零或插值。
指数与全市场共用存储结构，但序列独立，绝不相加重叠的指数成交额。
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from app.database import Database, runtime_database
from app.models import MarketIntradayCacheModel, MarketTurnoverPointModel
from app.services.market_indices import MARKET_INDEX_DEFINITIONS

SHANGHAI = ZoneInfo("Asia/Shanghai")
HISTORY_DAYS = 90
SERIES = (
    {"series_id": "market:sh_sz", "label": "沪深两市A股", "group": "market"},
    {"series_id": "market:sh_sz_bj", "label": "沪深北A股", "group": "market"},
    {"series_id": "market:sh", "label": "沪市A股", "group": "market"},
    {"series_id": "market:sz", "label": "深市A股", "group": "market"},
    {"series_id": "market:bj", "label": "北交所A股", "group": "market"},
    *({"series_id": f"index:{item.provider_symbol}", "label": item.index_name, "group": "indices"}
      for item in MARKET_INDEX_DEFINITIONS),
)


def as_utc(value):
    """SQLite读出的时间可能没有时区；表内统一存UTC，来源字符串本身必须含时区。"""
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def index_points(quotes: dict) -> list[dict]:
    """只拆分已验收的白名单指数快照，来源时间不替换为本机当前时间。"""
    return [
        {"series_id": f"index:{item.provider_symbol}", "quote_at": quote["quote_at"],
         "fetched_at": quote["fetched_at"], "amount_cny": quote["amount_cny"], "security_count": None}
        for item in MARKET_INDEX_DEFINITIONS
        if (quote := quotes.get(item.provider_symbol))
    ]


def market_points(snapshot: dict | None) -> list[dict]:
    """拆分互斥市场汇总；合计与分市场均保留原本的来源参考时间。"""
    if not snapshot:
        return []
    counts = {item["exchange"]: item["security_count"] for item in snapshot["exchanges"]}
    values = [
        ("sh_sz", snapshot["sh_sz_amount_cny"], counts["sh"] + counts["sz"]),
        ("sh_sz_bj", snapshot["sh_sz_bj_amount_cny"], snapshot["security_count"]),
        *((item["exchange"], item["amount_cny"], item["security_count"]) for item in snapshot["exchanges"]),
    ]
    return [
        {"series_id": f"market:{key}", "quote_at": snapshot["reference_at"],
         "fetched_at": snapshot["fetched_at"], "amount_cny": amount, "security_count": count}
        for key, amount, count in values
    ]


def save_points(session, points: list[dict], *, now: datetime) -> None:
    """与快照更新使用同一个事务，避免出现最新数值与曲线相互矛盾。

    同时传入旧快照和新快照，可把升级前的最后一个点保留下来。相同来源时间只更新
    原来的点；过期/失败请求不能以新抓取时间生成一个假点。金额回落原样保留供核验。
    """
    cutoff = now.astimezone(SHANGHAI).date() - timedelta(days=HISTORY_DAYS - 1)
    for point in points:
        quote_at, fetched_at = as_utc(point["quote_at"]), as_utc(point["fetched_at"])
        trade_date = quote_at.astimezone(SHANGHAI).date()
        if trade_date < cutoff:
            continue
        row = session.get(MarketTurnoverPointModel, (point["series_id"], quote_at))
        if row is None:
            row = MarketTurnoverPointModel(series_id=point["series_id"], quote_at=quote_at)
            session.add(row)
        elif as_utc(row.fetched_at) > fetched_at:
            continue
        row.trade_date, row.fetched_at = trade_date, fetched_at
        row.amount_cny, row.security_count = point["amount_cny"], point["security_count"]
    # 只清理这次有写入的序列，不让两个独立刷新事务争抢全部历史行。
    ids = {point["series_id"] for point in points}
    if ids:
        session.execute(delete(MarketTurnoverPointModel).where(
            MarketTurnoverPointModel.series_id.in_(ids), MarketTurnoverPointModel.trade_date < cutoff,
        ))


class MarketTurnoverHistoryService:
    """GET只查询本地历史；未指定日期时展示该序列最新一个有记录的日期。"""

    def __init__(self, database: Database = runtime_database):
        self.database = database
        self.database.create_schema()

    def history(self, series_id="market:sh_sz", data_date: date | None = None, *, now=None):
        definition = next((item for item in SERIES if item["series_id"] == series_id), None)
        if definition is None:
            raise ValueError("不支持的成交额序列")
        today = (now or datetime.now(timezone.utc)).astimezone(SHANGHAI).date()
        cutoff = today - timedelta(days=HISTORY_DAYS - 1)
        with self.database.session() as session:
            # 兼容升级前的最后一份快照：GET只把它作为一个真实点合并展示，不写库。
            cache_key = "all_a_turnover" if definition["group"] == "market" else "major_indices"
            cache = session.get(MarketIntradayCacheModel, cache_key)
            payload = cache.payload if cache else {}
            cached = market_points(payload.get("snapshot")) if definition["group"] == "market" else index_points(payload.get("quotes", {}))
            fallback = next((p for p in cached if p["series_id"] == series_id), None)
            fallback_date = as_utc(fallback["quote_at"]).astimezone(SHANGHAI).date() if fallback else None
            dates = set(session.scalars(select(MarketTurnoverPointModel.trade_date).where(
                MarketTurnoverPointModel.series_id == series_id, MarketTurnoverPointModel.trade_date >= cutoff,
            ).distinct()))
            if fallback_date and fallback_date >= cutoff:
                dates.add(fallback_date)
            selected = data_date or max(dates, default=today)
            rows = session.scalars(select(MarketTurnoverPointModel).where(
                MarketTurnoverPointModel.series_id == series_id,
                MarketTurnoverPointModel.trade_date == selected,
                MarketTurnoverPointModel.trade_date >= cutoff,
            ).order_by(MarketTurnoverPointModel.quote_at)).all()
            points = {as_utc(row.quote_at): {
                "quote_at": as_utc(row.quote_at), "fetched_at": as_utc(row.fetched_at),
                "amount_cny": row.amount_cny, "security_count": row.security_count,
            } for row in rows}
            if fallback_date == selected and selected >= cutoff:
                key = as_utc(fallback["quote_at"])
                if key not in points or as_utc(fallback["fetched_at"]) >= points[key]["fetched_at"]:
                    points[key] = {**fallback, "quote_at": key, "fetched_at": as_utc(fallback["fetched_at"])}
        ordered = [points[key] for key in sorted(points)]
        warnings = []
        if any(b["amount_cny"] < a["amount_cny"] for a, b in zip(ordered, ordered[1:])):
            warnings.append("累计金额出现回落，可能是来源修正或覆盖变化，不表示资金净流出。")
        if len({p["security_count"] for p in ordered if p["security_count"] is not None}) > 1:
            warnings.append("采样期间覆盖证券数量发生变化，点间差额不完全来自新增成交。")
        return {
            **definition, "series": SERIES, "data_date": selected, "available_dates": sorted(dates, reverse=True),
            "is_today": selected == today, "points": ordered, "warnings": warnings,
            "sampling": "手动采样 · 非连续分时行情 · 仅连接已保存的观测值",
            "time_basis": ("北京时间；来源参考行情时间，非逐股时间核验；分页汇总非同一毫秒截面。"
                           if definition["group"] == "market" else "北京时间；腾讯来源行情时间，非本机采集时间。"),
            "retention_days": HISTORY_DAYS,
        }


market_turnover_history_service = MarketTurnoverHistoryService()

"""独立的日成交额历史：一条日线对应一个交易日，不使用盘中采样拼全天金额。

    腾讯日线的第5列是成交量（手），第8列才是成交额（万元）。同一日期的科创50
    原始金额已与中证官网tradingValue（亿元）交叉核对，细节见盘中市场行情文档。
"""
from datetime import date, datetime, timedelta, timezone
import math

import httpx

from app.database import Database, runtime_database
from app.models import MarketIntradayCacheModel
from app.services.market_indices import MARKET_INDEX_DEFINITIONS
from app.services.market_intraday import SHANGHAI, _aware, claim_market_refresh

URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
SOURCE_NAME = "腾讯财经日线行情"
COOLDOWN_SECONDS = 180
SERIES = (
    {"series_id": "reference:sh_sz", "label": "沪深成交额参考合计", "group": "reference",
     "symbols": ("sh000001", "sz399001")},
    *({"series_id": f"index:{item.provider_symbol}", "label": item.index_name, "group": "indices",
       "symbols": (item.provider_symbol,)} for item in MARKET_INDEX_DEFINITIONS),
)


class DailyTurnoverError(ValueError):
    """外部日线缺失或金额口径无法确认。"""


def definition_for(series_id):
    item = next((item for item in SERIES if item["series_id"] == series_id), None)
    if item is None:
        raise ValueError("不支持的日成交额序列")
    return item


def normalize_daily(data, symbol: str, start: date, end: date) -> list[dict]:
    """只取明确的日线成交额；缺失值不当零，剔除当日未完成日线。"""
    if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
        raise DailyTurnoverError("日线响应结构异常，未覆盖旧历史。")
    payload = data["data"].get(symbol)
    rows = payload.get("day") if isinstance(payload, dict) else None
    if data.get("code") != 0 or not isinstance(rows, list) or not rows:
        raise DailyTurnoverError("来源没有返回可用的日线成交额。")
    points = {}
    for raw in rows:
        try:
            day = date.fromisoformat(raw[0])
            if not start <= day <= end:
                continue
            amount = float(raw[8]) * 10000
            if isinstance(raw[8], bool) or not math.isfinite(amount) or amount < 0:
                raise ValueError()
        except (TypeError, ValueError, IndexError, KeyError) as exc:
            raise DailyTurnoverError("日线成交额字段缺失或单位无法核验，未使用成交量替代。") from exc
        key = day.isoformat()
        if key in points and points[key] != amount:
            raise DailyTurnoverError("同一交易日存在冲突金额，未覆盖旧历史。")
        points[key] = amount
    if not points:
        raise DailyTurnoverError("指定范围内没有已结束交易日的成交额。")
    return [{"date": day, "amount_cny": points[day]} for day in sorted(points)]


class TencentDailyTurnoverProvider:
    def fetch(self, definition, *, end: date) -> list[dict]:
        # 一次刷新最多两个白名单指数；不访问几千只个股，不尝试绕过来源限制。
        start = end - timedelta(days=119)
        histories = []
        try:
            with httpx.Client(timeout=12, follow_redirects=False) as client:
                for symbol in definition["symbols"]:
                    response = client.get(URL, params={"param": f"{symbol},day,{start},{end},160,qfq"})
                    response.raise_for_status()
                    histories.append(normalize_daily(response.json(), symbol, start, end))
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            if isinstance(exc, DailyTurnoverError):
                raise
            raise DailyTurnoverError("日成交额来源暂不可用，请稍后手动重试。") from exc
        if len(histories) == 1:
            return histories[0]
        indexed = [{point["date"]: point["amount_cny"] for point in items} for items in histories]
        # 沪深参考合计必须同日两项都存在；单边缺失显示断点，而不是悄悄补零。
        try:
            return [{"date": day, "amount_cny": math.fsum(item[day] for item in indexed)
                     if all(day in item for item in indexed) else None}
                    for day in sorted(set().union(*(set(item) for item in indexed)))]
        except OverflowError as exc:
            raise DailyTurnoverError("日线合计金额溢出，未覆盖旧历史。") from exc


class MarketDailyTurnoverService:
    """复用缓存表存小规模日线序列；打开/切换只读，手动按钮才请求外部历史。"""

    def __init__(self, database: Database = runtime_database, provider=None):
        self.database, self.provider = database, provider or TencentDailyTurnoverProvider()
        self.database.create_schema()

    def overview(self, series_id="reference:sh_sz", period="3m", *, now=None):
        definition = definition_for(series_id)
        if period not in {"30d", "3m"}:
            raise ValueError("周期只支持近30日或近3个月")
        now = now or datetime.now(timezone.utc)
        end = now.astimezone(SHANGHAI).date() - timedelta(days=1)
        start = end - timedelta(days=29 if period == "30d" else 89)
        with self.database.session() as session:
            row = session.get(MarketIntradayCacheModel, "daily:" + series_id)
            payload = row.payload if row else {}
            attempted, completed = (_aware(row.attempted_at), _aware(row.completed_at)) if row else (None, None)
        points = [p for p in payload.get("points", []) if start.isoformat() <= p["date"] <= end.isoformat()]
        error = payload.get("error", "")
        warnings = ["部分日期缺少沪或深的日线金额，合计留空，不以单边数据代替。"] if any(p["amount_cny"] is None for p in points) else []
        return {
            "series_id": series_id, "label": definition["label"], "group": definition["group"],
            "series": [{k: item[k] for k in ("series_id", "label", "group")} for item in SERIES],
            "period": period, "range_start": start, "range_end": end,
            "data_date": points[-1]["date"] if points else None, "points": points,
            "status": ("cached_error" if points else "error") if error else ("available" if points else "not_synced"),
            "error": error, "warnings": warnings,
            "fetched_at": payload.get("fetched_at"), "attempted_at": attempted,
            "refreshing": bool(attempted and (not completed or attempted > completed) and (now - attempted).total_seconds() < 30),
            "source_name": SOURCE_NAME, "source_url": f"https://gu.qq.com/{definition['symbols'][0]}/zs",
            "scope": ("上证指数与深证成指的同日日线成交额相加，为沪深行情口径参考值；不等于上方逐股统计的全市场A股合计，不含北交所。"
                      if definition["group"] == "reference" else "数据源返回的该指数日线成交额，不表示整个板块成交额。"),
            "methodology": "每个交易日一个点；仅显示今日之前的日线，不含今日未最终确认金额。近30日/近3个月分别为30/90个自然日范围；休市日不补零。",
        }

    def refresh(self, series_id="reference:sh_sz", period="3m", *, now=None):
        definition = definition_for(series_id)
        self.overview(series_id, period, now=now)  # 在产生采集副作用之前校验周期。
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        key = "daily:" + series_id
        if not claim_market_refresh(self.database, key, now, COOLDOWN_SECONDS):
            return self.overview(series_id, period, now=now)
        points, error = None, ""
        try:
            points = self.provider.fetch(definition, end=now.astimezone(SHANGHAI).date() - timedelta(days=1))
        except DailyTurnoverError as exc:
            error = str(exc)
        with self.database.session() as session, session.begin():
            row = session.get(MarketIntradayCacheModel, key)
            if _aware(row.attempted_at) == now:
                old = row.payload or {}
                if points and old.get("points") and points[-1]["date"] < old["points"][-1]["date"]:
                    points, error = None, "来源最新日期回退，保留原有日线。"
                payload = {**old, "error": error}
                if points:
                    payload.update(points=points, fetched_at=datetime.now(timezone.utc).isoformat())
                row.payload, row.completed_at = payload, datetime.now(timezone.utc)
        return self.overview(series_id, period, now=now)


market_daily_turnover_service = MarketDailyTurnoverService()

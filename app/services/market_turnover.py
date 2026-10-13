"""全市场A股成交额：完整读取行情列表、按证券去重、分市场求和。

这里只持久化汇总、覆盖数量及时间，不保存几千条个股行情，不复用指数成交额。
每次刷新有总耗时/页数上限；失败保留完整的旧批次，绝不发布漏页的“全市场”。
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.database import Database, runtime_database
from app.models import MarketIntradayCacheModel
from app.services.market_intraday import SHANGHAI, _aware, claim_market_refresh, quote_freshness
from app.services.market_turnover_history import market_points, save_points

BASE_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
SOURCE_URL = "https://vip.stock.finance.sina.com.cn/mkt/#hs_a"
CACHE_ID = "all_a_turnover"
COOLDOWN_SECONDS = 180
MAX_FETCH_SECONDS = 45
PAGE_SIZE = 100
EXCHANGES = {"sh": "沪市A股", "sz": "深市A股", "bj": "北交所A股"}


class MarketTurnoverError(ValueError):
    """列表覆盖、金额、来源时钟或外部连接不可用。"""


def aggregate_turnover(rows: list[dict], *, expected_count: int, reference_at: datetime,
                       started_at: datetime, fetched_at: datetime) -> dict:
    """仅接受完整列表，三个市场的证券集合互斥，合计不重复。

    新浪列表只给逐股HH:MM:SS，没有逐股日期。reference_at是同源大盘的行情日期
    参考，不冒充每只股票独立确认的交易日期；页面显示采集窗口及这项限制。
    """
    unique = {}
    for row in rows:
        if not isinstance(row, dict):
            raise MarketTurnoverError("来源证券记录格式异常，未发布新合计。")
        symbol = str(row.get("symbol", ""))
        # hs_a已经由来源筛选A股，再检查代码避免基金、B股或未知市场混入。
        if not re.fullmatch(r"(?:sh6\d{5}|sz[03]\d{5}|bj[489]\d{5})", symbol):
            raise MarketTurnoverError("来源包含无法确认的A股代码，未发布新合计。")
        if symbol in unique and unique[symbol] != row:
            raise MarketTurnoverError("分页出现同一证券的冲突记录，未发布新合计。")
        unique[symbol] = row
    if len(unique) != expected_count or expected_count <= 0:
        raise MarketTurnoverError(f"行情列表不完整：去重后{len(unique)}条，来源应有{expected_count}条。")

    groups = {key: {"exchange": key, "label": label, "amounts": [], "security_count": 0}
              for key, label in EXCHANGES.items()}
    for symbol, row in unique.items():
        value = row.get("amount")
        try:
            amount = float(value)
            if isinstance(value, bool) or not math.isfinite(amount) or amount < 0:
                raise ValueError()
        except (TypeError, ValueError):
            # 不把停牌/缺失字段直接补零：来源明确给出0才视为零成交。
            raise MarketTurnoverError("部分证券成交额缺失，未将缺失值当零计入全市场。") from None
        group = groups[symbol[:2]]
        group["amounts"].append(amount)
        group["security_count"] += 1
    if any(not group["security_count"] for group in groups.values()):
        raise MarketTurnoverError("来源未覆盖沪、深、北全部市场，未发布新合计。")
    try:
        exchanges = [{"exchange": key, "label": group["label"],
                      "amount_cny": math.fsum(group["amounts"]), "security_count": group["security_count"]}
                     for key, group in groups.items()]
        totals = {item["exchange"]: item["amount_cny"] for item in exchanges}
        two_markets = math.fsum([totals["sh"], totals["sz"]])
        three_markets = math.fsum(totals.values())
    except OverflowError as exc:
        raise MarketTurnoverError("来源成交额汇总溢出，未发布新合计。") from exc
    return {
        "sh_sz_amount_cny": two_markets,
        "sh_sz_bj_amount_cny": three_markets,
        "exchanges": exchanges, "security_count": len(unique), "expected_count": expected_count,
        "reference_at": reference_at.isoformat(), "started_at": started_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
    }


class SinaMarketTurnoverProvider:
    """按代码稳定排序分页，顺序低并发采集，不重试、不绕过风控。"""

    def fetch(self, *, now: datetime) -> dict:
        deadline = time.monotonic() + MAX_FETCH_SECONDS
        try:
            with httpx.Client(follow_redirects=False) as client:
                def get(url, **kwargs):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MarketTurnoverError("全市场采集超时，保留上次完整快照。")
                    response = client.get(url, timeout=min(8, remaining), **kwargs)
                    response.raise_for_status()
                    return response

                def clock():
                    response = get("https://hq.sinajs.cn/list=sh000001", headers={"Referer": "https://finance.sina.com.cn/"})
                    match = re.search(r'hq_str_sh000001="([^"\r\n]+)"', response.content.decode("gb18030"))
                    fields = match[1].split(",") if match else []
                    return datetime.fromisoformat(f"{fields[30]}T{fields[31]}").replace(tzinfo=SHANGHAI)

                reference = clock()
                count_url = BASE_URL + "Market_Center.getHQNodeStockCount"
                expected = int(get(count_url, params={"node": "hs_a"}).json())
                if not 0 < expected <= 8000:
                    raise MarketTurnoverError("行情列表规模异常，停止采集。")
                rows = []
                for page in range(1, math.ceil(expected / PAGE_SIZE) + 1):
                    batch = get(BASE_URL + "Market_Center.getHQNodeData", params={
                        "page": page, "num": PAGE_SIZE, "sort": "symbol", "asc": 1,
                        "node": "hs_a", "symbol": "", "_s_r_a": "page",
                    }).json()
                    if not isinstance(batch, list) or len(batch) != min(PAGE_SIZE, expected - len(rows)):
                        raise MarketTurnoverError("来源分页缺失或条数变化，保留上次完整快照。")
                    rows.extend(batch)
                # 首尾总数与日期一致才汇总，避免上市变动或跨日采集拼成一个截面。
                if int(get(count_url, params={"node": "hs_a"}).json()) != expected:
                    raise MarketTurnoverError("采集期间证券数量发生变化，请稍后重试。")
                end_reference = clock()
                finished = datetime.now(timezone.utc)
                if (reference.date() != end_reference.date() or end_reference < reference
                        or end_reference > finished + timedelta(minutes=2)):
                    raise MarketTurnoverError("来源日期跨日、回退或异常，未发布新合计。")
                return aggregate_turnover(rows, expected_count=expected, reference_at=end_reference,
                                          started_at=now, fetched_at=finished)
        except MarketTurnoverError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, UnicodeError) as exc:
            raise MarketTurnoverError("全市场行情来源暂不可用，请稍后手动重试。") from exc


class MarketTurnoverService:
    """复用盘中缓存表的独立记录，不改变指数刷新或日线研究。"""

    def __init__(self, database: Database = runtime_database, provider=None):
        self.database, self.provider = database, provider or SinaMarketTurnoverProvider()
        self.database.create_schema()

    def overview(self, *, now: datetime | None = None):
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self.database.session() as session:
            row = session.get(MarketIntradayCacheModel, CACHE_ID)
            payload = row.payload if row else {}
            attempted = _aware(row.attempted_at) if row else None
            completed = _aware(row.completed_at) if row else None
        snapshot, error = payload.get("snapshot"), payload.get("error", "")
        return {
            "data_kind": "market_turnover", "source_name": "新浪财经A股行情列表汇总", "source_url": SOURCE_URL,
            "status": ("cached_error" if snapshot else "error") if error else ("available" if snapshot else "not_synced"),
            "snapshot": snapshot, "error": error, "attempted_at": attempted,
            "next_refresh_at": attempted + timedelta(seconds=COOLDOWN_SECONDS) if attempted else None,
            "refreshing": bool(attempted and (not completed or attempted > completed) and (now - attempted).total_seconds() < MAX_FETCH_SECONDS + 10),
            "freshness": quote_freshness(datetime.fromisoformat(snapshot["reference_at"]), now) if snapshot else "missing",
            "scope": "沪市A股（含科创板）+深市A股（含创业板）+北交所A股；不含B股、基金、债券。",
            "time_basis": "行情日期参考同源上证行情；个股列表仅提供时分秒，非逐股日期核验。",
            "limitation": "分页行情列表汇总，并非同一毫秒截面或交易所最终结算统计。覆盖数以来源A股列表为准。",
        }

    def refresh(self, *, now: datetime | None = None):
        fixed_now = now.astimezone(timezone.utc) if now else None
        now = fixed_now or datetime.now(timezone.utc)
        if not claim_market_refresh(self.database, CACHE_ID, now, COOLDOWN_SECONDS):
            return self.overview(now=now)
        snapshot, error = None, ""
        try:
            snapshot = self.provider.fetch(now=now)
        except MarketTurnoverError as exc:
            error = str(exc)
        with self.database.session() as session, session.begin():
            row = session.get(MarketIntradayCacheModel, CACHE_ID)
            if _aware(row.attempted_at) == now:
                previous = (row.payload or {}).get("snapshot")
                if (snapshot and previous and datetime.fromisoformat(snapshot["reference_at"])
                        < datetime.fromisoformat(previous["reference_at"])):
                    snapshot, error = None, "来源行情时间回退，保留上次完整快照。"
                row.payload = {"snapshot": snapshot or previous, "error": error}
                # 最新快照与曲线在同一事务提交；失败时最多补存升级前已有的真实点。
                save_points(session, market_points(previous) + market_points(snapshot), now=now)
                row.completed_at = fixed_now or datetime.now(timezone.utc)
        return self.overview(now=fixed_now)


market_turnover_service = MarketTurnoverService()

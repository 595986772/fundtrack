"""宝妈 / 韭菜指数接入：读取发布者结果，而不是重新发明两个同名指标。

韭菜50读取作者公开的历史指数文件，仅保留指数序列，不输出个股明细。
宝妈开源框架目前没有真实公开数据流，明确缺失，不导入其合成样本。
GET 和 Agent 只读数据库；只有显式刷新才访问固定公开地址，不调用模型或社媒。
"""
from __future__ import annotations

import calendar
import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from app.database import Database, runtime_database
from app.models import RetailIndexCacheModel


SOURCE_URL = "https://indices-toocoldcc.pages.dev/data/bagholder50.json"
SOURCE_PAGE = "https://indices-toocoldcc.pages.dev/"
METHOD_URL = "https://github.com/toocoldcc/bagholder50/blob/main/docs/methodology.md"
MOM_URL = "https://github.com/mihang123/mom-index"
SHANGHAI = ZoneInfo("Asia/Shanghai")


class RetailIndexError(ValueError):
    """来源不可用或格式变化时的业务错误，不向页面暴露底层异常。"""


def _number(value: object) -> float:
    # bool 在 Python 中也是 int，但不能把 true 当成指数点位 1。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RetailIndexError("指数点位格式有误，保留上次数据。")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise RetailIndexError("指数点位必须是有效正数，保留上次数据。")
    return result


def _date(value: object) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise RetailIndexError("来源日期格式发生变化，保留上次数据。") from exc


def _three_months_before(day: date) -> date:
    # 三个月按自然月计算，不偷换成 60 个交易日或固定 90 天。
    month_index = day.year * 12 + day.month - 1 - 3
    year, month = divmod(month_index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def parse_bagholder_series(raw: bytes, *, today: date) -> dict:
    """把发布者文件转换为本项目契约，窗口始终以来源末日为准。"""
    try:
        data = json.loads(raw)
        meta = data["meta"]
        if meta["internal_code"] != "bagholder50_index":
            raise RetailIndexError("返回的不是韭菜50指数，已停止更新。")
        # 点位直接使用完整序列的 close，meta.metrics 中的展示值精度更低。
        points = [{"date": _date(row["time"]).isoformat(), "value": _number(row["close"])}
                  for row in data["ohlc"]]
        dates = [point["date"] for point in points]
        if not dates or dates != sorted(set(dates)):
            raise RetailIndexError("指数日期为空、重复或乱序，保留上次数据。")
        end = _date(meta["data_end_date"])
        if dates[-1] != end.isoformat() or end > today:
            raise RetailIndexError("指数末日不一致或来自未来，保留上次数据。")
        publication_date = _date(meta["post_publication_start_date"])
        # 上游的基点定义也校验，避免误把 0-100 情绪分作为这条理论指数入库。
        if _date(meta["base_date"]) != date(2016, 1, 4) or _number(meta["base_point"]) != 1000:
            raise RetailIndexError("指数基点定义已变化，需要核对新版本。")
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RetailIndexError("指数来源格式已变化，保留上次数据并稍后重试。") from exc

    returns = {}
    for key, label, offset in [("1d", "近1交易日", 1), ("5d", "近5交易日", 5),
                               ("20d", "近20交易日", 20), ("30d", "近30日", None),
                               ("3m", "近3个月", None)]:
        # 自然日窗口取目标日当天或之前最后一个观测点；缺基点返回 None，不填 0。
        base = points[-offset - 1] if offset and len(points) > offset else None
        if offset is None:
            target = end - timedelta(days=30) if key == "30d" else _three_months_before(end)
            base = next((p for p in reversed(points) if p["date"] <= target.isoformat()), None)
        returns[key] = {
            "label": label,
            "value_pct": round((points[-1]["value"] / base["value"] - 1) * 100, 4) if base else None,
            "start_date": base["date"] if base else None,
            "end_date": end.isoformat(),
        }

    return {
        "indicator_id": "bagholder50", "name": "韭菜指数", "version": "冷西西韭菜50",
        "kind": "theoretical_index", "unit": "点", "value": points[-1]["value"],
        "data_date": end.isoformat(), "returns": returns,
        "history": [p for p in points if p["date"] >= _three_months_before(end).isoformat()],
        "publication_date": publication_date.isoformat(),
        "source_name": "冷西西公开指数", "source_url": SOURCE_PAGE,
        "data_url": SOURCE_URL, "methodology_url": METHOD_URL,
        "source_hash": hashlib.sha256(raw).hexdigest(),
        "evidence_id": f"market:retail:bagholder50:{end}:{hashlib.sha256(raw).hexdigest()[:12]}",
        "summary": "跟踪每天筛选的50只高拥挤股票的理论等权表现；点位不是0-100情绪分。",
        "methodology": "四因子：追涨热度、换手放大、龙虎榜频次、大单流量拥挤度。2016-01-04以1000点起算。",
        "limitations": [
            "理论毛收益，不包含费用、滑点和真实成交约束；不代表投资者实际收益。",
            f"{publication_date.isoformat()}之前属于模拟回测，之后是公开规则发布后表现。",
            "涨跌幅表示这篮子股票的指数表现，不表示散户情绪升降或未来涨跌。",
            "近30日和近3个月按自然日/自然月取起点之前最近观测日，其余按交易观测点。",
        ],
    }


def fetch_bagholder_series() -> bytes:
    """只请求作者的一个公开文件，无 Cookie、API Key、重定向或自动重试。"""
    try:
        with httpx.stream("GET", SOURCE_URL, timeout=20, follow_redirects=False) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 1_000_000:
                    raise RetailIndexError("指数文件超出预期大小，停止下载。")
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError as exc:
        raise RetailIndexError("无法连接韭菜50公开数据源，请稍后手动重试。") from exc


def missing_mom_index() -> dict:
    """不把开源仓库中的合成样本或无样本时的中性值伪装成真实指数。"""
    return {
        "indicator_id": "mom", "name": "宝妈指数", "version": "Mom Index 公开框架 v0.1",
        "kind": "sentiment_score", "status": "source_unavailable", "unit": "分",
        "value": None, "data_date": None, "returns": {}, "history": [],
        "source_name": "mihang123 / mom-index", "source_url": MOM_URL,
        "methodology_url": MOM_URL, "data_url": None, "source_hash": None,
        "evidence_id": None, "publication_date": None, "fetched_at": None,
        "attempted_at": None, "last_error": "",
        "summary": "尚未接通真实数据源。所选开源框架只公开合成演示样本，没有可直接读取的真实指数接口。",
        "methodology": "观察新手讨论、追涨和恐慌等文本信号，不识别发帖者的真实身份。",
        "limitations": ["需要指定可用的真实发布源后接通；目前不生成分数、不采集小红书。"],
    }


class RetailIndexService:
    """管理只读快照和显式刷新；与新闻采集、AI 研判互不触发。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def overview(self, *, now: datetime | None = None, include_history: bool = True) -> dict:
        now = now or datetime.now(timezone.utc)
        with self.database.session() as session:
            row = session.get(RetailIndexCacheModel, "bagholder50")
            item = dict(row.payload) if row and row.payload else {
                "indicator_id": "bagholder50", "name": "韭菜指数", "version": "冷西西韭菜50",
                "kind": "theoretical_index", "unit": "点", "value": None, "data_date": None,
                "returns": {}, "history": [], "source_name": "冷西西公开指数",
                "source_url": SOURCE_PAGE, "data_url": SOURCE_URL, "methodology_url": METHOD_URL,
                "summary": "尚未更新。手动更新后读取作者已发布的真实指数序列。",
                "methodology": "高拥挤股票的理论等权指数，不是0-100情绪分。",
                "limitations": [], "evidence_id": None, "source_hash": None, "publication_date": None,
            }
            # 不宣称盘中实时：超过四个自然日没有新观测即标记偏旧，日期始终可见。
            item["status"] = "available" if item["value"] is not None else "not_synced"
            if item["data_date"] and (now.astimezone(SHANGHAI).date() - _date(item["data_date"])).days > 4:
                item["status"] = "stale"
            if row and row.error:
                item["status"] = "cached_error" if item["value"] is not None else "error"
            item.update(fetched_at=row.fetched_at if row else None,
                        attempted_at=row.attempted_at if row else None,
                        last_error=row.error if row else "")
        if not include_history:
            item.pop("history", None)
        return {"data_kind": "retail_indices", "indicators": [missing_mom_index(), item]}

    def refresh(self) -> dict:
        now = datetime.now(timezone.utc)
        # 连续点击只读一分钟内的结果，限制请求量；不创建常驻轮询或自动采集任务。
        with self.database.session() as session:
            row = session.get(RetailIndexCacheModel, "bagholder50")
            if row and (now - row.attempted_at.replace(tzinfo=timezone.utc)).total_seconds() < 60:
                return self.overview(now=now)
        payload, error = None, ""
        try:
            payload = parse_bagholder_series(fetch_bagholder_series(), today=now.astimezone(SHANGHAI).date())
        except RetailIndexError as exc:
            error = str(exc)
        with self.database.session() as session, session.begin():
            row = session.get(RetailIndexCacheModel, "bagholder50")
            if row is None:
                row = RetailIndexCacheModel(indicator_id="bagholder50")
                session.add(row)
            # 来源回退不能抹掉已保存的新快照，也不能更新成一次成功抓取。
            if payload and row.payload and payload["data_date"] < row.payload["data_date"]:
                payload, error = None, "来源日期比已保存数据更旧，已保留原快照。"
            row.attempted_at, row.error = now, error
            if payload:
                row.payload, row.fetched_at = payload, now
        return self.overview(now=now)


retail_index_service = RetailIndexService()

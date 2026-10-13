"""简报的数据准备：逐源刷新、保存断点、检查日期，最后交给原投研图冻结。

普通场外基金在下午没有当日正式净值。本模块同时保存“最近披露净值”和“当日指数
行情”，绝不把指数涨跌冒充基金实时收益。所有来源失败和降级都会随报告保存。
"""

from copy import deepcopy
from datetime import date, datetime, timedelta
from math import isfinite

from fastapi.encoders import jsonable_encoder

from app.services.ai_governance_context import AIControlError
from app.services.daily_briefings import LAST_START, china_now
from app.services.fund_transaction_timing import CHINA_TIMEZONE, _calendar
from app.services.market_intraday import quote_freshness


class BriefingDataUnavailable(RuntimeError):
    pass


def _time(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value))
        return result.astimezone(CHINA_TIMEZONE) if result.tzinfo else None
    except ValueError:
        return None


def check_inputs(*, funds, indices, trade_date: str, captured_at: datetime, calendar=None):
    """关键数据闸门。缺净值/仅有旧行情时停止模型环节，而不是写一份貌似正常的简报。"""
    calendar = calendar or _calendar()
    warnings, usable = [], []
    # 净值允许两次已结束交易日的披露滞后，但必须明示日期；特殊基金可能仍需人工核对。
    previous = calendar.previous_session(trade_date)
    oldest = calendar.previous_session(previous).date()
    for fund in funds:
        try:
            nav_date = date.fromisoformat(str(fund.get("latest_nav_date")))
        except ValueError:
            nav_date = None
        nav = fund.get("latest_nav")
        if (not nav_date or not oldest <= nav_date <= date.fromisoformat(trade_date)
                or not isinstance(nav, (float, int)) or isinstance(nav, bool) or not isfinite(nav) or nav <= 0):
            warnings.append(f"{fund['fund_code']} 的披露净值缺失或超过两交易日，需核对。")
    blocked_funds = bool(warnings) or not funds
    for item in indices:
        quoted = _time(item.get("quote_at"))
        value = item.get("change_pct")
        if (quoted and quoted.date().isoformat() == trade_date and quoted <= captured_at + timedelta(minutes=2)
                and quote_freshness(quoted, captured_at) == "recent"
                and isinstance(value, (float, int)) and not isinstance(value, bool) and isfinite(value)):
            usable.append(item)
    if not usable:
        warnings.append("未取得当日有效指数行情，停止生成盘中 AI 结论。")
    elif len(usable) < len(indices):
        warnings.append("部分指数行情缺失或过期，未纳入本次判断。")
    return {"status": "blocked" if blocked_funds or not usable else "ready", "warnings": warnings}, usable


class DailyBriefingData:
    def __init__(self, *, stages, funds, clock=china_now, calendar=None):
        self.stages, self.funds, self.clock, self.calendar = stages, funds, clock, calendar

    def prepare(self, *, record, store, check):
        options = record.payload["daily_briefing"]
        current = store.get(user_id=record.user_id, job_id=record.id)
        data = deepcopy((current.result or {}).get("daily_briefing_input") or {})
        if data.get("gate", {}).get("status") == "ready":
            return data
        if data.get("gate", {}).get("status") == "blocked":
            raise BriefingDataUnavailable("本次数据检查未通过，请查看简报的数据状态。")
        now = self.clock()
        if now.date().isoformat() != options["trade_date"] or (not data and (
            options["trigger"] == "scheduled" and now.time() >= LAST_START
        )):
            raise BriefingDataUnavailable("任务排队后错过当日自动启动窗口，未刷新或调用模型。")
        data.setdefault("trade_date", options["trade_date"])
        data.setdefault("sources", [])
        data.setdefault("warnings", [])

        def save(message):
            check()
            latest = store.get(user_id=record.user_id, job_id=record.id)
            store.update(user_id=record.user_id, job_id=record.id,
                         changes={"result": {**(latest.result or {}), "daily_briefing_input": jsonable_encoder(data)},
                                  "progress_message": message})

        # 成功或失败的来源都记录下来。Worker 接管后不重复扫已完成来源，避免反复收费。
        for key, label, fetch in self.stages:
            check()
            if key in data:
                continue
            save(f"准备每日简报：{label}")
            started = self.clock()
            try:
                value = jsonable_encoder(fetch())
                data[key] = value
                status = value.get("stage_status", "ready")
                if status != "ready":
                    data["warnings"].append(f"{label}部分不可用，保留可核验数据。")
            except AIControlError:
                raise
            except Exception as exc:
                check()  # 取消、租约失效、总超时不能被来源降级吞掉。
                data[key], status = {}, "failed"
                data["warnings"].append(f"{label}刷新失败（{type(exc).__name__}），本次不补造数据。")
            data["sources"].append({"key": key, "label": label, "status": status,
                "started_at": started.isoformat(), "finished_at": self.clock().isoformat()})
            save(f"每日简报：{label}已处理")

        check()
        data["funds"] = jsonable_encoder(self.funds())
        captured = self.clock()
        all_quotes = data.get("indices", {}).get("items", [])
        try:
            gate, usable = check_inputs(funds=data["funds"], indices=all_quotes,
                trade_date=options["trade_date"], captured_at=captured, calendar=self.calendar)
        except (ValueError, KeyError, IndexError):
            gate, usable = {"status": "blocked", "warnings": ["交易日历不能验证数据日期，停止研判。"]}, []
        # 长时间中断后的未冻结输入不能拼上次日的新数据。
        if captured.date().isoformat() != options["trade_date"]:
            gate = {"status": "blocked", "warnings": ["任务跨交易日，停止生成混合时点的简报。"]}
        data.setdefault("indices", {})["items"] = usable
        for source in data["sources"]:
            if source["key"] == "indices":
                partial = len(usable) < len(all_quotes) or data["indices"].get("stage_status", "ready") != "ready"
                source["status"] = "failed" if not usable else "partial" if partial else "ready"
        data["captured_at"], data["gate"] = captured.isoformat(), gate
        data["warnings"] = list(dict.fromkeys([*data["warnings"], *gate["warnings"],
            "基金涨跌为最近已披露净值的变化，不是当日盘中基金估值。",
            "热点热度代表报道关注度，不代表资金流向或未来收益。",
            "本次不自动采集小红书或其他需登录的社媒。",
        ]))
        data["evidence"] = briefing_evidence(data)
        save("每日简报数据已冻结，开始组合研判" if gate["status"] == "ready" else "数据检查未通过，未调用研判模型")
        if gate["status"] != "ready":
            raise BriefingDataUnavailable("关键数据缺失或过期，未调用研判模型，请查看数据状态。")
        return data


def briefing_evidence(data):
    """Python 给数据发引用编号，模型只能引用这些事实，不能临时编数字。"""
    items = []
    for fund in data.get("funds", []):
        growth = fund.get("daily_growth_rate_pct")
        items.append({"evidence_id": f"briefing:nav:{fund['fund_code']}:{fund['latest_nav_date']}",
            "kind": "published_fund_nav", "title": f"{fund['short_name']} 最近披露净值",
            "value_text": f"净值 {fund['latest_nav']}；披露日涨跌 {growth}%" if growth is not None else f"净值 {fund['latest_nav']}；涨跌未提供",
            "summary": "正式披露净值，并非当日盘中估值。", "data_date": str(fund["latest_nav_date"]),
            "source_name": fund.get("source"), "fund_codes": [fund["fund_code"]], "direction": "neutral"})
    for quote in data.get("indices", {}).get("items", []):
        items.append({"evidence_id": f"briefing:index:{quote['index_code']}:{quote['quote_at']}",
            "kind": "intraday_index", "title": quote["index_name"], "value_text": f"涨跌 {quote['change_pct']:+.2f}%",
            "summary": "指数行情不等于基金实盘收益。", "data_date": quote["quote_at"],
            "source_name": data["indices"].get("source_name"), "source_url": quote.get("source_url"),
            "fund_codes": [], "direction": "neutral"})
    turnover = data.get("turnover", {})
    if turnover.get("snapshot") and turnover.get("stage_status") == "ready":
        snapshot = turnover["snapshot"]
        items.append({"evidence_id": f"briefing:turnover:{snapshot['reference_at']}", "kind": "market_turnover",
            "title": "沪深 A 股成交额", "value_text": f"{snapshot['sh_sz_amount_cny'] / 1e8:.2f} 亿元",
            "summary": turnover.get("limitation"), "data_date": snapshot["reference_at"],
            "source_name": turnover.get("source_name"), "source_url": turnover.get("source_url"),
            "fund_codes": [], "direction": "neutral"})
    for hotspot in data.get("hotspots", {}).get("items", [])[:6]:
        items.append({"evidence_id": f"briefing:hotspot:{hotspot['cluster_id']}", "kind": "hotspot_attention",
            "title": hotspot["title"], "value_text": f"报道 {hotspot['member_count']}；独立来源 {hotspot['source_count']}",
            "summary": hotspot["summary"], "data_date": hotspot["event_end"],
            "source_name": "、".join(hotspot.get("source_names", [])), "fund_codes": [], "direction": "neutral"})
    return items


def prepare_daily_briefing(*, record, store, check):
    """生产适配器：复用已有采集器。测试直接注入固定 stages，不访问外网。"""
    from app.services.fund_data_sync import fund_nav_sync_service
    from app.services.funds import fund_catalog_service
    from app.services.market_event_sync import market_event_sync_service
    from app.services.market_event_clusters import market_event_cluster_service
    from app.services.market_hotspots import market_hotspot_tracking_service
    from app.services.market_intraday import market_intraday_service
    from app.services.market_turnover import market_turnover_service
    from app.services.portfolios import portfolio_service
    from app.services.vector_rag import DashScopeEmbeddingClient

    portfolio = portfolio_service.get(user_id=record.user_id, portfolio_id=record.payload["portfolio_id"])
    codes = [item["fund_code"] for item in portfolio["holdings"]]

    def funds():
        result = []
        for code in codes:
            item = fund_catalog_service.get_fund(code)
            result.append({"fund_code": code, "short_name": item["profile"]["short_name"], **{
                key: item.get(key) for key in ("latest_nav", "latest_nav_date", "daily_growth_rate_pct", "source")}})
        return result

    def nav():
        value = fund_nav_sync_service.sync(fund_codes=codes)
        return {"successful_funds": value["successful_funds"], "failed_funds": value["failed_funds"],
                "fetched_at": value["fetched_at"], "stage_status": "partial" if value["failed_funds"] else "ready"}

    def news():
        value = market_event_sync_service.sync()
        # 新闻正文保存在原事件库，输入快照再由原投研图选择关联持仓的事件。
        return {key: value.get(key) for key in ("fetched_count", "inserted_count", "window_start", "window_end", "fetched_at", "successful_sources")} | {
            "failed_source_count": len(value.get("source_errors", [])),
            "stage_status": "partial" if value.get("source_errors") or not value.get("fetched_count") else "ready"}

    def hotspots():
        clustered = market_event_cluster_service.build(max_new_embeddings=30,
            embedding_factory=lambda: DashScopeEmbeddingClient(timeout=30, governed=True))
        market_hotspot_tracking_service.capture()
        result = market_hotspot_tracking_service.overview(limit=6)
        keys = ("cluster_id", "title", "summary", "event_end", "observed_at", "source_count", "member_count", "source_names", "themes", "lifecycle_label")
        return {"items": [{key: item.get(key) for key in keys} for item in result["hotspots"]],
                "deferred_embedding_count": clustered.get("deferred_embedding_count", 0),
                "stage_status": "partial" if clustered.get("deferred_embedding_count") else "ready"}

    def turnover():
        value = jsonable_encoder(market_turnover_service.refresh())
        return {key: value.get(key) for key in ("snapshot", "source_name", "source_url", "limitation")} | {
            "stage_status": "ready" if value.get("freshness") == "recent" and value.get("status") == "available" else "partial"}

    def indices():
        value = market_intraday_service.refresh()
        keys = ("index_code", "index_name", "last", "change_pct", "quote_at", "source_url")
        return {"items": [{key: item.get(key) for key in keys} for item in value["indices"]], "source_name": value.get("source_name"),
                "stage_status": "partial" if any(item.get("status") != "available" for item in value["indices"]) else "ready"}

    return DailyBriefingData(stages=[("nav_sync", "持仓净值", nav), ("news", "市场事件", news),
        ("hotspots", "热点聚合", hotspots), ("turnover", "全市场成交额", turnover), ("indices", "盘中指数行情", indices)],
        funds=funds).prepare(record=record, store=store, check=check)

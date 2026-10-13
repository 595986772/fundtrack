"""把已有的行业 ETF 数据整理成热门板块资金观察，不额外抓取或调用模型。

前端和 Agent 共用这份结果。金额来自份额变化，不把龙虎榜或成交大单混进来；
热门板块指项目关注的主题集合，不冒充全市场实时热度榜。
"""

from __future__ import annotations

from typing import Any


SECTOR_METHODOLOGY = (
    "板块按已配置主题归类，一只 ETF 只归一个主题，可能包含境内、港股和商品 ETF。"
    "净申购估算 = 同一 ETF 的起止份额差 × 期末可用价格；"
    "资金强度 = 净申购估算 / 可比 ETF 的期末规模，不是涨跌幅。"
    "覆盖率为可比 ETF 占该主题期末规模的比例。窗口按数据库有记录的交易日计算，"
    "不补齐缺失日期；不同窗口的样本可能变化，金额不外推至整个行业。"
)


def build_sector_analysis(
    topic_signals: list[dict[str, Any]], exposure_links: list[dict[str, Any]],
) -> dict[str, Any]:
    """按同一日期、同一窗口对板块排名，并生成能核验的中文解读。"""

    sectors = [item for item in topic_signals if item["topic_kind"] == "sector"]
    latest_date = max((item["data_date"] for item in topic_signals), default=None)
    windows = {}
    for period in (1, 5, 20):
        key = f"{period}d"
        rows = []
        for signal in sectors:
            metrics = signal["period_flows"][key]
            flow = metrics["net_flow_cny"]
            # 较旧快照仍展示日期，但不能进入最新一轮的板块资金排名。
            status = (
                "stale" if signal["data_date"] != latest_date
                else "insufficient" if flow is None
                else "available"
            )
            direction = (
                "insufficient" if status != "available"
                else "inflow" if flow > 0
                else "outflow" if flow < 0
                else "neutral"
            )
            evidence_id = (
                f"capital:sector:{signal['topic_code']}:{key}:"
                f"{metrics['start_date']}:{metrics['end_date']}"
                if status == "available" else None
            )
            # 只带板块暴露，不把基金名称匹配解释成百分之百持仓。
            related = [
                {
                    "scope": link["scope"], "scope_id": link["scope_id"],
                    "scope_name": link["scope_name"],
                    "disclosure_date": link.get("disclosure_date"),
                    "known_exposure_pct": match["known_exposure_pct"],
                }
                for link in exposure_links
                for match in link.get("matched_topics", [])
                if match["topic_code"] == signal["topic_code"]
            ]
            rows.append({
                "topic_code": signal["topic_code"], "topic_label": signal["topic_label"],
                "data_date": signal["data_date"], "etf_count": signal["etf_count"],
                **metrics, "status": status, "direction": direction,
                "evidence_id": evidence_id, "exposure_links": related,
                "interpretation": _interpretation(signal, metrics, period, status),
            })

        # 先看流入最大的板块，再到流出；缺数据的板块放最后，绝不当成 0 元排名。
        rows.sort(key=lambda item: (
            item["status"] != "available",
            -item["net_flow_cny"] if item["status"] == "available" else 0,
            item["topic_code"],
        ))
        available = [item for item in rows if item["status"] == "available"]
        for rank, item in enumerate(available, 1):
            item["rank"] = rank
        inflows = [item for item in available if item["net_flow_cny"] > 0]
        outflows = [item for item in available if item["net_flow_cny"] < 0]
        # 领先只指已覆盖样本金额，不用主观热度分，也不把净流入翻译成买入建议。
        leaders = {
            "inflow": _leader(inflows[0]) if inflows else None,
            "outflow": _leader(outflows[-1]) if outflows else None,
        }
        windows[key] = {
            "period": period, "available_count": len(available),
            "inflow_count": len(inflows), "outflow_count": len(outflows),
            "neutral_count": len(available) - len(inflows) - len(outflows),
            "unavailable_count": len(rows) - len(available),
            "leaders": leaders, "rows": rows,
        }
    return {
        "basis": "sector_etf_net_creation_estimate", "data_date": latest_date,
        "sector_count": len(sectors), "windows": windows,
        "methodology": SECTOR_METHODOLOGY,
        "boundary": "行业 ETF 样本不等于全行业资金流向，也不能识别具体机构。",
    }


def _leader(row: dict[str, Any]) -> dict[str, Any]:
    """摘要保留金额和覆盖，让第一名也带上样本范围。"""

    return {key: row[key] for key in (
        "topic_code", "topic_label", "net_flow_cny", "coverage_pct", "evidence_id",
    )}


def _interpretation(
    signal: dict[str, Any], metrics: dict[str, Any], period: int, status: str,
) -> str:
    """只描述可验证的方向；缺历史、覆盖不全和新旧日期分别说明。"""

    if status == "stale":
        return "快照日期落后，未纳入本轮排名。"
    if status == "insufficient":
        return f"缺少近 {period} 日的可比起止份额，尚不能判断申赎方向。"
    flow = metrics["net_flow_cny"]
    direction = "净申购" if flow > 0 else "净赎回" if flow < 0 else "申赎持平"
    parts = [f"近 {period} 日已覆盖样本{direction}"]
    # 重叠窗口不表示连续流入；短长方向相反时只作分化提示，不宣称趋势反转。
    daily_flow = signal["period_flows"]["1d"]["net_flow_cny"]
    if period > 1 and daily_flow is not None and flow * daily_flow < 0:
        parts.append("最近 1 日方向相反，短长窗口分化")
    if metrics["coverage_pct"] < 100:
        parts.append(f"覆盖 {metrics['coverage_pct']:.1f}%，未外推至整个板块")
    return "；".join(parts) + "。"

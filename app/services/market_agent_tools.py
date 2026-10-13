"""把确定性市场状态包装成主 Agent 可以调用的 LangChain Tool。"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field
from app.services.tool_recovery import business_tool_error

from app.services.large_capital_flows import large_capital_analysis_service
from app.services.retail_indices import retail_index_service
from app.services.market_events import MarketEventError, market_event_service
from app.services.market_hotspots import (
    MarketHotspotError,
    market_hotspot_tracking_service,
)
from app.services.market_sentiment import (
    MarketSentimentError,
    market_sentiment_service,
)


class MarketSentimentToolInput(BaseModel):
    """当前工具读取最新市场快照，因此不需要模型生成参数。"""


class SearchMarketEventsToolInput(BaseModel):
    """事件工具的参数保持精简，降低模型生成错误参数的概率。"""

    query: str | list[str] = Field(
        default="",
        description=(
            "可选单一主题词，例如白酒、消费、国债。只按基金代码查询，或用户同时"
            "询问政策、监管、公告等多类事件时请留空，不要拆成多次调用。"
        ),
    )
    fund_code: str | None = Field(
        default=None,
        pattern=r"^\d{6}$",
        description="可选六位基金代码；用于只查询与该基金相关的事件。",
    )
    event_type: str | list[str] | None = Field(
        default=None,
        description=(
            "仅当用户只询问一种事件时填写。用户同时询问多个类型时必须留空，一次"
            "调用会返回所有类型。"
        ),
    )
    limit: int = Field(default=8, ge=1, le=20, description="最多返回多少条事件。")


class LargeCapitalToolInput(BaseModel):
    """资金线索可以同时关联当前基金或当前组合。"""

    user_id: str = Field(
        default="chen",
        min_length=1,
        max_length=50,
        description="当前工作区用户；读取组合时必须传入。",
    )
    fund_code: str | None = Field(
        default=None,
        pattern=r"^\d{6}$",
        description="可选当前基金代码，用于关联定期披露持仓。",
    )
    portfolio_id: str | None = Field(
        default=None,
        pattern=r"^portfolio_[a-f0-9]{16}$",
        description="可选当前组合 ID，用于关联底层证券暴露。",
    )
    sector_period: Literal[1, 5, 20] = Field(
        default=5,
        description="热门板块的资金统计窗口，按有记录交易日取 1、5 或 20 日，默认 5 日。",
    )


class MarketHotspotToolInput(BaseModel):
    """热点工具一次返回趋势和证据，不需要模型再逐条搜索新闻。"""

    query: str = Field(
        default="",
        max_length=100,
        description="可选主题词，例如半导体、人工智能或消费。",
    )
    fund_code: str | None = Field(
        default=None,
        pattern=r"^\d{6}$",
        description="可选六位基金代码，只返回与当前基金关联的热点。",
    )
    lifecycle: str | None = Field(
        default=None,
        description=(
            "可选阶段：emerging、rising、peaking、steady、cooling、faded。"
            "用户没有明确指定阶段时留空。"
        ),
    )
    limit: int = Field(default=6, ge=1, le=10, description="最多返回多少个热点。")


def _load_tool_decorator():
    """延迟导入 LangChain，保持普通市场 API 的依赖边界清楚。"""

    from langchain_core.tools import tool

    return tool


tool = _load_tool_decorator()


def _bounded_event_result(result: dict) -> dict:
    """给 Agent 保留可回答、可引用的字段，并把单次结果控制在压缩阈值以内。

    事件仓库原对象还包含哈希、评分原因、标签等内部字段。它们适合 API 详情页，却会
    让六条事件超过 Context Compact 的 4k 阈值，模型随后看不到完整事件并重复查询。
    """

    top_level_keys = (
        "data_kind",
        "data_mode",
        "data_updated_at",
        "data_cutoff",
        "count",
        "total_matched",
        "fresh_event_count",
        "decaying_event_count",
        "expired_event_count",
        "source_count",
        "source_names",
        "disclaimer",
    )
    event_keys = (
        "event_id",
        "event_date",
        "event_type",
        "title",
        "summary",
        "impact_direction",
        "impact_horizon",
        "affected_fund_codes",
        "source_name",
        "source_level",
        "source_url",
        "freshness_status",
        "valid_until",
        "decay_factor",
    )
    return {
        "ok": True,
        "tool_result_scope": "bounded_agent_summary",
        **{key: result.get(key) for key in top_level_keys},
        "events": [
            {key: event.get(key) for key in event_keys}
            for event in result.get("events", [])
        ],
    }


def _bounded_market_result(result: dict) -> dict:
    """保留模型研判所需字段，同时避免多指数详情触发 Context Compact。

    REST 页面继续使用完整结果；Agent 不需要前端文案、重复 signal 和每项长摘要，
    因此只保留可计算指标、标签、日期、来源和限制。这样一次工具调用就足够回答
    “总览 + 指定指数”的问题。
    """

    index_overview = result.get("index_overview") or {}
    index_keys = (
        "index_code",
        "index_name",
        "daily_return_pct",
        "return_30d_pct",
        # return_3m_pct 与兼容字段 return_60d_pct 使用同一窗口，Agent 只保留前者，
        # 避免多个指数重复字段把 ToolMessage 推过 Context Compact 阈值。
        "return_3m_pct",
        "annualized_volatility_20d_pct",
        "max_drawdown_60d_pct",
        "volume_vs_five_day_average_pct",
        "trend_label",
        "volatility_label",
        "activity_label",
        "relative_strength_rank_30d",
        "relative_strength_rank_3m",
    )
    return {
        "ok": True,
        "tool_result_scope": "bounded_market_summary",
        "data_mode": result.get("data_mode"),
        "data_updated_at": result.get("data_updated_at"),
        "data_date": result.get("data_date"),
        "state_code": result.get("state_code"),
        "state_label": result.get("state_label"),
        "summary": result.get("summary"),
        "market_dimensions": {
            # REST 仍返回兼容字段 return_60d_pct；Agent 使用更直观的 3 个月别名，
            # 避免同一个值重复占用上下文。
            "trend": {
                key: (result.get("trend") or {}).get(key)
                for key in (
                    "proxy_code",
                    "proxy_name",
                    "tracking_target",
                    "return_20d_pct",
                    "return_30d_pct",
                    "return_3m_pct",
                    "label",
                )
            },
            "volatility": result.get("volatility"),
            "breadth": result.get("breadth"),
            "activity": result.get("activity"),
        },
        "index_overview": {
            "data_mode": index_overview.get("data_mode"),
            "data_date": index_overview.get("data_date"),
            "summary": index_overview.get("summary"),
            # 各指数来自同一 Provider，只保留一份来源，避免重复 URL。
            "source": (index_overview.get("indices") or [{}])[0].get("source"),
            "source_url": (index_overview.get("indices") or [{}])[0].get(
                "source_url"
            ),
            "indices": [
                {key: item.get(key) for key in index_keys}
                for item in index_overview.get("indices", [])
            ],
        },
        "evidence_ids": result.get("evidence_ids", []),
        "sources": result.get("sources", []),
        # Agent 只保留最关键的边界；完整限制与声明仍可在 REST 页面查看。
        "limitations": result.get("limitations", [])[:1],
    }


def _bounded_large_capital_result(result: dict, sector_period: int = 5) -> dict:
    """保留方向、金额和证据，限制 ToolMessage 长度以避免模型重复查询。"""

    period_key = f"{sector_period}d"
    institutional = result.get("institutional_signal") or {}
    institution_keys = (
        "status",
        "direction",
        "trade_date_start",
        "trade_date_end",
        "stock_count",
        "buy_cny",
        "sell_cny",
        "net_buy_cny",
        "evidence_id",
    )
    sectors = result.get("sector_analysis") or {}
    sector_window = sectors.get("windows", {}).get(period_key, {})
    sector_rows = sector_window.get("rows", [])
    # 标准“列名 + 行数据”表格省去九遍重复 JSON 字段名。金额和证据不截断，
    # 避免超过 Context Compact 的 4000 字符阈值后只剩前 900 字的预览。
    sector_keys = (
        "topic_label", "data_date", "net_flow_cny", "intensity_pct", "coverage_pct",
        "comparable_etf_count", "etf_count", "status",
    )
    return {
        "ok": True,
        "tool_result_scope": "bounded_large_capital_summary",
        "data_mode": result.get("data_mode"),
        "data_updated_at": result.get("data_updated_at"),
        "data_date": result.get("data_date"),
        "period": sector_period,
        "topic_signals": [
            {
                "topic_label": item["topic_label"],
                "net_flow_cny": item.get(f"flow_{period_key}_cny"),
                "coverage_pct": item.get(f"coverage_{period_key}_pct"),
                "evidence_id": item["evidence_id"],
            }
            for item in result.get("topic_signals", []) if item.get("topic_kind") == "broad"
        ],
        # 不再截断成“6 个宽基 + 前 2 个行业”，9 个行业都进入模型上下文。
        # 只传用户选定窗口，避免把三份重复中文解读同时塞进 ToolMessage。
        "sector_analysis": {
            "period": sector_period,
            "data_date": sectors.get("data_date"),
            "start_date": (sector_rows[0] if sector_rows else {}).get("start_date"),
            "end_date": (sector_rows[0] if sector_rows else {}).get("end_date"),
            "methodology": sectors.get("methodology"),
            "columns": sector_keys,
            "rows": [
                # 证据编号仍保留为字典字段，供现有证据提取器和引用校验读取。
                {"evidence_id": item.get("evidence_id"),
                 "values": [item.get(key) for key in sector_keys]}
                for item in sector_rows
            ],
        },
        "institutional_signal": {
            **{key: institutional.get(key) for key in institution_keys},
        },
        "exposure_links": [
            {
                "scope_name": link["scope_name"], "disclosure_date": link.get("disclosure_date"),
                "topics": {
                    topic["topic_label"]: topic["known_exposure_pct"]
                    for topic in link.get("matched_topics", [])
                },
                "basis": "已披露证券暴露%，主题相关但未核验的比例为0",
            }
            for link in result.get("exposure_links", [])[:2]
        ],
        "identity_boundary": (result.get("identity_boundary") or {}).get("explanation"),
        "limitations": result.get("limitations", [])[:3],
        "sources": result.get("sources", []),
    }


def _bounded_hotspot_result(result: dict) -> dict:
    """压缩时间线和来源证据，确保主 Agent 一次调用即可完成热点解释。"""

    hotspot_keys = (
        "rank",
        "cluster_id",
        "representative_event_id",
        "title",
        "summary",
        "event_start",
        "event_end",
        "impact_direction",
        "affected_fund_codes",
        "themes",
        "source_count",
        "source_names",
        "member_count",
        "heat_score",
        "peak_heat_score",
        "heat_level",
        "freshness_status",
        "lifecycle",
        "lifecycle_label",
        "trend_direction",
        "change_since_previous",
        "heat_change_24h",
        "heat_change_3d",
        "member_delta",
        "source_delta",
        "snapshot_count",
        "trend_confidence",
        "tracking_reason",
    )
    return {
        "ok": True,
        "tool_result_scope": "bounded_hotspot_tracking",
        "data_mode": result.get("data_mode"),
        "generated_at": result.get("generated_at"),
        "data_cutoff": result.get("data_cutoff"),
        "history_days": result.get("history_days"),
        "total_hotspot_count": result.get("total_hotspot_count"),
        "lifecycle_counts": result.get("lifecycle_counts"),
        "hotspots": [
            {
                **{key: item.get(key) for key in hotspot_keys},
                "evidence": (item.get("evidence") or [])[:3],
                "timeline": (item.get("timeline") or [])[-4:],
            }
            for item in result.get("hotspots", [])[:6]
        ],
        "disclaimer": result.get("disclaimer"),
    }


@tool(
    "get_market_sentiment_tool",
    args_schema=MarketSentimentToolInput,
    description=(
        "查询 FundScope 最新可用快照对应的市场状态。"
        "当用户询问当前市场环境、上证指数、深证成指、沪深300、中证500、"
        "中证1000、创业板、科创50、宽基趋势、市场波动、上涨下跌家数或成交活跃度时使用。"
        "返回总览和宽基指数分区的确定性指标、来源、日期与限制。"
        "这个工具不包含新闻、政策或社交媒体舆情。"
    ),
)
def get_market_sentiment_tool() -> str:
    """返回结构化 JSON，方便模型逐条引用市场证据。"""

    try:
        result = market_sentiment_service.analyze()
    except MarketSentimentError as exc:
        return json.dumps(
            {"ok": False, "error": str(exc)},
            ensure_ascii=False,
        )

    return json.dumps(
        _bounded_market_result(result),
        ensure_ascii=False,
        separators=(",", ":"),
    )


@tool(
    "get_large_capital_signals_tool",
    args_schema=LargeCapitalToolInput,
    description=(
        "查询 FundScope 已保存的 ETF 份额变化、宽基/行业资金方向和龙虎榜机构席位。"
        "当用户询问国家队线索、主力机构、大资金、ETF净申购、机构净买入、龙虎榜"
        "资金布局、热门板块资金排名，或这些线索与当前基金/组合的关系时使用。"
        "sector_analysis 用 columns 列名与 rows[].values 行数组返回全部关注行业的金额、强度、"
        "覆盖率与证据；据此解释，不应当成个股或具体机构。sector_period 可取 1、5、20；"
        "topic_signals 返回同一所选窗口的宽基。返回所选窗口的 ETF 估算"
        "流入、最近五个龙虎榜交易日机构买卖、evidence_id、来源和身份识别边界。"
        "ETF申购者与机构专用席位都可能匿名；除非工具明确返回 confirmed disclosure，"
        "不能把任何线索断言成国家队真实买卖。不要用它查询普通新闻或净值。"
    ),
)
def get_large_capital_signals_tool(
    user_id: str = "chen",
    fund_code: str | None = None,
    portfolio_id: str | None = None,
    sector_period: Literal[1, 5, 20] = 5,
) -> str:
    """复用市场页同一个确定性 Service，避免 Agent 产生另一套资金口径。"""

    result = large_capital_analysis_service.overview(
        user_id=user_id,
        fund_code=fund_code,
        portfolio_id=portfolio_id,
    )
    return json.dumps(
        _bounded_large_capital_result(result, sector_period),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


@tool(
    "get_market_hotspots_tool",
    args_schema=MarketHotspotToolInput,
    description=(
        "查询 FundScope 已保存的市场热点排行榜及历史热度变化。当用户询问热点、"
        "哪些主题正在升温或降温、事件是否持续发酵、近24小时或近3日关注度变化时使用。"
        "返回 lifecycle、热度变化、报道和独立来源增量、关联基金及原始 evidence。"
        "热点表示关注度，不自动等于利好、利空、买入或卖出信号；不要再重复调用新闻工具"
        "来证明同一批热点。"
    ),
)
def get_market_hotspots_tool(
    query: str = "",
    fund_code: str | None = None,
    lifecycle: str | None = None,
    limit: int = 6,
) -> str:
    """复用热点页面的历史快照 Service，让 Agent 看到同一套趋势。"""

    try:
        result = market_hotspot_tracking_service.overview(
            query=query,
            fund_code=fund_code,
            lifecycle=lifecycle,
            limit=limit,
        )
    except MarketHotspotError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    return json.dumps(
        _bounded_hotspot_result(result),
        ensure_ascii=False,
        separators=(",", ":"),
    )


@tool(
    "search_market_events_tool",
    args_schema=SearchMarketEventsToolInput,
    description=(
        "查询 FundScope 最新可用的新闻、政策、监管、经济数据、基金公告和社交仓位观点。"
        "当用户询问消息面、政策面、新闻影响、基金公告、白酒行业消息或国债发行时使用。"
        "可按关键词、基金代码和事件类型筛选；每条结果都包含 event_id、日期、来源等级、"
        "来源链接、影响标注、freshness_status、事件簇和热度。相似报道会共享 cluster_id；"
        "过期事件仍可追溯但不应作为当前影响。一次调用可以返回多个事件类型；用户问"
        "政策、监管、公告等多个类别时，保留 query 和 event_type 为空并只调用一次。"
        "social_positioning 和 D 级来源只表示多位作者未经核验的自述，不能描述成真实"
        "市场资金流。不要用它查询净值、回撤或市场宽度。"
    ),
)
def search_market_events_tool(
    query: str | list[str] = "",
    fund_code: str | None = None,
    event_type: str | list[str] | None = None,
    limit: int = 8,
) -> str:
    """把同一份事件 Service 包装成模型能主动调用的只读工具。"""

    try:
        if isinstance(query, list):
            normalized_query = query[0] if len(query) == 1 else ""
        else:
            normalized_query = str(query or "").strip()
        # “政策+监管”属于多类型筛选，不是全文必须同时出现的主题关键词。
        # 清空后由 fund_code 一次召回，再让模型按 event_type 整理结果。
        category_words = ("政策", "监管", "公告", "经济数据", "市场通知")
        if sum(word in normalized_query for word in category_words) >= 2:
            normalized_query = ""
        normalized_query = normalized_query[:100]

        # 部分模型会把“政策和监管”自然生成成数组。单项数组仍按精确类型查询；
        # 多项数组改为一次宽查询，避免为了多个类型连续调用同一个工具。
        normalized_event_type: str | None
        allowed_event_types = {
            "policy",
            "economic_data",
            "regulation",
            "fund_announcement",
            "market_notice",
            "social_positioning",
        }
        if isinstance(event_type, list):
            normalized_event_type = (
                event_type[0]
                if len(event_type) == 1 and event_type[0] in allowed_event_types
                else None
            )
        else:
            normalized_event_type = (
                event_type if event_type in allowed_event_types else None
            )
        result = market_event_service.search(
            query=normalized_query,
            fund_code=fund_code,
            event_type=normalized_event_type,
            limit=limit,
        )
    except MarketEventError as exc:
        return business_tool_error(exc)

    return json.dumps(_bounded_event_result(result), ensure_ascii=False)


@tool(
    "get_retail_indices_tool",
    args_schema=MarketSentimentToolInput,
    description=(
        "查询已保存的宝妈指数与韭菜指数。韭菜版本是冷西西韭菜50理论指数；"
        "包含日期、点位、近30日/3个月涨跌幅、来源及缺失状态。"
        "宝妈指数没有真实源时必须说明缺失，不能编造分数。只读缓存，不自动抓取。"
    ),
)
def get_retail_indices_tool() -> str:
    """Agent 和前端读同一份结果，曲线不占用模型上下文。"""
    return json.dumps({"ok": True, **retail_index_service.overview(include_history=False)},
                      ensure_ascii=False, default=str, separators=(",", ":"))


def get_market_agent_tools() -> list:
    """集中返回市场截面、资金行为、热点趋势与事件检索工具。"""

    return [
        get_market_sentiment_tool,
        get_large_capital_signals_tool,
        get_market_hotspots_tool,
        get_retail_indices_tool,
        search_market_events_tool,
    ]

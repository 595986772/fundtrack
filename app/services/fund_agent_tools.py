"""FundScope 提供给主 Agent 的只读基金工具。

前端基金页直接调用 REST API；大模型不能直接调用浏览器 API，所以这里把同一份
``FundCatalogService`` 数据包装成 LangChain Tool。这样基金页面和 Agent 回答使用
同一个数据来源，不会各自维护一套基金逻辑。
"""

from __future__ import annotations

import calendar
import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field
from app.services.tool_recovery import business_tool_error

from app.services.fund_assessment import (
    FundAssessmentError,
    evidence_fusion_service,
)
from app.services.funds import FundCatalogError, fund_catalog_service
from app.services.fund_holdings import fund_holdings_service
from app.services.fund_risk import FundRiskError, fund_risk_service
from app.services.fund_returns import (
    FundReturnSeriesError,
    fund_return_series_service,
)
from app.services.fund_semantic_review import (
    FundSemanticReviewError,
    fund_semantic_review_service,
)


class FundProfileToolInput(BaseModel):
    """查询基金档案时模型需要生成的参数。"""

    fund_code: str = Field(
        ...,
        pattern=r"^\d{6}$",
        description="六位基金代码，例如 110020、161725、110007。",
    )


class FundNavSummaryToolInput(BaseModel):
    """查询一段净值摘要时模型需要生成的参数。"""

    fund_code: str = Field(
        ...,
        pattern=r"^\d{6}$",
        description="六位基金代码，例如 110020、161725、110007。",
    )
    months: Literal[6, 12, 36] = Field(
        default=12,
        description="统计区间，只支持近 6、12 或 36 个月。",
    )


class FundRiskToolInput(BaseModel):
    """风险 Tool 使用基金代码和固定统计区间，不让模型自定义计算公式。"""

    fund_code: str = Field(
        ...,
        pattern=r"^\d{6}$",
        description="六位基金代码，例如 110020、161725、110007。",
    )
    months: Literal[6, 12, 36] = Field(
        default=12,
        description="风险统计区间，只支持近 6、12 或 36 个月。",
    )


class FundAssessmentToolInput(BaseModel):
    """综合研判 Tool 只让模型选择基金和统计区间，不允许修改评分规则。"""

    fund_code: str = Field(
        ...,
        pattern=r"^\d{6}$",
        description="六位基金代码，例如 110020、161725、110007。",
    )


class FundSemanticReviewToolInput(FundAssessmentToolInput):
    """语义研判额外接收用户原问题，让内层模型知道真正需要判断什么。"""

    question: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description=(
            "用户关于该基金的原始问题，例如‘结合当前数据，这只基金需要减仓吗？’。"
        ),
    )
    months: Literal[6, 12, 36] = Field(
        default=12,
        description="综合研判使用的历史风险和事件统计区间。",
    )


def _load_tool_decorator():
    """延迟导入 LangChain，保持普通基金 API 不依赖 Tool 初始化细节。"""

    from langchain_core.tools import tool

    return tool


def _json_result(payload: dict) -> str:
    """工具结果统一返回 JSON，方便模型准确读取字段和来源。"""

    return json.dumps(payload, ensure_ascii=False)


def _assessment_for_agent(assessment: dict) -> dict:
    """把完整 Assessment 裁成不会触发 Context Compact 的模型专用摘要。

    REST 和前端仍使用全部 Evidence。Agent 回答整体状况时只需要每组最关键的几条；
    如果把十几条完整证据全塞进 ToolMessage，反而会触发压缩并诱发重复工具查询。
    """

    def compact_evidence(items: list[dict], limit: int) -> list[dict]:
        compacted: list[dict] = []
        for item in items[:limit]:
            compact = {
                "evidence_id": item["evidence_id"],
                "kind": item["kind"],
                "direction": item["direction"],
                "strength": item["strength"],
                "title": item["title"],
                "value_text": item["value_text"],
                "explanation": str(item["explanation"])[:120],
                "data_date": item["data_date"],
                "source_name": item["source_name"],
            }
            # 事件证据才需要链接、聚类和时效字段。风险指标没有这些字段，输出 null
            # 只会白白占据 ToolMessage，甚至触发 Context Compact。
            if item.get("event_id"):
                compact.update(
                    {
                        "event_id": item["event_id"],
                        "source_url": item["source_url"],
                        "cluster_id": item.get("cluster_id"),
                        "heat_level": item.get("heat_level"),
                        "decay_factor": item.get("decay_factor", 1.0),
                        "freshness_status": item.get("freshness_status"),
                        "valid_until": item.get("valid_until"),
                    }
                )
            compacted.append(compact)
        return compacted

    risk_limit = 3
    support_limit = 2
    neutral_limit = 1
    score = assessment["score_breakdown"]
    risk_adjusted = assessment["risk_adjusted"]
    benchmark = assessment["benchmark_comparison"]
    return {
        "ok": True,
        "data_kind": assessment["data_kind"],
        "tool_result_scope": "bounded_agent_summary",
        "fund_code": assessment["fund_code"],
        "short_name": assessment["short_name"],
        "period_months": assessment["period_months"],
        "period_start": assessment["period_start"],
        "period_end": assessment["period_end"],
        "state_code": assessment["state_code"],
        "state_label": assessment["state_label"],
        "confidence": assessment["confidence"],
        "confidence_reason": assessment["confidence_reason"],
        "summary": assessment["summary"],
        "score_breakdown": {
            "supportive_points": score["supportive_points"],
            "caution_points": score["caution_points"],
            "net_points": score["net_points"],
            "expired_event_count": score.get("expired_event_count", 0),
            "method": score["method"],
        },
        "risk_evidence": compact_evidence(
            assessment["risk_evidence"],
            risk_limit,
        ),
        "supporting_evidence": compact_evidence(
            assessment["supporting_evidence"],
            support_limit,
        ),
        "neutral_evidence": compact_evidence(
            assessment["neutral_evidence"],
            neutral_limit,
        ),
        "omitted_evidence_counts": {
            "risk": max(0, len(assessment["risk_evidence"]) - risk_limit),
            "supporting": max(
                0,
                len(assessment["supporting_evidence"]) - support_limit,
            ),
            "neutral": max(
                0,
                len(assessment["neutral_evidence"]) - neutral_limit,
            ),
        },
        "evidence_count": assessment["evidence_count"],
        "event_count": assessment["event_count"],
        "event_cluster_count": assessment.get("event_cluster_count", 0),
        "active_event_count": assessment.get("active_event_count", 0),
        "expired_event_count": assessment.get("expired_event_count", 0),
        "component_dates": assessment["component_dates"],
        "risk_return_basis": assessment["risk_return_basis"],
        "risk_return_basis_label": assessment["risk_return_basis_label"],
        "risk_return_basis_status": assessment["risk_return_basis_status"],
        "risk_data_quality_warnings": assessment["risk_data_quality_warnings"],
        # Agent 只需要数值、假设和告警来完成解读。完整公式、来源 URL 等审计字段
        # 仍保留在 REST API 中，避免同一份长说明反复进入模型上下文。
        "risk_adjusted": {
            "annualized_return_pct": risk_adjusted["annualized_return_pct"],
            "risk_free_rate_annual_pct": risk_adjusted[
                "risk_free_rate_annual_pct"
            ],
            "sharpe_ratio": risk_adjusted["sharpe_ratio"],
            "sortino_ratio": risk_adjusted["sortino_ratio"],
            "warnings": risk_adjusted["warnings"],
        },
        "benchmark_comparison": {
            "benchmark_name": benchmark["benchmark_name"],
            "status": benchmark["status"],
            "period_return_pct": benchmark["period_return_pct"],
            "fund_aligned_return_pct": benchmark["fund_aligned_return_pct"],
            "excess_return_pct": benchmark["excess_return_pct"],
            "observation_count": benchmark["observation_count"],
            "coverage_pct": benchmark["coverage_pct"],
            "period_start": benchmark["period_start"],
            "period_end": benchmark["period_end"],
            "warnings": benchmark["warnings"],
        },
        "missing_information": assessment["missing_information"],
        "disclaimer": assessment["disclaimer"],
    }


def _semantic_review_for_agent(review: dict) -> dict:
    """保留模型判断和规则基线的关键字段，避免把整份 Assessment 重复塞入上下文。"""

    baseline = review["baseline_assessment"]
    interpretation = review["model_interpretation"]
    return {
        "ok": True,
        "data_kind": review["data_kind"],
        "tool_result_scope": "bounded_semantic_review",
        "fund_code": review["fund_code"],
        "short_name": review["short_name"],
        "period_months": review["period_months"],
        "assessment_date": review["assessment_date"],
        "rule_baseline": {
            "state_code": baseline["state_code"],
            "state_label": baseline["state_label"],
            "score_breakdown": baseline["score_breakdown"],
        },
        "model_interpretation": interpretation,
        "layer_contract": review["layer_contract"],
        "disclaimer": review["disclaimer"],
    }


def _subtract_months(value: date, months: int) -> date:
    """从日期中减去自然月，并处理 2 月等月份不存在 30/31 日的情况。"""

    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


tool = _load_tool_decorator()


@tool(
    "get_fund_profile_tool",
    args_schema=FundProfileToolInput,
    description=(
        "查询 FundScope 当前样本基金的基本档案。"
        "当用户询问当前基金的类型、基金经理、管理公司、规模、费率、跟踪标的或"
        "业绩基准时使用。工具会返回数据模式和截止日期，不是盘中实时行情。"
    ),
)
def get_fund_profile_tool(fund_code: str) -> str:
    """返回适合模型阅读的基金基本档案和数据来源。"""

    try:
        detail = fund_catalog_service.get_fund(fund_code)
    except FundCatalogError as exc:
        return business_tool_error(exc, fund_code=fund_code)

    profile = detail["profile"]
    data_kind = (
        "synced_database_snapshot"
        if detail.get("data_mode") == "database"
        else "frozen_snapshot"
    )
    return _json_result(
        {
            "ok": True,
            "data_kind": data_kind,
            "data_mode": detail.get("data_mode", "fixture"),
            "data_updated_at": detail.get("data_updated_at"),
            "fund_code": fund_code,
            "short_name": profile["short_name"],
            "full_name": profile.get("full_name"),
            "fund_type": profile["fund_type"],
            "sample_role": profile["sample_role"],
            "established": profile.get("established"),
            "net_asset_size": profile.get("net_asset_size"),
            "management_company": profile.get("management_company"),
            "custodian": profile.get("custodian"),
            "fund_managers": profile.get("fund_managers"),
            "management_fee": profile.get("management_fee"),
            "custodian_fee": profile.get("custodian_fee"),
            "benchmark": profile.get("benchmark"),
            "tracking_target": profile.get("tracking_target"),
            "snapshot_start": detail["snapshot_start"],
            "snapshot_end": detail["snapshot_end"],
            "source": detail["source"],
            "source_url": profile.get("source_url"),
        }
    )


@tool(
    "get_fund_nav_summary_tool",
    args_schema=FundNavSummaryToolInput,
    description=(
        "查询 FundScope 当前样本基金近 6、12 或 36 个月的净值摘要。"
        "当用户询问当前净值、日涨跌、区间涨跌、区间最高最低或历史表现时使用。"
        "只返回摘要而不是几百条原始净值，避免浪费模型上下文。"
    ),
)
def get_fund_nav_summary_tool(fund_code: str, months: int = 12) -> str:
    """按自然月截取净值，并计算模型回答最常用的几个确定性指标。"""

    try:
        detail = fund_catalog_service.get_fund(fund_code)
    except FundCatalogError as exc:
        return business_tool_error(exc, fund_code=fund_code)

    all_points = detail["nav_points"]
    latest_date = date.fromisoformat(all_points[-1]["nav_date"])
    cutoff = _subtract_months(latest_date, months)
    points = [
        point
        for point in all_points
        if date.fromisoformat(point["nav_date"]) >= cutoff
    ]
    first = points[0]
    latest = points[-1]
    minimum = min(points, key=lambda point: point["unit_nav"])
    maximum = max(points, key=lambda point: point["unit_nav"])
    try:
        return_series = fund_return_series_service.build(
            detail=detail,
            points=points,
        )
    except FundReturnSeriesError as exc:
        return _json_result(
            {"ok": False, "fund_code": fund_code, "error": str(exc)}
        )
    period_return = return_series.period_return_pct
    data_mode = detail.get("data_mode", "fixture")
    data_kind = (
        "synced_database_snapshot"
        if data_mode == "database"
        else "frozen_snapshot"
    )
    data_disclaimer = (
        "每日同步数据库快照，不是盘中实时行情"
        if data_mode == "database"
        else "本地固定历史快照"
    )

    return _json_result(
        {
            "ok": True,
            "data_kind": data_kind,
            "data_mode": data_mode,
            "data_updated_at": detail.get("data_updated_at"),
            "fund_code": fund_code,
            "short_name": detail["profile"]["short_name"],
            "period_months": months,
            "period_start": first["nav_date"],
            "period_end": latest["nav_date"],
            "start_unit_nav": round(first["unit_nav"], 4),
            "latest_unit_nav": round(latest["unit_nav"], 4),
            "latest_daily_growth_rate_pct": latest["daily_growth_rate_pct"],
            "period_return_pct": round(period_return, 2),
            **return_series.metadata(),
            "period_low": {
                "unit_nav": round(minimum["unit_nav"], 4),
                "date": minimum["nav_date"],
            },
            "period_high": {
                "unit_nav": round(maximum["unit_nav"], 4),
                "date": maximum["nav_date"],
            },
            "record_count": len(points),
            "source": detail["source"],
            "snapshot_end": detail["snapshot_end"],
            "disclaimer": f"{data_disclaimer}，仅用于学习和研究，不构成投资建议。",
        }
    )


@tool(
    "get_fund_risk_tool",
    args_schema=FundRiskToolInput,
    description=(
        "查询 FundScope 当前样本基金近 6、12 或 36 个月的确定性风险指标。"
        "当用户询问最大回撤、波动率、年化收益、夏普、Sortino、业绩基准、"
        "超额收益、最差单日表现或回撤持续时间时使用。结果基于工具返回截止日之前"
        "的历史总回报和已同步基准，不构成投资建议。回答时逐字符引用 evidence 中"
        "对应指标的 evidence_id，并说明统计区间；不得自行编造编号。"
    ),
)
def get_fund_risk_tool(fund_code: str, months: int = 12) -> str:
    """调用统一风险服务，保证 Agent 回答和 REST/前端显示的数字一致。"""

    try:
        risk = fund_risk_service.analyze(fund_code=fund_code, months=months)
    except (FundCatalogError, FundRiskError) as exc:
        return business_tool_error(exc, fund_code=fund_code)

    # 证据来自本次实际计算，不另查来源或叠加规则评分。编号同时包含基金和时间区间，
    # 避免两只基金或不同观察窗口的指标被当成同一份证据；回撤沿用风险接口的正幅度。
    period = f"{risk['period_start']}-{risk['period_end']}"
    evidence = [
        {
            "evidence_id": f"risk:{metric}:{fund_code}:{period}",
            "title": title,
            "value_pct": value,
            "source_name": risk["source"],
            "data_date": risk["period_end"],
            "period_start": risk["period_start"],
        }
        for metric, title, value in (
            ("volatility", "历史年化波动率", risk["annualized_volatility_pct"]),
            ("max_drawdown", "历史最大回撤幅度", risk["max_drawdown"]["drawdown_pct"]),
        )
    ]

    return _json_result(
        {
            "ok": True,
            "data_kind": (
                "synced_database_snapshot_risk"
                if risk.get("data_mode") == "database"
                else "frozen_snapshot_risk"
            ),
            **risk,
            "evidence": evidence,
        }
    )


@tool(
    "get_fund_assessment_tool",
    args_schema=FundAssessmentToolInput,
    description=(
        "综合查询一只 FundScope 样本基金的历史风险、当前市场状态和关联事件。"
        "当用户询问这只基金当前整体状况、主要风险、支撑因素、消息影响或希望做"
        "综合研判时优先使用。结果由公开规则确定性计算；回答时应引用 evidence_id，"
        "相似报道已经按 cluster_id 去重，事件证据按有效期乘以 decay_factor；"
        "heat_score 只描述多来源关注度，不会直接放大研判分数；"
        "不得把标签本身直接解释为买卖建议；用户明确询问持有、减仓、仓位或走势时，"
        "主模型可以结合全部证据给出带条件和不确定性的观点。成功返回的关键证据已足够"
        "完成综合回答，除非用户"
        "明确追问某项明细，否则不要再调用风险、市场或事件工具重复查询。"
    ),
)
def get_fund_assessment_tool(fund_code: str, months: int = 12) -> str:
    """复用统一融合服务，让 Agent 与前端看到同一份证据和结论。"""

    try:
        assessment = evidence_fusion_service.assess(
            fund_code=fund_code,
            months=months,
        )
    except (FundCatalogError, FundAssessmentError) as exc:
        return _json_result(
            {"ok": False, "fund_code": fund_code, "error": str(exc)}
        )

    return _json_result(_assessment_for_agent(assessment))


@tool(
    "get_fund_semantic_review_tool",
    args_schema=FundSemanticReviewToolInput,
    description=(
        "按需调用另一个受约束模型，对 FundScope 基金的关联事件判断语义方向、"
        "关联程度、影响周期和不确定性。当用户明确要求 AI/大模型独立判断消息影响、"
        "对照人工规则或检查规则是否可能误判时使用。普通基金综合分析不要调用，"
        "普通持有、减仓、仓位或走势问题也应优先使用 get_fund_assessment_tool。"
        "调用时必须传入用户原问题；结果未校准且不参与规则分。"
    ),
)
async def get_fund_semantic_review_tool(
    fund_code: str,
    question: str,
    months: int = 12,
) -> str:
    """执行一次嵌套模型研判，并返回适合主 Agent 阅读的分层结果。"""

    try:
        review = await fund_semantic_review_service.review(
            fund_code=fund_code,
            months=months,
            question=question,
        )
    except (FundCatalogError, FundSemanticReviewError) as exc:
        return _json_result(
            {"ok": False, "fund_code": fund_code, "error": str(exc)}
        )

    return _json_result(_semantic_review_for_agent(review))


@tool(
    "get_fund_holdings_tool",
    args_schema=FundProfileToolInput,
    description=(
        "读取单只基金的已披露股票、债券、行业集中度及同来源历史披露变化。"
        "分析这只基金买了什么、行业分布、重仓或持仓穿透时使用，不是用户组合穿透。"
        "比例均占基金净值；数据不是实时仓位。新出现/未再披露不代表买入/清仓，"
        "现金和 ETF 联接映射缺失时不能猜测。与事件结合时可另查市场事件工具。"
    ),
)
def get_fund_holdings_tool(fund_code: str) -> str:
    """与页面复用同一分析服务；限制模型上下文长度并披露截断数量。"""
    try:
        result = fund_holdings_service.analyze(fund_code=fund_code)
    except (RuntimeError, ValueError) as exc:
        return _json_result({"ok": False, "fund_code": fund_code, "error": str(exc)})
    disclosure = result["disclosure"]
    securities = disclosure["security_holdings"]
    # 股票/债券各留十条，避免只按总排名裁剪后漏掉某类资产。
    selected = [row for kind in ("stock", "bond") for row in sorted(
        (item for item in securities if item["asset_type"] == kind),
        key=lambda item: item["weight_pct"], reverse=True,
    )[:10]]
    disclosure["security_holdings"] = selected
    industries = disclosure["industry_allocations"]
    disclosure["industry_allocations"] = sorted(industries, key=lambda row: row["weight_pct"], reverse=True)[:10]
    omitted = {"securities": len(securities) - len(selected), "industries": max(0, len(industries) - 10)}
    for comparison in result["comparisons"]:
        comparison["omitted_count"] = max(0, len(comparison["rows"]) - 20)
        comparison["rows"] = comparison["rows"][:20]
    return _json_result({"ok": True, **result, "omitted_counts": omitted})


def get_fund_agent_tools() -> list:
    """集中返回基金工具，供主 Agent 注册表合并。"""
    from app.services.fund_report_tools import get_fund_report_research_tool

    return [
        get_fund_profile_tool,
        get_fund_holdings_tool,
        get_fund_report_research_tool,
        get_fund_nav_summary_tool,
        get_fund_risk_tool,
        get_fund_assessment_tool,
        get_fund_semantic_review_tool,
    ]

"""把组合研究服务包装成主 Agent 的只读 Tools。

这里的工具只负责读取已经由 Python 或数据库确定的数据。大模型可以解释组合业绩、
历史决策和后续结果，但不能通过这些工具新增交易或篡改决策日志。
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field
from app.services.tool_recovery import business_tool_error

from app.services.background_jobs import background_job_service
from app.services.portfolio_assessment import (
    PortfolioAssessmentError,
    portfolio_assessment_service,
)
from app.services.portfolio_exposure import (
    PortfolioExposureError,
    portfolio_exposure_service,
)
from app.services.portfolio_event_impacts import (
    PortfolioEventImpactError,
    portfolio_event_impact_service,
)
from app.services.portfolio_decisions import (
    PortfolioDecisionError,
    portfolio_decision_service,
)
from app.services.portfolio_performance import (
    PortfolioPerformanceError,
    portfolio_performance_service,
)
from app.services.portfolios import (
    PortfolioError,
    PortfolioNotFoundError,
    portfolio_service,
)


class PortfolioAssessmentToolInput(BaseModel):
    """模型需要提供当前用户、组合 ID 和统一观察区间。"""

    user_id: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="System Prompt 中给出的当前用户 ID。",
    )
    portfolio_id: str = Field(
        ...,
        pattern=r"^portfolio_[a-f0-9]{16}$",
        description="FundScope 当前选中的基金组合 ID。",
    )
    months: Literal[6, 12, 36] = Field(
        default=12,
        description="组合内所有基金共用的证据统计区间。",
    )


class PortfolioExposureToolInput(BaseModel):
    """底层持仓穿透只需要当前用户和组合 ID，不需要风险观察月份。"""

    user_id: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="System Prompt 中给出的当前用户 ID。",
    )
    portfolio_id: str = Field(
        ...,
        pattern=r"^portfolio_[a-f0-9]{16}$",
        description="FundScope 当前选中的基金组合 ID。",
    )


class PortfolioEventImpactToolInput(PortfolioExposureToolInput):
    """事件归因默认只返回最值得先看的八条，控制主 Agent 上下文大小。"""

    limit: int = Field(
        default=8,
        ge=1,
        le=12,
        description="返回按关注优先级排序的事件数量。",
    )


class PortfolioPerformanceToolInput(PortfolioAssessmentToolInput):
    """组合业绩与组合研判使用相同的 6/12/36 月观察区间。"""


class PortfolioDecisionListToolInput(PortfolioExposureToolInput):
    """决策日志默认只返回最近五条，避免历史快照挤占 Agent 上下文。"""

    limit: int = Field(
        default=5,
        ge=1,
        le=10,
        description="最多返回多少条最近的投资决策及其动态复盘结果。",
    )


class PortfolioResearchStartToolInput(PortfolioAssessmentToolInput):
    """启动组合研究时，幂等键由可信请求上下文覆盖模型输入。"""

    question: str = Field(default="", max_length=800, description="本次研究问题，由后端以用户原话覆盖。")

    idempotency_key: str | None = Field(
        default=None,
        max_length=100,
        description=(
            "防止同一次聊天重复创建研究任务的键。该值由后端自动注入，模型不要填写。"
        ),
    )


class PortfolioResearchStatusToolInput(PortfolioExposureToolInput):
    """查询当前组合的一次研究 Job。"""

    job_id: str = Field(
        ...,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
        description="start_portfolio_research_tool 返回的研究 Job ID。",
    )


def _load_tool_decorator():
    """延迟导入 LangChain，普通组合 API 不依赖 Tool 初始化细节。"""

    from langchain_core.tools import tool

    return tool


def _bounded_portfolio_result(assessment: dict) -> dict:
    """保留模型回答需要的关键组合证据，避免触发 Context Compact。"""

    def evidence(items: list[dict], limit: int) -> list[dict]:
        compacted = []
        for item in items[:limit]:
            compact = {
                "evidence_id": item["evidence_id"],
                "title": item["title"],
                # 动态事件关联会让解释文本更丰富；Tool 只保留回答所需的短证据，
                # 让整个 JSON 继续稳定落在 4,000 字符上下文预算以内。
                "explanation": item["explanation"][:80],
                "data_date": item["data_date"],
                "affected_fund_codes": item["affected_fund_codes"],
                "portfolio_weight_coverage_pct": item[
                    "portfolio_weight_coverage_pct"
                ],
                "weighted_points": item["weighted_points"],
                "source_name": item["source_name"],
                "source_url": item["source_url"],
            }
            # 风险和市场证据没有时间衰减，不输出重复的默认值。
            if item.get("event_id"):
                compact["decay_factor"] = item.get("decay_factor", 1.0)
                compact["freshness_status"] = item.get("freshness_status")
            compacted.append(compact)
        return compacted

    return {
        "ok": True,
        "data_kind": assessment["data_kind"],
        "tool_result_scope": "bounded_agent_summary",
        "portfolio_id": assessment["portfolio_id"],
        "portfolio_name": assessment["portfolio_name"],
        "position_mode": assessment.get("position_mode"),
        "weight_basis": assessment.get("weight_basis"),
        "period_months": assessment["period_months"],
        "assessment_date": assessment["assessment_date"],
        "state_code": assessment["state_code"],
        "state_label": assessment["state_label"],
        "confidence": assessment["confidence"],
        "summary": assessment["summary"],
        "concentration": assessment["concentration"],
        "allocations": assessment["allocations"],
        "fund_contributions": [
            {
                "fund_code": item["fund_code"],
                "short_name": item["short_name"],
                "weight_pct": item["weight_pct"],
                "assessment_state_label": item["assessment_state_label"],
                "weighted_caution_points": item["weighted_caution_points"],
                "weighted_supportive_points": item[
                    "weighted_supportive_points"
                ],
            }
            for item in assessment["fund_contributions"]
        ],
        "score_breakdown": assessment["score_breakdown"],
        # 动态基金事件会带来更多可追溯字段；主 Agent 先看最高优先级两条，
        # 其余数量仍通过 omitted_evidence_counts 告知模型。
        "risk_evidence": evidence(assessment["risk_evidence"], 2),
        "supporting_evidence": evidence(assessment["supporting_evidence"], 2),
        "neutral_evidence": evidence(assessment["neutral_evidence"], 0),
        "omitted_evidence_counts": {
            "risk": max(0, len(assessment["risk_evidence"]) - 2),
            "supporting": max(0, len(assessment["supporting_evidence"]) - 2),
            "neutral": len(assessment["neutral_evidence"]),
        },
        "evidence_count": assessment["evidence_count"],
        "event_count": assessment["event_count"],
        "event_cluster_count": assessment.get("event_cluster_count", 0),
        "active_event_count": assessment.get("active_event_count", 0),
        "expired_event_count": assessment.get("expired_event_count", 0),
        "missing_information": [
            str(item)[:100] for item in assessment["missing_information"][:3]
        ],
        "disclaimer": assessment["disclaimer"],
    }


def _bounded_exposure_result(exposure: dict) -> dict:
    """只给模型关键暴露和来源，完整长列表仍由 REST 页面按需展示。"""

    def compact_exposure(item: dict) -> dict:
        return {
            key: item[key]
            for key in (
                "evidence_id",
                "asset_type",
                "security_code",
                "security_name",
                "industry_name",
                "portfolio_exposure_pct",
                "source_fund_count",
                "report_date",
            )
            if key in item
        } | {
            "source_funds": item.get("source_funds", [])[:3],
            "source_url": item.get("source_url"),
        }

    return {
        "ok": True,
        "data_kind": exposure["data_kind"],
        "tool_result_scope": "bounded_agent_summary",
        "portfolio_id": exposure["portfolio_id"],
        "portfolio_name": exposure["portfolio_name"],
        "oldest_report_date": exposure["oldest_report_date"],
        "latest_report_date": exposure["latest_report_date"],
        "summary": exposure["summary"],
        "data_quality": exposure["data_quality"],
        "concentration": exposure["concentration"],
        "asset_exposures": exposure["asset_exposures"],
        "top_security_exposures": [
            compact_exposure(item) for item in exposure["security_exposures"][:8]
        ],
        "top_industry_exposures": [
            compact_exposure(item) for item in exposure["industry_exposures"][:6]
        ],
        "fund_overlaps": exposure["fund_overlaps"][:4],
        "fund_reports": [
            {
                "fund_code": item["fund_code"],
                "short_name": item["short_name"],
                "portfolio_weight_pct": item["portfolio_weight_pct"],
                "data_mode": item["data_mode"],
                "report_dates": item["report_dates"],
                "freshness_status": item["freshness_status"],
                "security_coverage_pct": item["security_coverage_pct"],
                "unpenetrated_pct": item["unpenetrated_pct"],
            }
            for item in exposure["fund_reports"]
        ],
        "omitted_counts": {
            "securities": max(0, len(exposure["security_exposures"]) - 8),
            "industries": max(0, len(exposure["industry_exposures"]) - 6),
            "fund_overlaps": max(0, len(exposure["fund_overlaps"]) - 4),
        },
        "limitations": exposure["limitations"],
        "disclaimer": exposure["disclaimer"],
    }


def _bounded_event_impact_result(result: dict) -> dict:
    """事件归因已是聚合结果，再删去词典 aliases 等前端展示字段。"""

    impacts = []
    for item in result["impacts"][:8]:
        impacts.append(
            {
                "impact_evidence_id": item["impact_evidence_id"],
                "event_id": item["event_id"],
                "cluster_id": item["cluster_id"],
                "title": item["title"],
                "event_date": item["event_date"],
                "direction": item["direction"],
                "horizon": item["horizon"],
                "severity": item["severity"],
                "confidence": item["confidence"],
                "freshness_status": item["freshness_status"],
                "decay_factor": item["decay_factor"],
                "source_name": item["source_name"],
                "source_url": item["source_url"],
                "affected_exposure_pct": item["affected_exposure_pct"],
                "priority_score": item["priority_score"],
                "match_basis": item["match_basis"],
                "match_label": item["match_label"],
                "matched_securities": item["matched_securities"][:5],
                "matched_sectors": item["matched_sectors"][:4],
                "matched_industries": item["matched_industries"][:3],
                "matched_funds": item["matched_funds"][:3],
                "reason": item["reason"][:180],
                "uncertainties": item["uncertainties"][:3],
            }
        )
    return {
        "ok": True,
        "data_kind": result["data_kind"],
        "tool_result_scope": "bounded_agent_summary",
        "portfolio_id": result["portfolio_id"],
        "portfolio_name": result["portfolio_name"],
        "event_data_cutoff": result["event_data_cutoff"],
        "latest_holding_report_date": result["latest_holding_report_date"],
        "summary": result["summary"],
        "quality": result["quality"],
        "event_counts": {
            "caution": result["caution_event_count"],
            "supportive": result["supportive_event_count"],
            "neutral": result["neutral_event_count"],
        },
        "top_sector_exposures": [
            {
                "sector_name": item["sector_name"],
                "portfolio_exposure_pct": item["portfolio_exposure_pct"],
                "security_count": item["security_count"],
            }
            for item in result["sector_exposures"][:6]
        ],
        "impacts": impacts,
        "omitted_impact_count": result["omitted_impact_count"],
        "methodology": result["methodology"],
        "limitations": result["limitations"],
        "disclaimer": result["disclaimer"],
    }


def _bounded_performance_result(performance: dict) -> dict:
    """删除日频曲线，只保留模型回答业绩问题真正需要的汇总指标。"""

    benchmark = performance["benchmark"]
    attribution = performance.get("attribution") or []
    return {
        "ok": True,
        "data_kind": performance["data_kind"],
        "tool_result_scope": "bounded_agent_summary",
        "portfolio_id": performance["portfolio_id"],
        "portfolio_name": performance["portfolio_name"],
        "performance_label": performance["performance_label"],
        "position_mode": performance["position_mode"],
        # 实际流水驱动的总回报业绩仍有估值假设，不能当作当前份额账本的对账金额。
        "valuation_note": (
            "期末价值、归因盈亏与权重按交易现金流和分红再投资总回报推进；不是当前份额乘净值的账本市值。"
            "不得将该模型盈亏表述为已核对的当前账户盈亏。"
            if performance["position_mode"] == "actual" else "当前目标权重的历史模拟，不是实际账户盈亏。"
        ),
        "period_months": performance["period_months"],
        "period_start": performance["period_start"],
        "period_end": performance["period_end"],
        "observation_count": performance["observation_count"],
        "returns": {
            "twr_pct": performance["twr_pct"],
            "xirr_pct": performance["xirr_pct"],
            "annualized_return_pct": performance["annualized_return_pct"],
        },
        "risk_adjusted": {
            "annualized_volatility_pct": performance[
                "annualized_volatility_pct"
            ],
            "sharpe_ratio": performance["sharpe_ratio"],
            "sortino_ratio": performance["sortino_ratio"],
            "risk_free_rate_annual_pct": performance[
                "risk_free_rate_annual_pct"
            ],
        },
        "max_drawdown": performance["max_drawdown"],
        "benchmark": {
            "benchmark_name": benchmark["benchmark_name"],
            "status": benchmark["status"],
            "period_return_pct": benchmark["period_return_pct"],
            "portfolio_aligned_return_pct": benchmark[
                "portfolio_aligned_return_pct"
            ],
            "excess_return_pct": benchmark["excess_return_pct"],
            "coverage_pct": benchmark["coverage_pct"],
            "source": benchmark.get("source"),
            "source_urls": (benchmark.get("source_urls") or [])[:3],
        },
        "cash_flows": performance["cash_flows"],
        "top_attribution": [
            {
                "fund_code": item["fund_code"],
                "short_name": item["short_name"],
                "ending_weight_pct": item["ending_weight_pct"],
                "pnl_contribution_amount": item[
                    "pnl_contribution_amount"
                ],
                "return_contribution_pct_points": item[
                    "return_contribution_pct_points"
                ],
                "return_basis_label": item["return_basis_label"],
            }
            for item in attribution[:8]
        ],
        "omitted_attribution_count": max(0, len(attribution) - 8),
        "assumptions": [str(item)[:160] for item in performance["assumptions"][:4]],
        "warnings": [str(item)[:180] for item in performance["warnings"][:5]],
        "methodology": str(performance["methodology"])[:400],
        "disclaimer": performance["disclaimer"],
    }


def _bounded_decision_result(
    *,
    portfolio_id: str,
    decisions: list[dict],
    limit: int,
) -> dict:
    """压缩历史决策，但保留“当时依据”和“后来结果”两条时间线。"""

    action_labels = {
        "watch": "继续观察",
        "hold": "维持持有",
        "add": "条件性增配",
        "reduce": "条件性减配",
        "rebalance": "重新平衡",
    }
    compacted = []
    for item in decisions[:limit]:
        snapshot = item.get("snapshot") or {}
        snapshot_performance = snapshot.get("performance") or {}
        snapshot_assessment = snapshot.get("assessment") or {}
        snapshot_research = snapshot.get("research") or None
        outcome = item.get("outcome") or {}
        compacted.append(
            {
                "decision_id": item["id"],
                "decision_date": str(item["decision_date"]),
                "target_date": str(item["target_date"]),
                "action": item["action"],
                "action_label": action_labels.get(item["action"], item["action"]),
                "thesis": str(item["thesis"])[:500],
                "horizon_days": item["horizon_days"],
                # snapshot 永远描述决策发生时看到的事实，不会被后续行情覆盖。
                "snapshot": {
                    "portfolio_name": (snapshot.get("portfolio") or {}).get(
                        "name"
                    ),
                    "performance": snapshot_performance,
                    "assessment": {
                        "assessment_date": snapshot_assessment.get(
                            "assessment_date"
                        ),
                        "state_code": snapshot_assessment.get("state_code"),
                        "state_label": snapshot_assessment.get("state_label"),
                        "summary": str(
                            snapshot_assessment.get("summary") or ""
                        )[:240],
                        "risk_evidence_ids": snapshot_assessment.get(
                            "risk_evidence_ids",
                            [],
                        )[:5],
                        "supporting_evidence_ids": snapshot_assessment.get(
                            "supporting_evidence_ids",
                            [],
                        )[:5],
                    },
                    "research": snapshot_research,
                },
                # outcome 每次读取都会用最新净值重算，因此和冻结 snapshot 分开返回。
                "outcome": {
                    "status": outcome.get("status"),
                    "as_of_date": (
                        str(outcome["as_of_date"])
                        if outcome.get("as_of_date") is not None
                        else None
                    ),
                    "elapsed_days": outcome.get("elapsed_days"),
                    "target_days": outcome.get("target_days"),
                    "portfolio_return_pct": outcome.get(
                        "portfolio_return_pct"
                    ),
                    "benchmark_return_pct": outcome.get(
                        "benchmark_return_pct"
                    ),
                    "excess_return_pct": outcome.get("excess_return_pct"),
                    "note": outcome.get("note"),
                },
                "review_note": str(item.get("review_note") or "")[:400],
                "reviewed_at": (
                    item["reviewed_at"].isoformat()
                    if item.get("reviewed_at") is not None
                    else None
                ),
            }
        )

    portfolio_name = next(
        (
            item["snapshot"]["portfolio_name"]
            for item in compacted
            if item["snapshot"].get("portfolio_name")
        ),
        None,
    )
    return {
        "ok": True,
        "data_kind": "portfolio_decision_history",
        "tool_result_scope": "bounded_agent_summary",
        "portfolio_id": portfolio_id,
        "portfolio_name": portfolio_name,
        "decision_count": len(decisions),
        "returned_count": len(compacted),
        "omitted_count": max(0, len(decisions) - len(compacted)),
        "decisions": compacted,
        "interpretation_rules": [
            "决策日志记录的是当时的判断和计划，不等于交易已经真实成交。",
            "snapshot 是冻结的历史依据；outcome 是按最新净值动态更新的观察结果。",
            "判断是否有效时应同时看原始 thesis、观察期、组合收益和基准超额。",
        ],
    }


def _bounded_research_report(report: dict) -> dict:
    """压缩报告的展示副本，但保留原问题、证据引用和未解决的缺口。"""

    assessment = report.get("assessment_snapshot") or {}
    quant_risk = report.get("quant_risk_snapshot") or {}
    performance = report.get("performance_snapshot") or {}
    market = report.get("market_snapshot") or {}
    decisions = report.get("decision_history") or {}
    research_snapshot = report.get("research_snapshot") or {}
    source_manifest = research_snapshot.get("source_manifest") or {}

    return {
        "data_kind": report.get("data_kind"),
        "research_mode": report.get("research_mode", "standard"),
        "research_plan": report.get("research_plan") or {},
        "run_id": report.get("run_id"),
        "portfolio_id": report.get("portfolio_id"),
        "portfolio_name": report.get("portfolio_name"),
        "period_months": report.get("period_months"),
        "assessment_date": report.get("assessment_date"),
        "headline": report.get("headline"),
        "summary": report.get("summary"),
        "research_snapshot": {
            "snapshot_id": research_snapshot.get("snapshot_id"),
            "schema_version": research_snapshot.get("schema_version"),
            "content_hash": research_snapshot.get("content_hash"),
            "capture_started_at": research_snapshot.get("capture_started_at"),
            "captured_at": research_snapshot.get("captured_at"),
            "immutable": research_snapshot.get("immutable", False),
            "source_manifest": {
                "source_dates": source_manifest.get("source_dates") or {},
                "record_counts": source_manifest.get("record_counts") or {},
                "versions": source_manifest.get("versions") or {},
            },
        },
        "assessment_snapshot": {
            "state_code": assessment.get("state_code"),
            "state_label": assessment.get("state_label"),
            "confidence": assessment.get("confidence"),
        },
        "quant_risk_snapshot": {
            "period_start": quant_risk.get("period_start"),
            "period_end": quant_risk.get("period_end"),
            "annualized_volatility_pct": quant_risk.get(
                "annualized_volatility_pct"
            ),
            "daily_cvar_95_pct": quant_risk.get("daily_cvar_95_pct"),
            "max_drawdown": quant_risk.get("max_drawdown"),
            "top_risk_contributor": quant_risk.get("top_risk_contributor"),
        },
        "performance_snapshot": {
            "data_kind": performance.get("data_kind"),
            "performance_label": performance.get("performance_label"),
            "position_mode": performance.get("position_mode"),
            "period_start": performance.get("period_start"),
            "period_end": performance.get("period_end"),
            "twr_pct": performance.get("twr_pct"),
            "xirr_pct": performance.get("xirr_pct"),
            "annualized_return_pct": performance.get("annualized_return_pct"),
            "benchmark": performance.get("benchmark"),
            "max_drawdown": performance.get("max_drawdown"),
        },
        "market_snapshot": {
            "data_date": market.get("data_date"),
            "state_code": market.get("state_code"),
            "state_label": market.get("state_label"),
            "summary": market.get("summary"),
            "not_requested": market.get("status") == "not_requested" or market.get("not_requested", False),
            "relevant_event_count": market.get("relevant_event_count"),
            "research_repair": market.get("research_repair") or {},
            "fund_reports": {key: (market.get("fund_reports") or {}).get(key)
                for key in ("status", "requested_fund_codes", "omitted_fund_codes", "documents", "warnings", "limitations")},
            # 报告追问继续使用原简报的时点和来源，不能重新查最新行情解释旧结论。
            **({"daily_briefing": {key: market["daily_briefing"].get(key)
                for key in ("trade_date", "captured_at", "funds", "indices", "hotspots", "evidence", "warnings")}}
                if market.get("daily_briefing") else {}),
        },
        "decision_history": {
            "decision_count": decisions.get("decision_count", 0),
            "has_decisions": decisions.get("has_decisions", False),
            "note": decisions.get("note"),
            "decisions": list(decisions.get("decisions") or [])[:3],
        },
        "key_findings": [
            # 引用必须和观点一起传递；只传编号会让追问模型猜原文或重查最新值。
            {key: item[key] for key in (
                "claim_id", "tone", "title", "detail", "fund_codes", "evidence_ids", "metric_keys",
                "evidence_references", "metric_references") if key in item}
            for item in list(report.get("key_findings") or [])[:12]
        ],
        "uncertainties": report.get("uncertainties") or [],
        "limitations": report.get("limitations") or [],
        "generation": report.get("generation") or {},
        # 从报告继续追问时保留双方观点和质询引用，不只传最终总结。这里仍然只读
        # 原报告，不重新启动投研会，也不会拿今天的数据解释昨天的分歧。
        **({"committee": {
            "version": report["committee"].get("version"),
            "status": report["committee"].get("status"),
            "snapshot_id": report["committee"].get("snapshot_id"),
            "synthesis": {key: (report["committee"].get("synthesis") or {}).get(key)
                for key in ("status", "failure_reason")},
            "roles": [{key: row.get(key) for key in (
                "role", "label", "status", "headline", "summary", "key_findings", "uncertainties", "failure_reason"
            )} for row in report["committee"].get("roles", [])[:2]],
            "reviews": [{key: row.get(key) for key in (
                "role", "label", "status", "responses", "uncertainties", "failure_reason"
            )} for row in report["committee"].get("reviews", [])[:2]],
            "warnings": report["committee"].get("warnings", []),
        }} if report.get("committee") else {}),
        "disclaimer": report.get("disclaimer"),
    }


def _bounded_research_job_result(
    service_result: dict,
    *,
    action: str,
    portfolio_id: str,
    portfolio_name: str | None = None,
    period_months: int | None = None,
) -> dict:
    """统一 start/status 两个 Tool 的返回结构，并隐藏 Worker 内部 payload。"""

    job = service_result["job"]
    result = job.get("result") if job.get("status") == "completed" else None
    report = _bounded_research_report(result) if isinstance(result, dict) else None
    resolved_portfolio_id = str(
        (result or {}).get("portfolio_id") or portfolio_id
    ).strip()
    resolved_portfolio_name = (
        (result or {}).get("portfolio_name") or portfolio_name
    )
    resolved_months = (result or {}).get("period_months") or period_months

    return {
        "ok": True,
        "data_kind": "portfolio_research_job",
        "action": action,
        "accepted": service_result.get("accepted"),
        "message": service_result.get("message"),
        "portfolio_id": resolved_portfolio_id,
        "portfolio_name": resolved_portfolio_name,
        "period_months": resolved_months,
        "job": {
            key: job.get(key)
            for key in (
                "id",
                "job_type",
                "title",
                "status",
                "current_step",
                "total_steps",
                "progress_percent",
                "progress_message",
                "error",
                "created_at",
                "updated_at",
                "completed_at",
            )
        },
        "events_endpoint": f"/background-jobs/{job['id']}/events",
        "report": report,
    }


tool = _load_tool_decorator()


@tool(
    "get_portfolio_assessment_tool",
    args_schema=PortfolioAssessmentToolInput,
    description=(
        "查询 FundScope 当前用户一份模拟基金组合的确定性研判。"
        "当用户询问我的组合、整体配置、集中度、哪只基金贡献主要风险或组合支撑因素"
        "时使用。返回持仓权重、集中度、加权证据分和关键 evidence_id。"
        "相似报道已经按 cluster_id 去重；事件分包含确定性时间衰减，"
        "热度用于观察而不直接放大组合分。"
        "成功后证据已足够完成整体回答，不要重复调用单基金或市场工具。"
    ),
)
def get_portfolio_assessment_tool(
    user_id: str,
    portfolio_id: str,
    months: int = 12,
) -> str:
    """让 Agent 和组合页面复用同一个确定性计算服务。"""

    try:
        assessment = portfolio_assessment_service.assess(
            user_id=user_id,
            portfolio_id=portfolio_id,
            months=months,
        )
    except (PortfolioNotFoundError, PortfolioError, PortfolioAssessmentError) as exc:
        return business_tool_error(exc, portfolio_id=portfolio_id)
    return json.dumps(_bounded_portfolio_result(assessment), ensure_ascii=False)


@tool(
    "get_portfolio_exposure_tool",
    args_schema=PortfolioExposureToolInput,
    description=(
        "查询 FundScope 当前组合的底层持仓穿透。用户询问底层股票或债券、行业暴露、"
        "多只基金是否重复持仓、隐性集中风险、季报覆盖率或未穿透资产时使用。"
        "结果由 Python 按基金当前市值权重乘以季报持仓权重计算，携带报告日期、"
        "freshness_status、coverage 和 evidence_id。不要把季报描述成实时仓位。"
    ),
)
def get_portfolio_exposure_tool(user_id: str, portfolio_id: str) -> str:
    """让 Agent 使用与前端完全相同的确定性穿透结果。"""

    try:
        exposure = portfolio_exposure_service.analyze(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
    except (PortfolioNotFoundError, PortfolioError, PortfolioExposureError) as exc:
        return business_tool_error(exc, portfolio_id=portfolio_id)
    return json.dumps(_bounded_exposure_result(exposure), ensure_ascii=False)


@tool(
    "get_portfolio_event_impacts_tool",
    args_schema=PortfolioEventImpactToolInput,
    description=(
        "查询最近市场消息、政策或基金公告具体影响当前组合的哪些底层证券、细分行业或"
        "持仓基金。用户询问哪些消息影响我、新闻与持仓的关系、政策影响范围、事件风险"
        "归因时使用。结果由 Python 使用已校验事件语义、时间衰减和季报权重计算，"
        "affected_exposure_pct 只是可关联仓位，不是预计涨跌幅。"
    ),
)
def get_portfolio_event_impacts_tool(
    user_id: str,
    portfolio_id: str,
    limit: int = 8,
) -> str:
    """给 Agent 提供与前端同源、带事件和持仓证据 ID 的归因结果。"""

    try:
        result = portfolio_event_impact_service.analyze(
            user_id=user_id,
            portfolio_id=portfolio_id,
            limit=limit,
        )
    except (PortfolioNotFoundError, PortfolioEventImpactError) as exc:
        return json.dumps(
            {
                "ok": False,
                "portfolio_id": portfolio_id,
                "error": str(exc),
            },
            ensure_ascii=False,
        )
    return json.dumps(_bounded_event_impact_result(result), ensure_ascii=False)


@tool(
    "get_portfolio_performance_tool",
    args_schema=PortfolioPerformanceToolInput,
    description=(
        "查询 FundScope 当前组合的历史业绩、基准比较和基金收益归因。用户询问组合"
        "赚亏、TWR、XIRR、年化收益、波动率、最大回撤、夏普、Sortino、超额收益或"
        "哪只基金贡献收益时使用。必须根据 data_kind 区分真实交易业绩和目标配置模拟；"
        "结果已经包含回答所需汇总，不要为了相同指标重复调用单基金风险工具。"
    ),
)
def get_portfolio_performance_tool(
    user_id: str,
    portfolio_id: str,
    months: int = 12,
) -> str:
    """给 Agent 返回不含数百个日频点的组合业绩摘要。"""

    try:
        performance = portfolio_performance_service.analyze(
            user_id=user_id,
            portfolio_id=portfolio_id,
            months=months,
        )
    except (PortfolioNotFoundError, PortfolioError, PortfolioPerformanceError) as exc:
        return business_tool_error(exc, portfolio_id=portfolio_id)
    return json.dumps(_bounded_performance_result(performance), ensure_ascii=False)


@tool(
    "list_portfolio_decisions_tool",
    args_schema=PortfolioDecisionListToolInput,
    description=(
        "只读查询 FundScope 当前组合最近的投资决策日志和决策后的动态表现。用户询问"
        "以前为什么观察、持有、增配或减配，历史判断后来是否成立，或者要求复盘时使用。"
        "snapshot 是当时冻结的依据，outcome 是最新净值重算的结果；决策记录不代表交易"
        "实际成交。本工具不能新增、修改或删除决策。"
    ),
)
def list_portfolio_decisions_tool(
    user_id: str,
    portfolio_id: str,
    limit: int = 5,
) -> str:
    """让 Agent 在不获得写权限的前提下复盘用户自己的历史决策。"""

    try:
        decisions = portfolio_decision_service.list(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
    except (
        PortfolioNotFoundError,
        PortfolioError,
        PortfolioDecisionError,
        PortfolioPerformanceError,
    ) as exc:
        return json.dumps(
            {
                "ok": False,
                "portfolio_id": portfolio_id,
                "error": str(exc),
            },
            ensure_ascii=False,
        )
    return json.dumps(
        _bounded_decision_result(
            portfolio_id=portfolio_id,
            decisions=decisions,
            limit=limit,
        ),
        ensure_ascii=False,
    )


@tool(
    "start_portfolio_research_tool",
    args_schema=PortfolioResearchStartToolInput,
    description=(
        "按当前问题启动所选基金组合的 LangGraph 研究，保存问题和计划；简单解释、季报比较和深入研判走不同路径。只有用户明确要求运行、"
        "生成或重新执行完整组合研究时使用；普通组合问答应使用对应只读工具。"
        "本工具立即返回 job_id，不等待研究完成，前端会通过 SSE 展示读取组合、规则证据、"
        "量化风险、历史业绩、市场上下文、决策历史、AI 综合和报告生成进度。"
        "同一请求成功后不要再次调用本工具。"
    ),
)
def start_portfolio_research_tool(
    user_id: str,
    portfolio_id: str,
    months: int = 12,
    idempotency_key: str | None = None,
    question: str = "",
) -> str:
    """确认组合归属后，把现有 LangGraph 工作流提交给 Background Job。"""

    try:
        portfolio = portfolio_service.get(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
        from app.services.financial_research_repair import current_repair_budget
        from app.services.investment_context import current_investment_context
        context = current_investment_context()
        budget = current_repair_budget()
        # 联网授权只来自本轮请求，不能在 Tool Schema 中让模型自行打开。
        result = background_job_service.start_job(
            user_id=user_id,
            title=f"{portfolio['name']}组合研究",
            job_type="portfolio_research",
            portfolio_id=portfolio_id,
            portfolio_months=months,
            idempotency_key=idempotency_key,
            research_question=question,
            research_mode="auto" if question else "standard",
            research_allow_public_refresh=bool(budget and budget.allow_public_refresh),
            research_investor_context={key: fact.text for key, fact in context.facts.items()
                if key in {"horizon", "loss_tolerance", "liquidity"}} if context and not context.needs_confirmation else {},
        )
    except (PortfolioNotFoundError, PortfolioError, ValueError, RuntimeError) as exc:
        return json.dumps(
            {
                "ok": False,
                "portfolio_id": portfolio_id,
                "error": str(exc),
            },
            ensure_ascii=False,
        )
    return json.dumps(
        _bounded_research_job_result(
            result,
            action="started",
            portfolio_id=portfolio_id,
            portfolio_name=portfolio["name"],
            period_months=months,
        ),
        ensure_ascii=False,
    )


@tool(
    "get_portfolio_research_status_tool",
    args_schema=PortfolioResearchStatusToolInput,
    description=(
        "查询一条已经启动的组合深度研究 Job 的当前进度或最终摘要。用户明确询问某个"
        "研究 job_id 是否完成、运行到哪一步或结果是什么时使用。只查询一次当前快照，"
        "不要在同一轮对话中循环轮询；前端会通过 SSE 自动持续更新。"
    ),
)
def get_portfolio_research_status_tool(
    user_id: str,
    portfolio_id: str,
    job_id: str,
) -> str:
    """按可信用户读取 Job；已完成时附带一份压缩研究报告。"""

    try:
        # 先读取组合，既补充名称，也确保当前页面组合确实属于该用户。
        portfolio = portfolio_service.get(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
        result = background_job_service.get_job(
            user_id=user_id,
            job_id=job_id,
        )
        job = result["job"]
        if job.get("job_type") != "portfolio_research":
            raise ValueError("该 job_id 不是组合深度研究任务。")
        # 排队中的 Job 没有 result，也必须核对建单时已经落库的组合归属。
        actual_portfolio_id = str((job.get("payload") or {}).get("portfolio_id")
            or (job.get("result") or {}).get("portfolio_id") or "")
        if actual_portfolio_id != portfolio_id:
            raise ValueError("该研究 Job 不属于当前选中的组合。")
    except (KeyError, PortfolioNotFoundError, PortfolioError, ValueError) as exc:
        return json.dumps(
            {
                "ok": False,
                "portfolio_id": portfolio_id,
                "job_id": job_id,
                "error": str(exc),
            },
            ensure_ascii=False,
        )
    return json.dumps(
        _bounded_research_job_result(
            result,
            action="status",
            portfolio_id=portfolio_id,
            portfolio_name=portfolio["name"],
        ),
        ensure_ascii=False,
    )


def get_portfolio_agent_tools() -> list:
    """集中返回组合工具，供主 Agent 工具池合并。"""

    return [
        get_portfolio_assessment_tool,
        get_portfolio_performance_tool,
        get_portfolio_exposure_tool,
        get_portfolio_event_impacts_tool,
        list_portfolio_decisions_tool,
        start_portfolio_research_tool,
        get_portfolio_research_status_tool,
    ]

"""把冻结研究报告整理成两端共享的展示结构，不查新数据、不调用模型、不重新评分。"""

from math import isfinite
from urllib.parse import urlsplit


def _text(value) -> str:
    return "--" if value is None or value == "" else str(value)


def _percent(value) -> str:
    return f"{value:.2f}%" if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else "--"


def _source_url(value: str | None) -> str:
    # 来源只能是公开网页，不把历史资料中的脚本、本地路径或凭据变成可点击链接。
    try:
        parsed = urlsplit(value or "")
        return value if parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username and not parsed.password else ""
    except ValueError:
        return ""


def build_portfolio_diagnosis(report: dict) -> dict:
    """只映射已有字段。前三条沿用原报告顺序，其余观点仍保留供展开阅读。"""
    assessment = report.get("assessment_snapshot") or {}
    quant = report.get("quant_risk_snapshot") or {}
    interpretation = report.get("model_interpretation") or {}
    generation = report.get("generation") or {}
    snapshot = report.get("research_snapshot") or {}
    # “报告时持仓”只能从该报告取，不能读此刻的账本，否则旧结论会配上新仓位。
    funds = {row["fund_code"]: dict(row) for row in assessment.get("fund_contributions", [])}
    for row in quant.get("risk_contributions", []):
        funds[row["fund_code"]] = {**funds.get(row["fund_code"], {}), **row}

    # 只按准确编号关联证据。不能用标题相似度推断“这条消息影响了哪只基金”。
    sources = {row["evidence_id"]: row for rows in (report.get("key_evidence") or {}).values() for row in rows}
    findings = []
    for index, finding in enumerate(report.get("key_findings") or []):
        references = []
        evidence_refs = {row["evidence_id"]: row for row in finding.get("evidence_references", [])}
        evidence_ids = list(dict.fromkeys([*finding.get("evidence_ids", []), *evidence_refs]))
        for evidence_id in evidence_ids:
            row = {**sources.get(evidence_id, {}), **evidence_refs.get(evidence_id, {})}
            references.append({"id": evidence_id, "label": row.get("title") or evidence_id,
                "value": _text(row.get("value_text")), "date": _text(row.get("data_date")),
                "source": row.get("source_name") or "报告未保留来源明细",
                "url": _source_url(row.get("source_url")),
                "explanation": row.get("summary") or row.get("explanation") or "报告未保留该引用的解释。"})
        metrics = finding.get("metric_references") or []
        for row in metrics:
            references.append({"id": row.get("key"), "label": row.get("label") or row.get("key"),
                "value": _text(row.get("value_text", row.get("value"))), "date": _text(row.get("data_date")),
                "source": "报告内计算指标", "url": "", "explanation": ""})
        # fund_codes 是原模型输出中经过白名单检查的关联，不从自由文本猜基金代码。
        related = []
        for code in dict.fromkeys(finding.get("fund_codes") or []):
            row = funds.get(code, {})
            related.append({"code": code, "name": row.get("short_name") or code,
                "weight": _percent(row.get("weight_pct")),
                "risk_share": _percent(row.get("absolute_risk_share_pct"))})
        findings.append({"id": f"finding-{index}", "tone": finding.get("tone", "neutral"),
            "tone_label": {"caution": "需关注", "supportive": "支撑因素", "neutral": "中性观察"}.get(finding.get("tone"), "待判断"),
            "title": finding.get("title") or "研究发现", "detail": finding.get("detail") or "",
            "scope": "关联持仓" if related else "组合整体 · 未指向具体基金",
            "holdings": related, "references": references,
            "gap": "" if references else "这条观点未附具体数值或来源，需进一步核验。"})

    def fund_row(row):
        return {"code": row.get("fund_code"), "name": row.get("short_name") or row.get("fund_code"),
                "weight": _percent(row.get("weight_pct")), "risk_share": _percent(row.get("absolute_risk_share_pct"))}

    used_model = generation.get("model_used", interpretation.get("model_used"))
    return {
        "version": "portfolio-diagnosis-v1", "report_id": report.get("run_id"),
        "portfolio_name": report.get("portfolio_name"), "report_date": _text(report.get("assessment_date")),
        "performance_label": (report.get("performance_snapshot") or {}).get("performance_label") or "业绩口径未记录",
        "headline": report.get("headline") or "报告尚无核心结论", "summary": report.get("summary") or "",
        "interpretation_label": "AI 研判" if used_model is True else "规则回退 · 非 AI 结论" if used_model is False else "生成方式未记录",
        "fallback_reason": generation.get("fallback_reason") or interpretation.get("fallback_reason") or "",
        "findings": findings[:3], "more_findings": findings[3:],
        "holdings": [fund_row(row) for row in funds.values()],
        "top_risk": fund_row(quant["top_risk_contributor"]) if quant.get("top_risk_contributor") else None,
        "risk_period": f"{_text(quant.get('period_start'))} 至 {_text(quant.get('period_end'))}",
        "conflicts": interpretation.get("conflicts") or [],
        "uncertainties": interpretation.get("uncertainties") or [],
        "conditional_view": interpretation.get("conditional_view") or "",
        "follow_ups": interpretation.get("follow_up_items") or [],
        "limitations": report.get("limitations") or [],
        "questions": ["为什么这样判断？请解释主要依据。", "哪只基金贡献了最多历史风险？为什么？", "什么变化会改变这份报告的结论？"],
        # 追溯字段仍然保留，只是由前端收进技术详情，不抢占结论的阅读位置。
        "technical": [{"label": label, "value": _text(value)} for label, value in (
            ("报告编号", report.get("run_id")), ("冻结时间", snapshot.get("captured_at")),
            ("快照编号", snapshot.get("snapshot_id")), ("输入指纹", snapshot.get("content_hash")),
            ("模型", generation.get("model") or interpretation.get("model")),
            ("提示词版本", generation.get("prompt_version") or interpretation.get("prompt_version")),
            ("流程版本", report.get("graph_version")),
            ("检查点存储", (report.get("graph_trace") or {}).get("checkpoint_backend")))],
    }


def report_with_diagnosis(report: dict) -> dict:
    # 展示结构随读取生成，不回写旧报告或冻结快照；旧报告同样能使用新页面。
    return {**report, "diagnosis": build_portfolio_diagnosis(report)}

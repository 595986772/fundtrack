"""组合研究的有限计划：用可审计规则选路径，不让模型任意增加阶段和费用。"""

PLAN_VERSION = "portfolio-plan-v1"
MODES = ("auto", "explain", "strategy", "standard", "committee")
LABELS = {"explain": "配置解释", "strategy": "季报策略比较", "standard": "综合分析", "committee": "多角色研判"}


def build_research_plan(question="", mode="standard", *, allow_public_refresh=False, investor_context=None):
    """只持久化 JSON 数据。显式模式优先，自动模式默认走综合分析，不冒充理解任意意图。

    所有路径保留确定性风险/业绩基线；轻路径跳过额外市场读取和决策历史。
    投资条件来自用户填入的字段，不从基金新闻猜测；缺失时提示，不给适当性结论。
    """
    if mode not in MODES:
        raise ValueError("不支持的 research_mode 组合研究模式")
    question = question.strip()
    if len(question) > 800:
        raise ValueError("研究问题不能超过800字")
    context = investor_context or {}
    if set(context) - {"horizon", "loss_tolerance", "liquidity"}:
        raise ValueError("投资条件包含不支持的字段")
    context = {key: str(value).strip() for key, value in context.items() if str(value).strip()}
    if any(len(value) > 240 for value in context.values()):
        raise ValueError("每项投资条件不能超过240字")
    selected, reason = mode, "用户显式选择"
    if mode == "auto":
        if any(word in question for word in ("深入", "多角色", "多Agent", "多agent", "反对理由", "交叉质询")):
            selected, reason = "committee", "问题要求多角度质询"
        elif any(word in question for word in ("季报", "季度报告", "经理观点", "策略变化", "策略比较")):
            selected, reason = "strategy", "问题主要涉及历史披露和策略比较"
        elif any(word in question for word in ("解释", "什么意思", "为什么", "怎么看", "收益来源")):
            selected, reason = "explain", "先解释已有持仓和确定性指标"
        else:
            selected, reason = "standard", "未匹配专门路径，使用综合分析"
    question = question or "分析当前组合的收益来源、主要风险与证据缺口。"
    focused = selected in {"explain", "strategy"}
    needs_personal = any(word in question for word in ("适合我", "买入", "加仓", "减仓", "卖出", "调仓"))
    questions = {"horizon": "这笔资金预计持有多久？", "loss_tolerance": "你能接受多大的阶段性亏损？",
                 "liquidity": "近期是否需要使用这笔资金？"}
    pending = [questions[key] for key in questions if needs_personal and key not in context]
    return {"version": PLAN_VERSION, "requested_mode": mode, "mode": selected, "label": LABELS[selected],
        "question": question, "reason": reason, "investor_context": context,
        "clarification_questions": pending, "allow_public_refresh": bool(allow_public_refresh),
        "sources": ["assessment", "quant_risk", "performance"]
            + ([] if focused else ["market", "decisions"])
            + ([] if selected == "explain" else ["fund_reports"]),
        "total_steps": 4 if focused else 13 if selected == "committee" else 8,
        "interpretation_call_limit": 5 if selected == "committee" else 2,
        "limitations": (["本轮仅解释确定性指标，不额外读取市场事件、决策历史或季报。"]
            if selected == "explain" else ["策略比较基于历史季报，不读取当日市场与决策历史。"]
            if selected == "strategy" else [])
            + (["个人投资条件不完整；本轮仅分析配置事实，不作是否适合个人的完整结论。"] if pending else [])}

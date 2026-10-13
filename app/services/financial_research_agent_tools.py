"""把金融研究计划器包装成主 Agent 的两个综合研究 Tool。

单基金和组合使用两个独立工具，是为了让模型的参数 schema 保持简单，也让后端可以
分别覆盖可信 fund_code、portfolio_id 和 user_id。工具只是入口，所有计算仍由
``FinancialResearchBriefService`` 中的 Python 服务完成。
默认只读；公开补查授权通过后端 ContextVar 传递，不放进模型可填写的参数 schema。
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from app.services.financial_research_brief import (
    FinancialResearchBriefError,
    financial_research_brief_service,
)


class FundResearchBriefToolInput(BaseModel):
    """单基金综合研究需要基金、原问题和固定观察区间。"""

    fund_code: str = Field(
        ...,
        pattern=r"^\d{6}$",
        description="FundScope 当前选中的六位基金代码。",
    )
    question: str = Field(
        default="",
        max_length=800,
        description="用户原始问题；执行时由后端可信请求覆盖，模型不需要改写。",
    )
    months: Literal[6, 12, 36] = Field(
        default=12,
        description="历史研究区间，只支持 6、12 或 36 个月。",
    )


class PortfolioResearchBriefToolInput(BaseModel):
    """组合综合研究额外需要当前可信用户和组合 ID。"""

    user_id: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="当前用户 ID，由后端自动注入。",
    )
    portfolio_id: str = Field(
        ...,
        pattern=r"^portfolio_[a-f0-9]{16}$",
        description="FundScope 当前选中的组合 ID。",
    )
    question: str = Field(
        default="",
        max_length=800,
        description="用户原始问题；执行时由后端可信请求覆盖，模型不需要改写。",
    )
    months: Literal[6, 12, 36] = Field(
        default=12,
        description="历史研究区间，只支持 6、12 或 36 个月。",
    )


def _load_tool_decorator():
    """延迟导入 LangChain，避免普通 REST 启动被 Tool 依赖绑死。"""

    from langchain_core.tools import tool

    return tool


tool = _load_tool_decorator()


@tool(
    "get_fund_research_brief_tool",
    args_schema=FundResearchBriefToolInput,
    description=(
        "对当前基金执行一次完整、受约束的金融研究流程。仅在用户明确要求全面、系统、"
        "多维度分析，或要求同时结合风险指标、业绩基准、披露持仓、市场环境和事件时使用。"
        "工具先生成研究计划，再并行读取确定性业务服务，并返回 evidence_gate。"
        "发现缺口会在预算内补查并返回 evidence_repair；公开刷新必须由用户本轮授权。"
        "成功后不要再调用基金风险、综合研判或市场工具重复查询；请按照 answer_contract "
        "解释数据充分度、事实、传导路径和缺口。"
    ),
)
def get_fund_research_brief_tool(
    fund_code: str,
    question: str = "",
    months: int = 12,
) -> str:
    """生成单基金综合研究 Brief；本函数本身不调用大模型。"""

    try:
        result = financial_research_brief_service.build_fund(
            fund_code=fund_code,
            question=question,
            months=months,
        )
    except (FinancialResearchBriefError, ValueError) as exc:
        result = {
            "ok": False,
            "data_kind": "deterministic_financial_research_brief",
            "scope": {"kind": "fund", "fund_code": fund_code},
            "error": str(exc),
        }
    return json.dumps(result, ensure_ascii=False)


@tool(
    "get_portfolio_research_brief_tool",
    args_schema=PortfolioResearchBriefToolInput,
    description=(
        "对当前用户的基金组合执行一次完整、受约束的金融研究流程。仅在用户明确要求"
        "全面、系统、多维度分析，或同时询问组合风险、历史业绩、底层暴露、事件影响时使用。"
        "工具并行读取规则研判、Riskfolio 量化风险、真实/模拟业绩、季报穿透和事件归因，"
        "再由 Python evidence_gate 判断证据是否足够。成功后不要重复调用这些底层工具。"
        "缺口补查记录在 evidence_repair；公开刷新必须由用户本轮授权。"
    ),
)
def get_portfolio_research_brief_tool(
    user_id: str,
    portfolio_id: str,
    question: str = "",
    months: int = 12,
) -> str:
    """生成当前用户组合的综合研究 Brief；不会启动后台 Job 或执行交易。"""

    try:
        result = financial_research_brief_service.build_portfolio(
            user_id=user_id,
            portfolio_id=portfolio_id,
            question=question,
            months=months,
        )
    except (FinancialResearchBriefError, ValueError) as exc:
        result = {
            "ok": False,
            "data_kind": "deterministic_financial_research_brief",
            "scope": {"kind": "portfolio", "portfolio_id": portfolio_id},
            "error": str(exc),
        }
    return json.dumps(result, ensure_ascii=False)


def get_financial_research_agent_tools() -> list:
    """集中返回新工具，供主 Agent 注册表按能力过滤。"""

    return [get_fund_research_brief_tool, get_portfolio_research_brief_tool]

"""Agent只整理草案和读取结果，不拥有人工确认/求解接口。"""
import json
from copy import deepcopy

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from app.services.portfolio_allocation import portfolio_allocation_service
from app.services.portfolio_allocation_contracts import AllocationDraftRequest, AllocationProposal
from app.services.portfolio_stress_tools import summarize_report


class AllocationToolInput(AllocationDraftRequest):
    portfolio_id: str = Field(pattern=r"^portfolio_[a-f0-9]{16}$")
    proposal: AllocationProposal


class AllocationReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(default="guest", min_length=1, max_length=50)
    portfolio_id: str = Field(pattern=r"^portfolio_[a-f0-9]{16}$")
    draft_id: str | None = Field(default=None, pattern=r"^alloc_[a-f0-9]{16}$")


def summarize_allocation(value):
    result = deepcopy(value)
    if (result.get("result") or {}).get("stress_report"):
        result["result"]["stress_report"] = summarize_report(result["result"]["stress_report"])
    return result


@tool("draft_portfolio_allocation_tool", args_schema=AllocationToolInput, description=(
    "将用户自然语言整理为约束求解草案，仅保存待确认条件，不计算候选权重。"
    "支持债券类基金最低占比、单只基金最高占比、组合单边换手上限(权重绝对变化之和/2)，"
    "单位百分数40表示40%。没提到的边界分别填0/100/100；不大幅调仓等模糊要求填null并追问。"
    "同字段冲突填null并追问。其他条件必须列入unsupported_constraints，不得忽略。"
    "目标为历史单日95%CVaR最小、固定现有基金池、长仓合计100%；不等价于未来或最大回撤保证。"
    "返回draft_id后引导用户进入组合的约束求解页读取记录、核对数字并点击确认。不能声称已优化。"
    "question/user_id/idempotency_key由后端注入。若沿用压力情景传source_run_id。"
))
def draft_portfolio_allocation_tool(**kwargs) -> str:
    portfolio_id = kwargs.pop("portfolio_id")
    proposal = AllocationProposal.model_validate(kwargs.pop("proposal"))
    result = portfolio_allocation_service.create(portfolio_id=portfolio_id, request=AllocationDraftRequest(**kwargs), proposal=proposal)
    return json.dumps(summarize_allocation(result), ensure_ascii=False)


@tool("get_portfolio_allocation_tool", args_schema=AllocationReadInput, description=(
    "只读约束草案/已确认求解结果。传draft_id读取原配置、候选权重、约束核对、风险权衡、"
    "同一历史压力情景及facts；不传则列最近20条。追问沿用对话中的draft_id，不能自选别的会话记录。"
    "awaiting_confirmation只能提示用户前往约束求解页确认；infeasible解释冲突并追问，不能编造权重。"
))
def get_portfolio_allocation_tool(user_id: str, portfolio_id: str, draft_id: str | None = None) -> str:
    result = portfolio_allocation_service.get(user_id=user_id, portfolio_id=portfolio_id, draft_id=draft_id) if draft_id else {
        "ok": True, "drafts": portfolio_allocation_service.list(user_id=user_id, portfolio_id=portfolio_id)}
    return json.dumps(summarize_allocation(result), ensure_ascii=False)

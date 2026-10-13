"""约束求解的数据契约：模型提议可以留空，用户确认必须给出明确数字。"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


Percent = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]


class AllocationConstraints(BaseModel):
    """只支持现有基金、非负权重、合计100%；百分数输入40表示40%，不是0.4。"""
    model_config = ConfigDict(extra="forbid")
    min_bond_weight_pct: Percent
    max_single_fund_weight_pct: Percent
    # 单边换手 = 各基金权重变化绝对值之和 / 2，不是单只调仓幅度。
    max_turnover_pct: Percent


class AllocationProposal(BaseModel):
    """未提要求使用中性边界；提了但模糊则留空，前端要求用户补充。"""
    model_config = ConfigDict(extra="forbid")
    min_bond_weight_pct: Percent | None = None
    max_single_fund_weight_pct: Percent | None = None
    max_turnover_pct: Percent | None = None
    questions: list[Annotated[str, Field(max_length=240)]] = Field(default_factory=list, max_length=6)
    unsupported_constraints: list[Annotated[str, Field(max_length=240)]] = Field(default_factory=list, max_length=6)


class AllocationDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(default="guest", min_length=1, max_length=50)
    question: str = Field(min_length=2, max_length=2000)
    source_run_id: str | None = Field(default=None, pattern=r"^stress_[a-f0-9]{16}$")
    idempotency_key: str = Field(min_length=1, max_length=100)


class AllocationConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(default="guest", min_length=1, max_length=50)
    draft_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    constraints: AllocationConstraints
    # 只在用户点击确认表单时发送；Agent 没有确认/求解工具。
    confirm: Literal[True]

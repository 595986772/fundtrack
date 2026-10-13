"""压力沙盘的 Agent 边界：模型填参数，Python 计算，仅保存分析记录。"""
import json
from copy import deepcopy

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.schemas import PortfolioStressRequest
from app.services.portfolio_stress import portfolio_stress_service


class StressToolInput(PortfolioStressRequest):
    portfolio_id: str = Field(pattern=r"^portfolio_[a-f0-9]{16}$")


class StressReadInput(BaseModel):
    user_id: str = Field(default="guest", min_length=1, max_length=50)
    portfolio_id: str = Field(pattern=r"^portfolio_[a-f0-9]{16}$")
    run_id: str | None = Field(default=None, pattern=r"^stress_[a-f0-9]{16}$")


def summarize_report(report: dict) -> dict:
    """曲线供前端画图，不必逐点塞给模型。保留权重、贡献和证据编号供解释。"""
    result = deepcopy(report)
    for scenario in result["scenarios"]:
        scenario.pop("curve", None)
    # 详细来源始终在原报告，工具保留每只基金的口径和时间字段，控制上下文体积。
    result["data_sources"] = [{k: v for k, v in source.items() if k in {
        "fund_code", "return_basis", "return_basis_label", "source", "source_name",
        "nav_start_date", "nav_end_date", "start_date", "end_date", "latest_nav_date",
        "snapshot_end", "return_basis_status",
    }} for source in report["data_sources"]]
    return {"ok": True, "data_kind": "portfolio_stress", **result}


@tool("run_portfolio_stress_tool", args_schema=StressToolInput, description=(
    "历史组合压力重演。默认从本地过去12个月净值选原配置最差连续20个共同观察日。"
    "months支持6/12/36，window_days支持20/40/60。返回原配置、基金贡献和报告编号。"
    "追问比较必须传之前的source_run_id和全部基金target_weights百分比(合计100)，"
    "不明确减仓资金转到哪里时先追问。比较不要填写months/window_days以继承原情景。"
    "不会自动刷新行情或修改持仓；仅新建分析记录。user_id和idempotency_key由后端注入。"
))
def run_portfolio_stress_tool(**kwargs) -> str:
    portfolio_id = kwargs.pop("portfolio_id")
    report = portfolio_stress_service.run(portfolio_id=portfolio_id, request=PortfolioStressRequest(**kwargs))
    return json.dumps(summarize_report(report), ensure_ascii=False)


@tool("get_portfolio_stress_tool", args_schema=StressReadInput, description=(
    "读取已保存历史压力报告，不重新计算。提供run_id读取同情景原配置/候选配置、贡献和证据；"
    "没有run_id时只列最近20条摘要。追问沿用对话里的报告编号，不擅自选择别的会话的最新报告。"
))
def get_portfolio_stress_tool(user_id: str, portfolio_id: str, run_id: str | None = None) -> str:
    if run_id:
        payload = summarize_report(portfolio_stress_service.get(user_id=user_id, portfolio_id=portfolio_id, run_id=run_id))
    else:
        payload = {"ok": True, "runs": portfolio_stress_service.list(user_id=user_id, portfolio_id=portfolio_id)}
    return json.dumps(payload, ensure_ascii=False)

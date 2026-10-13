"""模型只读季报证据；下载和更新索引必须由用户在页面手动触发。"""
import json
from pydantic import BaseModel, Field
from langchain_core.tools import tool
from app.services.fund_reports import get_fund_report_reader


class FundReportInput(BaseModel):
    fund_code: str = Field(pattern=r"^\d{6}$")
    question: str = Field(min_length=1, max_length=800)
    document_ids: list[str] | None = Field(default=None, max_length=2,
        description="已选择的两期季报编号；未指定时读取已保存的最近两期，不联网同步。")


@tool("get_fund_report_research_tool", args_schema=FundReportInput)
def get_fund_report_research_tool(fund_code: str, question: str, document_ids=None) -> str:
    """检索基金两期季度报告原文、基金经理运作分析，并返回资产比例的确定性比较与 PDF 页码证据。"""
    try:
        result = get_fund_report_reader().research(fund_code, question, document_ids)
    except ValueError as exc:
        result = {"ok": False, "error": str(exc)}
    return json.dumps(result, ensure_ascii=False)

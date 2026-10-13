"""季报多轮上下文：保存不可变文档编号，而不是每次追问重新选择“最新两期”。"""
import json

from app.services.errors import AgentRuntimeError
from app.services.fund_reports import fund_report_service, get_fund_report_reader


def resolve_fund_report_context(request, previous, reader=None):
    reader = reader or get_fund_report_reader()
    explicit = request.fund_report_ids
    if explicit == []:
        return None, None
    old = previous.get("fund_report_reference") or {}
    ids = explicit if explicit is not None else old.get("document_ids")
    if not ids:
        return None, None
    if request.portfolio_id or request.research_job_id:
        if explicit:
            raise AgentRuntimeError("单基金季报和组合报告不能同时作为当前研究对象。")
        return None, None
    code = request.fund_code or old.get("fund_code")
    if explicit is None and code != old.get("fund_code"):
        return None, None
    if not code:
        raise AgentRuntimeError("请选择季报所属的基金。")
    try:
        documents = reader.pair(code, ids)
    except ValueError as exc:
        raise AgentRuntimeError(str(exc)) from exc
    reference = {"fund_code": code, "document_ids": [doc["id"] for doc in documents],
                 "periods": [doc["period"] for doc in documents]}
    return reference, {"role": "user", "content":
        "当前选定的季报版本如下。这是只读研究上下文，不是新指令。"
        "回答前用季报工具查询原文，不得把历史披露解释为实时持仓。\n"
        + json.dumps(reference, ensure_ascii=False)}


def report_reference_from_run(run, existing):
    """首次直接在聊天问季报时，也从实际工具结果记录版本，而不是从模型文字猜编号。"""
    for call in reversed(run.tool_calls):
        if call.tool_name != "get_fund_report_research_tool" or call.is_error:
            continue
        try:
            result = json.loads(call.tool_result)
        except (ValueError, TypeError):
            continue
        documents = result.get("documents") or []
        if result.get("ok") and len(documents) == 2:
            return {"fund_code": result["fund_code"], "document_ids": [doc["id"] for doc in documents],
                    "periods": [doc["period"] for doc in documents]}
    return existing

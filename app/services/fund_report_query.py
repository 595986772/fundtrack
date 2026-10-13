"""季报按需上下文：只把明确的主项数字题分到轻量路径，其余保留完整研究。

这里不判断投资意图，也不调用分类模型。只识别有限的数字问法；存在未识别的
内容就回到完整路径，防止把“增加多少，是否主动加仓”误当成单纯查数字。
筛选仅作用于给模型的消息，不修改数据库、原始工具结果或历史回答。
"""

import json
import re

from app.services.fund_report_source import ALLOCATION_LABELS, allocation_chunks


# 允许词组按最长优先消去；“原因/买卖/经理观点”等不会被消掉，因此无法进入轻量路径。
_NUMERIC_WORDS = (
    "继续沿用", "所选", "选定", "刚才", "刚刚", "之前", "上面", "两期", "这两份", "这两季",
    "季度报告", "季报", "报告", "同一对", "一季度", "二季度", "第一季度", "第二季度", "Q1", "Q2", "Q3", "Q4",
    "请帮我", "帮我", "请问", "请", "比较", "对比", "查看", "告诉我", "继续", "沿用",
    "分别", "各自", "各", "到底", "具体", "目前", "是多少", "多少", "变化", "增加", "减少",
    "上升", "下降", "提高", "降低", "差值", "差多少", "相差", "几个", "个", "变", "又", "这", "中的", "中",
    "了", "的", "和", "与", "及", "是", "为",
    "占基金总资产比例", "占总资产比例", "占基金总资产", "占总资产", "比例", "占比", "金额", "百分点",
    "引用原文", "附原文", "给出原文", "引用", "原文", "页码", "链接",
    "数据截至日", "数据截止日", "披露日", "分别是哪天", "哪天", "日期",
    "详细", "完整", "逐项", "展开", "简要", "简短", "回答", "说明", "一下",
)
_NUMERIC_PATTERN = re.compile("|".join(map(re.escape, sorted(_NUMERIC_WORDS, key=len, reverse=True))))


def supports_evidence_synthesis(question: str) -> bool:
    """只试用历史经理观点的资料对照；因果、预测、适配和交易判断仍保留原模式。

    这是保守的任务边界，不是语义正确性判断。没识别到的问法也不自动降档。
    两种模式使用同一份完整ToolMessage，不删经理章节来换速度。
    """
    return (bool(re.search(r"经理|运作|策略|久期|重点|方向|主题", question))
        and bool(re.search(r"对比|比较|延续|变化|不同|两期|一季度|二季度", question))
        and not re.search(r"为什么|原因|是否|是不是|能否|适合|买|卖|加仓|减仓|预测|未来|下个月|前景|风险", question))


def report_query_profile(question: str, fund_code: str = "") -> dict:
    """缺少明确主项、只说“它呢”、或掺入解释问题时，不凭猜测删减证据。"""
    text = re.sub(r"\s+", "", question)
    labels = [label for label in ALLOCATION_LABELS if label in text]
    remainder = text
    for label in labels:
        remainder = remainder.replace(label, "")
    if re.fullmatch(r"\d{6}", fund_code):
        remainder = re.sub(rf"(?<!\d){fund_code}(?!\d)", "", remainder)
    remainder = _NUMERIC_PATTERN.sub("", remainder)
    remainder = re.sub(r"[，,。；;：:？?！!、（）()\"“”]", "", remainder)
    numeric = bool(labels and not remainder and re.search(r"比例|占比|金额|百分点|多少", text))
    # 明确只问经理文字/同一批原文风险提示时，不携带整套数字表。混合数值、
    # 买卖或当前持仓问题仍走full，不凭一两个主题关键词删减其所需资产证据。
    manager = (bool(re.search(r"经理|运作分析|操作段落|同一批原文.*风险提示", text))
        and not labels and not re.search(r"比例|占比|金额|百分点|规模|收益率|净资产|持仓|仓位|多少|多大|几成|几只|权重|买入|卖出|加仓|减仓|今天|今日|当前|最新|为什么|原因|为何", text))
    return {"mode": "allocation" if numeric else "manager" if manager else "full", "allocation_labels": labels if numeric else [],
            "detailed": bool(re.search(r"详细|完整|逐项|展开", text))}


def select_report_context(message, profile: dict) -> None:
    """原地替换实际ToolMessage的视图，引用核对随后只信这份可见证据。

完整路径不删经理原文；轻量路径保留所问表格行及表头，并明确经理章节未读取。
源工具输出另由AgentToolCall保存，评测仍可对照筛选前的真实结果。
"""
    if getattr(message, "type", None) != "tool":
        return
    try:
        payload = json.loads(message.content)
    except (TypeError, ValueError):
        return
    if not isinstance(payload, dict) or payload.get("ok") is not True or payload.get("data_kind") != "fund_report_research":
        return
    original_chars = sum(len(row.get("text", "")) for row in payload.get("evidence", []))
    # facts已在allocation_changes/table中携带，文档元数据不再重复放一份相同数字。
    payload["documents"] = [{key: value for key, value in doc.items() if key != "facts"}
                            for doc in payload.get("documents", [])]
    if profile["mode"] == "manager":
        # 不删经理段落或普通语义召回；仅省略本题未请求的专门资产表及其重复字段。
        payload["allocation_changes"] = []
        payload["allocation_tables"] = []
        payload["evidence"] = [row for row in payload.get("evidence", []) if row.get("section") != "allocation_table"]
    if profile["mode"] == "allocation":
        labels = profile["allocation_labels"]
        tables = [{**table, "rows": [row for row in table.get("rows", []) if row["label"] in labels]}
                  for table in payload.get("allocation_tables", [])]
        payload["allocation_tables"] = tables
        changes = {row["label"]: row for row in payload.get("allocation_changes", [])}
        # 两期都没有可靠数字的主项也要展示状态，而不是被数字差值列表悄悄省略。
        documents = sorted(payload["documents"], key=lambda doc: doc["period"])
        selected = []
        for label in labels:
            if label in changes:
                selected.append(changes[label])
                continue
            change = {"label": label, "denominator": "基金总资产", "change_percentage_points": None}
            for side, doc in zip(("before", "after"), documents):
                table = next((table for table in tables if table["document_id"] == doc["id"]), {})
                row = next((row for row in table.get("rows", []) if row["label"] == label), {})
                change.update({side: None, side + "_status": row.get("status", "not_found")})
            selected.append(change)
        payload["allocation_changes"] = selected
        payload["evidence"] = []
        for doc in documents:
            table = next((table for table in tables if table["document_id"] == doc["id"]), None)
            if table:
                payload["evidence"].extend(allocation_chunks({**doc, "allocation_table": table}))
        payload["manager_sections"] = [{"document_id": doc["id"], "period": doc["period"],
            "status": "not_requested", "included_chunks": 0, "total_chunks": 0} for doc in documents]
    payload["evidence_ids"] = [row["evidence_id"] for row in payload.get("evidence", [])]
    payload["query_profile"] = {**profile, "source_evidence_chars": original_chars,
        "selected_evidence_chars": sum(len(row.get("text", "")) for row in payload.get("evidence", []))}
    message.content = json.dumps(payload, ensure_ascii=False)

"""Agent 工具证据 ID 的纯解析与最终回答落地检查。

基金研判、市场事件和组合工具都会返回可追踪的 evidence_id / event_id。模型通常会
主动引用它们，但不能把“通常”当成工程保证：若最终答案漏写，后端可以从本轮真实
ToolMessage 中追加一个证据索引，而不是再次请求模型或凭空生成 ID。
"""

from __future__ import annotations

import json
import re
from typing import Any


EVIDENCE_KEYS = {
    "evidence_id",
    "impact_evidence_id",
    "event_id",
    "evidence_ids",
    "event_ids",
}
EVIDENCE_PATTERN = re.compile(
    r"(?:risk|market|portfolio-impact|fact|report):[A-Za-z0-9_:\-.]+|evt-[A-Za-z0-9_-]+"
)


def extract_tool_evidence_ids(tool_calls: list[Any]) -> list[str]:
    """从本轮工具 JSON 中递归收集真实证据 ID。"""

    found: set[str] = set()
    for call in tool_calls:
        raw = str(getattr(call, "tool_result", "") or "")
        try:
            _collect_evidence(json.loads(raw), found)
        except (json.JSONDecodeError, TypeError):
            found.update(EVIDENCE_PATTERN.findall(raw))
    return sorted(found)


def extract_answer_evidence_ids(answer: str) -> list[str]:
    """读取模型最终答案中已经明确写出的证据 ID。"""

    return sorted(set(EVIDENCE_PATTERN.findall(answer or "")))


def extract_tool_evidence_records(
    tool_calls: list[Any],
    *,
    max_items: int = 8,
) -> list[dict[str, Any]]:
    """把工具 JSON 中的证据整理成前端可直接展示的来源卡片。

    优先保留工具本身返回的 evidence_id/event_id。若一个事实工具只有总来源而没有
    单条证据 ID，则生成 `tool:` 前缀的运行引用；它只表示“本轮确实读取了这个来源”，
    不伪装成基金评分算法产生的证据。
    """

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for call in tool_calls:
        tool_name = str(getattr(call, "tool_name", "unknown_tool") or "unknown_tool")
        raw = str(getattr(call, "tool_result", "") or "")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict) or payload.get("ok") is False:
            continue

        before_count = len(records)
        _collect_evidence_records(
            payload,
            records=records,
            seen=seen,
            tool_name=tool_name,
            max_items=max_items,
        )
        if len(records) == before_count and (payload.get("source") or payload.get("source_url")):
            fund_or_scope = str(
                payload.get("fund_code")
                or payload.get("portfolio_id")
                or payload.get("data_kind")
                or "result"
            )
            data_date = str(
                payload.get("snapshot_end")
                or payload.get("period_end")
                or payload.get("data_date")
                or "current"
            )
            evidence_id = f"tool:{tool_name}:{fund_or_scope}:{data_date}"
            if evidence_id not in seen:
                seen.add(evidence_id)
                records.append(
                    {
                        "evidence_id": evidence_id,
                        "title": str(
                            payload.get("short_name")
                            or payload.get("summary")
                            or tool_name
                        )[:180],
                        "source_name": str(
                            payload.get("source") or "本轮工具结果"
                        )[:160],
                        "source_url": payload.get("source_url"),
                        "data_date": None if data_date == "current" else data_date,
                        "direction": None,
                        "tool_name": tool_name,
                    }
                )
        if len(records) >= max_items:
            break
    return records[:max_items]


def ensure_answer_evidence_index(
    *,
    answer: str,
    tool_calls: list[Any],
    max_ids: int = 8,
) -> tuple[str, list[str]]:
    """在答案完全漏引证据时追加真实索引，并返回本次追加的 ID。

    只要模型已经引用任意一个本轮真实 ID，就保留原文不动。这个检查不会修改观点、
    数字或来源，只补机器和用户都能追踪的引用入口。
    """

    available = extract_tool_evidence_ids(tool_calls)
    cited = extract_answer_evidence_ids(answer)
    # 只修复“完全漏写”。若模型已经写了一个虚构 ID，保持原文交给评测判失败，
    # 不能通过追加真 ID 掩盖 hallucination。
    if not available or cited:
        return answer, []

    # 季报优先给用户可点击的“季度 / 页码”，不在手机答案末尾堆一串机器编号。
    # 只认本轮工具确实返回的 PDF 链接；这仍只是来源索引，不证明模型观点正确。
    report_links = {}
    report_ids = []
    if tool_calls and all(getattr(call, "tool_name", "") == "get_fund_report_research_tool" for call in tool_calls):
        for call in tool_calls:
            try:
                payload = json.loads(call.tool_result)
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict) or not payload.get("ok"):
                continue
            for row in payload.get("evidence", []):
                url = row.get("source_url", "")
                if not re.fullmatch(r"https://pdf\.dfcfw\.com/pdf/H2_AN\d{18}_1\.pdf#page=\d+", url):
                    continue
                report_links.setdefault(row["document_id"], {})[url] = f"{row['period']} 第{row['page']}页"
                report_ids.append(row["evidence_id"])
        if report_links:
            if all(any(url in answer for url in links) for links in report_links.values()):
                return answer, []
            links = [f"[{label}]({url})" for sources in report_links.values() for url,label in list(sources.items())[:2]]
            return answer.rstrip() + "\n\n本轮原文来源：" + " · ".join(links), report_ids[:max_ids]

    appended = available[:max_ids]
    evidence_text = "、".join(f"`{evidence_id}`" for evidence_id in appended)
    separator = "\n\n" if answer.strip() else ""
    return f"{answer.rstrip()}{separator}证据索引（来自本轮工具）：{evidence_text}", appended


def _collect_evidence(value: Any, found: set[str], key: str = "") -> None:
    """递归遍历字典和列表，只读取约定的证据字段。"""

    if isinstance(value, dict):
        for child_key, child_value in value.items():
            _collect_evidence(child_value, found, str(child_key))
        return
    if isinstance(value, list):
        for child in value:
            _collect_evidence(child, found, key)
        return
    if key in EVIDENCE_KEYS and value:
        found.add(str(value))


def _collect_evidence_records(
    value: Any,
    *,
    records: list[dict[str, Any]],
    seen: set[str],
    tool_name: str,
    max_items: int,
) -> None:
    """递归寻找含真实 ID 的字典，并保留紧邻它的来源与标题字段。"""

    if len(records) >= max_items:
        return
    if isinstance(value, list):
        for child in value:
            _collect_evidence_records(
                child,
                records=records,
                seen=seen,
                tool_name=tool_name,
                max_items=max_items,
            )
        return
    if not isinstance(value, dict):
        return

    evidence_id = (
        value.get("evidence_id")
        or value.get("impact_evidence_id")
        or value.get("event_id")
    )
    if evidence_id:
        normalized_id = str(evidence_id)
        if normalized_id not in seen:
            seen.add(normalized_id)
            records.append(
                {
                    "evidence_id": normalized_id,
                    "title": str(
                        value.get("title")
                        or value.get("summary")
                        or value.get("value_text")
                        or normalized_id
                    )[:180],
                    "source_name": str(
                        value.get("source_name")
                        or value.get("source")
                        or "本轮工具结果"
                    )[:160],
                    "source_url": value.get("source_url"),
                    "data_date": (
                        str(value.get("data_date") or value.get("event_date"))
                        if value.get("data_date") or value.get("event_date")
                        else None
                    ),
                    "direction": (
                        str(value.get("direction"))
                        if value.get("direction")
                        else None
                    ),
                    "tool_name": tool_name,
                }
            )
    for child in value.values():
        _collect_evidence_records(
            child,
            records=records,
            seen=seen,
            tool_name=tool_name,
            max_items=max_items,
        )

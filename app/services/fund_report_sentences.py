"""给模型实际看见的季报原句编号，不让模型再负责抄写原文。

这是上下文的无损视图，不是新一轮检索：句子拼起来仍是原片段，既不增加资料，
也不重复放一份正文。编号绑定文档、页码、片段内容和位置；压缩删掉片段后，
它的编号自然不再可用。页面边缘可能只是半句话，因此不能将编号当作完整语义。
"""

import hashlib
import json
import re


CONTRACT = "report-sentence-ids-v1"


def sentence_units(row: dict) -> list[dict]:
    """只在中文句末切分，保留分号列举、否定、数字、换行及所有原始字符。

    不按长度再次裁句，不在小数点或“1.”这类条目编号处分割。长句、跨页尾句
    可以超过旧版240字的引文上限；模型只能选整段，不能自行截短限定条件。
    """
    text = row.get("text", "")
    identity = [row.get(key) for key in (
        "evidence_id", "document_id", "fund_code", "period", "page", "source_url", "section")]
    fingerprint = hashlib.sha256(json.dumps([CONTRACT, identity, text], ensure_ascii=False).encode()).hexdigest()
    ends = [match.end() for match in re.finditer(r"[。！？]+[”’」』]?", text)]
    if not ends or ends[-1] != len(text):
        ends.append(len(text))
    units, start = [], 0
    for end in ends:
        if end <= start:
            continue
        # 内容改变时整段的编号失效；同一段反复压缩或调换行顺序则保持编号不变。
        digest = hashlib.sha256(f"{fingerprint}:{start}:{end}".encode()).hexdigest()[:20]
        units.append({"sentence_id": "rs_" + digest, "quote": text[start:end]})
        start = end
    return units


def restore_evidence(row: dict) -> dict | None:
    """供校验器/压缩器恢复原文；编号列表必须与原文重新计算的结果完全一致。

    删除一句、修改一个字却沿用旧编号都不接受。原始工具行还没有编号时保持
    原样，数字题也不需要为了引用协议做额外处理。
    """
    if "sentences" not in row:
        return dict(row) if isinstance(row.get("text"), str) else None
    units = row["sentences"]
    if ("text" in row or not isinstance(units, list) or not units
            or any(not isinstance(unit, dict) or not isinstance(unit.get("quote"), str) for unit in units)):
        return None
    restored = {key: value for key, value in row.items() if key != "sentences"}
    restored["text"] = "".join(unit["quote"] for unit in units)
    return restored if sentence_units(restored) == units else None


def number_report_evidence(payload: dict) -> dict:
    """在最终送入模型前，把text替换为编号原句，不重复正文或动原始工具日志。"""
    if (payload.get("ok") is not True or payload.get("data_kind") != "fund_report_research"
            or (payload.get("query_profile") or {}).get("mode") == "allocation"):
        return payload
    originals = [original for row in payload.get("evidence", [])
        if (original := restore_evidence(row)) and original.get("text")]
    managers = [row for row in originals if row.get("section") == "manager_review"]
    rows, removed = [], 0
    # 仅去掉被本轮同页经理片段完整包含的普通摘录，不按相关性再删证据。
    # 经理正文、原句编号和章节覆盖不变；跨页、跨基金、只部分重叠的摘录都保留。
    for original in sorted(originals, key=lambda row: row.get("section") != "manager_review"):
        if original.get("section") not in ("manager_review", "allocation_table") and any(
                all(original.get(key) == manager.get(key) for key in ("document_id", "fund_code", "period", "page", "source_url"))
                and re.sub(r"\s+", "", original["text"]) in re.sub(r"\s+", "", manager["text"]) for manager in managers):
            removed += 1
            continue
        rows.append({**{key: value for key, value in original.items() if key != "text"},
            "sentences": sentence_units(original)})
    profile = payload.get("query_profile") or {"mode": "full", "detailed": False}
    return {**payload, "evidence": rows, "evidence_ids": [row["evidence_id"] for row in rows],
        "query_profile": {**profile, "deduplicated_excerpts": profile.get("deduplicated_excerpts", 0) + removed,
            "selected_evidence_chars": sum(len(unit["quote"]) for row in rows for unit in row["sentences"])},
        "citation_contract": CONTRACT}


def sentence_neighbors(rows: list[dict]) -> dict[str, dict[str, str]]:
    """建立实际可见原句的前后关系，不检索、不补回压缩删掉的内容。

    同一片段中的顺序是确定的。跨片段只连接经理章节中相邻的全节编号，且必须
    属于同一基金、版本和相邻物理页。旧资料没有全节编号时，只提供片段内语境。
    返回的是单步邻居；调用者不能递归扩展成整份报告。
    """
    neighbors, chunks = {}, {}

    def connect(left, right):
        neighbors[left]["after"] = right
        neighbors[right]["before"] = left

    for row in rows:
        if "sentences" not in row or restore_evidence(row) is None:
            continue
        ids = [unit["sentence_id"] for unit in row["sentences"]]
        for sentence_id in ids:
            neighbors[sentence_id] = {}
        for left, right in zip(ids, ids[1:]):
            connect(left, right)
        index = row.get("section_chunk_index")
        if row.get("section") == "manager_review" and type(index) is int and index >= 0:
            group = tuple(row.get(key) for key in ("document_id", "fund_code", "period", "section"))
            chunks.setdefault(group, {}).setdefault(index, []).append((row["page"], ids))
    for group in chunks.values():
        for index, left in group.items():
            right = group.get(index + 1, [])
            # 编号重复或中间缺段时不猜连接关系；也不把不相邻页拼成完整句子。
            if len(left) == len(right) == 1 and right[0][0] - left[0][0] in (0, 1):
                connect(left[0][1][-1], right[0][1][0])
    return neighbors

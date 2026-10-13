"""季报评分：确定的事情由代码核对，语义判断明确留给人工。

检索召回率检查“找到没有”；引用检查“定位得到没有”。两者都不能证明模型的每个
观点正确，因此不输出虚假的“回答准确率”，也不拿关键词命中冒充理解能力。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import re
from urllib.parse import urlsplit

from app.schemas import AgentEvalCheck

TOOL = "get_fund_report_research_tool"
GRADER_VERSION = "deterministic+manual-v3"


def known_regression_signals(case_id, report):
    """固定考题的已知问题筛查，不是面向所有基金的关键词投资判断。

    只看模型写的主题/解读，不拿引用里碰巧出现一个词当作已回答。检查通过仍需
    人工对照原文；尤其是否同义、因果是否成立、展望时态都不能靠这些规则认证。
    """
    statements = report.statements if report else []
    interpretations = [item.get("topic", "") + "：" + item.get("interpretation", "") for item in statements]
    if case_id == "report-active-theme":
        # 这是固定资料上已发现的展示缺口：写了具体终端场景，却只引用上一句标题。
        # 相邻语境也算已展示原文，但命中词仍不等于语义蕴含或预测成立。
        unsupported = []
        for item, text in zip(statements, interpretations):
            quotes = [quote for ref in item.get("after", [])
                for quote in [ref, *ref.get("context_before", []), *ref.get("context_after", [])]]
            evidence = " ".join(normalize(quote.get("quote", "")) for quote in quotes)
            for wording, original in ((r"手机直连", "手机直连卫星"), (r"车载(?:卫星)?通信", "车载卫星通信")):
                if re.search(wording, text) and original not in evidence:
                    unsupported.append(item.get("topic", "终端应用"))
        return [{"name": "IPO主题覆盖线索", "passed": any(re.search(r"IPO|上市|首次公开发行", text, re.I) for text in interpretations),
            "detail": "检查解读是否显式讨论前期列举的IPO方向；引用里含IPO不算完成主题对比，含词也不证明比较正确。",
            "manual_criteria": ["逐项核对星座招标/发射、火箭、终端需求、IPO，是否遗漏主要方向。",
                "招标与订单的措辞变化不能直接判定新主题；展望中的肯定措辞不等于已兑现。"]},
            {"name": "终端场景引用展示线索", "passed": not unsupported,
                "detail": "固定题中提到手机直连/车载通信时，展示的后期引文需包含具体场景句；只筛查已知缺口。"
                    + ("缺口主题：" + "、".join(unsupported) if unsupported else ""),
                "manual_criteria": ["逐条核对结论的具体细节和条件是否能由展示引文支持，不能只看有链接。",
                    "未来展望保留有望等条件；超出披露的推断独立标注并说明限制，标签也需复核。"]}]
    if case_id == "report-bond-duration":
        # 只拦截此前观察到的明确错误句型，不把所有“利率下行”都禁掉：Q1恰恰有这句。
        confused = any(re.search(r"(?:二季度|Q2)[^。；!?！？]{0,24}利率(?:下行|下降)后[^。；!?！？]{0,18}(?:拉长|增加久期|提高久期)", text)
            for text in interpretations)
        return [{"name": "经济数据与利率混淆线索", "passed": not confused,
            "detail": "仅筛查已知的“Q2利率下行后拉长”错误句型；未命中不代表其他因果表述正确。",
            "manual_criteria": ["Q1利率下行后小幅降低久期；Q2经济数据下行后拉长至超配，不能替换变量。",
                "Q2随后随利率上行增加超配，与前一个经济条件分开说明；不猜当前久期数值。"]}]
    return []


def normalize(text):
    """仅忽略 PDF 换行/空格；不删除数字、否定词或改变句意。"""
    return re.sub(r"\s+", "", str(text or ""))


def tool_payloads(run):
    result = []
    for call in run.tool_calls:
        if call.tool_name != TOOL or call.is_error:
            continue
        try:
            item = json.loads(call.tool_result)
        except (ValueError, TypeError):
            continue
        if isinstance(item, dict) and item.get("ok"):
            result.append(item)
    return result


def fraction(hits, total):
    # 没有适用题目时为 null，而不是凭空得到100分或0分。
    return {"numerator": hits, "denominator": total, "value": round(hits / total, 4) if total else None}


def equal_decimal(actual, expected):
    if expected is None:
        return actual is None
    try:
        return actual is not None and Decimal(str(actual)) == Decimal(str(expected))
    except (InvalidOperation, ValueError):
        return False


def grade_report_case(scenario, run, documents, config):
    """返回自动检查和可复核材料，不改写被测答案、不调用裁判模型。"""
    turns = getattr(run, "eval_turns", [{"run": run, "reference": None}])
    last = turns[-1]["run"]
    payloads = tool_payloads(last)
    payload = payloads[-1] if payloads else {}
    docs_by_period = {doc["period"]: doc for doc in documents}
    expected_ids = {doc["id"] for doc in documents}
    checks, metrics = [], {}

    def check(name, passed, detail, dimension="grounding"):
        checks.append(AgentEvalCheck(name=name, passed=passed, detail=detail,
            dimension=dimension, required=True, weight=1))

    # 引用入口可用不代表生成了答案。所有轮次都要完成协议校验，不能用末轮成功
    # 掩盖首轮limited；真正的无答案题可以正常解释证据不足，不能与执行失败混为一谈。
    delivery = []
    for index, turn in enumerate(turns, 1):
        report = getattr(turn["run"], "report_answer_check", None)
        substantive = bool(report and (report.statements or report.allocations
            or (report.unanswered and scenario.get("category") == "unanswerable")))
        passed = bool(report and report.fund_code == scenario["fund_code"]
            and report.status in {"consistent", "corrected"} and substantive)
        delivery.append({"turn": index, "status": report.status if report else "missing",
            "unanswered": report.unanswered if report else None, "passed": passed})
    metrics["answer_delivery"] = fraction(sum(row["passed"] for row in delivery), len(delivery))
    check("逐轮回答完成", all(row["passed"] for row in delivery),
        "limited/缺少核对记录/仅有链接不算完成；无答案题可明确说明材料不足。" + json.dumps(delivery, ensure_ascii=False), "execution")

    regression_signals = known_regression_signals(scenario["id"], getattr(last, "report_answer_check", None))
    for signal in regression_signals:
        check(signal["name"], signal["passed"], signal["detail"])

    # 每轮都检查实际工具结果，避免第二轮仅凭模型文字宣称沿用旧版本。
    version_ok = True
    for turn in turns:
        items = tool_payloads(turn["run"])
        if not items:
            version_ok = False
        for item in items:
            version_ok &= (item.get("fund_code") == scenario["fund_code"]
                and {doc.get("id") for doc in item.get("documents", [])} == expected_ids)
            version_ok &= all(row.get("document_id") in expected_ids
                and row.get("fund_code") == scenario["fund_code"] for row in item.get("evidence", []))
        reference = turn.get("reference") or {}
        if len(turns) > 1:
            version_ok &= (reference.get("fund_code") == scenario["fund_code"]
                and set(reference.get("document_ids", [])) == expected_ids)
    check("基金与两期版本隔离", bool(version_ok), "逐轮核对真实工具输出和已持久化会话引用。", "conversation")

    # 标注的是原文短句所在物理页，不是算法当前切出来的chunk ID，所以调chunk后仍可比较。
    rows = list(payload.get("evidence", []))
    for doc in payload.get("documents", []):
        rows.extend({"document_id": doc["id"], "page": fact.get("page"), "text": fact.get("quote", "")}
            for fact in doc.get("facts", []))
    gold_rows, found = [], 0
    for gold in scenario.get("gold_evidence", []):
        doc = docs_by_period[gold["period"]]
        hit = any(row.get("document_id") == doc["id"] and row.get("page") == gold["page"]
            and normalize(gold["quote"]) in normalize(row.get("text")) for row in rows)
        found += int(hit)
        gold_rows.append({**gold, "document_id": doc["id"], "retrieved": hit,
            "url": f"{doc['source_url']}#page={gold['page']}"})
    metrics["evidence_recall"] = fraction(found, len(gold_rows))
    if gold_rows:
        check("标注证据召回", found == len(gold_rows), f"找到 {found}/{len(gold_rows)} 段标注原文；不等同回答正确。", "retrieval")

    # 标准答案在JSON中独立录入，不能读取本次算法结果后再给算法自身打满分。
    changes = {item["label"]: item for item in payload.get("allocation_changes", [])}
    numeric_details, numeric_hits = [], 0
    for expected in scenario.get("gold_numeric", []):
        actual = changes.get(expected["label"])
        values = {"before": (actual.get("before") or {}).get("ratio_percent"),
            "after": (actual.get("after") or {}).get("ratio_percent"),
            "delta": actual.get("change_percentage_points")} if actual else {}
        valid = bool(actual) and actual.get("denominator") == "基金总资产"
        valid &= all(equal_decimal(values.get(key), expected.get(key)) for key in ("before", "after", "delta"))
        numeric_hits += int(valid)
        numeric_details.append({"label": expected["label"], "expected": expected, "actual": values, "passed": bool(valid)})
    metrics["disclosure_numeric"] = fraction(numeric_hits, len(numeric_details))
    if numeric_details:
        check("披露数字与百分点核对", numeric_hits == len(numeric_details),
            f"{numeric_hits}/{len(numeric_details)} 项与独立标注一致；横杠保持缺失，不补零。", "numeric")

    # 引用只核对URL、文档版本和页数范围。一个合法链接是否支持旁边的观点必须另审。
    answer = str(last.answer or "")
    citations = sorted(set(re.findall(r"https?://[^\s<>\[\]()\u3000]+", answer)))
    valid_citations, invalid_citations = [], []
    for url in citations:
        url = url.rstrip("。，；、")
        parsed = urlsplit(url)
        doc = next((doc for doc in documents if doc["source_url"] == url.split("#", 1)[0]), None)
        match = re.fullmatch(r"page=(\d+)", parsed.fragment)
        if doc and match and 1 <= int(match[1]) <= len(doc["pages"]):
            valid_citations.append(url)
        else:
            invalid_citations.append(url)
    metrics["citation_locatable"] = fraction(len(valid_citations), len(citations))
    if scenario.get("gold_evidence") or citations:
        check("引用可定位", bool(valid_citations) and not invalid_citations,
            f"有效页码链接 {len(valid_citations)}，无法定位 {len(invalid_citations)}；不自动判定观点被支持。", "citations")

    requested_mode = config["retrieval_mode"]
    actual_modes = [entry.get("mode") for item in payloads for entry in item.get("retrieval", [])]
    # 纯主项数字题刻意不调用 embedding，属于确定性路由而非静默降级。
    # 同时核对当前问题，不能靠工具自称 structured 把普通文字题绕过检索验收。
    from app.services.fund_report_query import report_query_profile
    question = scenario.get("follow_up") if len(turns) > 1 else scenario["message"]
    numeric_only = report_query_profile(question or "", scenario["fund_code"])["mode"] == "allocation"
    mode_ok = bool(actual_modes) and all(mode == requested_mode or (mode == "structured" and numeric_only)
                                        for mode in actual_modes)
    check("检索模式未静默降级", mode_ok, f"请求 {requested_mode}，实际 {actual_modes or '无检索结果'}。", "execution")

    # 保留完整末轮答案和逐轮引用，让人审能发现“合法引用搭配错误结论”，不看截断摘要打分。
    quality = {"metrics": metrics, "numeric_details": numeric_details, "gold_evidence": gold_rows,
        "reference_answer": scenario["reference_answer"], "review_criteria": [*scenario["review_criteria"],
            *[criterion for signal in regression_signals for criterion in signal["manual_criteria"]]],
        "answer_delivery": delivery, "known_regression_signals": regression_signals,
        "answer": answer, "valid_citations": valid_citations, "invalid_citations": invalid_citations,
        "actual_retrieval_modes": actual_modes, "run_config": config,
        "retrieved_evidence": payload.get("evidence", []),
        # 新增完成门槛不重写旧成绩，已通过的数字/召回也不代表观点正确。
        "answer_alignment": (last.report_answer_check.model_dump(mode="json")
            if getattr(last, "report_answer_check", None) else None),
        "manual_review": {"status": "pending", "note": "", "history": []},
        "turns": [report_turn_diagnostics(turn, index + 1) for index, turn in enumerate(turns)],
        "grading_note": "自动分只表示可计算检查。经理观点、否定/推断、引用支持性及无答案场景均需人工复核。"}
    return checks, quality


def report_turn_diagnostics(turn, index):
    """每轮独立保存已核对答案和诊断；末轮成功不再覆盖首轮的失败原因。

    report 不包含未通过核对的草稿，执行诊断只存长度、用量及程序生成的错误。
    这个结构也用于第二轮超时时保存已经完成的第一轮。
    """
    run = turn["run"]
    report = getattr(run, "report_answer_check", None)
    return {"turn": index, "question": run.question, "answer": run.answer,
        "reference": turn.get("reference"), "run_id": run.run_id,
        "answer_status": report.status if report else "missing",
        "answer_alignment": report.model_dump(mode="json") if report else None,
        "usage": run.usage.model_dump(mode="json"), "execution": report.execution if report else {}}


def aggregate_report_metrics(results):
    """按所有题目的分子/分母聚合，避免把无适用项算成零分或满分。"""
    report_results = [item for item in results if item.report_quality]
    if not report_results and not any(item.case_id.startswith("report-") for item in results):
        return {}
    metrics = {}
    for name in ("evidence_recall", "disclosure_numeric", "citation_locatable", "answer_delivery"):
        values = [item.report_quality.get("metrics", {}).get(name, {}) for item in report_results]
        metrics[name] = fraction(sum(v.get("numerator", 0) for v in values), sum(v.get("denominator", 0) for v in values))
    statuses = [item.report_quality.get("manual_review", {}).get("status", "pending") for item in report_results]
    metrics["manual_review"] = {status: statuses.count(status) for status in ("pending", "accepted", "needs_fix")}
    metrics["execution_failures"] = sum(bool(item.error) for item in results)
    metrics["graded_cases"] = len(report_results)
    metrics["total_cases"] = len(results)
    return metrics

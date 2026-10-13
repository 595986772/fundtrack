"""季报观点引用核对：离线验证结构、范围、修正上限、会话与SSE，不消耗模型额度。"""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.schemas import AgentChatRequest
from app.services.fund_report_answer import FundReportAnswerCheck, render_report_answer
from app.services.fund_report_source import allocation_chunks, allocation_facts, extract_allocation_table, manager_chunks, report_dates
from tests.test_fund_report_quality import compact


CODE = "015789"
IDS = [f"fr_{i:032d}" for i in (1, 2)]


def payload():
    """两份小型合成报告，原文条目故意不同编号，用来检查不能把chunk编号当原文编号。"""
    docs, evidence, tables = [], [], []
    for i in (1, 2):
        manager = ("二是可回收火箭试验情况。三是关注需求端是否有所突破。"
                   if i == 1 else "1.可回收火箭技术是关注重点。2.终端应用场景值得关注。")
        cells = "132,232,324.93 0.69" if i == 1 else "- -"
        pages = [{"page": 7, "text": f"4.4 报告期内基金的投资策略和运作分析\n{manager}\n4.5 业绩表现"},
            {"page": 8, "text": f"5.1 报告期末基金资产组合情况\n项目 金额(元) 占基金总资产比例(%)\n3 固定收益投资 {cells}\n5.2 下一节"}]
        table = extract_allocation_table(pages)
        doc = {"id": IDS[i - 1], "fund_code": CODE, "period": f"2026-Q{i}", "title": "合成测试季报",
            "source_url": f"https://example.com/q{i}.pdf", "published_at": f"2026-0{i * 3 + 1}-21", "page_count": 8,
            **report_dates(f"2026-Q{i}"), "pages": pages, "allocation_table": table}
        for row in table["rows"]:
            if row["page"]:
                row.update(evidence_id=f"report:{doc['id']}:p{row['page']}:allocation",
                           source_url=f"{doc['source_url']}#page={row['page']}")
        doc["facts"] = allocation_facts(table)
        for fact in doc["facts"]:
            fact.update(source_url=f"{doc['source_url']}#page={fact['page']}")
        evidence.extend(manager_chunks(doc) + allocation_chunks(doc))
        # 制造需要压缩、但不能用于观点比较的长普通摘录。
        evidence.append({"evidence_id": f"{doc['id']}:omitted", "document_id": doc["id"], "fund_code": CODE,
            "period": doc["period"], "page": 1, "source_url": doc["source_url"] + "#page=1", "text": "不相关的普通段落。" * 600})
        tables.append({"document_id": doc["id"], "period": doc["period"], **table})
        docs.append({key: value for key, value in doc.items() if key not in ("pages", "allocation_table")})
    change = {"label": "固定收益投资", "denominator": "基金总资产", "before": docs[0]["facts"][0],
        "after": None, "before_status": "value", "after_status": "reported_dash", "change_percentage_points": None}
    return {"ok": True, "data_kind": "fund_report_research", "fund_code": CODE, "documents": docs,
        "evidence": evidence, "allocation_changes": [change], "allocation_tables": tables,
        "retrieval": [{"period": doc["period"], "mode": "keyword"} for doc in docs],
        "limitations": ["比例变化不能确认主动或被动原因。"]}


def draft(check=None):
    # 假模型也只能选择实际上下文里的编号，不能预先背一个来自原始日志的ID。
    check = check or checker()
    def reference(doc_id, phrase):
        row = next(row for row in check.sentences.values() if row["document_id"] == doc_id
            and phrase in "".join(row["quote"].split()))
        return {"sentence_id": row["sentence_id"], "source_kind": "outlook"}
    return {"allocation_labels": [], "statements": [{"topic": "可回收火箭", "relation": "continuity",
        "interpretation": "两期都关注可回收火箭这一主题，表述侧重点有所不同。",
        "basis": "disclosure_comparison", "inference_limit": "",
        "before": [reference(IDS[0], "二是可回收火箭试验情况。")],
        "after": [reference(IDS[1], "1.可回收火箭技术是关注重点。")]}], "unanswered": False}


def checker(data=None):
    message = ToolMessage(content=json.dumps(compact(data or payload()), ensure_ascii=False), tool_call_id="report-1")
    return FundReportAnswerCheck.from_message(message, fund_code=CODE, document_ids=IDS, prepare=True)


class ReportAnswerTests(unittest.TestCase):
    def test_valid_quote_derives_scope_links_and_preserves_real_ordinal(self):
        check = checker()
        report = check.finish(json.dumps(draft()), attempts=0)
        self.assertEqual(report.status, "consistent")
        self.assertTrue(all(scope["status"] == "complete" for scope in report.reading_scope))
        text = render_report_answer(report)
        self.assertIn("二是可回收火箭试验情况。", text)
        self.assertNotIn("第三条", text)
        self.assertIn("经理第4.4节", text)
        self.assertIn("不代表已读完全部报告", text)
        self.assertIn("2026-03-31", text)
        self.assertIn("2026-07-21", text)
        self.assertIn("[2026-Q1 第7页](https://example.com/q1.pdf#page=7)", text)
        self.assertEqual(report.statements[0]["before"][0]["document_id"], IDS[0])

    def test_forged_quote_wrong_quarter_missing_and_omitted_ids_are_rejected(self):
        for mode in ("quote", "quarter", "missing", "omitted"):
            with self.subTest(mode=mode):
                value = draft()
                ref = value["statements"][0]["before"][0]
                if mode == "quote":
                    ref["quote"] = "三是可回收火箭试验情况。"
                elif mode == "quarter":
                    ref.update(value["statements"][0]["after"][0])
                else:
                    from app.services.fund_report_sentences import sentence_units
                    omitted = next(row for row in payload()["evidence"] if row["evidence_id"] == f"{IDS[0]}:omitted")
                    ref["sentence_id"] = "rs_" + "0" * 20 if mode == "missing" else sentence_units(omitted)[0]["sentence_id"]
                report = checker().finish(json.dumps(value))
                self.assertEqual(report.status, "limited")
                self.assertFalse(report.statements)

    def test_server_preserves_exact_whitespace_and_model_cannot_write_any_quote(self):
        data = payload()
        data["evidence"][0]["text"] = data["evidence"][0]["text"].replace("火箭试验", "火箭\n试验")
        check = checker(data)
        value = draft(check)
        report = check.finish(json.dumps(value))
        self.assertEqual(report.status, "consistent")
        self.assertEqual(report.statements[0]["before"][0]["quote"], "二是可回收火箭\n试验情况。")
        value["statements"][0]["before"][0]["quote"] = "二是可回收火箭，需求端是否有所突破。"
        self.assertTrue(check.check(json.dumps(value))[1])

    def test_scope_and_source_ordinals_are_not_free_model_fields(self):
        for text in ("Q2全篇未见这一主题。", "已通读整份报告。", "这是第三条观点。", "根据第7页解释。", "详情https://evil.example/x"):
            with self.subTest(text=text):
                value = draft()
                value["statements"][0]["interpretation"] = text
                report = checker().finish(json.dumps(value))
                self.assertEqual(report.status, "limited")
                self.assertNotIn(text, render_report_answer(report))
        value = draft()
        value["statements"][0]["before"][0]["page"] = 99
        self.assertTrue(checker().check(json.dumps(value))[1])
        value = draft()
        value["summary"] = "绕开引用校验的总结。"
        self.assertTrue(checker().check(json.dumps(value))[1])

    def test_expected_versions_and_row_ownership_are_checked(self):
        for update in ({"fund_code": "110020"}, {"document_ids": [IDS[0], "another-version"]}):
            args = {"fund_code": CODE, "document_ids": IDS, **update}
            check = FundReportAnswerCheck(compact(payload()), **args)
            self.assertTrue(check.input_issues)
        for field, wrong in (("fund_code", "110020"), ("period", "2026-Q2"), ("source_url", "https://evil.example"), ("page", 99)):
            data = payload()
            data["evidence"][0][field] = wrong
            self.assertTrue(checker(data).check(json.dumps(draft()))[1])
        data = payload()
        data["documents"][0]["page_count"] = None
        self.assertTrue(checker(data).check(json.dumps(draft()))[1])

    def test_absence_requires_complete_manager_section_and_is_only_model_observation(self):
        value = draft()
        item = value["statements"][0]
        item.update(relation="not_mentioned_after", after=[], interpretation="本期材料不足以确认该方向是否仍受关注。",
                    basis="inference", inference_limit="未提及不能证明已经放弃这一方向。")
        report = checker().finish(json.dumps(value))
        self.assertEqual(report.status, "consistent")
        self.assertIn("不是全文未披露或主题已放弃的证明", render_report_answer(report))
        data = payload()
        for row in data["evidence"]:
            if row["document_id"] == IDS[1] and row.get("section") == "manager_review":
                row["section_complete"] = False
        self.assertTrue(checker(data).check(json.dumps(value))[1])

    def test_manager_comparison_cannot_use_allocation_as_manager_evidence(self):
        value = draft()
        row = next(row for row in checker().sentences.values() if row["document_id"] == IDS[1]
            and row["section"] == "allocation_table")
        value["statements"][0]["after"][0] = {"sentence_id": row["sentence_id"], "source_kind": "reported_fact"}
        self.assertTrue(checker().check(json.dumps(value))[1])
        value = draft()
        value["statements"][0]["before"] = []
        self.assertTrue(checker().check(json.dumps(value))[1])

    def test_unadvertised_ids_duplicate_ids_and_unknown_time_labels_fail(self):
        check = FundReportAnswerCheck(compact(payload()), fund_code=CODE, document_ids=IDS)
        self.assertTrue(check.check(json.dumps(draft()))[1])
        for mode in ("duplicate", "label"):
            value = draft()
            refs = value["statements"][0]["before"]
            if mode == "duplicate":
                refs.append(dict(refs[0]))
            else:
                refs[0]["source_kind"] = "verified_by_python"
            self.assertTrue(checker().check(json.dumps(value))[1])

    def test_new_render_separates_interpretation_and_outlook_and_keeps_old_history(self):
        from app.schemas import FundReportAnswerReport
        report = checker().finish(json.dumps(draft()))
        text = render_report_answer(report)
        self.assertEqual(report.version, "fund-report-answer-v3")
        self.assertIn("披露对照（模型概括）", text)
        self.assertIn("未来展望", text)
        old = report.model_dump()
        old["version"] = "fund-report-answer-v1"
        for item in old["statements"]:
            item.pop("basis")
            item.pop("inference_limit")
            for ref in item["before"] + item["after"]:
                ref.pop("sentence_id")
                ref.pop("source_kind")
                ref.pop("context_before")
                ref.pop("context_after")
        self.assertIn("二是可回收火箭试验情况", render_report_answer(FundReportAnswerReport.model_validate(old)))
        old["version"] = "fund-report-answer-v2"
        self.assertIn("模型解读", render_report_answer(FundReportAnswerReport.model_validate(old)))

    def test_inference_requires_limits_and_is_never_rendered_as_disclosed_conclusion(self):
        value = draft()
        item = value["statements"][0]
        item.update(basis="inference", interpretation="这可能意味着管理人的关注从试验转向应用。")
        self.assertEqual(checker().finish(json.dumps(value)).status, "limited")
        item["inference_limit"] = "仅据措辞对照推测，不能确认实际投资行为。"
        report = checker().finish(json.dumps(value))
        self.assertEqual(report.status, "consistent")
        self.assertIn("模型推断（非季报结论）", render_report_answer(report))
        self.assertIn("推断限制", render_report_answer(report))
        item["basis"] = "disclosure_comparison"
        self.assertTrue(checker().check(json.dumps(value))[1])
        item.update(inference_limit="", relation="uncertain")
        self.assertTrue(checker().check(json.dumps(value))[1])

    def test_next_sentence_contains_detail_not_only_theme_heading(self):
        data = payload()
        row = next(row for row in data["evidence"] if row["document_id"] == IDS[1]
            and row.get("section") == "manager_review")
        row["text"] = "展望下一季度。终端应用场景值得关注。手机直连卫星、车载卫星通信有望进展。尚未兑现。"
        check = checker(data)
        value = draft()
        selected = next(unit for unit in check.sentences.values() if unit["quote"] == "终端应用场景值得关注。")
        item = value["statements"][0]
        item.update(topic="终端应用", interpretation="后期提及手机直连卫星和车载卫星通信的展望。",
            after=[{"sentence_id": selected["sentence_id"], "source_kind": "outlook"}])
        report = check.finish(json.dumps(value))
        ref = report.statements[0]["after"][0]
        self.assertEqual(ref["context_before"][0]["quote"], "展望下一季度。")
        self.assertEqual(ref["context_after"][0]["quote"], "手机直连卫星、车载卫星通信有望进展。")
        self.assertNotIn("source_kind", ref["context_after"][0])
        self.assertIn("手机直连卫星、车载卫星通信有望进展。", render_report_answer(report))
        self.assertIn("相邻语境", render_report_answer(report))
        # 不递归抓取第二个邻居，更不让模型自己提供额外原文。
        self.assertNotIn("尚未兑现", render_report_answer(report))
        item["after"][0]["context_after"] = [{"quote": "伪造补充依据"}]
        self.assertEqual(check.finish(json.dumps(value)).status, "limited")

    def test_neighbor_deduplication_and_final_visible_message_boundary(self):
        check = checker()
        value = draft(check)
        first = value["statements"][0]["before"][0]
        neighbor_id = check.neighbors[first["sentence_id"]]["after"]
        value["statements"][0]["before"].append({"sentence_id": neighbor_id, "source_kind": "outlook"})
        refs = check.finish(json.dumps(value)).statements[0]["before"]
        self.assertFalse(any(ref["context_before"] or ref["context_after"] for ref in refs))
        # 同页连续片段也不能绕过恢复后删减的实际上下文。
        data = payload()
        first_row = data["evidence"][0]
        following = {**first_row, "text": "新一段实际语境。", "section_chunk_index": 1,
            "evidence_id": first_row["evidence_id"].replace("manager0", "manager1")}
        first_row["section_chunk_index"] = 0
        data["evidence"].append(following)
        current = checker(data)
        tail = next(unit for unit in current.sentences.values() if unit["evidence_id"] == first_row["evidence_id"]
            and "需求端" in unit["quote"])
        value = draft(current)
        value["statements"][0]["before"] = [{"sentence_id": tail["sentence_id"], "source_kind": "outlook"}]
        self.assertIn("新一段实际语境", render_report_answer(current.finish(json.dumps(value))))
        data["evidence"].remove(following)
        self.assertNotIn("新一段实际语境", render_report_answer(checker(data).finish(json.dumps(value))))

    def test_frozen_report_terminal_context_keeps_actual_supporting_sentence(self):
        from app.services.fund_report_evals import FundReportEvalService
        from app.services.fund_report_sentences import number_report_evidence
        service = FundReportEvalService()
        if not (service.data_dir / "documents.json").exists():
            self.skipTest("Prepare public PDFs first: python -m scripts.prepare_report_fixtures")
        docs = [doc for doc in service.validate_fixture() if doc["fund_code"] == CODE]
        rows = [row for doc in docs for row in manager_chunks(doc)]
        data = {"ok": True, "data_kind": "fund_report_research", "fund_code": CODE,
            "documents": [{**doc, "page_count": len(doc["pages"])} for doc in docs], "evidence": rows,
            "allocation_changes": [], "allocation_tables": [], "limitations": []}
        check = FundReportAnswerCheck(number_report_evidence(compact(data)))
        # 重现上一轮真实模型选了主题句、却解释了下一句具体场景的情况。
        heading = next(row for row in check.sentences.values() if row["period"] == "2026-Q2"
            and "终端应用场景全面铺开" in "".join(row["quote"].split()))
        before = next(row for row in check.sentences.values() if row["period"] == "2026-Q1"
            and "需求端是否有所突破" in "".join(row["quote"].split()))
        value = {"allocation_labels": [], "unanswered": False, "statements": [{"topic": "终端应用",
            "relation": "emphasis_change", "basis": "disclosure_comparison",
            "interpretation": "后期展望更具体，提到手机直连卫星和车载卫星通信。",
            "before": [{"sentence_id": before["sentence_id"], "source_kind": "outlook"}],
            "after": [{"sentence_id": heading["sentence_id"], "source_kind": "outlook"}]}]}
        report = check.finish(json.dumps(value))
        self.assertEqual(report.status, "consistent")
        detail = report.statements[0]["after"][0]["context_after"][0]
        self.assertIn("手机直连卫星、车载卫星通信", "".join(detail["quote"].split()))
        self.assertIn("有望", detail["quote"])
        self.assertEqual(detail["page"], 7)
        self.assertEqual(detail["document_id"], heading["document_id"])
        self.assertIn("车载卫星通信", render_report_answer(report))

    def test_numeric_selection_keeps_dash_dates_and_original_link_without_guessing(self):
        value = {"allocation_labels": ["固定收益投资"], "statements": [], "unanswered": False}
        report = checker().finish(json.dumps(value))
        text = render_report_answer(report)
        self.assertEqual(report.status, "consistent")
        self.assertIn("0.69%", text)
        self.assertIn("原表为横杠", text)
        self.assertIn("3 固定收益投资 - -", text)
        self.assertIn("https://example.com/q2.pdf#page=8", text)
        self.assertIn("不可计算，不补零", text)
        self.assertNotIn("-0.69", text)
        value["allocation_labels"] = ["不存在的资产"]
        self.assertTrue(checker().check(json.dumps(value))[1])

    def test_user_json_is_not_a_trusted_tool_payload_and_empty_data_is_explicit(self):
        check = FundReportAnswerCheck.from_message(HumanMessage(content=json.dumps(payload())), fund_code=CODE)
        self.assertTrue(check.input_issues)
        report = check.finish(json.dumps(draft()))
        self.assertEqual(report.status, "limited")
        self.assertFalse(report.reading_scope)

    def test_semantic_truth_is_not_claimed_by_reference_validation(self):
        value = draft()
        value["statements"][0]["interpretation"] = "这些线索意味着所有相关企业都将盈利。"
        report = checker().finish(json.dumps(value))
        # 引用存在无法证明推理正确，测试明确保留这个边界，而不是给语义准确率打满分。
        self.assertEqual(report.status, "consistent")
        self.assertIn("未判定模型解释", report.verification_scope)

    def test_manager_quote_candidates_do_not_turn_generic_excerpts_into_manager_evidence(self):
        data = payload()
        manager = next(row for row in data["evidence"] if row.get("section") == "manager_review")
        generic = {**manager, "evidence_id": "ordinary-same-page", "section": "retrieved_excerpt",
            "text": manager["text"] + "这一句来自其他章节。"}
        data["evidence"].append(generic)
        check = checker(data)
        value = draft(check)
        candidate = value["statements"][0]["before"][0]["sentence_id"]
        copied = next(row for row in check.sentences.values() if row["evidence_id"] == "ordinary-same-page"
            and row["quote"] == check.sentences[candidate]["quote"])
        value["statements"][0]["before"][0]["sentence_id"] = copied["sentence_id"]
        text = json.dumps(value)
        self.assertEqual(check.finish(text).status, "limited")
        hints = check.repair_hints(text)
        self.assertEqual(hints[0]["same_page_manager_candidates"], [candidate])
        self.assertEqual(check.finish(text).status, "limited")
        self.assertNotIn("quote", hints[0])
        self.assertIn(candidate, check.instruction())
        # 模型明确重选后才通过；Python不偷偷替换被拒绝的引用。
        value["statements"][0]["before"][0]["sentence_id"] = candidate
        self.assertEqual(check.finish(json.dumps(value)).status, "consistent")

    def test_repair_errors_explain_reference_limits_without_saving_draft(self):
        value = draft()
        value["statements"][0]["after"] *= 3
        value["statements"][0]["interpretation"] = "private-draft"
        errors = checker().check(json.dumps(value))[1]
        self.assertIn("最多2，实际3条引用", errors[0])
        self.assertIn("按主题拆分", errors[0])
        self.assertNotIn("private-draft", str(errors))


class ReportAnswerLoopTests(unittest.IsolatedAsyncioTestCase):
    async def run_loop(self, *, broken=False, repair="valid", max_rounds=3, no_tool=False, changed_on_repair=False,
                       question="比较经理观点", mode="standard", hang_phase=None):
        import app.services.tool_agent as module
        from app.services.agent_routing import AgentRequestRoute
        from app.services.recovery import ModelCallResult
        from app.services.fund_report_query import report_query_profile
        from app.services.fund_report_query import supports_evidence_synthesis
        from app.services.fund_report_runtime import report_synthesis_scope

        profile = report_query_profile(question, CODE)

        phases, events = [], []
        reader = Mock()
        reader.research.return_value = payload()

        class Model:
            def bind_tools(self, tools):
                return self

        async def invoke(**kwargs):
            phases.append(kwargs["phase"])
            if hang_phase == ("decision" if len(phases) == 1 else "synthesis"):
                await asyncio.Event().wait()
            if len(phases) == 1:
                if no_tool:
                    return ModelCallResult(response=AIMessage(content="未经工具证实的错误草稿"))
                return ModelCallResult(response=AIMessage(content="", tool_calls=[{"id": "report-1", "name": "get_fund_report_research_tool",
                    "args": {"fund_code": CODE, "question": "比较经理观点", "document_ids": IDS}, "type": "tool_call"}]))
            self.assertTrue(any("季报最终回答只输出" in str(message.content) for message in kwargs["messages"]))
            if profile["mode"] == "allocation":
                data = json.loads(next(message.content for message in kwargs["messages"] if message.type == "tool"))
                self.assertFalse(any(row.get("section") == "manager_review" for row in data["evidence"]))
                value = {"allocation_labels": profile["allocation_labels"], "statements": [], "unanswered": False}
            else:
                message = next(message for message in kwargs["messages"] if message.type == "tool")
                visible = json.loads(message.content)
                self.assertTrue(all("text" not in row and "sentences" in row for row in visible["evidence"]))
                value = draft(FundReportAnswerCheck.from_message(message))
            if kwargs["phase"] == "fund_report_answer_repair":
                # 修正轮只接收一份协议、一份草稿，不携带上一条失败AI消息的副本。
                self.assertEqual(sum("季报最终回答只输出" in str(item.content)
                    for item in kwargs["messages"]), 1)
                correction = json.loads(kwargs["messages"][-1].content)
                self.assertIn("draft_to_correct", correction)
                self.assertIn("citation_hints", correction)
                self.assertNotIn("instruction", correction)
                self.assertFalse(any(item.type == "ai" and "Q2全篇未见" in str(item.content)
                    for item in kwargs["messages"]))
                self.assertEqual(kwargs["max_attempts"], 1)
                self.assertFalse(kwargs["allow_continuation"])
                if repair == "timeout":
                    raise RuntimeError("模拟模型不可用")
                if repair == "cancel":
                    raise asyncio.CancelledError()
                if repair == "hanging":
                    await asyncio.Event().wait()
                if changed_on_repair:
                    # 模拟模型错误恢复进一步压缩，最终校验必须重新读取实际ToolMessage。
                    message = next(message for message in kwargs["messages"] if message.type == "tool")
                    data = json.loads(message.content)
                    data["evidence"] = [row for row in data["evidence"] if row.get("section") != "manager_review"]
                    message.content = json.dumps(data)
                if repair == "invalid":
                    value["statements"][0]["before"][0]["quote"] = "三是可回收火箭试验情况。"
            elif broken:
                value["statements"][0]["interpretation"] = "Q2全篇未见，原文是第三条。"
            return ModelCallResult(response=AIMessage(content=json.dumps(value, ensure_ascii=False),
                usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}))

        route = AgentRequestRoute(name="fund_reports", domains=("fund_reports",), tool_names=("get_fund_report_research_tool",),
            reason="offline test", include_mcp_tools=False)
        async def progress(event):
            events.append(event)
        with (
            report_synthesis_scope(mode),
            patch.object(module, "get_chat_model", return_value=Model()) as model_factory,
            patch.object(module.agent_request_router, "route_hybrid", new=AsyncMock(return_value=route)),
            patch.object(module.model_recovery_service, "invoke", new=AsyncMock(side_effect=invoke)),
            patch.object(module.agent_runtime_state_service, "collect_response_fields", return_value={}),
            patch("app.services.fund_report_tools.get_fund_report_reader", return_value=reader),
            patch.object(module.context_compactor_service, "_save_original_tool_result", return_value="unused.json"),
        ):
            result = await module.ToolCallingAgentService().chat(AgentChatRequest(user_id="report-check-test", message=question,
                fund_code=CODE, fund_report_ids=IDS, max_tool_rounds=max_rounds), progress_callback=progress)
        # 实验仅换工具成功后的整理模型；原始决策轮、数字题及修正轮保持各自策略。
        if profile["mode"] == "allocation":
            self.assertIs(model_factory.call_args_list[0].kwargs["thinking"], False)
        else:
            self.assertNotIn("thinking", model_factory.call_args_list[0].kwargs)
        selected_light = mode in {"evidence", "balanced"} and profile["mode"] != "allocation" and supports_evidence_synthesis(question) and not no_tool
        if selected_light:
            self.assertIs(model_factory.call_args_list[1].kwargs["thinking"], mode == "balanced")
            if mode == "balanced":
                self.assertEqual(model_factory.call_args_list[1].kwargs["reasoning_effort"], "low")
        if "fund_report_answer_repair" in phases:
            self.assertEqual(model_factory.call_count, 2 + int(selected_light))
            self.assertEqual(model_factory.call_args.kwargs, {"max_retries": 0, "temperature": 0,
                "thinking": False, "timeout_seconds": module._REPORT_REPAIR_TIMEOUT_SECONDS})
        else:
            self.assertEqual(model_factory.call_count, 1 + int(selected_light))
        return result, phases, events

    async def test_valid_draft_uses_existing_summary_round_without_another_model(self):
        result, phases, events = await self.run_loop()
        self.assertEqual(phases, ["agent_round", "agent_round"])
        self.assertEqual(result.report_answer_check.status, "consistent")
        self.assertEqual(result.usage.total_tokens, 30)
        self.assertIsNone(result.answer_check)
        self.assertNotIn('"statements"', result.answer)
        self.assertTrue(any(event.get("phase") == "answer_check" for event in events))

    async def test_conflicts_are_corrected_once_and_usage_is_counted(self):
        result, phases, _ = await self.run_loop(broken=True)
        self.assertEqual(phases.count("fund_report_answer_repair"), 1)
        self.assertEqual(result.report_answer_check.status, "corrected")
        self.assertEqual(result.usage.total_tokens, 60)
        self.assertEqual(len(result.tool_calls), 1)
        self.assertNotIn("全篇未见", result.answer)
        self.assertNotIn("第三条", result.answer)

    async def test_invalid_or_failed_repair_does_not_leak_the_draft(self):
        for repair in ("invalid", "timeout"):
            result, phases, _ = await self.run_loop(broken=True, repair=repair)
            self.assertEqual(result.report_answer_check.status, "limited")
            self.assertFalse(result.report_answer_check.statements)
            self.assertEqual(phases.count("fund_report_answer_repair"), 1)
            self.assertNotIn("三是可回收", result.answer)

    async def test_forced_final_path_has_the_same_guard(self):
        result, phases, _ = await self.run_loop(broken=True, max_rounds=1)
        self.assertIn("forced_final_answer", phases)
        self.assertEqual(result.report_answer_check.status, "corrected")

    async def test_repair_deadline_discards_draft_without_extending_analysis_budget(self):
        with patch("app.services.tool_agent._REPORT_REPAIR_TIMEOUT_SECONDS", 0.01):
            result, phases, _ = await self.run_loop(broken=True, repair="hanging")
        self.assertEqual(result.report_answer_check.status, "limited")
        self.assertFalse(result.report_answer_check.statements)
        self.assertEqual(phases.count("fund_report_answer_repair"), 1)

    async def test_cancel_is_not_swallowed_by_repair(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.run_loop(broken=True, repair="cancel")

    async def test_model_cannot_skip_the_tool_and_present_unsupported_report(self):
        result, phases, _ = await self.run_loop(no_tool=True)
        self.assertEqual(phases, ["agent_round"])
        self.assertEqual(result.report_answer_check.status, "limited")
        self.assertNotIn("错误草稿", result.answer)

    async def test_compaction_during_repair_does_not_revive_removed_references(self):
        result, _, _ = await self.run_loop(broken=True, changed_on_repair=True)
        self.assertEqual(result.report_answer_check.status, "limited")
        self.assertFalse(result.report_answer_check.statements)

    async def test_persistence_and_sse_deliver_only_checked_rendered_answer(self):
        from app.database import Database
        from app.services.comprehensive_agent import ComprehensiveAgentService
        from app.services.conversations import ConversationService
        from app.services.observability import AgentObservabilityService
        from tests.test_conversations import FakeAgent, FakeRuntimeState
        from fastapi.testclient import TestClient
        from app.main import app

        report = checker().finish(json.dumps(draft()))
        class Agent(FakeAgent):
            async def chat(self, request, **kwargs):
                result = await super().chat(request, **kwargs)
                return result.model_copy(update={"report_answer_check": report, "answer": render_report_answer(report)})
        with TemporaryDirectory() as directory:
            database = Database(f"sqlite:///{Path(directory) / 'history.db'}")
            try:
                conversations = ConversationService(database)
                service = ComprehensiveAgentService(conversations=conversations, agent=Agent(),
                    runtime_state=FakeRuntimeState(), observability=AgentObservabilityService(database))
                result = await service.chat(AgentChatRequest(user_id="report-test", message="你好"))
                saved = conversations.detail(conversation_id=result.conversation.id, user_id="report-test")
                self.assertEqual(saved["messages"][-1]["run_metadata"]["report_answer_check"], report.model_dump(mode="json"))
                # SSE 入口已由协调器负责幂等/授权；本测试只验证核对后正文的交付，
                # 替换实际入口，避免旧替身未生效而访问产品授权库或真实模型。
                from app.services.agent_chat_runtime import ChatCoordinationResult
                from app.services.ai_capacity import MemoryAICapacity
                with patch("app.routers.workspace.agent_chat_coordinator.chat",
                           new=AsyncMock(return_value=ChatCoordinationResult(result))), \
                     patch("app.services.rate_limits.get_ai_capacity", return_value=MemoryAICapacity()):
                    with TestClient(app).stream("POST", "/api/v1/agent/chat/stream", json={"user_id": "report-test", "message": "你好"}) as response:
                        self.assertEqual(response.status_code, 200)
                        frames = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]
                self.assertEqual("".join(frame["delta"] for frame in frames if "delta" in frame), result.run.answer)
                final = next(frame for frame in frames if "assistant_message" in frame)
                self.assertEqual(final["assistant_message"]["run_metadata"]["report_answer_check"], report.model_dump(mode="json"))
            finally:
                database.engine.dispose()


if __name__ == "__main__":
    unittest.main()

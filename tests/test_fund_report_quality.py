"""季报回答质量的回归测试：验证进入模型的证据，不用关键词命中冒充答案正确。

这里不调用真实模型。固定 PDF 负责复现上轮缺失的经理段落；合成样本负责检查
长章节、跨页和缺失边界。模型是否正确理解证据仍需在真实评测后对照原文复核。
"""

import json
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from langchain_core.messages import ToolMessage

from app.database import Database
from app.services.context_compactor import context_compactor_service
from app.services.fund_report_eval_grading import normalize
from app.services.fund_report_evals import FundReportEvalService
from app.services.fund_report_source import manager_chunks
from app.services.prompt_builder import prompt_builder_service


def document(pages, number=1):
    return {"id": f"fr_{number:032d}", "fund_code": "015789", "period": f"2026-Q{number}",
        "source_url": f"https://example.com/q{number}.pdf", "pages": pages}


def compact(payload):
    content = json.dumps(payload, ensure_ascii=False)
    return json.loads(context_compactor_service._build_compacted_content(
        original_content=content, saved_path="unused.json", reason="test",
        original_chars=len(content), message=SimpleNamespace()))


class ManagerEvidenceTests(unittest.TestCase):
    def test_heading_variants_keep_cross_page_short_tail_and_sentence_boundaries(self):
        # PDF 常把标题中的汉字拆开；页尾只有几个字也可能决定句意，不能因太短而丢掉。
        body = "保持适度配置，关注需求是否兑现。" * 60
        for heading in ("4.4 报告期内基金投资策略和运作分析",
                        "4．4 报 告 期 内 基 金 的 投 资 策 略 和 运 作 分 析"):
            with self.subTest(heading=heading):
                doc = document([{"page": 7, "text": f"经理履历\n{heading}\n{body}"},
                    {"page": 8, "text": "但不保证收益。\n4.5 报告期内基金的业绩表现\n业绩表不属于这一节"}])
                rows = manager_chunks(doc)
                self.assertEqual("".join(row["text"] for row in rows), body + "但不保证收益。")
                self.assertTrue(all(row["section_complete"] for row in rows))
                self.assertTrue(all(row["text"].endswith("。") for row in rows))
                self.assertEqual({row["page"] for row in rows}, {7, 8})
                self.assertTrue(all(row["source_url"].endswith(f"#page={row['page']}") for row in rows))

    def test_long_section_is_bounded_but_keeps_tail_and_marks_missing_middle(self):
        body = "前期观点。" + "这里是有待验证的阶段性研究判断。" * 330 + "尾部风险提示：不能保证收益。"
        rows = manager_chunks(document([{"page": 7, "text":
            "4.4 报告期内基金的投资策略和运作分析\n" + body + "\n4.5 报告期内基金的业绩表现"}]))
        self.assertLessEqual(len(rows), 6)
        self.assertLessEqual(sum(len(row["text"]) for row in rows), 4200)
        self.assertTrue(rows[0]["text"].startswith("前期观点。"))
        self.assertTrue(rows[-1]["text"].endswith("尾部风险提示：不能保证收益。"))
        self.assertGreater(rows[0]["section_total_chunks"], len(rows))
        self.assertTrue(all(not row["section_complete"] for row in rows))

    def test_missing_boundary_and_missing_heading_do_not_claim_complete(self):
        doc = document([{"page": 7, "text": "4.4 报告期内基金投资策略和运作分析\n关注估值风险。"}])
        rows = manager_chunks(doc)
        self.assertTrue(rows)
        self.assertTrue(all(not row["section_complete"] for row in rows))
        self.assertEqual(manager_chunks(document([{"page": 7, "text": "其他章节：基金经理履历。"}])), [])

    def test_compactor_keeps_all_six_manager_fragments_without_recropping(self):
        # 上轮只保留前四段，会丢掉最后的展望/风险。现在经理正文优先，普通召回让出预算。
        docs, evidence = [], []
        for number in (1, 2):
            doc = document([], number)
            docs.append(doc)
            evidence.extend({"evidence_id": f"{doc['id']}:manager{i}", "document_id": doc["id"],
                "fund_code": doc["fund_code"], "period": doc["period"], "page": 7,
                "source_url": doc["source_url"] + "#page=7", "section": "manager_review",
                "section_complete": True, "section_total_chunks": 6,
                "text": (f"第{i}段结论。" * 100)[:700]} for i in range(6))
            evidence.append({"evidence_id": f"{doc['id']}:other", "document_id": doc["id"],
                "text": "普通召回摘录。" * 150, "page": 3})
        result = compact({"ok": True, "data_kind": "fund_report_research", "documents": docs,
            "evidence": evidence, "allocation_changes": [{"change_percentage_points": "2.19"}]})
        self.assertEqual(len(result["evidence"]), 12)
        self.assertEqual(result["allocation_changes"], [{"change_percentage_points": "2.19"}])
        original = {row["evidence_id"]: row["text"] for row in evidence}
        self.assertTrue(all(row["text"] == original[row["evidence_id"]] for row in result["evidence"]))
        self.assertTrue(all(row["status"] == "complete" for row in result["manager_sections"]))
        for doc in docs:
            self.assertLessEqual(sum(len(row["text"]) for row in result["evidence"]
                if row["document_id"] == doc["id"]), 4200)

    def test_budget_omission_recomputes_section_coverage(self):
        # 兼容旧格式或将来扩大的工具结果，不能照抄检索前的“完整”状态。
        doc = document([])
        rows = [{"evidence_id": f"manager{i}", "document_id": doc["id"], "section": "manager_review",
            "section_complete": True, "section_total_chunks": 7, "text": "甲" * 700} for i in range(7)]
        result = compact({"ok": True, "data_kind": "fund_report_research", "documents": [doc], "evidence": rows})
        self.assertEqual(result["manager_sections"][0], {"document_id": doc["id"], "period": "2026-Q1",
            "status": "partial", "included_chunks": 6, "total_chunks": 7})

    def test_non_manager_excerpt_is_preserved_whole_or_explicitly_omitted(self):
        doc = document([])
        text = "甲" * 710 + "但这不是已完成的交易。"
        row = {"evidence_id": "retrieved", "document_id": doc["id"], "page": 8, "text": text}
        result = compact({"ok": True, "data_kind": "fund_report_research",
            "documents": [doc], "evidence": [row]})
        self.assertEqual(result["evidence"][0]["text"], text)
        self.assertEqual(result["manager_sections"][0]["status"], "not_found")


class FrozenManagerEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 只读取并校验原来六份 PDF，没有改考卷，也不会下载或建立向量索引。
        cls.documents = FundReportEvalService().validate_fixture()

    def test_real_manager_sections_reach_tool_message_after_actual_compaction(self):
        for code in ("110020", "015789", "110007"):
            with self.subTest(fund_code=code):
                docs = [doc for doc in self.documents if doc["fund_code"] == code]
                rows = [row for doc in docs for row in manager_chunks(doc)]
                payload = {"ok": True, "data_kind": "fund_report_research", "documents": docs,
                    "evidence": rows, "allocation_changes": [], "fund_code": code}
                messages = [ToolMessage(content=json.dumps(payload, ensure_ascii=False), tool_call_id="report-test")]
                with patch.object(context_compactor_service, "_save_original_tool_result", return_value="unused.json"):
                    trace = context_compactor_service.compact_messages(messages, round_index=1)
                self.assertTrue(trace)
                model_payload = json.loads(messages[0].content)
                self.assertTrue(all(row["status"] == "complete" for row in model_payload["manager_sections"]))
                self.assertEqual({row["evidence_id"] for row in model_payload["evidence"]},
                    {row["evidence_id"] for row in rows})
                if code == "110007":
                    for period, phrase in (("2026-Q1", "跟随利率下行小幅降低久期"), ("2026-Q2", "拉长至超配基准")):
                        text = "".join(row["text"] for row in model_payload["evidence"] if row["period"] == period)
                        self.assertIn(phrase, normalize(text))
                if code == "015789":
                    q1 = "".join(row["text"] for row in model_payload["evidence"] if row["period"] == "2026-Q1")
                    normalized = "".join(q1.split()).lower()
                    for phrase in ("三是关注需求端是否有所突破", "汽车、机器人等终端领域", "四是国内外重要商业航天公司ipo进度"):
                        self.assertIn(phrase, normalized)

    def test_all_fixed_gold_reaches_model_after_keyword_retrieval_and_compaction(self):
        """验证原先漏掉的横杠行现在也能到达模型，分别检查召回和压缩两步。

        标注仍是原来的34条，不修改标准答案；这里只证明提供了这些证据，
        不会将离线结果冒充20题真实模型的回答准确率。
        """
        service = FundReportEvalService()
        with TemporaryDirectory() as directory:
            database = Database(f"sqlite:///{directory}/context.db")
            try:
                reader = service._prepare_reader(database, self.documents, lambda _: None, "keyword")
                compared = 0
                for case in service._load_suite()["scenarios"]:
                    docs = [doc for doc in self.documents if doc["fund_code"] == case["fund_code"]]
                    raw = reader.research(case["fund_code"], case.get("follow_up") or case["message"],
                        [doc["id"] for doc in docs])
                    model_payload = compact(raw)
                    from langchain_core.messages import ToolMessage
                    from app.services.fund_report_query import report_query_profile, select_report_context
                    from app.services.fund_report_sentences import number_report_evidence, restore_evidence
                    message = ToolMessage(content=json.dumps(raw, ensure_ascii=False), tool_call_id="gold-check")
                    select_report_context(message, report_query_profile(case.get("follow_up") or case["message"], case["fund_code"]))
                    visible = number_report_evidence(compact(json.loads(message.content)))
                    visible["evidence"] = [restore_evidence(row) for row in visible["evidence"]]

                    def contains(payload, gold):
                        rows = list(payload["evidence"])
                        # 数字表原文也会以allocation_changes的字段形式进入模型。
                        for change in payload["allocation_changes"]:
                            for side, doc in zip(("before", "after"), docs):
                                fact = change.get(side)
                                if fact:
                                    rows.append({"period": doc["period"], "page": fact["page"], "text": fact["quote"]})
                        return any(row["period"] == gold["period"] and row["page"] == gold["page"]
                            and normalize(gold["quote"]) in normalize(row["text"]) for row in rows)

                    for gold in case.get("gold_evidence", []):
                        compared += 1
                        with self.subTest(case=case["id"], page=gold["page"], period=gold["period"]):
                            self.assertTrue(contains(raw, gold), "原始召回不能遗漏标注原文。")
                            self.assertTrue(contains(model_payload, gold), "已召回的标注不能在压缩后丢失。")
                            self.assertTrue(contains(visible, gold), "按需视图及编号后仍须保留本题全部标注原文。")
                    for doc in model_payload["documents"]:
                        self.assertEqual(doc["data_as_of"], "2026-03-31" if doc["period"] == "2026-Q1" else "2026-06-30")
                        self.assertNotEqual(doc["data_as_of"], doc["published_at"])
                self.assertEqual(compared, 34, "必须实际比较完整的固定标注，不能跳过缺失项。")
            finally:
                database.engine.dispose()

    def test_prompt_distinguishes_mathematical_change_from_cause_and_historical_statement(self):
        prompt = prompt_builder_service.build(user_id="chen", tool_names=["get_fund_report_research_tool"],
            active_fund_code="015789", domains=("fund_reports",)).system_prompt
        # 仅断言规则被正确装配；不能据此宣称真实模型一定遵守，仍要复测原来两道题。
        for rule in ("manager_sections", "主动或被动", "买卖、申赎、估值", "首次", "历史表述",
                     "先贴因果标签", "金额减少也不能否定加仓", "比较主题语义", "data_as_of", "published_at",
                     "reported_dash", "parse_failed", "not_found", "明确的0.00"):
            self.assertIn(rule, prompt)
        ordinary = prompt_builder_service.build(user_id="chen", tool_names=["get_fund_profile_tool"],
            active_fund_code="015789", domains=("fund_profile",)).system_prompt
        self.assertNotIn("【原因与操作】", ordinary)


if __name__ == "__main__":
    unittest.main()

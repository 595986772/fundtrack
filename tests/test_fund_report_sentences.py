"""句子目录必须无损、可重复生成、与实际压缩范围一致。全部使用本地合成资料。"""

from copy import deepcopy
import unittest

from app.services.fund_report_sentences import number_report_evidence, restore_evidence, sentence_units, sentence_neighbors
from tests.test_fund_report_answer import payload
from tests.test_fund_report_quality import compact


class SentenceCatalogTests(unittest.TestCase):
    def test_view_contains_every_original_character_once_and_stays_stable(self):
        raw = compact(payload())
        original = deepcopy(raw)
        view = number_report_evidence(raw)
        self.assertEqual(raw, original)
        self.assertTrue(all("text" not in row for row in view["evidence"]))
        self.assertEqual({row["evidence_id"]: restore_evidence(row) for row in view["evidence"]},
            {row["evidence_id"]: row for row in raw["evidence"]})
        self.assertEqual(number_report_evidence(view), view)
        again = compact(view)
        self.assertEqual(again["evidence"], view["evidence"])
        reordered = number_report_evidence({**raw, "evidence": list(reversed(raw["evidence"]))})
        self.assertEqual({row["evidence_id"]: row for row in reordered["evidence"]},
            {row["evidence_id"]: row for row in view["evidence"]})
        self.assertEqual(view["evidence"][0]["section"], "manager_review")

    def test_only_fully_covered_same_page_excerpts_are_deduplicated(self):
        raw = compact(payload())
        manager = next(row for row in raw["evidence"] if row.get("section") == "manager_review")
        duplicate = {**manager, "evidence_id": "ordinary-duplicate", "section": "retrieved_excerpt",
            "text": sentence_units(manager)[0]["quote"]}
        other = {**duplicate, "evidence_id": "partial-overlap", "text": duplicate["text"] + "此外还有其他章节内容。"}
        raw["evidence"].extend([duplicate, other])
        view = number_report_evidence(raw)
        self.assertNotIn("ordinary-duplicate", view["evidence_ids"])
        self.assertIn("partial-overlap", view["evidence_ids"])
        self.assertEqual(view["query_profile"]["deduplicated_excerpts"], 1)
        self.assertEqual(number_report_evidence(view), view)
        original_manager = next(row for row in view["evidence"] if row["evidence_id"] == manager["evidence_id"])
        self.assertEqual(original_manager["sentences"], sentence_units(manager))
        for field, value in (("document_id", "other"), ("fund_code", "000001"), ("page", 999),
                             ("period", "2025-Q1"), ("source_url", "https://example.com/other")):
            changed = {**raw, "evidence": [manager, {**duplicate, field: value}]}
            self.assertIn("ordinary-duplicate", number_report_evidence(changed)["evidence_ids"])

    def test_no_cut_at_decimals_ordinals_semicolons_or_long_conditions(self):
        row = {**compact(payload())["evidence"][0], "text":
            "展望下一季度，1.经济数据下行后可能变化；2.利率并未下行，比例0.69%。\n"
            + "如果条件尚未满足，" * 40 + "则不能确认。\n跨页尾句"}
        units = sentence_units(row)
        self.assertEqual("".join(unit["quote"] for unit in units), row["text"])
        self.assertEqual(len(units), 3)
        self.assertIn("0.69%", units[0]["quote"])
        self.assertIn("展望", units[0]["quote"])
        self.assertGreater(len(units[1]["quote"]), 240)
        self.assertTrue(units[1]["quote"].endswith("则不能确认。"))

    def test_changed_content_ownership_or_page_cannot_reuse_ids(self):
        row = number_report_evidence(compact(payload()))["evidence"][0]
        for key, value in (("document_id", "other"), ("period", "2026-Q4"), ("page", 99),
                           ("fund_code", "110007"), ("source_url", "https://evil.example"), ("section", "other")):
            self.assertIsNone(restore_evidence({**row, key: value}))
        tampered = deepcopy(row)
        tampered["sentences"][0]["quote"] += "伪造"
        self.assertIsNone(restore_evidence(tampered))
        tampered = deepcopy(row)
        tampered["sentences"].pop(0)
        self.assertIsNone(restore_evidence(tampered))

    def test_removed_rows_or_malformed_catalog_do_not_reappear_in_recovery(self):
        view = number_report_evidence(compact(payload()))
        removed_id = view["evidence"][0]["evidence_id"]
        view["evidence"].pop(0)
        self.assertNotIn(removed_id, compact(view)["evidence_ids"])
        view["evidence"][0]["sentences"] = "broken"
        restored = compact(view)
        self.assertTrue(all(restore_evidence(row) is not None for row in restored["evidence"]))
        self.assertNotIn(removed_id, restored["evidence_ids"])

    def test_numeric_view_needs_no_catalog(self):
        raw = compact(payload())
        raw["query_profile"] = {"mode": "allocation", "allocation_labels": ["固定收益投资"]}
        self.assertEqual(number_report_evidence(raw), raw)

    def test_neighbors_keep_page_links_and_do_not_jump_gaps_or_versions(self):
        raw = compact(payload())
        base = next(row for row in raw["evidence"] if row.get("section") == "manager_review")
        rows = [{**base, "evidence_id": f"test:{i}", "section_chunk_index": i, "page": 7 + i,
            "source_url": f"https://example.com/q1.pdf#page={7 + i}",
            "text": text} for i, text in enumerate(("本页尾句有条件，", "下一页补全条件。第二句。", "第三页。"))]
        view = number_report_evidence({**raw, "evidence": rows})["evidence"]
        a, b, c = [row["sentences"][0]["sentence_id"] for row in view]
        neighbors = sentence_neighbors(view)
        self.assertEqual(neighbors[a]["after"], b)
        self.assertEqual(neighbors[b]["before"], a)
        self.assertEqual(neighbors, sentence_neighbors(list(reversed(view))))
        self.assertFalse(sentence_neighbors([view[0], view[2]])[a])
        self.assertFalse(sentence_neighbors([view[0], view[2]])[c])
        for changed in ({"section_chunk_index": 3}, {"document_id": IDS_OTHER},
                        {"period": "2026-Q2"}, {"fund_code": "110007"}, {"page": 10}, {"section": "allocation_table"}):
            other = number_report_evidence({**raw, "evidence": [rows[0], {**rows[1], **changed}]})["evidence"]
            self.assertNotIn("after", sentence_neighbors(other)[a])
        # 旧缓存无连续片段编号时，不能仅凭页码猜测相邻关系。
        old = [{key: value for key, value in row.items() if key != "section_chunk_index"} for row in view]
        self.assertNotIn("after", sentence_neighbors(old)[a])
        self.assertTrue(sentence_neighbors(old)[b].get("after"))


IDS_OTHER = "fr_" + "f" * 32

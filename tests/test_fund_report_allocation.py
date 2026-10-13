"""季报时间与资产表状态的离线测试，不请求模型或外部财经服务。"""

from datetime import datetime, timezone
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.database import Database
from app.models import FundReportDocumentModel
from app.services.fund_report_source import extract_allocation, extract_allocation_table, report_dates
from app.services.fund_reports import FundReportService
from tests.test_fund_report_quality import compact


def table_page(body, header="占基金总资产的比例(%)"):
    return {"page": 8, "text": f"5.1 报告期末基金资产组合情况\n序号 项目 金额(元) {header}\n{body}"}


class AllocationStateTests(unittest.TestCase):
    def test_zero_dash_missing_and_broken_cells_are_distinct(self):
        table = extract_allocation_table([table_page(
            "1 权益投资 0.00 0.00\n2 基金投资 — —\n3 固定收益投资 123,bad 0.69\n"
            "5.2 报告期末按行业分类的股票投资组合\n4 贵金属投资 50.00 1.00")])
        rows = {row["label"]: row for row in table["rows"]}
        self.assertEqual(table["status"], "located")
        self.assertEqual(rows["权益投资"]["status"], "value")
        self.assertEqual(rows["权益投资"]["ratio_percent"], "0.00")
        self.assertEqual(rows["基金投资"]["status"], "reported_dash")
        self.assertIn("— —", rows["基金投资"]["quote"])
        self.assertEqual(rows["固定收益投资"]["status"], "parse_failed")
        self.assertEqual(rows["贵金属投资"]["status"], "not_found")
        for label in ("基金投资", "固定收益投资", "贵金属投资"):
            self.assertIsNone(rows[label]["amount_yuan"])
            self.assertIsNone(rows[label]["ratio_percent"])
        self.assertIsNone(rows["贵金属投资"]["page"])
        self.assertIsNone(rows["贵金属投资"]["quote"])

    def test_cross_page_table_and_line_wrapped_cells_keep_physical_pages(self):
        pages = [table_page("1 权益投资\n1,234.00\n0.50\n2 基金投资 - -"),
            {"page": 9, "text": "3 固定收益投资 22.00 1.00\n"
             "7 银行存款和结算备付金合计 0.00 0.00\n5.2 下一节\n8 其他资产 88.00 88.00"}]
        table = extract_allocation_table(pages)
        rows = {row["label"]: row for row in table["rows"]}
        self.assertEqual(rows["权益投资"]["amount_yuan"], "1234.00")
        self.assertEqual(rows["固定收益投资"]["page"], 9)
        self.assertEqual(rows["其他资产"]["status"], "not_found")
        self.assertEqual([fact["ratio_percent"] for fact in extract_allocation(pages)], ["0.50", "1.00", "0.00"])

    def test_invalid_values_never_become_numeric_facts(self):
        for cells in ("1,23.00 1.00", "100.001 1.00", "100.00 1.001", "100.00 101.00",
                      "-1.00 1.00", "100.00 -", "- 0.00", "100.00", "", "NaN 1.00"):
            with self.subTest(cells=cells):
                pages = [table_page(f"1 权益投资 {cells}\n2 基金投资 20.00 2.00\n5.2 下一节")]
                row = extract_allocation_table(pages)["rows"][0]
                self.assertEqual(row["status"], "parse_failed")
                self.assertNotIn("基金投资", row["quote"])
                self.assertEqual([fact["label"] for fact in extract_allocation(pages)], ["基金投资"])

    def test_wrong_header_preserves_original_but_does_not_assign_total_asset_basis(self):
        pages = [table_page("1 权益投资 100.00 1.00", header="占基金资产净值比例(%)")]
        table = extract_allocation_table(pages)
        self.assertEqual(table["status"], "unrecognized_header")
        self.assertIsNone(table["denominator"])
        self.assertEqual(table["rows"][0]["status"], "parse_failed")
        self.assertIn("100.00", table["rows"][0]["quote"])
        self.assertEqual(extract_allocation(pages), [])

    def test_no_table_is_not_an_explicit_dash(self):
        table = extract_allocation_table([{"page": 1, "text": "基金投资策略：关注债券。"}])
        self.assertEqual(table["status"], "not_found")
        self.assertTrue(all(row["status"] == "not_found" for row in table["rows"]))

    def test_oversized_cell_is_marked_incomplete_and_cannot_be_a_numeric_fact(self):
        pages = [table_page("1 权益投资 " + "1" * 300 + ".00 1.00\n5.2下一节\n2 基金投资 10.00 2.00")]
        rows = extract_allocation_table(pages)["rows"]
        self.assertTrue(rows[0]["quote_truncated"])
        self.assertEqual(rows[0]["status"], "parse_failed")
        self.assertLessEqual(len(rows[0]["quote"]), 240)
        self.assertEqual(rows[1]["status"], "not_found")
        self.assertEqual(extract_allocation(pages), [])


class ReportDatesTests(unittest.TestCase):
    def test_quarter_end_is_independent_of_publication(self):
        for period, expected in (("2026-Q1", "2026-03-31"), ("2026-Q2", "2026-06-30"),
                                 ("2026-Q3", "2026-09-30"), ("2025-Q4", "2025-12-31")):
            with self.subTest(period=period):
                dates = report_dates(period)
                self.assertEqual(dates["period_end"], expected)
                self.assertEqual(dates["data_as_of"], expected)
                self.assertEqual(dates["data_as_of_basis"], "report_quarter_end")
        self.assertIsNone(report_dates("unknown")["data_as_of"])


class AllocationContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(f"sqlite:///{self.tmp.name}/reports.db")
        self.addCleanup(self.database.engine.dispose)
        self.database.create_schema()
        self.service = FundReportService(self.database)
        self.ids = [f"fr_{number:032d}" for number in (1, 2)]
        # 模拟升级前已经保存的报告：不加新数据库列，也不重新下载或覆盖旧原文。
        with self.database.session_factory() as session:
            for number, document_id in enumerate(self.ids, 1):
                cells = "132,232,324.93 0.69" if number == 1 else "- -"
                session.add(FundReportDocumentModel(id=document_id, fund_code="015789",
                    period=f"2026-Q{number}", title="历史季报", published_at=f"2026-0{number * 3 + 1}-21",
                    source_url=f"https://example.com/q{number}.pdf", content_hash=str(number) * 64,
                    pages=[table_page(f"3 固定收益投资 {cells}\n5.2 下一节")], facts=[],
                    created_at=datetime.now(timezone.utc)))
            session.commit()

    def test_existing_pages_supply_dates_states_and_originals_without_sync(self):
        with patch.object(self.service.source, "download") as download, patch.object(self.service, "_embedder") as embedder:
            raw = self.service.research("015789", "固定收益投资清仓了吗", self.ids)
            result = compact(raw)
        download.assert_not_called()
        embedder.assert_not_called()
        for payload in (raw, result):
            self.assertEqual([doc["data_as_of"] for doc in payload["documents"]], ["2026-03-31", "2026-06-30"])
            self.assertEqual([doc["published_at"] for doc in payload["documents"]], ["2026-04-21", "2026-07-21"])
            change = next(row for row in payload["allocation_changes"] if row["label"] == "固定收益投资")
            self.assertEqual(change["before"]["ratio_percent"], "0.69")
            self.assertIsNone(change["after"])
            self.assertIsNone(change["change_percentage_points"])
            self.assertEqual(change["before_status"], "value")
            self.assertEqual(change["after_status"], "reported_dash")
            table = payload["allocation_tables"][1]
            row = next(row for row in table["rows"] if row["label"] == "固定收益投资")
            self.assertEqual(row["status"], "reported_dash")
            self.assertEqual(row["quote"], "3 固定收益投资 - -")
            self.assertTrue(row["source_url"].endswith("#page=8"))
            self.assertTrue(any("3 固定收益投资 - -" in row["text"] for row in payload["evidence"]))
        with self.database.session_factory() as session:
            self.assertEqual(session.get(FundReportDocumentModel, self.ids[0]).facts, [])

    def test_original_rows_do_not_compete_with_full_manager_budget(self):
        raw = self.service.research("015789", "固定收益变化", self.ids)
        manager = []
        for doc in raw["documents"]:
            manager.extend({"document_id": doc["id"], "period": doc["period"], "evidence_id": f"{doc['id']}:manager{i}",
                "section": "manager_review", "section_complete": True, "section_total_chunks": 6,
                "text": "甲" * 700} for i in range(6))
        raw["evidence"].extend(manager)
        result = compact(raw)
        self.assertTrue(all(row["status"] == "complete" for row in result["manager_sections"]))
        self.assertTrue(any("3 固定收益投资 - -" in row["text"] for row in result["evidence"]))


if __name__ == "__main__":
    unittest.main()

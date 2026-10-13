"""组合季报链路：真实图、真实校验器与隔离的文档输入，不访问线上数据。"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.services.ai_governance_context import AIControlError, ai_execution_scope
from app.services.portfolio_report_evidence import PortfolioReportEvidenceService
from tests.test_portfolio_research_graph import PORTFOLIO_ID, build_graph_service
from tests.test_portfolio_research_committee import CommitteeModel, findings


def report_pair(code="110020"):
    documents = [{"id": f"doc-{code}-{period}", "fund_code": code, "period": period,
        "content_hash": period * 8, "source_url": "https://example.com/report.pdf",
        "published_at": "2026-07-20", "data_as_of": "2026-06-30"}
        for period in ("2026-Q1", "2026-Q2")]
    return {"fund_code": code, "documents": documents, "evidence": [
        {"evidence_id": f"report:{doc['id']}:p2:c0", "document_id": doc["id"],
         "fund_code": code, "period": doc["period"], "page": 2,
         "section": "manager_review", "text": "本期关注估值风险，配置策略仍取决于盈利改善。",
         "source_url": doc["source_url"] + "#page=2"} for doc in documents]}


def reader_for(payload):
    reader = Mock()
    reader.list_reports.return_value = {"selected_ids": [doc["id"] for doc in payload["documents"]]}
    reader.research.return_value = deepcopy(payload)
    return reader


class PortfolioReportEvidenceTests(TestCase):
    portfolio = {"holdings": [{"fund_code": "110020", "weight_pct": 60},
                              {"fund_code": "110007", "weight_pct": 40}]}

    def test_real_pdf_parser_sql_and_retrieval_keep_exact_page_quotes(self):
        from app.database import Database, PROJECT_ROOT
        from app.models import FundReportDocumentModel
        from app.services.fund_report_source import parse_pdf
        from app.services.fund_reports import FundReportService

        directory = PROJECT_ROOT / "data/evals/fund_reports_v1"
        manifest = json.loads((directory / "documents.json").read_text())["documents"]
        code = manifest[0]["fund_code"]
        documents = [doc for doc in manifest if doc["fund_code"] == code][:2]
        pages_by_id = {}
        with TemporaryDirectory() as temporary:
            database = Database(f"sqlite:///{temporary}/reports.db")
            database.create_schema()
            try:
                for doc in documents:
                    content = (directory / doc["pdf_path"]).read_bytes()
                    self.assertEqual(hashlib.sha256(content).hexdigest(), doc["content_hash"])
                    pages, facts = parse_pdf(content, code, doc["period"])
                    pages_by_id[doc["id"]] = {page["page"]: page["text"] for page in pages}
                    with database.session() as session, session.begin():
                        session.add(FundReportDocumentModel(**{key: doc[key] for key in (
                            "id", "fund_code", "period", "title", "published_at", "source_url", "content_hash")},
                            pages=pages, facts=facts, created_at=datetime.now(timezone.utc)))
                reader = FundReportService(database, directory=temporary, reranker=SimpleNamespace(available=False))
                with patch.object(reader, "_embedder", side_effect=AssertionError("离线验收不调用模型")):
                    result = PortfolioReportEvidenceService(reader).capture({"holdings": [{"fund_code": code}]})
                self.assertEqual(result["status"], "available")
                self.assertEqual({row["document_id"] for row in result["evidence"]}, set(pages_by_id))
                for row in result["evidence"]:
                    original = pages_by_id[row["document_id"]][row["page"]]
                    self.assertIn(re.sub(r"\s+", "", row["quote"]), re.sub(r"\s+", "", original))
            finally:
                database.engine.dispose()

    def test_both_quarters_exact_scope_priority_and_coverage(self):
        reader = reader_for(report_pair())
        result = PortfolioReportEvidenceService(reader, max_funds=1).capture(self.portfolio)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["omitted_fund_codes"], ["110007"])
        self.assertEqual(len(result["evidence"]), 2)
        self.assertEqual(result["funds"][0]["status"], "available")
        self.assertTrue(all(row["page"] == 2 and row["content_hash"] for row in result["evidence"]))
        reader.research.assert_called_once()
        self.assertEqual(reader.research.call_args.args[2], [doc["id"] for doc in result["documents"]])
        with self.assertRaises(ValueError):
            PortfolioReportEvidenceService(reader).capture(self.portfolio, fund_codes=["999999"])

    def test_missing_quarter_and_invalid_second_document_never_look_complete(self):
        payload = report_pair()
        payload["evidence"] = payload["evidence"][:1]
        result = PortfolioReportEvidenceService(reader_for(payload)).capture(self.portfolio, fund_codes=["110020"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["funds"][0]["missing_evidence_document_ids"]), 1)
        payload["documents"][1]["fund_code"] = "999999"
        result = PortfolioReportEvidenceService(reader_for(payload)).capture(self.portfolio, fund_codes=["110020"])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["documents"], [])
        self.assertEqual(result["evidence"], [])

    def test_budget_rejection_propagates_and_missing_reports_never_download(self):
        reader = reader_for(report_pair())
        reader.list_reports.return_value = {"selected_ids": []}
        result = PortfolioReportEvidenceService(reader).capture(self.portfolio)
        reader.research.assert_not_called()
        reader.sync.assert_not_called()
        self.assertEqual(result["status"], "unavailable")
        reader.list_reports.side_effect = AIControlError("budget", "拒绝")
        with self.assertRaises(AIControlError):
            PortfolioReportEvidenceService(reader).capture(self.portfolio)

    def test_graph_all_roles_share_frozen_quotes_and_resume_never_rereads(self):
        graph = build_graph_service()
        reader = reader_for(report_pair())
        graph.report_evidence = PortfolioReportEvidenceService(reader, max_funds=1)
        evidence_id = report_pair()["evidence"][0]["evidence_id"]

        def cite(result):
            result["key_findings"] = [*findings(), {"tone": "neutral", "title": "经理历史观点",
                "detail": "历史披露仍需结合当前证据理解。", "evidence_ids": [evidence_id],
                "metric_keys": [], "fund_codes": ["110020"]}]
            return result

        model = CommitteeModel(edits={"committee_synthesis": cite})
        with patch("app.services.portfolio_research_committee.get_chat_model", side_effect=model.create), \
                ai_execution_scope("chen", "report-test", "test"):
            report = graph.run(run_id="with-reports", user_id="chen", portfolio_id=PORTFOLIO_ID,
                               research_mode="committee")
            self.assertEqual(len(model.calls), 5)
            catalogs = [call["input"]["evidence_catalog"] for call in model.calls]
            self.assertTrue(all(catalog == catalogs[0] for catalog in catalogs))
            reference = report["key_findings"][-1]["evidence_references"][0]
            self.assertEqual(reference["evidence_id"], evidence_id)
            self.assertEqual(reference["quote"], report_pair()["evidence"][0]["text"])
            self.assertEqual(reference["page"], 2)
            reader.research.side_effect = AssertionError("恢复不可读取新版资料")
            restored = graph.run(run_id="with-reports", user_id="chen", portfolio_id=PORTFOLIO_ID,
                                 research_mode="committee")
        self.assertEqual(restored["research_snapshot"], report["research_snapshot"])
        self.assertEqual(len(model.calls), 5)
        self.assertEqual(reader.research.call_count, 1)
        self.assertTrue(report["limitations"])

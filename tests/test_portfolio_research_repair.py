"""冻结前补查的真实控制器测试；来源替身不下载、不扣费，不绕过执行边界。"""

from copy import deepcopy
from datetime import date
from unittest import TestCase
from unittest.mock import Mock, patch

from app.services.ai_governance_context import AIControlError
from app.services.job_runtime import JobCancelledError
from app.services.portfolio_research_graph import PortfolioResearchGraphService
from app.services.portfolio_research_plan import build_research_plan
from app.services.portfolio_research_repair import PortfolioResearchRepairService


class ResearchRepairTests(TestCase):
    def setUp(self):
        self.portfolio = {"holdings": [{"fund_code": "110020", "weight_pct": 70}, {"fund_code": "110007", "weight_pct": 30}]}
        self.reports = {"funds": [{"fund_code": "110020", "status": "missing_reports", "document_ids": []}],
            "evidence": [], "documents": [], "requested_fund_codes": ["110020"], "status": "unavailable"}
        self.market = {"fund_reports": deepcopy(self.reports), "events": []}
        self.reader = Mock()
        self.reader.capture.return_value = {**deepcopy(self.reports), "evidence": []}
        self.events = Mock(return_value={"events": [], "successful_sources": ["测试来源"]})
        self.service = PortfolioResearchRepairService(events=self.events)

    def run_repair(self, *, mode="standard", allowed=False, previous=None, checkpoint=None):
        return self.service.run(portfolio=self.portfolio, market=self.market,
            plan=build_research_plan("比较我的基金策略", mode, allow_public_refresh=allowed),
            reports_service=self.reader, compact_event=PortfolioResearchGraphService._compact_event,
            previous=previous, checkpoint=checkpoint)

    def test_no_permission_no_public_refresh_and_gaps_remain(self):
        result = self.run_repair()
        self.reader.reader.sync.assert_not_called()
        self.events.assert_not_called()
        self.assertEqual(result["research_repair"]["stop_reason"], "manual_refresh_required")
        self.assertEqual(result["research_repair"]["used_rounds"], 0)

    def test_quota_persisted_before_fetch_and_retries_never_reset(self):
        saved = []
        def reserve(value):
            saved.append(deepcopy(value))
        def fetch(code, **kwargs):
            self.assertIn("quarters:110020", saved[-1]["attempted"])
            self.assertFalse(kwargs["build_index"])
        self.reader.reader.sync.side_effect = fetch
        result = self.run_repair(allowed=True, checkpoint=reserve)
        self.assertEqual(result["research_repair"]["used_rounds"], 2)
        self.assertEqual(len(saved), 2)
        result = self.run_repair(allowed=True, previous=saved[-1])
        self.assertEqual(result["research_repair"]["attempts"], [])
        self.assertEqual(self.events.call_count, 1)
        self.assertEqual(self.reader.reader.sync.call_count, 1)

    def test_new_quarters_replace_only_current_unfrozen_capture(self):
        self.reader.capture.return_value = {"funds": [{"fund_code": "110020", "status": "available"}],
            "documents": [{"id": "new-doc", "fund_code": "110020"}],
            "evidence": [{"evidence_id": "new-evidence", "quote": "原文", "fund_codes": ["110020"]}]}
        before = deepcopy(self.market)
        result = self.run_repair(allowed=True, mode="strategy")
        self.assertEqual(result["fund_reports"]["status"], "available")
        self.assertEqual(result["research_repair"]["remaining_gaps"], [])
        self.assertEqual(result["research_repair"]["attempts"][0]["status"], "evidence_changed")
        self.assertEqual(self.market, before)

    def test_local_section_uses_locked_ids_without_network(self):
        self.market["fund_reports"]["funds"][0].update(status="partial", document_ids=["old-a", "old-b"])
        result = self.run_repair(mode="strategy")
        self.reader.reader.sync.assert_not_called()
        self.events.assert_not_called()
        self.assertEqual(self.reader.capture.call_args.kwargs["document_ids_by_fund"], {"110020": ["old-a", "old-b"]})
        self.assertEqual(result["research_repair"]["attempts"][0]["status"], "checked_no_change")

    def test_event_evidence_keeps_actual_date_and_scope(self):
        self.market.pop("fund_reports")
        today = date.today().isoformat()
        self.events.return_value = {"successful_sources": ["白名单来源"], "events": [
            {"event_id": "e1", "event_date": today, "title": "真实事件", "affected_fund_codes": ["110020"]},
            {"event_id": "e2", "event_date": today, "title": "其他基金", "affected_fund_codes": ["999999"]}]}
        result = self.run_repair(allowed=True)
        self.assertEqual([row["event_id"] for row in result["events"]], ["e1"])
        self.assertEqual(result["event_data_cutoff"], today)
        self.assertEqual(result["research_repair"]["stop_reason"], "complete")

    def test_timeout_retains_original_and_control_errors_propagate(self):
        self.reader.reader.sync.side_effect = TimeoutError("内部细节不外显")
        result = self.run_repair(allowed=True, mode="strategy")
        self.assertEqual(result["fund_reports"]["evidence"], [])
        self.assertEqual(result["research_repair"]["attempts"][0]["reason"], "TimeoutError")
        for error in (AIControlError("revoked", "授权撤销"), JobCancelledError("停止")):
            self.reader.reader.sync.side_effect = error
            with self.assertRaises(type(error)):
                self.run_repair(allowed=True)

    def test_eval_never_downloads_even_with_permission(self):
        with patch("app.services.portfolio_research_repair.frozen_eval_data_enabled", return_value=True):
            self.run_repair(allowed=True)
        self.reader.reader.sync.assert_not_called()
        self.events.assert_not_called()

    def test_weaker_local_result_does_not_replace_existing_quarters(self):
        reports = self.market["fund_reports"]
        reports["funds"][0].update(status="partial", document_ids=["old-a", "old-b"])
        reports["evidence"] = [{"evidence_id": doc, "document_id": doc, "quote": "完整原文",
            "fund_codes": ["110020"], "section": "manager_review"} for doc in ("old-a", "old-b")]
        self.reader.capture.return_value = {**deepcopy(reports), "evidence": reports["evidence"][:1]}
        result = self.run_repair(mode="strategy")
        self.assertEqual(result["fund_reports"]["evidence"], reports["evidence"])
        action = result["research_repair"]["attempts"][0]
        self.assertEqual(action["status"], "checked_no_change")
        self.assertEqual(action["reason"], "coverage_regression_prevented")

    def test_checkpoint_rejection_stops_before_external_work(self):
        with self.assertRaises(JobCancelledError):
            self.run_repair(allowed=True, checkpoint=Mock(side_effect=JobCancelledError("执行资格失效")))
        self.reader.reader.sync.assert_not_called()
        self.events.assert_not_called()

    def test_invalid_event_id_cannot_mark_old_events_fresh(self):
        self.market.pop("fund_reports")
        self.market["events"] = [{"event_id": "old", "event_date": "2026-01-01"}]
        self.market["event_data_cutoff"] = "2026-01-01"
        self.events.return_value = {"events": [{"event_date": date.today().isoformat(), "title": "缺编号"}]}
        result = self.run_repair(allowed=True)
        self.assertEqual(result["event_data_cutoff"], "2026-01-01")
        self.assertEqual(result["research_repair"]["attempts"][0]["status"], "checked_no_change")

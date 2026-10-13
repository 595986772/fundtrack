"""Offline tests of committee evidence through the real ToolMessage compactor."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from langchain_core.messages import ToolMessage

from app.services.context_compactor import ContextCompactorService


def finding(role: str, index: int) -> dict:
    value = (4.2, -6.4, 0)[index - 1]
    return {
        "claim_id": f"{role}:initial:{index}",
        "tone": "caution",
        "title": f"Frozen finding {index}",
        "detail": "Historical observations support this view only under the stated conditions. " * 3,
        "fund_codes": [],
        "evidence_ids": [f"event:frozen:{index}"],
        "metric_keys": [f"risk.metric_{index}"],
        "evidence_references": [{
            "evidence_id": f"event:frozen:{index}", "kind": "market_event",
            "direction": "neutral", "title": "Original disclosure",
            "summary": "The disclosure is historical and does not establish a future outcome. " * 2,
            "data_date": "2026-08-28", "fund_codes": [],
            "source_name": "Frozen disclosure publisher",
            "source_url": f"https://source.example/report.pdf?version=old#page={index}",
            "value_text": "Reported observations, not a forecast.",
        }],
        "metric_references": [{
            "key": f"risk.metric_{index}", "label": "Historical risk measure",
            "value": value, "unit": "%", "value_text": f"{value:+.2f}%",
            "scope": "portfolio", "data_date": "2026-08-27",
            "source_name": "Frozen NAV series", "source_url": "https://source.example/nav.csv",
        }],
    }


def research_job() -> dict:
    roles, reviews = [], []
    for role, other in (("opportunity", "risk"), ("risk", "opportunity")):
        roles.append({
            "role": role, "label": role.title(), "status": "accepted",
            "headline": "Evidence has limits", "summary": "Keep the original observation window.",
            "key_findings": [finding(role, index) for index in range(1, 4)],
            "uncertainties": [f"{role}: investment horizon remains unknown."],
            "duration_ms": 123, "model_calls": 1,
        })
        reviews.append({
            "role": role, "label": role.title(), "status": "accepted",
            "responses": [{
                "target_claim_id": f"{other}:initial:{index}",
                "verdict": ("agree", "qualify", "disagree")[index - 1],
                "finding": {key: value for key, value in finding(role, index).items() if key != "claim_id"},
            } for index in range(1, 4)],
            "uncertainties": [f"{role}: the next disclosure may change this assessment."],
            "duration_ms": 321, "model_calls": 1,
        })
    return {
        "ok": True, "data_kind": "portfolio_research_job", "action": "status",
        "portfolio_id": "portfolio_original", "portfolio_name": "Original portfolio", "period_months": 12,
        "job": {"id": "job_original", "job_type": "portfolio_research", "status": "completed",
                "current_step": 13, "total_steps": 13, "progress_percent": 100, "error": None},
        "report": {
            "run_id": "job_original", "portfolio_id": "portfolio_original", "period_months": 12,
            "assessment_date": "2026-08-28", "headline": "Qualified conclusion",
            "summary": "Disagreement remains about the historical evidence.",
            "research_snapshot": {"snapshot_id": "snapshot_original", "content_hash": "frozen-hash",
                                  "captured_at": "2026-08-28T12:00:00+00:00", "immutable": True},
            "generation": {"mode": "multi_agent_committee", "status": "accepted", "model_used": True},
            "key_findings": [finding("synthesis", 1)],
            "committee": {"status": "completed", "snapshot_id": "snapshot_original",
                          "roles": roles, "reviews": reviews, "warnings": [],
                          "synthesis": {"role": "synthesis", "status": "accepted", "duration_ms": 111}},
            "limitations": ["The report uses frozen evidence."], "disclaimer": "Research only.",
        },
    }


class CommitteeContextTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, value in (("PROJECT_ROOT", self.root), ("CONTEXT_STORE_DIR", self.root / "context")):
            patcher = patch(f"app.services.context_compactor.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.compactor = ContextCompactorService()

    def compact(self, payload):
        original = json.dumps(payload, ensure_ascii=False)
        self.assertGreater(len(original), 4000)
        message = ToolMessage(content=original, tool_call_id="status_call", name="get_portfolio_research_status_tool")
        messages = [message]
        traces = self.compactor.compact_messages(messages, round_index=1)
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].compacted_messages, 1)
        self.assertEqual(message.tool_call_id, "status_call")
        self.assertIsInstance(messages[0], ToolMessage)
        compacted = json.loads(messages[0].content)
        self.assertEqual(compacted["compact_strategy"], "portfolio_research_fields_v1")
        self.assertNotIn("preview", compacted)
        self.assertNotIn("tail_preview", compacted)
        saved = json.loads((self.root / compacted["saved_path"]).read_text(encoding="utf-8"))
        self.assertEqual(json.loads(saved["content"]), payload)
        return compacted, messages, traces

    def assert_finding_preserved(self, actual, expected):
        for key in ("claim_id", "tone", "title", "detail", "fund_codes", "evidence_ids", "metric_keys"):
            if key in expected:
                self.assertEqual(actual[key], expected[key])
        self.assertEqual(actual["evidence_references"], expected["evidence_references"])
        for actual_ref, expected_ref in zip(actual["metric_references"], expected["metric_references"], strict=True):
            self.assertEqual(actual_ref, {key: value for key, value in expected_ref.items() if key != "value_text"})

    def test_real_tool_message_keeps_every_claim_target_and_frozen_reference(self):
        payload = research_job()
        before = deepcopy(payload)
        compacted, messages, traces = self.compact(payload)
        report = compacted["report"]
        self.assertEqual(report["research_snapshot"], payload["report"]["research_snapshot"])
        committee = report["committee"]
        expected = payload["report"]["committee"]
        self.assertEqual(committee["snapshot_id"], "snapshot_original")
        for group in ("roles", "reviews"):
            self.assertEqual(len(committee[group]), 2)
            for actual, original in zip(committee[group], expected[group], strict=True):
                self.assertEqual(actual["uncertainties"], original["uncertainties"])
                self.assertEqual(actual["status"], original["status"])
                self.assertNotIn("duration_ms", actual)
                if group == "roles":
                    self.assertEqual(len(actual["key_findings"]), 3)
                    for row, old in zip(actual["key_findings"], original["key_findings"], strict=True):
                        self.assert_finding_preserved(row, old)
                else:
                    self.assertEqual(len(actual["responses"]), 3)
                    for response, old in zip(actual["responses"], original["responses"], strict=True):
                        self.assertEqual(response["target_claim_id"], old["target_claim_id"])
                        self.assertEqual(response["verdict"], old["verdict"])
                        self.assert_finding_preserved(response["finding"], old["finding"])
        self.assert_finding_preserved(report["key_findings"][0], payload["report"]["key_findings"][0])
        self.assertGreater(len(messages[0].content), 4000)
        self.assertLess(traces[0].after_chars, traces[0].before_chars)
        self.assertEqual(payload, before)

    def test_above_soft_limit_is_not_recompacted_even_during_recovery(self):
        compacted, messages, _ = self.compact(research_job())
        original_content = messages[0].content
        traces = self.compactor.compact_messages(messages, round_index=2, force=True)
        self.assertEqual(traces, [])
        self.assertEqual(messages[0].content, original_content)
        self.assertEqual(len(list((self.root / "context").glob("*.json"))), 1)
        self.assertEqual(compacted["report"]["committee"]["roles"][1]["key_findings"][2]["claim_id"], "risk:initial:3")

    def test_all_six_references_remain_attached_to_the_review_finding(self):
        payload = research_job()
        claim = payload["report"]["committee"]["reviews"][1]["responses"][2]["finding"]
        evidence, metric = claim["evidence_references"][0], claim["metric_references"][0]
        claim["evidence_references"] = [{**evidence, "evidence_id": f"event:original:{index}"} for index in range(6)]
        claim["metric_references"] = [{**metric, "key": f"risk.original_{index}"} for index in range(6)]
        claim["evidence_ids"] = [item["evidence_id"] for item in claim["evidence_references"]]
        claim["metric_keys"] = [item["key"] for item in claim["metric_references"]]
        compacted, _, _ = self.compact(payload)
        actual = compacted["report"]["committee"]["reviews"][1]["responses"][2]["finding"]
        self.assert_finding_preserved(actual, claim)

    def test_degraded_report_keeps_failures_uncertainties_and_successful_roles(self):
        payload = research_job()
        report = payload["report"]
        report["generation"] = {"mode": "deterministic_fallback", "status": "fallback", "model_used": False,
                                "fallback_reason": "The committee did not finish."}
        committee = report["committee"]
        committee["status"] = "degraded"
        committee["warnings"] = ["One role failed validation.", "No committee consensus was produced."]
        committee["roles"][1] = {"role": "risk", "label": "Risk", "status": "failed", "key_findings": [],
                                  "uncertainties": [], "failure_reason": "The role failed validation."}
        for review in committee["reviews"]:
            review.update(status="skipped", responses=[], failure_reason="Independent analysis is incomplete.")
        committee["synthesis"].update(status="failed", failure_reason="Required stages are incomplete.")
        compacted, _, _ = self.compact(payload)
        actual = compacted["report"]
        self.assertEqual(actual["generation"], report["generation"])
        self.assertEqual(actual["committee"]["status"], "degraded")
        self.assertEqual(actual["committee"]["warnings"], committee["warnings"])
        self.assertEqual(actual["committee"]["roles"][1], committee["roles"][1])
        self.assertEqual(actual["committee"]["roles"][0]["uncertainties"], committee["roles"][0]["uncertainties"])
        self.assertEqual(actual["committee"]["reviews"][0]["responses"], [])
        self.assertEqual(actual["committee"]["reviews"][0]["failure_reason"], committee["reviews"][0]["failure_reason"])
        self.assertEqual(actual["committee"]["synthesis"]["failure_reason"], committee["synthesis"]["failure_reason"])

    def test_missing_values_units_and_source_links_are_not_invented(self):
        payload = research_job()
        claim = payload["report"]["committee"]["roles"][0]["key_findings"][0]
        metric = claim["metric_references"][0]
        metric.update(value=None, value_text="Unavailable")
        metric.pop("unit")
        metric.pop("source_url")
        evidence = claim["evidence_references"][0]
        evidence.pop("source_url")
        evidence["source_urls"] = ["https://source.example/original.pdf", "javascript:alert(1)"]
        compacted, _, _ = self.compact(payload)
        actual = compacted["report"]["committee"]["roles"][0]["key_findings"][0]
        self.assertEqual(actual["metric_references"][0], metric)
        self.assertNotIn("source_url", actual["evidence_references"][0])
        self.assertEqual(actual["evidence_references"][0]["source_urls"], ["https://source.example/original.pdf"])
        self.assertEqual(actual["evidence_ids"], claim["evidence_ids"])

    def test_redundant_display_keeps_the_unit_when_it_is_not_a_separate_field(self):
        payload = research_job()
        metric = payload["report"]["committee"]["roles"][0]["key_findings"][0]["metric_references"][0]
        metric.pop("unit")
        metric["value_text"] = "+4.20%"
        compacted, _, _ = self.compact(payload)
        actual = compacted["report"]["committee"]["roles"][0]["key_findings"][0]["metric_references"][0]
        self.assertEqual(actual, {key: value for key, value in metric.items() if key != "value_text"} | {"unit": "%"})

    def test_rounded_or_qualified_numeric_displays_are_not_discarded(self):
        for display, unit in (("Rounded annual observation: +4.20%", "%"), ("+4.00%", "%"), ("+4.20%", "points")):
            with self.subTest(display=display, unit=unit):
                payload = research_job()
                metric = payload["report"]["committee"]["roles"][0]["key_findings"][0]["metric_references"][0]
                metric.update(value_text=display, unit=unit)
                compacted, _, _ = self.compact(payload)
                actual = compacted["report"]["committee"]["roles"][0]["key_findings"][0]["metric_references"][0]
                self.assertEqual(actual, metric)

    def test_invalid_source_urls_do_not_hide_the_original_evidence_id(self):
        for url in ("javascript:alert(1)", "file:///tmp/report.pdf", "https://", "//source.example/report",
                    "https://[broken", "https://user:password@source.example/report", "https://bad host/report",
                    "\x00https://source.example/report", "https://source.example:invalid/report"):
            with self.subTest(url=url):
                payload = research_job()
                evidence = payload["report"]["committee"]["roles"][0]["key_findings"][0]["evidence_references"][0]
                evidence["source_url"] = url
                compacted, _, _ = self.compact(payload)
                actual = compacted["report"]["committee"]["roles"][0]["key_findings"][0]["evidence_references"][0]
                self.assertNotIn("source_url", actual)
                for key in ("evidence_id", "summary", "data_date", "source_name"):
                    self.assertEqual(actual[key], evidence[key])

    def test_standard_mode_retains_original_snapshots_without_inventing_a_committee(self):
        payload = research_job()
        report = payload["report"]
        report.pop("committee")
        report["generation"] = {"mode": "structured_model_synthesis", "model_used": True}
        report["quant_risk_snapshot"] = {"annualized_volatility_pct": 8.5, "period_end": "2026-08-28"}
        report["performance_snapshot"] = {"twr_pct": 4.2, "position_mode": "allocation"}
        report["market_snapshot"] = {"data_date": "2026-08-28", "summary": "Original market context."}
        report["decision_history"] = {"decision_count": 0, "has_decisions": False}
        payload["unused_diagnostics"] = "not model evidence " * 300
        compacted, _, _ = self.compact(payload)
        self.assertNotIn("committee", compacted["report"])
        self.assertNotIn("unused_diagnostics", compacted)
        for key in ("quant_risk_snapshot", "performance_snapshot", "market_snapshot", "decision_history", "generation"):
            self.assertEqual(compacted["report"][key], report[key])

    def test_pending_and_failed_jobs_preserve_status_without_fabricating_a_report(self):
        for status in ("queued", "running", "failed", "cancelled"):
            with self.subTest(status=status):
                payload = research_job()
                payload.update(report=None, accepted=False, unused_diagnostics="not evidence " * 400)
                payload["job"].update(status=status, current_step=0, progress_percent=0, error="Original status reason")
                compacted, _, _ = self.compact(payload)
                self.assertIsNone(compacted["report"])
                self.assertFalse(compacted["accepted"])
                self.assertEqual(compacted["job"], payload["job"])

    def test_bounded_report_and_compaction_preserve_quarterly_quote_and_plan(self):
        from app.services.portfolio_agent_tools import _bounded_research_report

        payload = research_job()
        report = payload["report"]
        report["research_plan"] = {"mode": "strategy", "question": "比较两季策略", "allow_public_refresh": False}
        report["market_snapshot"] = {"not_requested": True, "research_repair": {
            "remaining_gaps": [{"kind": "user_information", "detail": "投资期限尚未提供"}]}}
        citation = report["key_findings"][0]["evidence_references"][0]
        citation.update(kind="fund_report", document_id="locked-quarter", page=4, period="2026Q2",
            quote="经理原文与限定条件。", section="manager_review", content_hash="fixed-hash")
        payload["report"] = _bounded_research_report(report)
        result, _, _ = self.compact(payload)
        self.assertEqual(result["report"]["research_plan"], report["research_plan"])
        self.assertEqual(result["report"]["key_findings"][0]["evidence_references"][0], citation)
        self.assertTrue(result["report"]["market_snapshot"]["not_requested"])
        self.assertIsNone(result["report"]["market_snapshot"]["relevant_event_count"])
        self.assertEqual(result["report"]["market_snapshot"]["research_repair"], report["market_snapshot"]["research_repair"])


if __name__ == "__main__":
    unittest.main()

"""诊断只是已有报告的展示映射；不重算、不添证据、不改冻结数据。"""
from copy import deepcopy
import unittest

from app.services.portfolio_diagnosis import build_portfolio_diagnosis, report_with_diagnosis


def report_fixture():
    return {
        "run_id": "report-old", "assessment_date": "2026-09-01", "headline": "历史结论",
        "generation": {"model_used": True},
        "assessment_snapshot": {"fund_contributions": [{"fund_code": "110020", "short_name": "指数基金", "weight_pct": 40}]},
        "quant_risk_snapshot": {
            "period_start": "2025-09-01", "period_end": "2026-09-01",
            "risk_contributions": [{"fund_code": "110020", "weight_pct": 40, "absolute_risk_share_pct": 60}],
        },
        "key_evidence": {"risk": [{"evidence_id": "e1", "source_url": "https://example.com/report"}]},
        "key_findings": [{"title": f"原有发现{i}", "detail": "指数基金110020不是文字匹配线索", "tone": "caution",
            "fund_codes": ["110020"] if i == 0 else [], "evidence_ids": ["e1"],
            "evidence_references": [{"evidence_id": "e1", "title": "波动", "value_text": "0.00%", "data_date": "2026-08-31", "source_name": "净值快照"}],
            "metric_references": [{"key": "metric1", "label": "贡献", "value_text": "-1.20 个百分点", "data_date": "2026-09-01"}]} for i in range(5)],
    }


class PortfolioDiagnosisTests(unittest.TestCase):
    def test_keeps_order_and_all_findings_without_mutating_report(self):
        report = report_fixture()
        before = deepcopy(report)
        result = report_with_diagnosis(report)
        self.assertEqual(report, before)
        self.assertEqual(result["run_id"], "report-old")
        d = result["diagnosis"]
        self.assertEqual(len(d["findings"]), 3)
        self.assertEqual([r["title"] for r in d["findings"] + d["more_findings"]], [f"原有发现{i}" for i in range(5)])

    def test_references_keep_distinct_units_dates_sources_and_exact_ids(self):
        d = build_portfolio_diagnosis(report_fixture())
        refs = d["findings"][0]["references"]
        self.assertEqual([r["id"] for r in refs], ["e1", "metric1"])
        self.assertEqual([r["value"] for r in refs], ["0.00%", "-1.20 个百分点"])
        self.assertEqual([r["date"] for r in refs], ["2026-08-31", "2026-09-01"])
        self.assertEqual(refs[0]["source"], "净值快照")
        self.assertEqual(refs[0]["url"], "https://example.com/report")

    def test_holding_relation_uses_frozen_whitelisted_codes_not_free_text(self):
        d = build_portfolio_diagnosis(report_fixture())
        self.assertEqual(d["findings"][0]["holdings"][0], {"code": "110020", "name": "指数基金", "weight": "40.00%", "risk_share": "60.00%"})
        self.assertEqual(d["findings"][1]["holdings"], [])
        self.assertIn("组合整体", d["findings"][1]["scope"])
        self.assertEqual(d["report_date"], "2026-09-01")

    def test_unknown_and_zero_are_not_interchangeable(self):
        report = report_fixture()
        report["quant_risk_snapshot"]["risk_contributions"][0]["absolute_risk_share_pct"] = None
        report["assessment_snapshot"]["fund_contributions"][0]["weight_pct"] = 0
        report["quant_risk_snapshot"]["risk_contributions"][0]["weight_pct"] = 0
        d = build_portfolio_diagnosis(report)
        self.assertEqual(d["holdings"][0]["weight"], "0.00%")
        self.assertEqual(d["holdings"][0]["risk_share"], "--")
        self.assertIsNone(d["top_risk"])

    def test_missing_references_and_counterarguments_do_not_become_verified(self):
        report = {"key_findings": [{"title": "未引用的发现"}]}
        d = build_portfolio_diagnosis(report)
        self.assertEqual(d["findings"][0]["references"], [])
        self.assertIn("需进一步核验", d["findings"][0]["gap"])
        self.assertEqual(d["conflicts"], [])
        self.assertEqual(d["interpretation_label"], "生成方式未记录")
        self.assertEqual(d["report_date"], "--")

    def test_fallback_remains_explicit_and_does_not_create_model_confidence(self):
        d = build_portfolio_diagnosis({"generation": {"model_used": False, "fallback_reason": "模型超时"}})
        self.assertEqual(d["interpretation_label"], "规则回退 · 非 AI 结论")
        self.assertEqual(d["fallback_reason"], "模型超时")
        self.assertNotIn("confidence", d)

    def test_unsafe_source_urls_are_not_links(self):
        for url in ["javascript:alert(1)", "file:///secret", "https://user:pass@example.com", "https://["]:
            with self.subTest(url=url):
                report = report_fixture()
                report["key_evidence"]["risk"][0]["source_url"] = url
                self.assertEqual(build_portfolio_diagnosis(report)["findings"][0]["references"][0]["url"], "")


if __name__ == "__main__":
    unittest.main()

"""对照报告只比较同条件数据，失败和未知用量都不能被筛掉。"""

from copy import deepcopy
from types import SimpleNamespace
import unittest

from app.schemas import AgentEvalCaseResult, AgentTokenUsage
from app.services.fund_report_eval_comparison import compare_report_runs, CONFIG_KEYS


def run(run_id):
    config = {key: "same" for key in CONFIG_KEYS}
    row = AgentEvalCaseResult(case_id="report-test", name="test", passed=True, score=100,
        duration_ms=100, stop_reason="model_finished", usage=AgentTokenUsage(total_tokens=50),
        report_quality={"run_config": config, "turns": [{"answer_status": "consistent", "execution": {
            "version": "fund-report-runtime-v2", "usage_complete": True, "model_attempts": [{"usage": {"total_tokens": 50}}]}}]})
    return SimpleNamespace(id=run_id, user_id="test", suite_id="fund_report_rag_v1",
        data_fingerprint="fixed-input", results=[row], duration_ms=100, report_metrics={})


class ComparisonTests(unittest.TestCase):
    def test_same_conditions_allow_code_changes_but_not_budget_changes(self):
        before, after = run("before"), run("after")
        before.results[0].report_quality["run_config"]["pipeline_fingerprint"] = "old-code"
        after.results[0].report_quality["run_config"]["pipeline_fingerprint"] = "new-code"
        after.results[0].duration_ms = 60
        result = compare_report_runs(before, after)
        self.assertTrue(result["comparable"])
        self.assertEqual(result["delta"]["mean_case_ms"], -40)
        after.results[0].report_quality["run_config"]["report_timeout_seconds"] = 200
        result = compare_report_runs(before, after)
        self.assertFalse(result["comparable"])
        self.assertIsNone(result["delta"])

    def test_different_cases_data_grader_or_owner_are_not_comparable(self):
        for field, value in (("data_fingerprint", "changed"), ("user_id", "other"), ("suite_id", "other")):
            before, after = run("before"), run("after")
            setattr(after, field, value)
            self.assertFalse(compare_report_runs(before, after)["comparable"])
        before, after = run("before"), run("after")
        after.results.append(deepcopy(after.results[0]))
        self.assertFalse(compare_report_runs(before, after)["comparable"])

    def test_timeout_stays_in_latency_and_legacy_usage_is_not_zero_cost(self):
        before, after = run("before"), run("after")
        failed = deepcopy(before.results[0])
        failed.case_id = "report-failed"
        failed.error = "timeout"
        failed.passed = False
        failed.duration_ms = 90000
        failed.report_quality = {}
        failed.usage = AgentTokenUsage()
        before.results.append(failed)
        after.results.append(deepcopy(failed))
        result = compare_report_runs(before, after)
        self.assertTrue(result["comparable"])
        self.assertEqual(result["before"]["p95_case_ms"], 90000)
        self.assertEqual(result["before"]["mean_case_ms"], 45050)
        self.assertEqual(result["before"]["execution_failures"], 1)
        self.assertEqual(result["before"]["known_tokens"], 50)
        self.assertFalse(result["before"]["usage_complete"])
        self.assertEqual(result["before"]["cases_with_legacy_diagnostics"], 1)
        self.assertTrue(any("完整费用" in text for text in result["warnings"]))

    def test_unknown_or_mixed_configs_cannot_claim_improvement(self):
        before, after = run("before"), run("after")
        before.results[0].report_quality = {}
        self.assertFalse(compare_report_runs(before, after)["comparable"])
        before = run("before")
        before.results.append(deepcopy(before.results[0]))
        before.results[-1].case_id = "report-next"
        before.results[-1].report_quality["run_config"]["model"] = "other-model"
        after.results.append(deepcopy(before.results[-1]))
        self.assertFalse(compare_report_runs(before, after)["comparable"])

    def test_changing_code_mid_run_is_not_a_single_candidate(self):
        before, after = run("before"), run("after")
        for value in (before, after):
            value.results.append(deepcopy(value.results[0]))
            value.results[-1].case_id = "report-next"
            for row in value.results:
                row.report_quality["run_config"]["pipeline_fingerprint"] = value.id
        self.assertTrue(compare_report_runs(before, after)["comparable"])
        after.results[-1].report_quality["run_config"]["pipeline_fingerprint"] = "changed-mid-run"
        self.assertFalse(compare_report_runs(before, after)["comparable"])

    def test_governance_changes_cannot_claim_same_budget_as_legacy_runs(self):
        before, after = run("before"), run("after")
        for value in (before, after):
            del value.results[0].report_quality["run_config"]["ai_governance"]
        # 旧测评仍可互相比较，但不能假装新加的 Token 上限早已存在。
        self.assertTrue(compare_report_runs(before, after)["comparable"])
        after.results[0].report_quality["run_config"]["ai_governance"] = {
            "enabled": True, "max_output_tokens": 16384}
        result = compare_report_runs(before, after)
        self.assertFalse(result["comparable"])
        self.assertTrue(any("ai_governance" in reason for reason in result["reasons"]))

    def test_changed_synthesis_policy_is_explicit_not_silent_cost_saving(self):
        before, after = run("before"), run("after")
        execution = after.results[0].report_quality["turns"][0]["execution"]
        execution.update(synthesis_policy="manager-timeout-fallback-v1", usage_complete=False)
        execution["model_attempts"] = [{"strategy": "primary", "usage": None},
            {"strategy": "timeout_non_thinking", "usage": {"total_tokens": 50}}]
        result = compare_report_runs(before, after)
        self.assertTrue(result["comparable"])
        self.assertEqual(result["after"]["timeout_fallback_attempts"], 1)
        self.assertEqual(result["after"]["attempts_missing_usage"], 1)
        self.assertFalse(result["after"]["usage_complete"])
        self.assertTrue(any("执行策略优化" in text for text in result["warnings"]))


if __name__ == "__main__":
    unittest.main()

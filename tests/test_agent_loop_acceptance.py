"""回归产品入口和验收判分器；真实模型输出质量应另跑已有付费评测。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from app.database import Database
from app.schemas import AgentEvalCaseResult, AgentEvalCheck, AgentToolCall, AgentTokenUsage
from app.services.agent_evals import AgentEvalService
from app.services.observability import AgentObservabilityService
from tests.agent_loop_acceptance import finish_grade, run_acceptance_suite
from app.routers import workspace
from app.services.agent_loop_guard import LoopGuard


class AcceptanceScoringTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Database(f"sqlite:///{Path(directory.name) / 'eval.db'}")
        self.addCleanup(database.engine.dispose)
        self.grader = AgentEvalService(observability=AgentObservabilityService(database))

    def grade(self, *, code="data_unavailable", tool="get_fund_risk_tool", contract=None, answer="净值不足，暂时无法比较。"):
        call = AgentToolCall(tool_name=tool, tool_args={}, is_error=True,
            tool_result=json.dumps({"ok": False, "error_code": code}),
            recovery_trace=[{"action": "attempt", "attempt": 1, "error_code": code}])
        run = SimpleNamespace(tool_calls=[call], answer=answer, stop_reason="insufficient_evidence",
            run_id="test", usage=AgentTokenUsage())
        scenario = {"id": "gap", "name": "缺证据"}
        if contract is not None:
            scenario["expected_tool_errors"] = contract
        return self.grader.grade_case(scenario=scenario, run=run, duration_ms=1)

    @staticmethod
    def contract():
        return [{"tool": "get_fund_risk_tool", "error_code": "data_unavailable", "count": 1}]

    def test_legacy_suite_still_rejects_tool_errors(self):
        result = self.grade()
        self.assertFalse(result.passed)
        self.assertIsNone(result.outcome)

    def test_exact_declared_error_is_allowed(self):
        self.assertTrue(self.grade(contract=self.contract()).passed)

    def test_wrong_code_or_wrong_tool_is_not_a_handled_gap(self):
        self.assertFalse(self.grade(contract=self.contract(), code="permission_denied").passed)
        self.assertFalse(self.grade(contract=self.contract(), tool="write_note_tool").passed)

    def test_error_count_must_match_and_invalid_contract_is_rejected(self):
        contract = self.contract()
        contract[0]["count"] = 2
        self.assertFalse(self.grade(contract=contract).passed)
        contract[0]["count"] = 0
        with self.assertRaises(ValueError):
            self.grade(contract=contract)

    def test_empty_contract_is_not_ignore_errors(self):
        self.assertFalse(self.grade(contract=[]).passed)

    def test_attempts_distinguish_retry_cache_and_unknown_trace(self):
        retry = AgentToolCall(tool_name="read", tool_args={}, tool_result="{}", recovery_trace=[
            {"action": "attempt"}, {"action": "retry_wait"}, {"action": "attempt"}])
        cache = retry.model_copy(update={"execution_kind": "cached", "recovery_trace": []})
        unknown = retry.model_copy(update={"recovery_trace": []})
        self.assertEqual(self.grader._physical_tool_attempts([retry, cache]), 2)
        self.assertIsNone(self.grader._physical_tool_attempts([retry, unknown]))

    def test_failed_sse_or_persistence_check_cannot_pass_as_handled_limitation(self):
        result = AgentEvalCaseResult(case_id="gap", name="gap", passed=True, score=100,
            duration_ms=1, stop_reason="insufficient_evidence", checks=[AgentEvalCheck(
                name="没有正常结束", passed=False, detail="SSE 缺少 done")])
        finish_grade(result, "handled_limitation")
        self.assertFalse(result.passed)
        self.assertEqual(result.outcome, "system_error")

    def test_old_json_keeps_new_metrics_unknown(self):
        result = AgentEvalCaseResult(case_id="legacy", name="legacy", passed=True,
            score=100, duration_ms=1, stop_reason="model_finished")
        self.assertIsNone(result.outcome)
        self.assertIsNone(result.physical_tool_attempts)


class EntryAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_chat_entry_scenarios(self):
        report = await run_acceptance_suite()
        self.assertEqual(len(report.results), 13)
        for result in report.results:
            with self.subTest(case=result.case_id):
                failed = [(check.name, check.detail) for check in result.checks if not check.passed]
                self.assertTrue(result.passed, f"{result.error or ''} {failed}")
                self.assertEqual(result.execution_diagnostics["usage_origin"], "synthetic_provider")

    async def test_missing_done_is_detected_even_with_a_correct_answer(self):
        frame = workspace._sse_frame
        with patch.object(workspace, "_sse_frame", side_effect=lambda name, payload:
            ": dropped-by-test\n\n" if name == "done" else frame(name, payload)):
            report = await run_acceptance_suite(case_ids=["complete"])
        result = report.results[0]
        self.assertFalse(result.passed)
        self.assertEqual(result.outcome, "system_error")
        self.assertTrue(any(check.name == "SSE 正常打开与结束" and not check.passed for check in result.checks))

    async def test_cache_regression_is_detected_by_independent_source_counter(self):
        # 故意绕开缓存。答案和工具数量都可能仍正确，只有实际来源计数能发现退化。
        async def uncached(self, name, args, selected_tool, context, invoke):
            return await invoke(), False
        with patch.object(LoopGuard, "read", new=uncached):
            report = await run_acceptance_suite(case_ids=["cache"])
        result = report.results[0]
        self.assertFalse(result.passed)
        self.assertEqual(result.physical_tool_attempts, 3)
        self.assertTrue(any(check.name == "来源执行次数符合契约" and not check.passed for check in result.checks))


if __name__ == "__main__":
    unittest.main()

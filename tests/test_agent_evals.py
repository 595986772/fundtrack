"""Agent Evals & Observability 的确定性测试。

这里使用 Fake Agent 和临时 SQLite，不会调用 DeepSeek，也不会污染正式运行记录。
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.database import Database
from app.schemas import (
    AgentChatRequest,
    AgentChatResponse,
    AgentPromptTrace,
    AgentTokenUsage,
    AgentToolCall,
)
from app.services.agent_evals import AgentEvalService
from app.services.agent_routing import AgentRequestRouter
from app.services.eval_context import use_frozen_eval_data
from app.services.funds import FundCatalogService
from app.services.observability import AgentObservabilityService


def make_run(
    *,
    answer: str,
    is_error: bool = False,
    tool_result: dict | None = None,
) -> AgentChatResponse:
    """构造一个包含真实格式证据 ID 的最小 Agent 响应。"""

    return AgentChatResponse(
        run_id="run-eval-1",
        user_id="eval-user",
        question="请综合分析基金",
        answer=answer,
        used_model="fake-model",
        max_tool_rounds=4,
        stop_reason="model_finished",
        tools_available=["get_fund_assessment_tool"],
        tool_calls=[
            AgentToolCall(
                tool_name="get_fund_assessment_tool",
                tool_args={"fund_code": "110020"},
                tool_result=json.dumps(
                    tool_result
                    or {
                        "evidence": [
                            {"evidence_id": "risk:max_drawdown:110020:12m"}
                        ]
                    }
                ),
                is_error=is_error,
            )
        ],
        prompt_trace=AgentPromptTrace(section_count=1, total_chars=10),
        rounds=[],
        pipeline_steps=["Receive user question"],
        usage=AgentTokenUsage(input_tokens=100, output_tokens=50, total_tokens=150),
    )


SCENARIO = {
    "id": "assessment",
    "name": "综合研判",
    "required_tools": ["get_fund_assessment_tool"],
    "forbidden_tools": [],
    "max_tool_calls": 1,
    "required_answer_groups": [
        {"name": "说明风险", "terms": ["风险", "回撤"]},
        {"name": "说明边界", "terms": ["不确定", "局限"]},
    ],
    "require_evidence_reference": True,
    "reject_absolute_claims": True,
}


class AgentEvalGraderTests(unittest.TestCase):
    def test_conditional_opinion_uses_assessment_without_semantic_review(self) -> None:
        """普通操作观点由主模型读取综合评估，不额外嵌套语义模型。"""

        suite_path = Path(__file__).resolve().parents[1] / "data" / "evals" / "fund_agent_v1.json"
        suite = json.loads(suite_path.read_text(encoding="utf-8"))
        scenario = next(
            item
            for item in suite["scenarios"]
            if item["id"] == "fund-conditional-opinion"
        )

        self.assertEqual(
            scenario["required_tools"],
            ["get_fund_assessment_tool"],
        )
        self.assertIn(
            "get_fund_semantic_review_tool",
            scenario["forbidden_tools"],
        )
        self.assertEqual(scenario["max_tool_calls"], 1)

    def test_valid_tool_grounded_answer_passes(self) -> None:
        service = AgentEvalService()
        run = make_run(
            answer=(
                "当前主要是回撤风险，依据 risk:max_drawdown:110020:12m；"
                "市场仍有不确定性。"
            )
        )

        result = service.grade_case(scenario=SCENARIO, run=run, duration_ms=120.5)

        self.assertTrue(result.passed)
        self.assertEqual(result.score, 100.0)

    def test_invented_evidence_and_absolute_claim_fail(self) -> None:
        service = AgentEvalService()
        run = make_run(
            answer=(
                "风险很低，依据 risk:invented:110020:12m，未来必然上涨并且保证收益。"
            )
        )

        result = service.grade_case(scenario=SCENARIO, run=run, duration_ms=80)

        self.assertFalse(result.passed)
        failed_names = {check.name for check in result.checks if not check.passed}
        self.assertIn("引用真实工具证据", failed_names)
        self.assertIn("无绝对化收益承诺", failed_names)

    def test_tool_result_assertion_catches_wrong_business_fact(self) -> None:
        """即使回答关键词齐全，工具原始数字不对也不能通过 grounding。"""

        scenario = {
            **SCENARIO,
            "tool_result_assertions": [
                {
                    "name": "最大回撤固定值",
                    "tool": "get_fund_assessment_tool",
                    "path": "max_drawdown_pct",
                    "operator": "equals",
                    "expected": 9.26,
                }
            ],
        }
        run = make_run(
            answer=(
                "当前存在回撤风险，依据 risk:max_drawdown:110020:12m；"
                "结果仍有不确定性。"
            ),
            tool_result={
                "max_drawdown_pct": 99.0,
                "evidence": [
                    {"evidence_id": "risk:max_drawdown:110020:12m"}
                ],
            },
        )

        result = AgentEvalService().grade_case(
            scenario=scenario,
            run=run,
            duration_ms=20,
        )

        self.assertFalse(result.passed)
        self.assertLess(result.metric_scores["grounding"], 100.0)

    def test_negated_claim_and_thousands_separator_are_not_false_failures(self) -> None:
        """“不是稳赚不赔”是风险警示，3,966 与 3966 也是同一个事实。"""

        scenario = {
            **SCENARIO,
            "required_answer_groups": [
                {
                    "name": "市场宽度数字",
                    "terms": ["3966", "1177", "76.15"],
                    "match": "all",
                }
            ],
        }
        run = make_run(
            answer=(
                "上涨 3,966 家、下跌 1,177 家，上涨占比 76.15%。"
                "这并非稳赚不赔，依据 risk:max_drawdown:110020:12m。"
            )
        )

        result = AgentEvalService().grade_case(
            scenario=scenario,
            run=run,
            duration_ms=20,
        )

        self.assertTrue(result.passed)


class VersionedEvalSuiteTests(unittest.TestCase):
    def test_v2_suite_has_twenty_cases_and_deterministic_routes(self) -> None:
        """场景文案本身也要回归，避免关键词修改后悄悄走错工具池。"""

        service = AgentEvalService()
        suite = service._load_suite()
        router = AgentRequestRouter()

        self.assertEqual(len(suite["scenarios"]), 20)
        self.assertEqual(len({item["id"] for item in suite["scenarios"]}), 20)
        self.assertTrue(service.suite_info().data_fingerprint.startswith("sha256:"))

        for scenario in suite["scenarios"]:
            request = AgentChatRequest(
                message=scenario["message"],
                fund_code=scenario.get("fund_code") or None,
                portfolio_id=(
                    "portfolio_0123456789abcdef"
                    if scenario.get("portfolio_fixture")
                    else None
                ),
            )
            route = router.route(request, history=[])
            self.assertEqual(
                route.name,
                scenario["expected_route"],
                msg=f"场景路由错误：{scenario['id']}",
            )
            self.assertEqual(
                set(route.tool_names),
                set(scenario["required_tools"]),
                msg=f"场景工具池不够精确：{scenario['id']}",
            )

    def test_frozen_context_skips_database_nav_points(self) -> None:
        """评测只读 fixture；离开上下文后产品读取行为必须恢复。"""

        class FakeSnapshotRepository:
            def __init__(self) -> None:
                self.call_count = 0

            def list_points(self, *, fund_code: str):
                self.call_count += 1
                if fund_code != "110020":
                    return []
                return [
                    {
                        "nav_date": "2099-01-01",
                        "unit_nav": 9.99,
                        "daily_growth_rate_pct": 1.0,
                        "source": "fake database",
                        "source_url": "https://example.com",
                        "fetched_at": "2099-01-01T00:00:00+00:00",
                    }
                ]

        repository = FakeSnapshotRepository()
        catalog = FundCatalogService(snapshot_repository=repository)
        self.assertEqual(catalog.get_fund("110020")["snapshot_end"], "2099-01-01")
        calls_before_eval = repository.call_count

        with use_frozen_eval_data():
            frozen = catalog.get_fund("110020")

        self.assertEqual(frozen["snapshot_end"], "2026-08-25")
        self.assertEqual(frozen["data_mode"], "fixture")
        self.assertEqual(repository.call_count, calls_before_eval)
        self.assertEqual(catalog.get_fund("110020")["snapshot_end"], "2099-01-01")


class ObservabilityPersistenceTests(unittest.TestCase):
    def test_run_metrics_are_persistent_and_user_isolated(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database = Database(
                f"sqlite:///{Path(temporary_directory) / 'observability.db'}"
            )
            service = AgentObservabilityService(database)
            run = make_run(
                answer="存在回撤风险，依据 risk:max_drawdown:110020:12m，仍有不确定性。"
            )

            service.record_run(
                run=run,
                user_id="chen",
                duration_ms=321.4,
                source="chat",
                conversation_id="conv-1",
            )

            recreated = AgentObservabilityService(Database(database.url))
            summary = recreated.summary(user_id="chen")
            self.assertEqual(summary.run_count, 1)
            self.assertEqual(summary.total_tokens, 150)
            self.assertEqual(summary.recent_runs[0].duration_ms, 321.4)
            self.assertEqual(recreated.summary(user_id="other").run_count, 0)


class FakeEvalAgent:
    """评测运行器使用的无网络 Agent。"""

    async def chat(self, request, *, history):
        return make_run(
            answer=(
                "当前有回撤风险，依据 risk:max_drawdown:110020:12m；"
                "结论仍有不确定性。"
            )
        )


class AgentEvalRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_suite_run_is_saved_with_case_observation(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            suite_path = root / "suite.json"
            suite_path.write_text(
                json.dumps(
                    {
                        "id": "test-suite",
                        "name": "测试套件",
                        "description": "测试",
                        "scenarios": [
                            {
                                **SCENARIO,
                                "description": "综合测试",
                                "fund_code": "110020",
                                "message": "请综合分析基金",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            observability = AgentObservabilityService(
                Database(f"sqlite:///{root / 'runner.db'}")
            )
            service = AgentEvalService(
                agent=FakeEvalAgent(),
                observability=observability,
                suite_path=suite_path,
            )

            result = await service.run_suite(user_id="chen")

            self.assertEqual(result.passed_cases, 1)
            self.assertEqual(result.score, 100.0)
            self.assertEqual(
                observability.summary(user_id="chen").eval_run_count,
                1,
            )
            self.assertEqual(
                observability.recent_eval_runs(user_id="chen")[0].id,
                result.id,
            )
            self.assertEqual(
                observability.recent_eval_runs(
                    user_id="chen",
                    suite_id="another-suite",
                ),
                [],
            )

    async def test_score_delta_only_compares_the_same_case_set(self) -> None:
        """单题调试不能成为完整套件的基线，相同单题再次运行才可以比较。"""

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            suite_path = root / "suite.json"
            suite_path.write_text(
                json.dumps(
                    {
                        "id": "case-set-suite",
                        "name": "集合口径测试",
                        "description": "测试",
                        "scenarios": [
                            {
                                **SCENARIO,
                                "id": case_id,
                                "name": case_id,
                                "description": case_id,
                                "message": "请综合分析基金",
                            }
                            for case_id in ("case-a", "case-b")
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            observability = AgentObservabilityService(
                Database(f"sqlite:///{root / 'case-set.db'}")
            )
            service = AgentEvalService(
                agent=FakeEvalAgent(),
                observability=observability,
                suite_path=suite_path,
            )

            first_single = await service.run_suite(
                user_id="chen",
                case_ids=["case-a"],
            )
            full_run = await service.run_suite(user_id="chen")
            second_single = await service.run_suite(
                user_id="chen",
                case_ids=["case-a"],
            )

            self.assertIsNone(first_single.score_delta)
            self.assertIsNone(full_run.score_delta)
            self.assertEqual(second_single.score_delta, 0.0)
            self.assertNotEqual(first_single.case_set_id, full_run.case_set_id)
            dashboard = service.dashboard(user_id="chen")
            self.assertEqual(dashboard.latest_run.id, second_single.id)
            self.assertEqual(dashboard.previous_run.id, first_single.id)

            changed_fixture = root / "changed-fixture.json"
            changed_fixture.write_text('{"version": 2}', encoding="utf-8")
            changed_data_service = AgentEvalService(
                agent=FakeEvalAgent(),
                observability=observability,
                suite_path=suite_path,
                fixture_paths=[changed_fixture],
            )
            changed_data_run = await changed_data_service.run_suite(
                user_id="chen",
                case_ids=["case-a"],
            )

            self.assertIsNone(changed_data_run.score_delta)
            changed_dashboard = changed_data_service.dashboard(user_id="chen")
            self.assertEqual(changed_dashboard.latest_run.id, changed_data_run.id)
            self.assertIsNone(changed_dashboard.previous_run)


if __name__ == "__main__":
    unittest.main()

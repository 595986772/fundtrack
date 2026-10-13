"""组合候选场景的确定性验证测试，不调用模型或网络。"""

from __future__ import annotations

from copy import deepcopy
import unittest

from app.services.portfolio_adjustment_scenarios import (
    PortfolioAdjustmentScenarioError,
    PortfolioAdjustmentScenarioService,
)


PORTFOLIO_ID = "portfolio_0123456789abcdef"
SNAPSHOT_ID = "research_snapshot_0123456789abcdef"


def research_context() -> dict:
    return {
        "portfolio_name": "场景测试组合",
        "period_months": 12,
        "holdings": [
            {
                "fund_code": "110020",
                "short_name": "权益样本",
                "weight_pct": 60.0,
            },
            {
                "fund_code": "110007",
                "short_name": "固收样本",
                "weight_pct": 40.0,
            },
        ],
        "risk_snapshot": {
            "annualized_volatility_pct": 12.5,
            "daily_cvar_95_pct": 1.4,
            "max_drawdown_pct": 9.2,
        },
        "research_snapshot": {
            "snapshot_id": SNAPSHOT_ID,
            "content_hash": "a" * 64,
            "captured_at": "2026-09-01T00:00:00+00:00",
            "graph_version": "portfolio-research-v7",
        },
    }


def generated_adjustment() -> dict:
    return {
        "stance": "reduce_risk",
        "adjustments": [
            {
                "fund_code": "110020",
                "target_weight_pct": 50.0,
            },
            {
                "fund_code": "110007",
                "target_weight_pct": 50.0,
            },
        ],
    }


def constraints(
    *,
    max_change: float = 15,
    max_weight: float = 70,
    max_turnover: float = 20,
) -> dict:
    return {
        "risk_preference": "conservative",
        "max_single_adjustment_pct": max_change,
        "max_single_fund_weight_pct": max_weight,
        "max_turnover_pct": max_turnover,
    }


class FakeSnapshotReader:
    def __init__(self) -> None:
        self.snapshot = {
            "id": SNAPSHOT_ID,
            "user_id": "chen",
            "portfolio_id": PORTFOLIO_ID,
            "period_months": 12,
            "graph_version": "portfolio-research-v7",
            "content_hash": "a" * 64,
            "payload": {
                "quant_risk": {
                    "_scenario_validation_inputs": {
                        "schema_version": "portfolio-scenario-input-v1",
                        "fund_codes": ["110020", "110007"],
                        "data_quality_warnings": [],
                    }
                }
            },
        }
        self.calls = 0

    def get(self, *, user_id: str, snapshot_id: str) -> dict:
        self.calls += 1
        if user_id != "chen" or snapshot_id != SNAPSHOT_ID:
            raise AssertionError("场景测试读取了错误的快照身份")
        return deepcopy(self.snapshot)


class FakeFrozenRiskEvaluator:
    """权重越偏向固收，三个历史风险指标越低。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, float]] = []

    def evaluate_frozen_weights(
        self,
        *,
        scenario_inputs: dict,
        weights_pct: dict[str, float],
    ) -> dict:
        self.calls.append(dict(weights_pct))
        defensive_change = float(weights_pct["110007"]) - 40.0
        return {
            "period_start": "2025-09-01",
            "period_end": "2026-09-01",
            "return_observation_count": 240,
            "period_return_pct": round(6.0 - defensive_change * 0.05, 2),
            "annualized_volatility_pct": round(
                12.5 - defensive_change * 0.2,
                2,
            ),
            "daily_cvar_95_pct": round(1.4 - defensive_change * 0.03, 2),
            "max_drawdown": {
                "drawdown_pct": round(9.2 - defensive_change * 0.2, 2),
            },
            "return_basis": "total_return_reinvested",
            "return_basis_label": "分红再投资总回报",
        }


class PortfolioAdjustmentScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshots = FakeSnapshotReader()
        self.evaluator = FakeFrozenRiskEvaluator()
        self.service = PortfolioAdjustmentScenarioService(
            snapshots=self.snapshots,
            risk_evaluator=self.evaluator,
        )

    def test_builds_baseline_gradual_and_full_scenarios(self) -> None:
        result = self.service.validate(
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            research_context=research_context(),
            generated_adjustment=generated_adjustment(),
            user_constraints=constraints(),
        )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["baseline_consistency"]["consistent"])
        self.assertEqual(
            [item["scenario_id"] for item in result["scenarios"]],
            ["baseline", "gradual_adjustment", "model_proposal"],
        )
        gradual, full = result["scenarios"][1:]
        self.assertEqual(gradual["metrics"]["turnover_pct"], 5.0)
        self.assertEqual(full["metrics"]["turnover_pct"], 10.0)
        self.assertEqual(full["objective_status"], "met")
        self.assertEqual(
            full["execution_feasibility"],
            "information_insufficient",
        )
        self.assertGreater(len(result["comparison_facts"]), 0)
        self.assertFalse(result["execution_ready"])
        self.assertEqual(len(self.evaluator.calls), 3)

    def test_full_candidate_fails_human_limit_but_gradual_remains_comparable(self) -> None:
        result = self.service.validate(
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            research_context=research_context(),
            generated_adjustment=generated_adjustment(),
            user_constraints=constraints(max_change=7),
        )

        gradual, full = result["scenarios"][1:]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(gradual["comparison_status"], "passed")
        self.assertEqual(full["comparison_status"], "failed")
        self.assertEqual(result["comparable_scenario_count"], 1)
        self.assertEqual(len(self.evaluator.calls), 2)

    def test_turnover_limit_rejects_only_the_full_candidate(self) -> None:
        result = self.service.validate(
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            research_context=research_context(),
            generated_adjustment=generated_adjustment(),
            user_constraints=constraints(max_turnover=7),
        )

        gradual, full = result["scenarios"][1:]
        self.assertEqual(gradual["comparison_status"], "passed")
        self.assertEqual(full["comparison_status"], "failed")
        turnover_check = next(
            item
            for item in full["constraint_checks"]
            if item["check_key"] == "max_turnover"
        )
        self.assertEqual(turnover_check["status"], "failed")

    def test_all_candidates_can_fail_without_turning_into_data_missing(self) -> None:
        result = self.service.validate(
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            research_context=research_context(),
            generated_adjustment=generated_adjustment(),
            user_constraints=constraints(max_turnover=0),
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["comparable_scenario_count"], 0)
        self.assertTrue(
            all(
                item["comparison_status"] == "failed"
                for item in result["scenarios"][1:]
            )
        )
        self.assertEqual(result["scenarios"][-1]["objective_status"], "failed")

    def test_maintain_candidate_completes_with_baseline_only(self) -> None:
        maintain = generated_adjustment()
        maintain["stance"] = "maintain"
        maintain["adjustments"][0]["target_weight_pct"] = 60.0
        maintain["adjustments"][1]["target_weight_pct"] = 40.0

        result = self.service.validate(
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            research_context=research_context(),
            generated_adjustment=maintain,
            user_constraints=constraints(),
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["scenarios"]), 1)
        self.assertEqual(result["scenarios"][0]["scenario_id"], "baseline")

    def test_legacy_report_without_snapshot_is_explicitly_insufficient(self) -> None:
        context = research_context()
        context["research_snapshot"] = {}

        result = self.service.validate(
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            research_context=context,
            generated_adjustment=generated_adjustment(),
            user_constraints=constraints(),
        )

        self.assertEqual(result["status"], "information_insufficient")
        self.assertEqual(result["comparable_scenario_count"], 0)
        self.assertIn("冻结", result["limitations"][0])
        self.assertEqual(self.snapshots.calls, 0)
        self.assertEqual(self.evaluator.calls, [])

    def test_snapshot_hash_mismatch_is_not_downgraded_to_missing_information(self) -> None:
        context = research_context()
        context["research_snapshot"]["content_hash"] = "b" * 64

        with self.assertRaisesRegex(
            PortfolioAdjustmentScenarioError,
            "content_hash",
        ):
            self.service.validate(
                user_id="chen",
                portfolio_id=PORTFOLIO_ID,
                research_context=context,
                generated_adjustment=generated_adjustment(),
                user_constraints=constraints(),
            )

    def test_baseline_mismatch_prevents_verified_claim(self) -> None:
        context = research_context()
        context["risk_snapshot"]["annualized_volatility_pct"] = 99.0

        result = self.service.validate(
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            research_context=context,
            generated_adjustment=generated_adjustment(),
            user_constraints=constraints(),
        )

        self.assertEqual(result["status"], "information_insufficient")
        self.assertFalse(result["baseline_consistency"]["consistent"])
        self.assertEqual(
            result["scenarios"][-1]["objective_status"],
            "information_insufficient",
        )


if __name__ == "__main__":
    unittest.main()

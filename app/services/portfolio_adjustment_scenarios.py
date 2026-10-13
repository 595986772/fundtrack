"""在同一份不可变研究快照上验证组合调整候选场景。

模型给出的目标权重在这里永远只是一个候选。Python 会先保留“不调整”基线，再构造
“渐进调整”和“完整调整”，使用研究开始时冻结的同一日收益矩阵重算历史风险。这个
Service 不调用大模型、不读取最新行情，也不会创建任何真实交易。
"""

from __future__ import annotations

import math
from typing import Any, Protocol

from app.services.portfolio_research_snapshots import (
    PortfolioResearchSnapshotError,
    portfolio_research_snapshot_service,
)
from app.services.portfolio_risk_engine import (
    PortfolioRiskEngineError,
    portfolio_risk_engine,
)


SCENARIO_COMPARISON_VERSION = "portfolio-scenario-comparison-v1"
METRIC_SPECS = (
    ("annualized_volatility_pct", "年化波动率", True, "percent"),
    ("daily_cvar_95_pct", "日 CVaR 95%", True, "percent"),
    ("max_drawdown_pct", "最大回撤", True, "percent"),
    ("period_return_pct", "区间历史收益", False, "percent"),
    ("concentration_hhi", "配置集中度 HHI", True, "decimal"),
    ("max_weight_pct", "最大单只权重", True, "percent"),
    ("turnover_pct", "单边换手率", True, "percent"),
)


class PortfolioAdjustmentScenarioError(RuntimeError):
    """快照身份损坏，或冻结数据无法完成可信重算。"""


class SnapshotReader(Protocol):
    def get(self, *, user_id: str, snapshot_id: str) -> dict[str, Any]: ...


class FrozenRiskEvaluator(Protocol):
    def evaluate_frozen_weights(
        self,
        *,
        scenario_inputs: dict[str, Any],
        weights_pct: dict[str, float],
    ) -> dict[str, Any]: ...


class PortfolioAdjustmentScenarioService:
    """构造候选、执行确定性重算，并公开可追溯的比较事实。"""

    def __init__(
        self,
        *,
        snapshots: SnapshotReader = portfolio_research_snapshot_service,
        risk_evaluator: FrozenRiskEvaluator = portfolio_risk_engine,
    ) -> None:
        self.snapshots = snapshots
        self.risk_evaluator = risk_evaluator

    def validate(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        research_context: dict[str, Any],
        generated_adjustment: dict[str, Any],
        user_constraints: dict[str, Any],
    ) -> dict[str, Any]:
        """比较基线、渐进方案和完整方案，返回纯 Python 事实。"""

        candidates = self._build_candidates(
            holdings=list(research_context.get("holdings") or []),
            adjustments=list(generated_adjustment.get("adjustments") or []),
        )
        snapshot_result = self._load_scenario_inputs(
            user_id=user_id,
            portfolio_id=portfolio_id,
            research_context=research_context,
        )
        if snapshot_result["scenario_inputs"] is None:
            return self._information_insufficient_result(
                candidates=candidates,
                source_snapshot=snapshot_result["source_snapshot"],
                reason=snapshot_result["reason"],
                user_constraints=user_constraints,
            )

        scenario_inputs = snapshot_result["scenario_inputs"]
        evaluated = [
            self._evaluate_candidate(
                candidate=candidate,
                scenario_inputs=scenario_inputs,
                max_single_adjustment_pct=float(
                    user_constraints["max_single_adjustment_pct"]
                ),
                max_single_fund_weight_pct=float(
                    user_constraints.get("max_single_fund_weight_pct", 70)
                ),
                max_turnover_pct=float(
                    user_constraints.get("max_turnover_pct", 20)
                ),
                has_free_text_constraints=bool(
                    str(user_constraints.get("additional_constraints") or "").strip()
                ),
            )
            for candidate in candidates
        ]
        baseline = evaluated[0]
        baseline_consistency = self._baseline_consistency(
            baseline=baseline,
            risk_snapshot=dict(research_context.get("risk_snapshot") or {}),
        )

        for scenario in evaluated:
            self._attach_deltas_and_objective(
                scenario=scenario,
                baseline=baseline,
                user_constraints=user_constraints,
                generated_adjustment=generated_adjustment,
                baseline_consistent=baseline_consistency["consistent"],
            )

        facts = self._comparison_facts(evaluated)
        comparable = [
            item
            for item in evaluated[1:]
            if item["comparison_status"] == "passed"
        ]
        candidate_results_complete = all(
            item["comparison_status"] != "information_insufficient"
            for item in evaluated[1:]
        )
        status = (
            "completed"
            if baseline_consistency["consistent"] and candidate_results_complete
            else "information_insufficient"
        )
        return {
            "data_kind": "portfolio_adjustment_scenario_comparison",
            "schema_version": SCENARIO_COMPARISON_VERSION,
            "status": status,
            "source_snapshot": snapshot_result["source_snapshot"],
            "baseline_scenario_id": "baseline",
            "baseline_consistency": baseline_consistency,
            "scenarios": evaluated,
            "comparison_facts": facts,
            "comparable_scenario_count": len(comparable),
            "execution_ready": False,
            "methodology": [
                "三个场景使用同一份冻结日收益矩阵和相同日期范围。",
                "组合收益按候选权重每日再平衡近似，历史结果不代表未来。",
                "单边换手率按 0.5 × 各基金权重变化绝对值之和计算。",
            ],
            "limitations": [
                "尚未取得申购赎回费、持有期限和赎回限制，不能声称方案可直接执行。",
                *(
                    ["补充约束是自然语言偏好，尚未被 Python 解析为确定性规则。"]
                    if str(
                        user_constraints.get("additional_constraints") or ""
                    ).strip()
                    else []
                ),
                "候选场景没有预测未来收益，也不会创建真实订单。",
                *list(scenario_inputs.get("data_quality_warnings") or []),
            ],
        }

    def _load_scenario_inputs(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        research_context: dict[str, Any],
    ) -> dict[str, Any]:
        """从来源报告绑定的快照读取内部矩阵，并严格核对身份和哈希。"""

        metadata = dict(research_context.get("research_snapshot") or {})
        snapshot_id = str(metadata.get("snapshot_id") or "").strip()
        public_source = {
            key: metadata.get(key)
            for key in (
                "snapshot_id",
                "content_hash",
                "captured_at",
                "graph_version",
            )
        }
        if not snapshot_id:
            return {
                "scenario_inputs": None,
                "source_snapshot": public_source,
                "reason": "来源研究创建于场景输入冻结功能之前。",
            }

        try:
            snapshot = self.snapshots.get(
                user_id=user_id,
                snapshot_id=snapshot_id,
            )
        except PortfolioResearchSnapshotError as exc:
            raise PortfolioAdjustmentScenarioError(
                "来源研究快照无法通过完整性校验。"
            ) from exc

        expected = {
            "portfolio_id": portfolio_id,
            "content_hash": metadata.get("content_hash"),
            "period_months": research_context.get("period_months"),
        }
        for key, value in expected.items():
            if value is not None and snapshot.get(key) != value:
                raise PortfolioAdjustmentScenarioError(
                    f"来源研究快照的 {key} 与调整流程不一致。"
                )

        quant_risk = (snapshot.get("payload") or {}).get("quant_risk") or {}
        scenario_inputs = quant_risk.get("_scenario_validation_inputs")
        public_source.update(
            {
                "snapshot_id": snapshot["id"],
                "content_hash": snapshot["content_hash"],
                "graph_version": snapshot["graph_version"],
            }
        )
        if not isinstance(scenario_inputs, dict):
            return {
                "scenario_inputs": None,
                "source_snapshot": public_source,
                "reason": "该研究快照没有冻结候选场景所需的日收益矩阵。",
            }
        return {
            "scenario_inputs": scenario_inputs,
            "source_snapshot": public_source,
            "reason": None,
        }

    @staticmethod
    def _build_candidates(
        *,
        holdings: list[dict[str, Any]],
        adjustments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """模型只给一个终点；Python 再加入原地不动和半程渐进两个透明场景。"""

        if not holdings:
            raise PortfolioAdjustmentScenarioError("研究上下文缺少当前持仓。")
        current = {
            str(item["fund_code"]): float(item["weight_pct"])
            for item in holdings
        }
        short_names = {
            str(item["fund_code"]): str(item.get("short_name") or item["fund_code"])
            for item in holdings
        }
        target = {
            str(item["fund_code"]): float(item["target_weight_pct"])
            for item in adjustments
        }
        if set(target) != set(current):
            raise PortfolioAdjustmentScenarioError(
                "模型候选的基金集合与当前组合不一致。"
            )

        raw_candidates = [
            ("baseline", "不调整基线", "current_weights", current),
        ]
        if any(abs(target[code] - current[code]) > 0.0001 for code in current):
            gradual = {
                code: (current[code] + target[code]) / 2
                for code in current
            }
            raw_candidates.extend(
                [
                    (
                        "gradual_adjustment",
                        "渐进调整",
                        "deterministic_half_step",
                        gradual,
                    ),
                    ("model_proposal", "完整调整", "model_target", target),
                ]
            )

        candidates = []
        seen: set[tuple[float, ...]] = set()
        for scenario_id, label, source, weights in raw_candidates:
            fingerprint = tuple(round(weights[code], 6) for code in current)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            candidates.append(
                {
                    "scenario_id": scenario_id,
                    "label": label,
                    "source": source,
                    "weights_pct": dict(weights),
                    "weight_rows": [
                        {
                            "fund_code": code,
                            "short_name": short_names[code],
                            "weight_pct": round(weights[code], 4),
                            "change_pct_points": round(
                                weights[code] - current[code],
                                4,
                            ),
                        }
                        for code in current
                    ],
                    "current_weights_pct": current,
                }
            )
        return candidates

    def _evaluate_candidate(
        self,
        *,
        candidate: dict[str, Any],
        scenario_inputs: dict[str, Any],
        max_single_adjustment_pct: float,
        max_single_fund_weight_pct: float,
        max_turnover_pct: float,
        has_free_text_constraints: bool,
    ) -> dict[str, Any]:
        """先检查硬约束；通过后才调用风险引擎计算历史指标。"""

        weights = dict(candidate["weights_pct"])
        current = dict(candidate["current_weights_pct"])
        total = sum(weights.values())
        non_negative = all(value >= 0 for value in weights.values())
        same_universe = set(weights) == set(scenario_inputs.get("fund_codes") or [])
        max_change = max(abs(weights[code] - current[code]) for code in current)
        within_change = max_change <= max_single_adjustment_pct + 0.0001
        turnover = 0.5 * sum(
            abs(weights[code] - current[code]) for code in current
        )
        within_weight = max(weights.values()) <= max_single_fund_weight_pct + 0.0001
        within_turnover = turnover <= max_turnover_pct + 0.0001

        checks = [
            self._check(
                "weight_total",
                "目标权重合计",
                "passed" if math.isclose(total, 100.0, abs_tol=0.01) else "failed",
                f"合计 {total:.2f}%",
            ),
            self._check(
                "non_negative",
                "权重非负",
                "passed" if non_negative else "failed",
                "所有权重均不小于零" if non_negative else "存在负权重",
            ),
            self._check(
                "fund_universe",
                "基金集合",
                "passed" if same_universe else "failed",
                "只使用当前组合基金" if same_universe else "基金集合发生越界",
            ),
            self._check(
                "max_single_adjustment",
                "单只最大调整",
                "passed" if within_change else "failed",
                f"最大变化 {max_change:.2f} 个百分点",
            ),
            self._check(
                "max_single_fund_weight",
                "单只最终权重",
                "passed" if within_weight else "failed",
                (
                    f"最大单只权重 {max(weights.values()):.2f}%"
                    f"，上限 {max_single_fund_weight_pct:.2f}%"
                ),
            ),
            self._check(
                "max_turnover",
                "单边换手率",
                "passed" if within_turnover else "failed",
                f"换手 {turnover:.2f}%，上限 {max_turnover_pct:.2f}%",
            ),
        ]
        structural_failed = any(
            item["status"] == "failed"
            for item in checks
            if item["check_key"] in {
                "weight_total",
                "non_negative",
                "fund_universe",
            }
        )
        candidate_constraint_failed = any(
            item["status"] == "failed"
            for item in checks
            if item["check_key"] not in {
                "weight_total",
                "non_negative",
                "fund_universe",
            }
        )
        # 基线即使已经超过用户希望的目标上限，也必须保留并计算，才能证明候选
        # 是否真的改善；这些约束只决定候选是否通过。
        hard_failed = structural_failed or (
            candidate["scenario_id"] != "baseline"
            and candidate_constraint_failed
        )
        metrics: dict[str, Any] | None = None
        evaluation_error: str | None = None
        if not hard_failed:
            try:
                metrics = self.risk_evaluator.evaluate_frozen_weights(
                    scenario_inputs=scenario_inputs,
                    weights_pct=weights,
                )
                checks.append(
                    self._check(
                        "frozen_return_sample",
                        "冻结历史样本",
                        "passed",
                        f"共同收益样本 {metrics['return_observation_count']} 条",
                    )
                )
            except PortfolioRiskEngineError as exc:
                evaluation_error = str(exc)
                checks.append(
                    self._check(
                        "frozen_return_sample",
                        "冻结历史样本",
                        "information_insufficient",
                        evaluation_error,
                    )
                )

        changed = turnover > 0.0001
        checks.extend(
            [
                self._check(
                    "transaction_costs",
                    "申购赎回费用",
                    "information_insufficient" if changed else "not_applicable",
                    "未提供真实费率" if changed else "基线没有换手",
                ),
                self._check(
                    "redemption_restrictions",
                    "持有期与赎回限制",
                    "information_insufficient" if changed else "not_applicable",
                    "未提供份额持有期限规则" if changed else "基线没有赎回",
                ),
                self._check(
                    "free_text_constraints",
                    "自然语言补充约束",
                    (
                        "information_insufficient"
                        if has_free_text_constraints
                        else "not_applicable"
                    ),
                    (
                        "仅用于生成候选，尚未转成 Python 规则"
                        if has_free_text_constraints
                        else "没有补充约束"
                    ),
                ),
            ]
        )
        comparison_status = (
            "failed"
            if hard_failed
            else "information_insufficient"
            if metrics is None
            else "passed"
        )
        concentration = self._concentration(weights)
        public_metrics = self._public_metrics(metrics)
        public_metrics.update(concentration)
        public_metrics["turnover_pct"] = round(turnover, 2)

        return {
            "scenario_id": candidate["scenario_id"],
            "label": candidate["label"],
            "source": candidate["source"],
            "comparison_status": comparison_status,
            "execution_feasibility": (
                "not_applicable"
                if not changed
                else "information_insufficient"
            ),
            "weights": candidate["weight_rows"],
            "metrics": public_metrics,
            "metric_deltas": {},
            "constraint_checks": checks,
            "objective_status": "pending",
            "objective_label": "等待与基线比较",
            "evaluation_error": evaluation_error,
        }

    @staticmethod
    def _public_metrics(metrics: dict[str, Any] | None) -> dict[str, Any]:
        if not metrics:
            return {
                "period_return_pct": None,
                "annualized_volatility_pct": None,
                "daily_cvar_95_pct": None,
                "max_drawdown_pct": None,
                "return_observation_count": None,
                "period_start": None,
                "period_end": None,
            }
        return {
            "period_return_pct": metrics.get("period_return_pct"),
            "annualized_volatility_pct": metrics.get(
                "annualized_volatility_pct"
            ),
            "daily_cvar_95_pct": metrics.get("daily_cvar_95_pct"),
            "max_drawdown_pct": (metrics.get("max_drawdown") or {}).get(
                "drawdown_pct"
            ),
            "return_observation_count": metrics.get("return_observation_count"),
            "period_start": metrics.get("period_start"),
            "period_end": metrics.get("period_end"),
            "return_basis": metrics.get("return_basis"),
            "return_basis_label": metrics.get("return_basis_label"),
        }

    @staticmethod
    def _concentration(weights: dict[str, float]) -> dict[str, Any]:
        shares = [value / 100 for value in weights.values()]
        hhi = sum(value * value for value in shares)
        return {
            "concentration_hhi": round(hhi, 4),
            "effective_fund_count": round(1 / hhi, 2) if hhi > 0 else 0,
            "max_weight_pct": round(max(weights.values()), 2),
        }

    @staticmethod
    def _baseline_consistency(
        *,
        baseline: dict[str, Any],
        risk_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """证明重算基线与来源报告一致，避免算法口径悄悄变化。"""

        checks = {
            "annualized_volatility_pct": risk_snapshot.get(
                "annualized_volatility_pct"
            ),
            "daily_cvar_95_pct": risk_snapshot.get("daily_cvar_95_pct"),
            "max_drawdown_pct": risk_snapshot.get("max_drawdown_pct"),
        }
        differences = []
        for key, expected in checks.items():
            actual = baseline["metrics"].get(key)
            if expected is None or actual is None:
                continue
            if abs(float(actual) - float(expected)) > 0.06:
                differences.append(
                    {
                        "metric_key": key,
                        "report_value": expected,
                        "recalculated_value": actual,
                    }
                )
        consistent = baseline["comparison_status"] == "passed" and not differences
        return {
            "consistent": consistent,
            "differences": differences,
            "message": (
                "冻结矩阵重算结果与来源研究基线一致。"
                if consistent
                else "来源报告与冻结矩阵重算基线不一致，候选结果不标记为已验证。"
            ),
        }

    @staticmethod
    def _attach_deltas_and_objective(
        *,
        scenario: dict[str, Any],
        baseline: dict[str, Any],
        user_constraints: dict[str, Any],
        generated_adjustment: dict[str, Any],
        baseline_consistent: bool,
    ) -> None:
        if scenario["scenario_id"] == "baseline":
            scenario["objective_status"] = "baseline"
            scenario["objective_label"] = "不调整参照"
            return

        deltas = {}
        for key, _label, _lower_is_better, _kind in METRIC_SPECS:
            value = scenario["metrics"].get(key)
            base_value = baseline["metrics"].get(key)
            deltas[key] = (
                round(float(value) - float(base_value), 4)
                if value is not None and base_value is not None
                else None
            )
        scenario["metric_deltas"] = deltas

        if scenario["comparison_status"] == "failed":
            scenario["objective_status"] = "failed"
            scenario["objective_label"] = "人工约束未通过"
            return
        if scenario["comparison_status"] != "passed":
            scenario["objective_status"] = "information_insufficient"
            scenario["objective_label"] = "暂不能验证目标"
            return
        if not baseline_consistent:
            scenario["objective_status"] = "information_insufficient"
            scenario["objective_label"] = "暂不能验证目标"
            scenario["comparison_status"] = "information_insufficient"
            return

        risk_objective = (
            user_constraints.get("risk_preference") == "conservative"
            or generated_adjustment.get("stance") == "reduce_risk"
        )
        if not risk_objective:
            scenario["objective_status"] = "descriptive_only"
            scenario["objective_label"] = "仅展示历史取舍"
            return

        risk_deltas = [
            deltas[key]
            for key in (
                "annualized_volatility_pct",
                "daily_cvar_95_pct",
                "max_drawdown_pct",
            )
            if deltas[key] is not None
        ]
        meets = bool(risk_deltas) and all(value <= 0.01 for value in risk_deltas) and any(
            value < -0.01 for value in risk_deltas
        )
        scenario["objective_status"] = "met" if meets else "not_met"
        scenario["objective_label"] = (
            "历史降风险目标通过" if meets else "历史降风险目标未通过"
        )

    @classmethod
    def _comparison_facts(
        cls,
        scenarios: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """为下一层模型解释生成稳定引用键，模型不能自行发明指标。"""

        facts = []
        for scenario in scenarios[1:]:
            for key, label, lower_is_better, kind in METRIC_SPECS:
                value = scenario["metrics"].get(key)
                delta = scenario["metric_deltas"].get(key)
                if value is None or delta is None:
                    continue
                direction = (
                    "neutral"
                    if abs(delta) <= 0.01
                    else "supportive"
                    if (delta < 0 and lower_is_better) or (
                        delta > 0 and not lower_is_better
                    )
                    else "caution"
                )
                facts.append(
                    {
                        "metric_key": f"scenario:{scenario['scenario_id']}:{key}",
                        "scenario_id": scenario["scenario_id"],
                        "metric": key,
                        "label": label,
                        "value": value,
                        "value_text": cls._value_text(value, kind),
                        "delta": delta,
                        "delta_text": cls._delta_text(delta, kind),
                        "direction": direction,
                    }
                )
        return facts

    @staticmethod
    def _value_text(value: float, kind: str) -> str:
        return f"{float(value):.2f}%" if kind == "percent" else f"{float(value):.4f}"

    @staticmethod
    def _delta_text(value: float, kind: str) -> str:
        prefix = "+" if value > 0 else ""
        suffix = " 个百分点" if kind == "percent" else ""
        precision = 2 if kind == "percent" else 4
        return f"{prefix}{float(value):.{precision}f}{suffix}"

    @staticmethod
    def _check(
        check_key: str,
        label: str,
        status: str,
        detail: str,
    ) -> dict[str, str]:
        return {
            "check_key": check_key,
            "label": label,
            "status": status,
            "detail": detail,
        }

    def _information_insufficient_result(
        self,
        *,
        candidates: list[dict[str, Any]],
        source_snapshot: dict[str, Any],
        reason: str,
        user_constraints: dict[str, Any],
    ) -> dict[str, Any]:
        """旧研究仍可生成权重，但页面必须诚实标记无法做同快照重算。"""

        scenarios = []
        for candidate in candidates:
            concentration = self._concentration(candidate["weights_pct"])
            turnover = 0.5 * sum(
                abs(
                    candidate["weights_pct"][code]
                    - candidate["current_weights_pct"][code]
                )
                for code in candidate["weights_pct"]
            )
            scenarios.append(
                {
                    "scenario_id": candidate["scenario_id"],
                    "label": candidate["label"],
                    "source": candidate["source"],
                    "comparison_status": "information_insufficient",
                    "execution_feasibility": "information_insufficient",
                    "weights": candidate["weight_rows"],
                    "metrics": {
                        **self._public_metrics(None),
                        **concentration,
                        "turnover_pct": round(turnover, 2),
                    },
                    "metric_deltas": {},
                    "constraint_checks": [
                        self._check(
                            "frozen_return_sample",
                            "冻结历史样本",
                            "information_insufficient",
                            reason,
                        )
                    ],
                    "objective_status": "information_insufficient",
                    "objective_label": "暂不能验证目标",
                    "evaluation_error": reason,
                }
            )
        return {
            "data_kind": "portfolio_adjustment_scenario_comparison",
            "schema_version": SCENARIO_COMPARISON_VERSION,
            "status": "information_insufficient",
            "source_snapshot": source_snapshot,
            "baseline_scenario_id": "baseline",
            "baseline_consistency": {
                "consistent": False,
                "differences": [],
                "message": reason,
            },
            "scenarios": scenarios,
            "comparison_facts": [],
            "comparable_scenario_count": 0,
            "execution_ready": False,
            "methodology": ["只有冻结日收益矩阵可用时才计算候选历史风险。"],
            "limitations": [
                reason,
                "当前权重建议没有通过同快照历史场景验证。",
                "尚未取得费用、持有期限和赎回限制。",
            ],
            "user_constraints": dict(user_constraints),
        }


portfolio_adjustment_scenario_service = PortfolioAdjustmentScenarioService()

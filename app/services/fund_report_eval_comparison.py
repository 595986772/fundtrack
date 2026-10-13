"""读取两次既有季报测评做对照，不重新调用模型，也不重算或改写旧成绩。

同题、同原文、同评分器、同运行预算才计算差值。代码指纹应当变化，因为我们
正是在比较代码优化；模型/检索/预算改变则属于另一种实验，不能混在一起。
"""

from math import ceil
from statistics import median


CONFIG_KEYS = ("model", "embedding_model", "embedding_dimensions", "retrieval_mode",
    "synthesis_mode", "report_timeout_seconds", "model_timeout_seconds", "model_max_attempts", "grader", "ai_governance", "reranker")


def _config_value(row, key):
    # 保留旧测评之间的只读对照；旧数据与新增治理的数据不能声称预算条件相同。
    return row.get(key, {"state": "not_recorded"}) if key in {"ai_governance", "reranker"} else row.get(key)


def _config(run):
    rows = [case.report_quality.get("run_config") or case.execution_diagnostics.get("run_config")
        for case in run.results]
    available = [row for row in rows if row]
    signature = {key: _config_value(available[0], key) for key in CONFIG_KEYS} if available else {}
    consistent = bool(signature) and all(signature.get(key) is not None for key in CONFIG_KEYS)
    consistent &= all(all(_config_value(row, key) == signature.get(key) for key in CONFIG_KEYS) for row in available)
    # 两次运行之间代码应可不同，但同一轮测评不能中途换代码后混算成绩。
    consistent &= len({row.get("pipeline_fingerprint") for row in available}) <= 1
    return signature, consistent, sum(not row for row in rows)


def _turns(case):
    return case.report_quality.get("turns") or case.execution_diagnostics.get("turns") or []


def _summary(run):
    """耗时包含失败，避免只统计成功题后把超时藏起来；小样本P95只描述此批题。"""
    durations = sorted(case.duration_ms for case in run.results)
    turns = [turn for case in run.results for turn in _turns(case)]
    attempts = [row for turn in turns for row in turn.get("execution", {}).get("model_attempts", [])]
    phases = {}
    for case in run.results:
        # 旧失败记录没有逐轮结构，仍可利用其阶段诊断，不伪造该轮答案。
        executions = [turn.get("execution", {}) for turn in _turns(case)] or [case.execution_diagnostics]
        for execution in executions:
            for stage in execution.get("stages", []):
                phase = stage["phase"]
                phases[phase] = round(phases.get(phase, 0) + stage["duration_ms"], 2)
    return {"run_id": run.id, "cases": len(run.results), "passed": sum(case.passed for case in run.results),
        "execution_failures": sum(bool(case.error) for case in run.results),
        "limited_turns": sum(turn.get("answer_status") == "limited" for turn in turns),
        "recorded_turns": len(turns), "duration_ms": run.duration_ms,
        "mean_case_ms": round(sum(durations) / len(durations), 2) if durations else None,
        "median_case_ms": round(median(durations), 2) if durations else None,
        "p95_case_ms": durations[ceil(len(durations) * .95) - 1] if durations else None,
        "known_tokens": sum(case.usage.total_tokens for case in run.results),
        "attempts_missing_usage": sum(row.get("usage") is None for row in attempts),
        "cases_with_legacy_diagnostics": sum(not _turns(case) or any(
            turn.get("execution", {}).get("version") != "fund-report-runtime-v2" for turn in _turns(case)) for case in run.results),
        "stage_totals_ms": phases, "report_metrics": run.report_metrics,
        "pipeline_fingerprints": sorted({config["pipeline_fingerprint"] for case in run.results
            if (config := case.report_quality.get("run_config") or case.execution_diagnostics.get("run_config"))
            and config.get("pipeline_fingerprint")}),
        "synthesis_policies": sorted({turn.get("execution", {}).get("synthesis_policy", "legacy") for turn in turns}),
        "timeout_fallback_attempts": sum(row.get("strategy") == "timeout_non_thinking" for row in attempts),
        "usage_complete": bool(turns) and all(not case.error and _turns(case) and all(
            turn.get("execution", {}).get("usage_complete", False) for turn in _turns(case)) for case in run.results)}


def compare_report_runs(before, after):
    """失败不剔除、人工复核不代签；旧记录缺失的详细诊断明确列为不可得。"""
    reasons, warnings = [], []
    before_ids = [case.case_id for case in before.results]
    after_ids = [case.case_id for case in after.results]
    if before.user_id != after.user_id:
        reasons.append("两次运行不属于同一用户。")
    if before.suite_id != "fund_report_rag_v1" or before.suite_id != after.suite_id:
        reasons.append("必须是同一季报套件。")
    if not before.data_fingerprint or before.data_fingerprint != after.data_fingerprint:
        reasons.append("原文、题目、检索配置或评分器指纹不同。")
    if not before_ids or before_ids != after_ids or len(set(before_ids)) != len(before_ids):
        reasons.append("题目集合或执行顺序不同；不能把三题诊断当作二十题全量改进。")
    configurations = []
    for run in (before, after):
        config, consistent, missing = _config(run)
        configurations.append(config)
        if not consistent:
            reasons.append(f"{run.id} 缺少完整配置或运行中配置不一致。")
        if missing:
            warnings.append(f"{run.id} 有{missing}题未单独记录配置，配置对照来自同次运行其余场景。")
    for key in CONFIG_KEYS:
        if configurations[0].get(key) != configurations[1].get(key):
            reasons.append(f"运行配置 {key} 不同。")
    summaries = [_summary(run) for run in (before, after)]
    if summaries[0]["synthesis_policies"] != summaries[1]["synthesis_policies"]:
        warnings.append("整理恢复策略发生变化，属于同预算下的执行策略优化；不是保持全部推理参数不变的实验。")
    if not all(summary["usage_complete"] for summary in summaries):
        warnings.append("存在超时或旧版缺失用量：known_tokens仅为已记录用量，不计算完整费用节省比例。")
    warnings.append("单次小样本结果受供应商负载及生成随机性影响；P95按整题计算，多轮题包含所有轮次，不是线上SLA。")
    warnings.append("自动通过率不是语义准确率；召回/数字/引用指标须连同覆盖分母和待人工复核数阅读。")
    comparison = {"comparable": not reasons, "reasons": reasons, "warnings": warnings,
        "before": summaries[0], "after": summaries[1], "configurations": configurations,
        "delta": None, "cases": []}
    if reasons:
        return comparison
    comparison["delta"] = {key: round(summaries[1][key] - summaries[0][key], 2)
        for key in ("passed", "execution_failures", "limited_turns", "duration_ms", "mean_case_ms", "median_case_ms", "p95_case_ms")}
    for old, new in zip(before.results, after.results):
        comparison["cases"].append({"case_id": old.case_id,
            "before_passed": old.passed, "after_passed": new.passed,
            "before_ms": old.duration_ms, "after_ms": new.duration_ms,
            "before_known_tokens": old.usage.total_tokens, "after_known_tokens": new.usage.total_tokens,
            "before_statuses": [turn.get("answer_status") for turn in _turns(old)],
            "after_statuses": [turn.get("answer_status") for turn in _turns(new)],
            "before_error": old.error, "after_error": new.error})
    return comparison

"""确定性的组合求解器：SciPy/HiGHS 求解 CVaR 线性规划，Riskfolio 独立复核风险。

模型不生成权重。长仓、满仓、固定基金池是本轮硬边界；无解只诊断，不偷偷放宽。
"""
import numpy as np
import riskfolio as rp
from scipy.optimize import linprog

from app.services.portfolio_allocation_contracts import AllocationConstraints
from app.services.portfolio_stress import PortfolioStressError, checked_weights


SOLVER_VERSION = "historical-cvar-highs-v1"
LABELS = {"min_bond_weight_pct": "债券类基金最低占比", "max_single_fund_weight_pct": "单只基金最高占比", "max_turnover_pct": "单边换手上限"}


def solve_allocation(frozen: dict, classifications: list[dict], constraints: AllocationConstraints) -> dict:
    """同一份冻结日收益，先判可行再优化；风险单位为百分数，不做年化或未来预测。"""
    codes = frozen["fund_codes"]
    baseline = checked_weights(frozen["baseline_weights"], codes)
    all_returns = np.asarray(frozen["daily_returns"], dtype=float)
    eligible = np.asarray(frozen["eligible"], dtype=bool)
    if all_returns.shape != (len(eligible), len(codes)) or not np.isfinite(all_returns).all() or (all_returns <= -1).any():
        raise PortfolioStressError("冻结收益不完整或含无效数值")
    returns = all_returns[eligible]
    if len(returns) < 60:
        raise PortfolioStressError("CVaR 求解至少需要60个完整共同观察日；请先补齐本地净值")
    types = {item["fund_code"]: item for item in classifications}
    if constraints.min_bond_weight_pct > 0 and any(types[c]["is_bond"] is None for c in codes):
        raise PortfolioStressError("部分基金类型未知，无法核对债券类基金约束；请先补齐基金档案")
    bonds = np.array([types[c]["is_bond"] is True for c in codes], dtype=float)
    limits = constraints.model_dump()
    n, t = len(codes), len(returns)
    # 变量顺序：基金权重w、损失阈值z、每一天超阈损失u、各基金调仓绝对值d。
    size, z, u, d = 2 * n + t + 1, n, n + 1, n + 1 + t
    bounds = [(0, 1)] * n + [(None, None)] + [(0, None)] * (t + n)
    equality = np.zeros((1, size)); equality[0, :n] = 1
    risk_objective = np.zeros(size); risk_objective[z] = 1; risk_objective[u:d] = 1 / (.05 * t)
    turnover_objective = np.zeros(size); turnover_objective[d:] = .5

    def optimize(active, objective, risk_ceiling=None):
        rows, rhs = [], []
        # CVaR = min_z [z + 平均超阈损失/5%]，收益乘100使求解与展示使用同一单位。
        tail = np.zeros((t, size)); tail[:, :n] = -returns * 100
        tail[:, z] = -1; tail[:, u:d] = -np.eye(t)
        rows.extend(tail); rhs.extend(np.zeros(t))
        for i in range(n):
            row = np.zeros(size); row[i] = 1; row[d + i] = -1
            rows.append(row); rhs.append(baseline[i])
            row = np.zeros(size); row[i] = -1; row[d + i] = -1
            rows.append(row); rhs.append(-baseline[i])
            if "max_single_fund_weight_pct" in active:
                row = np.zeros(size); row[i] = 1
                rows.append(row); rhs.append(limits["max_single_fund_weight_pct"] / 100)
        if "min_bond_weight_pct" in active:
            row = np.zeros(size); row[:n] = -bonds
            rows.append(row); rhs.append(-limits["min_bond_weight_pct"] / 100)
        if "max_turnover_pct" in active:
            rows.append(turnover_objective); rhs.append(limits["max_turnover_pct"] / 100)
        if risk_ceiling is not None:
            rows.append(risk_objective); rhs.append(risk_ceiling)
        result = linprog(objective, A_ub=rows, b_ub=rhs, A_eq=equality, b_eq=[1],
                         bounds=bounds, method="highs", options={"time_limit": 15})
        # 超时/数值错误不是“约束无解”，不能向用户提出错误的放宽建议。
        if result.status not in (0, 2):
            raise PortfolioStressError("求解器未完成计算，请重试；当前不能据此判断条件无解")
        return result

    active = list(limits)
    optimum = optimize(active, risk_objective)
    if optimum.status == 2:
        # 删除过滤得到一个不可约冲突子集：组内再去掉任意一条便可行。
        # 它不是全部冲突或最小放宽幅度，只用于准确说明“哪些条件不能同时满足”。
        conflict = active.copy()
        for key in active:
            trial = [item for item in conflict if item != key]
            if optimize(trial, np.zeros(size)).status == 2:
                conflict = trial
        reason = "以下条件与‘只使用当前基金、非负且合计100%’无法同时满足。"
        if n * limits["max_single_fund_weight_pct"] < 100:
            reason = f"只有{n}只基金，每只最多{limits['max_single_fund_weight_pct']:g}%，合计最多{n * limits['max_single_fund_weight_pct']:g}%，达不到100%。"
        elif not bonds.any() and limits["min_bond_weight_pct"] > 0:
            reason = "当前基金池没有可确认的债券类基金，无法达到债券最低占比。"
        return {"status": "infeasible", "reason": reason,
                "conflicts": [{"field": key, "label": LABELS[key], "value_pct": limits[key]} for key in conflict],
                "question": "你愿意修改哪一项？请新建草案确认新条件；系统没有自动放宽。"}

    # 在同等最优风险（1e-8个百分点数值容差）下，优先选择换手最小的配置。
    closest = optimize(active, turnover_objective, optimum.fun + 1e-8)
    if not closest.success:
        raise PortfolioStressError("最小换手复核未通过，请重试；未保存候选权重")
    weights = closest.x[:n]
    actual = {"min_bond_weight_pct": float(bonds @ weights * 100),
              "max_single_fund_weight_pct": float(weights.max() * 100),
              "max_turnover_pct": float(np.abs(weights - baseline).sum() * 50)}
    checks = [{"field": key, "label": LABELS[key], "limit_pct": limit, "actual_pct": actual[key],
               "passed": actual[key] >= limit - 1e-6 if key == "min_bond_weight_pct" else actual[key] <= limit + 1e-6}
              for key, limit in limits.items()]
    target = dict(zip(codes, (float(w * 100) for w in weights)))
    checked_weights(target, codes)
    if not all(item["passed"] for item in checks):
        raise PortfolioStressError("求解结果未通过独立约束复核，未生成候选方案")
    def metrics(w):
        daily = returns @ w
        return {"cvar_95_daily_pct": float(rp.CVaR_Hist(daily, alpha=.05) * 100),
                "mean_daily_return_pct": float(daily.mean() * 100)}
    original, candidate = metrics(baseline), metrics(weights)
    if abs(candidate["cvar_95_daily_pct"] - optimum.fun) > 1e-5:
        raise PortfolioStressError("Riskfolio 风险复核与求解目标不一致")
    return {"status": "solved", "target_weights": target, "checks": checks,
            "baseline": original, "candidate": candidate, "turnover_pct": actual["max_turnover_pct"],
            "risk_improved": candidate["cvar_95_daily_pct"] < original["cvar_95_daily_pct"] - 1e-6,
            "sample_count": t, "excluded_count": int((~eligible).sum()), "tail_observations": .05 * t,
            "objective": "最小化历史单日95% CVaR；同等风险下最小化单边换手",
            "solver_version": SOLVER_VERSION, "solver": "SciPy HiGHS / Riskfolio CVaR_Hist"}

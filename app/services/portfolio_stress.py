"""历史压力测试：冻结输入 -> 原组合选最差窗口 -> 同情景重演 -> 保存。

Agent 负责理解自然语言、填写参数与解释；此处只执行可复算的数学计算。
每个已完成报告不可变，比较只引用原始快照。没有行情刷新、订单或持仓写入。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
import json
from typing import Any, TypedDict
from uuid import uuid4

import numpy as np
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import Database, runtime_database
from app.models import PortfolioStressRunModel
from app.schemas import PortfolioStressRequest, PortfolioStressReport
from app.services.agent_authorization import guard_agent_portfolio
from app.services.portfolio_risk_engine import portfolio_risk_engine


ENGINE_VERSION = "historical-stress-daily-rebalanced-v1"


class PortfolioStressError(ValueError):
    """参数、数据或冻结输入不满足研究条件，不能勉强生成结果。"""


class PortfolioStressNotFound(PortfolioStressError):
    """不存在和不属于当前用户使用同一种错误，不泄露他人的记录。"""


def digest(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def checked_weights(weights: dict, codes: list[str]) -> np.ndarray:
    """比较方案必须明确给出每只基金的权重，不猜测减仓资金应该转向哪里。"""
    if set(weights) != set(codes):
        raise PortfolioStressError("候选权重必须包含冻结组合的全部基金，移除基金请填 0；本轮不支持新增基金")
    vector = np.asarray([weights[code] for code in codes], dtype=float)
    if not np.isfinite(vector).all() or (vector < 0).any() or not np.isclose(vector.sum(), 100, rtol=0, atol=0.0001):
        raise PortfolioStressError("候选权重必须非负且合计为 100%，请明确剩余资金的分配")
    return vector / 100


class StressState(TypedDict, total=False):
    frozen: dict[str, Any]
    target_weights: dict[str, float] | None
    window: dict[str, Any]
    scenarios: list[dict[str, Any]]


def select_window(state: StressState) -> dict:
    """按原组合累计收益选窗口，绝不为候选配置重新挑一段更有利的历史。"""
    frozen = state["frozen"]
    matrix = np.asarray(frozen["daily_returns"], dtype=float)
    codes, dates, days = frozen["fund_codes"], frozen["dates"], frozen["window_days"]
    if matrix.ndim != 2 or matrix.shape != (len(dates), len(codes)) or len(dates) < days:
        raise PortfolioStressError(f"共同净值样本不足，需要至少 {days} 个观察日")
    if not np.isfinite(matrix).all() or (matrix <= -1).any():
        raise PortfolioStressError("冻结收益含无效数值，不能重演")
    if len(frozen["start_dates"]) != len(dates) or len(frozen["eligible"]) != len(dates):
        raise PortfolioStressError("冻结日期与收益未对齐")
    weights = checked_weights(frozen["baseline_weights"], codes)
    returns = matrix @ weights
    # log 累积的差值就是滚动复利收益的对数，避免每个窗口重复扫描全部日期。
    prefix = np.r_[0.0, np.cumsum(np.log1p(returns))]
    losses = prefix[days:] - prefix[:-days]
    complete = np.convolve(np.asarray(frozen["eligible"], dtype=int), np.ones(days, dtype=int), "valid") == days
    if not complete.any():
        raise PortfolioStressError(f"没有连续 {days} 个完整共同观察日，缺日收益不能冒充单日，请缩短窗口或补齐数据")
    minimum = float(np.min(losses[complete]))
    first = int(np.flatnonzero(complete & np.isclose(losses, minimum, rtol=0, atol=1e-12))[0])
    # 同收益窗口的累加浮点尾差不应改变排序；极小容差内固定取最早窗口。
    window = {
        "start_index": first, "days": days, "period_start": frozen["start_dates"][first],
        "period_end": dates[first + days - 1], "search_start": frozen["start_dates"][0],
        "search_end": dates[-1], "candidate_windows": int(complete.sum()),
        "excluded_windows": int((~complete).sum()),
        "selection": "原配置累计收益最低的完整窗口；不是最大回撤窗口或未来预测",
    }
    if state.get("window") and state["window"] != window:
        raise PortfolioStressError("原情景窗口与冻结输入不一致，不能静默换期")
    return {"window": window}


def simulate(state: StressState) -> dict:
    """每日再平衡近似。贡献以每天的期初模拟净资产加权，合计等于复利收益。"""
    frozen, window = state["frozen"], state["window"]
    start, days = window["start_index"], window["days"]
    matrix = np.asarray(frozen["daily_returns"], dtype=float)[start:start + days]
    dates = [window["period_start"], *frozen["dates"][start:start + days]]
    codes = frozen["fund_codes"]
    candidates = [("baseline", "原配置", frozen["baseline_weights"])]
    if state.get("target_weights") is not None:
        candidates.append(("candidate", "候选配置", state["target_weights"]))
    scenarios = []
    for identifier, label, weights in candidates:
        vector = checked_weights(weights, codes)
        daily = matrix @ vector
        wealth = np.r_[1.0, np.cumprod(1 + daily)]
        drawdowns = 1 - wealth / np.maximum.accumulate(wealth)
        contributions = (matrix * vector * wealth[:-1, None]).sum(axis=0) * 100
        if not np.isfinite(wealth).all() or not np.isfinite(contributions).all():
            raise PortfolioStressError("收益计算溢出，请检查净值数据是否异常")
        scenarios.append({
            "scenario_id": identifier, "label": label, "weights": weights,
            "period_return_pct": round(float((wealth[-1] - 1) * 100), 6),
            "max_drawdown_pct": round(float(drawdowns.max() * 100), 6),
            "worst_daily_return_pct": round(float(daily.min() * 100), 6),
            "curve": [{"date": day, "index": round(float(value * 100), 6)} for day, value in zip(dates, wealth)],
            "contributions": [{
                "fund_code": code, "short_name": frozen["short_names"][code],
                "weight_pct": float(weights[code]), "return_contribution_pp": round(float(contributions[i]), 6),
            } for i, code in enumerate(codes)],
        })
    if len(scenarios) == 2:
        for key in ("period_return_pct", "max_drawdown_pct"):
            scenarios[1][key.replace("_pct", "_delta_pp")] = round(scenarios[1][key] - scenarios[0][key], 6)
    return {"scenarios": scenarios}


@lru_cache(maxsize=1)
def replay_graph():
    """两个纯计算节点共用一份冻结输入；图内没有模型或外部副作用。

    这里不是长任务 checkpoint 系统。完成记录由下面的 SQL 服务保存，刷新或重启后
    能读取、比较和重算；不声称可从一半算术运算处恢复。
    """
    graph = StateGraph(StressState)
    graph.add_node("select_window", select_window)
    graph.add_node("simulate", simulate)
    graph.add_edge(START, "select_window")
    graph.add_edge("select_window", "simulate")
    graph.add_edge("simulate", END)
    return graph.compile()


class PortfolioStressService:
    """SQL 只保存计算记录，组合账户保持只读；所有读取均校验用户和组合归属。"""

    def __init__(self, database: Database = runtime_database, *, risk_engine=portfolio_risk_engine):
        self.database, self.risk_engine = database, risk_engine

    def _row(self, session, *, user_id: str, portfolio_id: str, run_id: str):
        row = session.get(PortfolioStressRunModel, (user_id, run_id))
        if row is None or row.portfolio_id != portfolio_id:
            raise PortfolioStressNotFound("压力测试记录不存在或不属于当前组合")
        return row

    def _load(self, *, user_id: str, portfolio_id: str, run_id: str) -> tuple[dict, dict, str]:
        self.database.create_schema()
        with self.database.session() as session:
            row = self._row(session, user_id=user_id, portfolio_id=portfolio_id, run_id=run_id)
            root = self._row(session, user_id=user_id, portfolio_id=portfolio_id, run_id=row.root_id)
        # 历史记录可直接通过 SQL 读取，不能只信任调用方传入的组合编号。
        guard_agent_portfolio(row.user_id, row.portfolio_id)
        frozen = deepcopy(root.frozen_inputs)
        if not frozen or digest(frozen) != root.input_hash or row.input_hash != root.input_hash:
            raise PortfolioStressError("冻结输入完整性校验失败")
        if frozen.get("engine_version") != ENGINE_VERSION:
            raise PortfolioStressError("此记录的计算版本暂不支持重放，原报告仍保留在数据库")
        return deepcopy(row.report), frozen, root.id

    def get(self, *, user_id: str, portfolio_id: str, run_id: str) -> dict:
        return self._load(user_id=user_id, portfolio_id=portfolio_id, run_id=run_id)[0]

    def frozen_inputs(self, *, user_id: str, portfolio_id: str, run_id: str) -> dict:
        """供约束求解复用的只读副本；沿用归属、完整性和版本检查。"""
        return self._load(user_id=user_id, portfolio_id=portfolio_id, run_id=run_id)[1]

    def list(self, *, user_id: str, portfolio_id: str) -> list[dict]:
        guard_agent_portfolio(user_id, portfolio_id)
        self.database.create_schema()
        with self.database.session() as session:
            rows = session.scalars(select(PortfolioStressRunModel).where(
                PortfolioStressRunModel.user_id == user_id,
                PortfolioStressRunModel.portfolio_id == portfolio_id,
            ).order_by(PortfolioStressRunModel.created_at.desc(), PortfolioStressRunModel.id.desc()).limit(20)).all()
        # SQL 范围过滤后仍按落库关联复核，同一组合只检查一次。
        for owner_id, real_portfolio_id in {(row.user_id, row.portfolio_id) for row in rows}:
            guard_agent_portfolio(owner_id, real_portfolio_id)
        return [{key: row.report[key] for key in (
            "run_id", "root_run_id", "portfolio_id", "portfolio_name", "created_at", "window",
        )} for row in rows]

    def run(self, *, portfolio_id: str, request: PortfolioStressRequest) -> dict:
        """新情景读取当前本地净值；比较只读原情景。幂等重发不会新增相同记录。"""
        # 保存计算结果不是资产变更，但必须具有本次任务的研究执行能力。
        guard_agent_portfolio(request.user_id, portfolio_id, action="portfolio:research-run")
        self.database.create_schema()
        params = request.model_dump(exclude={"idempotency_key"}, mode="json")
        request_hash = digest({"portfolio_id": portfolio_id, **params})
        if request.idempotency_key:
            with self.database.session() as session:
                prior = session.scalar(select(PortfolioStressRunModel).where(
                    PortfolioStressRunModel.user_id == request.user_id,
                    PortfolioStressRunModel.idempotency_key == request.idempotency_key,
                ))
            if prior:
                guard_agent_portfolio(prior.user_id, prior.portfolio_id, action="portfolio:research-run")
                if prior.portfolio_id != portfolio_id or prior.request_hash != request_hash:
                    raise PortfolioStressError("同一请求编号不能用于不同压力测试参数")
                return deepcopy(prior.report)
        run_id = "stress_" + uuid4().hex[:16]
        source = None
        if request.source_run_id:
            source, frozen, root_id = self._load(user_id=request.user_id, portfolio_id=portfolio_id, run_id=request.source_run_id)
            if (request.months is not None and request.months != frozen["months"]) or (
                request.window_days is not None and request.window_days != frozen["window_days"]
            ):
                raise PortfolioStressError("比较必须沿用原情景区间；更换观察期或窗口请新建情景")
        else:
            started = datetime.now(timezone.utc).isoformat()
            frozen = self.risk_engine.capture_stress_inputs(user_id=request.user_id, portfolio_id=portfolio_id, months=request.months or 12)
            frozen.update(window_days=request.window_days or 20, engine_version=ENGINE_VERSION,
                          capture_started_at=started, captured_at=datetime.now(timezone.utc).isoformat())
            root_id = run_id
        result = replay_graph().invoke({"frozen": frozen, "target_weights": request.target_weights,
                                        **({"window": source["window"]} if source else {})})
        created = datetime.now(timezone.utc)
        facts = []
        for scenario in result["scenarios"]:
            for key, label in (("period_return_pct", "情景累计收益"), ("max_drawdown_pct", "情景最大回撤幅度")):
                facts.append({"evidence_id": f"fact:stress:{run_id}:{scenario['scenario_id']}:{key}",
                              "label": scenario["label"] + label, "value": scenario[key], "unit": "%",
                              "period": f"{result['window']['period_start']} 至 {result['window']['period_end']}"})
        report = PortfolioStressReport(
            run_id=run_id, root_run_id=root_id, portfolio_id=portfolio_id, portfolio_name=frozen["portfolio_name"],
            created_at=created.isoformat(), captured_at=frozen["captured_at"], input_hash=digest(frozen),
            engine_version=ENGINE_VERSION, months=frozen["months"], window=result["window"],
            holdings=[{"fund_code": code, "short_name": frozen["short_names"][code], "weight_pct": frozen["baseline_weights"][code]}
                      for code in frozen["fund_codes"]],
            scenarios=result["scenarios"], facts=facts, data_sources=frozen["data_sources"],
            stages=["读取原情景冻结输入" if source else "冻结本地持仓与复权净值", "按原配置定位压力窗口", "同情景重算配置与贡献", "保存不可变报告"],
            limitations=[
                "历史情景重演不是未来亏损预测，也不是你的真实账户收益。",
                "当前权重每日再平衡近似；不包含申购赎回费、税费或交易限制，不生成订单。",
                "最差仅针对当前可用样本和原配置，不能代表未来最坏情况；候选方案不重新选择窗口。",
                frozen["calendar_note"],
                "基金收益口径和数据日期见各来源；缺失分红资料时可能是净值近似。",
            ],
        ).model_dump(mode="json")
        row = PortfolioStressRunModel(user_id=request.user_id, id=run_id, portfolio_id=portfolio_id,
            root_id=root_id, idempotency_key=request.idempotency_key, request_hash=request_hash,
            input_hash=report["input_hash"], frozen_inputs=frozen if root_id == run_id else None,
            report=report, created_at=created)
        try:
            with self.database.session() as session, session.begin():
                session.add(row)
        except IntegrityError:
            # 唯一约束裁决并发重发；重新进入后只读已完成结果，不重复写入。
            if not request.idempotency_key:
                raise
            with self.database.session() as session:
                prior = session.scalar(select(PortfolioStressRunModel).where(
                    PortfolioStressRunModel.user_id == request.user_id,
                    PortfolioStressRunModel.idempotency_key == request.idempotency_key,
                ))
            if prior is None:
                raise PortfolioStressError("压力测试请求编号冲突")
            guard_agent_portfolio(prior.user_id, prior.portfolio_id, action="portfolio:research-run")
            if prior.portfolio_id != portfolio_id or prior.request_hash != request_hash:
                raise PortfolioStressError("压力测试请求编号冲突")
            return deepcopy(prior.report)
        return report

    def verify(self, *, user_id: str, portfolio_id: str, run_id: str) -> dict:
        """只用冻结输入复算并比较结果，不重新查询行情，也不新增记录。"""
        report, frozen, _ = self._load(user_id=user_id, portfolio_id=portfolio_id, run_id=run_id)
        candidate = next((row["weights"] for row in report["scenarios"] if row["scenario_id"] == "candidate"), None)
        replayed = replay_graph().invoke({"frozen": frozen, "target_weights": candidate, "window": report["window"]})
        return {"run_id": run_id, "consistent": replayed["scenarios"] == report["scenarios"],
                "input_hash": report["input_hash"], "engine_version": ENGINE_VERSION,
                "scope": "仅验证相同输入和版本能否复算出相同结果，不验证数据源真实性或未来表现"}


portfolio_stress_service = PortfolioStressService()

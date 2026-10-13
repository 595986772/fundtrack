"""投资决策日志与后续表现复盘服务。

决策日志不是聊天记录。创建时会把当时的组合指标、规则研判、证据 ID 和可选的
LangGraph 研究报告摘要冻结成 snapshot；列表读取时再用最新组合业绩计算“决策后到
现在/目标日”的表现。这样历史依据不会被新数据覆盖，而复盘结果又能持续更新。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select

from app.database import Database, runtime_database
from app.models import PortfolioDecisionModel
from app.services.agent_authorization import guard_agent_portfolio, guard_agent_write
from app.services.portfolio_assessment import (
    PortfolioAssessmentService,
    portfolio_assessment_service,
)
from app.services.portfolio_performance import (
    PortfolioPerformanceService,
    portfolio_performance_service,
)
from app.services.portfolios import PortfolioService, portfolio_service


DECISION_ACTIONS = {"watch", "hold", "add", "reduce", "rebalance"}
DECISION_HORIZONS = {30, 90, 180}


class PortfolioDecisionError(RuntimeError):
    """决策不存在或输入不符合明确的日志规则。"""


class PortfolioDecisionService:
    """持久化决策事实，并动态计算每条决策的后续表现。"""

    def __init__(
        self,
        database: Database = runtime_database,
        *,
        portfolios: PortfolioService = portfolio_service,
        performance: PortfolioPerformanceService = portfolio_performance_service,
        assessments: PortfolioAssessmentService = portfolio_assessment_service,
    ) -> None:
        self.database = database
        self.portfolios = portfolios
        self.performance = performance
        self.assessments = assessments
        self.database.create_schema()

    def create(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        action: str,
        thesis: str,
        horizon_days: int,
        research_job_id: str | None = None,
        research_report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """冻结当前研究上下文，并写入一条以后可复盘的决策。"""

        # 决策日志代表人工记录，不能由研究任务代替用户确认。
        guard_agent_write()
        action = str(action).strip().lower()
        if action not in DECISION_ACTIONS:
            raise PortfolioDecisionError("不支持的决策动作")
        if horizon_days not in DECISION_HORIZONS:
            raise PortfolioDecisionError("观察期只支持 30、90 或 180 天")
        thesis = str(thesis or "").strip()
        if len(thesis) < 5:
            raise PortfolioDecisionError("决策理由至少需要 5 个字符")

        portfolio = self.portfolios.get(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
        performance = self.performance.analyze(
            user_id=user_id,
            portfolio_id=portfolio_id,
            months=12,
        )
        assessment = self.assessments.assess(
            user_id=user_id,
            portfolio_id=portfolio_id,
            months=12,
        )
        today = date.today()
        now = datetime.now(timezone.utc)
        model = PortfolioDecisionModel(
            user_id=user_id,
            id=f"decision_{uuid4().hex[:16]}",
            portfolio_id=portfolio_id,
            decision_date=today,
            target_date=today + timedelta(days=horizon_days),
            action=action,
            thesis=thesis,
            horizon_days=horizon_days,
            research_job_id=research_job_id,
            snapshot=self._snapshot(
                portfolio=portfolio,
                performance=performance,
                assessment=assessment,
                research_report=research_report,
            ),
            review_note="",
            reviewed_at=None,
            created_at=now,
            updated_at=now,
        )
        with self.database.session() as session, session.begin():
            session.add(model)
        return self.get(
            user_id=user_id,
            portfolio_id=portfolio_id,
            decision_id=model.id,
        )

    def list(
        self,
        *,
        user_id: str,
        portfolio_id: str,
    ) -> list[dict[str, Any]]:
        """按决策时间倒序返回日志，并统一计算动态复盘结果。"""

        guard_agent_portfolio(user_id, portfolio_id)
        self.portfolios.get(user_id=user_id, portfolio_id=portfolio_id)
        statement = (
            select(PortfolioDecisionModel)
            .where(
                PortfolioDecisionModel.user_id == user_id,
                PortfolioDecisionModel.portfolio_id == portfolio_id,
            )
            .order_by(
                PortfolioDecisionModel.decision_date.desc(),
                PortfolioDecisionModel.created_at.desc(),
            )
        )
        with self.database.session() as session:
            models = list(session.scalars(statement))
        for owner_id, real_portfolio_id in {(model.user_id, model.portfolio_id) for model in models}:
            guard_agent_portfolio(owner_id, real_portfolio_id)
        performance = (
            self.performance.analyze(
                user_id=user_id,
                portfolio_id=portfolio_id,
                months=36,
            )
            if models
            else None
        )
        return [
            self._to_dict(model=model, performance=performance)
            for model in models
        ]

    def get(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        decision_id: str,
    ) -> dict[str, Any]:
        """读取单条决策；用户和组合 ID 都参与权限校验。"""

        self.portfolios.get(user_id=user_id, portfolio_id=portfolio_id)
        with self.database.session() as session:
            model = self._get_model(
                session,
                user_id=user_id,
                portfolio_id=portfolio_id,
                decision_id=decision_id,
            )
        guard_agent_portfolio(model.user_id, model.portfolio_id)
        performance = self.performance.analyze(
            user_id=user_id,
            portfolio_id=portfolio_id,
            months=36,
        )
        return self._to_dict(model=model, performance=performance)

    def review(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        decision_id: str,
        review_note: str,
    ) -> dict[str, Any]:
        """添加人工复盘，不修改原始理由和历史 snapshot。"""

        guard_agent_write()
        normalized_note = str(review_note or "").strip()
        if len(normalized_note) < 3:
            raise PortfolioDecisionError("复盘结论至少需要 3 个字符")
        now = datetime.now(timezone.utc)
        with self.database.session() as session, session.begin():
            model = self._get_model(
                session,
                user_id=user_id,
                portfolio_id=portfolio_id,
                decision_id=decision_id,
            )
            model.review_note = normalized_note
            model.reviewed_at = now
            model.updated_at = now
        return self.get(
            user_id=user_id,
            portfolio_id=portfolio_id,
            decision_id=decision_id,
        )

    def delete(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        decision_id: str,
    ) -> None:
        """删除一条误录日志，不影响组合和研究 Job。"""

        guard_agent_write()
        with self.database.session() as session, session.begin():
            model = self._get_model(
                session,
                user_id=user_id,
                portfolio_id=portfolio_id,
                decision_id=decision_id,
            )
            session.delete(model)

    def delete_for_portfolio(self, *, user_id: str, portfolio_id: str) -> None:
        """组合删除前清理其决策日志，保持业务数据生命周期一致。"""

        guard_agent_write()
        with self.database.session() as session, session.begin():
            session.execute(
                delete(PortfolioDecisionModel).where(
                    PortfolioDecisionModel.user_id == user_id,
                    PortfolioDecisionModel.portfolio_id == portfolio_id,
                )
            )

    @staticmethod
    def _snapshot(
        *,
        portfolio: dict[str, Any],
        performance: dict[str, Any],
        assessment: dict[str, Any],
        research_report: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """只冻结复盘真正需要的字段，避免把整份大响应重复塞进数据库。"""

        report_snapshot = None
        if research_report:
            interpretation = research_report.get("model_interpretation") or {}
            report_snapshot = {
                "headline": research_report.get("headline"),
                "summary": research_report.get("summary"),
                "conditional_view": interpretation.get("conditional_view"),
                "confidence": interpretation.get("confidence"),
                "evidence_ids": list(
                    dict.fromkeys(
                        reference.get("evidence_id")
                        for finding in research_report.get("key_findings") or []
                        for reference in finding.get("evidence_references") or []
                        if reference.get("evidence_id")
                    )
                ),
                "thread_id": research_report.get("thread_id"),
            }

        return {
            "portfolio": {
                "name": portfolio["name"],
                "position_mode": portfolio["position_mode"],
                "holdings": [
                    {
                        "fund_code": item["fund_code"],
                        "short_name": item["short_name"],
                        "weight_pct": item["weight_pct"],
                    }
                    for item in portfolio.get("holdings") or []
                ],
            },
            "performance": {
                "period_end": performance["period_end"],
                "twr_pct": performance["twr_pct"],
                "xirr_pct": performance["xirr_pct"],
                "annualized_volatility_pct": performance[
                    "annualized_volatility_pct"
                ],
                "max_drawdown_pct": performance["max_drawdown"][
                    "drawdown_pct"
                ],
                "sharpe_ratio": performance["sharpe_ratio"],
                "sortino_ratio": performance["sortino_ratio"],
                "benchmark_status": performance["benchmark"]["status"],
                "excess_return_pct": performance["benchmark"][
                    "excess_return_pct"
                ],
            },
            "assessment": {
                "assessment_date": str(assessment["assessment_date"]),
                "state_code": assessment["state_code"],
                "state_label": assessment["state_label"],
                "summary": assessment["summary"],
                "risk_evidence_ids": [
                    item["evidence_id"]
                    for item in (assessment.get("risk_evidence") or [])[:5]
                ],
                "supporting_evidence_ids": [
                    item["evidence_id"]
                    for item in (assessment.get("supporting_evidence") or [])[:5]
                ],
            },
            "research": report_snapshot,
        }

    def _to_dict(
        self,
        *,
        model: PortfolioDecisionModel,
        performance: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "id": model.id,
            "portfolio_id": model.portfolio_id,
            "decision_date": model.decision_date,
            "target_date": model.target_date,
            "action": model.action,
            "thesis": model.thesis,
            "horizon_days": model.horizon_days,
            "research_job_id": model.research_job_id,
            "snapshot": dict(model.snapshot or {}),
            "outcome": self._outcome(model=model, performance=performance),
            "review_note": model.review_note or "",
            "reviewed_at": self._as_utc(model.reviewed_at),
            "created_at": self._as_utc(model.created_at),
            "updated_at": self._as_utc(model.updated_at),
        }

    @staticmethod
    def _outcome(
        *,
        model: PortfolioDecisionModel,
        performance: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """从同一条组合指数上取决策日和观察日，避免现金流污染收益。"""

        if not performance or not performance.get("series"):
            return PortfolioDecisionService._unavailable_outcome(model)
        points = [
            {
                **point,
                "parsed_date": date.fromisoformat(str(point["date"])),
            }
            for point in performance["series"]
        ]
        baseline = next(
            (
                point
                for point in reversed(points)
                if point["parsed_date"] <= model.decision_date
            ),
            None,
        )
        if baseline is None:
            return PortfolioDecisionService._unavailable_outcome(model)

        observation_limit = min(model.target_date, points[-1]["parsed_date"])
        observed = next(
            (
                point
                for point in reversed(points)
                if baseline["parsed_date"]
                <= point["parsed_date"]
                <= observation_limit
            ),
            baseline,
        )
        portfolio_return = (
            float(observed["portfolio_index"])
            / float(baseline["portfolio_index"])
            - 1
        ) * 100
        baseline_benchmark = baseline.get("benchmark_index")
        observed_benchmark = observed.get("benchmark_index")
        benchmark_return = (
            (
                float(observed_benchmark) / float(baseline_benchmark) - 1
            )
            * 100
            if baseline_benchmark and observed_benchmark
            else None
        )
        elapsed_days = max(
            (observed["parsed_date"] - model.decision_date).days,
            0,
        )
        if model.reviewed_at is not None:
            status = "reviewed"
            note = "已记录人工复盘结论。"
        elif observed["parsed_date"] >= model.target_date:
            status = "review_ready"
            note = "观察期已结束，可以结合原始证据写复盘。"
        else:
            status = "tracking"
            note = "观察期进行中，结果会随最新净值更新。"
        return {
            "status": status,
            "as_of_date": observed["parsed_date"],
            "elapsed_days": elapsed_days,
            "target_days": model.horizon_days,
            "portfolio_return_pct": round(portfolio_return, 2),
            "benchmark_return_pct": (
                round(benchmark_return, 2)
                if benchmark_return is not None
                else None
            ),
            "excess_return_pct": (
                round(portfolio_return - benchmark_return, 2)
                if benchmark_return is not None
                else None
            ),
            "note": note,
        }

    @staticmethod
    def _unavailable_outcome(model: PortfolioDecisionModel) -> dict[str, Any]:
        return {
            "status": "reviewed" if model.reviewed_at else "unavailable",
            "as_of_date": None,
            "elapsed_days": 0,
            "target_days": model.horizon_days,
            "portfolio_return_pct": None,
            "benchmark_return_pct": None,
            "excess_return_pct": None,
            "note": "现有组合收益序列尚未覆盖决策日期。",
        }

    @staticmethod
    def _get_model(
        session,
        *,
        user_id: str,
        portfolio_id: str,
        decision_id: str,
    ) -> PortfolioDecisionModel:
        model = session.get(
            PortfolioDecisionModel,
            {"user_id": user_id, "id": decision_id},
        )
        if model is None or model.portfolio_id != portfolio_id:
            raise PortfolioDecisionError(f"决策日志不存在：{decision_id}")
        return model

    @staticmethod
    def _as_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


portfolio_decision_service = PortfolioDecisionService()

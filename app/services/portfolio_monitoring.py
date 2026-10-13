"""组合持续监控：保存策略、快照，并用确定性规则产生预警。

这里是整个闭环的判断中心：

1. 读取组合确定性 Assessment 和 Riskfolio 量化风险；
2. 计算市场事件实际影响了多少组合持仓；
3. 保存当前快照；
4. 与上一次快照比较风险状态、回撤、集中度和持仓事件影响；
5. 只有 Python 规则命中后，才让模型把变化解释成通俗中文；
6. 预警只提供研究入口，不会执行交易。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import re
from typing import Any, Callable, Protocol
from uuid import uuid4

from sqlalchemy import delete, select

from app.database import Database, runtime_database
from app.models import (
    PortfolioAlertModel,
    PortfolioMonitoringPolicyModel,
    PortfolioRiskSnapshotModel,
)
from app.services.portfolio_assessment import portfolio_assessment_service
from app.services.portfolio_monitoring_explanation import (
    portfolio_monitoring_explanation_service,
)
from app.services.portfolio_event_impacts import portfolio_event_impact_service
from app.services.portfolio_risk_engine import portfolio_risk_engine
from app.services.portfolios import portfolio_service


PortfolioMonitorProgress = Callable[[str, str], None]
PORTFOLIO_ID_PATTERN = re.compile(r"^portfolio_[a-f0-9]{16}$")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$")
RISK_PREFERENCES = {"conservative", "balanced", "growth"}
INVESTMENT_HORIZONS = {"under_1y", "one_to_three_years", "over_3y"}
SUPPORTED_MONTHS = {6, 12, 36}
ALERT_STATUSES = {"open", "acknowledged"}
RISK_STATE_RANK = {
    "supportive": 0,
    "mixed": 1,
    "cautious": 2,
    "high_attention": 3,
}


class PortfolioMonitoringError(RuntimeError):
    """监控策略、快照或预警无法完成当前操作。"""


class AssessmentReader(Protocol):
    def assess(self, *, user_id: str, portfolio_id: str, months: int) -> dict: ...


class RiskReader(Protocol):
    def analyze(self, *, user_id: str, portfolio_id: str, months: int) -> dict: ...


class EventImpactReader(Protocol):
    def analyze(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        limit: int,
    ) -> dict[str, Any]: ...


class ExplanationReader(Protocol):
    def explain(
        self,
        *,
        rule_alerts: list[dict[str, Any]],
        snapshot: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]: ...

    def skipped(self) -> dict[str, Any]: ...


class PortfolioMonitoringService:
    """组合监控的应用服务，同时承担 SQL Repository 的小型教学实现。"""

    DEFAULT_CRON = "0 21 * * 1-5"

    def __init__(
        self,
        *,
        database: Database = runtime_database,
        portfolios: Any = portfolio_service,
        assessments: AssessmentReader = portfolio_assessment_service,
        risks: RiskReader = portfolio_risk_engine,
        event_impacts: EventImpactReader = portfolio_event_impact_service,
        explainer: ExplanationReader = portfolio_monitoring_explanation_service,
    ) -> None:
        self.database = database
        self.portfolios = portfolios
        self.assessments = assessments
        self.risks = risks
        self.event_impacts = event_impacts
        self.explainer = explainer
        self.database.create_schema()

    def get_overview(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        max_alerts: int = 20,
    ) -> dict[str, Any]:
        """返回前端监控面板需要的策略、最近快照和预警。"""

        user_id, portfolio_id = self._normalize_key(user_id, portfolio_id)
        self.portfolios.get(user_id=user_id, portfolio_id=portfolio_id)
        policy = self.get_policy(user_id=user_id, portfolio_id=portfolio_id)
        snapshots = self.list_snapshots(
            user_id=user_id,
            portfolio_id=portfolio_id,
            max_results=2,
        )
        alerts = self.list_alerts(
            user_id=user_id,
            portfolio_id=portfolio_id,
            max_results=max_alerts,
        )
        return {
            "portfolio_id": portfolio_id,
            "policy": policy,
            "latest_snapshot": snapshots[0] if snapshots else None,
            "previous_snapshot": snapshots[1] if len(snapshots) > 1 else None,
            "alerts": alerts,
            "open_alert_count": sum(
                item["status"] == "open" for item in alerts
            ),
        }

    def get_policy(self, *, user_id: str, portfolio_id: str) -> dict[str, Any]:
        user_id, portfolio_id = self._normalize_key(user_id, portfolio_id)
        with self.database.session() as session:
            model = session.get(
                PortfolioMonitoringPolicyModel,
                (user_id, portfolio_id),
            )
            if model is None:
                return self._default_policy(
                    user_id=user_id,
                    portfolio_id=portfolio_id,
                )
            return self._policy_to_dict(model, persisted=True)

    def upsert_policy(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        enabled: bool,
        risk_preference: str,
        investment_horizon: str,
        monitor_months: int,
        max_single_adjustment_pct: float,
        drawdown_alert_pct_points: float,
        concentration_alert_pct_points: float,
        major_event_heat_threshold: float,
        alert_on_risk_state_worsening: bool,
        schedule_cron: str,
        event_impact_exposure_threshold_pct: float = 5.0,
    ) -> dict[str, Any]:
        """保存用户能看见、能修改的监控策略，不在这里猜隐含阈值。"""

        user_id, portfolio_id = self._normalize_key(user_id, portfolio_id)
        self.portfolios.get(user_id=user_id, portfolio_id=portfolio_id)
        values = self._normalize_policy_values(
            enabled=enabled,
            risk_preference=risk_preference,
            investment_horizon=investment_horizon,
            monitor_months=monitor_months,
            max_single_adjustment_pct=max_single_adjustment_pct,
            drawdown_alert_pct_points=drawdown_alert_pct_points,
            concentration_alert_pct_points=concentration_alert_pct_points,
            major_event_heat_threshold=major_event_heat_threshold,
            event_impact_exposure_threshold_pct=(
                event_impact_exposure_threshold_pct
            ),
            alert_on_risk_state_worsening=alert_on_risk_state_worsening,
            schedule_cron=schedule_cron,
        )
        now = datetime.now(timezone.utc)
        schedule_id = self._schedule_id(portfolio_id)

        with self.database.session() as session, session.begin():
            model = session.get(
                PortfolioMonitoringPolicyModel,
                (user_id, portfolio_id),
            )
            if model is None:
                model = PortfolioMonitoringPolicyModel(
                    user_id=user_id,
                    portfolio_id=portfolio_id,
                    schedule_id=schedule_id,
                    created_at=now,
                    updated_at=now,
                    **values,
                )
                session.add(model)
            else:
                for key, value in values.items():
                    setattr(model, key, value)
                model.schedule_id = schedule_id
                model.updated_at = now

        return self.get_policy(user_id=user_id, portfolio_id=portfolio_id)

    def run_monitor(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        run_id: str,
        event_impact_report: dict[str, Any] | None = None,
        progress_callback: PortfolioMonitorProgress | None = None,
    ) -> dict[str, Any]:
        """建立或比较一次快照；相同 run_id 重试时直接返回已保存结果。"""

        user_id, portfolio_id = self._normalize_key(user_id, portfolio_id)
        run_id = self._normalize_run_id(run_id)
        existing = self._snapshot_by_run(user_id=user_id, run_id=run_id)
        if existing is not None:
            return self._result_from_saved_snapshot(existing)

        portfolio = self.portfolios.get(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
        policy = self.get_policy(user_id=user_id, portfolio_id=portfolio_id)
        months = int(policy["monitor_months"])

        assessment = self.assessments.assess(
            user_id=user_id,
            portfolio_id=portfolio_id,
            months=months,
        )
        self._notify(progress_callback, "assessment", "组合规则证据已更新")

        quant_risk = self.risks.analyze(
            user_id=user_id,
            portfolio_id=portfolio_id,
            months=months,
        )
        self._notify(progress_callback, "quant_risk", "组合量化风险已更新")

        # 归因服务只返回可验证的证券、行业或基金关联。这里保存结果供下一次
        # 快照比较，大模型不参与“是否达到预警阈值”的判断。
        if event_impact_report is None:
            event_impact_report = self.event_impacts.analyze(
                user_id=user_id,
                portfolio_id=portfolio_id,
                limit=30,
            )
        self._notify(progress_callback, "event_impact", "持仓事件影响已更新")

        previous = self._latest_snapshot(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
        now = datetime.now(timezone.utc)
        current = self._build_snapshot(
            snapshot_id=f"snapshot_{uuid4().hex[:16]}",
            run_id=run_id,
            portfolio=portfolio,
            assessment=assessment,
            quant_risk=quant_risk,
            event_impact_report=event_impact_report,
            policy=policy,
            observed_at=now,
        )
        rule_alerts = (
            []
            if previous is None
            else self._compare_snapshots(
                previous=self._snapshot_to_dict(previous),
                current=current,
                policy=policy,
            )
        )
        explanation = (
            self.explainer.explain(
                rule_alerts=rule_alerts,
                snapshot=current,
                policy=policy,
            )
            if rule_alerts
            else self.explainer.skipped()
        )
        comparison = {
            "baseline_created": previous is None,
            "previous_snapshot_id": previous.id if previous is not None else None,
            "change_count": len(rule_alerts),
            "changes": rule_alerts,
        }
        current["comparison"] = comparison
        current["generation"] = explanation

        alert_models = [
            self._alert_model(
                user_id=user_id,
                portfolio_id=portfolio_id,
                snapshot_id=current["id"],
                rule_alert=rule_alert,
                explanation=explanation,
                created_at=now,
            )
            for rule_alert in rule_alerts
        ]
        with self.database.session() as session, session.begin():
            session.add(self._snapshot_model(user_id=user_id, data=current))
            session.add_all(alert_models)
        self._notify(progress_callback, "persist", "风险快照与预警已持久化")

        alerts = [self._alert_to_dict(model) for model in alert_models]
        return self._build_run_result(
            portfolio=portfolio,
            policy=policy,
            snapshot=current,
            alerts=alerts,
        )

    def list_snapshots(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        user_id, portfolio_id = self._normalize_key(user_id, portfolio_id)
        limit = max(1, min(int(max_results), 100))
        statement = (
            select(PortfolioRiskSnapshotModel)
            .where(
                PortfolioRiskSnapshotModel.user_id == user_id,
                PortfolioRiskSnapshotModel.portfolio_id == portfolio_id,
            )
            .order_by(PortfolioRiskSnapshotModel.observed_at.desc())
            .limit(limit)
        )
        with self.database.session() as session:
            return [
                self._snapshot_to_dict(model)
                for model in session.scalars(statement).all()
            ]

    def list_alerts(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        status: str | None = None,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        user_id, portfolio_id = self._normalize_key(user_id, portfolio_id)
        if status is not None and status not in ALERT_STATUSES:
            raise ValueError("预警状态只能是 open 或 acknowledged。")
        limit = max(1, min(int(max_results), 100))
        statement = select(PortfolioAlertModel).where(
            PortfolioAlertModel.user_id == user_id,
            PortfolioAlertModel.portfolio_id == portfolio_id,
        )
        if status is not None:
            statement = statement.where(PortfolioAlertModel.status == status)
        statement = statement.order_by(PortfolioAlertModel.created_at.desc()).limit(
            limit
        )
        with self.database.session() as session:
            return [
                self._alert_to_dict(model)
                for model in session.scalars(statement).all()
            ]

    def acknowledge_alert(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        alert_id: str,
    ) -> dict[str, Any]:
        user_id, portfolio_id = self._normalize_key(user_id, portfolio_id)
        alert_id = self._normalize_alert_id(alert_id)
        with self.database.session() as session, session.begin():
            model = session.get(PortfolioAlertModel, (user_id, alert_id))
            if model is None or model.portfolio_id != portfolio_id:
                raise KeyError(f"找不到组合预警：{alert_id}")
            if model.status == "open":
                model.status = "acknowledged"
                model.acknowledged_at = datetime.now(timezone.utc)
        return self._get_alert(user_id=user_id, alert_id=alert_id)

    def sync_schedule(self, *, policy: dict[str, Any], scheduler: Any) -> None:
        """让数据库策略与原有 Cron Scheduler 中的一条定义保持一致。"""

        schedules = scheduler.list_schedules(
            user_id=policy["user_id"],
            max_results=100,
        )["schedules"]
        existing = next(
            (item for item in schedules if item["id"] == policy["schedule_id"]),
            None,
        )
        expected_payload = {
            "portfolio_id": policy["portfolio_id"],
            "portfolio_months": policy["monitor_months"],
        }
        matches = bool(
            existing
            and existing["cron"] == policy["schedule_cron"]
            and existing["job_type"] == "portfolio_monitor"
            and existing.get("job_payload") == expected_payload
        )

        if existing is not None and (not policy["enabled"] or not matches):
            scheduler.cancel_schedule(
                user_id=policy["user_id"],
                schedule_id=policy["schedule_id"],
            )
            existing = None
        if policy["enabled"] and existing is None:
            portfolio = self.portfolios.get(
                user_id=policy["user_id"],
                portfolio_id=policy["portfolio_id"],
            )
            scheduler.create_schedule(
                user_id=policy["user_id"],
                schedule_id=policy["schedule_id"],
                cron=policy["schedule_cron"],
                prompt=f"监控组合：{portfolio['name']}",
                job_type="portfolio_monitor",
                job_payload=expected_payload,
                recurring=True,
                durable=True,
            )

    def ensure_all_schedules(self, scheduler: Any) -> list[str]:
        """应用启动时按数据库恢复所有已启用组合的 Cron 定义。"""

        errors: list[str] = []
        policies = self.list_enabled_policies()
        for policy in policies:
            try:
                self.sync_schedule(policy=policy, scheduler=scheduler)
            except Exception as exc:
                errors.append(f"{policy['portfolio_id']}: {exc}")
        return errors

    def list_enabled_policies(
        self,
        *,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """列出流水线可运行的组合；传入用户时严格限制在该用户范围内。"""

        statement = select(PortfolioMonitoringPolicyModel).where(
            PortfolioMonitoringPolicyModel.enabled.is_(True)
        )
        if user_id is not None:
            normalized_user = str(user_id or "").strip()
            if not USER_ID_PATTERN.fullmatch(normalized_user):
                raise ValueError("user_id 格式非法。")
            statement = statement.where(
                PortfolioMonitoringPolicyModel.user_id == normalized_user
            )
        statement = statement.order_by(
            PortfolioMonitoringPolicyModel.user_id,
            PortfolioMonitoringPolicyModel.portfolio_id,
        )
        with self.database.session() as session:
            return [
                self._policy_to_dict(model, persisted=True)
                for model in session.scalars(statement).all()
            ]

    def delete_monitoring_data(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        scheduler: Any | None = None,
    ) -> None:
        """删除组合时同步清理策略、快照、预警和对应 Cron。"""

        user_id, portfolio_id = self._normalize_key(user_id, portfolio_id)
        policy = self.get_policy(user_id=user_id, portfolio_id=portfolio_id)
        if scheduler is not None:
            schedules = scheduler.list_schedules(user_id=user_id)["schedules"]
            if any(item["id"] == policy["schedule_id"] for item in schedules):
                scheduler.cancel_schedule(
                    user_id=user_id,
                    schedule_id=policy["schedule_id"],
                )
        with self.database.session() as session, session.begin():
            session.execute(
                delete(PortfolioAlertModel).where(
                    PortfolioAlertModel.user_id == user_id,
                    PortfolioAlertModel.portfolio_id == portfolio_id,
                )
            )
            session.execute(
                delete(PortfolioRiskSnapshotModel).where(
                    PortfolioRiskSnapshotModel.user_id == user_id,
                    PortfolioRiskSnapshotModel.portfolio_id == portfolio_id,
                )
            )
            session.execute(
                delete(PortfolioMonitoringPolicyModel).where(
                    PortfolioMonitoringPolicyModel.user_id == user_id,
                    PortfolioMonitoringPolicyModel.portfolio_id == portfolio_id,
                )
            )

    def _compare_snapshots(
        self,
        *,
        previous: dict[str, Any],
        current: dict[str, Any],
        policy: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """唯一的预警触发入口：全部条件都是可复现的 Python 比较。"""

        changes: list[dict[str, Any]] = []
        previous_rank = RISK_STATE_RANK.get(previous["state_code"], 1)
        current_rank = RISK_STATE_RANK.get(current["state_code"], 1)
        if policy["alert_on_risk_state_worsening"] and current_rank > previous_rank:
            changes.append(
                {
                    "alert_type": "risk_state_worsened",
                    "severity": "high" if current_rank >= 3 else "medium",
                    "title": "组合风险状态上升",
                    "detail": (
                        f"组合状态由“{previous['state_label']}”变为"
                        f"“{current['state_label']}”。"
                    ),
                    "metric_data": {
                        "previous_state_code": previous["state_code"],
                        "current_state_code": current["state_code"],
                    },
                }
            )

        drawdown_delta = round(
            current["max_drawdown_pct"] - previous["max_drawdown_pct"],
            2,
        )
        drawdown_limit = float(policy["drawdown_alert_pct_points"])
        if drawdown_delta >= drawdown_limit:
            changes.append(
                {
                    "alert_type": "drawdown_expanded",
                    "severity": "high" if drawdown_delta >= drawdown_limit * 2 else "medium",
                    "title": "组合最大回撤扩大",
                    "detail": (
                        f"最大回撤由 {previous['max_drawdown_pct']:.2f}% 扩大至 "
                        f"{current['max_drawdown_pct']:.2f}%，增加 {drawdown_delta:.2f} 个百分点。"
                    ),
                    "metric_data": {
                        "previous_pct": previous["max_drawdown_pct"],
                        "current_pct": current["max_drawdown_pct"],
                        "change_pct_points": drawdown_delta,
                        "threshold_pct_points": drawdown_limit,
                    },
                }
            )

        concentration_delta = round(
            current["max_weight_pct"] - previous["max_weight_pct"],
            2,
        )
        concentration_limit = float(policy["concentration_alert_pct_points"])
        if concentration_delta >= concentration_limit:
            changes.append(
                {
                    "alert_type": "concentration_increased",
                    "severity": "medium",
                    "title": "组合集中度上升",
                    "detail": (
                        f"最大单只权重由 {previous['max_weight_pct']:.2f}% 上升至 "
                        f"{current['max_weight_pct']:.2f}%。"
                    ),
                    "metric_data": {
                        "previous_pct": previous["max_weight_pct"],
                        "current_pct": current["max_weight_pct"],
                        "change_pct_points": concentration_delta,
                        "threshold_pct_points": concentration_limit,
                    },
                }
            )

        # 持仓级事件预警比“基金可能相关”更精确：只有风险方向事件确实影响了
        # 足够多的真实持仓，或者同一事件的影响明显增强时，才会进入预警列表。
        event_impact_alert = self._compare_event_impacts(
            previous=previous,
            current=current,
            policy=policy,
        )
        if event_impact_alert is not None:
            changes.append(event_impact_alert)

        # 同一事件已经由持仓归因生成精确预警时，不再额外生成泛化的重大事件预警。
        covered_event_keys = {
            str(item.get("cluster_id") or item.get("event_id"))
            for item in current.get("event_impacts", [])
            if item.get("cluster_id") or item.get("event_id")
        }
        previous_event_ids = {
            str(item["event_key"]) for item in previous.get("major_events", [])
        }
        new_events = [
            item
            for item in current.get("major_events", [])
            if str(item["event_key"]) not in previous_event_ids
            and str(item["event_key"]) not in covered_event_keys
        ]
        if new_events:
            highest_heat = max(float(item["heat_score"]) for item in new_events)
            changes.append(
                {
                    "alert_type": "new_major_risk_event",
                    "severity": "high" if highest_heat >= 85 else "medium",
                    "title": f"出现 {len(new_events)} 条新的重大风险事件",
                    "detail": "；".join(item["title"] for item in new_events[:3]),
                    "metric_data": {
                        "event_count": len(new_events),
                        "highest_heat_score": highest_heat,
                        "threshold": policy["major_event_heat_threshold"],
                        "events": new_events,
                    },
                }
            )
        return changes

    def _build_snapshot(
        self,
        *,
        snapshot_id: str,
        run_id: str,
        portfolio: dict[str, Any],
        assessment: dict[str, Any],
        quant_risk: dict[str, Any],
        event_impact_report: dict[str, Any],
        policy: dict[str, Any],
        observed_at: datetime,
    ) -> dict[str, Any]:
        concentration = assessment["concentration"]
        scores = assessment["score_breakdown"]
        return {
            "id": snapshot_id,
            "run_id": run_id,
            "portfolio_id": portfolio["id"],
            "portfolio_name": portfolio["name"],
            "period_months": int(policy["monitor_months"]),
            "assessment_date": str(assessment["assessment_date"]),
            "state_code": str(assessment["state_code"]),
            "state_label": str(assessment["state_label"]),
            "caution_points": float(scores["caution_points"]),
            "supportive_points": float(scores["supportive_points"]),
            "annualized_volatility_pct": float(
                quant_risk["annualized_volatility_pct"]
            ),
            "max_drawdown_pct": float(
                quant_risk["max_drawdown"]["drawdown_pct"]
            ),
            "daily_cvar_95_pct": float(quant_risk["daily_cvar_95_pct"]),
            "concentration_hhi": float(concentration["hhi"]),
            "max_weight_pct": float(concentration["max_weight_pct"]),
            "major_events": self._major_events(
                assessment=assessment,
                heat_threshold=float(policy["major_event_heat_threshold"]),
            ),
            "event_impacts": self._snapshot_event_impacts(event_impact_report),
            "source_summary": {
                "assessment_summary": assessment["summary"],
                "risk_period_start": quant_risk["period_start"],
                "risk_period_end": quant_risk["period_end"],
                "top_risk_contributor": quant_risk.get("top_risk_contributor"),
                "event_count": assessment.get("event_count", 0),
                "active_event_count": assessment.get("active_event_count", 0),
                # 版本字段用于识别升级前的历史快照，避免首次部署时把所有旧事件
                # 都误报为“新增持仓影响”。
                "event_impact_snapshot_version": 1,
                "event_impact_summary": event_impact_report.get("summary", ""),
                "event_impact_quality": event_impact_report.get("quality", {}),
                "event_data_cutoff": event_impact_report.get("event_data_cutoff"),
            },
            "comparison": {},
            "generation": {},
            "observed_at": observed_at.isoformat(),
        }

    @staticmethod
    def _compare_event_impacts(
        *,
        previous: dict[str, Any],
        current: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any] | None:
        """比较两次持仓事件归因，聚合成至多一条可解释预警。"""

        # 旧版快照没有事件归因基线。升级后的第一次运行只建立基线，第二次起
        # 才判断新增或增强，避免把数据库中的全部历史事件一次性推给用户。
        previous_version = previous.get("source_summary", {}).get(
            "event_impact_snapshot_version"
        )
        if previous_version != 1:
            return None

        threshold = float(policy["event_impact_exposure_threshold_pct"])
        previous_by_id = {
            str(item["impact_evidence_id"]): item
            for item in previous.get("event_impacts", [])
            if item.get("impact_evidence_id")
        }
        relevant_changes: list[dict[str, Any]] = []

        for item in current.get("event_impacts", []):
            # 支撑和中性信息仍保存在快照中供界面查看，但不会触发风险预警。
            if item.get("direction") != "caution":
                continue
            current_exposure = float(item.get("affected_exposure_pct") or 0)
            if current_exposure < threshold:
                continue

            previous_item = previous_by_id.get(str(item["impact_evidence_id"]))
            change_type: str | None = None
            exposure_change = current_exposure
            if previous_item is None:
                change_type = "new_event"
            else:
                previous_exposure = float(
                    previous_item.get("affected_exposure_pct") or 0
                )
                exposure_change = round(current_exposure - previous_exposure, 4)
                if previous_item.get("direction") != "caution":
                    change_type = "turned_caution"
                elif previous_exposure < threshold <= current_exposure:
                    change_type = "threshold_crossed"
                elif int(item.get("severity") or 0) > int(
                    previous_item.get("severity") or 0
                ):
                    change_type = "severity_increased"
                elif exposure_change >= threshold:
                    change_type = "exposure_increased"

            if change_type is None:
                continue
            event_key = str(item.get("cluster_id") or item.get("event_id") or "")
            relevant_changes.append(
                {
                    **item,
                    "change_type": change_type,
                    "exposure_change_pct_points": round(exposure_change, 4),
                    "event_key": event_key,
                }
            )

        if not relevant_changes:
            return None

        relevant_changes.sort(
            key=lambda item: (
                float(item.get("priority_score") or 0),
                float(item.get("affected_exposure_pct") or 0),
            ),
            reverse=True,
        )
        top = relevant_changes[0]
        high_attention = any(
            int(item.get("severity") or 0) >= 3
            or float(item.get("affected_exposure_pct") or 0) >= 30
            for item in relevant_changes
        )
        detail_parts = []
        for item in relevant_changes[:3]:
            change_note = {
                "turned_caution": "，影响方向转为风险关注",
                "threshold_crossed": f"，首次超过 {threshold:.2f}% 预警阈值",
                "severity_increased": f"，严重度升至 {item['severity']} 级",
                "exposure_increased": (
                    f"，较上次增加 {item['exposure_change_pct_points']:.2f} 个百分点"
                ),
            }.get(item["change_type"], "")
            detail_parts.append(
                f"“{item['title']}”影响组合 {item['affected_exposure_pct']:.2f}%"
                f"（{item['match_label']}）{change_note}"
            )
        return {
            "alert_type": "portfolio_event_exposure_changed",
            "severity": "high" if high_attention else "medium",
            "title": (
                (
                    f"新的风险事件影响 {top['affected_exposure_pct']:.2f}% 持仓"
                    if top["change_type"] == "new_event"
                    else f"持仓事件风险影响升至 {top['affected_exposure_pct']:.2f}%"
                )
                if len(relevant_changes) == 1
                else f"{len(relevant_changes)} 条风险事件影响当前持仓"
            ),
            "detail": "；".join(detail_parts),
            "metric_data": {
                "threshold_pct": threshold,
                "event_count": len(relevant_changes),
                "highest_affected_exposure_pct": max(
                    float(item["affected_exposure_pct"])
                    for item in relevant_changes
                ),
                "event_keys": [
                    item["event_key"] for item in relevant_changes if item["event_key"]
                ],
                "events": relevant_changes,
            },
        }

    @staticmethod
    def _snapshot_event_impacts(
        report: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """只保存下一轮比较和前端解释需要的字段，避免快照无限膨胀。"""

        fields = (
            "impact_evidence_id",
            "event_id",
            "cluster_id",
            "title",
            "event_date",
            "direction",
            "severity",
            "confidence",
            "freshness_status",
            "affected_exposure_pct",
            "priority_score",
            "match_basis",
            "match_label",
            "source_name",
            "source_url",
            "matched_securities",
            "matched_sectors",
            "matched_industries",
            "matched_funds",
        )
        return [
            {field: item.get(field) for field in fields}
            for item in report.get("impacts", [])
            if item.get("impact_evidence_id")
        ]

    @staticmethod
    def _major_events(
        *,
        assessment: dict[str, Any],
        heat_threshold: float,
    ) -> list[dict[str, Any]]:
        events = []
        for item in assessment.get("risk_evidence", []):
            heat = item.get("heat_score")
            if item.get("kind") != "market_event" or heat is None:
                continue
            if float(item.get("decay_factor", 1.0)) <= 0:
                continue
            if float(heat) < heat_threshold:
                continue
            events.append(
                {
                    "event_key": str(
                        item.get("cluster_id")
                        or item.get("event_id")
                        or item["evidence_id"]
                    ),
                    "evidence_id": str(item["evidence_id"]),
                    "title": str(item["title"]),
                    "heat_score": float(heat),
                    "data_date": str(item["data_date"]),
                    "source_name": str(item["source_name"]),
                    "source_url": item.get("source_url"),
                    "affected_fund_codes": list(
                        item.get("affected_fund_codes", [])
                    ),
                }
            )
        return sorted(events, key=lambda item: item["heat_score"], reverse=True)

    def _build_run_result(
        self,
        *,
        portfolio: dict[str, Any],
        policy: dict[str, Any],
        snapshot: dict[str, Any],
        alerts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        comparison = snapshot["comparison"]
        status = (
            "baseline_created"
            if comparison["baseline_created"]
            else "alerts_created"
            if alerts
            else "no_material_change"
        )
        summary = (
            "首份风险基线已保存，后续运行会与本次快照比较。"
            if status == "baseline_created"
            else f"发现 {len(alerts)} 类达到用户阈值的风险变化。"
            if alerts
            else "本次未发现达到用户阈值的显著变化。"
        )
        return {
            "data_kind": "portfolio_monitoring_run",
            "workflow": "portfolio_monitor",
            "status": status,
            "run_id": snapshot["run_id"],
            "portfolio_id": portfolio["id"],
            "portfolio_name": portfolio["name"],
            "summary": summary,
            "policy": policy,
            "snapshot": snapshot,
            "alerts": alerts,
            "alert_count": len(alerts),
            "generation": snapshot["generation"],
            "execution": {
                "mode": "monitoring_only",
                "research_job_created": False,
                "orders_created": 0,
                "auto_trade": False,
            },
        }

    def _result_from_saved_snapshot(
        self,
        snapshot: PortfolioRiskSnapshotModel,
    ) -> dict[str, Any]:
        portfolio = self.portfolios.get(
            user_id=snapshot.user_id,
            portfolio_id=snapshot.portfolio_id,
        )
        policy = self.get_policy(
            user_id=snapshot.user_id,
            portfolio_id=snapshot.portfolio_id,
        )
        current = self._snapshot_to_dict(snapshot)
        alerts = self._alerts_for_snapshot(
            user_id=snapshot.user_id,
            snapshot_id=snapshot.id,
        )
        return self._build_run_result(
            portfolio=portfolio,
            policy=policy,
            snapshot=current,
            alerts=alerts,
        )

    def _snapshot_by_run(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> PortfolioRiskSnapshotModel | None:
        statement = select(PortfolioRiskSnapshotModel).where(
            PortfolioRiskSnapshotModel.user_id == user_id,
            PortfolioRiskSnapshotModel.run_id == run_id,
        )
        with self.database.session() as session:
            return session.scalar(statement)

    def _latest_snapshot(
        self,
        *,
        user_id: str,
        portfolio_id: str,
    ) -> PortfolioRiskSnapshotModel | None:
        statement = (
            select(PortfolioRiskSnapshotModel)
            .where(
                PortfolioRiskSnapshotModel.user_id == user_id,
                PortfolioRiskSnapshotModel.portfolio_id == portfolio_id,
            )
            .order_by(PortfolioRiskSnapshotModel.observed_at.desc())
            .limit(1)
        )
        with self.database.session() as session:
            return session.scalar(statement)

    def _alerts_for_snapshot(
        self,
        *,
        user_id: str,
        snapshot_id: str,
    ) -> list[dict[str, Any]]:
        statement = select(PortfolioAlertModel).where(
            PortfolioAlertModel.user_id == user_id,
            PortfolioAlertModel.snapshot_id == snapshot_id,
        )
        with self.database.session() as session:
            return [
                self._alert_to_dict(model)
                for model in session.scalars(statement).all()
            ]

    def _get_alert(self, *, user_id: str, alert_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            model = session.get(PortfolioAlertModel, (user_id, alert_id))
            if model is None:
                raise KeyError(f"找不到组合预警：{alert_id}")
            return self._alert_to_dict(model)

    @staticmethod
    def _snapshot_model(
        *,
        user_id: str,
        data: dict[str, Any],
    ) -> PortfolioRiskSnapshotModel:
        return PortfolioRiskSnapshotModel(
            user_id=user_id,
            id=data["id"],
            portfolio_id=data["portfolio_id"],
            run_id=data["run_id"],
            period_months=data["period_months"],
            assessment_date=date.fromisoformat(data["assessment_date"]),
            state_code=data["state_code"],
            state_label=data["state_label"],
            caution_points=data["caution_points"],
            supportive_points=data["supportive_points"],
            annualized_volatility_pct=data["annualized_volatility_pct"],
            max_drawdown_pct=data["max_drawdown_pct"],
            daily_cvar_95_pct=data["daily_cvar_95_pct"],
            concentration_hhi=data["concentration_hhi"],
            max_weight_pct=data["max_weight_pct"],
            major_events=data["major_events"],
            event_impacts=data["event_impacts"],
            source_summary=data["source_summary"],
            comparison=data["comparison"],
            generation=data["generation"],
            observed_at=datetime.fromisoformat(data["observed_at"]),
        )

    @staticmethod
    def _alert_model(
        *,
        user_id: str,
        portfolio_id: str,
        snapshot_id: str,
        rule_alert: dict[str, Any],
        explanation: dict[str, Any],
        created_at: datetime,
    ) -> PortfolioAlertModel:
        return PortfolioAlertModel(
            user_id=user_id,
            id=f"alert_{uuid4().hex[:16]}",
            portfolio_id=portfolio_id,
            snapshot_id=snapshot_id,
            alert_type=rule_alert["alert_type"],
            severity=rule_alert["severity"],
            title=rule_alert["title"],
            detail=rule_alert["detail"],
            metric_data=rule_alert["metric_data"],
            explanation=explanation,
            status="open",
            created_at=created_at,
            acknowledged_at=None,
        )

    @staticmethod
    def _policy_to_dict(
        model: PortfolioMonitoringPolicyModel,
        *,
        persisted: bool,
    ) -> dict[str, Any]:
        return {
            "user_id": model.user_id,
            "portfolio_id": model.portfolio_id,
            "enabled": model.enabled,
            "risk_preference": model.risk_preference,
            "investment_horizon": model.investment_horizon,
            "monitor_months": model.monitor_months,
            "max_single_adjustment_pct": model.max_single_adjustment_pct,
            "drawdown_alert_pct_points": model.drawdown_alert_pct_points,
            "concentration_alert_pct_points": (
                model.concentration_alert_pct_points
            ),
            "major_event_heat_threshold": model.major_event_heat_threshold,
            "event_impact_exposure_threshold_pct": (
                model.event_impact_exposure_threshold_pct
            ),
            "alert_on_risk_state_worsening": (
                model.alert_on_risk_state_worsening
            ),
            "schedule_cron": model.schedule_cron,
            "schedule_id": model.schedule_id,
            "persisted": persisted,
            "created_at": model.created_at.isoformat(),
            "updated_at": model.updated_at.isoformat(),
        }

    def _default_policy(self, *, user_id: str, portfolio_id: str) -> dict[str, Any]:
        return {
            "user_id": user_id,
            "portfolio_id": portfolio_id,
            "enabled": False,
            "risk_preference": "balanced",
            "investment_horizon": "one_to_three_years",
            "monitor_months": 12,
            "max_single_adjustment_pct": 15.0,
            "drawdown_alert_pct_points": 3.0,
            "concentration_alert_pct_points": 5.0,
            "major_event_heat_threshold": 70.0,
            "event_impact_exposure_threshold_pct": 5.0,
            "alert_on_risk_state_worsening": True,
            "schedule_cron": self.DEFAULT_CRON,
            "schedule_id": self._schedule_id(portfolio_id),
            "persisted": False,
            "created_at": None,
            "updated_at": None,
        }

    @staticmethod
    def _snapshot_to_dict(model: PortfolioRiskSnapshotModel) -> dict[str, Any]:
        return {
            "id": model.id,
            "run_id": model.run_id,
            "portfolio_id": model.portfolio_id,
            "period_months": model.period_months,
            "assessment_date": model.assessment_date.isoformat(),
            "state_code": model.state_code,
            "state_label": model.state_label,
            "caution_points": model.caution_points,
            "supportive_points": model.supportive_points,
            "annualized_volatility_pct": model.annualized_volatility_pct,
            "max_drawdown_pct": model.max_drawdown_pct,
            "daily_cvar_95_pct": model.daily_cvar_95_pct,
            "concentration_hhi": model.concentration_hhi,
            "max_weight_pct": model.max_weight_pct,
            "major_events": list(model.major_events or []),
            "event_impacts": list(model.event_impacts or []),
            "source_summary": dict(model.source_summary or {}),
            "comparison": dict(model.comparison or {}),
            "generation": dict(model.generation or {}),
            "observed_at": model.observed_at.isoformat(),
        }

    @staticmethod
    def _alert_to_dict(model: PortfolioAlertModel) -> dict[str, Any]:
        return {
            "id": model.id,
            "portfolio_id": model.portfolio_id,
            "snapshot_id": model.snapshot_id,
            "alert_type": model.alert_type,
            "severity": model.severity,
            "title": model.title,
            "detail": model.detail,
            "metric_data": dict(model.metric_data or {}),
            "explanation": (
                dict(model.explanation) if model.explanation else None
            ),
            "status": model.status,
            "created_at": model.created_at.isoformat(),
            "acknowledged_at": (
                model.acknowledged_at.isoformat()
                if model.acknowledged_at is not None
                else None
            ),
        }

    @staticmethod
    def _normalize_policy_values(**values: Any) -> dict[str, Any]:
        risk = str(values["risk_preference"] or "").strip().lower()
        horizon = str(values["investment_horizon"] or "").strip().lower()
        months = int(values["monitor_months"])
        max_adjustment = float(values["max_single_adjustment_pct"])
        drawdown = float(values["drawdown_alert_pct_points"])
        concentration = float(values["concentration_alert_pct_points"])
        event_heat = float(values["major_event_heat_threshold"])
        event_exposure = float(values["event_impact_exposure_threshold_pct"])
        cron = " ".join(str(values["schedule_cron"] or "").strip().split())
        if risk not in RISK_PREFERENCES:
            raise ValueError("risk_preference 格式非法。")
        if horizon not in INVESTMENT_HORIZONS:
            raise ValueError("investment_horizon 格式非法。")
        if months not in SUPPORTED_MONTHS:
            raise ValueError("monitor_months 只能是 6、12 或 36。")
        if not 1 <= max_adjustment <= 50:
            raise ValueError("max_single_adjustment_pct 必须在 1-50 之间。")
        if not 0.5 <= drawdown <= 30:
            raise ValueError("回撤预警阈值必须在 0.5-30 个百分点之间。")
        if not 1 <= concentration <= 50:
            raise ValueError("集中度预警阈值必须在 1-50 个百分点之间。")
        if not 0 <= event_heat <= 100:
            raise ValueError("重大事件热度阈值必须在 0-100 之间。")
        if not 1 <= event_exposure <= 100:
            raise ValueError("事件影响仓位阈值必须在 1-100% 之间。")
        # 复用项目已有 Cron 解析器，避免这里只检查长度、真正注册时才失败。
        from app.services.cron_scheduler import validate_cron

        cron_error = validate_cron(cron)
        if cron_error:
            raise ValueError(cron_error)
        return {
            "enabled": bool(values["enabled"]),
            "risk_preference": risk,
            "investment_horizon": horizon,
            "monitor_months": months,
            "max_single_adjustment_pct": round(max_adjustment, 2),
            "drawdown_alert_pct_points": round(drawdown, 2),
            "concentration_alert_pct_points": round(concentration, 2),
            "major_event_heat_threshold": round(event_heat, 2),
            "event_impact_exposure_threshold_pct": round(event_exposure, 2),
            "alert_on_risk_state_worsening": bool(
                values["alert_on_risk_state_worsening"]
            ),
            "schedule_cron": cron,
        }

    @staticmethod
    def _normalize_key(user_id: str, portfolio_id: str) -> tuple[str, str]:
        user = str(user_id or "").strip()
        portfolio = str(portfolio_id or "").strip()
        if not USER_ID_PATTERN.fullmatch(user):
            raise ValueError("user_id 格式非法。")
        if not PORTFOLIO_ID_PATTERN.fullmatch(portfolio):
            raise ValueError("portfolio_id 格式非法。")
        return user, portfolio

    @staticmethod
    def _normalize_run_id(run_id: str) -> str:
        value = str(run_id or "").strip()
        if not RUN_ID_PATTERN.fullmatch(value):
            raise ValueError("run_id 格式非法。")
        return value

    @staticmethod
    def _normalize_alert_id(alert_id: str) -> str:
        value = str(alert_id or "").strip()
        if not re.fullmatch(r"^alert_[a-f0-9]{16}$", value):
            raise ValueError("alert_id 格式非法。")
        return value

    @staticmethod
    def _schedule_id(portfolio_id: str) -> str:
        return f"portfolio-monitor-{portfolio_id.removeprefix('portfolio_')}"

    @staticmethod
    def _notify(
        callback: PortfolioMonitorProgress | None,
        stage: str,
        message: str,
    ) -> None:
        if callback is not None:
            callback(stage, message)


portfolio_monitoring_service = PortfolioMonitoringService()

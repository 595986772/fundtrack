"""比较同一组合的两次研究，并生成可追溯的结论变化审计。

数据职责分为三层：

1. Background Job 保存两次已经完成的研究报告；
2. ResearchInputSnapshot 保存报告真正消费的不可变输入；
3. 本 Service 用 Python 比较分区哈希、关键指标、持仓、事件和决策，再把差异事实交给
   受约束的模型解释。

因此模型不会决定“什么发生了变化”。即使模型不可用，用户仍能看到相同的前后值和
变化事实；模型只影响最后一段通俗说明。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from typing import Any

from sqlalchemy import select

from app.database import Database, runtime_database
from app.models import BackgroundJobModel
from app.services.agent_authorization import allowed_agent_portfolio_ids, guard_agent_portfolio
from app.services.portfolio_research_change_interpretation import (
    PortfolioResearchChangeExplainer,
    PortfolioResearchChangeInterpretationError,
    portfolio_research_change_interpretation_service,
)
from app.services.portfolio_research_snapshots import (
    PortfolioResearchSnapshotError,
    PortfolioResearchSnapshotStore,
    portfolio_research_snapshot_service,
)


AUDIT_VERSION = "portfolio-research-change-audit-v1"
MAX_RESEARCH_JOBS_PER_USER = 100
SECTION_LABELS = {
    "portfolio": "组合持仓",
    "assessment": "规则研判",
    "quant_risk": "量化风险",
    "performance_snapshot": "历史业绩",
    "market_context": "市场环境",
    "decision_history": "决策记录",
}

# 这些指标都有明确且稳定的业务含义。列表只决定“比较什么”，不负责判断涨跌是好是坏。
METRIC_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("assessment", "score_breakdown.caution_points", "风险证据分", "分"),
    ("assessment", "score_breakdown.supportive_points", "支撑证据分", "分"),
    ("assessment", "concentration.max_weight_pct", "最大单只权重", "%"),
    ("assessment", "concentration.effective_fund_count", "有效基金数", "只"),
    ("quant_risk", "period_return_pct", "区间收益", "%"),
    ("quant_risk", "annualized_volatility_pct", "风险年化波动率", "%"),
    ("quant_risk", "daily_cvar_95_pct", "日 CVaR", "%"),
    ("quant_risk", "max_drawdown.drawdown_pct", "风险最大回撤", "%"),
    ("performance_snapshot", "twr_pct", "时间加权收益率", "%"),
    (
        "performance_snapshot",
        "annualized_return_pct",
        "业绩年化收益率",
        "%",
    ),
    (
        "performance_snapshot",
        "annualized_volatility_pct",
        "业绩年化波动率",
        "%",
    ),
    ("performance_snapshot", "sharpe_ratio", "夏普比率", ""),
    ("performance_snapshot", "sortino_ratio", "Sortino 比率", ""),
    (
        "performance_snapshot",
        "max_drawdown.drawdown_pct",
        "业绩最大回撤",
        "%",
    ),
    (
        "performance_snapshot",
        "benchmark.excess_return_pct",
        "相对基准超额收益",
        "%",
    ),
    ("market_context", "relevant_event_count", "相关市场事件数", "条"),
    ("decision_history", "decision_count", "历史决策数量", "条"),
)


class PortfolioResearchAuditError(RuntimeError):
    """研究审计基础异常。"""


class PortfolioResearchAuditNotFoundError(PortfolioResearchAuditError):
    """指定研究 Job 不存在，或不属于当前用户。"""


class PortfolioResearchAuditValidationError(PortfolioResearchAuditError):
    """两次研究不属于同一组合或观察区间，不能直接比较。"""


class PortfolioResearchComparisonUnavailableError(PortfolioResearchAuditError):
    """当前报告之前没有同区间报告可作为比较基线。"""


class PortfolioResearchAuditService:
    """从持久化报告和不可变快照生成按需变化审计。"""

    def __init__(
        self,
        *,
        database: Database = runtime_database,
        snapshots: PortfolioResearchSnapshotStore = (
            portfolio_research_snapshot_service
        ),
        explainer: PortfolioResearchChangeExplainer = (
            portfolio_research_change_interpretation_service
        ),
    ) -> None:
        self.database = database
        self.snapshots = snapshots
        self.explainer = explainer
        self.database.create_schema()

    def read_report(self, *, user_id: str, portfolio_id: str, job_id: str) -> dict[str, Any]:
        """详情与追问共用的读取入口：校验用户、组合、完成状态和冻结证据。"""
        guard_agent_portfolio(user_id, portfolio_id)
        record = self._get_completed_record(user_id=user_id, job_id=job_id)
        report = self._report_for_record(record)
        if report.get("portfolio_id") != portfolio_id:
            raise PortfolioResearchAuditNotFoundError("这份报告不属于当前组合。")
        self._snapshot_for_record(record=record, report=report)
        from app.services.portfolio_diagnosis import report_with_diagnosis
        return report_with_diagnosis(report)

    def list_runs(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        months: int | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """列出可比较的已完成研究，并附带最近一份完整报告用于页面恢复。"""

        guard_agent_portfolio(user_id, portfolio_id)
        safe_limit = max(1, min(int(limit), 20))
        records = self._completed_records(user_id=user_id)
        matching: list[tuple[BackgroundJobModel, dict[str, Any], dict[str, Any]]] = []
        omitted_count = 0
        for record in records:
            try:
                report = self._report_for_record(record)
            except PortfolioResearchAuditValidationError:
                omitted_count += 1
                continue
            if report.get("portfolio_id") != portfolio_id:
                continue
            if months is not None and int(report.get("period_months") or 0) != months:
                continue
            try:
                snapshot = self._snapshot_for_record(record=record, report=report)
            except PortfolioResearchAuditValidationError:
                # 快照功能上线前的旧报告可以继续保留，但不能伪装成可审计报告。
                # 这里跳过并公开数量；用户明确指定旧 Job 对比时仍会得到严格错误。
                omitted_count += 1
                continue
            if len(matching) < safe_limit:
                matching.append((record, report, snapshot))

        runs = [
            self._run_summary(record=record, report=report, snapshot=snapshot)
            for record, report, snapshot in matching
        ]
        from app.services.portfolio_diagnosis import report_with_diagnosis
        return {
            "data_kind": "portfolio_research_history",
            "user_id": user_id,
            "portfolio_id": portfolio_id,
            "period_months": months,
            "count": len(runs),
            "omitted_count": omitted_count,
            "message": (
                f"有 {omitted_count} 份旧研究缺少不可变快照，不能参与变化审计。"
                if omitted_count
                else ""
            ),
            "runs": runs,
            # 页面只恢复一份完整报告，其余历史项保持轻量，避免一次传输多份大 JSON。
            "latest_report": report_with_diagnosis(deepcopy(matching[0][1])) if matching else None,
        }

    def compare(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        target_job_id: str,
        base_job_id: str | None = None,
    ) -> dict[str, Any]:
        """比较目标报告与较早基线；未指定基线时自动选择最近的同区间报告。"""

        guard_agent_portfolio(user_id, portfolio_id)
        target_record = self._get_completed_record(
            user_id=user_id,
            job_id=target_job_id,
        )
        target_report = self._report_for_record(target_record)
        self._validate_portfolio(target_report, portfolio_id=portfolio_id)

        if base_job_id:
            if base_job_id == target_job_id:
                raise PortfolioResearchAuditValidationError(
                    "基线研究和目标研究不能是同一个 Job。"
                )
            base_record = self._get_completed_record(
                user_id=user_id,
                job_id=base_job_id,
            )
            base_report = self._report_for_record(base_record)
        else:
            base_record, base_report = self._find_previous_report(
                user_id=user_id,
                portfolio_id=portfolio_id,
                target_record=target_record,
                target_report=target_report,
            )

        self._validate_portfolio(base_report, portfolio_id=portfolio_id)
        self._validate_periods(base_report=base_report, target_report=target_report)

        base_snapshot = self._snapshot_for_record(
            record=base_record,
            report=base_report,
        )
        target_snapshot = self._snapshot_for_record(
            record=target_record,
            report=target_report,
        )
        change_set = self._build_change_set(
            base_report=base_report,
            target_report=target_report,
            base_snapshot=base_snapshot,
            target_snapshot=target_snapshot,
        )

        try:
            interpretation = self.explainer.interpret(change_set=change_set)
        except PortfolioResearchChangeInterpretationError as exc:
            interpretation = self.explainer.fallback(
                change_set=change_set,
                reason=str(exc),
            )

        base_run = self._run_summary(
            record=base_record,
            report=base_report,
            snapshot=base_snapshot,
        )
        target_run = self._run_summary(
            record=target_record,
            report=target_report,
            snapshot=target_snapshot,
        )
        fingerprint = self._fingerprint(
            {
                "base_snapshot": base_snapshot["content_hash"],
                "target_snapshot": target_snapshot["content_hash"],
                "base_conclusion": self._conclusion_fingerprint(base_report),
                "target_conclusion": self._conclusion_fingerprint(target_report),
                "audit_version": AUDIT_VERSION,
            }
        )
        return {
            "data_kind": "portfolio_research_change_audit",
            "audit_version": AUDIT_VERSION,
            "comparison_id": f"research_change_{fingerprint[:16]}",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id,
            "portfolio_id": portfolio_id,
            "period_months": int(target_report["period_months"]),
            "base_run": base_run,
            "target_run": target_run,
            "input_changed": change_set["input_changed"],
            "conclusion_changed": change_set["conclusion_changed"],
            "change_scope": change_set["change_scope"],
            "changed_section_count": change_set["changed_section_count"],
            "changed_sections": change_set["changed_sections"],
            "facts": change_set["facts"],
            "interpretation": interpretation,
            "limitations": change_set["limitations"],
            "methodology": (
                "Python 先比较两份不可变输入快照的分区哈希和白名单字段；"
                "DeepSeek 仅解释差异目录并引用 fact_key。"
            ),
        }

    def _completed_records(self, *, user_id: str) -> list[BackgroundJobModel]:
        """读取当前用户全部研究 Job；每用户已有总量上限，因此最多扫描一百条。"""

        allowed_ids = allowed_agent_portfolio_ids(user_id)
        statement = (
            select(BackgroundJobModel)
            .where(
                BackgroundJobModel.user_id == user_id,
                BackgroundJobModel.job_type == "portfolio_research",
                BackgroundJobModel.status == "completed",
            )
            .order_by(
                BackgroundJobModel.completed_at.desc(),
                BackgroundJobModel.updated_at.desc(),
            )
            .limit(MAX_RESEARCH_JOBS_PER_USER)
        )
        if allowed_ids is not None:
            # 依据 Job 的持久化输入过滤，不让未授权组合占用列表额度或泄露计数。
            statement = statement.where(
                BackgroundJobModel.payload["portfolio_id"].as_string().in_(allowed_ids)
            )
        with self.database.session() as session:
            return list(session.scalars(statement).all())

    def _get_completed_record(
        self,
        *,
        user_id: str,
        job_id: str,
    ) -> BackgroundJobModel:
        with self.database.session() as session:
            record = session.get(
                BackgroundJobModel,
                {"user_id": user_id, "id": job_id},
            )
        if record is None or record.job_type != "portfolio_research":
            raise PortfolioResearchAuditNotFoundError(
                f"组合研究 Job 不存在：{job_id}"
            )
        guard_agent_portfolio(record.user_id, (record.payload or {}).get("portfolio_id", ""))
        if record.status != "completed" or not record.result:
            raise PortfolioResearchAuditValidationError(
                f"组合研究 Job 尚未完成：{job_id}"
            )
        return record

    def _find_previous_report(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        target_record: BackgroundJobModel,
        target_report: dict[str, Any],
    ) -> tuple[BackgroundJobModel, dict[str, Any]]:
        """从目标报告向历史方向查找最近一份同组合、同区间报告。"""

        target_seen = False
        target_months = int(target_report.get("period_months") or 0)
        for record in self._completed_records(user_id=user_id):
            if record.id == target_record.id:
                target_seen = True
                continue
            if not target_seen:
                continue
            report = self._report_for_record(record)
            if report.get("portfolio_id") != portfolio_id:
                continue
            if int(report.get("period_months") or 0) != target_months:
                continue
            return record, report
        raise PortfolioResearchComparisonUnavailableError(
            "当前报告之前还没有同组合、同观察区间的研究报告。"
        )

    @staticmethod
    def _report_for_record(record: BackgroundJobModel) -> dict[str, Any]:
        # 调用参数、Job 输入和报告 JSON 不能互相冒充；两份持久化关联都需在授权内。
        guard_agent_portfolio(record.user_id, (record.payload or {}).get("portfolio_id", ""))
        report = deepcopy(record.result or {})
        if report.get("workflow") != "portfolio_research":
            raise PortfolioResearchAuditValidationError(
                f"Job {record.id} 没有可比较的组合研究报告。"
            )
        guard_agent_portfolio(record.user_id, report.get("portfolio_id", ""))
        return report

    def _snapshot_for_record(
        self,
        *,
        record: BackgroundJobModel,
        report: dict[str, Any],
    ) -> dict[str, Any]:
        """从独立快照表读取并核对报告引用，防止拿错或被篡改。"""

        try:
            snapshot = self.snapshots.get_for_run(
                user_id=record.user_id,
                run_id=record.id,
            )
        except PortfolioResearchSnapshotError as exc:
            raise PortfolioResearchAuditValidationError(str(exc)) from exc
        if snapshot is None:
            raise PortfolioResearchAuditValidationError(
                f"研究 Job {record.id} 缺少不可变输入快照。"
            )
        metadata = report.get("research_snapshot") or {}
        expected = {
            "id": metadata.get("snapshot_id"),
            "content_hash": metadata.get("content_hash"),
            "portfolio_id": report.get("portfolio_id"),
            "period_months": report.get("period_months"),
        }
        for key, value in expected.items():
            if snapshot.get(key) != value:
                raise PortfolioResearchAuditValidationError(
                    f"研究 Job {record.id} 的快照 {key} 与报告引用不一致。"
                )
        return snapshot

    @staticmethod
    def _validate_portfolio(report: dict[str, Any], *, portfolio_id: str) -> None:
        if report.get("portfolio_id") != portfolio_id:
            raise PortfolioResearchAuditValidationError(
                "研究报告与当前组合不匹配。"
            )

    @staticmethod
    def _validate_periods(
        *,
        base_report: dict[str, Any],
        target_report: dict[str, Any],
    ) -> None:
        if base_report.get("period_months") != target_report.get("period_months"):
            raise PortfolioResearchAuditValidationError(
                "两次研究的观察区间不同，不能直接比较。"
            )

    def _build_change_set(
        self,
        *,
        base_report: dict[str, Any],
        target_report: dict[str, Any],
        base_snapshot: dict[str, Any],
        target_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        base_payload = base_snapshot["payload"]
        target_payload = target_snapshot["payload"]
        base_hashes = (base_snapshot.get("source_manifest") or {}).get(
            "section_hashes"
        ) or {}
        target_hashes = (target_snapshot.get("source_manifest") or {}).get(
            "section_hashes"
        ) or {}

        changed_sections = []
        for section, label in SECTION_LABELS.items():
            before_hash = str(base_hashes.get(section) or "")
            after_hash = str(target_hashes.get(section) or "")
            changed_sections.append(
                {
                    "section": section,
                    "label": label,
                    "changed": before_hash != after_hash,
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                }
            )

        facts: list[dict[str, Any]] = []
        self._append_state_facts(
            facts,
            base_payload=base_payload,
            target_payload=target_payload,
        )
        self._append_metric_facts(
            facts,
            base_payload=base_payload,
            target_payload=target_payload,
        )
        self._append_holding_facts(
            facts,
            base_payload=base_payload,
            target_payload=target_payload,
        )
        self._append_event_facts(
            facts,
            base_payload=base_payload,
            target_payload=target_payload,
        )
        self._append_decision_facts(
            facts,
            base_payload=base_payload,
            target_payload=target_payload,
        )

        conclusion_changed = self._append_conclusion_facts(
            facts,
            base_report=base_report,
            target_report=target_report,
        )

        # 某个分区哈希改变但白名单字段没变时，也要公开这一事实，不能让变化悄悄消失。
        fact_sections = {item["section"] for item in facts}
        for item in changed_sections:
            section = item["section"]
            if not item["changed"] or section in fact_sections:
                continue
            facts.append(
                self._fact(
                    fact_key=f"section:{section}",
                    section=section,
                    kind="section",
                    label=f"{item['label']}存在其他结构变化",
                    before_value=item["before_hash"],
                    after_value=item["after_hash"],
                    before_text=self._short_hash(item["before_hash"]),
                    after_text=self._short_hash(item["after_hash"]),
                )
            )

        changed_count = sum(1 for item in changed_sections if item["changed"])
        if changed_count == 0 and not conclusion_changed:
            scope = "none"
        elif changed_count == 0:
            scope = "wording_only"
        elif changed_count == 1:
            scope = "limited"
        elif changed_count <= 3:
            scope = "multi_source"
        else:
            scope = "broad"

        return {
            "input_changed": changed_count > 0,
            "conclusion_changed": conclusion_changed,
            "change_scope": scope,
            "changed_section_count": changed_count,
            "changed_sections": changed_sections,
            "facts": facts[:60],
            "limitations": [
                "比较对象是研究图消费的结构化输入，不是全部原始净值和新闻全文。",
                "结构变化范围不等于投资风险等级，仍需结合具体前后值理解。",
            ],
        }

    def _append_state_facts(
        self,
        facts: list[dict[str, Any]],
        *,
        base_payload: dict[str, Any],
        target_payload: dict[str, Any],
    ) -> None:
        specs = (
            (
                "assessment",
                "state_code",
                "state_label",
                "规则研判状态",
            ),
            (
                "assessment",
                "concentration.state_code",
                "concentration.state_label",
                "组合集中度状态",
            ),
            (
                "market_context",
                "state_code",
                "state_label",
                "市场环境状态",
            ),
        )
        for section, code_path, label_path, label in specs:
            before_section = base_payload.get(section) or {}
            after_section = target_payload.get(section) or {}
            before_code = self._path_value(before_section, code_path)
            after_code = self._path_value(after_section, code_path)
            if before_code == after_code:
                continue
            before_label = self._path_value(before_section, label_path) or before_code
            after_label = self._path_value(after_section, label_path) or after_code
            facts.append(
                self._fact(
                    fact_key=f"state:{section}.{code_path}",
                    section=section,
                    kind="state",
                    label=label,
                    before_value=before_code,
                    after_value=after_code,
                    before_text=str(before_label or "暂无"),
                    after_text=str(after_label or "暂无"),
                    tone=self._state_tone(after_code),
                )
            )

    def _append_metric_facts(
        self,
        facts: list[dict[str, Any]],
        *,
        base_payload: dict[str, Any],
        target_payload: dict[str, Any],
    ) -> None:
        for section, path, label, unit in METRIC_SPECS:
            before = self._path_value(base_payload.get(section) or {}, path)
            after = self._path_value(target_payload.get(section) or {}, path)
            if self._values_equal(before, after):
                continue
            before_number = self._number(before)
            after_number = self._number(after)
            delta = (
                round(after_number - before_number, 6)
                if before_number is not None and after_number is not None
                else None
            )
            facts.append(
                self._fact(
                    fact_key=f"metric:{section}.{path}",
                    section=section,
                    kind="metric",
                    label=label,
                    before_value=before,
                    after_value=after,
                    before_text=self._format_value(before, unit),
                    after_text=self._format_value(after, unit),
                    delta=delta,
                    delta_text=self._format_delta(delta, unit),
                )
            )

    def _append_holding_facts(
        self,
        facts: list[dict[str, Any]],
        *,
        base_payload: dict[str, Any],
        target_payload: dict[str, Any],
    ) -> None:
        base_holdings = self._keyed_items(
            (base_payload.get("portfolio") or {}).get("holdings"),
            key="fund_code",
        )
        target_holdings = self._keyed_items(
            (target_payload.get("portfolio") or {}).get("holdings"),
            key="fund_code",
        )
        for fund_code in sorted(set(base_holdings) | set(target_holdings)):
            before = base_holdings.get(fund_code)
            after = target_holdings.get(fund_code)
            short_name = str(
                (after or before or {}).get("short_name") or fund_code
            )
            if before is None or after is None:
                facts.append(
                    self._fact(
                        fact_key=f"holding:{fund_code}:membership",
                        section="portfolio",
                        kind="holding",
                        label=f"{short_name}持仓",
                        before_value=before,
                        after_value=after,
                        before_text=(self._holding_text(before) if before else "未持有"),
                        after_text=(self._holding_text(after) if after else "未持有"),
                    )
                )
                continue
            before_weight = before.get("weight_pct")
            after_weight = after.get("weight_pct")
            if self._values_equal(before_weight, after_weight):
                continue
            before_number = self._number(before_weight)
            after_number = self._number(after_weight)
            delta = (
                round(after_number - before_number, 6)
                if before_number is not None and after_number is not None
                else None
            )
            facts.append(
                self._fact(
                    fact_key=f"holding:{fund_code}:weight_pct",
                    section="portfolio",
                    kind="holding",
                    label=f"{short_name}组合权重",
                    before_value=before_weight,
                    after_value=after_weight,
                    before_text=self._format_value(before_weight, "%"),
                    after_text=self._format_value(after_weight, "%"),
                    delta=delta,
                    delta_text=self._format_delta(delta, "个百分点"),
                )
            )

    def _append_event_facts(
        self,
        facts: list[dict[str, Any]],
        *,
        base_payload: dict[str, Any],
        target_payload: dict[str, Any],
    ) -> None:
        base_events = self._keyed_items(
            (base_payload.get("market_context") or {}).get("events"),
            key="event_id",
        )
        target_events = self._keyed_items(
            (target_payload.get("market_context") or {}).get("events"),
            key="event_id",
        )
        for event_id in sorted(set(base_events) | set(target_events)):
            before = base_events.get(event_id)
            after = target_events.get(event_id)
            if before is None or after is None:
                event = after or before or {}
                direction = str(event.get("impact_direction") or "neutral")
                facts.append(
                    self._fact(
                        fact_key=f"event:{event_id}:{'added' if after else 'removed'}",
                        section="market_context",
                        kind="event",
                        label=("新增市场事件" if after else "移出市场事件"),
                        before_value=before,
                        after_value=after,
                        before_text=self._event_text(before) if before else "未出现",
                        after_text=self._event_text(after) if after else "已不在有效事件中",
                        tone=self._direction_tone(direction) if after else "neutral",
                    )
                )
                continue
            before_signature = self._event_signature(before)
            after_signature = self._event_signature(after)
            if before_signature == after_signature:
                continue
            facts.append(
                self._fact(
                    fact_key=f"event:{event_id}:changed",
                    section="market_context",
                    kind="event",
                    label="市场事件属性变化",
                    before_value=before_signature,
                    after_value=after_signature,
                    before_text=self._event_text(before),
                    after_text=self._event_text(after),
                    tone=self._direction_tone(
                        str(after.get("impact_direction") or "neutral")
                    ),
                )
            )

    def _append_decision_facts(
        self,
        facts: list[dict[str, Any]],
        *,
        base_payload: dict[str, Any],
        target_payload: dict[str, Any],
    ) -> None:
        base_decisions = self._keyed_items(
            (base_payload.get("decision_history") or {}).get("decisions"),
            key="decision_id",
        )
        target_decisions = self._keyed_items(
            (target_payload.get("decision_history") or {}).get("decisions"),
            key="decision_id",
        )
        for decision_id in sorted(set(base_decisions) | set(target_decisions)):
            before = base_decisions.get(decision_id)
            after = target_decisions.get(decision_id)
            if before is None or after is None:
                facts.append(
                    self._fact(
                        fact_key=(
                            f"decision:{decision_id}:{'added' if after else 'removed'}"
                        ),
                        section="decision_history",
                        kind="decision",
                        label=("新增历史决策" if after else "移出历史决策"),
                        before_value=before,
                        after_value=after,
                        before_text=self._decision_text(before) if before else "无记录",
                        after_text=self._decision_text(after) if after else "已不在最近记录中",
                    )
                )
                continue
            before_signature = self._decision_signature(before)
            after_signature = self._decision_signature(after)
            if before_signature == after_signature:
                continue
            facts.append(
                self._fact(
                    fact_key=f"decision:{decision_id}:outcome",
                    section="decision_history",
                    kind="decision",
                    label="历史决策观察结果",
                    before_value=before_signature,
                    after_value=after_signature,
                    before_text=self._decision_text(before),
                    after_text=self._decision_text(after),
                )
            )

    def _append_conclusion_facts(
        self,
        facts: list[dict[str, Any]],
        *,
        base_report: dict[str, Any],
        target_report: dict[str, Any],
    ) -> bool:
        before = self._conclusion_fingerprint(base_report)
        after = self._conclusion_fingerprint(target_report)
        if before == after:
            return False

        before_model = base_report.get("model_interpretation") or {}
        after_model = target_report.get("model_interpretation") or {}
        if before_model.get("current_state") != after_model.get("current_state"):
            facts.append(
                self._fact(
                    fact_key="conclusion:model_state",
                    section="conclusion",
                    kind="conclusion",
                    label="AI 综合状态",
                    before_value=before_model.get("current_state"),
                    after_value=after_model.get("current_state"),
                    before_text=str(before_model.get("current_state") or "暂无"),
                    after_text=str(after_model.get("current_state") or "暂无"),
                    tone=self._state_tone(after_model.get("current_state")),
                )
            )
        if base_report.get("headline") != target_report.get("headline"):
            facts.append(
                self._fact(
                    fact_key="conclusion:headline",
                    section="conclusion",
                    kind="conclusion",
                    label="报告结论表达",
                    before_value=base_report.get("headline"),
                    after_value=target_report.get("headline"),
                    before_text=str(base_report.get("headline") or "暂无")[:140],
                    after_text=str(target_report.get("headline") or "暂无")[:140],
                )
            )
        elif base_report.get("summary") != target_report.get("summary"):
            facts.append(
                self._fact(
                    fact_key="conclusion:summary",
                    section="conclusion",
                    kind="conclusion",
                    label="报告摘要表达",
                    before_value=base_report.get("summary"),
                    after_value=target_report.get("summary"),
                    before_text=str(base_report.get("summary") or "暂无")[:140],
                    after_text=str(target_report.get("summary") or "暂无")[:140],
                )
            )
        return True

    @staticmethod
    def _fact(
        *,
        fact_key: str,
        section: str,
        kind: str,
        label: str,
        before_value: Any,
        after_value: Any,
        before_text: str,
        after_text: str,
        delta: float | None = None,
        delta_text: str = "",
        tone: str = "neutral",
    ) -> dict[str, Any]:
        return {
            "fact_key": fact_key,
            "section": section,
            "section_label": SECTION_LABELS.get(section, "最终结论"),
            "kind": kind,
            "label": label,
            "before_value": deepcopy(before_value),
            "after_value": deepcopy(after_value),
            "before_text": before_text,
            "after_text": after_text,
            "delta": delta,
            "delta_text": delta_text,
            "tone": tone,
        }

    @staticmethod
    def _run_summary(
        *,
        record: BackgroundJobModel,
        report: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        interpretation = report.get("model_interpretation") or {}
        assessment = report.get("assessment_snapshot") or {}
        completed_at = record.completed_at or record.updated_at
        return {
            "job_id": record.id,
            "portfolio_id": report["portfolio_id"],
            "portfolio_name": report.get("portfolio_name") or "",
            "period_months": int(report["period_months"]),
            "completed_at": PortfolioResearchAuditService._date_text(completed_at),
            "assessment_date": report.get("assessment_date"),
            "headline": str(report.get("headline") or ""),
            "rule_state_code": assessment.get("state_code"),
            "rule_state_label": assessment.get("state_label"),
            "model_state_code": interpretation.get("current_state"),
            "snapshot_id": snapshot["id"],
            "content_hash": snapshot["content_hash"],
            "graph_version": snapshot["graph_version"],
        }

    @staticmethod
    def _conclusion_fingerprint(report: dict[str, Any]) -> dict[str, Any]:
        interpretation = report.get("model_interpretation") or {}
        generation = report.get("generation") or {}
        return {
            "headline": report.get("headline"),
            "summary": report.get("summary"),
            "current_state": interpretation.get("current_state"),
            "confidence": interpretation.get("confidence"),
            "prompt_version": generation.get("prompt_version"),
            "model_used": generation.get("model_used"),
        }

    @staticmethod
    def _path_value(value: dict[str, Any], path: str) -> Any:
        current: Any = value
        for part in path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    @staticmethod
    def _keyed_items(items: Any, *, key: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get(key) or "").strip()
            if identifier:
                result[identifier] = item
        return result

    @staticmethod
    def _number(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @classmethod
    def _values_equal(cls, before: Any, after: Any) -> bool:
        before_number = cls._number(before)
        after_number = cls._number(after)
        if before_number is not None and after_number is not None:
            return math.isclose(before_number, after_number, rel_tol=0, abs_tol=1e-9)
        return before == after

    @classmethod
    def _format_value(cls, value: Any, unit: str) -> str:
        number = cls._number(value)
        if number is None:
            return "暂无" if value is None else str(value)
        if unit == "%":
            return f"{number:.2f}%"
        if unit in {"分", "只"}:
            return f"{number:.2f} {unit}"
        if unit == "条":
            return f"{int(round(number))} 条"
        return f"{number:.2f}"

    @staticmethod
    def _format_delta(delta: float | None, unit: str) -> str:
        if delta is None:
            return ""
        suffix = " 个百分点" if unit in {"%", "个百分点"} else (f" {unit}" if unit else "")
        return f"{delta:+.2f}{suffix}"

    @classmethod
    def _holding_text(cls, holding: dict[str, Any]) -> str:
        name = str(holding.get("short_name") or holding.get("fund_code") or "基金")
        weight = cls._format_value(holding.get("weight_pct"), "%")
        return f"{name}，权重 {weight}"

    @staticmethod
    def _event_signature(event: dict[str, Any]) -> tuple[Any, ...]:
        return (
            event.get("title"),
            event.get("impact_direction"),
            event.get("impact_horizon"),
            event.get("freshness_status"),
            event.get("decay_factor"),
        )

    @staticmethod
    def _event_text(event: dict[str, Any]) -> str:
        title = str(event.get("title") or "未命名事件")
        direction = str(event.get("impact_direction") or "neutral")
        event_date = str(event.get("event_date") or "日期未知")
        return f"{event_date} · {title} · {direction}"

    @staticmethod
    def _decision_signature(decision: dict[str, Any]) -> tuple[Any, ...]:
        outcome = decision.get("outcome") or {}
        return (
            decision.get("action"),
            outcome.get("status"),
            outcome.get("as_of_date"),
            outcome.get("portfolio_return_pct"),
            outcome.get("benchmark_return_pct"),
            outcome.get("excess_return_pct"),
            decision.get("review_note"),
            decision.get("reviewed_at"),
        )

    @staticmethod
    def _decision_text(decision: dict[str, Any]) -> str:
        action = str(decision.get("action_label") or decision.get("action") or "未分类")
        date = str(decision.get("decision_date") or "日期未知")
        outcome = decision.get("outcome") or {}
        status = str(outcome.get("status") or "尚未形成结果")
        return f"{date} · {action} · {status}"

    @staticmethod
    def _state_tone(state: Any) -> str:
        value = str(state or "").lower()
        if value in {"cautious", "high_attention", "high", "concentrated"}:
            return "caution"
        if value in {"supportive", "low", "diversified"}:
            return "supportive"
        return "neutral"

    @staticmethod
    def _direction_tone(direction: str) -> str:
        if direction == "caution":
            return "caution"
        if direction == "supportive":
            return "supportive"
        return "neutral"

    @staticmethod
    def _short_hash(value: str) -> str:
        return f"{value[:10]}…" if value else "缺失"

    @staticmethod
    def _fingerprint(value: Any) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @staticmethod
    def _date_text(value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "isoformat"):
            return str(value.isoformat())
        return str(value)


portfolio_research_audit_service = PortfolioResearchAuditService()

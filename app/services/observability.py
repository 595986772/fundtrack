"""Agent Evals & Observability：运行观测持久化服务。

这个服务不决定 Agent 应该怎么回答，只在回答结束后记录客观事实：耗时、模型轮次、
工具调用、错误、证据 ID 和 token。聊天接口与评测运行器共用它，后续可以把同一批
数据接入日志平台、Prometheus 或 tracing 系统，而不必改 Agent Loop。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text

from app.database import Database, runtime_database
from app.models import AgentEvalRunModel, AgentRunObservationModel
from app.schemas import (
    AgentEvalCaseResult,
    AgentEvalRun,
    AgentObservabilitySummary,
    AgentRunObservation,
    AgentTokenUsage,
)
from app.services.eval_context import eval_case_set_id
from app.services.fund_report_eval_grading import aggregate_report_metrics
from app.services.evidence import (
    extract_answer_evidence_ids,
    extract_tool_evidence_ids,
)


class AgentObservabilityService:
    """读写 Agent 运行观测和评测汇总。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def record_run(
        self,
        *,
        run: Any,
        user_id: str,
        duration_ms: float,
        source: str,
        conversation_id: str | None = None,
        eval_run_id: str | None = None,
        eval_case_id: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> AgentRunObservation:
        """把一次已完成的 AgentChatResponse 保存为结构化观测。"""

        tool_calls = [
            {
                "name": call.tool_name,
                "is_error": bool(call.is_error),
                "recovery_trace": call.recovery_trace,
            }
            for call in run.tool_calls
        ]
        evidence_ids = self.extract_evidence_ids(run.tool_calls)
        usage = getattr(run, "usage", AgentTokenUsage())
        route = getattr(run, "route", None)
        now = datetime.now(timezone.utc)

        model = AgentRunObservationModel(
            user_id=user_id,
            run_id=run.run_id,
            conversation_id=conversation_id,
            source=source,
            eval_run_id=eval_run_id,
            eval_case_id=eval_case_id,
            question=self._bounded_text(run.question, 1200),
            answer_preview=self._bounded_text(run.answer, 2400),
            model=run.used_model,
            stop_reason=run.stop_reason,
            duration_ms=round(max(duration_ms, 0.0), 2),
            round_count=len(run.rounds),
            tool_calls=tool_calls,
            evidence_ids=evidence_ids,
            input_tokens=int(usage.input_tokens),
            output_tokens=int(usage.output_tokens),
            total_tokens=int(usage.total_tokens),
            metrics={
                "tool_error_count": sum(1 for item in tool_calls if item["is_error"]),
                "hook_count": len(run.hooks_trace),
                "recovery_count": len(run.recovery_trace),
                "compaction_count": len(run.context_trace),
                "route_name": getattr(route, "name", "full"),
                "selected_tool_count": len(run.tools_available),
                "prompt_chars": int(run.prompt_trace.total_chars),
                # 复用现有metrics JSON，不增加表或把正文重复写进观测库。
                "report_execution": (run.report_answer_check.execution
                    if getattr(run, "report_answer_check", None) else {}),
                # 与真实 token usage 分开保存；可比较初始/后续 Schema 字符数、搜索
                # 次数和补入的工具，不把一次本地搜索误算成模型调用或金融证据。
                "tool_discovery": (run.tool_discovery.model_dump(mode="json")
                    if getattr(run, "tool_discovery", None) else {}),
                "loop_control": (run.loop_control.model_dump(mode="json")
                    if getattr(run, "loop_control", None) else {}),
                "execution_budget": getattr(run, "execution_budget", {}),
                **(metrics or {}),
            },
            created_at=now,
        )

        with self.database.session() as session, session.begin():
            # 同一个 run_id 只应记录一次。merge 让开发模式下的请求重放保持幂等。
            session.merge(model)

        return self._observation_to_schema(model)

    def recent_runs(
        self,
        *,
        user_id: str,
        limit: int = 20,
    ) -> list[AgentRunObservation]:
        """按时间倒序读取当前用户的最近运行。"""

        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(AgentRunObservationModel)
                    .where(AgentRunObservationModel.user_id == user_id)
                    .order_by(AgentRunObservationModel.created_at.desc())
                    .limit(limit)
                )
            )
        return [self._observation_to_schema(row) for row in rows]

    def summary(self, *, user_id: str, limit: int = 50) -> AgentObservabilitySummary:
        """基于最近一批运行计算轻量质量摘要。"""

        runs = self.recent_runs(user_id=user_id, limit=limit)
        run_count = len(runs)
        total_duration = sum(item.duration_ms for item in runs)
        return AgentObservabilitySummary(
            user_id=user_id,
            run_count=run_count,
            chat_run_count=sum(1 for item in runs if item.source == "chat"),
            eval_run_count=sum(1 for item in runs if item.source == "eval"),
            average_duration_ms=round(total_duration / run_count, 2) if run_count else 0.0,
            total_tokens=sum(item.usage.total_tokens for item in runs),
            tool_error_count=sum(
                int(item.metrics.get("tool_error_count") or 0) for item in runs
            ),
            recent_runs=runs[:8],
        )

    def save_eval_run(self, run: AgentEvalRun) -> AgentEvalRun:
        """保存整套评测的汇总和每个 case 的检查结果。"""

        run.report_metrics = aggregate_report_metrics(run.results)
        model = AgentEvalRunModel(
            user_id=run.user_id,
            id=run.id,
            suite_id=run.suite_id,
            suite_name=run.suite_name,
            status=run.status,
            total_cases=run.total_cases,
            passed_cases=run.passed_cases,
            failed_cases=run.failed_cases,
            score=run.score,
            duration_ms=run.duration_ms,
            total_tokens=run.total_tokens,
            results=[item.model_dump(mode="json") for item in run.results],
            created_at=self._parse_datetime(run.created_at),
        )
        with self.database.session() as session, session.begin():
            session.merge(model)
        return run

    def get_eval_run(self, *, user_id: str, run_id: str) -> AgentEvalRun:
        """按所属用户读取完整评测，不允许通过猜编号读取其他账号的回答。"""
        with self.database.session() as session:
            row = session.scalar(select(AgentEvalRunModel).where(
                AgentEvalRunModel.id == run_id, AgentEvalRunModel.user_id == user_id))
            if row is None:
                raise LookupError("评测记录不存在。")
            return self._eval_run_to_schema(row)

    def review_report_case(self, *, user_id, run_id, case_id, status, note):
        """人工复核单独保存，不改自动分、不改原答案，保留最近20次复核历史。"""
        if status not in {"pending", "accepted", "needs_fix"}:
            raise ValueError("不支持的复核状态。")
        if status == "needs_fix" and not note.strip():
            raise ValueError("标记需修改时请说明问题，方便下一轮定位。")
        with self.database.session() as session:
            # SQLite提前取得写锁；PostgreSQL锁当前行，避免两人审核不同题时互相覆盖。
            if self.database.engine.dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            row = session.scalar(select(AgentEvalRunModel).where(
                AgentEvalRunModel.id == run_id, AgentEvalRunModel.user_id == user_id).with_for_update())
            if row is None or row.suite_id != "fund_report_rag_v1":
                raise LookupError("季报评测记录不存在。")
            results = [dict(item) for item in row.results]
            result = next((item for item in results if item["case_id"] == case_id), None)
            if result is None or not result.get("report_quality"):
                raise LookupError("该场景没有可供复核的回答。")
            quality = dict(result["report_quality"])
            review = {"status": status, "note": note.strip(), "reviewed_by": user_id,
                "reviewed_at": datetime.now(timezone.utc).isoformat()}
            history = list(quality.get("manual_review", {}).get("history", []))
            quality["manual_review"] = {**review, "history": [*history, review][-20:]}
            result["report_quality"] = quality
            row.results = results
            session.commit()
            return self._eval_run_to_schema(row)

    def recent_eval_runs(
        self,
        *,
        user_id: str,
        limit: int = 10,
        suite_id: str | None = None,
    ) -> list[AgentEvalRun]:
        """返回当前用户最近评测；可限定同一版本套件进行可比回归。"""

        statement = select(AgentEvalRunModel).where(
            AgentEvalRunModel.user_id == user_id
        )
        if suite_id:
            statement = statement.where(AgentEvalRunModel.suite_id == suite_id)
        statement = statement.order_by(AgentEvalRunModel.created_at.desc()).limit(limit)
        with self.database.session() as session:
            rows = list(session.scalars(statement))
        return [self._eval_run_to_schema(row) for row in rows]

    @classmethod
    def extract_evidence_ids(cls, tool_calls: list[Any]) -> list[str]:
        """从工具 JSON 中递归提取 evidence_id / event_id。

        工具结果在 AgentToolCall 中是字符串；优先按 JSON 解析，解析失败时再用受限正则
        查找已知 ID 格式。评分器会用这份集合判断回答是否引用了真实工具证据。
        """

        return extract_tool_evidence_ids(tool_calls)

    @classmethod
    def extract_cited_evidence_ids(cls, answer: str) -> list[str]:
        """从最终回答中提取模型实际写出的证据 ID。"""

        return extract_answer_evidence_ids(answer)

    def _observation_to_schema(self, model: AgentRunObservationModel) -> AgentRunObservation:
        return AgentRunObservation(
            run_id=model.run_id,
            user_id=model.user_id,
            conversation_id=model.conversation_id,
            source=model.source,
            eval_run_id=model.eval_run_id,
            eval_case_id=model.eval_case_id,
            question=model.question,
            answer_preview=model.answer_preview,
            model=model.model,
            stop_reason=model.stop_reason,
            duration_ms=model.duration_ms,
            round_count=model.round_count,
            tool_calls=list(model.tool_calls or []),
            evidence_ids=list(model.evidence_ids or []),
            usage=AgentTokenUsage(
                input_tokens=model.input_tokens,
                output_tokens=model.output_tokens,
                total_tokens=model.total_tokens,
            ),
            metrics=dict(model.metrics or {}),
            created_at=self._isoformat(model.created_at),
        )

    def _eval_run_to_schema(self, model: AgentEvalRunModel) -> AgentEvalRun:
        # 聚合指标可以由每个 case 的 JSON 明细重新计算，因此不需要给旧数据库表新增
        # 一批列；历史 v1 记录也能借助 Schema 默认值继续读取。
        results = [
            AgentEvalCaseResult.model_validate(item)
            for item in list(model.results or [])
        ]
        metric_values: dict[str, list[float]] = {}
        for result in results:
            for dimension, score in result.metric_scores.items():
                metric_values.setdefault(dimension, []).append(float(score))
        metric_scores = {
            dimension: round(sum(values) / len(values), 1)
            for dimension, values in metric_values.items()
            if values
        }
        data_profile = next(
            (item.data_profile for item in results if item.data_profile),
            "",
        )
        data_fingerprint = next(
            (item.data_fingerprint for item in results if item.data_fingerprint),
            "",
        )
        # case 列表已经存在 results JSON 中，因此无需为旧数据库新增字段。
        case_ids = [item.case_id for item in results]
        total_cases = len(results)
        return AgentEvalRun(
            id=model.id,
            user_id=model.user_id,
            suite_id=model.suite_id,
            suite_name=model.suite_name,
            status=model.status,
            total_cases=model.total_cases,
            passed_cases=model.passed_cases,
            failed_cases=model.failed_cases,
            score=model.score,
            duration_ms=model.duration_ms,
            total_tokens=model.total_tokens,
            total_tool_calls=sum(item.tool_call_count for item in results),
            average_duration_ms=(
                round(sum(item.duration_ms for item in results) / total_cases, 2)
                if total_cases
                else 0.0
            ),
            average_tokens=(
                round(model.total_tokens / total_cases, 1) if total_cases else 0.0
            ),
            data_profile=data_profile,
            data_fingerprint=data_fingerprint,
            case_ids=case_ids,
            case_set_id=eval_case_set_id(case_ids),
            metric_scores=metric_scores,
            report_metrics=aggregate_report_metrics(results),
            results=results,
            created_at=self._isoformat(model.created_at),
        )

    @staticmethod
    def _bounded_text(value: Any, limit: int) -> str:
        text = str(value or "")
        return text if len(text) <= limit else f"{text[:limit]}\n[已截断]"

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _isoformat(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()


agent_observability_service = AgentObservabilityService()

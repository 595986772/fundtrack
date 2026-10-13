"""使用 LangGraph interrupt 实现组合调整方案的人机协作流程。

现有 ``portfolio_research`` 仍是一键生成客观研究报告。本图是后续独立流程：

1. ``prepare_review``：把已完成研究报告整理成人工确认页面需要的内容；
2. ``human_review``：调用 ``interrupt()`` 暂停并释放 Worker；
3. ``generate_adjustment``：恢复后由 DeepSeek 生成候选目标权重；
4. ``validate_scenarios``：Python 在同一冻结样本上比较基线、渐进和完整方案；
5. ``compose_plan``：生成只读场景报告，绝不执行交易。

恢复必须使用同一个 Background Job ID 作为 thread_id，并把人工输入放进
``Command(resume=...)``。这让 API 或 Worker 重启后仍能从数据库 checkpoint 继续。
"""

from __future__ import annotations

from dataclasses import dataclass
from operator import add
from typing import Annotated, Any, Callable, Literal, Protocol, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from app.services.agent_authorization import check_active_task_authorization, guard_agent_portfolio
from app.services.agent_execution_context import (
    AgentExecutionContext, agent_execution_scope, current_agent_execution_context,
)
from app.services.portfolio_adjustment_generation import (
    PortfolioAdjustmentGenerationError,
    portfolio_adjustment_generation_service,
)
from app.services.portfolio_adjustment_scenario_interpretation import (
    PortfolioAdjustmentScenarioInterpretationError,
    portfolio_adjustment_scenario_interpretation_service,
)
from app.services.portfolio_adjustment_scenarios import (
    portfolio_adjustment_scenario_service,
)
from app.services.portfolio_research_checkpoints import (
    portfolio_workflow_checkpoint_runtime,
)


PortfolioAdjustmentProgress = Callable[[str, str], None]


class PortfolioAdjustmentState(TypedDict, total=False):
    """五个节点持久化共享的业务状态。"""

    run_id: str
    user_id: str
    portfolio_id: str
    source_research_job_id: str
    research_context: dict[str, Any]
    review_request: dict[str, Any]
    user_constraints: dict[str, Any]
    generated_adjustment: dict[str, Any]
    scenario_validation: dict[str, Any]
    plan: dict[str, Any]
    completed_nodes: Annotated[list[str], add]


@dataclass(frozen=True)
class PortfolioAdjustmentContext:
    """本次 Worker 的回调和恢复信息，不写入业务 checkpoint。"""

    progress_callback: PortfolioAdjustmentProgress | None = None
    checkpoint_backend: str = "memory"
    checkpoint_durable: bool = False
    resumed_from_checkpoint: bool = False
    execution_context: AgentExecutionContext | None = None


@dataclass(frozen=True)
class PortfolioAdjustmentGraphOutcome:
    """Graph 返回给 JobRunner 的两种正常结果：暂停或完成。"""

    status: Literal["waiting_input", "completed"]
    result: dict[str, Any]
    completed_node_count: int


class PortfolioAdjustmentGenerator(Protocol):
    """图节点实际使用的最小模型接口，测试可替换为 Fake。"""

    def generate(
        self,
        *,
        research_context: dict[str, Any],
        user_constraints: dict[str, Any],
    ) -> dict[str, Any]: ...

    def fallback(
        self,
        *,
        research_context: dict[str, Any],
        user_constraints: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]: ...


class PortfolioScenarioValidator(Protocol):
    """候选验证节点依赖的纯 Python 接口。"""

    def validate(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        research_context: dict[str, Any],
        generated_adjustment: dict[str, Any],
        user_constraints: dict[str, Any],
    ) -> dict[str, Any]: ...


class PortfolioScenarioExplainer(Protocol):
    """验证完成后，模型只能解释比较事实。"""

    def interpret(self, *, comparison: dict[str, Any]) -> dict[str, Any]: ...

    def fallback(
        self,
        *,
        comparison: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]: ...


class PortfolioAdjustmentGraphService:
    """执行、暂停、恢复并检查组合调整 LangGraph。"""

    GRAPH_VERSION = "portfolio-adjustment-v2"
    NODE_LABELS = {
        "prepare_review": "研究证据与当前权重已整理",
        "human_review": "人工投资约束已确认",
        "generate_adjustment": "DeepSeek 候选权重已生成",
        "validate_scenarios": "冻结样本场景体检已完成",
        "compose_plan": "经过验证的场景比较已生成",
        "compose_cancelled": "用户已停止本次方案生成",
    }

    def __init__(
        self,
        *,
        generator: PortfolioAdjustmentGenerator = (
            portfolio_adjustment_generation_service
        ),
        scenario_validator: PortfolioScenarioValidator = (
            portfolio_adjustment_scenario_service
        ),
        scenario_explainer: PortfolioScenarioExplainer = (
            portfolio_adjustment_scenario_interpretation_service
        ),
        checkpointer: Any | None = None,
        checkpoint_backend: str = "memory",
        checkpoint_durable: bool = False,
    ) -> None:
        self.generator = generator
        self.scenario_validator = scenario_validator
        self.scenario_explainer = scenario_explainer
        self.checkpointer = checkpointer or InMemorySaver()
        self.checkpoint_backend = checkpoint_backend
        self.checkpoint_durable = checkpoint_durable
        self.graph = self._build_graph()

    def _build_graph(self):
        """声明顺序节点，并在人工确认后按 continue/cancel 分流。"""

        builder = StateGraph(
            PortfolioAdjustmentState,
            context_schema=PortfolioAdjustmentContext,
        )
        builder.add_node("prepare_review", self._authorized_node(self._prepare_review))
        builder.add_node("human_review", self._authorized_node(self._human_review))
        builder.add_node("generate_adjustment", self._authorized_node(self._generate_adjustment))
        builder.add_node("validate_scenarios", self._authorized_node(self._validate_scenarios))
        builder.add_node("compose_plan", self._authorized_node(self._compose_plan))
        builder.add_node("compose_cancelled", self._authorized_node(self._compose_cancelled))

        builder.add_edge(START, "prepare_review")
        builder.add_edge("prepare_review", "human_review")
        builder.add_conditional_edges(
            "human_review",
            self._route_after_review,
            {
                "continue": "generate_adjustment",
                "cancel": "compose_cancelled",
            },
        )
        builder.add_edge("generate_adjustment", "validate_scenarios")
        builder.add_edge("validate_scenarios", "compose_plan")
        builder.add_edge("compose_plan", END)
        builder.add_edge("compose_cancelled", END)
        return builder.compile(checkpointer=self.checkpointer)

    @staticmethod
    def _authorized_node(node):
        def invoke(state: PortfolioAdjustmentState, runtime: Runtime[PortfolioAdjustmentContext]):
            context = runtime.context.execution_context or current_agent_execution_context()
            with agent_execution_scope(context):
                guard_agent_portfolio(state["user_id"], state["portfolio_id"], "portfolio:research-run")
                result = node(state, runtime)
                guard_agent_portfolio(state["user_id"], state["portfolio_id"], "portfolio:research-run")
                return result
        return invoke

    def run(
        self,
        *,
        run_id: str,
        user_id: str,
        portfolio_id: str,
        source_research_job_id: str,
        source_report: dict[str, Any] | None = None,
        resume_payload: dict[str, Any] | None = None,
        progress_callback: PortfolioAdjustmentProgress | None = None,
    ) -> PortfolioAdjustmentGraphOutcome:
        """首次运行到 interrupt；收到人工输入后从同一 checkpoint 恢复。"""

        guard_agent_portfolio(user_id, portfolio_id, "portfolio:research-run")
        config = self._graph_config(
            run_id=run_id,
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
        before = self.checkpoint_info(run_id=run_id)
        state = before["state"]

        if before["exists"]:
            expected = {
                "user_id": user_id,
                "portfolio_id": portfolio_id,
                "source_research_job_id": source_research_job_id,
            }
            for key, value in expected.items():
                if state.get(key) != value:
                    raise ValueError(f"组合调整 checkpoint 中的 {key} 不一致。")

        if before["status"] == "completed":
            guard_agent_portfolio(user_id, portfolio_id, "portfolio:research-run")
            return PortfolioAdjustmentGraphOutcome(
                status="completed",
                result=self._decorate_plan(
                    state["plan"],
                    checkpoint=before,
                    resumed_from_checkpoint=True,
                ),
                completed_node_count=before["completed_node_count"],
            )

        # 重复投递一个仍在等待人工输入的 Job 时，只返回原 interrupt，不能重新触发节点。
        if before["status"] == "waiting_input" and resume_payload is None:
            guard_agent_portfolio(user_id, portfolio_id, "portfolio:research-run")
            return self._waiting_outcome(
                run_id=run_id,
                state=state,
                checkpoint=before,
            )

        resumed = before["status"] in {"waiting_input", "resumable"}
        if before["status"] == "waiting_input":
            if resume_payload is None:
                raise ValueError("恢复 interrupt 时缺少人工输入。")
            graph_input: Any = Command(resume=resume_payload)
        elif before["status"] == "resumable":
            # 普通节点异常与人工 interrupt 不同，使用 None 从失败节点重试。
            graph_input = None
        elif before["status"] == "empty":
            if source_report is None:
                raise ValueError("首次生成组合调整方案时缺少研究报告。")
            graph_input = {
                "run_id": run_id,
                "user_id": user_id,
                "portfolio_id": portfolio_id,
                "source_research_job_id": source_research_job_id,
                "research_context": self._compact_research_report(source_report),
                "completed_nodes": [],
            }
        else:
            raise RuntimeError("组合调整 checkpoint 无法恢复。")

        final_state = self.graph.invoke(
            graph_input,
            config=config,
            context=PortfolioAdjustmentContext(
                progress_callback=progress_callback,
                checkpoint_backend=self.checkpoint_backend,
                checkpoint_durable=self.checkpoint_durable,
                resumed_from_checkpoint=resumed,
                execution_context=current_agent_execution_context(),
            ),
        )
        after = self.checkpoint_info(run_id=run_id)
        guard_agent_portfolio(user_id, portfolio_id, "portfolio:research-run")
        if after["status"] == "waiting_input":
            return self._waiting_outcome(
                run_id=run_id,
                state=after["state"],
                checkpoint=after,
            )
        return PortfolioAdjustmentGraphOutcome(
            status="completed",
            result=self._decorate_plan(
                final_state["plan"],
                checkpoint=after,
                resumed_from_checkpoint=resumed,
            ),
            completed_node_count=after["completed_node_count"],
        )

    def checkpoint_info(self, *, run_id: str) -> dict[str, Any]:
        """读取 checkpoint 摘要，并把 Interrupt 对象转换成普通 JSON。"""

        snapshot = self.graph.get_state(
            {"configurable": {"thread_id": run_id}}
        )
        state = dict(snapshot.values)
        checkpoint_id = (
            snapshot.config.get("configurable", {}).get("checkpoint_id")
            if snapshot.config
            else None
        )
        interrupts = [
            {"id": item.id, "value": item.value}
            for item in snapshot.interrupts
        ]
        completed_nodes = list(dict.fromkeys(state.get("completed_nodes", [])))
        exists = bool(state or checkpoint_id)
        if state.get("plan") and not snapshot.next:
            status = "completed"
        elif interrupts:
            status = "waiting_input"
        elif exists and snapshot.next:
            status = "resumable"
        elif exists:
            status = "incomplete"
        else:
            status = "empty"
        return {
            "exists": exists,
            "status": status,
            "backend": self.checkpoint_backend,
            "durable": self.checkpoint_durable,
            "checkpoint_id": checkpoint_id,
            "completed_nodes": completed_nodes,
            "completed_node_count": len(completed_nodes),
            "next_nodes": list(snapshot.next),
            "interrupts": interrupts,
            "state": state,
        }

    def _prepare_review(
        self,
        state: PortfolioAdjustmentState,
        runtime: Runtime[PortfolioAdjustmentContext],
    ) -> dict[str, Any]:
        """节点一：生成前端确认页数据，不调用模型。"""

        context = state["research_context"]
        review_request = {
            "kind": "portfolio_adjustment_review",
            "title": "确认组合调整约束",
            "portfolio_name": context["portfolio_name"],
            "source_headline": context["headline"],
            "source_summary": context["summary"],
            "current_holdings": context["holdings"],
            "risk_snapshot": context["risk_snapshot"],
            "required_fields": [
                "decision",
                "risk_preference",
                "investment_horizon",
                "max_single_adjustment_pct",
                "max_single_fund_weight_pct",
                "max_turnover_pct",
            ],
        }
        self._notify(runtime, "prepare_review")
        return {
            "review_request": review_request,
            "completed_nodes": ["prepare_review"],
        }

    def _human_review(
        self,
        state: PortfolioAdjustmentState,
        runtime: Runtime[PortfolioAdjustmentContext],
    ) -> dict[str, Any]:
        """节点二：暂停；恢复时 Command.resume 的值会从 interrupt() 返回。"""

        # 该节点在恢复时会从头执行，因此 interrupt 之前只读取 State、没有数据库写入
        # 或模型调用等副作用。
        answer = interrupt(state["review_request"])
        constraints = self._validate_resume_payload(answer)
        self._notify(runtime, "human_review")
        return {
            "user_constraints": constraints,
            "completed_nodes": ["human_review"],
        }

    @staticmethod
    def _route_after_review(state: PortfolioAdjustmentState) -> str:
        return str(state["user_constraints"]["decision"])

    def _generate_adjustment(
        self,
        state: PortfolioAdjustmentState,
        runtime: Runtime[PortfolioAdjustmentContext],
    ) -> dict[str, Any]:
        """节点三：只有人工选择继续后才调用 DeepSeek。"""

        try:
            generated = self.generator.generate(
                research_context=state["research_context"],
                user_constraints=state["user_constraints"],
            )
        except PortfolioAdjustmentGenerationError as exc:
            generated = self.generator.fallback(
                research_context=state["research_context"],
                user_constraints=state["user_constraints"],
                reason=str(exc),
            )
        self._notify(runtime, "generate_adjustment")
        return {
            "generated_adjustment": generated,
            "completed_nodes": ["generate_adjustment"],
        }

    def _validate_scenarios(
        self,
        state: PortfolioAdjustmentState,
        runtime: Runtime[PortfolioAdjustmentContext],
    ) -> dict[str, Any]:
        """节点四：Python 先重算候选，模型随后只能解释比较事实。"""

        comparison = self.scenario_validator.validate(
            user_id=state["user_id"],
            portfolio_id=state["portfolio_id"],
            research_context=state["research_context"],
            generated_adjustment=state["generated_adjustment"],
            user_constraints=state["user_constraints"],
        )
        try:
            explanation = self.scenario_explainer.interpret(
                comparison=comparison
            )
        except PortfolioAdjustmentScenarioInterpretationError as exc:
            explanation = self.scenario_explainer.fallback(
                comparison=comparison,
                reason=str(exc),
            )

        # comparison 只含场景摘要、约束状态和指标事实，不含冻结日收益矩阵。
        comparison["interpretation"] = explanation
        self._notify(runtime, "validate_scenarios")
        return {
            "scenario_validation": comparison,
            "completed_nodes": ["validate_scenarios"],
        }

    def _compose_plan(
        self,
        state: PortfolioAdjustmentState,
        runtime: Runtime[PortfolioAdjustmentContext],
    ) -> dict[str, Any]:
        """节点五：把候选、验证结果和解释装配成最终只读方案。"""

        generated = state["generated_adjustment"]
        scenario_validation = state["scenario_validation"]
        explanation = scenario_validation["interpretation"]
        plan = {
            "data_kind": "langgraph_portfolio_adjustment",
            "workflow": "portfolio_adjustment",
            "status": "completed",
            "graph_version": self.GRAPH_VERSION,
            "run_id": state["run_id"],
            "thread_id": state["run_id"],
            "portfolio_id": state["portfolio_id"],
            "portfolio_name": state["research_context"]["portfolio_name"],
            "source_research_job_id": state["source_research_job_id"],
            "source_headline": state["research_context"]["headline"],
            # 最终文案必须发生在 Python 场景重算之后；候选生成阶段的原文只作为
            # 审计信息保留，不能直接冒充“方案已经验证”。
            "headline": explanation["headline"],
            "summary": explanation["summary"],
            "stance": generated["stance"],
            "confidence": generated["confidence"],
            "adjustments": generated["adjustments"],
            "key_tradeoffs": (
                explanation.get("tradeoffs") or generated["key_tradeoffs"]
            ),
            "monitoring_items": list(
                dict.fromkeys(
                    [
                        *explanation.get("follow_up_items", []),
                        *generated["monitoring_items"],
                    ]
                )
            )[:6],
            "scenario_validation": scenario_validation,
            "user_constraints": state["user_constraints"],
            "generation": {
                "status": generated["status"],
                "model_used": generated["model_used"],
                "model": generated["model"],
                "prompt_version": generated["prompt_version"],
                "attempt_count": generated["attempt_count"],
                "fallback_reason": generated.get("fallback_reason"),
                "candidate_headline": generated["headline"],
                "candidate_summary": generated["summary"],
                "scenario_interpretation": {
                    key: explanation.get(key)
                    for key in (
                        "status",
                        "model_used",
                        "model",
                        "prompt_version",
                        "attempt_count",
                        "fallback_reason",
                    )
                },
            },
            "graph_trace": self._base_trace(state, runtime, "compose_plan"),
            "execution": {
                "mode": "research_only",
                "orders_created": 0,
                "auto_trade": False,
            },
            "disclaimer": "候选权重只在冻结历史样本上完成研究比较，不代表未来风险，也未验证真实费用和赎回限制；系统不会自动执行交易。",
        }
        self._notify(runtime, "compose_plan")
        return {"plan": plan, "completed_nodes": ["compose_plan"]}

    def _compose_cancelled(
        self,
        state: PortfolioAdjustmentState,
        runtime: Runtime[PortfolioAdjustmentContext],
    ) -> dict[str, Any]:
        """用户拒绝后正常结束图，不调用模型，也不生成任何权重变化。"""

        holdings = state["research_context"]["holdings"]
        plan = {
            "data_kind": "langgraph_portfolio_adjustment",
            "workflow": "portfolio_adjustment",
            "status": "cancelled_by_user",
            "graph_version": self.GRAPH_VERSION,
            "run_id": state["run_id"],
            "thread_id": state["run_id"],
            "portfolio_id": state["portfolio_id"],
            "portfolio_name": state["research_context"]["portfolio_name"],
            "source_research_job_id": state["source_research_job_id"],
            "headline": "已停止生成组合调整方案",
            "summary": "用户在人工确认节点选择停止，DeepSeek 未被调用，当前权重没有变化。",
            "stance": "maintain",
            "confidence": 1.0,
            "adjustments": [
                {
                    "fund_code": item["fund_code"],
                    "short_name": item["short_name"],
                    "action": "hold",
                    "current_weight_pct": item["weight_pct"],
                    "target_weight_pct": item["weight_pct"],
                    "change_pct_points": 0.0,
                    "rationale": "用户停止了本次方案生成。",
                    "conditions": [],
                    "evidence_ids": [],
                }
                for item in holdings
            ],
            "key_tradeoffs": [],
            "monitoring_items": [],
            "scenario_validation": {
                "status": "skipped",
                "execution_ready": False,
                "scenarios": [],
                "comparison_facts": [],
            },
            "user_constraints": state["user_constraints"],
            "generation": {
                "status": "skipped",
                "model_used": False,
                "model": None,
                "prompt_version": None,
                "attempt_count": 0,
                "fallback_reason": None,
            },
            "graph_trace": self._base_trace(
                state,
                runtime,
                "compose_cancelled",
            ),
            "execution": {
                "mode": "research_only",
                "orders_created": 0,
                "auto_trade": False,
            },
            "disclaimer": "本次流程已由用户停止，没有执行交易。",
        }
        self._notify(runtime, "compose_cancelled")
        return {"plan": plan, "completed_nodes": ["compose_cancelled"]}

    def _waiting_outcome(
        self,
        *,
        run_id: str,
        state: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> PortfolioAdjustmentGraphOutcome:
        """把 LangGraph Interrupt 转成 Job result 中可序列化的等待信息。"""

        interrupt_info = checkpoint["interrupts"][0]
        return PortfolioAdjustmentGraphOutcome(
            status="waiting_input",
            completed_node_count=checkpoint["completed_node_count"],
            result={
                "data_kind": "langgraph_portfolio_adjustment",
                "workflow": "portfolio_adjustment",
                "status": "waiting_input",
                "graph_version": self.GRAPH_VERSION,
                "run_id": run_id,
                "thread_id": run_id,
                "portfolio_id": state["portfolio_id"],
                "source_research_job_id": state["source_research_job_id"],
                "interrupt": interrupt_info,
                "graph_trace": {
                    "completed_nodes": checkpoint["completed_nodes"],
                    "next_nodes": checkpoint["next_nodes"],
                    "checkpoint_backend": self.checkpoint_backend,
                    "checkpoint_durable": self.checkpoint_durable,
                    "checkpoint_status": "waiting_input",
                    "checkpoint_id": checkpoint["checkpoint_id"],
                },
            },
        )

    def _decorate_plan(
        self,
        plan: dict[str, Any],
        *,
        checkpoint: dict[str, Any],
        resumed_from_checkpoint: bool,
    ) -> dict[str, Any]:
        result = dict(plan)
        trace = dict(result.get("graph_trace") or {})
        trace.update(
            {
                "checkpoint_backend": self.checkpoint_backend,
                "checkpoint_durable": self.checkpoint_durable,
                "checkpoint_status": checkpoint["status"],
                "checkpoint_id": checkpoint["checkpoint_id"],
                "resumed_from_checkpoint": resumed_from_checkpoint,
            }
        )
        result["graph_trace"] = trace
        return result

    def _base_trace(
        self,
        state: PortfolioAdjustmentState,
        runtime: Runtime[PortfolioAdjustmentContext],
        final_node: str,
    ) -> dict[str, Any]:
        return {
            "completed_nodes": [
                *state.get("completed_nodes", []),
                final_node,
            ],
            "checkpoint_backend": runtime.context.checkpoint_backend,
            "checkpoint_durable": runtime.context.checkpoint_durable,
            "resumed_from_checkpoint": runtime.context.resumed_from_checkpoint,
            "human_in_the_loop": True,
        }

    def _notify(
        self,
        runtime: Runtime[PortfolioAdjustmentContext],
        node_name: str,
    ) -> None:
        callback = runtime.context.progress_callback
        check_active_task_authorization()
        if callback is not None:
            callback(node_name, self.NODE_LABELS[node_name])
        check_active_task_authorization()

    def _graph_config(
        self,
        *,
        run_id: str,
        user_id: str,
        portfolio_id: str,
    ) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": run_id},
            "tags": ["fundscope", "portfolio-adjustment", "hitl"],
            "metadata": {
                "user_id": user_id,
                "portfolio_id": portfolio_id,
                "graph_version": self.GRAPH_VERSION,
            },
        }

    @staticmethod
    def _validate_resume_payload(value: Any) -> dict[str, Any]:
        """API 已做 Pydantic 校验，图内再保留可独立调用的最小边界。"""

        if not isinstance(value, dict):
            raise ValueError("人工确认内容必须是 JSON 对象。")
        decision = str(value.get("decision") or "").strip().lower()
        risk = str(value.get("risk_preference") or "").strip().lower()
        horizon = str(value.get("investment_horizon") or "").strip().lower()
        max_change = float(value.get("max_single_adjustment_pct") or 0)
        max_weight_value = value.get("max_single_fund_weight_pct", 70)
        max_turnover_value = value.get("max_turnover_pct", 20)
        max_weight = float(70 if max_weight_value is None else max_weight_value)
        max_turnover = float(
            20 if max_turnover_value is None else max_turnover_value
        )
        constraints = str(value.get("additional_constraints") or "").strip()
        if decision not in {"continue", "cancel"}:
            raise ValueError("人工确认 decision 非法。")
        if risk not in {"conservative", "balanced", "growth"}:
            raise ValueError("人工确认 risk_preference 非法。")
        if horizon not in {"under_1y", "one_to_three_years", "over_3y"}:
            raise ValueError("人工确认 investment_horizon 非法。")
        if max_change < 1 or max_change > 50:
            raise ValueError("人工确认的最大调整幅度必须在 1-50 之间。")
        if max_weight < 10 or max_weight > 100:
            raise ValueError("人工确认的单只最终权重上限必须在 10-100 之间。")
        if max_turnover < 0 or max_turnover > 100:
            raise ValueError("人工确认的换手率上限必须在 0-100 之间。")
        if len(constraints) > 1000:
            raise ValueError("人工补充约束最多 1000 个字符。")
        return {
            "decision": decision,
            "risk_preference": risk,
            "investment_horizon": horizon,
            "max_single_adjustment_pct": round(max_change, 2),
            "max_single_fund_weight_pct": round(max_weight, 2),
            "max_turnover_pct": round(max_turnover, 2),
            "additional_constraints": constraints,
        }

    @staticmethod
    def _compact_research_report(report: dict[str, Any]) -> dict[str, Any]:
        """只把生成方案真正需要的研究字段写进 checkpoint。"""

        assessment = report.get("assessment_snapshot") or {}
        quant_risk = report.get("quant_risk_snapshot") or {}
        interpretation = report.get("model_interpretation") or {}
        holdings = [
            {
                "fund_code": str(item["fund_code"]),
                "short_name": str(item["short_name"]),
                "weight_pct": float(item["weight_pct"]),
            }
            for item in assessment.get("fund_contributions", [])
        ]
        if not holdings:
            raise ValueError("研究报告缺少组合持仓权重。")
        return {
            "portfolio_name": report["portfolio_name"],
            "period_months": report["period_months"],
            "headline": report["headline"],
            "summary": report["summary"],
            "holdings": holdings,
            "risk_snapshot": {
                "state_code": assessment.get("state_code"),
                "state_label": assessment.get("state_label"),
                "annualized_volatility_pct": quant_risk.get(
                    "annualized_volatility_pct"
                ),
                "max_drawdown_pct": (
                    quant_risk.get("max_drawdown") or {}
                ).get("drawdown_pct"),
                "daily_cvar_95_pct": quant_risk.get("daily_cvar_95_pct"),
                "top_risk_contributor": quant_risk.get(
                    "top_risk_contributor"
                ),
            },
            "market_snapshot": {
                key: (report.get("market_snapshot") or {}).get(key)
                for key in ("data_date", "state_code", "state_label", "summary")
            },
            "key_findings": [
                {
                    "tone": item.get("tone"),
                    "title": item.get("title"),
                    "detail": item.get("detail"),
                    "evidence_ids": list(item.get("evidence_ids", [])),
                    "metric_references": list(
                        item.get("metric_references", [])
                    ),
                    "fund_codes": list(item.get("fund_codes", [])),
                }
                for item in report.get("key_findings", [])[:6]
            ],
            "conflicts": list(interpretation.get("conflicts", []))[:4],
            "uncertainties": list(interpretation.get("uncertainties", []))[:6],
            "conditional_view": interpretation.get("conditional_view"),
            "limitations": list(report.get("limitations", []))[:8],
            "research_snapshot": {
                key: (report.get("research_snapshot") or {}).get(key)
                for key in (
                    "snapshot_id",
                    "content_hash",
                    "captured_at",
                    "graph_version",
                )
            },
        }


portfolio_adjustment_graph_service = PortfolioAdjustmentGraphService(
    checkpointer=portfolio_workflow_checkpoint_runtime.checkpointer,
    checkpoint_backend=portfolio_workflow_checkpoint_runtime.backend,
    checkpoint_durable=portfolio_workflow_checkpoint_runtime.durable,
)

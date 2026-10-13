"""使用 LangGraph 编排一次组合研究工作流。

这个文件负责“冻结什么、先做什么、哪些步骤可以并行、结果如何汇总”。首次执行先
按计划调用原有 Service 生成结构化输入，季报补查完成后保存为不可变 ResearchInputSnapshot；
LangGraph 节点随后只消费同一个 snapshot_id，因此断点恢复不会混入最新数据库值。
金融算法仍留在原 Service 中，这里不复制公式。

综合模式包含八个节点：

1. ``load_portfolio``：读取用户保存的模拟组合；
2. ``calculate_assessment``：复用现有组合确定性研判；
3. ``calculate_quant_risk``：使用 Riskfolio-Lib 计算真实净值序列风险；
4. ``calculate_performance``：读取交易调整业绩、基准和收益归因；
5. ``load_market_context``：读取市场情绪和与持仓相关的事件；
6. ``load_decision_history``：读取冻结决策快照和动态观察结果；
7. ``interpret_with_llm``：让模型对共享输入做结构化语义综合；
8. ``compose_report``：把已校验的模型结果和确定性快照整理成研究报告。

节点 1 到 6 将已冻结分区写入图 State，其中节点 2 到 6 并行执行，随后在节点 7 汇合。
模型不可用或输出越界时，节点 7 会生成确定性回退结果，因此一次外部模型故障不会让
整个研究 Job 丢失。

显式选择 committee 时，第七节点改为模式选择，再进入五个角色阶段：两位研究员
独立分析、双方各回应一次、综合者汇总。全部节点仍共享同一快照与持久化检查点。

配置解释和季报比较使用四节点路径：读取组合、消费按需冻结的输入、模型解释、生成报告。
前者不读取市场/季报，后者读取两期季报但不读取当日市场和历史决策；两者都保留数值基线。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from operator import add
from typing import Annotated, Any, Callable, Protocol, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.services.agent_authorization import check_active_task_authorization, guard_agent_portfolio
from app.services.agent_execution_context import (
    AgentExecutionContext, agent_execution_scope, current_agent_execution_context,
)
from app.services.market_events import market_event_service
from app.services.market_sentiment import market_sentiment_service
from app.services.portfolio_assessment import portfolio_assessment_service
from app.services.portfolio_decisions import portfolio_decision_service
from app.services.portfolio_performance import portfolio_performance_service
from app.services.portfolio_research_checkpoints import (
    portfolio_workflow_checkpoint_runtime,
)
from app.services.portfolio_research_interpretation import (
    PortfolioResearchInterpretationError,
    portfolio_research_interpretation_service,
)
from app.services.portfolio_research_committee import (
    PortfolioResearchCommitteeService,
    portfolio_research_committee_service,
)
from app.services.portfolio_report_evidence import portfolio_report_evidence_service
from app.services.portfolio_research_plan import build_research_plan
from app.services.portfolio_research_repair import portfolio_research_repair_service
from app.services.portfolio_research_snapshots import (
    InMemoryPortfolioResearchSnapshotStore,
    PortfolioResearchSnapshotError,
    PortfolioResearchSnapshotStore,
    portfolio_research_snapshot_service,
    public_snapshot_metadata,
)
from app.services.portfolio_risk_engine import portfolio_risk_engine
from app.services.portfolios import portfolio_service
from app.services.tracing import capture_trace_context, current_trace_id, trace_span, traced


# Background Job 会在每个节点完成后收到一次通知，并把它转换成前端可见的进度。
PortfolioResearchProgress = Callable[[str, str], None]


class PortfolioResearchState(TypedDict, total=False):
    """八个节点共享的状态对象，相当于这条工作流的结构化工作台。

    ``Annotated[..., add]`` 告诉 LangGraph：并行节点同时返回 completed_nodes 时应当把
    两个列表相加，而不是让后完成的节点覆盖先完成的节点。
    """

    run_id: str
    user_id: str
    portfolio_id: str
    months: int
    # State 只保存快照引用和公开清单；完整 payload 独立保存在 SQL 快照表中。
    input_snapshot: dict[str, Any]
    portfolio: dict[str, Any]
    assessment: dict[str, Any]
    quant_risk: dict[str, Any]
    performance_snapshot: dict[str, Any]
    market_context: dict[str, Any]
    decision_history: dict[str, Any]
    interpretation: dict[str, Any]
    report: dict[str, Any]
    completed_nodes: Annotated[list[str], add]
    # 旧任务缺少此字段时走轻量模式；每个角色有独立 channel，便于并行和断点恢复。
    research_mode: str
    research_plan: dict[str, Any]
    committee_opportunity: dict[str, Any]
    committee_risk: dict[str, Any]
    committee_opportunity_review: dict[str, Any]
    committee_risk_review: dict[str, Any]
    committee: dict[str, Any]


@dataclass(frozen=True)
class PortfolioResearchContext:
    """不应写入 checkpoint 的运行时依赖。

    progress_callback 是 Python 函数，不能当业务数据保存进 State。LangGraph 的
    ``context`` 专门用来向节点传递这类只在本次运行期间存在的对象。
    """

    progress_callback: PortfolioResearchProgress | None = None
    # 首次运行和恢复都会按 snapshot_id 从持久化 Store 读取一次。节点只消费这里的
    # 冻结副本，不再各自读取“数据库当前值”。
    snapshot_payload: dict[str, Any] | None = None
    # 这些字段只描述“本次执行”，不属于金融业务 State。compose_report 会把它们
    # 写进轻量 graph_trace，方便 API 和前端说明是否发生过断点恢复。
    checkpoint_backend: str = "memory"
    checkpoint_durable: bool = False
    resumed_from_checkpoint: bool = False
    resume_checkpoint_id: str | None = None
    execution_context: AgentExecutionContext | None = None
    # 只用于本次执行的跨线程追踪，不进入金融快照或 Checkpoint 业务状态。
    trace_context: dict[str, Any] | None = None


class PortfolioReader(Protocol):
    """图节点读取组合时真正需要的最小接口，方便测试注入 Fake Service。"""

    def get(self, *, user_id: str, portfolio_id: str) -> dict[str, Any]: ...


class PortfolioAssessor(Protocol):
    """组合研判节点依赖的最小接口。"""

    def assess(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        months: int,
    ) -> dict[str, Any]: ...


class PortfolioRiskAnalyzer(Protocol):
    """量化风险节点依赖的最小接口。"""

    def analyze(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        months: int,
    ) -> dict[str, Any]: ...


class PortfolioPerformanceAnalyzer(Protocol):
    """组合业绩节点只依赖汇总分析接口。"""

    def analyze(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        months: int,
    ) -> dict[str, Any]: ...


class PortfolioDecisionReader(Protocol):
    """决策节点只读当前用户一份组合的历史记录。"""

    def list(
        self,
        *,
        user_id: str,
        portfolio_id: str,
    ) -> list[dict[str, Any]]: ...


class MarketAnalyzer(Protocol):
    """市场上下文节点读取量化市场状态时需要的接口。"""

    def analyze(self) -> dict[str, Any]: ...


class EventSearcher(Protocol):
    """市场上下文节点检索事件时需要的接口。"""

    def search(self, *, limit: int = 20, **kwargs: Any) -> dict[str, Any]: ...


class PortfolioResearchInterpreter(Protocol):
    """语义汇合节点只依赖模型研判和确定性回退两个动作。"""

    def interpret(
        self,
        *,
        portfolio: dict[str, Any],
        assessment: dict[str, Any],
        quant_risk: dict[str, Any],
        performance_snapshot: dict[str, Any],
        market_context: dict[str, Any],
        decision_history: dict[str, Any],
    ) -> dict[str, Any]: ...

    def fallback(
        self,
        *,
        portfolio: dict[str, Any],
        assessment: dict[str, Any],
        quant_risk: dict[str, Any],
        performance_snapshot: dict[str, Any],
        market_context: dict[str, Any],
        decision_history: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]: ...


class PortfolioResearchGraphService:
    """构建并执行 FundScope 的 LangGraph 组合研究图。"""

    GRAPH_VERSION = "portfolio-research-v10"
    NODE_LABELS = {
        "load_portfolio": "组合数据已读取",
        "load_focused_inputs": "当前问题所需的指标与证据已整理",
        "calculate_assessment": "组合规则证据已计算",
        "calculate_quant_risk": "组合量化风险已计算",
        "calculate_performance": "组合历史业绩与归因已计算",
        "load_market_context": "市场情绪与相关事件已整理",
        "load_decision_history": "历史决策与后续结果已整理",
        "interpret_with_llm": "DeepSeek 综合研判已完成",
        "compose_report": "组合研究报告已生成",
        "committee_opportunity": "机会研究员独立分析已结束",
        "committee_risk": "风险研究员独立分析已结束",
        "committee_opportunity_review": "机会研究员回应已结束",
        "committee_risk_review": "风险研究员回应已结束",
        "committee_synthesis": "多角色综合研判已结束",
    }

    def __init__(
        self,
        *,
        portfolios: PortfolioReader = portfolio_service,
        assessments: PortfolioAssessor = portfolio_assessment_service,
        quant_risk: PortfolioRiskAnalyzer = portfolio_risk_engine,
        performance: PortfolioPerformanceAnalyzer = portfolio_performance_service,
        decisions: PortfolioDecisionReader = portfolio_decision_service,
        market_sentiment: MarketAnalyzer = market_sentiment_service,
        market_events: EventSearcher = market_event_service,
        interpreter: PortfolioResearchInterpreter = (
            portfolio_research_interpretation_service
        ),
        snapshots: PortfolioResearchSnapshotStore | None = None,
        checkpointer: Any | None = None,
        checkpoint_backend: str = "memory",
        checkpoint_durable: bool = False,
        committee: PortfolioResearchCommitteeService = portfolio_research_committee_service,
        report_evidence=None,
        repair=portfolio_research_repair_service,
    ) -> None:
        # 业务 Service 通过构造函数注入。单元测试可以替换为固定返回值，不访问真实数据库。
        self.portfolios = portfolios
        self.assessments = assessments
        self.quant_risk = quant_risk
        self.performance = performance
        self.decisions = decisions
        self.market_sentiment = market_sentiment
        self.market_events = market_events
        self.interpreter = interpreter
        self.committee = committee
        # 应用级实例显式注入；隔离测试不应因默认参数偷偷读取产品数据库或访问模型。
        self.report_evidence = report_evidence
        self.repair = repair
        # 普通构造默认用内存 Store，避免单元测试污染项目数据库；文件末尾的应用级
        # 实例会显式注入 SQL Store，Worker 重启后仍能读取同一份快照。
        self.snapshots = snapshots or InMemoryPortfolioResearchSnapshotStore()

        # 测试不传 checkpointer 时仍使用隔离的内存实现；应用级实例会在文件末尾显式
        # 注入 SQLite / PostgreSQL saver，避免不同测试共享历史 thread。
        self.checkpointer = checkpointer or InMemorySaver()
        self.checkpoint_backend = checkpoint_backend
        self.checkpoint_durable = checkpoint_durable
        self.graph = self._build_graph()

    def _build_graph(self):
        """声明节点和边，然后编译为可执行图。"""

        builder = StateGraph(
            PortfolioResearchState,
            context_schema=PortfolioResearchContext,
        )
        builder.add_node("load_portfolio", self._authorized_node(self._load_portfolio))
        builder.add_node("calculate_assessment", self._authorized_node(self._calculate_assessment))
        builder.add_node("calculate_quant_risk", self._authorized_node(self._calculate_quant_risk))
        builder.add_node("calculate_performance", self._authorized_node(self._calculate_performance))
        builder.add_node("load_market_context", self._authorized_node(self._load_market_context))
        builder.add_node("load_decision_history", self._authorized_node(self._load_decision_history))
        builder.add_node("interpret_with_llm", self._authorized_node(self._interpret_with_llm))
        builder.add_node("compose_report", self._authorized_node(self._compose_report))
        builder.add_node("load_focused_inputs", self._authorized_node(self._load_focused_inputs))
        # 角色直接作为父图节点，而不是在一个节点里手写五次调用。这样现有租约隔离、
        # pending writes 迁移可以保留成功的并行分支，不必引入第二套子图恢复协议。
        for role in ("opportunity", "risk"):
            builder.add_node(f"committee_{role}", self._authorized_node(partial(self._committee_role, role=role), f"committee_{role}"))
            builder.add_node(f"committee_{role}_review", self._authorized_node(partial(self._committee_role, role=role, review=True), f"committee_{role}_review"))
        builder.add_node("committee_synthesis", self._authorized_node(self._committee_synthesis))

        builder.add_edge(START, "load_portfolio")
        # 组合读取完成后，五路只读数据互不依赖，可以并行执行。
        builder.add_conditional_edges("load_portfolio", lambda state:
            ["load_focused_inputs"] if state.get("research_mode") in {"explain", "strategy"}
            else ["calculate_assessment", "calculate_quant_risk", "calculate_performance",
                  "load_market_context", "load_decision_history"])
        builder.add_edge("load_focused_inputs", "interpret_with_llm")
        # 列表形式表示 fan-in：五个并行节点都完成后，模型才能看到完整上下文。
        builder.add_edge(
            [
                "calculate_assessment",
                "calculate_quant_risk",
                "calculate_performance",
                "load_market_context",
                "load_decision_history",
            ],
            "interpret_with_llm",
        )
        builder.add_conditional_edges("interpret_with_llm", self._route_interpretation)
        # 两方独立分析都保存成功后才相互看见观点。双方回应也在同一轮并行完成。
        for role in ("opportunity", "risk"):
            builder.add_edge(["committee_opportunity", "committee_risk"], f"committee_{role}_review")
        builder.add_edge(["committee_opportunity_review", "committee_risk_review"], "committee_synthesis")
        builder.add_edge("committee_synthesis", "compose_report")
        builder.add_edge("compose_report", END)
        return builder.compile(checkpointer=self.checkpointer)

    @staticmethod
    def _authorized_node(node, name=None):
        name = name or node.__name__.lstrip("_")
        def invoke(state: PortfolioResearchState, runtime: Runtime[PortfolioResearchContext]):
            # 同步节点可能换线程执行，不能把上下文传播寄托在框架的默认行为上。
            context = runtime.context.execution_context or current_agent_execution_context()
            with agent_execution_scope(context), trace_span(
                f"research.{name}", carrier=runtime.context.trace_context,
                attributes={"langfuse.observation.type": "chain", "research.node": name},
            ) as span:
                with trace_span("research.authorize", attributes={"langfuse.observation.type": "guardrail"}):
                    guard_agent_portfolio(state["user_id"], state["portfolio_id"], "portfolio:research-run")
                result = node(state, runtime)
                guard_agent_portfolio(state["user_id"], state["portfolio_id"], "portfolio:research-run")
                # 业务已降级也要可见，但绝不把报告正文当观测属性上传。
                for section in ("interpretation", name):
                    status = (result.get(section) or {}).get("status")
                    if status in {"accepted", "generated", "fallback", "completed", "failed",
                                  "limited", "unavailable", "degraded", "skipped"}:
                        span.set_attribute("research.result_status", status)
                return result
        return invoke

    @traced("research.workflow", **{"research.graph_version": GRAPH_VERSION})
    def run(
        self,
        *,
        run_id: str,
        user_id: str,
        portfolio_id: str,
        months: int = 12,
        progress_callback: PortfolioResearchProgress | None = None,
        checkpoint_thread_id: str | None = None,
        daily_briefing: dict[str, Any] | None = None,
        research_mode: str = "standard",
        research_plan: dict[str, Any] | None = None,
        repair_previous: dict[str, Any] | None = None,
        repair_checkpoint: Callable | None = None,
    ) -> dict[str, Any]:
        """首次执行从 START 开始，失败重试则从最后 checkpoint 继续。"""

        guard_agent_portfolio(user_id, portfolio_id, "portfolio:research-run")
        if months not in {6, 12, 36}:
            raise ValueError("组合研究区间只能是 6、12 或 36 个月。")
        # 后台任务传入创建时的计划。校验后原样保存，重试不重新猜用户意图。
        plan = research_plan or build_research_plan(mode=research_mode)
        if research_plan is not None and plan != build_research_plan(plan.get("question", ""),
                plan.get("requested_mode", "standard"), allow_public_refresh=plan.get("allow_public_refresh", False),
                investor_context=plan.get("investor_context")):
            raise ValueError("研究计划版本或内容不受支持，请创建新任务。")
        research_mode = plan["mode"]
        if daily_briefing is not None and research_mode != "standard":
            raise ValueError("每日简报只使用轻量综合模式。")

        config = self._graph_config(
            run_id=run_id,
            user_id=user_id,
            portfolio_id=portfolio_id,
            checkpoint_thread_id=checkpoint_thread_id,
        )
        before = self.checkpoint_info(run_id=run_id, checkpoint_thread_id=checkpoint_thread_id)
        existing_state = before["state"]

        if before["exists"]:
            if existing_state.get("research_mode", "standard") != research_mode:
                raise ValueError("不能在恢复已有 checkpoint 时切换研究模式。")
            saved_plan = existing_state.get("research_plan") or build_research_plan(mode=research_mode)
            if saved_plan != plan:
                raise ValueError("不能在恢复已有 checkpoint 时修改研究问题或计划。")
            # Job ID 理论上全局唯一。这里再核对一次 checkpoint 身份，防止调用者误用
            # 同一个 thread_id 去运行另一位用户或另一份组合。
            expected_identity = {
                "user_id": user_id,
                "portfolio_id": portfolio_id,
                "months": months,
            }
            for key, expected in expected_identity.items():
                if existing_state.get(key) != expected:
                    raise ValueError(
                        f"checkpoint 中的 {key} 与本次组合研究请求不一致。"
                    )

        # Worker 可能在“图已经完成、Job 完成状态还没写回”之间退出。重试时直接读取
        # checkpoint 中的报告即可，不能再次调用业务 Service 或 DeepSeek。
        if before["status"] == "completed":
            completed_snapshot = existing_state.get("input_snapshot")
            if isinstance(completed_snapshot, dict):
                # 新版报告即使已经完成，也先校验外部快照仍存在且哈希一致。旧版报告
                # 没有该字段时保持只读兼容，不伪造后来才出现的审计信息。
                self._load_snapshot(
                    metadata=completed_snapshot,
                    run_id=run_id,
                    user_id=user_id,
                    portfolio_id=portfolio_id,
                    months=months,
                )
            guard_agent_portfolio(user_id, portfolio_id, "portfolio:research-run")
            return self._decorate_report(
                existing_state["report"],
                checkpoint=before,
                resumed_from_checkpoint=True,
            )

        resumed = before["status"] == "resumable"
        if before["exists"] and not resumed:
            raise RuntimeError("已有 checkpoint 无可恢复节点，也没有最终报告。")

        if resumed:
            snapshot_metadata = existing_state.get("input_snapshot")
            if not isinstance(snapshot_metadata, dict):
                # 旧版 checkpoint 没有快照引用，直接恢复会继续混用新旧数据。与其静默
                # 生成一份看似可复现的报告，不如要求创建新的研究 Job。
                raise PortfolioResearchSnapshotError(
                    "该 checkpoint 来自未冻结输入的旧版研究，请重新创建研究任务。"
                )
            snapshot = self._load_snapshot(
                metadata=snapshot_metadata,
                run_id=run_id,
                user_id=user_id,
                portfolio_id=portfolio_id,
                months=months,
            )
        else:
            snapshot = self._get_or_capture_snapshot(
                run_id=run_id,
                user_id=user_id,
                portfolio_id=portfolio_id,
                months=months,
                daily_briefing=daily_briefing,
                research_plan=plan,
                repair_previous=repair_previous,
                repair_checkpoint=repair_checkpoint,
            )
            snapshot_metadata = public_snapshot_metadata(snapshot)

        frozen_plan = snapshot["payload"].get("research_plan")
        if frozen_plan is not None and frozen_plan != plan:
            raise ValueError("当前研究计划与已冻结输入不一致，请创建新任务。")

        initial_state: PortfolioResearchState = {
            "run_id": run_id,
            "user_id": user_id,
            "portfolio_id": portfolio_id,
            "months": months,
            "input_snapshot": snapshot_metadata,
            "completed_nodes": [],
            "research_mode": research_mode,
            "research_plan": plan,
        }
        # LangGraph 的恢复语义不是重新提交 initial_state，而是给同一 thread_id 传 None。
        # 它会载入最后一个成功 super-step，并只调度尚未完成或曾失败的节点。
        graph_input = None if resumed else initial_state
        final_state = self.graph.invoke(
            graph_input,
            config=config,
            context=PortfolioResearchContext(
                progress_callback=progress_callback,
                snapshot_payload=deepcopy(snapshot["payload"]),
                checkpoint_backend=self.checkpoint_backend,
                checkpoint_durable=self.checkpoint_durable,
                resumed_from_checkpoint=resumed,
                resume_checkpoint_id=before["checkpoint_id"],
                execution_context=current_agent_execution_context(),
                trace_context=capture_trace_context(),
            ),
        )
        after = self.checkpoint_info(run_id=run_id, checkpoint_thread_id=checkpoint_thread_id)
        guard_agent_portfolio(user_id, portfolio_id, "portfolio:research-run")
        return self._decorate_report(
            final_state["report"],
            checkpoint=after,
            resumed_from_checkpoint=resumed,
        )

    @traced("research.snapshot")
    def _get_or_capture_snapshot(
        self,
        *,
        run_id: str,
        user_id: str,
        portfolio_id: str,
        months: int,
        daily_briefing: dict[str, Any] | None = None,
        research_plan: dict[str, Any] | None = None,
        repair_previous: dict[str, Any] | None = None,
        repair_checkpoint: Callable | None = None,
    ) -> dict[str, Any]:
        """首次运行并行采集五路输入；崩溃重投时直接复用已经落库的版本。"""

        guard_agent_portfolio(user_id, portfolio_id, "portfolio:research-run")
        existing = self.snapshots.get_for_run(user_id=user_id, run_id=run_id)
        if existing is not None:
            return self._load_snapshot(
                metadata=public_snapshot_metadata(existing),
                run_id=run_id,
                user_id=user_id,
                portfolio_id=portfolio_id,
                months=months,
            )

        capture_started_at = datetime.now(timezone.utc)
        plan = research_plan or build_research_plan()
        portfolio = self.portfolios.get(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )

        # 这里仍然保留原来的并行优势，只是把并行结果先收进一个不可变输入包。图中
        # 后续节点负责显式消费对应分区、写 checkpoint 和上报八阶段进度。
        with ThreadPoolExecutor(
            max_workers=6,
            thread_name_prefix="portfolio-snapshot",
        ) as executor:
            def submit(section, operation, *args, **kwargs):
                def guarded():
                    with trace_span(f"research.retrieve.{section}", attributes={"langfuse.observation.type": "retriever"}):
                        guard_agent_portfolio(user_id, portfolio_id, "portfolio:research-run")
                        result = operation(*args, **kwargs)
                        guard_agent_portfolio(user_id, portfolio_id, "portfolio:research-run")
                        return result
                # 每次提交都复制一份，同一个 Context 不能被多个线程同时进入。
                return executor.submit(copy_context().run, guarded)

            assessment_future = submit(
                "assessment",
                self.assessments.assess,
                user_id=user_id,
                portfolio_id=portfolio_id,
                months=months,
            )
            # 生产风险引擎会额外冻结场景比较所需的日收益矩阵。测试 Fake 或旧适配器
            # 没有该方法时仍调用普通 analyze，保持依赖契约向后兼容。
            risk_analyzer = getattr(
                self.quant_risk,
                "analyze_for_research_snapshot",
                self.quant_risk.analyze,
            )
            risk_future = submit(
                "quant_risk",
                risk_analyzer,
                user_id=user_id,
                portfolio_id=portfolio_id,
                months=months,
            )
            performance_future = submit(
                "performance",
                self.performance.analyze,
                user_id=user_id,
                portfolio_id=portfolio_id,
                months=months,
            )
            market_future = submit(
                "market",
                self._capture_market_context,
                portfolio,
            ) if "market" in plan["sources"] else None
            decisions_future = submit(
                "decisions",
                self.decisions.list,
                user_id=user_id,
                portfolio_id=portfolio_id,
            ) if "decisions" in plan["sources"] else None
            # 普通组合研究增加一份季报证据，角色不再各自检索。每日盘中简报仍只
            # 使用用户启用的当日刷新范围，不能因本次升级暗中增加季报模型费用。
            target_codes = [row["fund_code"] for row in portfolio.get("holdings", [])
                if row["fund_code"] in plan["question"] or (row.get("short_name") and row["short_name"] in plan["question"])]
            reports_future = (submit("fund_reports", self.report_evidence.capture, portfolio,
                question=plan["question"], fund_codes=target_codes or None)
                if self.report_evidence is not None and daily_briefing is None
                and "fund_reports" in plan["sources"] else None)

            assessment = assessment_future.result()
            quant_risk = risk_future.result()
            raw_performance = performance_future.result()
            market_context = market_future.result() if market_future else {"status": "not_requested",
                "state_code": "not_requested", "state_label": "本轮未读取", "summary": "本轮未额外读取市场数据。"}
            raw_decisions = decisions_future.result() if decisions_future else []
            if reports_future is not None:
                market_context["fund_reports"] = reports_future.result()
            market_context["research_plan"] = deepcopy(plan)

        # 有限补查只发生在首轮快照冻结前。恢复快照和每日简报不走这里的补查逻辑。
        if daily_briefing is None:
            market_context = self.repair.run(portfolio=portfolio, market=market_context, plan=plan,
                reports_service=self.report_evidence, compact_event=self._compact_event,
                previous=repair_previous, checkpoint=repair_checkpoint)

        # 简报增加的是有时间戳的盘中证据，不覆盖原有日频风险和净值口径。
        # 这部分一起进入不可变快照，恢复执行时不会重新抓取或换成下一天行情。
        if daily_briefing is not None:
            prepared_codes = {item["fund_code"] for item in daily_briefing.get("funds", [])}
            if prepared_codes != {item["fund_code"] for item in portfolio.get("holdings", [])}:
                raise ValueError("准备简报期间持仓基金发生变化，停止生成不一致的报告。")
            market_context["daily_briefing"] = deepcopy(daily_briefing)

        payload = {
            "research_plan": plan,
            "portfolio": portfolio,
            "assessment": assessment,
            "quant_risk": quant_risk,
            "performance_snapshot": self._compact_performance(raw_performance),
            "market_context": market_context,
            "decision_history": self._compact_decision_history(raw_decisions),
        }
        source_manifest = self._snapshot_source_manifest(payload)
        guard_agent_portfolio(user_id, portfolio_id, "portfolio:research-run")
        snapshot = self.snapshots.create_once(
            run_id=run_id,
            user_id=user_id,
            portfolio_id=portfolio_id,
            period_months=months,
            graph_version=self.GRAPH_VERSION,
            capture_started_at=capture_started_at,
            source_manifest=source_manifest,
            payload=payload,
        )
        return self._load_snapshot(
            metadata=public_snapshot_metadata(snapshot),
            run_id=run_id,
            user_id=user_id,
            portfolio_id=portfolio_id,
            months=months,
        )

    def _load_snapshot(
        self,
        *,
        metadata: dict[str, Any],
        run_id: str,
        user_id: str,
        portfolio_id: str,
        months: int,
    ) -> dict[str, Any]:
        """加载并核对身份和哈希，防止 checkpoint 被绑定到另一份输入。"""

        guard_agent_portfolio(user_id, portfolio_id, "portfolio:research-run")
        snapshot_id = str(metadata.get("snapshot_id") or "").strip()
        if not snapshot_id:
            raise PortfolioResearchSnapshotError("checkpoint 缺少 snapshot_id。")
        snapshot = self.snapshots.get(
            user_id=user_id,
            snapshot_id=snapshot_id,
        )
        expected = {
            "run_id": run_id,
            "portfolio_id": portfolio_id,
            "period_months": months,
            "graph_version": metadata.get("graph_version"),
            "content_hash": metadata.get("content_hash"),
        }
        for key, value in expected.items():
            if snapshot.get(key) != value:
                raise PortfolioResearchSnapshotError(
                    f"研究快照的 {key} 与 checkpoint 不一致。"
                )
        # v9 仅在市场证据分区追加季报资料，节点和原有必填字段保持兼容。
        # 旧快照没有季报时明确保持缺失，恢复过程不重新采集或升级旧输入。
        if snapshot["graph_version"] not in {"portfolio-research-v7", "portfolio-research-v8", "portfolio-research-v9", self.GRAPH_VERSION}:
            raise PortfolioResearchSnapshotError("研究快照的 graph_version 不受当前流程支持。")
        guard_agent_portfolio(user_id, portfolio_id, "portfolio:research-run")
        return snapshot

    def _capture_market_context(
        self,
        portfolio: dict[str, Any],
    ) -> dict[str, Any]:
        """在快照采集阶段冻结市场状态和最多八条相关事件。"""

        sentiment = self.market_sentiment.analyze()
        event_result = self.market_events.search(limit=50)
        fund_codes = {
            str(item["fund_code"])
            for item in portfolio.get("holdings", [])
        }
        relevant_events = []
        for event in event_result.get("events", []):
            affected = {
                str(code) for code in event.get("affected_fund_codes", [])
            }
            if affected and not affected.intersection(fund_codes):
                continue
            relevant_events.append(self._compact_event(event))
            if len(relevant_events) >= 8:
                break

        return {
            "data_date": self._date_text(sentiment.get("data_date")),
            "state_code": sentiment.get("state_code", "unknown"),
            "state_label": sentiment.get("state_label", "未知"),
            "summary": str(sentiment.get("summary") or "暂无市场状态摘要。"),
            "confidence": sentiment.get("confidence", "low"),
            "signals": [
                {
                    "key": item.get("key"),
                    "label": item.get("label"),
                    "direction": item.get("direction"),
                    "value_text": item.get("value_text"),
                    "evidence": item.get("evidence"),
                }
                for item in sentiment.get("signals", [])[:4]
            ],
            "evidence_ids": list(sentiment.get("evidence_ids", [])),
            "event_data_cutoff": self._date_text(event_result.get("data_cutoff")),
            "relevant_event_count": len(relevant_events),
            "events": relevant_events,
        }

    def _snapshot_source_manifest(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """生成可公开的数据日期、数量和算法版本，不公开完整业务输入。"""

        portfolio = payload["portfolio"]
        assessment = payload["assessment"]
        quant_risk = payload["quant_risk"]
        performance = payload["performance_snapshot"]
        market = payload["market_context"]
        decisions = payload["decision_history"]
        decision_items = decisions.get("decisions") or []
        return {
            "source_dates": {
                "portfolio_updated_at": self._date_text(
                    portfolio.get("updated_at")
                ),
                "assessment_date": self._date_text(
                    assessment.get("assessment_date")
                ),
                "quant_risk_period_end": self._date_text(
                    quant_risk.get("period_end")
                ),
                "performance_period_end": self._date_text(
                    performance.get("period_end")
                ),
                "market_data_date": self._date_text(market.get("data_date")),
                "event_data_cutoff": self._date_text(
                    market.get("event_data_cutoff")
                ),
                "latest_decision_date": (
                    self._date_text(decision_items[0].get("decision_date"))
                    if decision_items
                    else None
                ),
            },
            "record_counts": {
                "portfolio_holdings": len(portfolio.get("holdings") or []),
                "quant_return_observations": quant_risk.get(
                    "return_observation_count"
                ),
                "scenario_return_observations": (
                    quant_risk.get("_scenario_validation_inputs") or {}
                ).get("return_observation_count"),
                "performance_observations": performance.get(
                    "observation_count"
                ),
                "relevant_events": market.get("relevant_event_count"),
                "decisions": decisions.get("decision_count", 0),
                "fund_report_documents": len((market.get("fund_reports") or {}).get("documents", [])),
            },
            "versions": {
                "graph": self.GRAPH_VERSION,
                "assessment_method": (
                    (assessment.get("score_breakdown") or {}).get("method")
                ),
                "quant_engine": quant_risk.get("engine") or {},
                "return_basis": quant_risk.get("return_basis"),
                "fund_report_evidence": (market.get("fund_reports") or {}).get("version"),
            },
            "fund_report_documents": (market.get("fund_reports") or {}).get("documents", []),
        }

    @traced("research.restore_checkpoint")
    def prepare_attempt_checkpoint(self, *, target_thread_id: str, source_thread_ids) -> None:
        """把最近一轮的断点复制到新执行分区，隔离旧 Worker 的迟到 checkpoint。

        仅适用于本研究图：单 namespace、普通完整状态 channel、没有子图或 DeltaChannel。
        同时迁移 pending writes，避免一个并行分支失败后重复运行其他已完成分支。
        这不是通用 LangGraph 数据库迁移器；若图结构改变，需同步扩展此协议。
        """
        check_active_task_authorization()
        target = {"configurable": {"thread_id": target_thread_id, "checkpoint_ns": ""}}
        if self.checkpointer.get_tuple(target) is not None:
            check_active_task_authorization()
            return
        for source_id in reversed(source_thread_ids):
            source = self.checkpointer.get_tuple({"configurable": {"thread_id": source_id, "checkpoint_ns": ""}})
            if source is None:
                continue
            checkpoint = deepcopy(source.checkpoint)
            state = checkpoint.get("channel_values", {})
            guard_agent_portfolio(state.get("user_id", ""), state.get("portfolio_id", ""), "portfolio:research-run")
            # SQLite 存整份状态，Postgres 还按版本单独存 channel blobs；这里必须提供所有版本。
            metadata = deepcopy(source.metadata)
            metadata["thread_id"] = target_thread_id
            metadata["recovered_from_thread"] = source_id
            config = self.checkpointer.put(target, checkpoint, metadata, checkpoint["channel_versions"])
            writes_by_task = {}
            for task_id, channel, value in source.pending_writes or []:
                writes_by_task.setdefault(task_id, []).append((channel, deepcopy(value)))
            for task_id, writes in writes_by_task.items():
                check_active_task_authorization()
                self.checkpointer.put_writes(config, writes, task_id)
            check_active_task_authorization()
            return
        check_active_task_authorization()

    def checkpoint_info(self, *, run_id: str, checkpoint_thread_id: str | None = None) -> dict[str, Any]:
        """返回不含大块业务数据的 checkpoint 摘要，供 Job 和 API 展示。"""

        snapshot = self.graph.get_state(
            {"configurable": {"thread_id": checkpoint_thread_id or run_id}}
        )
        state = dict(snapshot.values)
        checkpoint_id = (
            snapshot.config.get("configurable", {}).get("checkpoint_id")
            if snapshot.config
            else None
        )
        completed_nodes = list(dict.fromkeys(state.get("completed_nodes", [])))
        exists = bool(state or checkpoint_id)
        if state.get("report") and not snapshot.next:
            status = "completed"
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
            "checkpoint_thread_id": checkpoint_thread_id or run_id,
            "completed_nodes": completed_nodes,
            "completed_node_count": len(completed_nodes),
            "next_nodes": list(snapshot.next),
            # 只供 run() 在进程内做身份校验，不会放进 API 的 graph_trace。
            "state": state,
        }

    def checkpoint_state(self, *, run_id: str) -> dict[str, Any]:
        """读取教学用的最新 checkpoint，便于测试观察节点是否真的写入 State。"""

        snapshot = self.graph.get_state(
            {"configurable": {"thread_id": run_id}}
        )
        return dict(snapshot.values)

    def _graph_config(
        self,
        *,
        run_id: str,
        user_id: str,
        portfolio_id: str,
        checkpoint_thread_id: str | None = None,
    ) -> dict[str, Any]:
        """业务 run_id 不变；纳管任务由执行租约指定本轮独立 checkpoint 分区。"""

        # 老调用保持 Job ID 兼容，新执行分区可以从报告的 graph_trace 中定位。
        return {
            "configurable": {"thread_id": checkpoint_thread_id or run_id},
            "tags": ["fundscope", "portfolio-research"],
            "metadata": {
                "user_id": user_id,
                "portfolio_id": portfolio_id,
                "graph_version": self.GRAPH_VERSION,
            },
        }

    def _decorate_report(
        self,
        report: dict[str, Any],
        *,
        checkpoint: dict[str, Any],
        resumed_from_checkpoint: bool,
    ) -> dict[str, Any]:
        """给 Job/API 返回值补充最终 checkpoint 游标，不修改图中原始 State。"""

        result = dict(report)
        graph_trace = dict(result.get("graph_trace") or {})
        graph_trace.update(
            {
                "checkpoint_backend": self.checkpoint_backend,
                "checkpoint_durable": self.checkpoint_durable,
                "checkpoint_status": checkpoint["status"],
                "checkpoint_id": checkpoint["checkpoint_id"],
                "checkpoint_thread_id": checkpoint["checkpoint_thread_id"],
                "resumed_from_checkpoint": resumed_from_checkpoint,
                "trace_id": current_trace_id(),
            }
        )
        result["graph_trace"] = graph_trace
        return result

    def _load_portfolio(
        self,
        state: PortfolioResearchState,
        runtime: Runtime[PortfolioResearchContext],
    ) -> dict[str, Any]:
        """节点 1：从不可变输入包读取组合，而不是再次查询当前持仓。"""

        portfolio = self._snapshot_section(runtime, "portfolio")
        self._notify(runtime, "load_portfolio")
        return {
            "portfolio": portfolio,
            "completed_nodes": ["load_portfolio"],
        }

    def _calculate_assessment(
        self,
        state: PortfolioResearchState,
        runtime: Runtime[PortfolioResearchContext],
    ) -> dict[str, Any]:
        """节点 2：读取采集窗口内已经冻结的规则证据结果。"""

        assessment = self._snapshot_section(runtime, "assessment")
        self._notify(runtime, "calculate_assessment")
        return {
            "assessment": assessment,
            "completed_nodes": ["calculate_assessment"],
        }

    def _load_focused_inputs(self, state, runtime):
        """轻路径消费所需的冻结输入，不触发被省略的数据源，也不写假零值。"""
        sections = {key: self._snapshot_section(runtime, key) for key in (
            "assessment", "quant_risk", "performance_snapshot", "market_context", "decision_history")}
        sections["quant_risk"].pop("_scenario_validation_inputs", None)
        self._notify(runtime, "load_focused_inputs")
        return {**sections, "completed_nodes": ["load_focused_inputs"]}

    def _calculate_quant_risk(
        self,
        state: PortfolioResearchState,
        runtime: Runtime[PortfolioResearchContext],
    ) -> dict[str, Any]:
        """节点 3：读取同一快照中冻结的 Riskfolio 量化结果。"""

        quant_risk = self._snapshot_section(runtime, "quant_risk")
        # 冻结日收益矩阵只供后续候选场景重算。它不进入 LangGraph State、Job result、
        # 聊天上下文或前端，避免用数百行日频数据污染正常研究报告。
        quant_risk.pop("_scenario_validation_inputs", None)
        self._notify(runtime, "calculate_quant_risk")
        return {
            "quant_risk": quant_risk,
            "completed_nodes": ["calculate_quant_risk"],
        }

    def _calculate_performance(
        self,
        state: PortfolioResearchState,
        runtime: Runtime[PortfolioResearchContext],
    ) -> dict[str, Any]:
        """节点 4：读取已压缩业绩快照，日频曲线从采集时就不会进入快照表。"""

        snapshot = self._snapshot_section(runtime, "performance_snapshot")
        self._notify(runtime, "calculate_performance")
        return {
            "performance_snapshot": snapshot,
            "completed_nodes": ["calculate_performance"],
        }

    def _load_market_context(
        self,
        state: PortfolioResearchState,
        runtime: Runtime[PortfolioResearchContext],
    ) -> dict[str, Any]:
        """节点 5：读取采集时已按当前持仓过滤的市场状态与事件。"""

        market_context = self._snapshot_section(runtime, "market_context")
        self._notify(runtime, "load_market_context")
        return {
            "market_context": market_context,
            "completed_nodes": ["load_market_context"],
        }

    def _load_decision_history(
        self,
        state: PortfolioResearchState,
        runtime: Runtime[PortfolioResearchContext],
    ) -> dict[str, Any]:
        """节点 6：读取采集时冻结的历史决策与当时计算出的动态结果。"""

        history = self._snapshot_section(runtime, "decision_history")
        self._notify(runtime, "load_decision_history")
        return {
            "decision_history": history,
            "completed_nodes": ["load_decision_history"],
        }

    def _interpret_with_llm(
        self,
        state: PortfolioResearchState,
        runtime: Runtime[PortfolioResearchContext],
    ) -> dict[str, Any]:
        """节点 7：综合五路结果；模型失败时在本节点内完成降级。"""

        if state.get("research_mode") == "committee":
            if state["market_context"].get("daily_briefing"):
                raise ValueError("每日简报快照不能用于多角色模式。")
            if runtime.context.progress_callback:
                runtime.context.progress_callback("interpret_with_llm", "证据已准备，进入多角色研判")
            return {"completed_nodes": ["interpret_with_llm"]}

        try:
            interpretation = self.interpreter.interpret(
                portfolio=state["portfolio"],
                assessment=state["assessment"],
                quant_risk=state["quant_risk"],
                performance_snapshot=state["performance_snapshot"],
                market_context=state["market_context"],
                decision_history=state["decision_history"],
            )
        except PortfolioResearchInterpretationError as exc:
            # 外部模型不是报告可用性的单点故障。回退仍会引用同一批 Python 指标，
            # 并在 generation 字段明确标记本次没有采用模型结果。
            interpretation = self.interpreter.fallback(
                portfolio=state["portfolio"],
                assessment=state["assessment"],
                quant_risk=state["quant_risk"],
                performance_snapshot=state["performance_snapshot"],
                market_context=state["market_context"],
                decision_history=state["decision_history"],
                reason=str(exc),
            )

        self._notify(runtime, "interpret_with_llm")
        return {
            "interpretation": interpretation,
            "completed_nodes": ["interpret_with_llm"],
        }

    @staticmethod
    def _route_interpretation(state: PortfolioResearchState) -> list[str]:
        """明确模式选择，不允许模型修改工作流或通过回复增加调用次数。"""
        return (["committee_opportunity", "committee_risk"]
            if state.get("research_mode") == "committee" else ["compose_report"])

    def _committee_evidence(self, state: PortfolioResearchState):
        return self.committee.prepare({key: state[key] for key in (
            "portfolio", "assessment", "quant_risk", "market_context", "performance_snapshot", "decision_history"
        )})

    def _committee_role(self, state: PortfolioResearchState, runtime: Runtime[PortfolioResearchContext],
                        *, role: str, review: bool = False) -> dict:
        evidence = self._committee_evidence(state)
        node = f"committee_{role}" + ("_review" if review else "")
        if review:
            other = "risk" if role == "opportunity" else "opportunity"
            result = self.committee.cross_review(role, evidence,
                state[f"committee_{role}"], state[f"committee_{other}"])
        else:
            result = self.committee.analyze(role, evidence)
        self._notify(runtime, node)
        return {node: result, "completed_nodes": [node]}

    def _committee_synthesis(self, state: PortfolioResearchState,
                             runtime: Runtime[PortfolioResearchContext]) -> dict:
        interpretation, committee = self.committee.synthesize(
            self._committee_evidence(state),
            [state["committee_opportunity"], state["committee_risk"]],
            [state["committee_opportunity_review"], state["committee_risk_review"]],
            state["input_snapshot"]["snapshot_id"],
        )
        self._notify(runtime, "committee_synthesis")
        return {"interpretation": interpretation, "committee": committee,
            "completed_nodes": ["committee_synthesis"]}

    def _compose_report(
        self,
        state: PortfolioResearchState,
        runtime: Runtime[PortfolioResearchContext],
    ) -> dict[str, Any]:
        """节点 8：保留确定性快照，并把已校验语义结果装配成最终报告。"""

        portfolio = state["portfolio"]
        assessment = state["assessment"]
        quant_risk = state["quant_risk"]
        performance = state["performance_snapshot"]
        market = state["market_context"]
        decisions = state["decision_history"]
        interpretation = state["interpretation"]
        plan = state.get("research_plan") or {}
        # 数据缺口由程序保留，不能因为模型没有复述而从报告中消失。
        interpretation = {**interpretation, "uncertainties": list(dict.fromkeys([
            *interpretation.get("uncertainties", []), *plan.get("clarification_questions", []),
            *(market.get("fund_reports") or {}).get("warnings", []),
            *(row["detail"] for row in (market.get("research_repair") or {}).get("remaining_gaps", []))]))}
        completed_nodes = [*state.get("completed_nodes", []), "compose_report"]

        report = {
            "data_kind": "langgraph_portfolio_research",
            "workflow": "portfolio_research",
            "graph_version": self.GRAPH_VERSION,
            "run_id": state["run_id"],
            "thread_id": state["run_id"],
            "portfolio_id": state["portfolio_id"],
            "portfolio_name": portfolio["name"],
            "period_months": state["months"],
            "research_mode": state.get("research_mode", "standard"),
            "research_plan": plan,
            **({"committee": state["committee"]} if state.get("committee") else {}),
            "assessment_date": self._date_text(assessment.get("assessment_date")),
            # 这里只公开审计元数据。完整 payload 保存在独立快照表，由当前用户和
            # snapshot_id 联合定位，不会重复塞进 Job result 或聊天消息。
            "research_snapshot": state["input_snapshot"],
            # headline、summary 和 findings 已在上一个节点完成白名单校验或回退。
            "headline": interpretation["headline"],
            "summary": interpretation["summary"],
            "assessment_snapshot": {
                "state_code": assessment["state_code"],
                "state_label": assessment["state_label"],
                "confidence": assessment["confidence"],
                "score_breakdown": assessment["score_breakdown"],
                "concentration": assessment["concentration"],
                "fund_contributions": assessment.get("fund_contributions", []),
            },
            "quant_risk_snapshot": quant_risk,
            "performance_snapshot": performance,
            "market_snapshot": market,
            "decision_history": decisions,
            "model_interpretation": interpretation,
            "key_findings": interpretation["key_findings"],
            "key_evidence": {
                "reports": (market.get("fund_reports") or {}).get("evidence", []),
                "risk": [
                    self._compact_evidence(item)
                    for item in assessment.get("risk_evidence", [])[:3]
                ],
                "supporting": [
                    self._compact_evidence(item)
                    for item in assessment.get("supporting_evidence", [])[:3]
                ],
            },
            "graph_trace": {
                "completed_nodes": completed_nodes,
                "parallel_nodes": [] if state.get("research_mode") in {"explain", "strategy"} else [
                    "calculate_assessment",
                    "calculate_quant_risk",
                    "calculate_performance",
                    "load_market_context",
                    "load_decision_history",
                ],
                **({"committee_parallel_groups": [
                    ["committee_opportunity", "committee_risk"],
                    ["committee_opportunity_review", "committee_risk_review"],
                ]} if state.get("committee") else {}),
                "checkpoint_backend": runtime.context.checkpoint_backend,
                "checkpoint_durable": runtime.context.checkpoint_durable,
                "resumed_from_checkpoint": (
                    runtime.context.resumed_from_checkpoint
                ),
                "resume_checkpoint_id": runtime.context.resume_checkpoint_id,
                "snapshot_id": state["input_snapshot"]["snapshot_id"],
                "snapshot_content_hash": state["input_snapshot"][
                    "content_hash"
                ],
            },
            "generation": {
                "mode": (
                    ("multi_agent_committee" if state.get("committee") else "structured_model_synthesis")
                    if interpretation["model_used"]
                    else "deterministic_fallback"
                ),
                "model_used": interpretation["model_used"],
                "model": interpretation["model"],
                "prompt_version": interpretation["prompt_version"],
                "status": interpretation["status"],
                "attempt_count": interpretation["attempt_count"],
                "fallback_reason": interpretation.get("fallback_reason"),
            },
            "limitations": list(
                dict.fromkeys(
                    [
                        *plan.get("limitations", []),
                        *(state.get("committee") or {}).get("warnings", []),
                        *(market.get("daily_briefing") or {}).get("warnings", []),
                        *(market.get("fund_reports") or {}).get("warnings", []),
                        *(market.get("fund_reports") or {}).get("limitations", []),
                        *assessment.get("limitations", []),
                        *quant_risk.get("limitations", []),
                        *performance.get("warnings", []),
                    ]
                )
            ),
            "disclaimer": assessment["disclaimer"],
        }
        self._notify(runtime, "compose_report")
        return {
            "report": report,
            "completed_nodes": ["compose_report"],
        }

    @staticmethod
    def _snapshot_section(
        runtime: Runtime[PortfolioResearchContext],
        section: str,
    ) -> dict[str, Any]:
        """给节点返回独立副本，防止某个节点原地修改共享快照。"""

        payload = runtime.context.snapshot_payload
        if not isinstance(payload, dict) or section not in payload:
            raise PortfolioResearchSnapshotError(
                f"运行时研究快照缺少输入分区：{section}"
            )
        value = payload[section]
        if not isinstance(value, dict):
            raise PortfolioResearchSnapshotError(
                f"研究快照输入分区不是对象：{section}"
            )
        return deepcopy(value)

    def _notify(
        self,
        runtime: Runtime[PortfolioResearchContext],
        node_name: str,
    ) -> None:
        """让图保持业务无关：有回调就上报，没有回调也可以独立运行。"""

        callback = runtime.context.progress_callback
        check_active_task_authorization()
        if callback is not None:
            callback(node_name, self.NODE_LABELS[node_name])
        check_active_task_authorization()

    @staticmethod
    def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
        """避免把完整新闻正文和全部模型字段写入 Job 结果。"""

        semantic = event.get("semantic_analysis") or {}
        semantic_accepted = semantic.get("status") == "accepted"
        return {
            "event_id": event.get("event_id"),
            "event_date": PortfolioResearchGraphService._date_text(
                event.get("event_date")
            ),
            "title": event.get("title"),
            "summary": event.get("summary"),
            "impact_direction": (
                semantic.get("impact_direction")
                if semantic_accepted
                else event.get("impact_direction")
            ),
            "impact_horizon": (
                semantic.get("impact_horizon")
                if semantic_accepted
                else event.get("impact_horizon")
            ),
            "affected_fund_codes": list(event.get("affected_fund_codes", [])),
            "source_name": event.get("source_name"),
            "source_url": event.get("source_url"),
            "freshness_status": event.get("freshness_status"),
            "decay_factor": event.get("decay_factor", 1.0),
        }

    @staticmethod
    def _compact_performance(performance: dict[str, Any]) -> dict[str, Any]:
        """保留报告需要的业绩指标和归因，主动丢弃 ``series`` 日频大数组。"""

        benchmark = performance.get("benchmark") or {}
        attribution = performance.get("attribution") or []
        return {
            "data_kind": performance.get("data_kind"),
            "performance_label": performance.get("performance_label"),
            "position_mode": performance.get("position_mode"),
            "period_months": performance.get("period_months"),
            "period_start": PortfolioResearchGraphService._date_text(
                performance.get("period_start")
            ),
            "period_end": PortfolioResearchGraphService._date_text(
                performance.get("period_end")
            ),
            "observation_count": performance.get("observation_count"),
            "twr_pct": performance.get("twr_pct"),
            "xirr_pct": performance.get("xirr_pct"),
            "annualized_return_pct": performance.get("annualized_return_pct"),
            "annualized_volatility_pct": performance.get(
                "annualized_volatility_pct"
            ),
            "sharpe_ratio": performance.get("sharpe_ratio"),
            "sortino_ratio": performance.get("sortino_ratio"),
            "max_drawdown": dict(performance.get("max_drawdown") or {}),
            "benchmark": {
                key: benchmark.get(key)
                for key in (
                    "benchmark_name",
                    "status",
                    "period_return_pct",
                    "portfolio_aligned_return_pct",
                    "excess_return_pct",
                    "coverage_pct",
                    "source",
                )
            },
            "cash_flows": dict(performance.get("cash_flows") or {}),
            "attribution": [
                {
                    key: item.get(key)
                    for key in (
                        "fund_code",
                        "short_name",
                        "ending_weight_pct",
                        "pnl_contribution_amount",
                        "return_contribution_pct_points",
                        "return_basis_label",
                    )
                }
                for item in attribution[:8]
            ],
            "omitted_attribution_count": max(0, len(attribution) - 8),
            "assumptions": [
                str(item)[:180]
                for item in (performance.get("assumptions") or [])[:4]
            ],
            "warnings": [
                str(item)[:180]
                for item in (performance.get("warnings") or [])[:5]
            ],
            "methodology": str(performance.get("methodology") or "")[:500],
        }

    @staticmethod
    def _compact_decision_history(decisions: list[dict[str, Any]]) -> dict[str, Any]:
        """同时保留冻结依据和动态 outcome，并把历史长度限制在最近五条。"""

        action_labels = {
            "watch": "继续观察",
            "hold": "维持持有",
            "add": "条件性增配",
            "reduce": "条件性减配",
            "rebalance": "重新平衡",
        }
        items = []
        for decision in decisions[:5]:
            snapshot = decision.get("snapshot") or {}
            assessment = snapshot.get("assessment") or {}
            outcome = decision.get("outcome") or {}
            items.append(
                {
                    "decision_id": decision.get("id"),
                    "decision_date": PortfolioResearchGraphService._date_text(
                        decision.get("decision_date")
                    ),
                    "target_date": PortfolioResearchGraphService._date_text(
                        decision.get("target_date")
                    ),
                    "action": decision.get("action"),
                    "action_label": action_labels.get(
                        decision.get("action"),
                        decision.get("action"),
                    ),
                    "thesis": str(decision.get("thesis") or "")[:500],
                    "horizon_days": decision.get("horizon_days"),
                    "snapshot": {
                        "performance": dict(snapshot.get("performance") or {}),
                        "assessment": {
                            "assessment_date": assessment.get("assessment_date"),
                            "state_code": assessment.get("state_code"),
                            "state_label": assessment.get("state_label"),
                            "summary": str(assessment.get("summary") or "")[:240],
                            "risk_evidence_ids": list(
                                assessment.get("risk_evidence_ids") or []
                            )[:5],
                            "supporting_evidence_ids": list(
                                assessment.get("supporting_evidence_ids") or []
                            )[:5],
                        },
                        "research": snapshot.get("research"),
                    },
                    "outcome": {
                        "status": outcome.get("status"),
                        "as_of_date": PortfolioResearchGraphService._date_text(
                            outcome.get("as_of_date")
                        ),
                        "elapsed_days": outcome.get("elapsed_days"),
                        "target_days": outcome.get("target_days"),
                        "portfolio_return_pct": outcome.get(
                            "portfolio_return_pct"
                        ),
                        "benchmark_return_pct": outcome.get(
                            "benchmark_return_pct"
                        ),
                        "excess_return_pct": outcome.get("excess_return_pct"),
                        "note": outcome.get("note"),
                    },
                    "review_note": str(decision.get("review_note") or "")[:400],
                    "reviewed_at": PortfolioResearchGraphService._date_text(
                        decision.get("reviewed_at")
                    ),
                }
            )
        return {
            "data_kind": "portfolio_decision_history",
            "decision_count": len(decisions),
            "returned_count": len(items),
            "omitted_count": max(0, len(decisions) - len(items)),
            "has_decisions": bool(items),
            "decisions": items,
            "note": (
                "决策快照是当时依据，outcome 按最新净值动态更新；决策不代表真实成交。"
                if items
                else "当前组合暂无可复盘决策。"
            ),
        }

    @staticmethod
    def _compact_evidence(item: dict[str, Any]) -> dict[str, Any]:
        """报告只保留用户和 Agent 真正需要引用的证据字段。"""

        return {
            "evidence_id": item.get("evidence_id"),
            "title": item.get("title"),
            "explanation": item.get("explanation"),
            "data_date": PortfolioResearchGraphService._date_text(
                item.get("data_date")
            ),
            "source_name": item.get("source_name"),
            "source_url": item.get("source_url"),
            "weighted_points": item.get("weighted_points"),
        }

    @staticmethod
    def _date_text(value: Any) -> str | None:
        """把 date/datetime 和普通字符串统一成可写入 SQL JSON 的文本。"""

        if value is None:
            return None
        if hasattr(value, "isoformat"):
            return str(value.isoformat())
        return str(value)


# 应用级实例使用真正可持久化的 saver；测试仍可单独构造隔离的内存实例。
portfolio_research_graph_service = PortfolioResearchGraphService(
    report_evidence=portfolio_report_evidence_service,
    snapshots=portfolio_research_snapshot_service,
    checkpointer=portfolio_workflow_checkpoint_runtime.checkpointer,
    checkpoint_backend=portfolio_workflow_checkpoint_runtime.backend,
    checkpoint_durable=portfolio_workflow_checkpoint_runtime.durable,
)

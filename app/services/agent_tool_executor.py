"""主 Agent 的单次工具执行管线。

Agent Loop 决定“何时执行工具”；本模块固定“工具必须怎样执行”：可信参数注入、
PreToolUse/Permission、Python 或 MCP 分发、PostToolUse，以及 ToolMessage 回填。
"""

from __future__ import annotations

from uuid import uuid4

import json
import asyncio
from hashlib import sha256
from typing import Any

from app.schemas import AgentChatRequest, AgentToolCall
from app.services.agent_execution_context import (
    AgentExecutionContext,
    agent_execution_scope,
)
from app.services.tool_hooks import HookTrace, run_post_tool_use_hooks, run_pre_tool_use_hooks
from app.services.investment_context import current_investment_context
from app.services.agent_authorization import AgentAuthorizationError, get_task_authorization_service
from app.services.tracing import mark_error, set_trace_attributes, traced
from app.services.agent_loop_guard import current_loop_guard
from app.services.ai_governance_context import AIControlError
from app.services.tool_recovery import (
    ToolFailure, ToolRecoveryState, check_tool_active, classify_exception,
    current_tool_recovery, returned_failure,
)


# 这些工具的数据必须按当前 HTTP 请求用户隔离。user_id 永远由后端覆盖，不能相信模型
# 自己生成的参数，否则模型可能误读或改写另一个用户的任务、记忆或团队状态。
USER_SCOPED_TOOLS = frozenset(
    {
        "todo_write_tool",
        "remember_tool",
        "search_memory_tool",
        "task_create_tool",
        "task_list_tool",
        "task_update_tool",
        "task_claim_tool",
        "background_job_start_tool",
        "background_job_status_tool",
        "background_job_list_tool",
        "background_job_cancel_tool",
        "spawn_teammate_tool",
        "list_teammates_tool",
        "send_team_message_tool",
        "check_team_inbox_tool",
        "request_teammate_shutdown_tool",
        "list_team_protocols_tool",
        "review_team_plan_tool",
        "start_teammate_autonomy_tool",
        "list_autonomy_sessions_tool",
        "stop_autonomy_session_tool",
        "create_task_worktree_tool",
        "list_worktrees_tool",
        "keep_worktree_tool",
        "remove_worktree_tool",
        "schedule_cron_tool",
        "list_crons_tool",
        "cancel_cron_tool",
        # 组合 ID 本身不能作为授权依据；真实 user_id 必须由请求后端覆盖。
        "get_portfolio_assessment_tool",
        "get_portfolio_performance_tool",
        "get_portfolio_exposure_tool",
        "get_portfolio_event_impacts_tool",
        "get_large_capital_signals_tool",
        "list_portfolio_decisions_tool",
        "start_portfolio_research_tool",
        "get_portfolio_research_status_tool",
        "get_portfolio_research_brief_tool",
        "run_portfolio_stress_tool",
        "get_portfolio_stress_tool",
        "draft_portfolio_allocation_tool",
        "get_portfolio_allocation_tool",
    }
)

# 页面当前选中的基金也是可信 HTTP 上下文。后端覆盖模型生成的 fund_code，避免模型
# 把“这只基金”误填成别的代码；没有选择基金时仍允许模型从用户原文提取代码。
FUND_SCOPED_TOOLS = frozenset(
    {
        "get_fund_profile_tool",
        "get_fund_holdings_tool",
        "get_fund_report_research_tool",
        "get_fund_nav_summary_tool",
        "get_fund_risk_tool",
        "get_fund_assessment_tool",
        "get_fund_semantic_review_tool",
        "get_fund_research_brief_tool",
        "search_market_events_tool",
        "get_large_capital_signals_tool",
        "get_market_hotspots_tool",
    }
)


class AgentToolExecutor:
    """执行模型生成的一条 tool_call，并返回可继续喂给模型的结果。"""

    @traced("agent.tool", **{"langfuse.observation.type": "tool"})
    async def execute(
        self,
        *,
        tool_call: dict[str, Any],
        tool_map: dict[str, Any],
        request: AgentChatRequest,
        execution_context: AgentExecutionContext,
        tool_index: int,
        ToolMessage: type,
    ) -> tuple[AgentToolCall, Any, list[HookTrace]]:
        tool_name = str(tool_call.get("name") or "")
        check_tool_active()
        # 只记录注册过的工具名，不记录模型可任意生成的参数或原始内容。
        set_trace_attributes(**{"tool.name": tool_name if tool_name in tool_map else "unknown", "tool.index": tool_index})
        tool_args = dict(tool_call.get("args") or {})
        tool_call_id = str(tool_call.get("id") or f"tool_call_{tool_index}")

        # 对明确提出的另一资源直接拒绝，不能把 B 静默替换成 A 后告诉模型“调用成功”。
        # 缺少参数时仍可用用户已选择的 A 补齐；授权范围永远不从这些参数产生。
        proposed_portfolio = tool_args.get("portfolio_id")

        self._inject_trusted_arguments(
            tool_name=tool_name,
            tool_args=tool_args,
            request=request,
            execution_context=execution_context,
        )

        # Permission 是 PreToolUse Hook 的一部分。只有这里返回 allowed，实际 Python
        # 函数或 MCP tools/call 才有机会运行。
        allowed, block_reason, hook_traces = run_pre_tool_use_hooks(
            tool_name=tool_name,
            tool_args=tool_args,
            execution_context=execution_context,
        )
        if allowed and execution_context.task_grant_id:
            try:
                authorizer = get_task_authorization_service()
                if proposed_portfolio and proposed_portfolio != tool_args.get("portfolio_id"):
                    await asyncio.to_thread(authorizer.authorize, execution_context,
                        action="portfolio:read", portfolio_id=str(proposed_portfolio))
                    raise AgentAuthorizationError("resource_binding_mismatch", "工具目标与本次选择的组合不一致。")
                # OpenFGA/SQL 是同步客户端，放线程池避免占用 FastAPI 事件循环。
                await asyncio.to_thread(authorizer.authorize_tool, execution_context, tool_name, tool_args)
                hook_traces.append(HookTrace(event="PreToolUse", hook_name="task_authorization",
                    status="passed", message="本次任务的资源授权检查通过"))
            except AgentAuthorizationError as exc:
                allowed, block_reason = False, str(exc)
                hook_traces.append(HookTrace(event="PreToolUse", hook_name="task_authorization",
                    status="blocked", message=str(exc), metadata={"code": exc.code}))
        if not allowed:
            mark_error("tool_permission_denied")
            result = json.dumps(
                {
                    **ToolFailure("permission_denied", block_reason).payload(),
                    "permission": "denied",
                    "reason": block_reason,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                },
                ensure_ascii=False,
            )
            return self._build_result(
                tool_name=tool_name,
                tool_args=tool_args,
                tool_result=result,
                tool_call_id=tool_call_id,
                is_error=True,
                hook_traces=hook_traces,
                ToolMessage=ToolMessage,
            )

        selected_tool = tool_map.get(tool_name)
        execution_kind = "executed"
        recovery_trace = []
        if selected_tool is None:
            mark_error("tool_not_found")
            tool_result = json.dumps(
                ToolFailure("unknown_tool", f"未知工具：{tool_name}").payload(),
                ensure_ascii=False,
            )
            is_error = True
        else:
            try:
                # LangChain BaseTool 对同步和异步工具都提供 ainvoke，主循环无需关心
                # 底层最终执行的是普通 Python 函数还是 MCP ClientSession。
                # ContextVar 让工具内部继续派生的 Subagent 继承同一模式和身份；作用域
                # 只覆盖当前调用，不会污染并发执行的其他用户请求。
                with agent_execution_scope(execution_context):
                    cached = False
                    invocation_count = 0
                    async def invoke_once():
                        nonlocal invocation_count
                        check_tool_active()
                        if invocation_count:
                            # 退避期间权限可能已变化。第二次实际请求仍须经过 Hook/FGA，
                            # 不能把第一次 allowed 当成本轮永久通行证。
                            retry_allowed, reason, traces = run_pre_tool_use_hooks(
                                tool_name=tool_name, tool_args=tool_args, execution_context=execution_context)
                            hook_traces.extend(traces)
                            if not retry_allowed:
                                raise AgentAuthorizationError("tool_retry_denied", reason)
                            if execution_context.task_grant_id:
                                await asyncio.to_thread(get_task_authorization_service().authorize_tool,
                                    execution_context, tool_name, tool_args)
                        check_tool_active()
                        invocation_count += 1
                        result = await selected_tool.ainvoke(tool_args)
                        check_tool_active()
                        if execution_context.task_grant_id:
                            await asyncio.to_thread(get_task_authorization_service().check_context, execution_context)
                        return result

                    recovery = current_tool_recovery() or ToolRecoveryState()
                    async def invoke():
                        return await recovery.run(tool_name, tool_args, selected_tool,
                                                  execution_context, invoke_once, recovery_trace)

                    guard = current_loop_guard()
                    if guard is not None:
                        tool_result, cached = await guard.read(
                            tool_name, tool_args, selected_tool, execution_context, invoke)
                        if cached:
                            execution_kind = "cached"
                            hook_traces.append(HookTrace(event="PostToolUse", hook_name="loop_read_cache",
                                status="passed", message="复用本次请求已授权的相同只读结果，未重新刷新来源"))
                    else:
                        tool_result = await invoke()
                    # 工具可能运行数秒；运行中撤销后不再把它的结果交给模型。
                    # 缓存命中也重查，不能因缓存或等待去重锁而跳过撤销检查。
                    if cached and execution_context.task_grant_id:
                        await asyncio.to_thread(get_task_authorization_service().check_context, execution_context)
                is_error = returned_failure(tool_result) is not None
            except AgentAuthorizationError as exc:
                # 不返回撤销前取到的证据；主循环会在下一阶段再次核验任务有效性。
                mark_error(exc)
                tool_result = json.dumps(ToolFailure("permission_denied", str(exc)).payload(), ensure_ascii=False)
                recovery_trace.append({"action": "access_stopped", "error_code": "permission_denied"})
                is_error = True
            except AIControlError:
                raise  # 截止、额度与执行资格信号交回上层，不能转成 JSON 后继续消耗。
            except Exception as exc:
                check_tool_active()
                mark_error(exc)
                tool_result = json.dumps(classify_exception(exc).payload(), ensure_ascii=False)
                is_error = True

        if not isinstance(tool_result, str):
            tool_result = json.dumps(tool_result, ensure_ascii=False, default=str)
        if is_error:
            mark_error("tool_failed")
        if recovery_trace:
            hook_traces.append(HookTrace(event="PostToolUse", hook_name="tool_recovery",
                status="failed" if is_error else "passed", message="工具错误分类与有限恢复",
                metadata={"steps": recovery_trace}))
        hook_traces.extend(
            run_post_tool_use_hooks(
                tool_name=tool_name,
                tool_args=tool_args,
                tool_result=str(tool_result),
                is_error=is_error,
            )
        )
        return self._build_result(
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=str(tool_result),
            tool_call_id=tool_call_id,
            is_error=is_error,
            hook_traces=hook_traces,
            ToolMessage=ToolMessage,
            execution_kind=execution_kind,
            recovery_trace=recovery_trace,
        )

    def _inject_trusted_arguments(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        request: AgentChatRequest,
        execution_context: AgentExecutionContext,
    ) -> None:
        """把只应由后端决定的参数放进模型生成的参数之后。"""

        if tool_name == "rag_search_tool" and "top_k" not in tool_args:
            tool_args["top_k"] = request.top_k
        if tool_name in USER_SCOPED_TOOLS:
            # user_id 来自 Session 构造的 ExecutionContext，不来自请求体或模型。
            tool_args["user_id"] = execution_context.user_id
        if tool_name in {
            "get_fund_research_brief_tool",
            "get_portfolio_research_brief_tool",
        }:
            # 综合研究必须理解用户原问题。这里覆盖模型复述，防止它在 tool_call 中
            # 无意删掉“期限、风险承受能力”等影响证据闸门的条件。
            background = current_investment_context()
            tool_args["question"] = (
                background.effective_question(request.message) if background is not None else request.message
            )
        if (
            request.portfolio_id
            and tool_name
            in {
                "get_portfolio_assessment_tool",
                "get_portfolio_performance_tool",
                "get_portfolio_exposure_tool",
                "get_portfolio_event_impacts_tool",
                "get_large_capital_signals_tool",
                "list_portfolio_decisions_tool",
                "start_portfolio_research_tool",
                "get_portfolio_research_status_tool",
                "get_portfolio_research_brief_tool",
                "run_portfolio_stress_tool",
                "get_portfolio_stress_tool",
                "draft_portfolio_allocation_tool",
                "get_portfolio_allocation_tool",
            }
        ):
            # 当前页面选中的组合和 user_id 一样属于可信 HTTP 上下文，不能让模型改成
            # 另一个组合 ID。无页面组合时才保留模型从用户原文提取的参数。
            tool_args["portfolio_id"] = request.portfolio_id
        if tool_name == "draft_portfolio_allocation_tool":
            # 原始需求与请求身份来自HTTP上下文；模型只负责提议，不提供确认凭据。
            tool_args["question"] = request.message
            tool_args["idempotency_key"] = "allocation:" + sha256(
                f"{request.idempotency_key or uuid4().hex}:{request.portfolio_id}:{request.message}:{tool_args.get('source_run_id')}".encode()
            ).hexdigest()
        if tool_name == "run_portfolio_stress_tool":
            # 同一轮内原方案、候选方案参数不同，不能共享同一个键；重试相同参数则复用。
            tool_args.pop("idempotency_key", None)
            encoded = json.dumps(tool_args, sort_keys=True, ensure_ascii=False)
            tool_args["idempotency_key"] = (
                "stress:" + sha256(f"{request.idempotency_key}:{encoded}".encode()).hexdigest()
                if request.idempotency_key else None
            )
        if tool_name == "start_portfolio_research_tool":
            tool_args["question"] = request.message[:800]
            # 模型不能自选幂等键，否则它既可能撞到旧 Job，也可能在重试时换键。
            # 浏览器对同一次发送会复用 request.idempotency_key，前缀则与聊天建单隔离。
            tool_args["idempotency_key"] = (
                f"portfolio-research:{request.idempotency_key}"
                if request.idempotency_key
                else None
            )
        if request.fund_code and tool_name in FUND_SCOPED_TOOLS:
            tool_args["fund_code"] = request.fund_code
        if tool_name == "get_fund_report_research_tool":
            # 服务端验证后的版本优先于模型参数，不允许追问时擅自换成最新季度。
            tool_args["document_ids"] = request.fund_report_ids or None
            tool_args["question"] = request.message

    def _build_result(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_result: str,
        tool_call_id: str,
        is_error: bool,
        hook_traces: list[HookTrace],
        ToolMessage: type,
        execution_kind: str = "executed",
        recovery_trace: list[dict] | None = None,
    ) -> tuple[AgentToolCall, Any, list[HookTrace]]:
        """同时生成审计记录和 LangChain 下一轮需要的 ToolMessage。"""

        # 即使在真正执行前已被权限/未知工具检查拒绝，也把稳定错误码留进审计。
        recovery_trace = recovery_trace or []
        if is_error and not recovery_trace:
            failure = returned_failure(tool_result)
            recovery_trace.append({"action": "rejected", "attempt": 0,
                                   "error_code": failure.code if failure else "internal_error"})
        executed_call = AgentToolCall(
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=tool_result,
            is_error=is_error,
            execution_kind=execution_kind,
            recovery_trace=recovery_trace,
        )
        tool_message = ToolMessage(
            content=tool_result,
            tool_call_id=tool_call_id,
        )
        return executed_call, tool_message, hook_traces


agent_tool_executor = AgentToolExecutor()

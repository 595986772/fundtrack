"""HTTP 产品聊天共享的截止时间；不替代 Token 账本或后台 Job 的执行时限。

倒计时从聊天协调入口开始，排队、准备、路由和重试不会重新领取时间。
asyncio 只能取消协程等待，不能杀死已开始的同步线程，所以线程中的后续模型
调用也检查阶段是否已经关闭；已发出的网络请求仍依赖其客户端超时和最终结算。
"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import math
from time import monotonic
from uuid import uuid4

from app.config import settings
from app.services.ai_governance_context import AIControlError, current_ai_scope


class ChatTimeBudgetExceeded(AIControlError):
    def __init__(self, phase):
        super().__init__("chat_time_budget_exhausted",
            "本次分析的执行时间已用完，已停止后续步骤。请缩小问题范围后重试。",
            status_code=504, retry_after=0)
        self.phase = phase


@dataclass
class StageWindow:
    name: str
    deadline: float
    closed: bool = False


@dataclass
class LoopProgress:
    """只留已经返回的工具和已知用量，绝不把模型的未校验草稿作为超时答案。"""
    run_id: str = field(default_factory=lambda: f"agent_{uuid4().hex[:16]}")
    calls: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    visible_calls: list = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    rounds: list = field(default_factory=list)
    pipeline_steps: list = field(default_factory=list)
    tool_names: list = field(default_factory=list)
    prompt_trace: object = None


_budget = ContextVar("chat_execution_budget", default=None)
_window = ContextVar("chat_execution_window", default=None)
_progress = ContextVar("chat_loop_progress", default=None)


def current_chat_budget():
    budget = _budget.get()
    # 本地 Job 即使复制了 ContextVar，也有独立的 AI run_id，不能继承聊天时限。
    if budget and budget.run_id and current_ai_scope().run_id != budget.run_id:
        return None
    return budget


def current_loop_progress():
    return _progress.get() if current_chat_budget() else None


@contextmanager
def loop_progress_scope():
    progress = LoopProgress()
    token = _progress.set(progress)
    try:
        yield progress
    finally:
        _progress.reset(token)


@contextmanager
def chat_budget_scope(budget):
    token, window_token = _budget.set(budget), _window.set(None)
    try:
        yield budget
    finally:
        _window.reset(window_token)
        _budget.reset(token)


def chat_deadline():
    budget = current_chat_budget()
    if budget is None:
        return None
    window = _window.get()
    return min(budget.deadline, window.deadline) if window else budget.work_deadline


def check_chat_active():
    budget = current_chat_budget()
    if budget is None:
        return
    window = _window.get()
    if budget.cancelled or budget.closed or (window and window.closed) or monotonic() >= chat_deadline():
        raise ChatTimeBudgetExceeded(window.name if window else "work")


class ChatExecutionBudget:
    def __init__(self, seconds=None, reserve_seconds=None):
        self.seconds = settings.agent_chat_timeout_seconds if seconds is None else seconds
        self.reserve = settings.agent_chat_finalize_reserve_seconds if reserve_seconds is None else reserve_seconds
        if not (math.isfinite(self.seconds) and math.isfinite(self.reserve)
                and 0 <= self.reserve < self.seconds):
            raise AIControlError("invalid_chat_budget", "聊天时间预算配置无效", status_code=503)
        self.started = monotonic()
        self.deadline = self.started + self.seconds
        self.work_deadline = self.deadline - self.reserve
        self.run_id = None
        self.cancelled = False
        self.closed = False
        self.stages = []
        self.expired_phase = None

    def snapshot(self):
        return {"version": "chat-time-budget-v1", "budget_seconds": self.seconds,
            "finalize_reserve_seconds": self.reserve,
            "elapsed_ms": round((monotonic() - self.started) * 1000, 2),
            "remaining_ms": round(max(0, self.deadline - monotonic()) * 1000, 2),
            "expired_phase": self.expired_phase, "cancelled": self.cancelled,
            "stages": list(self.stages)}

    async def run(self, phase, operation, *, closing=False):
        # operation 是工厂：时间不够时不创建协程，更不能先发送请求再做检查。
        parent = _window.get()
        deadline = self.deadline if closing else self.work_deadline
        if parent:
            deadline = min(deadline, parent.deadline)
        window = StageWindow(phase, deadline)
        token = _window.set(window)
        started, status = monotonic(), "completed"
        # 用剩余时长设置 asyncio 定时器，不假设所有事件循环的 time() 原点一致。
        timeout = asyncio.timeout(max(0, deadline - started))
        try:
            check_chat_active()
            async with timeout:
                result = await operation()
                # 同步运算阻塞事件循环时，定时取消可能尚未执行，返回前仍复核时间。
                check_chat_active()
                return result
        except TimeoutError:
            if not timeout.expired():
                status = "error"  # 来源自己的超时不是整次聊天超时。
                raise
            status, self.expired_phase = "timeout", phase
            raise ChatTimeBudgetExceeded(phase) from None
        except ChatTimeBudgetExceeded as exc:
            status, self.expired_phase = "timeout", exc.phase
            raise
        except asyncio.CancelledError:
            # 外层倒计时与用户停止都向下取消，不在这里冒充普通工具失败后继续。
            status = "cancelled"
            raise
        except BaseException:
            status = "error"
            raise
        finally:
            window.closed = True
            self.stages.append({"phase": phase, "status": status,
                "duration_ms": round((monotonic() - started) * 1000, 2)})
            _window.reset(token)


async def chat_stage(phase, operation, *, closing=False):
    budget = current_chat_budget()
    return await budget.run(phase, operation, closing=closing) if budget else await operation()


def bounded_product_chat(function):
    """只在聊天协调入口启用；纯 Worker 调用和管理员开发模式维持原预算。"""
    @wraps(function)
    async def wrapped(self, request, *, execution_context=None, **kwargs):
        if not settings.agent_chat_budget_enabled or (execution_context and execution_context.mode != "product"):
            return await function(self, request, execution_context=execution_context, **kwargs)
        budget = current_chat_budget()
        if budget is not None:
            return await function(self, request, execution_context=execution_context, **kwargs)
        budget = ChatExecutionBudget()
        with chat_budget_scope(budget):
            try:
                # 各可等待的业务阶段执行截止检查。最终短事务与费用清理不被再次
                # 强制中断，避免“消息已提交，却标失败重放”造成重复保存。
                return await function(self, request, execution_context=execution_context, **kwargs)
            except asyncio.CancelledError:
                budget.cancelled = True
                raise
            finally:
                # 异步返回后遗留的线程仍持有同一对象，不能继续发起新模型调用。
                budget.closed = True
    return wrapped


def partial_loop_response(request, progress, guard):
    """时间耗尽时只说明完成范围和缺口；不调用模型、不暴露未验证的中间结论。"""
    from app.schemas import AgentChatResponse, AgentPromptTrace, AgentTokenUsage, AgentLoopControl
    from app.services.agent_loop_guard import object_payload, successful
    budget = current_chat_budget()
    labels = {"get_fund_risk_tool": "基金历史风险", "get_fund_report_research_tool": "基金季报",
        "get_portfolio_performance_tool": "组合收益", "get_portfolio_assessment_tool": "组合风险",
        "search_market_events_tool": "市场事件", "get_fund_profile_tool": "基金档案"}
    returned = []
    for call in progress.calls:
        if call.is_error or not successful(object_payload(call.tool_result)):
            continue
        label = labels.get(call.tool_name, call.tool_name)
        target = call.tool_args.get("fund_code") or call.tool_args.get("portfolio_id")
        returned.append(label + (f"（{target}）" if target else ""))
    answer = "本轮分析达到执行时限，已停止后续查询，尚未形成完整结论。"
    answer += ("\n\n已返回的资料：" + "、".join(dict.fromkeys(returned)) + "。"
               if returned else "\n\n本轮尚未取得可用的工具资料。")
    if guard:
        # 使用 Loop 实际保留的消息核对，不用部分并发返回冒充模型已读完证据。
        guard.evaluate(progress.messages, progress.visible_calls)
        if guard.trace.missing:
            answer += "\n\n仍待补齐或核对：" + "；".join(guard.trace.missing) + "。"
        guard.trace.stop_reason = "time_budget_exhausted"
    answer += "\n\n未完成的资料读取、综合分析或校验不能算作已完成，请缩小问题范围后重试。"
    return AgentChatResponse(run_id=progress.run_id, user_id=request.user_id, question=request.message,
        answer=answer, used_model=settings.deepseek_model, max_tool_rounds=request.max_tool_rounds,
        stop_reason="time_budget_exhausted", tools_available=progress.tool_names, tool_calls=progress.calls,
        rounds=progress.rounds, pipeline_steps=[*progress.pipeline_steps,
            "Chat shared deadline reached; returned deterministic partial status"],
        prompt_trace=progress.prompt_trace or AgentPromptTrace(section_count=0, total_chars=0, sections=[]),
        usage=AgentTokenUsage(**progress.usage), loop_control=guard.trace if guard else AgentLoopControl(),
        execution_budget=budget.snapshot() if budget else {})

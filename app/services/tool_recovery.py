"""工具错误协议和有限恢复，不取代模型重试、业务补查或任务恢复。

重试资格由后端白名单决定。模型只能看错误和下一步建议，不能通过 retryable
参数扩大权限、次数或刷新范围。状态仅存在于一次 Agent 调用中。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
import math
from time import monotonic

import httpx
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeoutError

from app.services.ai_governance_context import AIControlError, current_ai_scope
from app.services.chat_execution_budget import check_chat_active, chat_deadline
from app.services.errors import ServiceError


# 只读已保存的业务数据，无内层模型、自动抓取或写入。并行工具、MCP 的 readOnly
# 标注、名字以 get 开头都不是重试授权。新工具需审查副作用后才能加入。
REPLAY_SAFE_READS = frozenset({
    "get_fund_profile_tool", "get_fund_nav_summary_tool", "get_fund_risk_tool",
    "get_portfolio_assessment_tool", "get_portfolio_performance_tool",
    "get_portfolio_exposure_tool", "search_market_events_tool",
})
TRANSIENT_CODES = frozenset({"source_timeout", "source_unavailable", "source_rate_limited"})
ACTIONS = {
    "invalid_arguments": "correct_arguments",
    "resource_not_found": "clarify_resource",
    "data_unavailable": "report_data_gap",
    "permission_denied": "stop_access",
    "unknown_tool": "discover_authorized_tool",
    "business_error": "explain_failure",
    "internal_error": "report_failure",
    **dict.fromkeys(TRANSIENT_CODES, "report_source_failure"),
}
TOOL_RECOVERY_INSTRUCTION = (
    "工具错误处理：ok=false 不是有效证据。遵守 error_code 和 suggested_action；"
    "后端已处理获准的临时重试，不要重复 retryable=false 的相同查询。"
    "参数错误只允许一次修正；身份和基金代码不明确时追问，不猜代码。"
    "缺数据不等于事实不存在或风险为零。不得换工具绕过权限，也不得未经用户同意刷新来源。"
    "保留成功结果，仅说明失败部分的缺口。"
)
_state = ContextVar("tool_recovery_state", default=None)


@dataclass(frozen=True)
class ToolFailure:
    code: str
    message: str
    fields: tuple = ()
    retry_after: float = 0.2

    def payload(self, *, action=None):
        result = {"ok": False, "error": self.message, "error_code": self.code,
                  "retryable": False, "suggested_action": action or ACTIONS[self.code]}
        if self.fields:
            result["field_errors"] = list(self.fields)
        return result


def business_tool_error(exc: ServiceError, **identity):
    """业务 Tool 捕获异常时保留原因；直接调用工具的 API 也能得到同一协议。"""
    code = exc.code if exc.code in ACTIONS else "business_error"
    return json.dumps({**identity, **ToolFailure(code, str(exc)).payload()}, ensure_ascii=False)


def _retry_after(value):
    """同时接受 Retry-After 秒数和 HTTP 日期；长等待不截短后强行请求。"""
    if not value:
        return 0.2
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0.2
    return max(0.2, seconds) if math.isfinite(seconds) else float("inf")


def argument_failure(exc: ValidationError):
    """只由入口 Schema 校验调用；工具内部解析来源数据失败不属于模型参数错误。"""
    fields = tuple({"field": ".".join(map(str, row["loc"])), "type": row["type"],
                    "message": row["msg"][:160]}
                   for row in exc.errors(include_input=False, include_url=False,
                                         include_context=False)[:8])
    return ToolFailure("invalid_arguments", "工具参数不符合要求，请按字段提示修正一次。", fields)


def classify_exception(exc):
    """仅凭确定的类型/状态分类，字符串里出现 timeout 不足以允许重试。"""
    if isinstance(exc, ServiceError):
        return ToolFailure(exc.code if exc.code in ACTIONS else "business_error", str(exc))
    if isinstance(exc, PermissionError):
        return ToolFailure("permission_denied", "当前操作未获授权。")
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return ToolFailure("source_timeout", "数据源未在约定时间内返回，本次没有取得有效结果。")
    if isinstance(exc, (httpx.NetworkError, ConnectionError)):
        return ToolFailure("source_unavailable", "暂时无法连接数据源。")
    if isinstance(exc, PoolTimeoutError) or (isinstance(exc, DBAPIError) and exc.connection_invalidated):
        # 只处理连接暂不可用；SQL 语法、约束冲突等数据库错误不能靠重试掩盖。
        return ToolFailure("source_unavailable", "读取数据的连接暂不可用。")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            return ToolFailure("permission_denied", "数据源拒绝访问，请检查访问权限。")
        if status in {408, 429, 502, 503, 504}:
            code = "source_rate_limited" if status == 429 else "source_unavailable"
            return ToolFailure(code, "数据源暂时无法完成查询。",
                               retry_after=_retry_after(exc.response.headers.get("Retry-After")))
    # 不把 SDK 的完整异常、URL 或请求体放进模型上下文，避免泄露凭证。
    return ToolFailure("internal_error", "工具执行失败，需要检查服务端诊断，不能据此推断业务结论。")


def returned_failure(result):
    """兼容旧 JSON 回执；空列表是成功查询的空结果，不自动解释为故障。"""
    try:
        payload = result if isinstance(result, dict) else json.loads(result)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not (payload.get("ok") is False or payload.get("error")):
        return None
    code = payload.get("error_code")
    code = code if isinstance(code, str) and code in ACTIONS else "business_error"
    # 回执上的 retryable/建议不决定后端政策；错误摘要有界，原业务字段由调用方保留。
    failure = ToolFailure(code, str(payload.get("error") or payload.get("reason") or "业务操作未完成。")[:500])
    return failure


def check_tool_active():
    check_chat_active()
    callback = current_ai_scope().check_cancelled
    if callback:
        callback()  # Worker 的取消/执行版本失效也必须中断，不能包装成可重试错误。


@contextmanager
def tool_recovery_scope():
    state = ToolRecoveryState()
    token = _state.set(state)
    try:
        yield state
    finally:
        state.closed = True
        _state.reset(token)


def current_tool_recovery():
    return _state.get()


class ToolRecoveryState:
    """失败回执与修参次数按请求隔离；锁防止并行重复查询各拿一次重试额度。"""

    def __init__(self):
        self.failures = {}
        self.locks = {}
        self.invalid_counts = {}
        self.closed = False

    async def run(self, name, args, selected_tool, context, invoke, trace):
        check_tool_active()
        if self.closed:
            raise asyncio.CancelledError()
        schema = getattr(selected_tool, "args_schema", None)
        normalized = args
        validation = None
        if isinstance(schema, type) and hasattr(schema, "model_validate"):
            try:
                normalized = schema.model_validate(args).model_dump(mode="json")
            except ValidationError as exc:
                validation = argument_failure(exc)
        scope = (context.user_id, context.task_grant_id, context.task_run_id, context.task_agent_id)
        key = sha256(json.dumps([scope, name, normalized], sort_keys=True, default=str).encode()).hexdigest()
        repair_key = (*scope, name)
        # 限制内存大小；达到上限就拒绝新增调用，不通过丢弃旧失败记录重获重试资格。
        if key not in self.locks and len(self.locks) >= 128:
            trace.append({"action": "capacity_exhausted", "attempt": 0, "error_code": "business_error"})
            return json.dumps(ToolFailure("business_error", "本次工具查询种类已达上限，请缩小范围。").payload())
        async with self.locks.setdefault(key, asyncio.Lock()):
            check_tool_active()
            if self.closed:
                raise asyncio.CancelledError()
            if key in self.failures:
                trace.append({"action": "blocked_duplicate", "attempt": 0,
                              "error_code": returned_failure(self.failures[key]).code})
                return self.failures[key]
            if self.invalid_counts.get(repair_key, 0) >= 2:
                return self._finish(key, ToolFailure("invalid_arguments", "本次工具参数修正次数已用完，请补充或确认参数。"),
                                    trace, action="ask_user", decision="correction_exhausted")
            if validation:
                self.invalid_counts[repair_key] = self.invalid_counts.get(repair_key, 0) + 1
                return self._finish(key, validation, trace,
                    action="ask_user" if self.invalid_counts[repair_key] >= 2 else "correct_arguments",
                    decision="validation_failed")

            for attempt in (1, 2):
                # invoke 包含重新授权；睡眠期间撤销、取消或到期也不能再访问来源。
                check_tool_active()
                if self.closed:
                    raise asyncio.CancelledError()
                started = monotonic()
                result = None
                is_returned_error = False
                exception_type = None
                try:
                    result = await invoke()
                    failure = returned_failure(result)
                    is_returned_error = failure is not None
                except AIControlError:
                    raise  # 额度、租约、授权和总时限沿用上层收尾，不能换工具/模型绕过。
                except Exception as exc:
                    check_tool_active()
                    failure = classify_exception(exc)
                    exception_type = type(exc).__name__
                trace.append({"action": "attempt", "attempt": attempt,
                              "duration_ms": round((monotonic() - started) * 1000, 2),
                              "exception_type": exception_type,
                              "error_code": failure.code if failure else None})
                if not failure:
                    return result
                if failure.code == "invalid_arguments":
                    self.invalid_counts[repair_key] = self.invalid_counts.get(repair_key, 0) + 1
                eligible = name in REPLAY_SAFE_READS and failure.code in TRANSIENT_CODES
                # 业务回执不携带可信 Retry-After，因此只重试有类型的传输异常。
                # 已返回的业务失败由各自 Service 决定何时再次读取，避免嵌套重试。
                if eligible and attempt == 1 and not is_returned_error:
                    deadline = chat_deadline()
                    wait = failure.retry_after
                    # 最长退避两秒；若供应商要求更久，直接结束，不提前发请求。
                    if wait <= 2 and (deadline is None or deadline - monotonic() > wait + 0.05):
                        trace.append({"action": "retry_wait", "delay_seconds": wait, "error_code": failure.code})
                        await asyncio.sleep(wait)
                        continue
                    decision = "retry_wait_exceeds_budget"
                else:
                    decision = "retry_exhausted" if eligible and attempt == 2 else "not_replayed"
                action = "ask_user" if self.invalid_counts.get(repair_key, 0) >= 2 else None
                if name not in REPLAY_SAFE_READS and failure.code in TRANSIENT_CODES:
                    action = "verify_operation_state"
                return self._finish(key, failure, trace, action=action, decision=decision, original=result)

    def _finish(self, key, failure, trace, *, action=None, decision, original=None):
        trace.append({"action": decision, "error_code": failure.code})
        # 保留旧业务回执中的基金/组合 ID、缺项等字段，不破坏专用工具的返回契约。
        try:
            original = original if isinstance(original, dict) else json.loads(original)
        except (ValueError, TypeError):
            original = {}
        payload = original if isinstance(original, dict) else {}
        result = json.dumps({**payload, **failure.payload(action=action)}, ensure_ascii=False)
        self.failures[key] = result
        return result

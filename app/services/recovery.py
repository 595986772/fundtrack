"""s11 Error Recovery：模型调用错误恢复服务。

这一层只包围“调用大模型”这一步，不包围工具执行：

- 模型 API 超时、限流或服务端错误，可以安全地有限重试；
- 上下文超限时，先尝试压缩旧 ToolMessage，再重试一次；
- 模型因为输出长度上限被截断时，额外请求一次续写；
- 配置错误、参数错误等不可恢复问题直接失败；
- 所有恢复动作都有明确上限，防止形成新的无限循环。

工具写入可能有副作用，所以不能简单地把整个 Agent Loop 都重跑一遍。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import re
from time import monotonic
from typing import Any, Callable

from app.services.context_compactor import CompactTrace, context_compactor_service
from app.services.errors import AgentConfigError
from app.services.ai_governance_context import AIControlError


TRANSIENT_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
TRUNCATED_FINISH_REASONS = {
    "length",
    "max_tokens",
    "max_output_tokens",
    "token_limit",
}


def model_attempt_metrics(messages, response):
    """只统计长度和供应商用量，不把提示词、思考过程或失败草稿写进诊断。

    没收到 usage 时保留 None，不能把超时请求当作免费调用。字符数仅帮助定位
    上下文膨胀，不冒充 tokenizer 计算的 Token 数。
    """
    def content_length(message):
        content = getattr(message, "content", "")
        return len(content) if isinstance(content, str) else len(json.dumps(content, ensure_ascii=False))

    metadata = getattr(response, "response_metadata", {}) or {}
    raw = getattr(response, "usage_metadata", None) or metadata.get("token_usage")
    usage = None
    if isinstance(raw, dict):
        input_tokens = int(raw.get("input_tokens", raw.get("prompt_tokens", 0)) or 0)
        output_tokens = int(raw.get("output_tokens", raw.get("completion_tokens", 0)) or 0)
        details = raw.get("output_token_details") or raw.get("completion_tokens_details") or {}
        usage = {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": int(raw.get("total_tokens") or input_tokens + output_tokens),
            "reasoning_tokens": details.get("reasoning", details.get("reasoning_tokens"))}
    finish = metadata.get("finish_reason")
    return {"input_chars": sum(content_length(message) for message in messages),
        "message_count": len(messages), "output_chars": content_length(response) if response is not None else None,
        "usage": usage, "finish_reason": finish if finish in {"stop", "length", "tool_calls", "content_filter"} else None}


@dataclass(frozen=True)
class RecoveryTrace:
    """一次恢复动作的可观察记录。"""

    event: str
    phase: str
    round_index: int
    attempt: int
    max_attempts: int
    action: str
    error_type: str
    message: str
    delay_seconds: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelCallResult:
    """恢复层成功完成一次模型调用后的结果。"""

    # response 是第一次模型调用返回的原始 AIMessage。
    # Agent Loop 仍然从这里读取 tool_calls，不改变原有循环结构。
    response: Any

    # 只有第一次回答被长度上限截断时，这里才会保存额外续写文本。
    continuation_text: str = ""
    # 保留续写的原始响应，仅用于汇总 token usage 等观测数据。
    continuation_response: Any | None = None

    traces: tuple[RecoveryTrace, ...] = ()
    compact_traces: tuple[CompactTrace, ...] = ()


@dataclass(frozen=True)
class _AttemptResult:
    """内部使用：一组有上限重试的执行结果。"""

    response: Any
    traces: tuple[RecoveryTrace, ...]
    compact_traces: tuple[CompactTrace, ...]


class ModelRecoveryError(RuntimeError):
    """模型调用经过允许的恢复动作后仍然失败。"""

    def __init__(
        self,
        *,
        phase: str,
        round_index: int,
        original_error: Exception,
        traces: list[RecoveryTrace],
        compact_traces: list[CompactTrace],
    ) -> None:
        self.phase = phase
        self.round_index = round_index
        self.original_error = original_error
        self.traces = traces
        self.compact_traces = compact_traces

        super().__init__(
            f"{phase} 恢复失败：{self._safe_error_message(original_error)}"
        )

    @staticmethod
    def _safe_error_message(error: Exception) -> str:
        """生成简短错误信息，同时避免意外暴露常见 API Key。"""

        message = str(error).replace("\n", " ").strip()
        message = re.sub(
            r"\bsk-[A-Za-z0-9._-]{8,}\b",
            "[REDACTED_API_KEY]",
            message,
        )
        return (message or error.__class__.__name__)[:500]

    @property
    def is_timeout(self) -> bool:
        """把上游超时转成可读提示，原始异常仍保留给日志和排障链。"""

        return (
            isinstance(self.original_error, TimeoutError)
            or "timeout" in self.original_error.__class__.__name__.lower()
            or "timed out" in str(self.original_error).lower()
        )


class ModelRecoveryService:
    """为 LangChain ChatModel / Runnable 提供有限错误恢复。"""

    def __init__(self, *, context_compactor: Any = context_compactor_service) -> None:
        # 允许测试传入假的 compactor，这样不需要真的写文件或调用模型。
        self._context_compactor = context_compactor

    async def invoke(
        self,
        *,
        runnable: Any,
        messages: list[Any],
        phase: str,
        round_index: int,
        continuation_runnable: Any | None = None,
        max_attempts: int = 3,
        base_delay_seconds: float = 0.5,
        allow_continuation: bool = True,
        deadline: float | None = None,
        attempt_callback: Callable[[dict], None] | None = None,
    ) -> ModelCallResult:
        """调用模型，并在允许范围内做重试、压缩或续写。

        max_attempts 表示总尝试次数，不是“失败后再试几次”。
        默认 3 表示：第一次调用 + 最多两次重试。
        """

        from app.services.chat_execution_budget import chat_deadline, check_chat_active
        check_chat_active()
        shared_deadline = chat_deadline()
        if shared_deadline is not None:
            deadline = min(deadline, shared_deadline) if deadline is not None else shared_deadline
        self._validate_limits(
            max_attempts=max_attempts,
            base_delay_seconds=base_delay_seconds,
        )

        primary = await self._invoke_with_retries(
            runnable=runnable,
            messages=messages,
            phase=phase,
            round_index=round_index,
            max_attempts=max_attempts,
            base_delay_seconds=base_delay_seconds,
            deadline=deadline,
            attempt_callback=attempt_callback,
        )
        traces = list(primary.traces)
        compact_traces = list(primary.compact_traces)
        response = primary.response

        # tool_calls 可能需要继续进入 Agent Loop，不能把它当作普通文本续写。
        # 只有“没有工具调用的文本回答”被截断时，才进行一次 continuation。
        tool_calls = getattr(response, "tool_calls", None) or []
        finish_reason = self._finish_reason(response)

        if (
            allow_continuation
            and continuation_runnable is not None
            and not tool_calls
            and self._is_truncated_finish_reason(finish_reason)
        ):
            traces.append(
                RecoveryTrace(
                    event="ModelRecovery",
                    phase=phase,
                    round_index=round_index,
                    attempt=1,
                    max_attempts=max_attempts,
                    action="continuation_requested",
                    error_type="output_truncated",
                    message=(
                        "模型返回 finish_reason="
                        f"{finish_reason}，请求一次有上限的续写。"
                    ),
                    metadata={"finish_reason": finish_reason},
                )
            )

            continuation_messages = self._build_continuation_messages(
                messages=messages,
                response=response,
            )

            try:
                continuation = await self._invoke_with_retries(
                    runnable=continuation_runnable,
                    messages=continuation_messages,
                    phase=f"{phase}_continuation",
                    round_index=round_index,
                    max_attempts=max_attempts,
                    base_delay_seconds=base_delay_seconds,
                    deadline=deadline,
                    attempt_callback=attempt_callback,
                )
            except ModelRecoveryError as exc:
                # 把第一次调用和续写调用的 trace 合并，便于定位完整恢复过程。
                raise ModelRecoveryError(
                    phase=phase,
                    round_index=round_index,
                    original_error=exc.original_error,
                    traces=[*traces, *exc.traces],
                    compact_traces=[*compact_traces, *exc.compact_traces],
                ) from exc

            traces.extend(continuation.traces)
            compact_traces.extend(continuation.compact_traces)
            continuation_text = self._content_to_text(
                getattr(continuation.response, "content", "")
            )
            continuation_finish_reason = self._finish_reason(continuation.response)
            traces.append(
                RecoveryTrace(
                    event="ModelRecovery",
                    phase=phase,
                    round_index=round_index,
                    attempt=1,
                    max_attempts=max_attempts,
                    action="continuation_completed",
                    error_type="output_truncated",
                    message="已完成一次续写；即使仍被截断也不会无限续写。",
                    metadata={
                        "finish_reason": continuation_finish_reason,
                        "continuation_chars": len(continuation_text),
                        "still_truncated": self._is_truncated_finish_reason(
                            continuation_finish_reason
                        ),
                    },
                )
            )

            return ModelCallResult(
                response=response,
                continuation_text=continuation_text,
                continuation_response=continuation.response,
                traces=tuple(traces),
                compact_traces=tuple(compact_traces),
            )

        return ModelCallResult(
            response=response,
            traces=tuple(traces),
            compact_traces=tuple(compact_traces),
        )

    async def _invoke_with_retries(
        self,
        *,
        runnable: Any,
        messages: list[Any],
        phase: str,
        round_index: int,
        max_attempts: int,
        base_delay_seconds: float,
        deadline: float | None = None,
        attempt_callback: Callable[[dict], None] | None = None,
    ) -> _AttemptResult:
        """执行一组有限重试，并返回过程中产生的 trace。"""

        traces: list[RecoveryTrace] = []
        compact_traces: list[CompactTrace] = []
        context_recovery_used = False
        last_error_type = ""

        for attempt in range(1, max_attempts + 1):
            try:
                from app.services.chat_execution_budget import check_chat_active
                check_chat_active()
                # 不传deadline的旧业务保持原行为。季报传绝对截止时间，重试、续写、
                # 备用供应商只能使用剩余秒数，不能再各自领取一份90秒预算。
                if deadline is not None and deadline <= monotonic():
                    raise TimeoutError("模型共享时间预算已耗尽")
                started = monotonic()
                attempt_status = "ok"
                response = None
                try:
                    async with asyncio.timeout(None if deadline is None else deadline - monotonic()):
                        response = await runnable.ainvoke(messages)
                except asyncio.CancelledError:
                    attempt_status = "timeout" if deadline is not None and monotonic() >= deadline else "cancelled"
                    raise
                except Exception as exc:
                    attempt_status = "timeout" if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower() else "error"
                    raise
                finally:
                    if attempt_callback:
                        attempt_callback({"phase": phase, "round_index": round_index, "attempt": attempt,
                            "status": attempt_status, "duration_ms": round((monotonic() - started) * 1000, 2),
                            **model_attempt_metrics(messages, response)})
            except (AgentConfigError, AIControlError):
                # 缺少 API Key 之类的配置错误，重试不会变好，直接交给上层处理。
                raise
            except Exception as exc:
                check_chat_active()
                error_type = self.classify_error(exc)
                last_error_type = error_type
                exception_class = exc.__class__.__name__
                error_message = self._safe_error_message(exc)

                # 剩余不足3秒不再发起恢复请求；等待退避也必须计入共享预算。
                delay_seconds = base_delay_seconds * (2 ** (attempt - 1))
                if deadline is not None and deadline - monotonic() < delay_seconds + 3:
                    traces.append(RecoveryTrace(event="ModelRecovery", phase=phase, round_index=round_index,
                        attempt=attempt, max_attempts=max_attempts, action="deadline_exhausted", error_type=error_type,
                        message="剩余时间不足以再次调用模型，已停止重试。"))
                    raise ModelRecoveryError(phase=phase, round_index=round_index, original_error=exc,
                        traces=traces, compact_traces=compact_traces) from exc

                if (
                    error_type == "context_overflow"
                    and not context_recovery_used
                    and attempt < max_attempts
                ):
                    # Context Compact 会原地修改 messages。
                    # force=True 表示这是 API 已明确报告超限后的应急压缩。
                    new_compact_traces = self._context_compactor.compact_messages(
                        messages,
                        round_index=round_index,
                        max_total_chars=8_000,
                        max_tool_message_chars=1_500,
                        min_compact_chars=300,
                        preserve_recent_tool_messages=0,
                        force=True,
                    )

                    if new_compact_traces:
                        context_recovery_used = True
                        compact_traces.extend(new_compact_traces)
                        compacted_count = sum(
                            item.compacted_messages for item in new_compact_traces
                        )
                        traces.append(
                            RecoveryTrace(
                                event="ModelRecovery",
                                phase=phase,
                                round_index=round_index,
                                attempt=attempt,
                                max_attempts=max_attempts,
                                action="compact_and_retry",
                                error_type=error_type,
                                message=(
                                    "模型报告上下文超限，已压缩 "
                                    f"{compacted_count} 条 ToolMessage 后重试。"
                                ),
                                metadata={
                                    "exception_class": exception_class,
                                    "error": error_message,
                                    "compacted_messages": compacted_count,
                                },
                            )
                        )
                        continue

                    # 没有可压缩的 ToolMessage 时，再发相同请求没有意义。
                    traces.append(
                        RecoveryTrace(
                            event="ModelRecovery",
                            phase=phase,
                            round_index=round_index,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            action="context_compaction_unavailable",
                            error_type=error_type,
                            message="上下文超限，但没有找到可继续压缩的 ToolMessage。",
                            metadata={
                                "exception_class": exception_class,
                                "error": error_message,
                            },
                        )
                    )
                    raise ModelRecoveryError(
                        phase=phase,
                        round_index=round_index,
                        original_error=exc,
                        traces=traces,
                        compact_traces=compact_traces,
                    ) from exc

                if error_type == "transient" and attempt < max_attempts:
                    delay_seconds = base_delay_seconds * (2 ** (attempt - 1))
                    traces.append(
                        RecoveryTrace(
                            event="ModelRecovery",
                            phase=phase,
                            round_index=round_index,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            action="retry_scheduled",
                            error_type=error_type,
                            message=(
                                f"遇到临时错误，{delay_seconds:g} 秒后进行下一次尝试。"
                            ),
                            delay_seconds=delay_seconds,
                            metadata={
                                "exception_class": exception_class,
                                "error": error_message,
                            },
                        )
                    )

                    if delay_seconds > 0:
                        await asyncio.sleep(delay_seconds)
                    continue

                # fatal 错误、第二次 context overflow，或者已经用完尝试次数，都停止。
                traces.append(
                    RecoveryTrace(
                        event="ModelRecovery",
                        phase=phase,
                        round_index=round_index,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        action="recovery_exhausted",
                        error_type=error_type,
                        message="错误不可重试，或已经达到恢复次数上限。",
                        metadata={
                            "exception_class": exception_class,
                            "error": error_message,
                        },
                    )
                )
                raise ModelRecoveryError(
                    phase=phase,
                    round_index=round_index,
                    original_error=exc,
                    traces=traces,
                    compact_traces=compact_traces,
                ) from exc

            if attempt > 1:
                traces.append(
                    RecoveryTrace(
                        event="ModelRecovery",
                        phase=phase,
                        round_index=round_index,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        action="recovered",
                        error_type=last_error_type,
                        message=f"模型调用已在第 {attempt} 次尝试恢复成功。",
                    )
                )

            return _AttemptResult(
                response=response,
                traces=tuple(traces),
                compact_traces=tuple(compact_traces),
            )

        # 理论上 for 循环的成功和失败分支都会 return / raise。
        raise RuntimeError("模型恢复循环意外结束。")

    def classify_error(self, error: Exception) -> str:
        """把第三方 SDK 的各种异常归纳为三类。"""

        class_name = error.__class__.__name__.lower()
        message = str(error).lower()
        status_code = self._status_code(error)

        context_patterns = (
            "context_length_exceeded",
            "maximum context length",
            "context length exceeded",
            "context window",
            "too many tokens",
            "input is too long",
            "prompt is too long",
        )
        if any(pattern in message for pattern in context_patterns):
            return "context_overflow"

        transient_classes = (
            "ratelimit",
            "timeout",
            "connection",
            "internalserver",
            "serviceunavailable",
        )
        transient_patterns = (
            "rate limit",
            "too many requests",
            "timed out",
            "timeout",
            "connection error",
            "connection reset",
            "temporarily unavailable",
            "service unavailable",
            "internal server error",
            "bad gateway",
            "gateway timeout",
        )

        if status_code in TRANSIENT_STATUS_CODES:
            return "transient"

        if any(token in class_name for token in transient_classes):
            return "transient"

        if any(pattern in message for pattern in transient_patterns):
            return "transient"

        return "fatal"

    def _status_code(self, error: Exception) -> int | None:
        """兼容 OpenAI SDK 和普通 HTTP 异常的 status_code 位置。"""

        direct_status = getattr(error, "status_code", None)
        if isinstance(direct_status, int):
            return direct_status

        response = getattr(error, "response", None)
        response_status = getattr(response, "status_code", None)
        if isinstance(response_status, int):
            return response_status

        return None

    def _finish_reason(self, response: Any) -> str:
        """读取 OpenAI-compatible AIMessage 的 finish_reason。"""

        for attribute_name in ("response_metadata", "additional_kwargs"):
            metadata = getattr(response, attribute_name, None)
            if not isinstance(metadata, dict):
                continue

            for key in ("finish_reason", "stop_reason"):
                value = metadata.get(key)
                if value is not None:
                    return str(value).strip().lower()

        return ""

    def _is_truncated_finish_reason(self, finish_reason: str) -> bool:
        """判断模型是否因为输出 token 上限停止。"""

        return finish_reason in TRUNCATED_FINISH_REASONS

    def _build_continuation_messages(
        self,
        *,
        messages: list[Any],
        response: Any,
    ) -> list[Any]:
        """构造只允许一次的续写上下文。"""

        try:
            from langchain_core.messages import HumanMessage
        except ModuleNotFoundError as exc:
            raise AgentConfigError("请先安装依赖：pip install -r requirements.txt") from exc

        return [
            *messages,
            response,
            HumanMessage(
                content=(
                    "上一条回答因为达到输出长度上限被截断。"
                    "请从中断处继续，只输出后续内容，不要重复前文，也不要调用工具。"
                )
            ),
        ]

    def _content_to_text(self, content: Any) -> str:
        """把续写消息 content 转成最终可拼接字符串。"""

        if isinstance(content, str):
            return content

        return json.dumps(content, ensure_ascii=False)

    def _safe_error_message(self, error: Exception) -> str:
        """复用异常类中的脱敏逻辑。"""

        return ModelRecoveryError._safe_error_message(error)

    def _validate_limits(
        self,
        *,
        max_attempts: int,
        base_delay_seconds: float,
    ) -> None:
        """限制恢复参数，避免错误配置造成长时间等待。"""

        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts 必须在 1 到 5 之间。")

        if not 0 <= base_delay_seconds <= 10:
            raise ValueError("base_delay_seconds 必须在 0 到 10 之间。")


model_recovery_service = ModelRecoveryService()

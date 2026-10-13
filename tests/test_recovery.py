"""s11 Error Recovery 的本地故障注入测试。

这些测试使用假的 Runnable，不会读取 API Key，也不会调用 DeepSeek。
运行方式：

    conda run -n dl python -m unittest tests.test_recovery -v
"""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from app.services.context_compactor import CompactTrace
from app.services.recovery import ModelRecoveryError, ModelRecoveryService


class FakeResponse:
    """模拟 LangChain AIMessage，只保留恢复层需要的字段。"""

    def __init__(
        self,
        content: str,
        *,
        finish_reason: str = "stop",
        tool_calls: list[dict] | None = None,
    ) -> None:
        self.content = content
        self.response_metadata = {"finish_reason": finish_reason}
        self.tool_calls = tool_calls or []


class SequenceRunnable:
    """按顺序返回结果或抛出异常，方便稳定制造故障。"""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def ainvoke(self, messages: list[object]) -> object:
        self.calls += 1
        outcome = self._outcomes.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        return outcome


class FakeContextCompactor:
    """模拟 Context Compact，避免测试写入 data/agent_context。"""

    def __init__(self) -> None:
        self.calls = 0

    def compact_messages(self, messages: list[object], **kwargs: object) -> list[CompactTrace]:
        self.calls += 1
        before_chars = sum(len(str(getattr(item, "content", ""))) for item in messages)

        if messages:
            messages[-1].content = '{"context_compacted": true}'

        return [
            CompactTrace(
                event="ContextCompact",
                round_index=int(kwargs["round_index"]),
                before_chars=before_chars,
                after_chars=28,
                compacted_messages=1,
                reason="compacted for deterministic test",
                saved_paths=["data/agent_context/fake-test.json"],
            )
        ]


class ErrorRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_stops_after_two_total_attempts(self) -> None:
        """两次总尝试只重试一次，不会误解为额外重试两次。"""

        runnable = SequenceRunnable([TimeoutError("Request timed out.")] * 3)
        service = ModelRecoveryService(context_compactor=FakeContextCompactor())
        with self.assertRaises(ModelRecoveryError) as caught:
            await service.invoke(
                runnable=runnable, messages=[], phase="agent_round", round_index=2,
                max_attempts=2, base_delay_seconds=0,
            )
        self.assertEqual(runnable.calls, 2)
        self.assertTrue(caught.exception.is_timeout)
        self.assertEqual(caught.exception.round_index, 2)
        self.assertEqual(caught.exception.traces[-1].action, "recovery_exhausted")

    def test_sdk_timeout_class_and_fatal_error_are_distinguished(self) -> None:
        # SDK 超时不一定继承 Python 的 TimeoutError，因此也覆盖类名识别。
        class APITimeoutError(Exception):
            pass

        for error, expected in ((APITimeoutError("Request timeout"), True),
                                (ValueError("invalid parameter"), False)):
            wrapped = ModelRecoveryError(phase="test", round_index=1, original_error=error,
                                         traces=[], compact_traces=[])
            self.assertEqual(wrapped.is_timeout, expected)

    async def test_transient_error_retries_then_recovers(self) -> None:
        """超时属于临时错误，应等待后重试并成功。"""

        runnable = SequenceRunnable(
            [
                TimeoutError("request timed out"),
                FakeResponse("恢复成功"),
            ]
        )
        service = ModelRecoveryService(context_compactor=FakeContextCompactor())

        result = await service.invoke(
            runnable=runnable,
            messages=[],
            phase="test_transient",
            round_index=1,
            max_attempts=3,
            base_delay_seconds=0,
        )

        self.assertEqual(runnable.calls, 2)
        self.assertEqual(result.response.content, "恢复成功")
        self.assertEqual(
            [trace.action for trace in result.traces],
            ["retry_scheduled", "recovered"],
        )

    async def test_context_overflow_compacts_then_retries(self) -> None:
        """上下文超限时应先压缩 ToolMessage，再重新调用模型。"""

        runnable = SequenceRunnable(
            [
                RuntimeError("maximum context length exceeded"),
                FakeResponse("压缩后成功"),
            ]
        )
        compactor = FakeContextCompactor()
        service = ModelRecoveryService(context_compactor=compactor)
        messages = [
            SimpleNamespace(
                type="tool",
                tool_call_id="fake-tool-call",
                content="x" * 2_000,
            )
        ]

        result = await service.invoke(
            runnable=runnable,
            messages=messages,
            phase="test_context",
            round_index=2,
            max_attempts=3,
            base_delay_seconds=0,
        )

        self.assertEqual(runnable.calls, 2)
        self.assertEqual(compactor.calls, 1)
        self.assertEqual(len(result.compact_traces), 1)
        self.assertEqual(
            [trace.action for trace in result.traces],
            ["compact_and_retry", "recovered"],
        )

    async def test_truncated_output_continues_only_once(self) -> None:
        """finish_reason=length 时续写一次，即使再次截断也不会循环。"""

        primary = SequenceRunnable(
            [FakeResponse("第一段", finish_reason="length")]
        )
        continuation = SequenceRunnable(
            [FakeResponse("第二段", finish_reason="length")]
        )
        service = ModelRecoveryService(context_compactor=FakeContextCompactor())

        result = await service.invoke(
            runnable=primary,
            messages=[],
            phase="test_continuation",
            round_index=3,
            continuation_runnable=continuation,
            max_attempts=3,
            base_delay_seconds=0,
        )

        self.assertEqual(primary.calls, 1)
        self.assertEqual(continuation.calls, 1)
        self.assertEqual(result.continuation_text, "第二段")
        self.assertEqual(
            [trace.action for trace in result.traces],
            ["continuation_requested", "continuation_completed"],
        )
        self.assertTrue(result.traces[-1].metadata["still_truncated"])

    async def test_fatal_error_does_not_retry(self) -> None:
        """无效参数属于不可恢复错误，重复发送相同请求没有意义。"""

        runnable = SequenceRunnable([ValueError("invalid request parameter")])
        service = ModelRecoveryService(context_compactor=FakeContextCompactor())

        with self.assertRaises(ModelRecoveryError) as caught:
            await service.invoke(
                runnable=runnable,
                messages=[],
                phase="test_fatal",
                round_index=4,
                max_attempts=3,
                base_delay_seconds=0,
            )

        self.assertEqual(runnable.calls, 1)
        self.assertEqual(caught.exception.traces[-1].action, "recovery_exhausted")
        self.assertEqual(caught.exception.traces[-1].error_type, "fatal")


if __name__ == "__main__":
    unittest.main()

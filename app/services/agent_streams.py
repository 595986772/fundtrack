"""主聊天 SSE 运行的轻量取消注册表。

它只保存当前进程内“正在 await 的 asyncio.Task”。聊天结果和会话仍进入 SQL；
注册表不是持久化数据，也不承担任务恢复。浏览器点击停止时，通过 stream_id 找到
对应 Task 并发出协作式取消，Agent 会在当前可取消的 await 边界停止。
"""

from __future__ import annotations

import asyncio
from threading import Lock
from dataclasses import dataclass


@dataclass(frozen=True)
class ActiveAgentStream:
    task: asyncio.Task[None]
    user_id: str


class AgentStreamRegistry:
    """线程安全地登记、取消和移除当前 API 进程中的聊天 Task。"""

    def __init__(self) -> None:
        self._tasks: dict[str, ActiveAgentStream] = {}
        self._lock = Lock()

    def register(
        self,
        *,
        stream_id: str,
        task: asyncio.Task[None],
        user_id: str,
    ) -> None:
        with self._lock:
            self._tasks[stream_id] = ActiveAgentStream(task=task, user_id=user_id)

    def cancel(self, *, stream_id: str, user_id: str | None = None) -> bool:
        with self._lock:
            active = self._tasks.get(stream_id)
        if active is None or active.task.done():
            return False
        if user_id is not None and active.user_id != user_id:
            return False
        active.task.cancel()
        return True

    def unregister(self, *, stream_id: str) -> None:
        with self._lock:
            self._tasks.pop(stream_id, None)


agent_stream_registry = AgentStreamRegistry()

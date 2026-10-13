"""TodoWrite 的内存状态服务。

TodoWrite 对应 Agent Harness 里的“短期计划”能力：
- 它不是数据库任务系统；
- 它不负责真正执行任务；
- 它只让 Agent 在复杂任务中显式维护计划状态，避免做着做着跑偏。

当前 demo 先把 todo 保存在内存里。服务重启后会清空。
后面学习 s12 Task System 时，再做可持久化、可依赖、可认领的任务系统。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from threading import Lock
from typing import Any, Literal


TodoStatus = Literal["pending", "in_progress", "completed"]
TODO_STATUSES = {"pending", "in_progress", "completed"}
MAX_TODO_ITEMS = 20


@dataclass(frozen=True)
class TodoItem:
    """一条短期计划。

    id:
        稳定编号，方便后续更新同一条 todo。
    content:
        人能看懂的任务内容。
    status:
        当前状态。这个 demo 只保留三个最常见状态。
    """

    id: str
    content: str
    status: TodoStatus


class TodoStateService:
    """按 user_id 保存当前会话里的 todo 列表。"""

    def __init__(self) -> None:
        self._todos_by_user: dict[str, list[TodoItem]] = {}
        self._lock = Lock()

    def replace(self, *, user_id: str, raw_items: list[Any]) -> list[dict[str, str]]:
        """用新 todo 列表替换某个用户的当前计划。

        LangChain 工具调用拿到的是普通 JSON 参数，所以这里主动做一层校验。
        这层校验是 Harness 的责任：模型可以提出计划，但后端负责保证结构合法。
        """

        normalized_user_id = self._normalize_user_id(user_id)
        items = self._validate_items(raw_items)

        with self._lock:
            self._todos_by_user[normalized_user_id] = items
            return [self._to_dict(item) for item in items]

    def list(self, *, user_id: str) -> list[dict[str, str]]:
        """查看某个用户当前的 todo 列表。"""

        normalized_user_id = self._normalize_user_id(user_id)

        with self._lock:
            return [
                self._to_dict(item)
                for item in self._todos_by_user.get(normalized_user_id, [])
            ]

    def _validate_items(self, raw_items: list[Any]) -> list[TodoItem]:
        """把模型传来的 JSON 参数变成可靠的 TodoItem 列表。"""

        if len(raw_items) > MAX_TODO_ITEMS:
            raise ValueError(f"todo 最多允许 {MAX_TODO_ITEMS} 条，避免计划过长。")

        items: list[TodoItem] = []
        seen_ids: set[str] = set()
        in_progress_count = 0

        for index, raw_item in enumerate(raw_items, start=1):
            item_data = self._as_dict(raw_item)
            item_id = str(item_data.get("id") or "").strip()
            content = str(item_data.get("content") or "").strip()
            status = str(item_data.get("status") or "").strip()

            if not item_id:
                raise ValueError(f"第 {index} 条 todo 缺少 id。")

            if item_id in seen_ids:
                raise ValueError(f"todo id 重复：{item_id}")

            if len(item_id) > 50:
                raise ValueError(f"todo id 过长：{item_id}")

            if not content:
                raise ValueError(f"第 {index} 条 todo 缺少 content。")

            if len(content) > 300:
                raise ValueError(f"第 {index} 条 todo 内容太长，请控制在 300 字以内。")

            if status not in TODO_STATUSES:
                raise ValueError(
                    f"第 {index} 条 todo 状态非法：{status}，"
                    "只能是 pending、in_progress、completed。"
                )

            if status == "in_progress":
                in_progress_count += 1

            seen_ids.add(item_id)
            items.append(
                TodoItem(
                    id=item_id,
                    content=content,
                    status=status,  # type: ignore[arg-type]
                )
            )

        if in_progress_count > 1:
            raise ValueError("同一时间最多只能有一条 todo 是 in_progress。")

        return items

    def _normalize_user_id(self, user_id: str) -> str:
        """限制 user_id，避免空字符串或特别长的 key。"""

        normalized = str(user_id or "guest").strip() or "guest"
        return normalized[:50]

    def _as_dict(self, raw_item: Any) -> dict[str, Any]:
        """兼容 dict 和 Pydantic 对象。"""

        if isinstance(raw_item, dict):
            return raw_item

        if hasattr(raw_item, "model_dump"):
            return raw_item.model_dump()

        if hasattr(raw_item, "dict"):
            return raw_item.dict()

        raise ValueError("todo item 必须是 JSON object。")

    def _to_dict(self, item: TodoItem) -> dict[str, str]:
        """统一转换成可 JSON 序列化的 dict。"""

        return asdict(item)


todo_state = TodoStateService()

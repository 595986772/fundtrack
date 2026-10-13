"""s12 Task System：可持久化、支持依赖关系的任务服务。

TodoWrite 和 Task System 解决的是不同问题：

- TodoWrite 是当前复杂请求里的临时执行计划，服务重启后会清空；
- Task System 是跨请求、跨重启保存的正式任务，可以记录负责人和依赖关系。

当前教学版把每个任务保存成 ``data/tasks/<user_id>/<task_id>.json``。
真实生产系统通常会换成数据库，但任务状态机和依赖校验思路基本相同。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import fcntl
import json
from pathlib import Path
import re
from threading import RLock
from typing import Any, Literal
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASKS_ROOT = PROJECT_ROOT / "data" / "tasks"

# TaskStatus 是任务状态机允许出现的全部状态。
# 这里用 Literal 是为了让编辑器、类型检查器和读代码的人都知道：
# 任务状态不能随便写字符串，只能在这四个值里选。
TaskStatus = Literal["pending", "in_progress", "completed", "cancelled"]

# 下面这些常量是 Task System 的“边界条件”。
# 教学版先用简单限制防止数据过大、路径不安全、依赖链过乱。
TASK_STATUSES = {"pending", "in_progress", "completed", "cancelled"}
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SIMPLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$")

MAX_TASKS_PER_USER = 200
MAX_DEPENDENCIES = 20
MAX_SUBJECT_CHARS = 200
MAX_DESCRIPTION_CHARS = 2_000


@dataclass(frozen=True)
class TaskRecord:
    """写入 JSON 文件的一条正式任务。

    这个类可以理解成“数据库表的一行”。
    当前项目暂时不用 MySQL/PostgreSQL，而是把每个 TaskRecord 存成一个
    JSON 文件，所以这里的字段基本就是 JSON 文件里的字段。

    frozen=True 表示创建后不直接修改对象本身。
    如果要改任务状态，代码会用 dataclasses.replace() 生成一个新对象，
    这样状态流转更清楚，也不容易在中途把旧对象改乱。
    """

    # id 是任务自己的稳定编号，同时也会成为 JSON 文件名。
    id: str
    # user_id 决定任务保存到 data/tasks/<user_id>/ 下面。
    # 这样不同用户的任务不会混在一起。
    user_id: str
    # subject 是短标题，适合放在任务列表里快速浏览。
    subject: str
    # description 是更长的说明，可以写验收标准、背景和边界。
    description: str
    # status 表示当前任务进行到哪里。
    status: TaskStatus
    # owner 是认领者。pending 任务通常没有 owner，被 claim 后才有。
    owner: str | None
    # blocked_by 保存“当前任务依赖哪些任务先完成”的 ID 列表。
    blocked_by: list[str]
    # s18：可选的安全 worktree 名称。真正路径和分支由 WorktreeService 管理。
    worktree_name: str | None
    # created_at/updated_at 用字符串保存，方便直接写入 JSON。
    created_at: str
    updated_at: str


class TaskStoreService:
    """负责任务持久化、状态流转和依赖校验。

    你可以把它看成 Task System 的 service 层：

    - API 层或 Tool 层只负责接收请求；
    - TaskStoreService 负责真正的业务规则；
    - JSON 文件只是当前教学版的存储介质。

    以后如果换成数据库，最应该替换的是读写文件这部分，
    但 create/list/update/claim 这几个业务动作仍然可以保留类似结构。
    """

    def __init__(self, *, root: Path = TASKS_ROOT) -> None:
        # root 可以在测试中替换成临时目录，避免污染真实 data/tasks/。
        self.root = Path(root)
        self._lock = RLock()

    def create_task(
        self,
        *,
        user_id: str,
        subject: str,
        description: str = "",
        blocked_by: list[str] | None = None,
        task_id: str | None = None,
        worktree_name: str | None = None,
    ) -> dict[str, Any]:
        """创建 pending 任务，并立即写入 JSON。"""

        # 先统一清洗输入。这样后面的业务逻辑拿到的都是“安全、规范”的值。
        normalized_user_id = self._normalize_user_id(user_id)
        normalized_subject = self._normalize_subject(subject)
        normalized_description = self._normalize_description(description)
        normalized_task_id = (
            self._normalize_task_id(task_id)
            if task_id
            else f"task-{uuid4().hex[:10]}"
        )
        normalized_dependencies = self._normalize_dependencies(blocked_by or [])
        normalized_worktree = (
            self._normalize_worktree_name(worktree_name)
            if worktree_name
            else None
        )

        with self._board_write_lock(normalized_user_id):
            # 读出当前用户所有任务，因为创建任务时要检查：
            # 1. 数量是否超限；
            # 2. task_id 是否重复；
            # 3. blocked_by 中引用的依赖是否存在；
            # 4. 新任务加入后是否形成循环依赖。
            tasks = self._load_task_map(normalized_user_id)

            if len(tasks) >= MAX_TASKS_PER_USER:
                raise ValueError(
                    f"每个用户最多保存 {MAX_TASKS_PER_USER} 个任务。"
                )

            if normalized_task_id in tasks:
                raise ValueError(f"任务 ID 已存在：{normalized_task_id}")

            self._validate_dependencies_exist(
                dependency_ids=normalized_dependencies,
                tasks=tasks,
            )

            now = self._now()
            # 新创建的任务一定从 pending 开始。
            # 它还没有被任何 Agent 认领，所以 owner 是 None。
            record = TaskRecord(
                id=normalized_task_id,
                user_id=normalized_user_id,
                subject=normalized_subject,
                description=normalized_description,
                status="pending",
                owner=None,
                blocked_by=normalized_dependencies,
                worktree_name=normalized_worktree,
                created_at=now,
                updated_at=now,
            )

            # proposed_tasks 表示“如果把新任务放进去之后”的完整任务图。
            # 先在内存里验证通过，再真正写文件。
            proposed_tasks = {**tasks, normalized_task_id: record}
            self._validate_no_dependency_cycle(proposed_tasks)
            self._write_record(record)

            return {
                "ok": True,
                "task": self._record_to_public_dict(record, proposed_tasks),
                "message": "任务已持久化到 data/tasks/。",
            }

    def list_tasks(
        self,
        *,
        user_id: str,
        status: str | None = None,
        owner: str | None = None,
        only_blocked: bool = False,
        max_results: int = 50,
    ) -> dict[str, Any]:
        """查看当前用户任务，并可按状态、负责人或阻塞状态过滤。"""

        # 查询类接口也要清洗 user_id/status/owner，避免读取到越权目录。
        normalized_user_id = self._normalize_user_id(user_id)
        normalized_status = self._normalize_status(status) if status else None
        normalized_owner = self._normalize_owner(owner) if owner else None
        safe_limit = max(1, min(int(max_results), 100))

        with self._lock:
            tasks = self._load_task_map(normalized_user_id)
            # public_tasks 会在原始 JSON 字段之外，额外带上 is_blocked/can_claim。
            # 这些是根据当前依赖状态动态算出来的，不需要写回 JSON。
            public_tasks = [
                self._record_to_public_dict(record, tasks)
                for record in tasks.values()
            ]

        if normalized_status:
            public_tasks = [
                item for item in public_tasks if item["status"] == normalized_status
            ]

        if normalized_owner:
            public_tasks = [
                item for item in public_tasks if item["owner"] == normalized_owner
            ]

        if only_blocked:
            public_tasks = [item for item in public_tasks if item["is_blocked"]]

        # 排序规则让“正在做”和“待认领”的任务排在前面，
        # completed/cancelled 这种历史任务排在后面。
        status_order = {
            "in_progress": 0,
            "pending": 1,
            "completed": 2,
            "cancelled": 3,
        }
        public_tasks.sort(
            key=lambda item: (
                status_order.get(str(item["status"]), 99),
                str(item["created_at"]),
            )
        )
        selected = public_tasks[:safe_limit]

        return {
            "ok": True,
            "user_id": normalized_user_id,
            "count": len(selected),
            "total_count": len(tasks),
            "tasks": selected,
        }

    def get_task(self, *, user_id: str, task_id: str) -> dict[str, Any]:
        """按稳定 ID 读取一条 Task，不受列表分页上限影响。"""

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_task_id = self._normalize_task_id(task_id)
        with self._lock:
            tasks = self._load_task_map(normalized_user_id)
            record = self._require_task(tasks, normalized_task_id)
            return {
                "ok": True,
                "task": self._record_to_public_dict(record, tasks),
            }

    def update_task(
        self,
        *,
        user_id: str,
        task_id: str,
        subject: str | None = None,
        description: str | None = None,
        status: str | None = None,
        blocked_by: list[str] | None = None,
        worktree_name: str | None = None,
    ) -> dict[str, Any]:
        """更新任务内容、依赖或状态。

        ``in_progress`` 不能通过这里直接设置，必须调用 claim_task。
        这样可以保证认领前一定检查依赖和 owner。
        """

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_task_id = self._normalize_task_id(task_id)

        if all(
            value is None
            for value in (subject, description, status, blocked_by, worktree_name)
        ):
            raise ValueError("至少提供一个要更新的字段。")

        with self._board_write_lock(normalized_user_id):
            tasks = self._load_task_map(normalized_user_id)
            current = self._require_task(tasks, normalized_task_id)
            # updated 会随着每个可选字段逐步 replace。
            # current 始终代表“更新前”的任务，方便校验状态流转。
            updated = current

            if subject is not None:
                updated = replace(updated, subject=self._normalize_subject(subject))

            if description is not None:
                updated = replace(
                    updated,
                    description=self._normalize_description(description),
                )

            if blocked_by is not None:
                if current.status != "pending":
                    raise ValueError("只有 pending 任务可以修改 blocked_by。")

                # 依赖关系只允许在任务开始前改。
                # 一旦任务进入 in_progress，再改依赖会让“为什么能开始”变得不清楚。
                normalized_dependencies = self._normalize_dependencies(blocked_by)

                if normalized_task_id in normalized_dependencies:
                    raise ValueError("任务不能依赖自己。")

                self._validate_dependencies_exist(
                    dependency_ids=normalized_dependencies,
                    tasks=tasks,
                )
                updated = replace(updated, blocked_by=normalized_dependencies)

            if worktree_name is not None:
                if current.status != "pending" or current.owner is not None:
                    raise ValueError("只有尚未认领的 pending 任务可以绑定 worktree。")
                updated = replace(
                    updated,
                    worktree_name=self._normalize_worktree_name(worktree_name),
                )

            if status is not None:
                normalized_status = self._normalize_status(status)
                # 状态机是 Task System 的核心规则：
                # 例如 pending 不能直接 completed，必须先 claim 成 in_progress。
                self._validate_status_transition(
                    current_status=current.status,
                    requested_status=normalized_status,
                )

                next_owner = updated.owner
                if normalized_status in {"pending", "cancelled"}:
                    # 退回 pending 或取消时释放 owner，后续可重新认领。
                    next_owner = None

                updated = replace(
                    updated,
                    status=normalized_status,  # type: ignore[arg-type]
                    owner=next_owner,
                )

            updated = replace(updated, updated_at=self._now())
            proposed_tasks = {**tasks, normalized_task_id: updated}
            # 更新 blocked_by 后也要重新检查循环依赖。
            self._validate_no_dependency_cycle(proposed_tasks)
            self._write_record(updated)

            # 如果这次更新把某个任务改成 completed，
            # 这里会顺手告诉调用方：哪些下游任务现在不再被阻塞了。
            newly_unblocked = self._newly_unblocked_task_ids(
                completed_task=updated,
                tasks=proposed_tasks,
            )

            return {
                "ok": True,
                "task": self._record_to_public_dict(updated, proposed_tasks),
                "newly_unblocked_task_ids": newly_unblocked,
                "message": "任务已更新。",
            }

    def bind_worktree(
        self,
        *,
        user_id: str,
        task_id: str,
        worktree_name: str,
    ) -> dict[str, Any]:
        """把 pending Task 和已创建 worktree 绑定，不提前改变任务状态。"""

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_task_id = self._normalize_task_id(task_id)
        normalized_worktree = self._normalize_worktree_name(worktree_name)

        with self._board_write_lock(normalized_user_id):
            tasks = self._load_task_map(normalized_user_id)
            current = self._require_task(tasks, normalized_task_id)
            if current.status != "pending" or current.owner is not None:
                raise ValueError("只有尚未认领的 pending 任务可以绑定 worktree。")
            if current.worktree_name and current.worktree_name != normalized_worktree:
                raise ValueError(
                    f"任务已经绑定 worktree：{current.worktree_name}"
                )

            updated = replace(
                current,
                worktree_name=normalized_worktree,
                updated_at=self._now(),
            )
            proposed_tasks = {**tasks, normalized_task_id: updated}
            self._write_record(updated)
            return {
                "ok": True,
                "task": self._record_to_public_dict(updated, proposed_tasks),
                "message": "Task 已绑定 worktree，状态仍保持 pending。",
            }

    def unbind_worktree(
        self,
        *,
        user_id: str,
        task_id: str,
        worktree_name: str,
    ) -> dict[str, Any]:
        """只供 Worktree 创建失败回滚绑定，不暴露为模型工具。

        正常生命周期保留 Task 上的 worktree_name 作为审计线索；只有 Git/数据库创建在
        中途失败时，才需要把仍为 pending 的 Task 恢复成未绑定状态。
        """

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_task_id = self._normalize_task_id(task_id)
        normalized_worktree = self._normalize_worktree_name(worktree_name)

        with self._board_write_lock(normalized_user_id):
            tasks = self._load_task_map(normalized_user_id)
            current = self._require_task(tasks, normalized_task_id)
            if current.status != "pending" or current.owner is not None:
                raise ValueError("已认领的 Task 不能回滚 Worktree 绑定。")
            if current.worktree_name != normalized_worktree:
                raise ValueError("Task 当前绑定与要回滚的 Worktree 不一致。")

            updated = replace(
                current,
                worktree_name=None,
                updated_at=self._now(),
            )
            proposed_tasks = {**tasks, normalized_task_id: updated}
            self._write_record(updated)
            return {
                "ok": True,
                "task": self._record_to_public_dict(updated, proposed_tasks),
                "message": "失败的 Worktree 创建已回滚 Task 绑定。",
            }

    def claim_task(
        self,
        *,
        user_id: str,
        task_id: str,
        owner: str,
    ) -> dict[str, Any]:
        """认领一个未阻塞的 pending 任务。"""

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_task_id = self._normalize_task_id(task_id)
        normalized_owner = self._normalize_owner(owner)

        with self._board_write_lock(normalized_user_id):
            tasks = self._load_task_map(normalized_user_id)
            current = self._require_task(tasks, normalized_task_id)

            # 已经 in_progress 的任务不能被别人重复抢走。
            # 如果同一个 owner 再次认领，直接返回成功，方便重复请求保持幂等。
            if current.status == "in_progress":
                if current.owner == normalized_owner:
                    return {
                        "ok": True,
                        "task": self._record_to_public_dict(current, tasks),
                        "message": "该任务已经由同一 owner 认领。",
                    }

                raise ValueError(
                    f"任务已由 {current.owner or '其他 owner'} 认领。"
                )

            if current.status != "pending":
                raise ValueError(
                    f"只有 pending 任务可以认领，当前状态是 {current.status}。"
                )

            # 认领前必须检查依赖。只要 blocked_by 中还有未 completed 的任务，
            # 当前任务就不能进入 in_progress。
            unresolved = self._unresolved_dependency_ids(current, tasks)
            if unresolved:
                raise ValueError(
                    "任务仍被依赖阻塞，未完成依赖：" + ", ".join(unresolved)
                )

            claimed = replace(
                current,
                status="in_progress",
                owner=normalized_owner,
                updated_at=self._now(),
            )
            proposed_tasks = {**tasks, normalized_task_id: claimed}
            self._write_record(claimed)

            return {
                "ok": True,
                "task": self._record_to_public_dict(claimed, proposed_tasks),
                "message": f"任务已由 {normalized_owner} 认领。",
            }

    def list_claimable_tasks(
        self,
        *,
        user_id: str,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        """返回 s17 自治成员当前真正可以认领的任务。

        “可以认领”同时满足三个条件：pending、没有 owner、所有依赖都 completed。
        扫描只是发现候选项，最终仍必须调用 ``claim_task`` 在文件锁内重新校验。
        """

        result = self.list_tasks(
            user_id=user_id,
            status="pending",
            max_results=max(1, min(int(max_results), 100)),
        )
        return [
            task
            for task in result["tasks"]
            if task["owner"] is None and task["can_claim"]
        ]

    def complete_task(
        self,
        *,
        user_id: str,
        task_id: str,
        owner: str,
    ) -> dict[str, Any]:
        """只允许当前 owner 完成自己已经认领的任务。

        s12 的通用 ``update_task`` 主要供主 Agent 管理任务；s17 自治执行需要更严格的
        owner 检查，避免一个 teammate 把另一个 teammate 的任务标成 completed。
        """

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_task_id = self._normalize_task_id(task_id)
        normalized_owner = self._normalize_owner(owner)

        with self._board_write_lock(normalized_user_id):
            tasks = self._load_task_map(normalized_user_id)
            current = self._require_task(tasks, normalized_task_id)
            if current.status == "completed" and current.owner == normalized_owner:
                return {
                    "ok": True,
                    "task": self._record_to_public_dict(current, tasks),
                    "newly_unblocked_task_ids": [],
                    "message": "该任务已经由同一 owner 完成。",
                }
            if current.status != "in_progress":
                raise ValueError(
                    f"只有 in_progress 任务可以完成，当前状态是 {current.status}。"
                )
            if current.owner != normalized_owner:
                raise ValueError(
                    f"只有当前 owner {current.owner or 'none'} 可以完成该任务。"
                )

            completed = replace(
                current,
                status="completed",
                updated_at=self._now(),
            )
            proposed_tasks = {**tasks, normalized_task_id: completed}
            self._write_record(completed)
            return {
                "ok": True,
                "task": self._record_to_public_dict(completed, proposed_tasks),
                "newly_unblocked_task_ids": self._newly_unblocked_task_ids(
                    completed_task=completed,
                    tasks=proposed_tasks,
                ),
                "message": f"任务已由 {normalized_owner} 完成。",
            }

    def recent_index(
        self,
        *,
        user_id: str,
        max_items: int = 20,
    ) -> list[dict[str, Any]]:
        """给 `/agent/chat` 返回当前任务状态。

        这个方法面向接口响应使用，所以返回的是结构化 list。
        前端或测试可以直接读 current_tasks 字段。
        """

        result = self.list_tasks(
            user_id=user_id,
            max_results=max(1, min(max_items, 50)),
        )
        return list(result["tasks"])

    def index_for_prompt(self, *, user_id: str, max_items: int = 10) -> str:
        """生成放入 system prompt 的轻量任务索引。

        这个方法面向大模型使用，所以返回的是一段文本。
        Prompt Builder 会把它拼进 system prompt，让模型知道当前用户已有任务。
        """

        tasks = self.recent_index(user_id=user_id, max_items=max_items)

        if not tasks:
            return "当前用户暂无持久化任务。"

        lines = ["当前用户持久化任务索引（只包含轻量状态）："]

        for task in tasks:
            display_status = "blocked" if task["is_blocked"] else task["status"]
            owner = task["owner"] or "unassigned"
            dependencies = task["unresolved_dependencies"] or []
            lines.append(
                "- "
                f"[{display_status}] {task['id']}: {task['subject']} "
                f"(owner={owner}, unresolved={dependencies})"
            )

        return "\n".join(lines)

    def _load_task_map(self, user_id: str) -> dict[str, TaskRecord]:
        """读取一个用户目录下的所有 JSON 任务。"""

        folder = self.root / user_id
        if not folder.is_dir():
            return {}

        tasks: dict[str, TaskRecord] = {}

        for path in sorted(folder.glob("*.json")):
            try:
                # 这里不直接信任 JSON 文件内容，而是重新走一遍 normalize。
                # 如果文件被手动改坏，会尽早抛出清楚的错误。
                data = json.loads(path.read_text(encoding="utf-8"))
                record = self._record_from_dict(data)
            except Exception as exc:
                raise ValueError(f"任务文件损坏：{self._display_path(path)}") from exc

            if record.user_id != user_id:
                raise ValueError(
                    f"任务文件 user_id 不匹配：{self._display_path(path)}"
                )

            tasks[record.id] = record

        return tasks

    def _write_record(self, record: TaskRecord) -> None:
        """原子写入任务 JSON，避免留下只写了一半的文件。"""

        folder = self.root / record.user_id
        folder.mkdir(parents=True, exist_ok=True)
        target_path = folder / f"{record.id}.json"
        temporary_path = folder / f".{record.id}.{uuid4().hex[:8]}.tmp"
        payload = {"version": 2, **asdict(record)}

        # 先写临时文件，再 replace 成正式文件。
        # 如果程序在写入中途崩溃，正式文件不会变成半截 JSON。
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(target_path)

    def _record_from_dict(self, data: dict[str, Any]) -> TaskRecord:
        """把 JSON 数据恢复成 TaskRecord。"""

        status = self._normalize_status(str(data.get("status") or ""))
        owner_value = data.get("owner")
        owner = self._normalize_owner(str(owner_value)) if owner_value else None

        return TaskRecord(
            id=self._normalize_task_id(str(data.get("id") or "")),
            user_id=self._normalize_user_id(str(data.get("user_id") or "")),
            subject=self._normalize_subject(str(data.get("subject") or "")),
            description=self._normalize_description(
                str(data.get("description") or "")
            ),
            status=status,  # type: ignore[arg-type]
            owner=owner,
            blocked_by=self._normalize_dependencies(
                list(data.get("blocked_by") or data.get("blockedBy") or [])
            ),
            worktree_name=(
                self._normalize_worktree_name(str(data.get("worktree_name")))
                if data.get("worktree_name")
                else None
            ),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )

    def _record_to_public_dict(
        self,
        record: TaskRecord,
        tasks: dict[str, TaskRecord],
    ) -> dict[str, Any]:
        """增加 is_blocked、can_claim 等运行时派生字段。"""

        # unresolved/is_blocked/can_claim 都是“当前时刻算出来”的状态。
        # 例如某个依赖刚 completed，下游任务的 can_claim 会立刻变成 True。
        unresolved = self._unresolved_dependency_ids(record, tasks)
        path = self.root / record.user_id / f"{record.id}.json"

        return {
            **asdict(record),
            "unresolved_dependencies": unresolved,
            "is_blocked": bool(unresolved),
            "can_claim": (
                record.status == "pending"
                and record.owner is None
                and not unresolved
            ),
            "path": self._display_path(path),
        }

    def _unresolved_dependency_ids(
        self,
        record: TaskRecord,
        tasks: dict[str, TaskRecord],
    ) -> list[str]:
        """返回尚未 completed 的依赖任务 ID。"""

        return [
            dependency_id
            for dependency_id in record.blocked_by
            if dependency_id not in tasks
            or tasks[dependency_id].status != "completed"
        ]

    def _newly_unblocked_task_ids(
        self,
        *,
        completed_task: TaskRecord,
        tasks: dict[str, TaskRecord],
    ) -> list[str]:
        """任务完成后，找出因此刚刚可以认领的下游任务。"""

        if completed_task.status != "completed":
            return []

        return sorted(
            record.id
            for record in tasks.values()
            if record.status == "pending"
            and completed_task.id in record.blocked_by
            and not self._unresolved_dependency_ids(record, tasks)
        )

    def _validate_dependencies_exist(
        self,
        *,
        dependency_ids: list[str],
        tasks: dict[str, TaskRecord],
    ) -> None:
        """拒绝悬空依赖，避免任务永远无法解除阻塞。"""

        missing = [task_id for task_id in dependency_ids if task_id not in tasks]
        if missing:
            raise ValueError("依赖任务不存在：" + ", ".join(missing))

    @contextmanager
    def _board_write_lock(self, user_id: str):
        """同时使用线程锁和文件锁保护一次任务板“读-检查-写”。

        ``RLock`` 只能保护同一个 Python 进程。Docker 中 API 与多个 Celery Worker 是
        不同进程，所以还要对共享 ``data/tasks/<user_id>/.board.lock`` 使用 flock。
        两个 teammate 同时 claim 时，只有拿到锁的进程能先完成读取和写入，另一个进程
        随后会读到最新 owner，从而拒绝重复认领。
        """

        folder = self.root / user_id
        folder.mkdir(parents=True, exist_ok=True)
        lock_path = folder / ".board.lock"
        with self._lock:
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _validate_no_dependency_cycle(
        self,
        tasks: dict[str, TaskRecord],
    ) -> None:
        """使用 DFS 检查 A -> B -> A 这类循环依赖。"""

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            # visiting 表示当前递归路径上的任务。
            # 如果再次遇到同一个 task_id，就说明依赖链绕回来了。
            if task_id in visiting:
                raise ValueError(f"检测到任务循环依赖，涉及任务：{task_id}")

            if task_id in visited:
                return

            visiting.add(task_id)
            record = tasks[task_id]

            # blocked_by 的方向是“当前任务依赖 dependency_id”。
            # 所以 DFS 要沿着 blocked_by 往上游依赖继续走。
            for dependency_id in record.blocked_by:
                if dependency_id in tasks:
                    visit(dependency_id)

            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in tasks:
            visit(task_id)

    def _validate_status_transition(
        self,
        *,
        current_status: str,
        requested_status: str,
    ) -> None:
        """限制任务状态机，避免跳过认领或重复完成。"""

        if current_status == requested_status:
            return

        if requested_status == "in_progress":
            raise ValueError("进入 in_progress 必须使用 task_claim_tool。")

        # 这张表就是整个 Task System 允许的状态流转图：
        # pending -> cancelled
        # pending -> in_progress 只能通过 claim_task，不在这里放行
        # in_progress -> completed/pending/cancelled
        # completed 是终态，不能再改
        # cancelled 可以恢复成 pending
        allowed_transitions = {
            "pending": {"cancelled"},
            "in_progress": {"pending", "completed", "cancelled"},
            "completed": set(),
            "cancelled": {"pending"},
        }

        if requested_status not in allowed_transitions.get(current_status, set()):
            raise ValueError(
                f"不允许从 {current_status} 变更为 {requested_status}。"
            )

    def _require_task(
        self,
        tasks: dict[str, TaskRecord],
        task_id: str,
    ) -> TaskRecord:
        """读取任务，不存在时给出清楚错误。"""

        record = tasks.get(task_id)
        if record is None:
            raise ValueError(f"未找到任务：{task_id}")
        return record

    def _normalize_user_id(self, user_id: str) -> str:
        """限制用户目录名，防止路径穿越。"""

        normalized = str(user_id or "guest").strip()
        if not SIMPLE_NAME_PATTERN.fullmatch(normalized):
            raise ValueError("user_id 只能包含字母、数字、下划线和短横线。")
        return normalized

    def _normalize_task_id(self, task_id: str | None) -> str:
        """任务 ID 也是文件名，因此只允许简单字符。"""

        normalized = str(task_id or "").strip().lower()
        if not TASK_ID_PATTERN.fullmatch(normalized):
            raise ValueError(
                "task_id 必须以字母或数字开头，只能包含字母、数字、下划线和短横线。"
            )
        return normalized

    def _normalize_subject(self, subject: str) -> str:
        """校验简短任务标题。"""

        normalized = " ".join(str(subject or "").split())
        if not normalized:
            raise ValueError("subject 不能为空。")
        if len(normalized) > MAX_SUBJECT_CHARS:
            raise ValueError(f"subject 不能超过 {MAX_SUBJECT_CHARS} 字符。")
        return normalized

    def _normalize_description(self, description: str) -> str:
        """校验任务详细说明。"""

        normalized = str(description or "").strip()
        if len(normalized) > MAX_DESCRIPTION_CHARS:
            raise ValueError(
                f"description 不能超过 {MAX_DESCRIPTION_CHARS} 字符。"
            )
        return normalized

    def _normalize_owner(self, owner: str) -> str:
        """owner 表示主 Agent 或未来的 teammate 名称。"""

        normalized = str(owner or "").strip()
        if not SIMPLE_NAME_PATTERN.fullmatch(normalized):
            raise ValueError("owner 只能包含字母、数字、下划线和短横线。")
        return normalized

    def _normalize_dependencies(self, blocked_by: list[Any]) -> list[str]:
        """清洗依赖 ID、去重并限制数量。"""

        if len(blocked_by) > MAX_DEPENDENCIES:
            raise ValueError(f"blocked_by 最多允许 {MAX_DEPENDENCIES} 个任务。")

        normalized: list[str] = []
        for raw_task_id in blocked_by:
            task_id = self._normalize_task_id(str(raw_task_id))
            # 去重但保持原有顺序，便于用户看依赖列表时和输入顺序一致。
            if task_id not in normalized:
                normalized.append(task_id)
        return normalized

    def _normalize_worktree_name(self, value: str) -> str:
        """Worktree 名称只保存安全 slug，不在 Task JSON 中保存绝对路径。"""

        normalized = str(value or "").strip()
        if (
            normalized in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", normalized)
        ):
            raise ValueError(
                "worktree_name 只能包含字母、数字、点、下划线和连字符。"
            )
        return normalized

    def _normalize_status(self, status: str | None) -> str:
        """校验任务状态。"""

        normalized = str(status or "").strip().lower()
        if normalized not in TASK_STATUSES:
            raise ValueError(
                "status 只能是 pending、in_progress、completed、cancelled。"
            )
        return normalized

    def _display_path(self, path: Path) -> str:
        """项目内返回相对路径，测试临时目录则返回绝对路径。"""

        resolved = path.resolve()
        try:
            return resolved.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            return resolved.as_posix()

    def _now(self) -> str:
        """统一生成适合 JSON 保存和排序的时间。"""

        return datetime.now().isoformat(timespec="seconds")


task_store_service = TaskStoreService()

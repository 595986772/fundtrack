"""s18 Worktree Isolation：Task 与真实 Git Worktree 的生命周期服务。

一个 Worktree 是同一 Git 仓库的另一个工作目录。它有自己的分支和文件状态，但复用主
仓库的对象数据库，因此比复制整个项目更轻。当前服务负责：

1. 检查当前项目是否已经是可用 Git 仓库；
2. 为 pending Task 创建并绑定独立 Worktree；
3. 查询 dirty 文件和领先提交数；
4. 完成后保留 Worktree 供 review；
5. 删除前检查未提交或未合并工作，避免静默丢失。

模型从不提供绝对路径，也不能执行任意 shell。所有 Git 子命令由 Harness 使用固定参数
数组调用，用户输入只会进入经过白名单校验的名称或 ref。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import shutil
import subprocess
from threading import RLock
from typing import Any, Iterator
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.database import Database, runtime_database
from app.models import AgentWorktreeEventModel, AgentWorktreeModel
from app.services.tasks import TaskStoreService, task_store_service


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKTREES_ROOT = PROJECT_ROOT / ".worktrees"

WORKTREE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$")
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
BASE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")

ACTIVE_WORKTREE_STATUSES = {"active", "kept", "failed"}
MAX_WORKTREES_PER_USER = 30


def utc_now() -> datetime:
    """统一生成带时区的 UTC 时间。"""

    return datetime.now(timezone.utc)


class GitCommandError(RuntimeError):
    """受控 Git 命令执行失败，保留适合展示的错误摘要。"""


class WorktreeService:
    """管理 Worktree 的 Git 实体、数据库状态和 Task 绑定。"""

    def __init__(
        self,
        database: Database = runtime_database,
        *,
        repository_root: Path = PROJECT_ROOT,
        worktrees_root: Path | None = None,
        tasks: TaskStoreService = task_store_service,
    ) -> None:
        # 初始化时只保存路径并建 SQL 表，不执行 Git 命令。这样项目尚未 git init 时，
        # FastAPI 其他章节仍可正常启动，readiness 接口会说明缺少什么。
        self.database = database
        self.repository_root = Path(repository_root).resolve()
        self.worktrees_root = Path(
            worktrees_root or (self.repository_root / ".worktrees")
        ).resolve()
        self.tasks = tasks
        self._thread_lock = RLock()
        self.database.create_schema()

    def readiness(self) -> dict[str, Any]:
        """检查 s18 是否具备真实创建 Worktree 的前置条件。"""

        git_path = shutil.which("git")
        base = {
            "ready": False,
            "git_available": bool(git_path),
            "repository_root": self.repository_root.as_posix(),
            "worktrees_root": self.worktrees_root.as_posix(),
            "head_commit": None,
            "current_branch": None,
            "reason": "",
        }
        if git_path is None:
            return {**base, "reason": "本机未找到 git 命令。"}
        if not self.repository_root.is_dir():
            return {**base, "reason": "项目目录不存在。"}

        try:
            detected_root = Path(
                self._run_git(
                    ["rev-parse", "--show-toplevel"],
                    cwd=self.repository_root,
                )
            ).resolve()
        except GitCommandError:
            return {
                **base,
                "reason": (
                    "当前项目还不是 Git 仓库。请先由你确认并执行 git init、"
                    "创建首个 commit；服务不会自动提交本地文件。"
                ),
            }

        # 如果父目录是一个大仓库，也不能把它误当作当前项目的安全边界。
        if detected_root != self.repository_root:
            return {
                **base,
                "reason": (
                    "检测到 Git 根目录不是当前 FastAPI 项目目录："
                    f"{detected_root.as_posix()}"
                ),
            }

        try:
            head_commit = self._run_git(
                ["rev-parse", "--verify", "HEAD"],
                cwd=self.repository_root,
            )
            current_branch = self._run_git(
                ["branch", "--show-current"],
                cwd=self.repository_root,
            )
        except GitCommandError as exc:
            return {
                **base,
                "reason": f"仓库还没有可用的首个 commit：{exc}",
            }

        return {
            **base,
            "ready": True,
            "head_commit": head_commit,
            "current_branch": current_branch or "(detached HEAD)",
            "reason": "Git 仓库和 HEAD 已就绪，可以创建隔离 Worktree。",
        }

    def create_worktree(
        self,
        *,
        user_id: str,
        name: str,
        task_id: str | None = None,
        base_ref: str = "HEAD",
    ) -> dict[str, Any]:
        """创建真实 Git Worktree，并可原子地绑定一条 pending Task。"""

        user = self._normalize_user_id(user_id)
        worktree_name = self._normalize_name(name)
        normalized_task_id = self._normalize_task_id(task_id) if task_id else None
        normalized_base_ref = self._normalize_base_ref(base_ref)

        ready = self.readiness()
        if not ready["ready"]:
            raise RuntimeError(str(ready["reason"]))

        # 路径与分支都由后端推导。即使模型尝试传入 ../../，名称校验也会在这里拒绝。
        path = self._derived_path(user_id=user, name=worktree_name)
        branch_name = f"agent/{user}/{worktree_name}"

        with self._lifecycle_lock():
            self._assert_create_available(
                user_id=user,
                name=worktree_name,
                task_id=normalized_task_id,
                path=path,
                branch_name=branch_name,
            )
            if normalized_task_id:
                self._assert_task_can_bind(
                    user_id=user,
                    task_id=normalized_task_id,
                )

            # 先把 base ref 固定成 commit SHA。后续 ahead 统计不受主分支移动影响。
            base_commit = self._run_git(
                ["rev-parse", "--verify", f"{normalized_base_ref}^{{commit}}"],
                cwd=self.repository_root,
            )

            try:
                self._run_git(
                    [
                        "worktree",
                        "add",
                        "-b",
                        branch_name,
                        path.as_posix(),
                        base_commit,
                    ],
                    cwd=self.repository_root,
                )
            except Exception:
                # Git 可能创建了一部分管理元数据；prune 能清理失效登记。
                self._prune_worktrees()
                raise

            task_bound = False
            try:
                if normalized_task_id:
                    self.tasks.bind_worktree(
                        user_id=user,
                        task_id=normalized_task_id,
                        worktree_name=worktree_name,
                    )
                    task_bound = True

                now = utc_now()
                model = AgentWorktreeModel(
                    user_id=user,
                    name=worktree_name,
                    task_id=normalized_task_id,
                    branch_name=branch_name,
                    path=path.as_posix(),
                    repository_root=self.repository_root.as_posix(),
                    base_ref=normalized_base_ref,
                    base_commit=base_commit,
                    status="active",
                    dirty_file_count=0,
                    ahead_commit_count=0,
                    last_error=None,
                    created_at=now,
                    updated_at=now,
                    removed_at=None,
                )
                with self.database.session() as session, session.begin():
                    session.add(model)
                    self._add_event(
                        session,
                        model=model,
                        event_type="created",
                        detail=(
                            f"从 {normalized_base_ref} ({base_commit[:12]}) "
                            f"创建分支 {branch_name}。"
                        ),
                    )
            except Exception as exc:
                # 数据库或 Task 绑定任一步失败，都回收刚创建的 Git 实体。
                if task_bound and normalized_task_id:
                    self.tasks.unbind_worktree(
                        user_id=user,
                        task_id=normalized_task_id,
                        worktree_name=worktree_name,
                    )
                self._remove_git_entities(
                    path=path,
                    branch_name=branch_name,
                    force=True,
                )
                if isinstance(exc, IntegrityError):
                    raise ValueError("Worktree 名称或 Task 绑定发生并发冲突。") from exc
                raise

        created = self.get_worktree(user_id=user, name=worktree_name)
        return {
            "ok": True,
            **created,
            "message": (
                "真实 Git Worktree 已创建；Task 仍保持 pending，等待自治 teammate 认领。"
                if normalized_task_id
                else "真实 Git Worktree 已创建。"
            ),
        }

    def list_worktrees(
        self,
        *,
        user_id: str,
        status: str | None = None,
        max_results: int = 30,
    ) -> dict[str, Any]:
        """列出当前用户的 Worktree，并刷新仍存在目录的 Git 状态。"""

        user = self._normalize_user_id(user_id)
        normalized_status = self._normalize_status(status) if status else None
        limit = max(1, min(int(max_results), 100))

        with self.database.session() as session:
            statement = select(AgentWorktreeModel).where(
                AgentWorktreeModel.user_id == user
            )
            if normalized_status:
                statement = statement.where(
                    AgentWorktreeModel.status == normalized_status
                )
            models = list(
                session.scalars(
                    statement.order_by(AgentWorktreeModel.updated_at.desc()).limit(limit)
                )
            )

        worktrees = [
            self._refresh_and_read(user_id=user, name=model.name)
            for model in models
        ]
        return {
            "ok": True,
            "user_id": user,
            "count": len(worktrees),
            "worktrees": worktrees,
            "readiness": self.readiness(),
        }

    def get_worktree(self, *, user_id: str, name: str) -> dict[str, Any]:
        """读取单个 Worktree、近期生命周期事件和最新 Git 状态。"""

        user = self._normalize_user_id(user_id)
        worktree_name = self._normalize_name(name)
        worktree = self._refresh_and_read(user_id=user, name=worktree_name)
        return {
            "worktree": worktree,
            "events": self._list_events(user_id=user, name=worktree_name),
        }

    def keep_worktree(self, *, user_id: str, name: str) -> dict[str, Any]:
        """保留 Worktree 供人工 review，不删除文件或分支。"""

        user = self._normalize_user_id(user_id)
        worktree_name = self._normalize_name(name)

        with self._lifecycle_lock():
            current = self._require_model(user_id=user, name=worktree_name)
            if current.status == "removed":
                raise ValueError("Worktree 已删除，不能再保留。")
            if current.status == "kept":
                return {
                    "ok": True,
                    **self.get_worktree(user_id=user, name=worktree_name),
                    "message": "Worktree 已经处于 kept 状态。",
                }

            self._refresh_and_read(user_id=user, name=worktree_name)
            with self.database.session() as session, session.begin():
                model = self._require_model_in_session(
                    session,
                    user_id=user,
                    name=worktree_name,
                )
                model.status = "kept"
                model.updated_at = utc_now()
                self._add_event(
                    session,
                    model=model,
                    event_type="kept",
                    detail="任务执行结束，保留 Worktree 等待人工 review。",
                )

        return {
            "ok": True,
            **self.get_worktree(user_id=user, name=worktree_name),
            "message": "Worktree 已保留，文件和分支都没有删除。",
        }

    def remove_worktree(
        self,
        *,
        user_id: str,
        name: str,
        discard_changes: bool = False,
    ) -> dict[str, Any]:
        """删除 Worktree；有未提交或领先提交时默认拒绝。"""

        user = self._normalize_user_id(user_id)
        worktree_name = self._normalize_name(name)

        with self._lifecycle_lock():
            current = self._require_model(user_id=user, name=worktree_name)
            if current.status == "removed":
                return {
                    "ok": True,
                    **self.get_worktree(user_id=user, name=worktree_name),
                    "message": "Worktree 已经删除。",
                }

            refreshed = self._refresh_and_read(user_id=user, name=worktree_name)
            dirty_count = int(refreshed["dirty_file_count"])
            ahead_count = int(refreshed["ahead_commit_count"])
            if (dirty_count or ahead_count) and not discard_changes:
                raise ValueError(
                    "Worktree 仍有未提交文件或独立提交。"
                    f"dirty={dirty_count}, ahead={ahead_count}；"
                    "确认不再需要后才能显式设置 discard_changes=true。"
                )

            self._remove_git_entities(
                path=Path(refreshed["path"]),
                branch_name=str(refreshed["branch_name"]),
                force=bool(discard_changes),
            )

            with self.database.session() as session, session.begin():
                model = self._require_model_in_session(
                    session,
                    user_id=user,
                    name=worktree_name,
                )
                model.status = "removed"
                model.last_error = None
                model.updated_at = utc_now()
                model.removed_at = model.updated_at
                self._add_event(
                    session,
                    model=model,
                    event_type="removed",
                    detail=(
                        "显式丢弃未提交/未合并工作后删除。"
                        if discard_changes
                        else "确认无未提交或独立提交后安全删除。"
                    ),
                )

        return {
            "ok": True,
            **self.get_worktree(user_id=user, name=worktree_name),
            "message": "Worktree 目录和临时分支已删除，数据库保留审计记录。",
        }

    def resolve_task_workspace(
        self,
        *,
        user_id: str,
        task: dict[str, Any],
    ) -> dict[str, Any] | None:
        """把 Task 的安全名称解析成后端可信路径，供自治执行链注入。"""

        worktree_name = str(task.get("worktree_name") or "").strip()
        if not worktree_name:
            return None

        user = self._normalize_user_id(user_id)
        task_id = self._normalize_task_id(str(task.get("id") or ""))
        name = self._normalize_name(worktree_name)
        worktree = self._refresh_and_read(user_id=user, name=name)

        if worktree["task_id"] != task_id:
            raise RuntimeError(
                f"Task {task_id} 与 Worktree {name} 的数据库绑定不一致。"
            )
        if worktree["status"] != "active":
            raise RuntimeError(
                f"Task {task_id} 的 Worktree 当前状态为 {worktree['status']}，"
                "不能继续执行。"
            )
        path = Path(str(worktree["path"])).resolve()
        if not path.is_dir():
            raise RuntimeError(f"Task {task_id} 的 Worktree 目录已不存在。")

        return {
            "name": name,
            "path": path,
            "branch_name": worktree["branch_name"],
        }

    def recent_index(
        self,
        *,
        user_id: str,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """给 Agent 响应返回轻量 Worktree 快照。"""

        return self.list_worktrees(
            user_id=user_id,
            max_results=max_results,
        )["worktrees"]

    def index_for_prompt(self, *, user_id: str, max_results: int = 8) -> str:
        """构造短索引，不把完整事件和 Git 输出长期塞进 system prompt。"""

        readiness = self.readiness()
        if not readiness["ready"]:
            return f"Worktree 尚未就绪：{readiness['reason']}"

        worktrees = self.recent_index(user_id=user_id, max_results=max_results)
        if not worktrees:
            return "当前用户还没有 Worktree。"

        lines = ["当前 Worktree："]
        for item in worktrees:
            lines.append(
                "- "
                f"{item['name']} | task={item['task_id'] or '-'} | "
                f"branch={item['branch_name']} | status={item['status']} | "
                f"dirty={item['dirty_file_count']} | ahead={item['ahead_commit_count']}"
            )
        return "\n".join(lines)

    def _assert_create_available(
        self,
        *,
        user_id: str,
        name: str,
        task_id: str | None,
        path: Path,
        branch_name: str,
    ) -> None:
        """在执行 Git 之前检查数据库、路径、分支和用户配额。"""

        with self.database.session() as session:
            existing_name = session.get(
                AgentWorktreeModel,
                {"user_id": user_id, "name": name},
            )
            if existing_name is not None:
                raise ValueError(
                    f"Worktree 名称已存在（包括已删除的审计记录）：{name}"
                )
            if task_id:
                existing_task = session.scalar(
                    select(AgentWorktreeModel).where(
                        AgentWorktreeModel.user_id == user_id,
                        AgentWorktreeModel.task_id == task_id,
                    )
                )
                if existing_task is not None:
                    raise ValueError(
                        f"Task 已绑定 Worktree：{existing_task.name}"
                    )
            count = int(
                session.scalar(
                    select(func.count())
                    .select_from(AgentWorktreeModel)
                    .where(
                        AgentWorktreeModel.user_id == user_id,
                        AgentWorktreeModel.status != "removed",
                    )
                )
                or 0
            )
            if count >= MAX_WORKTREES_PER_USER:
                raise ValueError(
                    f"每个用户最多保留 {MAX_WORKTREES_PER_USER} 个未删除 Worktree。"
                )

        if path.exists():
            raise ValueError(f"Worktree 目标目录已经存在：{path}")
        branch_exists = subprocess.run(
            [
                "git",
                "-C",
                self.repository_root.as_posix(),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch_name}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=self._git_environment(),
        )
        if branch_exists.returncode == 0:
            raise ValueError(f"Git 分支已经存在：{branch_name}")

    def _assert_task_can_bind(self, *, user_id: str, task_id: str) -> None:
        """Task 必须存在、未认领、未绑定，创建 Worktree 才有意义。"""

        task = self.tasks.get_task(user_id=user_id, task_id=task_id)["task"]
        if task["status"] != "pending" or task["owner"] is not None:
            raise ValueError("只有尚未认领的 pending Task 可以创建 Worktree。")
        if task.get("worktree_name"):
            raise ValueError(
                f"Task 已绑定 Worktree：{task['worktree_name']}"
            )

    def _refresh_and_read(self, *, user_id: str, name: str) -> dict[str, Any]:
        """刷新 dirty/ahead 后返回公开字典；失败会写入 last_error。"""

        model = self._require_model(user_id=user_id, name=name)
        if model.status == "removed":
            return self._model_to_dict(model)

        path = Path(model.path).resolve()
        try:
            if not path.is_dir():
                raise GitCommandError("Worktree 目录不存在。")
            porcelain = self._run_git(
                ["status", "--porcelain"],
                cwd=path,
            )
            dirty_count = len(
                [line for line in porcelain.splitlines() if line.strip()]
            )
            ahead_output = self._run_git(
                ["rev-list", "--count", f"{model.base_commit}..HEAD"],
                cwd=path,
            )
            ahead_count = int(ahead_output or 0)
            status = model.status if model.status != "failed" else "active"
            last_error = None
        except Exception as exc:
            dirty_count = model.dirty_file_count
            ahead_count = model.ahead_commit_count
            status = "failed"
            last_error = str(exc)

        with self.database.session() as session, session.begin():
            current = self._require_model_in_session(
                session,
                user_id=user_id,
                name=name,
            )
            current.dirty_file_count = dirty_count
            current.ahead_commit_count = ahead_count
            current.status = status
            current.last_error = last_error
            current.updated_at = utc_now()

        return self._model_to_dict(self._require_model(user_id=user_id, name=name))

    def _remove_git_entities(
        self,
        *,
        path: Path,
        branch_name: str,
        force: bool,
    ) -> None:
        """删除 Git worktree 登记和临时分支，不通过 shell 拼接命令。"""

        if path.exists():
            arguments = ["worktree", "remove"]
            if force:
                arguments.append("--force")
            arguments.append(path.as_posix())
            self._run_git(arguments, cwd=self.repository_root)
        else:
            self._prune_worktrees()

        branch_arguments = ["branch", "-D" if force else "-d", branch_name]
        try:
            self._run_git(branch_arguments, cwd=self.repository_root)
        except GitCommandError:
            # force 清理时，分支可能已被人工删除；确认不存在即可视为完成。
            verification = subprocess.run(
                [
                    "git",
                    "-C",
                    self.repository_root.as_posix(),
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch_name}",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env=self._git_environment(),
            )
            if verification.returncode == 0:
                raise

    def _prune_worktrees(self) -> None:
        """清理 Git 中已经失效的 Worktree 元数据。"""

        try:
            self._run_git(
                ["worktree", "prune"],
                cwd=self.repository_root,
            )
        except GitCommandError:
            # readiness 失败或 cleanup 阶段不覆盖原始异常。
            pass

    def _run_git(self, arguments: list[str], *, cwd: Path) -> str:
        """执行一个固定 Git 子命令，并返回去除首尾空白的 stdout。"""

        try:
            completed = subprocess.run(
                ["git", "-C", Path(cwd).resolve().as_posix(), *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env=self._git_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitCommandError(f"Git 命令无法完成：{exc}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown error").strip()
            # 错误只返回一小段，避免把命令环境或超长输出带进模型上下文。
            raise GitCommandError(detail[:1000])
        return completed.stdout.strip()

    def _git_environment(self) -> dict[str, str]:
        """构造非交互 Git 环境，并移除可能劫持仓库位置的变量。"""

        environment = dict(os.environ)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment.pop("GIT_DIR", None)
        environment.pop("GIT_WORK_TREE", None)
        return environment

    @contextmanager
    def _lifecycle_lock(self) -> Iterator[None]:
        """用线程锁 + 文件锁串行化跨 API/Worker 的 Git 生命周期操作。"""

        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.worktrees_root / ".manager.lock"
        with self._thread_lock:
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _derived_path(self, *, user_id: str, name: str) -> Path:
        """由安全 slug 推导路径，并再次验证没有逃出 .worktrees。"""

        path = (self.worktrees_root / user_id / name).resolve()
        try:
            path.relative_to(self.worktrees_root)
        except ValueError as exc:
            raise ValueError("Worktree 路径逃出了受控目录。") from exc
        return path

    def _require_model(self, *, user_id: str, name: str) -> AgentWorktreeModel:
        with self.database.session() as session:
            return self._require_model_in_session(
                session,
                user_id=user_id,
                name=name,
            )

    def _require_model_in_session(
        self,
        session: Any,
        *,
        user_id: str,
        name: str,
    ) -> AgentWorktreeModel:
        model = session.get(
            AgentWorktreeModel,
            {"user_id": user_id, "name": name},
        )
        if model is None:
            raise KeyError(f"未找到 Worktree：{name}")
        return model

    def _add_event(
        self,
        session: Any,
        *,
        model: AgentWorktreeModel,
        event_type: str,
        detail: str,
    ) -> None:
        """在同一数据库事务里追加一条不可变生命周期事件。"""

        session.add(
            AgentWorktreeEventModel(
                user_id=model.user_id,
                id=f"wte-{uuid4().hex[:12]}",
                worktree_name=model.name,
                task_id=model.task_id,
                event_type=event_type,
                detail=detail,
                created_at=utc_now(),
            )
        )

    def _list_events(
        self,
        *,
        user_id: str,
        name: str,
        max_results: int = 30,
    ) -> list[dict[str, Any]]:
        with self.database.session() as session:
            events = list(
                session.scalars(
                    select(AgentWorktreeEventModel)
                    .where(
                        AgentWorktreeEventModel.user_id == user_id,
                        AgentWorktreeEventModel.worktree_name == name,
                    )
                    .order_by(AgentWorktreeEventModel.created_at.desc())
                    .limit(max(1, min(int(max_results), 100)))
                )
            )
        return [
            {
                "id": event.id,
                "user_id": event.user_id,
                "worktree_name": event.worktree_name,
                "task_id": event.task_id,
                "event_type": event.event_type,
                "detail": event.detail,
                "created_at": self._iso(event.created_at),
            }
            for event in events
        ]

    def _model_to_dict(self, model: AgentWorktreeModel) -> dict[str, Any]:
        return {
            "user_id": model.user_id,
            "name": model.name,
            "task_id": model.task_id,
            "branch_name": model.branch_name,
            "path": model.path,
            "repository_root": model.repository_root,
            "base_ref": model.base_ref,
            "base_commit": model.base_commit,
            "status": model.status,
            "dirty_file_count": model.dirty_file_count,
            "ahead_commit_count": model.ahead_commit_count,
            "last_error": model.last_error,
            "created_at": self._iso(model.created_at),
            "updated_at": self._iso(model.updated_at),
            "removed_at": self._iso(model.removed_at),
        }

    def _normalize_user_id(self, value: str) -> str:
        normalized = str(value or "").strip()
        if not USER_ID_PATTERN.fullmatch(normalized):
            raise ValueError("user_id 只能包含字母、数字、下划线和连字符。")
        return normalized

    def _normalize_name(self, value: str) -> str:
        normalized = str(value or "").strip()
        if normalized in {".", ".."} or not WORKTREE_NAME_PATTERN.fullmatch(normalized):
            raise ValueError(
                "Worktree 名称必须以字母或数字开头，只能包含字母、数字、点、"
                "下划线和连字符，最长 64 位。"
            )
        return normalized

    def _normalize_task_id(self, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not TASK_ID_PATTERN.fullmatch(normalized):
            raise ValueError("task_id 格式非法。")
        return normalized

    def _normalize_base_ref(self, value: str) -> str:
        normalized = str(value or "HEAD").strip()
        if (
            not BASE_REF_PATTERN.fullmatch(normalized)
            or ".." in normalized
            or "//" in normalized
        ):
            raise ValueError("base_ref 格式非法。")
        return normalized

    def _normalize_status(self, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in {"active", "kept", "removed", "failed"}:
            raise ValueError("status 只能是 active、kept、removed 或 failed。")
        return normalized

    def _iso(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat(timespec="microseconds")


# REST、主 Agent tools、Prompt Builder 和自治 Worker 共用同一个状态服务。
worktree_service = WorktreeService()

"""当前 AgentRun 的工作目录上下文。

s18 的关键不是给文件工具增加一个由模型填写的 ``root`` 参数。那样模型仍然可以尝试
指定任意目录。这里使用 ``ContextVar``，由后端在执行 teammate 工具前注入工作目录：

- 普通主 Agent / 临时 Subagent 默认读取主项目；
- 绑定 Worktree 的自治 Task 读取和写入自己的隔离目录；
- 模型只能看到相对路径，不能改变根目录。

ContextVar 会跟随当前 asyncio Task 传播，因此多个并发 teammate 可以各自使用不同
Worktree，而不会像修改一个全局变量那样互相串目录。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 默认值保持 s18 之前的行为：文件工具仍然以当前 FastAPI 项目作为根目录。
_workspace_root: ContextVar[Path] = ContextVar(
    "agent_workspace_root",
    default=PROJECT_ROOT,
)
_isolated_workspace: ContextVar[bool] = ContextVar(
    "agent_isolated_workspace",
    default=False,
)
_worktree_name: ContextVar[str | None] = ContextVar(
    "agent_worktree_name",
    default=None,
)


def current_workspace_root() -> Path:
    """返回当前 AgentRun 被 Harness 分配的可信工作目录。"""

    return _workspace_root.get().resolve()


def is_isolated_workspace() -> bool:
    """当前工具调用是否运行在受控 Git Worktree 中。"""

    return _isolated_workspace.get()


def current_worktree_name() -> str | None:
    """返回当前 Worktree 的安全名称，普通项目上下文返回 ``None``。"""

    return _worktree_name.get()


@contextmanager
def workspace_scope(
    root: Path,
    *,
    isolated: bool,
    worktree_name: str | None = None,
) -> Iterator[None]:
    """在一个受控代码块内临时切换文件工具的根目录。

    ``root`` 只由 WorktreeService 查询后传入，不来自 LLM tool arguments。退出代码块时
    token 会恢复旧值，即使工具抛出异常也不会污染后续 AgentRun。
    """

    resolved_root = Path(root).resolve()
    if not resolved_root.is_dir():
        raise ValueError(f"Agent 工作目录不存在：{resolved_root}")
    if isolated and not str(worktree_name or "").strip():
        raise ValueError("隔离工作目录必须关联 worktree_name。")

    root_token = _workspace_root.set(resolved_root)
    isolated_token = _isolated_workspace.set(bool(isolated))
    name_token = _worktree_name.set(
        str(worktree_name).strip() if worktree_name else None
    )
    try:
        yield
    finally:
        _worktree_name.reset(name_token)
        _isolated_workspace.reset(isolated_token)
        _workspace_root.reset(root_token)

"""s18 Worktree Isolation 的真实 Git 生命周期测试。

运行：
    conda run -n dl python -m unittest tests.test_worktrees -v

每个测试都在临时目录里执行真正的 ``git init / worktree add / worktree remove``。
不会修改当前 FastApiDemo，也不会调用真实大模型、Redis 或 Docker。
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from app.database import Database
from app.services.agent_teams import AgentTeamService
from app.services.autonomous_agents import AutonomousAgentService
from app.services.tasks import TaskStoreService
from app.services.team_protocols import TeamProtocolService
from app.services.tool_permissions import check_tool_permission
from tests.agent_test_context import developer_context
from app.services.workspace_context import workspace_scope
from app.services.worktrees import WorktreeService


class WorktreeTests(unittest.TestCase):
    """验证 Task -> Worktree -> Teammate 的完整确定性控制链。"""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()

        # 临时仓库必须有首个 commit，Git 才能从 HEAD 创建独立分支。
        self._git("init", cwd=self.repository)
        self._git("config", "user.name", "FastAPI Agent Test", cwd=self.repository)
        self._git(
            "config",
            "user.email",
            "agent-test@example.com",
            cwd=self.repository,
        )
        (self.repository / ".gitignore").write_text(
            ".worktrees/\n",
            encoding="utf-8",
        )
        (self.repository / "seed.txt").write_text(
            "main workspace\n",
            encoding="utf-8",
        )
        self._git("add", ".gitignore", "seed.txt", cwd=self.repository)
        self._git("commit", "-m", "initial", cwd=self.repository)

        self.database = Database(f"sqlite:///{self.root / 'worktrees.db'}")
        self.tasks = TaskStoreService(root=self.root / "tasks")
        self.service = WorktreeService(
            self.database,
            repository_root=self.repository,
            worktrees_root=self.repository / ".worktrees",
            tasks=self.tasks,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _git(
        self,
        *arguments: str,
        cwd: Path,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """在临时仓库运行 Git；测试失败时保留 stderr 作为断言上下文。"""

        return subprocess.run(
            ["git", "-C", cwd.as_posix(), *arguments],
            check=check,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def _create_bound_worktree(
        self,
        *,
        task_id: str = "task-one",
        name: str = "wt-task-one",
    ) -> dict:
        """准备一条 pending Task，并为它创建真实 Worktree。"""

        self.tasks.create_task(
            user_id="chen",
            task_id=task_id,
            subject="在隔离目录修改代码",
        )
        return self.service.create_worktree(
            user_id="chen",
            name=name,
            task_id=task_id,
        )

    def test_readiness_reports_non_git_directory_without_crashing(self) -> None:
        """项目尚未 git init 时只返回不可用原因，FastAPI 其他能力仍可启动。"""

        plain_directory = self.root / "plain"
        plain_directory.mkdir()
        service = WorktreeService(
            self.database,
            repository_root=plain_directory,
            worktrees_root=plain_directory / ".worktrees",
            tasks=self.tasks,
        )

        readiness = service.readiness()

        self.assertFalse(readiness["ready"])
        self.assertTrue(readiness["git_available"])
        self.assertIn("不是 Git 仓库", readiness["reason"])

    def test_create_worktree_binds_task_without_claiming_it(self) -> None:
        """绑定只准备工作区；Task 仍应由 s17 自治 Harness 原子认领。"""

        result = self._create_bound_worktree()
        task = self.tasks.get_task(user_id="chen", task_id="task-one")["task"]
        worktree = result["worktree"]

        self.assertTrue(Path(worktree["path"]).is_dir())
        self.assertEqual(worktree["status"], "active")
        self.assertEqual(worktree["task_id"], "task-one")
        self.assertEqual(task["status"], "pending")
        self.assertIsNone(task["owner"])
        self.assertEqual(task["worktree_name"], "wt-task-one")
        self.assertEqual(result["events"][0]["event_type"], "created")

    def test_invalid_name_and_duplicate_task_binding_are_rejected(self) -> None:
        """路径穿越名称和一条 Task 绑定两个 Worktree 都必须在后端被拒绝。"""

        self.tasks.create_task(
            user_id="chen",
            task_id="safe-task",
            subject="安全测试",
        )
        with self.assertRaises(ValueError):
            self.service.create_worktree(
                user_id="chen",
                name="../outside",
                task_id="safe-task",
            )

        self.service.create_worktree(
            user_id="chen",
            name="first-worktree",
            task_id="safe-task",
        )
        with self.assertRaises(ValueError):
            self.service.create_worktree(
                user_id="chen",
                name="second-worktree",
                task_id="safe-task",
            )

    def test_file_tools_read_and_write_only_inside_injected_worktree(self) -> None:
        """同样的相对路径工具在 ContextVar 内应指向 Worktree，而不是主目录。"""

        created = self._create_bound_worktree()
        worktree_path = Path(created["worktree"]["path"])

        # 延迟导入避免测试模块加载时把 LangChain 工具当成测试依赖说明的重点。
        from app.services.agent_tools import (
            read_file_tool,
            workspace_write_file_tool,
        )

        with workspace_scope(
            worktree_path,
            isolated=True,
            worktree_name="wt-task-one",
        ):
            write_result = json.loads(
                workspace_write_file_tool.invoke(
                    {
                        "path": "result.md",
                        "content": "# isolated result\n",
                        "overwrite": False,
                    }
                )
            )
            read_result = json.loads(
                read_file_tool.invoke(
                    {
                        "path": "result.md",
                        "start_line": 1,
                        "max_lines": 20,
                    }
                )
            )

        denied_outside_context = json.loads(
            workspace_write_file_tool.invoke(
                {
                    "path": "must-not-exist.md",
                    "content": "blocked",
                    "overwrite": False,
                }
            )
        )

        self.assertTrue(write_result["ok"])
        self.assertIn("isolated result", read_result["content"])
        self.assertFalse((self.repository / "result.md").exists())
        self.assertFalse(denied_outside_context["ok"])
        self.assertFalse((self.repository / "must-not-exist.md").exists())

    def test_remove_protects_dirty_and_ahead_work_until_explicit_discard(self) -> None:
        """未提交文件或独立 commit 都不能被默认清理。"""

        created = self._create_bound_worktree()
        worktree_path = Path(created["worktree"]["path"])
        (worktree_path / "implementation.py").write_text(
            "VALUE = 18\n",
            encoding="utf-8",
        )
        self._git("add", "implementation.py", cwd=worktree_path)
        self._git("commit", "-m", "isolated implementation", cwd=worktree_path)
        (worktree_path / "draft.md").write_text("not committed\n", encoding="utf-8")

        refreshed = self.service.get_worktree(
            user_id="chen",
            name="wt-task-one",
        )["worktree"]
        self.assertEqual(refreshed["ahead_commit_count"], 1)
        self.assertEqual(refreshed["dirty_file_count"], 1)

        with self.assertRaises(ValueError):
            self.service.remove_worktree(
                user_id="chen",
                name="wt-task-one",
            )

        kept = self.service.keep_worktree(
            user_id="chen",
            name="wt-task-one",
        )
        self.assertEqual(kept["worktree"]["status"], "kept")
        removed = self.service.remove_worktree(
            user_id="chen",
            name="wt-task-one",
            discard_changes=True,
        )

        self.assertEqual(removed["worktree"]["status"], "removed")
        self.assertFalse(worktree_path.exists())
        branch_check = self._git(
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/agent/chen/wt-task-one",
            cwd=self.repository,
            check=False,
        )
        self.assertNotEqual(branch_check.returncode, 0)

    def test_autonomy_injects_bound_path_and_keeps_completed_worktree(self) -> None:
        """s17 领取 Task 后，s18 路径应传入 teammate，而完成后保留供 review。"""

        teams = AgentTeamService(self.database)
        protocols = TeamProtocolService(self.database)
        autonomy = AutonomousAgentService(
            self.database,
            tasks=self.tasks,
            teams=teams,
            protocols=protocols,
            worktrees=self.service,
        )
        teams.spawn_teammate(
            user_id="chen",
            name="coder",
            role="隔离代码实现者",
            capabilities=[
                "read_project",
                "search_project",
                "write_workspace",
            ],
        )
        created = self._create_bound_worktree()
        worktree_path = created["worktree"]["path"]

        with patch.object(
            autonomy,
            "_dispatch_job",
            return_value={"job": {"id": "fake-job", "status": "queued"}},
        ):
            started = autonomy.start_session(
                user_id="chen",
                teammate_name="coder",
                max_tasks=1,
                max_idle_polls=1,
                poll_interval_seconds=0.05,
            )

        fake_execute = AsyncMock(return_value={"summary": "隔离修改已完成"})
        with patch.object(teams, "execute_autonomous_task", fake_execute):
            result = autonomy.run_session(
                user_id="chen",
                session_id=started["session"]["id"],
                wait=lambda _seconds: None,
            )

        call = fake_execute.await_args.kwargs
        task = self.tasks.get_task(user_id="chen", task_id="task-one")["task"]
        worktree = self.service.get_worktree(
            user_id="chen",
            name="wt-task-one",
        )["worktree"]

        self.assertEqual(call["workspace_root"], worktree_path)
        self.assertEqual(call["worktree_name"], "wt-task-one")
        self.assertEqual(task["status"], "completed")
        self.assertEqual(worktree["status"], "kept")
        self.assertEqual(result["task_results"][0]["worktree"]["name"], "wt-task-one")

    def test_permissions_default_deny_unsafe_worktree_operations(self) -> None:
        """主 Agent 生命周期工具和 teammate 文件写入都经过独立权限规则。"""

        context = developer_context()
        allowed = check_tool_permission(
            "create_task_worktree_tool",
            {
                "user_id": "chen",
                "name": "wt-safe",
                "task_id": "task-one",
                "base_ref": "HEAD",
            },
            execution_context=context,
        )
        traversal = check_tool_permission(
            "workspace_write_file_tool",
            {"path": "../main.py", "content": "unsafe"},
            execution_context=context,
        )
        absolute = check_tool_permission(
            "workspace_write_file_tool",
            {"path": "/tmp/unsafe.py", "content": "unsafe"},
            execution_context=context,
        )

        self.assertTrue(allowed.allowed)
        self.assertFalse(traversal.allowed)
        self.assertFalse(absolute.allowed)


if __name__ == "__main__":
    unittest.main()

"""s17 Autonomous Agents 的任务发现、生命周期和协议优先级测试。

运行：
    conda run -n dl python -m unittest tests.test_autonomous_agents -v

测试全部使用临时 SQLite 与临时 Task Board，不调用真实模型、Redis 或 Docker。
"""

from __future__ import annotations

from pathlib import Path
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


class AutonomousAgentTests(unittest.TestCase):
    """用确定性假模型观察 IDLE -> claim -> WORK -> IDLE。"""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.database = Database(f"sqlite:///{root / 'autonomy.db'}")
        self.tasks = TaskStoreService(root=root / "tasks")
        self.teams = AgentTeamService(self.database)
        self.protocols = TeamProtocolService(self.database)
        self.autonomy = AutonomousAgentService(
            self.database,
            tasks=self.tasks,
            teams=self.teams,
            protocols=self.protocols,
        )
        self.teams.spawn_teammate(
            user_id="chen",
            name="researcher",
            role="代码研究员",
            instructions="读取源码后回答。",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _start(self, *, max_tasks: int = 1, max_idle_polls: int = 1) -> dict:
        """跳过真实 Queue，只创建可由测试直接执行的持久化会话。"""

        with patch.object(
            self.autonomy,
            "_dispatch_job",
            return_value={"job": {"id": "fake-autonomy-job", "status": "queued"}},
        ):
            return self.autonomy.start_session(
                user_id="chen",
                teammate_name="researcher",
                poll_interval_seconds=0.05,
                max_idle_polls=max_idle_polls,
                max_tasks=max_tasks,
                max_rounds_per_task=2,
                timeout_seconds=30,
            )

    def test_claimable_scan_and_owner_checked_completion(self) -> None:
        """阻塞任务不能领取；领取后只有相同 teammate 才能完成。"""

        self.tasks.create_task(user_id="chen", task_id="first", subject="先完成")
        self.tasks.create_task(
            user_id="chen",
            task_id="second",
            subject="后完成",
            blocked_by=["first"],
        )

        claimable = self.tasks.list_claimable_tasks(user_id="chen")
        self.assertEqual([item["id"] for item in claimable], ["first"])
        claimed = self.autonomy.claim_next_task(
            user_id="chen", teammate_name="researcher"
        )
        self.assertEqual(claimed["task"]["owner"], "researcher")
        with self.assertRaises(ValueError):
            self.tasks.complete_task(user_id="chen", task_id="first", owner="writer")

    def test_autonomy_completes_dependency_chain(self) -> None:
        """完成上游后，下游会在下一轮扫描中自动解锁并被同一成员认领。"""

        self.tasks.create_task(user_id="chen", task_id="first", subject="读取入口")
        self.tasks.create_task(
            user_id="chen",
            task_id="second",
            subject="总结调用链",
            blocked_by=["first"],
        )
        started = self._start(max_tasks=2)
        fake_execute = AsyncMock(
            side_effect=[{"summary": "入口已读取"}, {"summary": "调用链已总结"}]
        )

        with patch.object(self.teams, "execute_autonomous_task", fake_execute):
            result = self.autonomy.run_session(
                user_id="chen",
                session_id=started["session"]["id"],
                wait=lambda _seconds: None,
            )

        tasks = self.tasks.list_tasks(user_id="chen")["tasks"]
        self.assertEqual([item["status"] for item in tasks], ["completed", "completed"])
        self.assertEqual(result["session"]["stop_reason"], "max_tasks")
        self.assertEqual(fake_execute.await_count, 2)

    def test_empty_board_stops_after_bounded_idle_polls(self) -> None:
        """没有任务时不能无限空转，达到 max_idle_polls 后正常结束。"""

        started = self._start(max_idle_polls=2)
        result = self.autonomy.run_session(
            user_id="chen",
            session_id=started["session"]["id"],
            wait=lambda _seconds: None,
        )

        self.assertEqual(result["session"]["status"], "completed")
        self.assertEqual(result["session"]["stop_reason"], "idle_timeout")
        self.assertEqual(result["session"]["idle_poll_count"], 2)

    def test_retry_resumes_already_claimed_task(self) -> None:
        """模型失败后 Task 保持 in_progress，下一次 Job 尝试应恢复而非遗弃它。"""

        self.tasks.create_task(user_id="chen", task_id="retry-task", subject="重试任务")
        started = self._start(max_tasks=1)
        first_attempt = AsyncMock(side_effect=RuntimeError("temporary model error"))
        with patch.object(self.teams, "execute_autonomous_task", first_attempt):
            with self.assertRaises(RuntimeError):
                self.autonomy.run_session(
                    user_id="chen",
                    session_id=started["session"]["id"],
                    wait=lambda _seconds: None,
                )

        claimed = self.tasks.list_tasks(user_id="chen")["tasks"][0]
        self.assertEqual(claimed["status"], "in_progress")
        self.autonomy.mark_retrying(
            user_id="chen",
            session_id=started["session"]["id"],
            error="temporary model error",
        )

        second_attempt = AsyncMock(return_value={"summary": "retry recovered"})
        with patch.object(self.teams, "execute_autonomous_task", second_attempt):
            result = self.autonomy.run_session(
                user_id="chen",
                session_id=started["session"]["id"],
                wait=lambda _seconds: None,
            )

        completed = self.tasks.list_tasks(user_id="chen")["tasks"][0]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(result["session"]["stop_reason"], "max_tasks")
        second_attempt.assert_awaited_once()

    def test_message_wakes_existing_autonomy_without_second_job(self) -> None:
        """活动自治 Worker 会消费 inbox，所以 send_message 不应重复派发 Job。"""

        started = self._start(max_idle_polls=2)
        with patch.object(self.teams, "_dispatch_teammate_job") as dispatch:
            sent = self.teams.send_message(
                user_id="chen",
                recipient="researcher",
                content="优先检查这条新指令。",
                dispatch=True,
            )

        dispatch.assert_not_called()
        self.assertEqual(sent["delivery_mode"], "autonomy_wakeup")
        self.assertEqual(sent["autonomy_session_id"], started["session"]["id"])

    def test_shutdown_request_is_processed_before_task_board(self) -> None:
        """有 pending shutdown 时，自治循环必须先离线，不能继续领取任务。"""

        self.tasks.create_task(user_id="chen", task_id="ignored", subject="不应领取")
        started = self._start(max_tasks=1)
        with patch.object(self.protocols, "_dispatch_protocol_job") as dispatch:
            shutdown = self.protocols.request_shutdown(
                user_id="chen",
                teammate_name="researcher",
                reason="测试安全退出",
                dispatch=True,
            )
        dispatch.assert_not_called()
        self.assertTrue(shutdown["dispatched"])

        result = self.autonomy.run_session(
            user_id="chen",
            session_id=started["session"]["id"],
            wait=lambda _seconds: None,
        )

        task = self.tasks.list_tasks(user_id="chen")["tasks"][0]
        teammate = self.teams.get_teammate(
            user_id="chen", name="researcher"
        )["teammate"]
        self.assertEqual(result["session"]["stop_reason"], "shutdown_protocol")
        self.assertEqual(task["status"], "pending")
        self.assertEqual(teammate["status"], "offline")

    def test_autonomy_tools_use_default_deny_permission_layer(self) -> None:
        """新工具必须显式登记；非法 session_id 不能穿过 PreToolUse。"""

        context = developer_context()
        self.assertTrue(
            check_tool_permission(
                "start_teammate_autonomy_tool",
                {"user_id": "chen", "teammate_name": "researcher"},
                execution_context=context,
            ).allowed
        )
        self.assertTrue(
            check_tool_permission(
                "list_autonomy_sessions_tool",
                {"user_id": "chen"},
                execution_context=context,
            ).allowed
        )
        self.assertFalse(
            check_tool_permission(
                "stop_autonomy_session_tool",
                {"user_id": "chen", "session_id": "../other"},
                execution_context=context,
            ).allowed
        )


if __name__ == "__main__":
    unittest.main()

"""s15 Agent Teams 的持久化 Registry、Message Bus 和 Worker 接入测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import AsyncMock, patch

from app.database import Database
from app.services.agent_teams import AgentTeamService
from app.services.background_jobs import BackgroundJobService
from app.services.job_executor import LocalJobExecutor
from app.services.job_store import InMemoryJobStore
from app.services.tool_permissions import check_tool_permission
from tests.agent_test_context import developer_context


class AgentTeamTests(unittest.TestCase):
    """所有测试使用临时 SQLite，不连接 Docker，也不调用真实模型。"""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "team.db"
        self.database_url = f"sqlite:///{database_path}"
        self.service = AgentTeamService(Database(self.database_url))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _spawn_researcher(self, *, user_id: str = "chen") -> dict:
        return self.service.spawn_teammate(
            user_id=user_id,
            name="researcher",
            role="代码研究员",
            instructions="先读取源码证据，再给出结论。",
        )

    def test_teammate_survives_service_recreation_and_is_user_isolated(self) -> None:
        """成员写入数据库后应跨 Service 存在，并按 user_id 隔离。"""

        self._spawn_researcher()
        recreated = AgentTeamService(Database(self.database_url))

        chen_team = recreated.list_teammates(user_id="chen")
        alice_team = recreated.list_teammates(user_id="alice")

        self.assertEqual(chen_team["count"], 1)
        self.assertEqual(chen_team["teammates"][0]["name"], "researcher")
        self.assertEqual(alice_team["count"], 0)

    def test_duplicate_and_reserved_teammate_names_are_rejected(self) -> None:
        """同一用户不能创建重名成员，main-agent 是保留身份。"""

        self._spawn_researcher()
        with self.assertRaises(ValueError):
            self._spawn_researcher()
        with self.assertRaises(ValueError):
            self.service.spawn_teammate(
                user_id="chen",
                name="main-agent",
                role="伪造负责人",
            )

    def test_message_bus_inbox_and_mark_read(self) -> None:
        """只写 inbox 时不调用模型，并能明确控制未读状态。"""

        self._spawn_researcher()
        sent = self.service.send_message(
            user_id="chen",
            recipient="researcher",
            content="分析当前项目的 Tool Calling。",
            dispatch=False,
        )
        unread = self.service.check_inbox(
            user_id="chen",
            recipient="researcher",
            unread_only=True,
        )
        marked = self.service.check_inbox(
            user_id="chen",
            recipient="researcher",
            mark_read=True,
        )

        self.assertFalse(sent["dispatched"])
        self.assertEqual(unread["unread_count"], 1)
        self.assertEqual(marked["unread_count"], 0)
        self.assertTrue(marked["messages"][0]["is_read"])

    def test_send_message_dispatches_job_and_sets_teammate_queued(self) -> None:
        """dispatch=true 应关联 Job，并把 teammate 状态改为 queued。"""

        self._spawn_researcher()
        def fake_dispatch(**kwargs):
            return {
                "job": {
                    "id": f"team-{kwargs['message_id']}",
                    "status": "queued",
                }
            }

        with patch.object(
            self.service,
            "_dispatch_teammate_job",
            side_effect=fake_dispatch,
        ) as dispatch:
            sent = self.service.send_message(
                user_id="chen",
                recipient="researcher",
                content="检查 Agent Runtime 的关键类。",
                dispatch=True,
            )

        teammate = self.service.get_teammate(
            user_id="chen",
            name="researcher",
        )["teammate"]
        dispatch.assert_called_once()
        self.assertTrue(sent["dispatched"])
        self.assertEqual(sent["message"]["job_id"], sent["job"]["id"])
        self.assertEqual(teammate["status"], "queued")
        self.assertEqual(teammate["current_job_id"], sent["job"]["id"])

    def test_teammate_executes_with_own_context_and_replies_once(self) -> None:
        """Worker 执行后，回复进入 main-agent inbox；重复投递不能再次调用模型。"""

        self._spawn_researcher()
        sent = self.service.send_message(
            user_id="chen",
            recipient="researcher",
            content="用一句话总结 Message Bus。",
            dispatch=False,
        )
        message_id = sent["message"]["id"]

        from app.services.subagent import subagent_service

        fake_response = {
            "answer": "Message Bus 是 Agent 之间持久化交换消息的通道。",
            "tool_calls": [],
        }
        with patch.object(
            subagent_service,
            "run",
            new=AsyncMock(return_value=fake_response),
        ) as teammate_run:
            first = asyncio.run(
                self.service.execute_teammate_message(
                    user_id="chen",
                    teammate_name="researcher",
                    message_id=message_id,
                )
            )
            second = asyncio.run(
                self.service.execute_teammate_message(
                    user_id="chen",
                    teammate_name="researcher",
                    message_id=message_id,
                )
            )

        main_inbox = self.service.check_inbox(
            user_id="chen",
            recipient="main-agent",
        )
        teammate = self.service.get_teammate(
            user_id="chen",
            name="researcher",
        )["teammate"]

        teammate_run.assert_awaited_once()
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(main_inbox["count"], 1)
        self.assertEqual(main_inbox["messages"][0]["sender"], "researcher")
        self.assertEqual(teammate["status"], "idle")

    def test_unknown_recipient_is_rejected(self) -> None:
        """Message Bus 不能把工作交给未注册的虚构成员。"""

        with self.assertRaises(KeyError):
            self.service.send_message(
                user_id="chen",
                recipient="missing-agent",
                content="执行任务",
            )

    def test_team_write_tools_have_bounded_permissions(self) -> None:
        """创建和派发属于写操作，必须经过明确边界检查。"""

        context = developer_context()
        allowed = check_tool_permission(
            "spawn_teammate_tool",
            {"user_id": "chen", "name": "reviewer", "role": "代码审查员"},
            execution_context=context,
        )
        denied = check_tool_permission(
            "send_team_message_tool",
            {"user_id": "../other", "recipient": "reviewer", "content": "test"},
            execution_context=context,
        )

        self.assertTrue(allowed.allowed)
        self.assertFalse(denied.allowed)

    def test_job_runner_dispatches_teammate_message_workflow(self) -> None:
        """teammate_message 必须穿过现有 Background Job Runtime。"""

        store = InMemoryJobStore()
        executor = LocalJobExecutor(store)
        jobs = BackgroundJobService(store=store, executor=executor)

        from app.services.agent_teams import agent_team_service

        fake_result = {
            "summary": "研究员已完成分析。",
            "workflow": "teammate_message",
            "teammate": "researcher",
            "reply": {},
        }
        try:
            with patch.object(
                agent_team_service,
                "execute_teammate_message",
                new=AsyncMock(return_value=fake_result),
            ) as execute:
                jobs.start_job(
                    user_id="chen",
                    job_id="team-runtime-test",
                    title="main-agent -> researcher",
                    job_type="teammate_message",
                    prompt="分析项目",
                    teammate_name="researcher",
                    team_message_id="msg-runtime-test",
                    max_retries=0,
                )

                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    job = jobs.get_job(
                        user_id="chen",
                        job_id="team-runtime-test",
                    )["job"]
                    if job["status"] in {"completed", "failed", "cancelled"}:
                        break
                    time.sleep(0.01)
        finally:
            jobs.shutdown(wait=True)

        execute.assert_awaited_once()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["workflow"], "teammate_message")


if __name__ == "__main__":
    unittest.main()

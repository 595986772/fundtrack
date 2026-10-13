"""s16 Team Protocols 的请求状态、事件匹配和 Worker 分发测试。

运行命令：

    conda run -n dl python -m unittest tests.test_team_protocols -v

测试全部使用临时 SQLite 和内存 Job Store，不会调用真实模型，也不要求启动 Docker。
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from app.database import Database
from app.services.agent_teams import AgentTeamService
from app.services.agent_tools import (
    get_agent_tools,
    get_subagent_tools,
    get_teammate_tools,
)
from app.services.background_jobs import BackgroundJobService
from app.services.job_executor import LocalJobExecutor
from app.services.job_store import InMemoryJobStore
from app.services.team_protocols import TeamProtocolService
from app.services.tool_permissions import check_tool_permission
from tests.agent_test_context import developer_context


class TeamProtocolTests(unittest.TestCase):
    """每个测试创建全新的数据库，避免协议状态在测试之间互相影响。"""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "protocols.db"
        self.database_url = f"sqlite:///{database_path}"
        self.database = Database(self.database_url)
        self.teams = AgentTeamService(self.database)
        self.protocols = TeamProtocolService(self.database)
        self.teams.spawn_teammate(
            user_id="chen",
            name="researcher",
            role="代码研究员",
            instructions="需要执行前先提交计划。",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_plan_request_is_persisted_as_pending_typed_event(self) -> None:
        """提交计划应同时写入 ProtocolState 和 plan_approval_request 事件。"""

        submitted = self.protocols.submit_plan(
            user_id="chen",
            teammate_name="researcher",
            plan="先搜索 Tool 定义，再读取 Agent Loop。",
            request_id="req-plan-persist",
        )

        # 重新创建 Service 模拟服务重启；数据仍能按 request_id 查回，说明不是内存变量。
        recreated = TeamProtocolService(Database(self.database_url))
        loaded = recreated.get_protocol(
            user_id="chen",
            request_id="req-plan-persist",
        )

        self.assertEqual(submitted["request"]["status"], "pending")
        self.assertEqual(loaded["request"]["protocol_type"], "plan_approval")
        self.assertEqual(len(loaded["events"]), 1)
        self.assertEqual(loaded["events"][0]["message_type"], "plan_approval_request")
        self.assertEqual(loaded["events"][0]["request_id"], "req-plan-persist")

    def test_plan_response_matches_request_and_rejects_duplicate(self) -> None:
        """审批响应必须复用原 request_id，一条请求也只能正式响应一次。"""

        self.protocols.submit_plan(
            user_id="chen",
            teammate_name="researcher",
            plan="读取三个关键文件后输出结论。",
            request_id="req-plan-review",
        )
        reviewed = self.protocols.review_plan(
            user_id="chen",
            request_id="req-plan-review",
            approve=True,
            feedback="批准，限制为只读分析。",
        )

        self.assertEqual(reviewed["request"]["status"], "approved")
        self.assertEqual(
            reviewed["response_event"]["message_type"],
            "plan_approval_response",
        )
        self.assertEqual(reviewed["response_event"]["request_id"], "req-plan-review")
        self.assertTrue(reviewed["response_event"]["approve"])

        # 如果允许第二次回答，同一 request_id 就可能同时出现批准和拒绝，状态将失去可信度。
        with self.assertRaises(ValueError):
            self.protocols.review_plan(
                user_id="chen",
                request_id="req-plan-review",
                approve=False,
                feedback="重复修改决定",
            )

    def test_plan_can_be_rejected_with_feedback(self) -> None:
        """rejected 也是正常终态，反馈会进入 typed response 供 teammate 后续读取。"""

        self.protocols.submit_plan(
            user_id="chen",
            teammate_name="researcher",
            plan="直接修改所有文件。",
            request_id="req-plan-reject",
        )
        reviewed = self.protocols.review_plan(
            user_id="chen",
            request_id="req-plan-reject",
            approve=False,
            feedback="范围太大，请先只分析 service 层。",
        )
        context = self.protocols.context_for_teammate(
            user_id="chen",
            teammate_name="researcher",
        )

        self.assertEqual(reviewed["request"]["status"], "rejected")
        self.assertFalse(reviewed["response_event"]["approve"])
        self.assertIn("req-plan-reject", context)
        self.assertIn("approve=False", context)

    def test_shutdown_dispatch_turns_teammate_offline_and_is_idempotent(self) -> None:
        """Worker 分发 shutdown_request 后应写匹配响应，并安全处理重复投递。"""

        self.protocols.request_shutdown(
            user_id="chen",
            teammate_name="researcher",
            reason="本轮协作结束。",
            dispatch=False,
            request_id="req-shutdown-direct",
        )
        first = self.protocols.dispatch_protocol_request(
            user_id="chen",
            request_id="req-shutdown-direct",
        )
        second = self.protocols.dispatch_protocol_request(
            user_id="chen",
            request_id="req-shutdown-direct",
        )
        teammate = self.teams.get_teammate(
            user_id="chen",
            name="researcher",
        )["teammate"]

        self.assertEqual(first["request"]["status"], "approved")
        self.assertEqual(first["response_event"]["message_type"], "shutdown_response")
        self.assertTrue(second["deduplicated"])
        self.assertEqual(teammate["status"], "offline")

    def test_protocol_state_is_isolated_by_user(self) -> None:
        """相同 request_id 可以属于不同用户，查询时 user_id 必须参与定位。"""

        self.protocols.submit_plan(
            user_id="chen",
            teammate_name="researcher",
            plan="chen 的计划",
            request_id="req-shared",
        )
        self.teams.spawn_teammate(
            user_id="alice",
            name="researcher",
            role="研究员",
        )
        self.protocols.submit_plan(
            user_id="alice",
            teammate_name="researcher",
            plan="alice 的计划",
            request_id="req-shared",
        )

        chen = self.protocols.get_protocol(user_id="chen", request_id="req-shared")
        alice = self.protocols.get_protocol(user_id="alice", request_id="req-shared")
        self.assertEqual(chen["request"]["payload"], "chen 的计划")
        self.assertEqual(alice["request"]["payload"], "alice 的计划")

    def test_teammate_protocol_runs_through_background_job_runtime(self) -> None:
        """shutdown 不在 HTTP 线程里直接完成，而是复用 s13 的 JobRunner。"""

        self.protocols.request_shutdown(
            user_id="chen",
            teammate_name="researcher",
            dispatch=False,
            request_id="req-shutdown-runtime",
        )
        store = InMemoryJobStore()
        jobs = BackgroundJobService(store=store, executor=LocalJobExecutor(store))

        try:
            # JobRunner 在运行时导入全局 service；测试替换成当前临时数据库实例。
            with patch(
                "app.services.team_protocols.team_protocol_service",
                self.protocols,
            ):
                jobs.start_job(
                    user_id="chen",
                    job_id="protocol-runtime-test",
                    title="shutdown protocol: researcher",
                    job_type="teammate_protocol",
                    teammate_name="researcher",
                    team_protocol_request_id="req-shutdown-runtime",
                    max_retries=0,
                )

                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    job = jobs.get_job(
                        user_id="chen",
                        job_id="protocol-runtime-test",
                    )["job"]
                    if job["status"] in {"completed", "failed", "cancelled"}:
                        break
                    time.sleep(0.01)
        finally:
            jobs.shutdown(wait=True)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["workflow"], "teammate_protocol")
        self.assertEqual(job["result"]["request_id"], "req-shutdown-runtime")

    def test_protocol_tools_have_role_and_permission_boundaries(self) -> None:
        """主 Agent 不能冒充 teammate 提交计划，写协议也必须先过 Permission。"""

        main_tool_names = {item.name for item in get_agent_tools()}
        temporary_tool_names = {item.name for item in get_subagent_tools()}
        teammate_tool_names = {item.name for item in get_teammate_tools()}

        self.assertNotIn("submit_team_plan_tool", main_tool_names)
        self.assertNotIn("submit_team_plan_tool", temporary_tool_names)
        self.assertIn("submit_team_plan_tool", teammate_tool_names)

        context = developer_context()
        allowed = check_tool_permission(
            "review_team_plan_tool",
            {
                "user_id": "chen",
                "request_id": "req-review-1",
                "approve": True,
                "feedback": "同意",
            },
            execution_context=context,
        )
        denied = check_tool_permission(
            "request_teammate_shutdown_tool",
            {
                "user_id": "../other",
                "teammate_name": "researcher",
                "reason": "test",
            },
            execution_context=context,
        )
        self.assertTrue(allowed.allowed)
        self.assertFalse(denied.allowed)


if __name__ == "__main__":
    unittest.main()

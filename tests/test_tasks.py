"""s12 Task System 的纯本地确定性测试。

测试使用临时目录，不会修改真实 ``data/tasks/``，也不会调用大模型。

运行方式：

    conda run -n dl python -m unittest tests.test_tasks -v
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from app.schemas import AgentChatRequest
from app.services import agent_tools as agent_tools_module
from app.services import tool_agent as tool_agent_module
from app.services.tasks import TaskStoreService
from app.services.tool_permissions import check_tool_permission
from tests.agent_test_context import developer_context


class TaskSystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "tasks"
        self.service = TaskStoreService(root=self.root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_task_survives_service_recreation(self) -> None:
        """重新创建 service 后仍能从 JSON 读回任务。"""

        created = self.service.create_task(
            user_id="chen",
            task_id="analyze-agent",
            subject="分析 Agent Loop",
            description="阅读 tool_agent.py 并形成总结。",
        )
        self.assertTrue(created["ok"])

        # 模拟服务重启：不用原来的 Python 对象，重新创建一个 service。
        restarted_service = TaskStoreService(root=self.root)
        listed = restarted_service.list_tasks(user_id="chen")

        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["tasks"][0]["id"], "analyze-agent")
        self.assertEqual(listed["tasks"][0]["status"], "pending")
        self.assertTrue((self.root / "chen" / "analyze-agent.json").is_file())

    def test_dependency_blocks_claim_until_completed(self) -> None:
        """下游任务必须等待所有 blocked_by 任务完成。"""

        self.service.create_task(
            user_id="chen",
            task_id="design-auth",
            subject="设计登录接口",
        )
        self.service.create_task(
            user_id="chen",
            task_id="implement-auth",
            subject="实现登录接口",
            blocked_by=["design-auth"],
        )

        with self.assertRaisesRegex(ValueError, "未完成依赖"):
            self.service.claim_task(
                user_id="chen",
                task_id="implement-auth",
                owner="main-agent",
            )

        self.service.claim_task(
            user_id="chen",
            task_id="design-auth",
            owner="main-agent",
        )
        completed = self.service.update_task(
            user_id="chen",
            task_id="design-auth",
            status="completed",
        )

        self.assertEqual(
            completed["newly_unblocked_task_ids"],
            ["implement-auth"],
        )

        claimed = self.service.claim_task(
            user_id="chen",
            task_id="implement-auth",
            owner="main-agent",
        )
        self.assertEqual(claimed["task"]["status"], "in_progress")
        self.assertEqual(claimed["task"]["owner"], "main-agent")

    def test_cycle_dependency_is_rejected(self) -> None:
        """A 依赖 B、B 又依赖 A 时必须拒绝。"""

        self.service.create_task(
            user_id="chen",
            task_id="task-a",
            subject="任务 A",
        )
        self.service.create_task(
            user_id="chen",
            task_id="task-b",
            subject="任务 B",
            blocked_by=["task-a"],
        )

        with self.assertRaisesRegex(ValueError, "循环依赖"):
            self.service.update_task(
                user_id="chen",
                task_id="task-a",
                blocked_by=["task-b"],
            )

    def test_status_machine_requires_claim_before_complete(self) -> None:
        """pending 不能直接 completed，必须先认领。"""

        self.service.create_task(
            user_id="chen",
            task_id="write-tests",
            subject="编写测试",
        )

        with self.assertRaisesRegex(ValueError, "不允许从 pending"):
            self.service.update_task(
                user_id="chen",
                task_id="write-tests",
                status="completed",
            )

        self.service.claim_task(
            user_id="chen",
            task_id="write-tests",
            owner="main-agent",
        )
        completed = self.service.update_task(
            user_id="chen",
            task_id="write-tests",
            status="completed",
        )
        self.assertEqual(completed["task"]["status"], "completed")

    def test_tasks_are_isolated_by_user_id(self) -> None:
        """一个用户不能通过任务列表看到另一个用户的任务。"""

        self.service.create_task(
            user_id="chen",
            task_id="chen-task",
            subject="Chen 的任务",
        )
        self.service.create_task(
            user_id="alice",
            task_id="alice-task",
            subject="Alice 的任务",
        )

        chen_tasks = self.service.list_tasks(user_id="chen")["tasks"]
        alice_tasks = self.service.list_tasks(user_id="alice")["tasks"]

        self.assertEqual([item["id"] for item in chen_tasks], ["chen-task"])
        self.assertEqual([item["id"] for item in alice_tasks], ["alice-task"])

    def test_permission_checks_persistent_task_writes(self) -> None:
        """持久化写工具必须经过 data/tasks/ 专用权限规则。"""

        context = developer_context()
        allowed = check_tool_permission(
            "task_create_tool",
            {
                "user_id": "chen",
                "task_id": "safe-task",
                "subject": "安全任务",
                "blocked_by": [],
            },
            execution_context=context,
        )
        denied = check_tool_permission(
            "task_create_tool",
            {
                "user_id": "../../outside",
                "task_id": "unsafe-task",
                "subject": "非法任务",
            },
            execution_context=context,
        )

        self.assertTrue(allowed.allowed)
        self.assertFalse(denied.allowed)
        self.assertTrue(
            agent_tools_module._is_excluded_path(
                agent_tools_module.PROJECT_ROOT
                / "data"
                / "tasks"
                / "chen"
                / "safe-task.json"
            )
        )


class TaskToolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_dispatch_injects_user_and_runs_hooks(self) -> None:
        """任务工具应复用主 Agent 的用户注入、Permission 和 Hooks。"""

        from langchain_core.messages import ToolMessage

        with TemporaryDirectory() as temporary_directory:
            temporary_store = TaskStoreService(
                root=Path(temporary_directory) / "tasks"
            )
            original_agent_tools_store = agent_tools_module.task_store_service
            agent_tools_module.task_store_service = temporary_store

            try:
                executed_call, _, hook_traces = (
                    await tool_agent_module.tool_calling_agent_service._execute_tool_call(
                        tool_call={
                            "id": "task-tool-call-1",
                            "name": "task_create_tool",
                            "args": {
                                "task_id": "integration-task",
                                "subject": "验证任务工具分发",
                            },
                        },
                        tool_map=agent_tools_module.get_agent_tool_map(),
                        request=AgentChatRequest(
                            user_id="chen",
                            message="创建一个持久化任务",
                        ),
                        execution_context=developer_context(),
                        tool_index=1,
                        ToolMessage=ToolMessage,
                    )
                )
            finally:
                agent_tools_module.task_store_service = original_agent_tools_store

        result = json.loads(executed_call.tool_result)
        self.assertTrue(result["ok"])
        self.assertEqual(executed_call.tool_args["user_id"], "chen")
        self.assertEqual(result["task"]["user_id"], "chen")
        self.assertTrue(
            any(
                trace.hook_name == "permission_pre_tool_use"
                and trace.status == "passed"
                for trace in hook_traces
            )
        )


if __name__ == "__main__":
    unittest.main()

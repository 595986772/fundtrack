"""s13 Background Tasks 的纯本地确定性测试。

测试只启动短时间的本地线程，不读取 API Key，也不会调用 DeepSeek。

运行方式：

    conda run -n dl python -m unittest tests.test_background_jobs -v
"""

from __future__ import annotations

import json
import time
import unittest

from app.schemas import AgentChatRequest
from app.services import agent_tools as agent_tools_module
from app.services import tool_agent as tool_agent_module
from app.services.background_jobs import BackgroundJobService
from app.services.tool_permissions import check_tool_permission
from tests.agent_test_context import developer_context


class BackgroundJobServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        # 每个测试使用独立内存注册表，避免测试之间互相看到 Job。
        self.service = BackgroundJobService()

    def tearDown(self) -> None:
        # 如果断言提前失败，也要停止仍在运行的守护线程。
        self.service.shutdown(wait=True)

    def _wait_for_terminal(
        self,
        *,
        user_id: str,
        job_id: str,
        timeout: float = 2.0,
    ) -> dict:
        """用有上限的短轮询等待测试 Job，避免测试无限卡住。"""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.service.get_job(user_id=user_id, job_id=job_id)["job"]
            if job["status"] in {"completed", "failed", "cancelled"}:
                return job
            time.sleep(0.01)
        self.fail(f"后台 Job {job_id} 未在 {timeout} 秒内结束")

    def test_start_returns_job_id_before_work_finishes(self) -> None:
        """start_job 应立即返回 queued Job，而不是等待全部步骤。"""

        started = self.service.start_job(
            user_id="chen",
            job_id="non-blocking-demo",
            title="后台分析项目",
            total_steps=4,
            step_delay_seconds=0.08,
        )

        self.assertTrue(started["accepted"])
        self.assertEqual(started["job"]["id"], "non-blocking-demo")
        self.assertEqual(started["job"]["current_step"], 0)

        completed = self._wait_for_terminal(
            user_id="chen",
            job_id="non-blocking-demo",
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["progress_percent"], 100.0)
        self.assertEqual(completed["result"]["completed_steps"], 4)

    def test_running_job_can_be_cancelled(self) -> None:
        """取消信号应把运行中 Job 变成 cancelled，并阻止后续完成。"""

        self.service.start_job(
            user_id="chen",
            job_id="cancel-demo",
            title="可取消任务",
            total_steps=10,
            step_delay_seconds=0.1,
        )
        cancelled = self.service.cancel_job(user_id="chen", job_id="cancel-demo")
        final_job = self._wait_for_terminal(user_id="chen", job_id="cancel-demo")

        self.assertTrue(cancelled["ok"])
        self.assertEqual(final_job["status"], "cancelled")
        self.assertTrue(final_job["cancel_requested"])
        self.assertIsNone(final_job["result"])

    def test_jobs_are_isolated_by_user(self) -> None:
        """相同 job_id 可以属于不同用户，列表只能看到自己的 Job。"""

        for user_id in ("chen", "alice"):
            self.service.start_job(
                user_id=user_id,
                job_id="same-id",
                title=f"{user_id} 的后台任务",
                total_steps=1,
                step_delay_seconds=0.05,
            )

        chen_jobs = self.service.list_jobs(user_id="chen")["jobs"]
        alice_jobs = self.service.list_jobs(user_id="alice")["jobs"]

        self.assertEqual([job["user_id"] for job in chen_jobs], ["chen"])
        self.assertEqual([job["user_id"] for job in alice_jobs], ["alice"])

    def test_completed_job_is_injected_into_prompt_index(self) -> None:
        """下一次组装 Prompt 时应能看到已完成 Job 的轻量通知。"""

        self.service.start_job(
            user_id="chen",
            job_id="prompt-notice",
            title="生成学习报告",
            total_steps=1,
            step_delay_seconds=0.05,
        )
        self._wait_for_terminal(user_id="chen", job_id="prompt-notice")

        prompt_index = self.service.index_for_prompt(user_id="chen")
        self.assertIn("[completed] prompt-notice", prompt_index)
        self.assertIn("生成学习报告 已完成", prompt_index)

    def test_permission_limits_background_job_runtime(self) -> None:
        """Permission 应拒绝非法用户和超出边界的耗时参数。"""

        context = developer_context()
        allowed = check_tool_permission(
            "background_job_start_tool",
            {
                "user_id": "chen",
                "title": "安全后台任务",
                "total_steps": 5,
                "step_delay_seconds": 0.2,
            },
            execution_context=context,
        )
        denied = check_tool_permission(
            "background_job_start_tool",
            {
                "user_id": "../../outside",
                "title": "非法后台任务",
                "total_steps": 999,
                "step_delay_seconds": 60,
            },
            execution_context=context,
        )

        self.assertTrue(allowed.allowed)
        self.assertFalse(denied.allowed)

    def test_background_job_persists_only_trusted_agent_mode(self) -> None:
        """异步 Worker 应恢复后端上下文，而不是相信 prompt 或请求体中的文字。"""

        context = developer_context()
        self.service.start_job(
            user_id="chen",
            job_id="developer-mode-job",
            title="开发者后台任务",
            total_steps=1,
            step_delay_seconds=0.05,
            execution_context=context,
        )

        record = self.service.store.get(
            user_id="chen",
            job_id="developer-mode-job",
        )
        self.assertEqual(record.payload["agent_mode"], "developer")

        with self.assertRaisesRegex(ValueError, "可信执行身份"):
            self.service.start_job(
                user_id="alice",
                title="伪造其他用户任务",
                execution_context=context,
            )


class BackgroundJobToolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_dispatch_injects_user_and_runs_hooks(self) -> None:
        """后台工具应复用主 Agent 的 user_id 注入、Permission 和 Hooks。"""

        from langchain_core.messages import ToolMessage

        temporary_service = BackgroundJobService()
        original_agent_tools_service = agent_tools_module.background_job_service
        agent_tools_module.background_job_service = temporary_service

        try:
            executed_call, _, hook_traces = (
                await tool_agent_module.tool_calling_agent_service._execute_tool_call(
                    tool_call={
                        "id": "background-tool-call-1",
                        "name": "background_job_start_tool",
                        "args": {
                            "job_id": "integration-job",
                            "title": "验证后台工具分发",
                            "total_steps": 1,
                            "step_delay_seconds": 0.05,
                        },
                    },
                    tool_map=agent_tools_module.get_agent_tool_map(),
                    request=AgentChatRequest(
                        user_id="chen",
                        message="启动一个后台任务",
                    ),
                    execution_context=developer_context(),
                    tool_index=1,
                    ToolMessage=ToolMessage,
                )
            )
        finally:
            agent_tools_module.background_job_service = original_agent_tools_service
            temporary_service.shutdown(wait=True)

        result = json.loads(executed_call.tool_result)
        self.assertTrue(result["ok"])
        self.assertEqual(executed_call.tool_args["user_id"], "chen")
        self.assertEqual(result["job"]["user_id"], "chen")
        self.assertTrue(
            any(
                trace.hook_name == "permission_pre_tool_use"
                and trace.status == "passed"
                for trace in hook_traces
            )
        )


if __name__ == "__main__":
    unittest.main()

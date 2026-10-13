"""工程化 Agent Runtime 的纯本地确定性测试。

这些测试不连接 Redis/PostgreSQL，也不调用 DeepSeek：

- SQL 持久化使用临时 SQLite；
- 执行器使用本地线程；
- agent_prompt 用 AsyncMock 替代真实模型。

Docker Compose 完整链路会在单元测试之后单独做集成验证。
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import AsyncMock, patch

from app.database import Database
from app.services.background_jobs import BackgroundJobService
from app.services.job_executor import LocalJobExecutor
from app.services.job_store import InMemoryJobStore, SqlJobStore


class FakeAgentResponse:
    """只实现 JobRunner 真正读取的 AgentChatResponse 字段。"""

    answer = "Tool Calling 由模型决策、后端执行工具、结果回填三部分组成。"
    stop_reason = "model_finished"
    tool_calls = []

    def model_dump(self, *, mode: str) -> dict:
        return {
            "answer": self.answer,
            "stop_reason": self.stop_reason,
            "tool_calls": self.tool_calls,
        }


class AgentRuntimeTests(unittest.TestCase):
    def _wait_for_terminal(
        self,
        service: BackgroundJobService,
        *,
        user_id: str,
        job_id: str,
        timeout: float = 4.0,
    ) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = service.get_job(user_id=user_id, job_id=job_id)["job"]
            if job["status"] in {"completed", "failed", "cancelled"}:
                return job
            time.sleep(0.01)
        self.fail(f"Job {job_id} 未在 {timeout} 秒内进入终态")

    def test_sql_job_survives_store_recreation(self) -> None:
        """重新创建 Store 模拟服务重启，完成记录仍应存在。"""

        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "runtime.db"
            database = Database(f"sqlite:///{database_path}")
            first_store = SqlJobStore(database)
            first_executor = LocalJobExecutor(first_store)
            service = BackgroundJobService(store=first_store, executor=first_executor)

            started = service.start_job(
                user_id="chen",
                job_id="persistent-job",
                title="验证数据库持久化",
                total_steps=2,
                step_delay_seconds=0.05,
                max_retries=0,
            )
            self.assertTrue(started["accepted"])
            completed = self._wait_for_terminal(
                service,
                user_id="chen",
                job_id="persistent-job",
            )
            service.shutdown(wait=True)

            recreated_store = SqlJobStore(Database(f"sqlite:///{database_path}"))
            recreated = recreated_store.get(user_id="chen", job_id="persistent-job")

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(recreated.status, "completed")
        self.assertEqual(recreated.result["completed_steps"], 2)

    def test_idempotency_key_returns_original_job(self) -> None:
        """同一个请求重发时只能产生一条 Job。"""

        store = InMemoryJobStore()
        executor = LocalJobExecutor(store)
        service = BackgroundJobService(store=store, executor=executor)
        try:
            first = service.start_job(
                user_id="chen",
                job_id="idempotent-first",
                title="幂等测试",
                total_steps=1,
                step_delay_seconds=0.05,
                idempotency_key="request-001",
            )
            second = service.start_job(
                user_id="chen",
                job_id="idempotent-second",
                title="这次提交不应该创建新 Job",
                total_steps=1,
                step_delay_seconds=0.05,
                idempotency_key="request-001",
            )

            self.assertTrue(first["accepted"])
            self.assertFalse(second["accepted"])
            self.assertEqual(second["job"]["id"], "idempotent-first")
            self.assertEqual(service.list_jobs(user_id="chen")["total_count"], 1)
        finally:
            service.shutdown(wait=True)

    def test_local_executor_retries_once_then_completes(self) -> None:
        """第一次工作流失败后应重新排队，第二次领取后完成。"""

        store = InMemoryJobStore()
        executor = LocalJobExecutor(store)
        service = BackgroundJobService(store=store, executor=executor)
        original_run_demo = executor.runner._run_demo_steps
        call_count = 0

        def flaky_run_demo(*, record, started_at):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("故障注入：第一次执行失败")
            return original_run_demo(record=record, started_at=started_at)

        try:
            with patch.object(executor.runner, "_run_demo_steps", side_effect=flaky_run_demo):
                service.start_job(
                    user_id="chen",
                    job_id="retry-job",
                    title="重试测试",
                    total_steps=1,
                    step_delay_seconds=0.05,
                    max_retries=1,
                )
                completed = self._wait_for_terminal(
                    service,
                    user_id="chen",
                    job_id="retry-job",
                )
        finally:
            service.shutdown(wait=True)

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["attempt_count"], 2)
        self.assertEqual(call_count, 2)

    def test_agent_prompt_runs_existing_agent_service_in_worker(self) -> None:
        """agent_prompt 必须调用真实 Agent service 边界并保存结构化结果。"""

        store = InMemoryJobStore()
        executor = LocalJobExecutor(store)
        service = BackgroundJobService(store=store, executor=executor)

        # 先导入目标对象，JobRunner 内部的延迟导入会拿到同一个 service 实例。
        from app.services.tool_agent import tool_calling_agent_service

        try:
            with patch.object(
                tool_calling_agent_service,
                "chat",
                new=AsyncMock(return_value=FakeAgentResponse()),
            ) as mocked_chat:
                service.start_job(
                    user_id="chen",
                    job_id="real-agent-boundary",
                    title="后台分析 Tool Calling",
                    job_type="agent_prompt",
                    prompt="总结当前项目 Tool Calling",
                    max_retries=0,
                )
                completed = self._wait_for_terminal(
                    service,
                    user_id="chen",
                    job_id="real-agent-boundary",
                )
        finally:
            service.shutdown(wait=True)

        mocked_chat.assert_awaited_once()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["workflow"], "agent_prompt")
        self.assertIn("模型决策", completed["result"]["summary"])


if __name__ == "__main__":
    unittest.main()

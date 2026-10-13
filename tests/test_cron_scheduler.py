"""s14 Cron Scheduler 的纯本地确定性测试。

测试不会调用 DeepSeek。Cron 命中后只启动 s13 的受控 Background Job。

运行方式：

    conda run -n dl python -m unittest tests.test_cron_scheduler -v
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from app.schemas import AgentChatRequest
from app.services import agent_tools as agent_tools_module
from app.services import tool_agent as tool_agent_module
from app.services.background_jobs import BackgroundJobService
from app.services.cron_scheduler import (
    CronSchedulerService,
    cron_matches,
    validate_cron,
)
from app.services.tool_permissions import check_tool_permission
from tests.agent_test_context import developer_context


class CronExpressionTests(unittest.TestCase):
    def test_validate_supported_cron_syntax(self) -> None:
        """五段式表达式应支持通配、步长、范围和列表。"""

        self.assertIsNone(validate_cron("*/5 9-17 * * 1-5"))
        self.assertIsNone(validate_cron("0,30 */2 * * *"))
        self.assertIsNotNone(validate_cron("*/0 * * * *"))
        self.assertIsNotNone(validate_cron("60 * * * *"))
        self.assertIsNotNone(validate_cron("* * * *"))

    def test_cron_matching_uses_dom_dow_or_semantics(self) -> None:
        """日期和星期同时受限时，满足任意一个就应命中。"""

        # 2026-07-14 是星期二，Cron 星期值为 2；日期值是 14。
        moment = datetime(2026, 7, 14, 9, 30)
        self.assertTrue(cron_matches("30 9 14 7 1", moment))
        self.assertTrue(cron_matches("30 9 15 7 2", moment))
        self.assertFalse(cron_matches("30 9 15 7 1", moment))


class CronSchedulerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "schedules"
        self.background_jobs = BackgroundJobService()
        self.service = CronSchedulerService(
            root=self.root,
            background_jobs=self.background_jobs,
            scheduler_poll_seconds=0.02,
            queue_poll_seconds=0.02,
        )

    def tearDown(self) -> None:
        self.service.stop(wait=True)
        self.background_jobs.shutdown(wait=True)
        self.temporary_directory.cleanup()

    def test_domain_callback_runs_once_per_minute_without_a_cron_record(self):
        calls = []
        self.service.set_tick_callback(calls.append)
        moment = datetime.fromisoformat("2026-09-17T14:30:00+08:00")
        self.service.tick(moment=moment)
        self.service.tick(moment=moment.replace(second=10))
        self.service.tick(moment=moment.replace(minute=31))
        self.assertEqual(len(calls), 2)
        self.service.set_tick_callback(None)
        self.service.tick(moment=moment.replace(minute=32))
        self.assertEqual(len(calls), 2)

    def test_durable_schedule_survives_service_recreation(self) -> None:
        """durable=True 应写 JSON，新 Service 能从磁盘恢复。"""

        self.service.create_schedule(
            user_id="chen",
            schedule_id="daily-report",
            cron="0 9 * * *",
            prompt="生成每日学习报告",
            recurring=True,
            durable=True,
        )

        restarted = CronSchedulerService(
            root=self.root,
            background_jobs=BackgroundJobService(),
        )
        try:
            listed = restarted.list_schedules(user_id="chen")
            self.assertEqual(listed["count"], 1)
            self.assertEqual(listed["schedules"][0]["id"], "daily-report")
            self.assertTrue((self.root / "chen" / "daily-report.json").is_file())
        finally:
            restarted.background_jobs.shutdown(wait=True)

    def test_session_only_schedule_is_not_loaded_after_restart(self) -> None:
        """durable=False 只保存在当前进程内存。"""

        self.service.create_schedule(
            user_id="chen",
            schedule_id="session-only",
            cron="* * * * *",
            prompt="仅当前进程运行",
            durable=False,
        )

        restarted = CronSchedulerService(
            root=self.root,
            background_jobs=BackgroundJobService(),
        )
        try:
            self.assertEqual(restarted.list_schedules(user_id="chen")["count"], 0)
        finally:
            restarted.background_jobs.shutdown(wait=True)

    def test_scheduler_produces_queue_event_only_once_per_minute(self) -> None:
        """Scheduler 每秒检查，但同一分钟不能重复入队。"""

        self.service.create_schedule(
            user_id="chen",
            schedule_id="minute-job",
            cron="* * * * *",
            prompt="每分钟检查学习进度",
            durable=True,
        )
        moment = datetime(2026, 7, 14, 10, 5, 1)

        first = self.service.tick(moment=moment)
        second = self.service.tick(moment=moment.replace(second=50))
        state = self.service.list_schedules(user_id="chen")

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(state["queue_depth"], 1)
        self.assertEqual(state["schedules"][0]["trigger_count"], 1)
        # tick 只负责生产事件；还没消费前不应出现 Background Job。
        self.assertEqual(self.background_jobs.list_jobs(user_id="chen")["count"], 0)

    def test_queue_processor_delivers_event_to_background_job(self) -> None:
        """Queue Processor 应把触发事件交给 s13 BackgroundJobService。"""

        self.service.create_schedule(
            user_id="chen",
            schedule_id="delivery-demo",
            cron="* * * * *",
            prompt="扫描待处理任务",
            durable=False,
        )
        self.service.trigger_now(user_id="chen", schedule_id="delivery-demo")
        delivery = self.service.process_next_event()

        self.assertIsNotNone(delivery)
        assert delivery is not None
        self.assertEqual(delivery["status"], "delivered")
        self.assertTrue(delivery["background_job_id"].startswith("cron-delivery-demo-"))
        jobs = self.background_jobs.list_jobs(user_id="chen")["jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "[Scheduled] 扫描待处理任务")

    def test_one_shot_schedule_is_removed_after_time_match(self) -> None:
        """recurring=False 命中一次后应删除定义和 durable 文件。"""

        self.service.create_schedule(
            user_id="chen",
            schedule_id="one-shot",
            cron="* * * * *",
            prompt="一次性提醒",
            recurring=False,
            durable=True,
        )
        self.service.tick(moment=datetime(2026, 7, 14, 11, 0))

        self.assertEqual(self.service.list_schedules(user_id="chen")["count"], 0)
        self.assertFalse((self.root / "chen" / "one-shot.json").exists())

    def test_independent_threads_fire_without_http_request(self) -> None:
        """启动 Service 后，即使没有 Agent 请求也应自动检查和交付。"""

        self.service.create_schedule(
            user_id="chen",
            schedule_id="automatic",
            cron="* * * * *",
            prompt="自动交付测试",
            durable=False,
        )
        self.service.start()

        deadline = time.monotonic() + 2.0
        deliveries: list[dict] = []
        while time.monotonic() < deadline:
            deliveries = self.service.recent_deliveries(user_id="chen")
            if deliveries:
                break
            time.sleep(0.02)

        self.assertTrue(self.service.is_running)
        self.assertEqual(deliveries[0]["status"], "delivered")

    def test_permission_checks_cron_writes(self) -> None:
        """非法表达式或越权用户应在 PreToolUse 阶段被拒绝。"""

        context = developer_context()
        allowed = check_tool_permission(
            "schedule_cron_tool",
            {
                "user_id": "chen",
                "cron": "*/5 * * * *",
                "prompt": "每五分钟检查一次",
            },
            execution_context=context,
        )
        denied = check_tool_permission(
            "schedule_cron_tool",
            {
                "user_id": "../../outside",
                "cron": "99 * * * *",
                "prompt": "非法 Cron",
            },
            execution_context=context,
        )

        self.assertTrue(allowed.allowed)
        self.assertFalse(denied.allowed)


class CronToolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_dispatch_injects_user_and_runs_hooks(self) -> None:
        """Cron 工具应复用主 Agent 的 user_id 注入、Permission 和 Hooks。"""

        from langchain_core.messages import ToolMessage

        with TemporaryDirectory() as temporary_directory:
            temporary_background_jobs = BackgroundJobService()
            temporary_scheduler = CronSchedulerService(
                root=Path(temporary_directory) / "schedules",
                background_jobs=temporary_background_jobs,
            )
            original_agent_tools_service = agent_tools_module.cron_scheduler_service
            agent_tools_module.cron_scheduler_service = temporary_scheduler

            try:
                executed_call, _, hook_traces = (
                    await tool_agent_module.tool_calling_agent_service._execute_tool_call(
                        tool_call={
                            "id": "cron-tool-call-1",
                            "name": "schedule_cron_tool",
                            "args": {
                                "schedule_id": "integration-cron",
                                "cron": "*/10 * * * *",
                                "prompt": "定时检查项目",
                                "durable": True,
                            },
                        },
                        tool_map=agent_tools_module.get_agent_tool_map(),
                        request=AgentChatRequest(
                            user_id="chen",
                            message="创建一个 Cron",
                        ),
                        execution_context=developer_context(),
                        tool_index=1,
                        ToolMessage=ToolMessage,
                    )
                )
            finally:
                agent_tools_module.cron_scheduler_service = original_agent_tools_service
                temporary_scheduler.stop(wait=True)
                temporary_background_jobs.shutdown(wait=True)

        result = json.loads(executed_call.tool_result)
        self.assertTrue(result["ok"])
        self.assertEqual(executed_call.tool_args["user_id"], "chen")
        self.assertEqual(result["schedule"]["user_id"], "chen")
        self.assertTrue(
            any(
                trace.hook_name == "permission_pre_tool_use"
                and trace.status == "passed"
                for trace in hook_traces
            )
        )


if __name__ == "__main__":
    unittest.main()

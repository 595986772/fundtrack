"""通过真实 Redis/Celery 投递，测试模型为固定返回值，不使用收费 API。"""

from contextlib import ExitStack
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from app.config import settings
from app.database import Database
from app.services.background_jobs import BackgroundJobService
from app.services.job_executor import CeleryJobExecutor
from app.services.job_store import SqlJobStore, utc_now
from app.services.portfolio_research_checkpoints import create_portfolio_research_checkpoint_runtime
from tests.test_reliable_research_checkpoints import build_durable_graph, PORTFOLIO_ID


@unittest.skipUnless(os.getenv("TEST_JOB_REDIS_URL"), "需要 TEST_JOB_REDIS_URL")
class CeleryReliableJobTests(unittest.TestCase):
    def test_real_broker_duplicate_delivery_does_not_repeat_research(self):
        from celery import Celery
        from celery.contrib.testing.worker import start_worker
        from redis import Redis
        from app.worker_tasks import execute_background_job

        with ExitStack() as stack:
            directory = Path(stack.enter_context(TemporaryDirectory()))
            url = f"sqlite:///{directory / 'runtime.db'}"
            database = Database(url)
            stack.callback(database.engine.dispose)
            stack.enter_context(patch.object(settings, "ai_governance_enabled", False))
            stack.enter_context(patch.object(settings, "reliable_research_jobs_enabled", True))
            store = SqlJobStore(database)
            runtime = create_portfolio_research_checkpoint_runtime(backend="sqlite", database_url=url)
            stack.callback(runtime.close)
            graph = build_durable_graph(database, runtime)
            graph.interpreter.interpret = Mock(wraps=graph.interpreter.interpret)
            stack.enter_context(patch("app.services.portfolio_research_graph.portfolio_research_graph_service", graph))
            stack.enter_context(patch("app.worker_tasks.runtime_database", database))

            # 单独 Celery app/队列/Redis key 前缀，绝不清空开发环境的队列或数据库。
            prefix = "fundscope-reliability-test:" + uuid4().hex + ":"
            redis = Redis.from_url(os.environ["TEST_JOB_REDIS_URL"])
            stack.callback(redis.close)
            def clear_test_keys():
                keys = list(redis.scan_iter(match=prefix + "*"))
                if keys:
                    redis.delete(*keys)
            stack.callback(clear_test_keys)
            celery = Celery("reliability-test", broker=os.environ["TEST_JOB_REDIS_URL"])
            stack.callback(celery.close)
            celery.conf.update(task_default_queue="research-test", task_ignore_result=True,
                broker_transport_options={"global_keyprefix": prefix, "socket_timeout": 3})
            # 注册生产任务的原始 run，只有依赖数据库和模型被测试替换。
            task = celery.task(name="agent_runtime.execute_background_job")(execute_background_job.run)
            stack.enter_context(patch("app.worker_tasks.execute_background_job", task))
            stack.enter_context(start_worker(celery, perform_ping_check=False, pool="solo",
                                            concurrency=1, shutdown_timeout=10))
            service = BackgroundJobService(store=store, executor=CeleryJobExecutor())
            response = service.start_job(user_id="chen", job_id="broker-test", title="test",
                job_type="portfolio_research", portfolio_id=PORTFOLIO_ID)
            self.assertTrue(response["accepted"])
            service.delivery.repository.clock = utc_now
            delivery = service.delivery.repository.reserve_delivery(*service.delivery.repository.due_deliveries("celery")[0])
            # 模拟发布后未收到确认而再次发送，两条消息都走真正的 Redis。
            for _ in range(2):
                service.executor.submit(user_id="chen", job_id="broker-test", timeout_seconds=60, dispatch_id=delivery.id)
            deadline = time.monotonic() + 15
            while store.get(user_id="chen", job_id="broker-test").status != "completed":
                if time.monotonic() >= deadline:
                    self.fail(str(service.get_job(user_id="chen", job_id="broker-test")))
                time.sleep(0.1)
            # 让 solo worker 有机会消费第二条消息，再检查领取次数和调用次数。
            time.sleep(0.5)
            self.assertEqual(store.get(user_id="chen", job_id="broker-test").attempt_count, 1)
            self.assertEqual(graph.interpreter.interpret.call_count, 1)

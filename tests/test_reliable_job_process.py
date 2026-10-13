"""真实终止 Worker 进程，再用另一个进程恢复；所有行情和模型都是固定测试数据。"""

from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from app.config import settings
from app.database import Database
from app.services.background_jobs import BackgroundJobService
from app.services.job_store import SqlJobStore, utc_now
from app.services.reliable_jobs import ReliableJobRepository
from tests.test_reliable_jobs import CapturingReliableExecutor


class WorkerProcessRecoveryTests(unittest.TestCase):
    def test_sigkill_during_model_and_after_report(self):
        for pause in ("model", "commit"):
            with self.subTest(pause=pause), ExitStack() as stack:
                directory = Path(stack.enter_context(TemporaryDirectory()))
                url = f"sqlite:///{directory / 'runtime.db'}"
                database = Database(url)
                stack.callback(database.engine.dispose)
                stack.enter_context(patch.object(settings, "ai_governance_enabled", False))
                stack.enter_context(patch.object(settings, "reliable_research_jobs_enabled", True))
                store = SqlJobStore(database)
                service = BackgroundJobService(store=store, executor=CapturingReliableExecutor())
                service.start_job(user_id="chen", job_id="kill-test", title="test", job_type="portfolio_research",
                    portfolio_id="portfolio_0123456789abcdef", timeout_seconds=120)
                repository = ReliableJobRepository(store, clock=utc_now, lease_seconds=3)
                delivery = repository.reserve_delivery(*repository.due_deliveries("local")[0])
                env = {**os.environ, "DATABASE_URL": url, "LANGGRAPH_CHECKPOINT_DATABASE_URL": url,
                    "JOB_LEASE_SECONDS": "3", "AI_GOVERNANCE_ENABLED": "false"}
                # 子进程输出到临时日志，父进程有明确 30 秒上限，不会在 readline 永久卡住。
                log = directory / "worker.log"
                output = stack.enter_context(log.open("w+"))
                process = subprocess.Popen([sys.executable, "-u", "-m", "tests.reliable_worker_fixture",
                    url, "kill-test", delivery.id, pause], env=env, stdout=output, stderr=subprocess.STDOUT)
                def cleanup():
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                stack.callback(cleanup)
                deadline = time.monotonic() + 30
                while "READY_FOR_KILL" not in log.read_text():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        self.fail(log.read_text())
                    time.sleep(0.1)
                # 长模型调用没有输出，但心跳独立续约；超过原租约时仍不能误接管。
                time.sleep(4)
                self.assertEqual(repository.recover_expired("local"), 0)
                process.kill()
                process.wait(timeout=5)
                deadline = time.monotonic() + 8
                while repository.recover_expired("local") == 0:
                    if time.monotonic() >= deadline:
                        self.fail("失联后没有接管")
                    time.sleep(0.1)
                self.assertIsNone(repository.claim("chen", "kill-test", delivery.id))
                delivery2 = repository.reserve_delivery(*repository.due_deliveries("local")[0])
                restarted = subprocess.run([sys.executable, "-u", "-m", "tests.reliable_worker_fixture",
                    url, "kill-test", delivery2.id, "none"], env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(restarted.returncode, 0, restarted.stdout + restarted.stderr)
                self.assertEqual(restarted.stdout.count("FAKE_MODEL_CALL"), 1 if pause == "model" else 0)
                record = store.get(user_id="chen", job_id="kill-test")
                self.assertEqual((record.status, record.attempt_count), ("completed", 2))
                self.assertEqual(repository.summary("chen", "kill-test")["recovery_count"], 1)
                self.assertTrue(record.result["graph_trace"]["resumed_from_checkpoint"])


if __name__ == "__main__":
    unittest.main()

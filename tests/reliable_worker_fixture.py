"""故障测试子进程。使用真 SQL/图和假模型；不是生产 Worker 或用户操作入口。"""

import sys
import time
from unittest.mock import patch

from app.database import Database
from app.services.job_store import SqlJobStore
from app.services.portfolio_research_checkpoints import create_portfolio_research_checkpoint_runtime
from app.services.reliable_job_runtime import run_reliable_job
from app.services.reliable_jobs import ReliableJobRepository
from tests.test_reliable_research_checkpoints import build_durable_graph, FakePortfolioInterpreter


def main():
    database_url, job_id, event_id, pause = sys.argv[1:]
    database = Database(database_url)
    runtime = create_portfolio_research_checkpoint_runtime(backend="sqlite", database_url=database_url)

    def block_for_kill():
        print("READY_FOR_KILL", flush=True)
        time.sleep(60)
        raise AssertionError("父进程未在期限内结束测试 Worker")

    class Interpreter(FakePortfolioInterpreter):
        def interpret(self, **kwargs):
            print("FAKE_MODEL_CALL", flush=True)
            if pause == "model":
                block_for_kill()
            return super().interpret(**kwargs)

    graph = build_durable_graph(database, runtime, Interpreter())
    original_check = ReliableJobRepository.check

    def check(repository, lease, **kwargs):
        if pause == "commit" and (kwargs.get("changes") or {}).get("status") == "completed":
            block_for_kill()
        return original_check(repository, lease, **kwargs)

    try:
        with patch("app.services.portfolio_research_graph.portfolio_research_graph_service", graph), \
                patch.object(ReliableJobRepository, "check", check):
            result = run_reliable_job(SqlJobStore(database), user_id="chen", job_id=job_id, dispatch_id=event_id)
            if result is None or result.status != "completed":
                raise AssertionError("恢复后未完成")
            print("RECOVERED", flush=True)
    finally:
        runtime.close()
        database.engine.dispose()


if __name__ == "__main__":
    main()

"""带租约的投研执行入口。业务流程仍由原 JobRunner/LangGraph 负责。"""

import logging
from threading import Event, Thread

from app.services.ai_governance_context import ai_execution_scope, stable_run_id
from app.services.reliable_jobs import JobLeaseLostError, ReliableJobRepository

logger = logging.getLogger(__name__)


class LeaseBoundJobStore:
    """把执行权固定到本轮对象上，LangGraph 的并行回调也无法遗漏版本校验。"""

    def __init__(self, repository, lease):
        self.repository, self.lease = repository, lease
        self.lost = Event()

    def get(self, *, user_id, job_id):
        self._check_identity(user_id, job_id)
        return self.repository.check(self.lease)

    def update(self, *, user_id, job_id, changes, allowed_statuses=None):
        self._check_identity(user_id, job_id)
        return self.repository.check(self.lease, changes=changes)

    def _check_identity(self, user_id, job_id):
        if self.lost.is_set() or (user_id, job_id) != (self.lease.user_id, self.lease.job_id):
            raise JobLeaseLostError()


def run_reliable_job(store, *, user_id, job_id, dispatch_id):
    """领取、续约、执行、结算。租约失效只退出，绝不替新 Worker 写 failed。"""
    from app.services.job_runtime import JobRunner

    repository = ReliableJobRepository(store)
    claimed = repository.claim(user_id, job_id, dispatch_id)
    if claimed is None:
        return None
    record, lease = claimed
    bound = LeaseBoundJobStore(repository, lease)
    stopped = Event()

    def renew():
        # 独立心跳，不以“长模型请求没有输出”推断 Worker 已宕机。
        while not stopped.wait(repository.lease_seconds / 3):
            try:
                repository.check(lease, heartbeat=True)
            except Exception:
                # 不能确认执行权时保守停写；数据库恢复后由补投循环接管。
                bound.lost.set()
                return

    heartbeat = Thread(target=renew, name=f"lease-{job_id}", daemon=True)
    heartbeat.start()
    try:
        # 同一个 Job 的接管/重试始终共用 Token 预算；不删除待确认的供应商消耗。
        with ai_execution_scope(user_id, stable_run_id("job", job_id), "background_job",
                                lambda: bound.get(user_id=user_id, job_id=job_id)):
            return JobRunner(bound)._execute_claimed(record)
    except JobLeaseLostError:
        return None
    except Exception as exc:
        repository.execution_failed(lease, exc)
        logger.warning("research attempt failed: job=%s version=%s error=%s", job_id, lease.version, type(exc).__name__)
        return None
    finally:
        stopped.set()
        heartbeat.join(timeout=2)

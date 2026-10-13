"""Background Job 的执行后端。

同一个 ``BackgroundJobService`` 可以选择：

- ``LocalJobExecutor``：当前进程线程，适合测试和不启动 Docker 的学习模式；
- ``CeleryJobExecutor``：Redis 队列 + 独立 Worker，适合工程化运行。

上层 REST、Agent Tools 和 Cron 都不需要知道当前使用哪一种。
"""

from __future__ import annotations

import logging
from threading import RLock, Thread
import time
from typing import Protocol
from uuid import uuid4

from app.services.job_runtime import JobCancelledError, JobRunner
from app.services.job_store import JobStore, utc_now
from app.services.ai_governance_context import AIControlError


logger = logging.getLogger(__name__)


class JobExecutor(Protocol):
    """Service 提交和撤销任务时依赖的执行器协议。

    BackgroundJobService 只调用这个协议，不 import 线程或 Redis 的具体细节。这就是
    依赖倒置：上层业务依赖稳定接口，底层执行方式可以按环境替换。
    """

    backend_name: str

    def submit(self, *, user_id: str, job_id: str, timeout_seconds: int, dispatch_id: str | None = None) -> str: ...

    def cancel(self, *, task_id: str | None) -> None: ...

    def shutdown(self, *, wait: bool, timeout: float) -> None: ...


class LocalJobExecutor:
    """线程执行器，状态仍然通过 Store 持久化。

    它适合直接运行 conda 项目时学习。线程只负责“在哪里跑”，真正的状态和 payload
    仍由 Store 管理，因此与 Celery 模式复用同一个 JobRunner。
    """

    backend_name = "local"

    def __init__(self, store: JobStore) -> None:
        self.store = store
        self.runner = JobRunner(store)
        self._lock = RLock()
        self._threads: dict[str, Thread] = {}

    def submit(self, *, user_id: str, job_id: str, timeout_seconds: int, dispatch_id: str | None = None) -> str:
        # local task_id 只是执行器句柄，业务查询仍使用 job_id。
        task_id = f"local-{uuid4().hex}"
        # daemon=True 保证关闭 FastAPI 时不会被教学线程永久卡住；正常关闭仍可 join。
        thread = Thread(
            target=self._execute_with_retry,
            kwargs={"user_id": user_id, "job_id": job_id, "dispatch_id": dispatch_id},
            name=f"background-{job_id}",
            daemon=True,
        )
        with self._lock:
            # 保存 Thread 只用于 shutdown 等待，不把它当成权威 Job 状态。
            self._threads[task_id] = thread
        thread.start()
        return task_id

    def cancel(self, *, task_id: str | None) -> None:
        # Python 线程不能安全强杀。取消标记已经由 Service 写入 Store，
        # JobRunner 会在步骤边界主动停止，因此这里无需直接操作 Thread。
        return None

    def shutdown(self, *, wait: bool, timeout: float) -> None:
        if not wait:
            return
        with self._lock:
            threads = list(self._threads.values())
        # 每个线程最多等待 timeout；这里不尝试强杀 Python 线程。
        for thread in threads:
            thread.join(timeout=max(0.0, timeout))

    def _execute_with_retry(self, *, user_id: str, job_id: str, dispatch_id: str | None = None) -> None:
        """本地模式也执行有上限的重试，行为与 Celery Worker 对齐。"""

        from app.services.job_store import SqlJobStore
        from app.services.reliable_jobs import ReliableJobRepository
        if isinstance(self.store, SqlJobStore) and ReliableJobRepository(self.store).is_managed(user_id, job_id):
            from app.services.reliable_job_runtime import run_reliable_job
            # 该分支有自己的带版本重试，不能落入下面无租约的旧处理器。
            try:
                run_reliable_job(self.store, user_id=user_id, job_id=job_id, dispatch_id=dispatch_id)
            except Exception as exc:
                logger.warning("reliable local execution interrupted: %s", type(exc).__name__)
            return

        while True:
            try:
                # JobRunner 会原子 claim；重试循环每次都必须重新从 queued 开始领取。
                self.runner.run(user_id=user_id, job_id=job_id)
                return
            except JobCancelledError:
                # 取消是预期控制流，不记录为失败，也不触发重试。
                return
            except Exception as exc:
                # 异常发生后重新读取数据库，而不是依赖可能已经过期的局部对象。
                record = self.store.get(user_id=user_id, job_id=job_id)
                if record.cancel_requested or record.status == "cancelled":
                    return

                # attempt_count 在 claim 时递增，所以第一次失败对应 retries_used=0。
                retries_used = max(0, record.attempt_count - 1)
                if retries_used < record.max_retries and not isinstance(exc, AIControlError):
                    retry_number = retries_used + 1
                    delay = min(2 ** retries_used, 5)
                    self.store.update(
                        user_id=user_id,
                        job_id=job_id,
                        allowed_statuses={"running"},
                        changes={
                            "status": "queued",
                            "error": str(exc),
                            "progress_message": (
                                f"执行失败，{delay} 秒后进行第 {retry_number} 次重试。"
                            ),
                        },
                    )
                    # 指数退避避免故障期间无间隔地反复调用外部模型或数据库。
                    time.sleep(delay)
                    continue

                self.store.update(
                    user_id=user_id,
                    job_id=job_id,
                    allowed_statuses={"running", "queued"},
                    changes={
                        "status": "failed",
                        "error": str(exc),
                        "progress_message": "后台工作重试耗尽，执行失败。",
                        "completed_at": utc_now(),
                    },
                )
                logger.exception(
                    "local background job failed",
                    extra={"user_id": user_id, "job_id": job_id},
                )
                return


class CeleryJobExecutor:
    """把任务投递到 Redis，由独立 Celery Worker 消费。"""

    backend_name = "celery"

    def submit(self, *, user_id: str, job_id: str, timeout_seconds: int, dispatch_id: str | None = None) -> str:
        # 延迟导入避免普通 local 模式也强制依赖 Celery。
        from app.worker_tasks import execute_background_job

        # Celery 消息只携带定位所需的两个 ID，不复制完整 payload；Worker 从数据库读取
        # 最新权威记录，API 与 Worker 的数据契约因此落在 BackgroundJobRecord 上。
        task_id = f"job-{user_id}-{job_id}-{uuid4().hex[:8]}"
        execute_background_job.apply_async(
            args=[user_id, job_id],
            kwargs={"dispatch_id": dispatch_id} if dispatch_id else {},
            task_id=task_id,
            # 纳管任务由 SQL Outbox 重试投递，避免发布端内部长时间重试卡住扫描。
            **({"retry": False} if dispatch_id else {}),
            # soft limit 先给 Python 抛出超时机会；hard limit 多留 10 秒作为最终保险。
            soft_time_limit=timeout_seconds,
            time_limit=timeout_seconds + 10,
        )
        return task_id

    def cancel(self, *, task_id: str | None) -> None:
        if not task_id:
            return
        from app.celery_app import celery_app

        # terminate=False 不会粗暴杀死正在执行的 Python 代码；Worker 通过数据库取消标记退出。
        celery_app.control.revoke(task_id, terminate=False)

    def shutdown(self, *, wait: bool, timeout: float) -> None:
        # Worker 是独立进程，关闭 FastAPI 不应该顺带关闭 Worker。
        return None

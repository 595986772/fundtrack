"""Celery Worker 真正执行的任务入口。

API 进程只往 Redis 发送 ``user_id + job_id``。独立 Worker 收到后从 PostgreSQL 读取
完整 Job，再交给 JobRunner。这样队列消息很小，数据库是唯一权威状态源。
"""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.database import runtime_database
from app.services.job_runtime import JobCancelledError, JobRunner
from app.services.job_store import SqlJobStore, utc_now
from app.services.ai_governance_context import AIControlError


logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="agent_runtime.execute_background_job")
def execute_background_job(self, user_id: str, job_id: str, dispatch_id: str | None = None):
    """从共享数据库读取 payload，执行工作流，并进行有上限重试。

    ``bind=True`` 让函数拿到 Celery task 自身的 request/retry 能力，而不是 Agent tool。
    """

    # 每个 Worker 进程有自己的 Python 对象，但连接的是和 API 相同的数据库。
    store = SqlJobStore(runtime_database)
    runner = JobRunner(store)

    from app.services.reliable_jobs import ReliableJobRepository
    if ReliableJobRepository(store).is_managed(user_id, job_id):
        from app.services.reliable_job_runtime import run_reliable_job
        # broker redelivered 不是 Worker 失联证明；这里只凭 SQL 租约和事件版本领取。
        result = run_reliable_job(store, user_id=user_id, job_id=job_id, dispatch_id=dispatch_id)
        return {"job_id": job_id, "status": result.status if result else "skipped"}

    # acks_late + task_reject_on_worker_lost 会在 Worker 异常退出后重新投递消息。
    # 原进程来不及把 running 改回 queued，所以只对 broker 明确标记的 redelivery
    # 恢复状态；普通重复消息仍会被 JobStore.claim 拒绝，避免并发执行两次。
    delivery_info = getattr(self.request, "delivery_info", {}) or {}
    current = store.get(user_id=user_id, job_id=job_id)
    if delivery_info.get("redelivered") and current.status == "running":
        store.update(
            user_id=user_id,
            job_id=job_id,
            allowed_statuses={"running"},
            changes={
                "status": "queued",
                "progress_message": "检测到 Worker 异常退出，任务已重新入队。",
            },
        )

    try:
        # runner.run 内部先原子 claim；重复队列消息会返回 None，而不会重复执行。
        result = runner.run(user_id=user_id, job_id=job_id)
        if result is None:
            return {"job_id": job_id, "skipped": True}
        return {
            "job_id": job_id,
            "status": result.status,
            "result": result.result,
        }
    except JobCancelledError:
        # 协作式取消是正常终止，不应触发 Celery retry。
        return {"job_id": job_id, "status": "cancelled"}
    except Exception as exc:
        # runner 抛错时重新读当前状态，用户取消优先于自动重试。
        record = store.get(user_id=user_id, job_id=job_id)
        if record.cancel_requested or record.status == "cancelled":
            return {"job_id": job_id, "status": "cancelled"}

        # attempt_count 统计实际领取次数；max_retries 统计首次之外最多再试几次。
        retries_used = max(0, record.attempt_count - 1)
        if retries_used < record.max_retries and not isinstance(exc, AIControlError):
            retry_number = retries_used + 1
            delay = min(2 ** retries_used, 30)
            store.update(
                user_id=user_id,
                job_id=job_id,
                allowed_statuses={"running"},
                changes={
                    "status": "queued",
                    "error": str(exc),
                    "progress_message": (
                        f"Worker 执行失败，{delay} 秒后进行第 {retry_number} 次重试。"
                    ),
                },
            )
            # self.retry 会向 Redis 重新投递同一个业务 Job；下次仍需重新 claim。
            raise self.retry(
                exc=exc,
                countdown=delay,
                max_retries=record.max_retries,
            )

        # 只有达到重试上限才写 failed，错误和完成时间都进入共享数据库供 SSE 查询。
        store.update(
            user_id=user_id,
            job_id=job_id,
            allowed_statuses={"running", "queued"},
            changes={
                "status": "failed",
                "error": str(exc),
                "progress_message": "Worker 重试耗尽，后台工作执行失败。",
                "completed_at": utc_now(),
            },
        )
        logger.exception(
            "celery background job failed",
            extra={"user_id": user_id, "job_id": job_id},
        )
        return {"job_id": job_id, "status": "failed", "error": str(exc)}

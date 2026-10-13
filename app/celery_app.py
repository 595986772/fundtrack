"""Celery 应用配置。

启动命令：

    celery -A app.celery_app.celery_app worker --loglevel=INFO

FastAPI 只负责提交任务；该 Worker 进程从 Redis 获取消息并更新 PostgreSQL。
"""

from celery import Celery
from celery.signals import worker_process_shutdown, worker_shutdown

from app.config import settings
from app.services.deployment import ensure_recovery_released

# 在创建 Celery 应用前检查，不能只挂到会吞掉异常的 signal handler 上。
ensure_recovery_released()


@worker_process_shutdown.connect
@worker_shutdown.connect
def flush_worker_traces(**kwargs):
    # Provider 首次使用时在当前子进程创建；正常退出尽量清空导出队列。
    from app.services.tracing import shutdown_tracing
    shutdown_tracing()


celery_app = Celery(
    "fastapi_agent_runtime",
    # broker 负责传递“要执行什么”的消息；result backend 保存 Celery 自身任务结果。
    # 本项目的业务 Job 结果仍以 PostgreSQL background_jobs 表为准。
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.worker_tasks"],
)

# 这些设置让消息和结果保持普通 JSON，避免不安全的 pickle 反序列化。
celery_app.conf.update(
    # 输入、输出和允许接收的内容全部限制为 JSON。
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # enable_utc 让内部时间统一，timezone 主要影响日志和调度显示。
    timezone="Asia/Shanghai",
    enable_utc=True,
    # STARTED 状态便于 Celery 运维观察；业务 running 状态仍由 JobStore.claim 写入。
    task_track_started=True,
    # Worker 完成后才 ack；若进程意外退出，broker 可以重新投递未完成消息。
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # 每个 Worker 子进程一次只预取一个任务，减少长任务导致的分配不均。
    worker_prefetch_multiplier=1,
    # 启动时 Redis 暂时未就绪会继续重连，而不是直接退出容器。
    broker_connection_retry_on_startup=True,
    # Outbox 会负责补投，单次 Redis 连接/发送失败应尽快返回发布者。
    broker_connection_timeout=3,
    broker_transport_options={"socket_connect_timeout": 3, "socket_timeout": 3},
)

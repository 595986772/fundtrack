"""Outbox 投递循环：补投已存在的研究任务，不创建研究、不主动刷新行情。"""

import logging
from threading import Event, Lock, Thread

from app.config import settings
from app.services.reliable_jobs import ReliableJobRepository
from app.services.tracing import trace_span

logger = logging.getLogger(__name__)


class JobDeliveryService:
    def __init__(self, store, executor):
        self.repository = ReliableJobRepository(store)
        self.executor = executor
        self._stop = Event()
        self._wake = Event()
        self._lock = Lock()
        self._thread = None

    def start(self):
        """每个 API 副本都可以启动；SQL 抢占保证一条事件只被一个发布者领取。"""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = Thread(target=self._run, name="research-outbox", daemon=True)
            self._thread.start()

    def wake(self):
        self._wake.set()

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def stop(self, timeout=2):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def tick(self):
        """一次有界扫描。测试可以显式调用，不需要依赖真实轮询时间。"""
        backend = self.executor.backend_name
        self.repository.recover_expired(backend)
        for user_id, job_id, event_id in self.repository.due_deliveries(backend):
            if self._stop.is_set():
                break
            delivery = self.repository.reserve_delivery(user_id, job_id, event_id)
            if delivery is None:
                continue
            # 不在 SQL 事务里等待 Redis；发送成功但 ACK 丢失时重复投递也安全。
            try:
                record = self.repository.store.get(user_id=user_id, job_id=job_id)
                with trace_span("job.dispatch", carrier=record.payload.get("_trace_context") or {},
                        attributes={"job.id": job_id, "job.dispatch_id": delivery.id,
                                    "job.backend": backend}):
                    task_id = self.executor.submit(user_id=user_id, job_id=job_id,
                        timeout_seconds=delivery.timeout_seconds, dispatch_id=delivery.id)
            except Exception as exc:
                self.repository.acknowledge_delivery(delivery, error=exc)
            else:
                self.repository.acknowledge_delivery(delivery, task_id=task_id)

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.tick()
            except Exception as exc:
                # 数据库短暂不可用时留待下轮处理，不把连接串等敏感信息写入日志。
                logger.warning("research delivery scan failed: %s", type(exc).__name__)
            self._wake.wait(settings.job_outbox_poll_seconds)

"""组合投研任务的可靠性账本，不执行模型，也不刷新市场数据。

Job 是业务状态，Outbox 是待投递清单，Lease 是某一轮 Worker 的执行权。
所有状态转换都在短 SQL 事务内完成；消息发送和业务计算必须在事务外做。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import and_, func, or_, select

from app.config import settings
from app.models import BackgroundJobModel, JobOutboxModel, ReliableJobStateModel
from app.services.ai_governance_context import AIControlError
from app.services.ai_usage import lock_governance_transaction


ACTIVE_DELIVERIES = ("pending", "sending", "sent")


def aware(value):
    """SQLite 会丢失时区标签，表里的时间统一按 UTC 解释。"""
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def database_now(session):
    """投递时间与执行租约共用 SQL 时钟，不依赖 API/Worker 的本机时钟一致。"""
    expression = func.clock_timestamp() if session.bind.dialect.name == "postgresql" else func.now()
    return aware(session.scalar(select(expression)))


def add_delivery(session, job, version, now, delay=0):
    event = JobOutboxModel(
        id=uuid4().hex, user_id=job.user_id, job_id=job.id,
        execution_version=version, status="pending",
        available_at=now + timedelta(seconds=delay), dispatch_attempts=0,
        created_at=now, updated_at=now,
    )
    session.add(event)
    return event


def enroll_reliable_job(session, record):
    """由 SqlJobStore.create 调用，不能在这里另开事务或直接发送消息。"""
    if (not settings.reliable_research_jobs_enabled
            or record.job_type != "portfolio_research"
            or record.execution_backend not in {"local", "celery"}):
        return
    if settings.langgraph_checkpoint_backend == "memory":
        raise ValueError("可靠投研任务需要持久化 checkpoint，请使用 auto、sqlite 或 postgres。")
    now = database_now(session)
    session.add(ReliableJobStateModel(
        user_id=record.user_id, job_id=record.id, execution_version=0,
        recovery_count=0, failure_retry_count=0,
        max_recoveries=settings.job_recovery_max_attempts, checkpoint_threads=[],
        updated_at=now,
    ))
    add_delivery(session, record, 1, now)


class JobLeaseLostError(AIControlError):
    """执行权已丢失。继承治理错误，避免模型包装层把它当成可重试的供应商故障。"""

    def __init__(self):
        super().__init__("job_lease_lost", "任务执行权已转移或已取消，旧 Worker 停止写入。", status_code=409)


@dataclass(frozen=True)
class JobLease:
    user_id: str
    job_id: str
    version: int
    token: str
    checkpoint_thread_id: str
    previous_threads: tuple[str, ...]


@dataclass(frozen=True)
class Delivery:
    id: str
    user_id: str
    job_id: str
    sender_token: str
    timeout_seconds: int


class ReliableJobRepository:
    """多个 API/Worker 共用的 SQL 协调器。锁定 Job 行后再操作租约和投递事件。"""

    def __init__(self, store, *, clock=None, lease_seconds=None):
        self.store = store
        self.database = store.database
        # clock 只供故障测试推进时间；生产使用数据库时钟，不依赖各 Worker 时钟一致。
        self.clock = clock
        self.lease_seconds = lease_seconds or settings.job_lease_seconds

    def _now(self, session):
        if self.clock:
            return self.clock()
        # PostgreSQL now() 固定为事务起点；clock_timestamp() 是拿到行锁后的当前时刻。
        return database_now(session)

    @contextmanager
    def _locked(self, user_id, job_id):
        with self.database.session() as session, session.begin():
            if self.database.engine.dialect.name == "sqlite":
                lock_governance_transaction(session, "reliable-job")
            job = session.scalar(select(BackgroundJobModel).where(
                BackgroundJobModel.user_id == user_id, BackgroundJobModel.id == job_id,
            ).with_for_update())
            state = session.get(ReliableJobStateModel, (user_id, job_id))
            if job is None or state is None:
                raise KeyError(f"找不到纳管任务：{job_id}")
            yield session, job, state, self._now(session)

    def is_managed(self, user_id, job_id):
        with self.database.session() as session:
            return session.get(ReliableJobStateModel, (user_id, job_id)) is not None

    def summary(self, user_id, job_id):
        """只展示恢复信息，不向客户端暴露 owner_token 和内部执行权。"""
        with self.database.session() as session:
            state = session.get(ReliableJobStateModel, (user_id, job_id))
            if state is None:
                return None
            return {
                "enabled": True, "execution_version": state.execution_version,
                "recovery_count": state.recovery_count,
                "failure_retry_count": state.failure_retry_count,
                "max_recoveries": state.max_recoveries,
                "last_recovery_reason": state.last_recovery_reason,
                "heartbeat_at": aware(state.heartbeat_at).isoformat() if state.heartbeat_at else None,
                "deadline_at": aware(state.deadline_at).isoformat() if state.deadline_at else None,
            }

    def due_deliveries(self, backend, limit=20):
        with self.database.session() as session:
            now = self._now(session)
            return list(session.execute(select(JobOutboxModel.user_id, JobOutboxModel.job_id, JobOutboxModel.id)
                .join(BackgroundJobModel, and_(
                    BackgroundJobModel.user_id == JobOutboxModel.user_id,
                    BackgroundJobModel.id == JobOutboxModel.job_id))
                .where(JobOutboxModel.status.in_(ACTIVE_DELIVERIES), JobOutboxModel.available_at <= now,
                       BackgroundJobModel.execution_backend == backend)
                .order_by(JobOutboxModel.available_at).limit(limit)))

    def reserve_delivery(self, user_id, job_id, event_id):
        """抢占一次发送机会。发送中宕机或发送后无人领取，都会在到期后再次补投。"""
        with self._locked(user_id, job_id) as (session, job, state, now):
            event = session.get(JobOutboxModel, event_id)
            if event is None or (event.user_id, event.job_id) != (user_id, job_id):
                return None
            if event.status not in ACTIVE_DELIVERIES or aware(event.available_at) > now:
                return None
            if (job.status != "queued" or job.cancel_requested
                    or event.execution_version != state.execution_version + 1):
                event.status = "closed"
                return None
            if state.deadline_at and aware(state.deadline_at) <= now:
                self._fail(job, state, now, "任务总执行期限已到。")
                event.status = "closed"
                return None
            event.status = "sending"
            event.sender_token = uuid4().hex
            event.dispatch_attempts += 1
            event.available_at = now + timedelta(seconds=30)
            event.updated_at = now
            return Delivery(event.id, user_id, job_id, event.sender_token, job.timeout_seconds)

    def acknowledge_delivery(self, delivery, *, task_id=None, error=None):
        with self._locked(delivery.user_id, delivery.job_id) as (session, job, state, now):
            event = session.get(JobOutboxModel, delivery.id)
            # Worker 可能比发送者的确认更快：已经 consumed 的事件不能被改回 sent。
            if event.status != "sending" or event.sender_token != delivery.sender_token:
                return
            if job.status != "queued" or job.cancel_requested:
                event.status = "closed"
                return
            event.status = "pending" if error else "sent"
            delay = min(2 ** min(event.dispatch_attempts, 6), 60) if error else settings.job_delivery_ack_seconds
            event.available_at = now + timedelta(seconds=delay)
            # 不保存可能包含连接密码的完整异常文本。
            event.last_error = type(error).__name__ if error else None
            event.updated_at = now
            job.celery_task_id = task_id or job.celery_task_id
            job.progress_message = "队列暂不可用，任务已保存，将自动补投。" if error else "已投递，等待 Worker 领取。"
            job.updated_at = now

    def claim(self, user_id, job_id, event_id):
        with self._locked(user_id, job_id) as (session, job, state, now):
            event = session.get(JobOutboxModel, event_id) if event_id else None
            if (event is None or (event.user_id, event.job_id) != (user_id, job_id)
                    or event.status not in {"sending", "sent"}
                    or event.execution_version != state.execution_version + 1
                    or job.status != "queued" or job.cancel_requested):
                return None
            if state.deadline_at and aware(state.deadline_at) <= now:
                self._fail(job, state, now, "任务总执行期限已到。")
                event.status = "closed"
                return None
            event.status = "consumed"
            event.updated_at = now
            state.execution_version += 1
            state.owner_token = uuid4().hex
            state.deadline_at = state.deadline_at or now + timedelta(seconds=job.timeout_seconds)
            state.heartbeat_at = state.updated_at = now
            state.lease_expires_at = min(now + timedelta(seconds=self.lease_seconds), aware(state.deadline_at))
            # 接管后使用新 checkpoint 分区。旧 Worker 最迟到的图状态也无法污染新执行。
            identity = sha256(f"{user_id}:{job_id}".encode()).hexdigest()[:32]
            thread_id = f"research:{identity}:v{state.execution_version}"
            previous = tuple(state.checkpoint_threads)
            state.checkpoint_threads = [*previous, thread_id]
            job.status = "running"
            job.attempt_count += 1
            job.started_at = job.started_at or now
            job.updated_at = now
            job.progress_message = "Worker 已领取，正在恢复已保存进度。" if previous else "Worker 已领取任务，开始执行。"
            session.flush()
            return self.store._model_to_record(job), JobLease(user_id, job_id, state.execution_version,
                state.owner_token, thread_id, previous)

    @staticmethod
    def _owns(job, state, lease, now):
        return (job.status == "running" and not job.cancel_requested
                and state.execution_version == lease.version and state.owner_token == lease.token
                and state.lease_expires_at is not None and aware(state.lease_expires_at) > now)

    def check(self, lease, *, heartbeat=False, changes=None):
        """检查执行权与写进度/结果在同一事务，不能采用先查询、再无条件 UPDATE。"""
        with self._locked(lease.user_id, lease.job_id) as (session, job, state, now):
            if not self._owns(job, state, lease, now):
                raise JobLeaseLostError()
            if heartbeat:
                state.heartbeat_at = state.updated_at = now
                state.lease_expires_at = min(now + timedelta(seconds=self.lease_seconds), aware(state.deadline_at))
            if changes:
                allowed = {"status", "current_step", "progress_message", "result", "error", "completed_at"}
                if not set(changes) <= allowed or changes.get("status", "running") not in {"running", "completed"}:
                    raise ValueError("投研 Worker 不允许执行该状态更新。")
                for key, value in changes.items():
                    setattr(job, key, value)
                job.updated_at = now
                if job.status == "completed":
                    state.owner_token = state.lease_expires_at = None
                session.flush()
            return self.store._model_to_record(job)

    @staticmethod
    def _fail(job, state, now, reason):
        job.status, job.error, job.progress_message = "failed", reason, reason
        job.completed_at = job.updated_at = now
        state.owner_token = state.lease_expires_at = None
        state.updated_at = now

    @staticmethod
    def _requeue(session, job, state, now, reason, delay=0):
        state.owner_token = state.lease_expires_at = None
        state.last_recovery_reason = reason
        state.updated_at = now
        job.status, job.progress_message, job.updated_at = "queued", reason, now
        add_delivery(session, job, state.execution_version + 1, now, delay)

    def execution_failed(self, lease, exc):
        """正常报错有独立重试上限；过期/被取消的 Worker 没有修改新任务的权力。"""
        with self._locked(lease.user_id, lease.job_id) as (session, job, state, now):
            if not self._owns(job, state, lease, now):
                return
            reason = f"投研执行失败（{type(exc).__name__}）。"
            if isinstance(exc, AIControlError):
                reason = str(exc)  # 服务端生成的额度/并发提示，让用户知道该等候还是调整预算。
            if isinstance(exc, AIControlError) or state.failure_retry_count >= job.max_retries:
                self._fail(job, state, now, reason + "已停止自动重试。")
                return
            state.failure_retry_count += 1
            self._requeue(session, job, state, now,
                reason + f"将从断点进行第 {state.failure_retry_count} 次重试。",
                delay=min(2 ** state.failure_retry_count, 30))

    def recover_expired(self, backend, limit=100):
        """仅接管租约真的到期的运行，不把普通重复消息误当成 Worker 已死亡。"""
        with self.database.session() as session:
            now = self._now(session)
            candidates = list(session.execute(select(BackgroundJobModel.user_id, BackgroundJobModel.id)
                .join(ReliableJobStateModel, and_(
                    ReliableJobStateModel.user_id == BackgroundJobModel.user_id,
                    ReliableJobStateModel.job_id == BackgroundJobModel.id))
                .where(BackgroundJobModel.execution_backend == backend,
                       BackgroundJobModel.status.in_(["queued", "running"]), or_(
                           ReliableJobStateModel.lease_expires_at <= now,
                           ReliableJobStateModel.deadline_at <= now))
                .order_by(ReliableJobStateModel.updated_at).limit(limit)))
        recovered = 0
        for user_id, job_id in candidates:
            with self._locked(user_id, job_id) as (session, job, state, now):
                if job.status not in {"queued", "running"} or job.cancel_requested:
                    continue
                if state.deadline_at and aware(state.deadline_at) <= now:
                    self._fail(job, state, now, "任务总执行期限已到，停止恢复。")
                elif job.status == "running" and state.lease_expires_at and aware(state.lease_expires_at) <= now:
                    if state.recovery_count >= state.max_recoveries:
                        self._fail(job, state, now, "Worker 多次失联，自动恢复次数已耗尽。")
                    else:
                        state.recovery_count += 1
                        self._requeue(session, job, state, now,
                            f"Worker 心跳已过期，正在进行第 {state.recovery_count} 次断点接管。")
                        recovered += 1
        return recovered

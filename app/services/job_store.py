"""Background Job 的存储抽象与两种实现。

为什么不让 ``BackgroundJobService`` 直接操作一个全局字典或 SQLAlchemy Session？
因为“业务规则”和“存在哪里”是两个变化方向：

- 单元测试希望使用快速、隔离的内存 Store；
- 本地运行希望使用 SQLite；
- Docker/企业环境希望使用 PostgreSQL。

三种场景对上层 service 暴露完全相同的方法，这就是 Repository/Store 层的价值。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Literal, Protocol, cast

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.database import Database
from app.models import BackgroundJobModel
from app.config import settings
from app.services.ai_governance_context import AIControlError
from app.services.ai_usage import lock_governance_transaction


BackgroundJobStatus = Literal[
    "queued",
    "running",
    "waiting_input",
    "completed",
    "failed",
    "cancelled",
]

TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
PENDING_JOB_STATUSES = {"queued", "running"}


def check_pending_job_limits(total: int, owned: int) -> None:
    """只算尚未结束的工作，历史记录和人工确认页不占执行队列。"""
    if total >= settings.ai_global_pending_jobs or owned >= settings.ai_user_pending_jobs:
        raise AIControlError("job_queue_full", "后台任务队列已满，请等待已有任务完成或取消后再提交")


def utc_now() -> datetime:
    """统一生成带时区的 UTC 时间，交给 API 时再输出 ISO 字符串。"""

    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class BackgroundJobRecord:
    """与数据库表解耦的领域对象。

    frozen=True 防止调用方绕过 Store 直接修改共享状态。每次更新都会返回一个新对象。
    """

    id: str
    user_id: str
    title: str
    job_type: str
    status: BackgroundJobStatus
    execution_backend: str
    current_step: int
    total_steps: int
    progress_message: str
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    cancel_requested: bool
    attempt_count: int
    max_retries: int
    timeout_seconds: int
    idempotency_key: str | None
    celery_task_id: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class JobStore(Protocol):
    """BackgroundJobService 依赖的最小存储协议。

    Protocol 只规定“要有哪些方法”，不要求继承某个基类。这样内存实现和 SQL 实现
    都能被 Service/JobRunner 替换使用，也便于测试时注入假实现。
    """

    # create 只负责持久化初始记录，不负责启动线程或发送 Celery 消息。
    def create(self, record: BackgroundJobRecord) -> BackgroundJobRecord: ...

    # user_id 必须参与每次定位，避免只凭 job_id 越过用户边界。
    def get(self, *, user_id: str, job_id: str) -> BackgroundJobRecord: ...

    def find_by_idempotency_key(
        self,
        *,
        user_id: str,
        idempotency_key: str,
    ) -> BackgroundJobRecord | None: ...

    def list(
        self,
        *,
        user_id: str,
        status: str | None,
        limit: int,
    ) -> tuple[list[BackgroundJobRecord], int]: ...

    def count(self, *, user_id: str) -> int: ...

    # claim 是 Worker 的原子“抢单”操作，只有 queued 才能变成 running。
    def claim(self, *, user_id: str, job_id: str) -> BackgroundJobRecord | None: ...

    def update(
        self,
        *,
        user_id: str,
        job_id: str,
        changes: dict[str, Any],
        allowed_statuses: set[str] | None = None,
    ) -> BackgroundJobRecord: ...


class InMemoryJobStore:
    """线程安全的内存实现，主要用于纯本地单元测试。"""

    def __init__(self) -> None:
        # 一个 Service 可能同时被 HTTP 线程和本地 Worker 线程访问，因此字典也要加锁。
        # RLock 允许 create 内部再次调用同样会加锁的 find_by_idempotency_key。
        self._lock = RLock()
        self._records: dict[tuple[str, str], BackgroundJobRecord] = {}

    def create(self, record: BackgroundJobRecord) -> BackgroundJobRecord:
        """模拟数据库主键和幂等唯一约束，保证两种 Store 行为尽量一致。"""

        key = (record.user_id, record.id)
        with self._lock:
            if key in self._records:
                raise ValueError(f"Job ID 已存在：{record.id}")
            if record.idempotency_key and self.find_by_idempotency_key(
                user_id=record.user_id,
                idempotency_key=record.idempotency_key,
            ):
                raise ValueError("idempotency_key 已存在。")
            self._records[key] = record
            # frozen dataclass 已不可原地修改，replace 再返回副本可进一步隔离调用方。
            return replace(record)

    def get(self, *, user_id: str, job_id: str) -> BackgroundJobRecord:
        with self._lock:
            record = self._records.get((user_id, job_id))
            if record is None:
                raise KeyError(f"找不到后台 Job：{job_id}")
            return replace(record)

    def find_by_idempotency_key(
        self,
        *,
        user_id: str,
        idempotency_key: str,
    ) -> BackgroundJobRecord | None:
        with self._lock:
            for record in self._records.values():
                if (
                    record.user_id == user_id
                    and record.idempotency_key == idempotency_key
                ):
                    return replace(record)
        return None

    def list(
        self,
        *,
        user_id: str,
        status: str | None,
        limit: int,
    ) -> tuple[list[BackgroundJobRecord], int]:
        """返回当前页和过滤后的总数；前端可区分 count 与 total_count。"""

        with self._lock:
            records = [
                replace(record)
                for record in self._records.values()
                if record.user_id == user_id
                and (status is None or record.status == status)
            ]
        records.sort(key=lambda item: item.updated_at, reverse=True)
        return records[:limit], len(records)

    def count(self, *, user_id: str) -> int:
        with self._lock:
            return sum(record.user_id == user_id for record in self._records.values())

    def claim(self, *, user_id: str, job_id: str) -> BackgroundJobRecord | None:
        """原子地把 queued 变为 running，避免两个 Worker 重复执行同一任务。"""

        with self._lock:
            current = self._require(user_id=user_id, job_id=job_id)
            # 返回 None 不是错误，而是告诉重复 Worker：“这条任务已被领取或结束”。
            if current.status != "queued" or current.cancel_requested:
                return None
            now = utc_now()
            claimed = replace(
                current,
                status="running",
                attempt_count=current.attempt_count + 1,
                started_at=current.started_at or now,
                updated_at=now,
                progress_message="Worker 已领取任务，开始执行。",
            )
            self._records[(user_id, job_id)] = claimed
            return replace(claimed)

    def update(
        self,
        *,
        user_id: str,
        job_id: str,
        changes: dict[str, Any],
        allowed_statuses: set[str] | None = None,
    ) -> BackgroundJobRecord:
        """在可选状态前置条件成立时更新，类似轻量 compare-and-set。"""

        with self._lock:
            current = self._require(user_id=user_id, job_id=job_id)
            # 例如取消已先把状态改为 cancelled，迟到的 Worker 完成写入就会在这里失效。
            if allowed_statuses is not None and current.status not in allowed_statuses:
                return replace(current)
            updated = replace(current, **changes, updated_at=utc_now())
            self._records[(user_id, job_id)] = updated
            return replace(updated)

    def _require(self, *, user_id: str, job_id: str) -> BackgroundJobRecord:
        record = self._records.get((user_id, job_id))
        if record is None:
            raise KeyError(f"找不到后台 Job：{job_id}")
        return record


class SqlJobStore:
    """SQLAlchemy 实现，可同时运行在 SQLite 和 PostgreSQL。

    每个公开方法都创建短生命周期 Session。API 与 Celery Worker 不共享 Python 对象，
    但它们连接同一数据库，所以能看到同一份权威状态。
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.create_schema()

    def create(self, record: BackgroundJobRecord) -> BackgroundJobRecord:
        """在一个事务中插入初始 Job，并把数据库唯一约束错误翻译成业务错误。"""

        model = self._record_to_model(record)
        try:
            with self.database.session() as session, session.begin():
                if settings.ai_governance_enabled:
                    # 锁和 INSERT 在同一事务；多个 API 副本同时建单也不能突破队列上限。
                    lock_governance_transaction(session, "ai-job-admission")
                    self._check_capacity(session, record.user_id)
                session.add(model)
                # Job 与待投递事件一起提交，彻底消除“已落库但来不及发消息”的空窗。
                from app.services.reliable_jobs import enroll_reliable_job
                enroll_reliable_job(session, record)
        except IntegrityError as exc:
            raise ValueError(
                "Job ID 或 idempotency_key 已存在，请查询原任务而不是重复提交。"
            ) from exc
        return record

    def get(self, *, user_id: str, job_id: str) -> BackgroundJobRecord:
        # session.get 可以直接利用 (user_id, id) 联合主键定位。
        with self.database.session() as session:
            model = session.get(BackgroundJobModel, (user_id, job_id))
            if model is None:
                raise KeyError(f"找不到后台 Job：{job_id}")
            return self._model_to_record(model)

    @staticmethod
    def _check_capacity(session, user_id):
        statement = select(func.count()).select_from(BackgroundJobModel).where(
            BackgroundJobModel.status.in_(PENDING_JOB_STATUSES))
        total = int(session.scalar(statement) or 0)
        owned = int(session.scalar(statement.where(BackgroundJobModel.user_id == user_id)) or 0)
        check_pending_job_limits(total, owned)

    def find_by_idempotency_key(
        self,
        *,
        user_id: str,
        idempotency_key: str,
    ) -> BackgroundJobRecord | None:
        # 幂等键必须和 user_id 一起查；否则不同用户的相同业务键会互相影响。
        statement = select(BackgroundJobModel).where(
            BackgroundJobModel.user_id == user_id,
            BackgroundJobModel.idempotency_key == idempotency_key,
        )
        with self.database.session() as session:
            model = session.scalar(statement)
            return self._model_to_record(model) if model is not None else None

    def list(
        self,
        *,
        user_id: str,
        status: str | None,
        limit: int,
    ) -> tuple[list[BackgroundJobRecord], int]:
        """同一套 filters 同时用于分页查询和总数查询。"""

        filters = [BackgroundJobModel.user_id == user_id]
        if status is not None:
            filters.append(BackgroundJobModel.status == status)

        statement = (
            select(BackgroundJobModel)
            .where(*filters)
            .order_by(BackgroundJobModel.updated_at.desc())
            .limit(limit)
        )
        # limit 只影响本页 records；total 仍返回所有符合条件的数量。
        count_statement = select(func.count()).select_from(BackgroundJobModel).where(*filters)
        with self.database.session() as session:
            models = list(session.scalars(statement))
            total = int(session.scalar(count_statement) or 0)
            return [self._model_to_record(model) for model in models], total

    def count(self, *, user_id: str) -> int:
        # SELECT ... FOR UPDATE 会把“检查 queued”和“改成 running”放进同一事务。
        # PostgreSQL 中另一个 Worker 会等待锁，随后看到 running 并返回 None。
        statement = (
            select(func.count())
            .select_from(BackgroundJobModel)
            .where(BackgroundJobModel.user_id == user_id)
        )
        with self.database.session() as session:
            return int(session.scalar(statement) or 0)

    def claim(self, *, user_id: str, job_id: str) -> BackgroundJobRecord | None:
        """用行锁完成原子领取，PostgreSQL 下多个 Worker 不会重复执行。"""

        statement = (
            select(BackgroundJobModel)
            .where(
                BackgroundJobModel.user_id == user_id,
                BackgroundJobModel.id == job_id,
            )
            .with_for_update()
        )
        with self.database.session() as session, session.begin():
            # SQLite 的 FOR UPDATE 不提供行锁，用写事务保证重复消息只能领取一次。
            if self.database.engine.dialect.name == "sqlite":
                lock_governance_transaction(session, "ai-job-claim")
            model = session.scalar(statement)
            if model is None:
                raise KeyError(f"找不到后台 Job：{job_id}")
            # cancel_requested 也要参与判断，防止已请求取消但状态尚未观察到的任务启动。
            from app.models import ReliableJobStateModel
            if model.job_type == "portfolio_research" and session.get(ReliableJobStateModel, (user_id, job_id)) is not None:
                # 纳管任务必须携带 Outbox 事件领取，不能退回没有 fencing 的旧入口。
                return None
            if model.status != "queued" or model.cancel_requested:
                return None

            now = utc_now()
            model.status = "running"
            model.attempt_count += 1
            model.started_at = model.started_at or now
            model.updated_at = now
            model.progress_message = "Worker 已领取任务，开始执行。"
            session.flush()
            return self._model_to_record(model)

    def update(
        self,
        *,
        user_id: str,
        job_id: str,
        changes: dict[str, Any],
        allowed_statuses: set[str] | None = None,
    ) -> BackgroundJobRecord:
        """锁定一行后更新，并用 allowed_statuses 防止迟到结果覆盖终态。"""

        statement = (
            select(BackgroundJobModel)
            .where(
                BackgroundJobModel.user_id == user_id,
                BackgroundJobModel.id == job_id,
            )
            .with_for_update()
        )
        with self.database.session() as session, session.begin():
            # waiting_input -> queued 也相当于重新排队，必须和新任务使用同一准入锁。
            if settings.ai_governance_enabled and changes.get("status") == "queued":
                lock_governance_transaction(session, "ai-job-admission")
            elif self.database.engine.dialect.name == "sqlite":
                # 取消与 Worker 的带租约写入也必须串行化，避免迟到完成覆盖取消。
                lock_governance_transaction(session, "job-update")
            model = session.scalar(statement)
            if model is None:
                raise KeyError(f"找不到后台 Job：{job_id}")
            if allowed_statuses is not None and model.status not in allowed_statuses:
                return self._model_to_record(model)

            if (settings.ai_governance_enabled and changes.get("status") == "queued"
                    and model.status not in PENDING_JOB_STATUSES):
                self._check_capacity(session, user_id)

            # changes 来自 service/runner 内部，但仍做字段白名单式存在性检查。
            for field_name, value in changes.items():
                if not hasattr(model, field_name):
                    raise ValueError(f"不允许更新 Job 字段：{field_name}")
                setattr(model, field_name, value)
            model.updated_at = utc_now()
            session.flush()
            return self._model_to_record(model)

    def _record_to_model(self, record: BackgroundJobRecord) -> BackgroundJobModel:
        """把不依赖 ORM 的领域对象转换成数据库对象。"""

        return BackgroundJobModel(**record.__dict__)

    def _model_to_record(self, model: BackgroundJobModel) -> BackgroundJobRecord:
        """离开 Session 前复制成领域对象，避免上层持有已脱离 Session 的 ORM 对象。"""

        return BackgroundJobRecord(
            id=model.id,
            user_id=model.user_id,
            title=model.title,
            job_type=model.job_type,
            status=cast(BackgroundJobStatus, model.status),
            execution_backend=model.execution_backend,
            current_step=model.current_step,
            total_steps=model.total_steps,
            progress_message=model.progress_message,
            payload=dict(model.payload or {}),
            result=dict(model.result) if model.result is not None else None,
            error=model.error,
            cancel_requested=model.cancel_requested,
            attempt_count=model.attempt_count,
            max_retries=model.max_retries,
            timeout_seconds=model.timeout_seconds,
            idempotency_key=model.idempotency_key,
            celery_task_id=model.celery_task_id,
            created_at=self._ensure_timezone(model.created_at),
            updated_at=self._ensure_timezone(model.updated_at),
            started_at=self._ensure_timezone(model.started_at),
            completed_at=self._ensure_timezone(model.completed_at),
        )

    def _ensure_timezone(self, value: datetime | None) -> datetime | None:
        # SQLite 读取 DateTime 时可能丢失 tzinfo；统一补回 UTC，保证 API 输出一致。
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

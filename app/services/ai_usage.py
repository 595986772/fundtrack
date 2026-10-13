"""持久化 Token 额度：预留 -> 发出请求 -> 按 usage 结算。

Redis 管“同时能跑多少”，SQL 管“已经花了多少”。这里用短事务同时更新每日桶、
任务桶和调用明细，不在等待模型期间持有数据库锁。SQLite 供本地使用，PostgreSQL
供多进程部署；两种实现都把同一用户的检查与扣减放在同一把事务锁内。
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import Database, runtime_database
from app.models import AIModelCallModel, AIUsageBucketModel
from app.services.ai_governance_context import AIControlError, AIExecutionScope


def lock_governance_transaction(session, scope: str) -> None:
    """事务结束自动释放锁，避免先查额度、再扣额度之间被另一个请求插队。"""
    if session.bind.dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    else:
        # PostgreSQL advisory lock 只使用服务端生成的哈希整数，不拼接用户 SQL。
        key = int.from_bytes(sha256(scope.encode()).digest()[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


class AIUsageService:
    def __init__(self, database: Database = runtime_database):
        self.database = database
        self.database.create_schema()

    @contextmanager
    def _transaction(self, user_id: str):
        try:
            with self.database.session() as session, session.begin():
                lock_governance_transaction(session, f"ai-usage:{user_id}")
                yield session
        except SQLAlchemyError as exc:
            raise AIControlError("ai_ledger_unavailable", "AI 用量账本暂不可用，请稍后再试", status_code=503) from exc

    @staticmethod
    def day_key(now: datetime) -> str:
        # 每天额度按北京时间切换；跨午夜结束的调用仍结算到发起当天。
        return "day:" + now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()

    @staticmethod
    def _bucket(session, user_id: str, key: str, now: datetime):
        bucket = session.get(AIUsageBucketModel, (user_id, key))
        if bucket is None:
            bucket = AIUsageBucketModel(user_id=user_id, scope_key=key,
                used_tokens=0, reserved_tokens=0, uncertain_tokens=0, updated_at=now)
            session.add(bucket)
            session.flush()
        return bucket

    def reserve(self, *, call_id: str, scope: AIExecutionScope, provider: str, model: str,
                tokens: int, ttl_seconds: float, now: datetime | None = None) -> None:
        """先预留再联网。任何一个额度桶不够时，整个事务回滚，不会只扣一半。"""
        now = now or datetime.now(timezone.utc)
        if tokens <= 0:
            raise ValueError("预留 Token 必须大于零")
        day = self.day_key(now)
        with self._transaction(scope.user_id) as session:
            self._expire(session, scope.user_id, now)
            existing = session.get(AIModelCallModel, call_id)
            if existing is not None:
                raise AIControlError("duplicate_model_call", "该模型请求已登记，不能重复发送")
            for key, limit in ((day, settings.ai_daily_token_limit),
                               (scope.run_id, settings.ai_run_token_limit)):
                bucket = self._bucket(session, scope.user_id, key, now)
                committed = bucket.used_tokens + bucket.reserved_tokens + bucket.uncertain_tokens
                if committed + tokens > limit:
                    label = "今日" if key == day else "本次任务"
                    raise AIControlError("token_budget_exceeded",
                        f"{label}的 AI Token 额度不足；请缩小分析范围或稍后再试。待确认用量仍占用额度。",
                        retry_after=60)
                bucket.reserved_tokens += tokens
                bucket.updated_at = now
            session.add(AIModelCallModel(id=call_id, user_id=scope.user_id, run_id=scope.run_id,
                day_key=day, source=scope.source, provider=provider, model=model,
                status="reserved", reserved_tokens=tokens, created_at=now,
                expires_at=now + timedelta(seconds=max(1, ttl_seconds))))

    def mark_started(self, *, user_id: str, call_id: str) -> None:
        """只有写下 in_flight 后才能向供应商发送；崩溃后据此区分是否可能计费。"""
        with self._transaction(user_id) as session:
            row = self._require(session, user_id, call_id)
            if row.status != "reserved":
                raise AIControlError("model_reservation_expired", "模型调用预留已失效，请重新提交")
            row.status = "in_flight"

    def renew(self, *, user_id: str, call_id: str, ttl_seconds: float) -> bool:
        """长流式请求仍活跃时延长预留期限，避免用量查询把它误判为崩溃遗留。"""
        with self._transaction(user_id) as session:
            row = self._require(session, user_id, call_id)
            if row.status != "in_flight":
                return False
            row.expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
            return True

    def settle(self, *, user_id: str, call_id: str, usage: dict | None,
               reason: str | None = None) -> None:
        """重复结算不会重复扣减。未知请求晚收到真实 usage 时，也可以补结算。"""
        now = datetime.now(timezone.utc)
        with self._transaction(user_id) as session:
            row = self._require(session, user_id, call_id)
            if row.status in {"settled", "released"}:
                return
            self._settle(session, row, usage, reason, now)

    def _settle(self, session, row, usage, reason, now):
        if usage is None and row.status == "unknown":
            return
        # reserved 尚未联网，可以退回；in_flight 无 usage 则保守转入待确认。
        target = "settled" if usage is not None else ("released" if row.status == "reserved" else "unknown")
        if usage is not None:
            values = [usage[name] for name in ("input_tokens", "output_tokens", "total_tokens")]
            if any(type(value) is not int or value < 0 for value in values) or values[2] < sum(values[:2]):
                raise ValueError("供应商 Token 用量无效")
        for key in (row.day_key, row.run_id):
            bucket = self._bucket(session, row.user_id, key, now)
            if row.status == "unknown":
                bucket.uncertain_tokens -= row.reserved_tokens
            else:
                bucket.reserved_tokens -= row.reserved_tokens
            if target == "unknown":
                bucket.uncertain_tokens += row.reserved_tokens
            elif usage is not None:
                # 实际用量超过预估也如实记录，不能为了满足预算把账单截断。
                bucket.used_tokens += usage["total_tokens"]
            bucket.updated_at = now
        row.status = target
        row.reason = (reason or "")[:200] or None
        row.settled_at = now
        if usage is not None:
            row.input_tokens, row.output_tokens, row.total_tokens = values

    def reconcile(self, *, user_id: str, call_id: str, usage: dict, note: str):
        """管理员只补结算待确认记录，不能覆盖已结算事实或抢先结束正在运行的调用。"""
        with self._transaction(user_id) as session:
            self._expire(session, user_id, datetime.now(timezone.utc))
            row = self._require(session, user_id, call_id)
            if row.status == "settled" and all(getattr(row, key) == value for key, value in usage.items()):
                return
            if row.status != "unknown":
                raise ValueError("只有待确认用量可以人工对账")
            self._settle(session, row, usage, f"reconciled:{note}", datetime.now(timezone.utc))

    def _expire(self, session, user_id, now):
        """惰性回收崩溃遗留状态。只释放未发出的请求，已发出的保留待确认费用。"""
        rows = list(session.scalars(select(AIModelCallModel).where(
            AIModelCallModel.user_id == user_id,
            AIModelCallModel.status.in_(["reserved", "in_flight"]),
            AIModelCallModel.expires_at <= now)))
        for row in rows:
            self._settle(session, row, None, "reservation_expired", now)

    @staticmethod
    def _require(session, user_id, call_id):
        row = session.get(AIModelCallModel, call_id)
        if row is None or row.user_id != user_id:
            raise LookupError("找不到该用户的模型调用记录")
        return row

    def summary(self, *, user_id: str, now: datetime | None = None) -> dict:
        """供当前用户查看；未知用量显示单独的预留估计，不混入已知消耗。"""
        now = now or datetime.now(timezone.utc)
        day = self.day_key(now)
        with self._transaction(user_id) as session:
            self._expire(session, user_id, now)
            bucket = self._bucket(session, user_id, day, now)
            recent = list(session.scalars(select(AIModelCallModel).where(
                AIModelCallModel.user_id == user_id).order_by(AIModelCallModel.created_at.desc()).limit(30)))
            return {"user_id": user_id, "day": day[4:], "timezone": "Asia/Shanghai",
                "unit": "tokens", "daily_limit": settings.ai_daily_token_limit,
                "run_limit": settings.ai_run_token_limit, "used_tokens": bucket.used_tokens,
                "reserved_tokens": bucket.reserved_tokens, "uncertain_tokens": bucket.uncertain_tokens,
                "remaining_tokens": max(0, settings.ai_daily_token_limit - bucket.used_tokens
                    - bucket.reserved_tokens - bucket.uncertain_tokens),
                "calls": [{"id": row.id, "run_id": row.run_id, "source": row.source,
                    "provider": row.provider, "model": row.model, "status": row.status,
                    "reserved_tokens": row.reserved_tokens, "input_tokens": row.input_tokens,
                    "output_tokens": row.output_tokens, "total_tokens": row.total_tokens,
                    "reason": row.reason, "created_at": row.created_at.isoformat()} for row in recent]}


# 延迟创建，避免 CLI 仅导入模型类时就连接数据库，也便于测试注入临时账本。
_usage_service: AIUsageService | None = None


def get_ai_usage_service() -> AIUsageService:
    global _usage_service
    if _usage_service is None:
        _usage_service = AIUsageService()
    return _usage_service

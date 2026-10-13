"""s14 Cron Scheduler：按时间自动生产后台工作。

这个模块沿用 learn-claude-code s14 的四层结构：

1. Scheduler：独立线程每秒检查 Cron 表达式；
2. Queue：时间命中后只把触发事件放进队列；
3. Queue Processor：从队列取出事件；
4. Consumer：当前项目把事件交给 s13 BackgroundJobService 执行。

调度器不会自动调用 DeepSeek，避免无人操作时产生 API 调用和费用。以后需要真正的自动
Agent 时，只要替换 Consumer，Scheduler 和 Queue 仍可以继续使用。
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
import json
from pathlib import Path
import re
from threading import Event, RLock, Thread
from typing import Any, Callable, Literal
from uuid import uuid4

from app.services.background_jobs import BackgroundJobService, background_job_service


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEDULES_ROOT = PROJECT_ROOT / "data" / "schedules"

SCHEDULE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$")

MAX_SCHEDULES_PER_USER = 50
MAX_PROMPT_CHARS = 500
MAX_QUEUE_SIZE = 200

CronFireSource = Literal["scheduler", "manual"]
CronDeliveryStatus = Literal["queued", "delivered", "failed"]


@dataclass
class CronScheduleRecord:
    """一条 Cron 定义，durable=True 时会写入本地 JSON。"""

    id: str
    user_id: str
    cron: str
    prompt: str
    # job_type 决定时间命中后由 Background Job 执行哪一种受控工作流。
    job_type: str
    recurring: bool
    durable: bool
    created_at: str
    last_fired_at: str | None
    last_fire_marker: str | None
    trigger_count: int
    last_background_job_id: str | None
    last_error: str | None
    # 普通教学 Cron 不需要 payload；领域 Cron 用它携带 portfolio_id 等结构化参数。
    job_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CronFireEvent:
    """Scheduler 写入 Queue 的不可变触发事件。"""

    id: str
    schedule: CronScheduleRecord
    fired_at: str
    source: CronFireSource


@dataclass
class CronDeliveryRecord:
    """Queue Processor 交付一次事件后的观察记录。"""

    event_id: str
    schedule_id: str
    user_id: str
    prompt: str
    job_type: str
    source: CronFireSource
    fired_at: str
    delivered_at: str | None
    status: CronDeliveryStatus
    background_job_id: str | None
    error: str | None


def _parse_cron_field(field: str, lower: int, upper: int) -> set[int]:
    """把一个 Cron 字段解析成允许值集合。

    支持教学源码里的 ``*``、``*/N``、``N``、``N-M``、``N,M``，并额外支持
    ``N-M/S``。不支持 L、W、? 和月份英文名。
    """

    value = str(field or "").strip()
    if not value:
        raise ValueError("字段不能为空")

    allowed: set[int] = set()

    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            raise ValueError(f"列表包含空项：{value}")

        base = part
        step = 1
        if "/" in part:
            pieces = part.split("/")
            if len(pieces) != 2 or not pieces[1].isdigit():
                raise ValueError(f"步长格式非法：{part}")
            base = pieces[0]
            step = int(pieces[1])
            if step <= 0:
                raise ValueError(f"步长必须大于 0：{part}")

        if base == "*":
            start, end = lower, upper
        elif "-" in base:
            range_parts = base.split("-")
            if len(range_parts) != 2 or not all(item.isdigit() for item in range_parts):
                raise ValueError(f"范围格式非法：{part}")
            start, end = (int(range_parts[0]), int(range_parts[1]))
            if start > end:
                raise ValueError(f"范围起点不能大于终点：{part}")
        elif base.isdigit():
            if "/" in part:
                raise ValueError(f"单个数值后不能使用步长：{part}")
            start = end = int(base)
        else:
            raise ValueError(f"字段格式非法：{part}")

        if start < lower or end > upper:
            raise ValueError(f"{part} 超出允许范围 {lower}-{upper}")

        allowed.update(range(start, end + 1, step))

    return allowed


def validate_cron(cron_expression: str) -> str | None:
    """校验标准五段式 Cron；合法返回 None，非法返回可读错误。"""

    fields = str(cron_expression or "").strip().split()
    if len(fields) != 5:
        return f"Cron 必须是 5 段，当前是 {len(fields)} 段。"

    field_specs = (
        ("分钟", 0, 59),
        ("小时", 0, 23),
        ("日期", 1, 31),
        ("月份", 1, 12),
        ("星期", 0, 6),
    )
    for field, (name, lower, upper) in zip(fields, field_specs):
        try:
            _parse_cron_field(field, lower, upper)
        except ValueError as exc:
            return f"{name}字段错误：{exc}"

    return None


def cron_matches(cron_expression: str, moment: datetime) -> bool:
    """判断五段式 Cron 是否匹配指定时间。

    标准 Cron 中，当“日期”和“星期”都受限制时使用 OR：满足任意一个即可。
    """

    if validate_cron(cron_expression):
        return False

    minute, hour, day_of_month, month, day_of_week = cron_expression.split()
    cron_weekday = (moment.weekday() + 1) % 7  # Python 周一=0，Cron 周日=0。

    minute_ok = moment.minute in _parse_cron_field(minute, 0, 59)
    hour_ok = moment.hour in _parse_cron_field(hour, 0, 23)
    month_ok = moment.month in _parse_cron_field(month, 1, 12)
    dom_ok = moment.day in _parse_cron_field(day_of_month, 1, 31)
    dow_ok = cron_weekday in _parse_cron_field(day_of_week, 0, 6)

    if not (minute_ok and hour_ok and month_ok):
        return False

    dom_unconstrained = day_of_month == "*"
    dow_unconstrained = day_of_week == "*"
    if dom_unconstrained and dow_unconstrained:
        return True
    if dom_unconstrained:
        return dow_ok
    if dow_unconstrained:
        return dom_ok
    return dom_ok or dow_ok


class CronSchedulerService:
    """管理 Schedule、独立调度线程、触发队列和交付线程。"""

    def __init__(
        self,
        *,
        root: Path = SCHEDULES_ROOT,
        background_jobs: BackgroundJobService = background_job_service,
        scheduler_poll_seconds: float = 1.0,
        queue_poll_seconds: float = 0.2,
    ) -> None:
        self.root = Path(root)
        self.background_jobs = background_jobs
        self.scheduler_poll_seconds = max(0.01, float(scheduler_poll_seconds))
        self.queue_poll_seconds = max(0.01, float(queue_poll_seconds))

        # Schedule、Queue 和 Delivery 会被 API 请求与两个线程共同访问。
        self._lock = RLock()
        self._schedules: dict[tuple[str, str], CronScheduleRecord] = {}
        self._queue: deque[CronFireEvent] = deque()
        self._deliveries: deque[CronDeliveryRecord] = deque(maxlen=200)
        self._load_errors: list[str] = []
        # 领域日程可订阅同一个时钟，不再为自动简报启动第二套调度线程。
        # 回调只负责查策略、建单；联网采集和模型调用仍在 Worker 中完成。
        self._tick_callback: Callable[[datetime], Any] | None = None
        self._callback_minute: str | None = None

        self._stop_event = Event()
        self._scheduler_thread: Thread | None = None
        self._queue_thread: Thread | None = None
        self._load_durable_schedules()

    def start(self) -> None:
        """启动 Scheduler 和 Queue Processor；重复调用不会创建重复线程。"""

        with self._lock:
            if self.is_running:
                return

            self._stop_event.clear()
            self._scheduler_thread = Thread(
                target=self._scheduler_loop,
                name="cron-scheduler",
                daemon=True,
            )
            self._queue_thread = Thread(
                target=self._queue_processor_loop,
                name="cron-queue-processor",
                daemon=True,
            )
            self._scheduler_thread.start()
            self._queue_thread.start()

    def stop(self, *, wait: bool = True, timeout: float = 2.0) -> None:
        """停止两个守护线程；持久化 Schedule 文件不会删除。"""

        self._stop_event.set()
        with self._lock:
            threads = [self._scheduler_thread, self._queue_thread]

        if wait:
            for thread in threads:
                if thread is not None:
                    thread.join(timeout=max(0.0, timeout))

    @property
    def is_running(self) -> bool:
        return bool(
            self._scheduler_thread
            and self._scheduler_thread.is_alive()
            and self._queue_thread
            and self._queue_thread.is_alive()
        )

    def create_schedule(
        self,
        *,
        user_id: str,
        cron: str,
        prompt: str,
        job_type: str = "demo_steps",
        recurring: bool = True,
        durable: bool = True,
        schedule_id: str | None = None,
        job_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """注册 Cron 定义；durable=True 时立即原子写入 JSON。"""

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_cron = self._normalize_cron(cron)
        normalized_prompt = self._normalize_prompt(prompt)
        normalized_job_type = self._normalize_job_type(job_type)
        normalized_job_payload = self._normalize_job_payload(
            normalized_job_type,
            job_payload,
        )
        normalized_schedule_id = (
            self._normalize_schedule_id(schedule_id)
            if schedule_id
            else f"cron-{uuid4().hex[:10]}"
        )
        key = (normalized_user_id, normalized_schedule_id)

        with self._lock:
            user_schedule_count = sum(
                1 for stored_user_id, _ in self._schedules if stored_user_id == normalized_user_id
            )
            if user_schedule_count >= MAX_SCHEDULES_PER_USER:
                raise ValueError(f"每个用户最多保存 {MAX_SCHEDULES_PER_USER} 个 Cron。")
            if key in self._schedules:
                raise ValueError(f"Cron ID 已存在：{normalized_schedule_id}")

            record = CronScheduleRecord(
                id=normalized_schedule_id,
                user_id=normalized_user_id,
                cron=normalized_cron,
                prompt=normalized_prompt,
                job_type=normalized_job_type,
                recurring=bool(recurring),
                durable=bool(durable),
                created_at=self._now(),
                last_fired_at=None,
                last_fire_marker=None,
                trigger_count=0,
                last_background_job_id=None,
                last_error=None,
                job_payload=normalized_job_payload,
            )
            self._schedules[key] = record
            if record.durable:
                self._write_record(record)

            return {
                "ok": True,
                "schedule": self._record_to_public_dict(record),
                "message": (
                    "Cron 已注册并持久化。"
                    if record.durable
                    else "Cron 已注册为当前进程内的 session-only 任务。"
                ),
            }

    def list_schedules(
        self,
        *,
        user_id: str,
        max_results: int = 50,
    ) -> dict[str, Any]:
        """列出当前用户的 Cron 定义和最近交付记录。"""

        normalized_user_id = self._normalize_user_id(user_id)
        safe_limit = max(1, min(int(max_results), 100))

        with self._lock:
            records = [
                record
                for (stored_user_id, _), record in self._schedules.items()
                if stored_user_id == normalized_user_id
            ]
            records.sort(key=lambda item: item.created_at, reverse=True)
            schedules = [
                self._record_to_public_dict(record)
                for record in records[:safe_limit]
            ]
            deliveries = self._recent_deliveries_locked(
                user_id=normalized_user_id,
                max_items=min(safe_limit, 20),
            )
            queue_depth = sum(
                1 for event in self._queue if event.schedule.user_id == normalized_user_id
            )

        return {
            "ok": True,
            "user_id": normalized_user_id,
            "count": len(schedules),
            "schedules": schedules,
            "queue_depth": queue_depth,
            "recent_deliveries": deliveries,
            "scheduler_running": self.is_running,
        }

    def cancel_schedule(self, *, user_id: str, schedule_id: str) -> dict[str, Any]:
        """取消 Cron 定义；已进入 Queue 的历史触发事件不会回滚。"""

        key = self._normalize_key(user_id=user_id, schedule_id=schedule_id)
        with self._lock:
            record = self._schedules.pop(key, None)
            if record is None:
                raise KeyError(f"找不到 Cron：{key[1]}")

            if record.durable:
                self._delete_record(record)

            return {
                "ok": True,
                "schedule": self._record_to_public_dict(record),
                "message": "Cron 已取消；未来不会再产生新的触发事件。",
            }

    def trigger_now(self, *, user_id: str, schedule_id: str) -> dict[str, Any]:
        """手动把 Schedule 放入 Queue，便于不用等待整分钟进行教学测试。"""

        key = self._normalize_key(user_id=user_id, schedule_id=schedule_id)
        with self._lock:
            record = self._require_schedule(key)
            event = self._enqueue_locked(record=record, source="manual")
            # 手动触发也算一次真实触发，但不写 last_fire_marker，
            # 因此不会妨碍同一分钟内真正的 Cron 时间命中。
            updated = replace(
                record,
                last_fired_at=event.fired_at,
                trigger_count=record.trigger_count + 1,
                last_error=None,
            )
            self._schedules[key] = updated
            if updated.durable:
                self._write_record(updated)
            user_queue_depth = sum(
                1 for queued in self._queue if queued.schedule.user_id == key[0]
            )

            return {
                "ok": True,
                "event": self._event_to_public_dict(event),
                "queue_depth": user_queue_depth,
                "message": "已手动触发并进入 Queue，Queue Processor 会自动交付。",
            }

    def set_tick_callback(self, callback: Callable[[datetime], Any] | None) -> None:
        self._tick_callback = callback
        self._callback_minute = None

    def tick(self, *, moment: datetime | None = None) -> list[dict[str, Any]]:
        """执行一次时间检查；公开此方法是为了确定性测试 Cron 匹配。"""

        current = moment or datetime.now().astimezone()
        minute_marker = current.strftime("%Y-%m-%d %H:%M")
        fired_events: list[CronFireEvent] = []

        with self._lock:
            for key, record in list(self._schedules.items()):
                try:
                    if not cron_matches(record.cron, current):
                        continue
                    # Scheduler 每秒轮询，但同一 Schedule 在同一分钟只能触发一次。
                    if record.last_fire_marker == minute_marker:
                        continue

                    event = self._enqueue_locked(record=record, source="scheduler")
                    fired_events.append(event)
                    updated = replace(
                        record,
                        last_fired_at=event.fired_at,
                        last_fire_marker=minute_marker,
                        trigger_count=record.trigger_count + 1,
                        last_error=None,
                    )

                    if record.recurring:
                        self._schedules[key] = updated
                        if updated.durable:
                            self._write_record(updated)
                    else:
                        # 一次性任务时间命中后立即删除定义，但事件已经安全留在 Queue 中。
                        self._schedules.pop(key, None)
                        if record.durable:
                            self._delete_record(record)
                except Exception as exc:
                    # 单个坏 Schedule 不能杀掉整个调度线程。
                    failed = replace(record, last_error=str(exc))
                    self._schedules[key] = failed
                    if failed.durable:
                        self._write_record(failed)

        # 不持有 Cron 的锁执行数据库操作；分钟节流只是减少轮询，SQL 幂等才负责防重。
        if self._tick_callback and self._callback_minute != minute_marker:
            self._tick_callback(current)
            self._callback_minute = minute_marker
        return [self._event_to_public_dict(event) for event in fired_events]

    def process_next_event(self) -> dict[str, Any] | None:
        """Queue Processor 的一次消费动作，并把事件交给 BackgroundJobService。"""

        with self._lock:
            if not self._queue:
                return None
            event = self._queue.popleft()

        delivery = CronDeliveryRecord(
            event_id=event.id,
            schedule_id=event.schedule.id,
            user_id=event.schedule.user_id,
            prompt=event.schedule.prompt,
            job_type=event.schedule.job_type,
            source=event.source,
            fired_at=event.fired_at,
            delivered_at=None,
            status="queued",
            background_job_id=None,
            error=None,
        )

        try:
            # 当前 Consumer 使用 s13 的受控后台 Job。这里没有调用 LLM，也没有执行 shell。
            background_job_id = (
                f"cron-{event.schedule.id[:38]}-{uuid4().hex[:8]}"
            )
            result = self.background_jobs.start_job(
                user_id=event.schedule.user_id,
                job_id=background_job_id,
                title=f"[Scheduled] {event.schedule.prompt}",
                job_type=event.schedule.job_type,
                prompt=event.schedule.prompt,
                total_steps=4,
                step_delay_seconds=0.2,
                portfolio_id=event.schedule.job_payload.get("portfolio_id"),
                portfolio_months=int(
                    event.schedule.job_payload.get("portfolio_months") or 12
                ),
            )
            delivery.status = "delivered"
            delivery.background_job_id = str(result["job"]["id"])
            delivery.delivered_at = self._now()
            self._update_last_delivery(
                event=event,
                background_job_id=delivery.background_job_id,
                error=None,
            )
        except Exception as exc:
            delivery.status = "failed"
            delivery.error = str(exc)
            delivery.delivered_at = self._now()
            self._update_last_delivery(event=event, background_job_id=None, error=str(exc))

        with self._lock:
            self._deliveries.appendleft(delivery)
            return self._delivery_to_public_dict(delivery)

    def recent_index(
        self,
        *,
        user_id: str,
        max_items: int = 20,
    ) -> list[dict[str, Any]]:
        """返回给 `/agent/chat` 的当前 Cron 结构化索引。"""

        return list(
            self.list_schedules(
                user_id=user_id,
                max_results=max(1, min(max_items, 50)),
            )["schedules"]
        )

    def recent_deliveries(
        self,
        *,
        user_id: str,
        max_items: int = 20,
    ) -> list[dict[str, Any]]:
        """返回当前用户最近从 Queue 交付出去的事件。"""

        normalized_user_id = self._normalize_user_id(user_id)
        with self._lock:
            return self._recent_deliveries_locked(
                user_id=normalized_user_id,
                max_items=max(1, min(max_items, 50)),
            )

    def index_for_prompt(self, *, user_id: str, max_items: int = 10) -> str:
        """生成 Prompt 使用的轻量 Schedule 与交付状态。"""

        result = self.list_schedules(user_id=user_id, max_results=max_items)
        schedules = result["schedules"]
        deliveries = result["recent_deliveries"][:5]

        if not schedules and not deliveries:
            return "当前用户暂无 Cron Schedule 或触发记录。"

        lines = [
            f"Scheduler running={result['scheduler_running']}, queue_depth={result['queue_depth']}."
        ]
        for schedule in schedules:
            lines.append(
                "- "
                f"[schedule] {schedule['id']}: cron={schedule['cron']!r}, "
                f"recurring={schedule['recurring']}, durable={schedule['durable']}, "
                f"prompt={schedule['prompt']!r}"
            )
        for delivery in deliveries:
            lines.append(
                "- "
                f"[delivery:{delivery['status']}] {delivery['schedule_id']} -> "
                f"background_job_id={delivery['background_job_id']}"
            )
        return "\n".join(lines)

    def _scheduler_loop(self) -> None:
        """第一条守护线程：只检查时间并生产 Queue 事件。"""

        while not self._stop_event.wait(self.scheduler_poll_seconds):
            try:
                self.tick()
            except Exception as exc:
                with self._lock:
                    self._load_errors.append(f"scheduler loop: {exc}")

    def _queue_processor_loop(self) -> None:
        """第二条守护线程：消费 Queue，不负责判断 Cron 时间。"""

        while not self._stop_event.wait(self.queue_poll_seconds):
            try:
                self.process_next_event()
            except Exception as exc:
                with self._lock:
                    self._load_errors.append(f"queue processor: {exc}")

    def _enqueue_locked(
        self,
        *,
        record: CronScheduleRecord,
        source: CronFireSource,
    ) -> CronFireEvent:
        """在持有锁时创建事件并写入有界 Queue。"""

        if len(self._queue) >= MAX_QUEUE_SIZE:
            raise ValueError(f"Cron Queue 已满，最多保留 {MAX_QUEUE_SIZE} 个事件。")

        event = CronFireEvent(
            id=f"event-{uuid4().hex[:12]}",
            # Queue 保存快照，避免一次性 Schedule 删除后事件失去内容。
            schedule=replace(record),
            fired_at=self._now(),
            source=source,
        )
        self._queue.append(event)
        return event

    def _update_last_delivery(
        self,
        *,
        event: CronFireEvent,
        background_job_id: str | None,
        error: str | None,
    ) -> None:
        """把最近交付结果回写到仍然存在的 recurring Schedule。"""

        key = (event.schedule.user_id, event.schedule.id)
        with self._lock:
            current = self._schedules.get(key)
            if current is None:
                return
            updated = replace(
                current,
                last_background_job_id=background_job_id,
                last_error=error,
            )
            self._schedules[key] = updated
            if updated.durable:
                self._write_record(updated)

    def _load_durable_schedules(self) -> None:
        """启动时加载 data/schedules 下合法的 durable JSON，坏文件会被跳过。"""

        if not self.root.is_dir():
            return

        for path in sorted(self.root.glob("*/*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                record = self._record_from_dict(payload)
                if not record.durable:
                    continue
                expected_path = self.root / record.user_id / f"{record.id}.json"
                if path.resolve() != expected_path.resolve():
                    raise ValueError("Schedule 文件路径与 user_id/id 不匹配")
                self._schedules[(record.user_id, record.id)] = record
            except Exception as exc:
                self._load_errors.append(f"{self._display_path(path)}: {exc}")

    def _write_record(self, record: CronScheduleRecord) -> None:
        """原子写入 durable Schedule，避免中途崩溃留下半截 JSON。"""

        folder = self.root / record.user_id
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{record.id}.json"
        temporary = folder / f".{record.id}.{uuid4().hex[:8]}.tmp"
        temporary.write_text(
            json.dumps({"version": 1, **asdict(record)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(target)

    def _delete_record(self, record: CronScheduleRecord) -> None:
        path = self.root / record.user_id / f"{record.id}.json"
        path.unlink(missing_ok=True)

    def _record_from_dict(self, data: dict[str, Any]) -> CronScheduleRecord:
        cron = self._normalize_cron(str(data.get("cron") or ""))
        # 旧版 JSON 没有 job_type/job_payload；缺失时继续按 demo_steps 读取。
        job_type = self._normalize_job_type(
            str(data.get("job_type") or "demo_steps")
        )
        return CronScheduleRecord(
            id=self._normalize_schedule_id(str(data.get("id") or "")),
            user_id=self._normalize_user_id(str(data.get("user_id") or "")),
            cron=cron,
            prompt=self._normalize_prompt(str(data.get("prompt") or "")),
            job_type=job_type,
            recurring=bool(data.get("recurring", True)),
            durable=bool(data.get("durable", True)),
            created_at=str(data.get("created_at") or self._now()),
            last_fired_at=(
                str(data["last_fired_at"]) if data.get("last_fired_at") else None
            ),
            last_fire_marker=(
                str(data["last_fire_marker"]) if data.get("last_fire_marker") else None
            ),
            trigger_count=max(0, int(data.get("trigger_count", 0))),
            last_background_job_id=(
                str(data["last_background_job_id"])
                if data.get("last_background_job_id")
                else None
            ),
            last_error=str(data["last_error"]) if data.get("last_error") else None,
            job_payload=self._normalize_job_payload(
                job_type,
                data.get("job_payload"),
            ),
        )

    def _record_to_public_dict(self, record: CronScheduleRecord) -> dict[str, Any]:
        data = asdict(record)
        data["path"] = (
            self._display_path(self.root / record.user_id / f"{record.id}.json")
            if record.durable
            else None
        )
        return data

    def _event_to_public_dict(self, event: CronFireEvent) -> dict[str, Any]:
        return {
            "id": event.id,
            "schedule_id": event.schedule.id,
            "user_id": event.schedule.user_id,
            "prompt": event.schedule.prompt,
            "job_type": event.schedule.job_type,
            "job_payload": dict(event.schedule.job_payload),
            "fired_at": event.fired_at,
            "source": event.source,
        }

    def _delivery_to_public_dict(self, delivery: CronDeliveryRecord) -> dict[str, Any]:
        return asdict(delivery)

    def _recent_deliveries_locked(
        self,
        *,
        user_id: str,
        max_items: int,
    ) -> list[dict[str, Any]]:
        selected = [
            self._delivery_to_public_dict(delivery)
            for delivery in self._deliveries
            if delivery.user_id == user_id
        ]
        return selected[:max_items]

    def _require_schedule(self, key: tuple[str, str]) -> CronScheduleRecord:
        record = self._schedules.get(key)
        if record is None:
            raise KeyError(f"找不到 Cron：{key[1]}")
        return record

    def _normalize_key(self, *, user_id: str, schedule_id: str) -> tuple[str, str]:
        return (
            self._normalize_user_id(user_id),
            self._normalize_schedule_id(schedule_id),
        )

    def _normalize_user_id(self, user_id: str) -> str:
        value = str(user_id or "").strip()
        if not USER_ID_PATTERN.fullmatch(value):
            raise ValueError("user_id 只能包含字母、数字、下划线和连字符，最长 50 位。")
        return value

    def _normalize_schedule_id(self, schedule_id: str | None) -> str:
        value = str(schedule_id or "").strip()
        if not SCHEDULE_ID_PATTERN.fullmatch(value):
            raise ValueError("schedule_id 只能包含字母、数字、下划线和连字符，最长 64 位。")
        return value

    def _normalize_cron(self, cron: str) -> str:
        value = " ".join(str(cron or "").strip().split())
        error = validate_cron(value)
        if error:
            raise ValueError(error)
        return value

    def _normalize_prompt(self, prompt: str) -> str:
        value = str(prompt or "").strip()
        if not value:
            raise ValueError("Cron prompt 不能为空。")
        if len(value) > MAX_PROMPT_CHARS:
            raise ValueError(f"Cron prompt 最多 {MAX_PROMPT_CHARS} 个字符。")
        return value

    def _normalize_job_type(self, job_type: str) -> str:
        """Cron 只能投递 Background Job 已登记的公开工作流。"""

        value = str(job_type or "").strip().lower()
        allowed = {
            "demo_steps",
            "agent_prompt",
            "fund_nav_sync",
            "market_snapshot_sync",
            "market_event_sync",
            "market_intelligence_pipeline",
            "portfolio_monitor",
        }
        if value not in allowed:
            raise ValueError(f"Cron job_type 只能是：{', '.join(sorted(allowed))}")
        return value

    def _normalize_job_payload(
        self,
        job_type: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """只允许领域任务声明过的字段进入持久化 Schedule。"""

        value = payload or {}
        if not isinstance(value, dict):
            raise ValueError("job_payload 必须是 JSON 对象。")
        if job_type != "portfolio_monitor":
            if value:
                raise ValueError(f"{job_type} 不接受 job_payload。")
            return {}

        portfolio_id = str(value.get("portfolio_id") or "").strip()
        if not re.fullmatch(r"^portfolio_[a-f0-9]{16}$", portfolio_id):
            raise ValueError("portfolio_monitor 缺少合法的 portfolio_id。")
        months = int(value.get("portfolio_months") or 12)
        if months not in {6, 12, 36}:
            raise ValueError("portfolio_months 只能是 6、12 或 36。")
        return {
            "portfolio_id": portfolio_id,
            "portfolio_months": months,
        }

    def _display_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            return str(path.resolve())

    def _now(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")


cron_scheduler_service = CronSchedulerService()

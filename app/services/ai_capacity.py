"""共享请求限流和模型执行槽位。

Redis 的 Lua 脚本把“清理过期项、检查上限、登记请求”作为一个原子操作。
memory 只用于明确的单进程本地模式，不会在 Redis 故障时偷偷启用。
这里不保存提示词，也不参与 Token 结算。
"""

from collections import defaultdict, deque
from functools import lru_cache
from hashlib import sha256
import math
from threading import RLock
from time import monotonic
from uuid import uuid4

from app.config import settings
from app.services.ai_governance_context import AIControlError


def capacity_limits() -> tuple[int, int, int, int, float, float]:
    values = (settings.ai_global_concurrency, settings.ai_user_concurrency,
              settings.ai_global_queue_size, settings.ai_user_queue_size,
              settings.ai_slot_lease_seconds, settings.ai_queue_wait_seconds)
    if (not all(math.isfinite(v) for v in values)
            or any(v <= 0 for v in (values[0], values[1], values[4]))
            or any(v < 0 for v in (values[2], values[3], values[5]))):
        raise AIControlError("invalid_ai_limits", "AI 并发配置无效，请联系管理员", status_code=503)
    return values


class MemoryAICapacity:
    """同一进程内共享；多个 API 进程必须选择 Redis 后端。"""
    backend_name = "memory"

    def __init__(self):
        self.lock = RLock()
        self.active: dict[str, tuple[str, float]] = {}
        self.waiting: dict[str, tuple[str, float]] = {}
        self.rates: dict[str, deque] = defaultdict(deque)

    def consume_rate(self, key, limit, window_seconds=60):
        if limit <= 0 or window_seconds <= 0:
            raise AIControlError("invalid_ai_rate", "AI 频率限制配置无效", status_code=503)
        now = monotonic()
        with self.lock:
            # 删除长期不用的用户键，避免匿名来源不断增加导致内存无界增长。
            for old_key in list(self.rates):
                if not self.rates[old_key] or self.rates[old_key][-1] <= now - window_seconds:
                    del self.rates[old_key]
            events = self.rates[key]
            while events and events[0] <= now - window_seconds:
                events.popleft()
            if len(events) >= limit:
                return False, 0, max(1, math.ceil(events[0] + window_seconds - now))
            events.append(now)
            return True, limit - len(events), 0

    def _purge(self):
        now = monotonic()
        for entries in (self.active, self.waiting):
            for ticket, (_, expires) in list(entries.items()):
                if expires <= now:
                    del entries[ticket]

    def acquire(self, user_id, ticket):
        global_max, user_max, global_queue, user_queue, lease, wait = capacity_limits()
        with self.lock:
            self._purge()
            if ticket in self.active:
                return "acquired"
            if len(self.active) < global_max and sum(u == user_id for u, _ in self.active.values()) < user_max:
                self.waiting.pop(ticket, None)
                self.active[ticket] = (user_id, monotonic() + lease)
                return "acquired"
            if ticket not in self.waiting:
                if len(self.waiting) >= global_queue or sum(u == user_id for u, _ in self.waiting.values()) >= user_queue:
                    return "full"
                self.waiting[ticket] = (user_id, monotonic() + wait + 5)
            return "waiting"

    def renew(self, user_id, ticket):
        with self.lock:
            self._purge()
            if ticket not in self.active:
                return False
            self.active[ticket] = (user_id, monotonic() + settings.ai_slot_lease_seconds)
            return True

    def release(self, user_id, ticket):
        with self.lock:
            # ticket 是每次调用生成的随机 ID，旧请求不会释放新请求的槽位。
            self.active.pop(ticket, None)
            self.waiting.pop(ticket, None)

    def snapshot(self, user_id):
        with self.lock:
            self._purge()
            return {"running": len(self.active), "waiting": len(self.waiting),
                "user_running": sum(u == user_id for u, _ in self.active.values()),
                "user_waiting": sum(u == user_id for u, _ in self.waiting.values())}


# 时间取 Redis TIME，而不是各 API 机器的系统时钟，避免多机时钟漂移影响窗口。
_TIME = "local t=redis.call('TIME'); local now=t[1]*1000+math.floor(t[2]/1000); "
_RATE = _TIME + """
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now-ARGV[1])
local count=redis.call('ZCARD',KEYS[1])
if count>=tonumber(ARGV[2]) then
  local first=redis.call('ZRANGE',KEYS[1],0,0,'WITHSCORES')
  return {0,0,math.max(1,math.ceil((tonumber(first[2])+ARGV[1]-now)/1000))}
end
redis.call('ZADD',KEYS[1],now,ARGV[3]); redis.call('PEXPIRE',KEYS[1],ARGV[1])
return {1,tonumber(ARGV[2])-count-1,0}
"""
_ACQUIRE = _TIME + """
for i=1,4 do redis.call('ZREMRANGEBYSCORE',KEYS[i],'-inf',now) end
if redis.call('ZSCORE',KEYS[1],ARGV[1]) then return 1 end
if redis.call('ZCARD',KEYS[1])<tonumber(ARGV[2]) and redis.call('ZCARD',KEYS[2])<tonumber(ARGV[3]) then
  for i=1,2 do
    redis.call('ZADD',KEYS[i],now+ARGV[6],ARGV[1]); redis.call('PEXPIRE',KEYS[i],ARGV[6]*2)
  end
  redis.call('ZREM',KEYS[3],ARGV[1]); redis.call('ZREM',KEYS[4],ARGV[1]); return 1
end
if not redis.call('ZSCORE',KEYS[3],ARGV[1]) then
  if redis.call('ZCARD',KEYS[3])>=tonumber(ARGV[4]) or redis.call('ZCARD',KEYS[4])>=tonumber(ARGV[5]) then return -1 end
  for i=3,4 do
    redis.call('ZADD',KEYS[i],now+ARGV[7],ARGV[1]); redis.call('PEXPIRE',KEYS[i],ARGV[7]*2)
  end
end
return 0
"""
_RENEW = _TIME + """
local expiry=redis.call('ZSCORE',KEYS[1],ARGV[1])
if not expiry or tonumber(expiry)<=now then return 0 end
for i=1,2 do
  redis.call('ZADD',KEYS[i],now+ARGV[2],ARGV[1]); redis.call('PEXPIRE',KEYS[i],ARGV[2]*2)
end
return 1
"""
_RELEASE = "for i=1,4 do redis.call('ZREM',KEYS[i],ARGV[1]) end return 1"
_SNAPSHOT = _TIME + """
local result={}
for i=1,4 do
  redis.call('ZREMRANGEBYSCORE',KEYS[i],'-inf',now); result[i]=redis.call('ZCARD',KEYS[i])
end
return result
"""


class RedisAICapacity:
    backend_name = "redis"

    def __init__(self, url, prefix):
        from redis import Redis
        self.client = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1,
            decode_responses=True)
        self.prefix = prefix

    def _keys(self, user_id):
        owner = sha256(user_id.encode()).hexdigest()
        return [f"{self.prefix}:active", f"{self.prefix}:user:{owner}:active",
                f"{self.prefix}:waiting", f"{self.prefix}:user:{owner}:waiting"]

    def _eval(self, script, keys, *args):
        from redis.exceptions import RedisError
        try:
            return self.client.eval(script, len(keys), *keys, *args)
        except RedisError as exc:
            # 不回退内存：否则多副本各自放行，会绕过全局并发和频率限制。
            raise AIControlError("ai_control_unavailable", "AI 调用控制服务暂不可用，请稍后再试",
                status_code=503) from exc

    def consume_rate(self, key, limit, window_seconds=60):
        if limit <= 0 or window_seconds <= 0:
            raise AIControlError("invalid_ai_rate", "AI 频率限制配置无效", status_code=503)
        name = f"{self.prefix}:rate:{sha256(key.encode()).hexdigest()}"
        allowed, remaining, retry = self._eval(_RATE, [name], int(window_seconds * 1000), limit, uuid4().hex)
        return bool(allowed), remaining, retry

    def acquire(self, user_id, ticket):
        global_max, user_max, global_queue, user_queue, lease, wait = capacity_limits()
        status = self._eval(_ACQUIRE, self._keys(user_id), ticket, global_max, user_max,
            global_queue, user_queue, int(lease * 1000), int((wait + 5) * 1000))
        return {1: "acquired", 0: "waiting", -1: "full"}[status]

    def renew(self, user_id, ticket):
        return bool(self._eval(_RENEW, self._keys(user_id)[:2], ticket, int(settings.ai_slot_lease_seconds * 1000)))

    def release(self, user_id, ticket):
        self._eval(_RELEASE, self._keys(user_id), ticket)

    def snapshot(self, user_id):
        running, user_running, waiting, user_waiting = self._eval(_SNAPSHOT, self._keys(user_id))
        return dict(running=running, user_running=user_running, waiting=waiting, user_waiting=user_waiting)


@lru_cache(maxsize=8)
def _backend(name, url, prefix):
    if name == "memory":
        return MemoryAICapacity()
    if name == "redis":
        return RedisAICapacity(url, prefix)
    raise AIControlError("invalid_ai_backend", "AI_CONTROL_BACKEND 只能设置 auto、memory 或 redis", status_code=503)


def get_ai_capacity():
    name = settings.ai_control_backend
    if name == "auto":
        name = "redis" if settings.job_execution_mode == "celery" else "memory"
    return _backend(name, settings.ai_redis_url, settings.ai_redis_prefix)

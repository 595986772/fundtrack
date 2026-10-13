"""调用治理测试：真实 SQL 事务 + 模拟供应商，不消耗真实模型余额。

设置 TEST_AI_REDIS_URL 后，同一份并发断言还会跑在真实 Redis 上；每次使用随机
前缀并只清理自己的键，不会 FLUSHDB 或修改其他项目的数据。
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
import multiprocessing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from pydantic import BaseModel

from app.config import settings
from app.database import Database
from app.services.ai_capacity import MemoryAICapacity, RedisAICapacity
from app.services.ai_governance_context import AIControlError, AIExecutionScope, ai_execution_scope, current_ai_scope
from app.services.ai_usage import AIUsageService
from app.services.governed_model import GovernedChatOpenAI


def redis_process_rate(args):
    """spawn 子进程各自建连接，证明共享来自 Redis 而不是 Python 全局对象。"""
    url, prefix = args
    capacity = RedisAICapacity(url, prefix)
    try:
        return capacity.consume_rate("cross-process", 1)[0]
    finally:
        capacity.client.close()


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(TemporaryDirectory())
        self.db = Database(f"sqlite:///{Path(directory) / 'usage.db'}")
        self.stack.callback(self.db.engine.dispose)
        self.ledger = AIUsageService(self.db)
        self.stack.enter_context(patch.object(settings, "ai_daily_token_limit", 1000))
        self.stack.enter_context(patch.object(settings, "ai_run_token_limit", 500))
        self.scope = AIExecutionScope("alice", "run:1", "test")

    def reserve(self, call_id="a", tokens=100, **kwargs):
        self.ledger.reserve(call_id=call_id, scope=kwargs.pop("scope", self.scope),
            provider="fake", model="fake", tokens=tokens, ttl_seconds=10, **kwargs)

    def test_reserve_and_idempotent_settlement(self):
        self.reserve()
        self.assertEqual(self.ledger.summary(user_id="alice")["reserved_tokens"], 100)
        self.ledger.mark_started(user_id="alice", call_id="a")
        for _ in range(2):
            self.ledger.settle(user_id="alice", call_id="a",
                usage={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30})
        result = self.ledger.summary(user_id="alice")
        self.assertEqual((result["used_tokens"], result["reserved_tokens"], result["remaining_tokens"]), (30, 0, 970))

    def test_run_budget_survives_service_restart(self):
        self.reserve(tokens=400)
        restarted = AIUsageService(self.db)
        with self.assertRaises(AIControlError):
            restarted.reserve(call_id="b", scope=self.scope, provider="f", model="f", tokens=101, ttl_seconds=10)
        self.assertEqual(len(restarted.summary(user_id="alice")["calls"]), 1)

    def test_daily_budget_shared_between_runs_and_users_are_isolated(self):
        with patch.object(settings, "ai_run_token_limit", 1000):
            self.reserve(tokens=600)
            self.reserve("b", tokens=400, scope=AIExecutionScope("alice", "run:2", "test"))
            with self.assertRaises(AIControlError):
                self.reserve("c", tokens=1, scope=AIExecutionScope("alice", "run:3", "test"))
            self.reserve("d", tokens=500, scope=AIExecutionScope("bob", "run:1", "test"))
        self.assertEqual(len(self.ledger.summary(user_id="bob")["calls"]), 1)

    def test_parallel_reservations_do_not_overspend(self):
        def reserve(i):
            try:
                self.reserve(str(i), tokens=100)
                return True
            except AIControlError:
                return False
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(reserve, range(12)))
        self.assertEqual(sum(results), 5)
        self.assertEqual(self.ledger.summary(user_id="alice")["reserved_tokens"], 500)

    def test_cancel_before_network_releases_reservation(self):
        self.reserve()
        self.ledger.settle(user_id="alice", call_id="a", usage=None, reason="cancelled_before_send")
        result = self.ledger.summary(user_id="alice")
        self.assertEqual(result["calls"][0]["status"], "released")
        self.assertEqual(result["remaining_tokens"], 1000)

    def test_timeout_usage_is_unknown_and_late_settlement_is_possible(self):
        self.reserve()
        self.ledger.mark_started(user_id="alice", call_id="a")
        self.ledger.settle(user_id="alice", call_id="a", usage=None, reason="timeout")
        result = self.ledger.summary(user_id="alice")
        self.assertEqual((result["uncertain_tokens"], result["used_tokens"], result["remaining_tokens"]), (100, 0, 900))
        self.assertIsNone(result["calls"][0]["total_tokens"])
        self.ledger.settle(user_id="alice", call_id="a",
            usage={"input_tokens": 40, "output_tokens": 10, "total_tokens": 50})
        self.assertEqual(self.ledger.summary(user_id="alice")["uncertain_tokens"], 0)

    def test_crash_expiry_distinguishes_sent_and_unsent(self):
        now = datetime.now(timezone.utc)
        self.reserve("unsent", now=now)
        self.reserve("sent", now=now)
        self.ledger.mark_started(user_id="alice", call_id="sent")
        result = self.ledger.summary(user_id="alice", now=now + timedelta(seconds=20))
        states = {item["id"]: item["status"] for item in result["calls"]}
        self.assertEqual(states, {"unsent": "released", "sent": "unknown"})
        self.assertEqual(result["uncertain_tokens"], 100)

    def test_active_stream_renews_reservation_without_reviving_settled_call(self):
        now = datetime.now(timezone.utc)
        self.reserve(now=now)
        self.ledger.mark_started(user_id="alice", call_id="a")
        self.assertTrue(self.ledger.renew(user_id="alice", call_id="a", ttl_seconds=300))
        result = self.ledger.summary(user_id="alice", now=now + timedelta(seconds=20))
        self.assertEqual(result["calls"][0]["status"], "in_flight")
        self.assertEqual(result["reserved_tokens"], 100)
        self.ledger.settle(user_id="alice", call_id="a",
            usage={"input_tokens": 4, "output_tokens": 1, "total_tokens": 5})
        self.assertFalse(self.ledger.renew(user_id="alice", call_id="a", ttl_seconds=300))

    def test_cross_midnight_settles_to_original_day(self):
        now = datetime(2026, 9, 14, 15, 59, tzinfo=timezone.utc)
        self.reserve(now=now)
        self.ledger.mark_started(user_id="alice", call_id="a")
        self.ledger.settle(user_id="alice", call_id="a", usage={"input_tokens": 9, "output_tokens": 1, "total_tokens": 10})
        self.assertEqual(self.ledger.summary(user_id="alice", now=now)["used_tokens"], 10)
        self.assertEqual(self.ledger.summary(user_id="alice", now=now + timedelta(minutes=2))["used_tokens"], 0)

    def test_duplicate_call_id_does_not_resend_or_double_reserve(self):
        self.reserve()
        with self.assertRaises(AIControlError):
            self.reserve()
        self.assertEqual(self.ledger.summary(user_id="alice")["reserved_tokens"], 100)

    def test_actual_usage_above_estimate_is_not_clipped(self):
        self.reserve(tokens=10)
        self.ledger.mark_started(user_id="alice", call_id="a")
        self.ledger.settle(user_id="alice", call_id="a", usage={"input_tokens": 40, "output_tokens": 10, "total_tokens": 50})
        self.assertEqual(self.ledger.summary(user_id="alice")["used_tokens"], 50)

    def test_reconcile_cannot_rewrite_settled_or_other_user_call(self):
        self.reserve()
        usage = {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5}
        with self.assertRaises(LookupError):
            self.ledger.reconcile(user_id="bob", call_id="a", usage=usage, note="invoice")
        with self.assertRaises(ValueError):
            self.ledger.reconcile(user_id="alice", call_id="a", usage=usage, note="invoice")
        self.ledger.mark_started(user_id="alice", call_id="a")
        self.ledger.settle(user_id="alice", call_id="a", usage=None)
        self.ledger.reconcile(user_id="alice", call_id="a", usage=usage, note="invoice")
        self.ledger.reconcile(user_id="alice", call_id="a", usage=usage, note="same invoice")
        with self.assertRaises(ValueError):
            self.ledger.reconcile(user_id="alice", call_id="a", usage={**usage, "total_tokens": 10}, note="changed")


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {"ai_global_concurrency": 2, "ai_user_concurrency": 1,
            "ai_global_queue_size": 2, "ai_user_queue_size": 1, "ai_slot_lease_seconds": 10}.items():
            self.stack.enter_context(patch.object(settings, name, value))
        self.capacity = MemoryAICapacity()

    def test_per_user_global_capacity_and_queue_limit(self):
        c = self.capacity
        self.assertEqual(c.acquire("alice", "a"), "acquired")
        self.assertEqual(c.acquire("alice", "b"), "waiting")
        self.assertEqual(c.acquire("alice", "c"), "full")
        self.assertEqual(c.acquire("bob", "d"), "acquired")
        self.assertEqual(c.acquire("carol", "e"), "waiting")
        self.assertEqual(c.acquire("dave", "f"), "full")
        c.release("alice", "a")
        self.assertEqual(c.acquire("alice", "b"), "acquired")
        c.release("alice", "a")
        self.assertEqual(c.snapshot("alice")["user_running"], 1)

    def test_atomic_rate_window(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            decisions = list(pool.map(lambda _: self.capacity.consume_rate("same-user", 5)[0], range(20)))
        self.assertEqual(sum(decisions), 5)
        self.assertTrue(self.capacity.consume_rate("other-user", 5)[0])

    def test_cancel_waiter_removes_queue_ticket(self):
        self.capacity.acquire("a", "running")
        self.capacity.acquire("a", "waiting")
        self.capacity.release("a", "waiting")
        self.assertEqual(self.capacity.snapshot("a")["user_waiting"], 0)


@unittest.skipUnless(os.getenv("TEST_AI_REDIS_URL"), "需要 TEST_AI_REDIS_URL 才运行真实 Redis 测试")
class RedisCapacityTests(CapacityTests):
    def setUp(self):
        super().setUp()
        self.prefix = f"fundscope-test:{{{uuid4().hex}}}"
        self.capacity = RedisAICapacity(os.environ["TEST_AI_REDIS_URL"], self.prefix)
        self.addCleanup(self.cleanup_keys)

    def cleanup_keys(self):
        keys = list(self.capacity.client.scan_iter(match=self.prefix + "*"))
        if keys:
            self.capacity.client.delete(*keys)
        self.capacity.client.close()

    def test_independent_clients_share_limits(self):
        other = RedisAICapacity(os.environ["TEST_AI_REDIS_URL"], self.prefix)
        self.addCleanup(other.client.close)
        self.assertEqual(self.capacity.acquire("a", "first"), "acquired")
        self.assertEqual(other.acquire("a", "second"), "waiting")
        self.capacity.consume_rate("shared", 1)
        self.assertFalse(other.consume_rate("shared", 1)[0])

    def test_redis_error_never_falls_back_to_memory(self):
        from redis.exceptions import ConnectionError
        with patch.object(self.capacity.client, "eval", side_effect=ConnectionError("offline")):
            with self.assertRaises(AIControlError) as caught:
                self.capacity.acquire("alice", "a")
        self.assertEqual(caught.exception.status_code, 503)

    def test_two_processes_share_one_rate_budget(self):
        with multiprocessing.get_context("spawn").Pool(2) as pool:
            # 子进程启动异常时也要有终点，不能让 Pool 无期限重建失败的 worker。
            pending = pool.map_async(redis_process_rate, [(os.environ["TEST_AI_REDIS_URL"], self.prefix)] * 2)
            results = pending.get(timeout=45)
        self.assertEqual(sum(results), 1)

    def test_expired_slot_cannot_be_renewed_or_release_successor(self):
        self.capacity.acquire("alice", "old")
        # 将本测试的旧票设置为已经过期，避免 100ms 租约受 Docker 调度抖动影响。
        for key in self.capacity._keys("alice")[:2]:
            self.capacity.client.zadd(key, {"old": 0})
        self.assertFalse(self.capacity.renew("alice", "old"))
        self.assertEqual(self.capacity.acquire("alice", "new"), "acquired")
        self.capacity.release("alice", "old")
        self.assertEqual(self.capacity.snapshot("alice")["user_running"], 1)


def model_response(content="hello", *, usage=True):
    body = {"id": "offline", "object": "chat.completion", "created": 1, "model": "fake",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}]}
    if usage:
        body["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    return httpx.Response(200, json=body)


class ModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = self.stack.enter_context(TemporaryDirectory())
        self.db = Database(f"sqlite:///{Path(root) / 'model.db'}")
        self.stack.callback(self.db.engine.dispose)
        self.ledger = AIUsageService(self.db)
        self.capacity = MemoryAICapacity()
        self.stack.enter_context(patch("app.services.governed_model.get_ai_capacity", return_value=self.capacity))
        self.stack.enter_context(patch("app.services.governed_model.get_ai_usage_service", return_value=self.ledger))
        self.stack.enter_context(ai_execution_scope("alice", "run:test", "test"))
        for name, value in {"ai_global_concurrency": 2, "ai_user_concurrency": 1,
            "ai_global_queue_size": 2, "ai_user_queue_size": 1, "ai_queue_wait_seconds": 1,
            "ai_daily_token_limit": 100000, "ai_run_token_limit": 100000,
            "ai_model_requests_per_minute": 100, "ai_max_output_tokens": 100}.items():
            self.stack.enter_context(patch.object(settings, name, value))

    def model(self, handler, async_handler=None, **kwargs):
        sync = self.stack.enter_context(httpx.Client(transport=httpx.MockTransport(handler)))
        client = httpx.AsyncClient(transport=httpx.MockTransport(async_handler or handler))
        self.addAsyncCleanup(client.aclose)
        return GovernedChatOpenAI(model="fake", api_key="offline-placeholder", base_url="https://model.invalid/v1",
            http_client=sync, http_async_client=client, **kwargs)

    def test_sync_call_charges_and_bounds_output(self):
        bodies = []
        def handler(request):
            bodies.append(json.loads(request.content))
            return model_response()
        result = self.model(handler, governance_provider="deepseek-primary").bind(max_tokens=9999).invoke("hello")
        self.assertEqual(result.content, "hello")
        self.assertEqual(bodies[0]["max_tokens"], 100)
        self.assertNotIn("max_completion_tokens", bodies[0])
        summary = self.ledger.summary(user_id="alice")
        self.assertEqual(summary["used_tokens"], 15)
        self.assertEqual(self.capacity.snapshot("alice")["running"], 0)

    async def test_async_call_preserves_identity_in_threads(self):
        result = await self.model(lambda _: model_response()).ainvoke("hello")
        self.assertEqual(result.content, "hello")
        self.assertEqual(self.ledger.summary(user_id="alice")["used_tokens"], 15)

    def test_structured_parser_failure_still_records_real_usage(self):
        class Answer(BaseModel):
            number: int
        model = self.model(lambda _: model_response("not-json"))
        with self.assertRaises(Exception):
            model.with_structured_output(Answer, method="json_mode").invoke("give number")
        self.assertEqual(self.ledger.summary(user_id="alice")["used_tokens"], 15)

    def test_structured_success_and_lcel(self):
        class Answer(BaseModel):
            number: int
        from langchain_core.prompts import ChatPromptTemplate
        model = self.model(lambda _: model_response('{"number": 3}'))
        chain = ChatPromptTemplate.from_template("{question}") | model.with_structured_output(Answer, method="json_mode")
        self.assertEqual(chain.invoke({"question": "number"}).number, 3)
        self.assertEqual(len(self.ledger.summary(user_id="alice")["calls"]), 1)

    def test_budget_rejection_never_calls_supplier(self):
        calls = []
        model = self.model(lambda request: calls.append(request) or model_response())
        with patch.object(settings, "ai_run_token_limit", 1), self.assertRaises(AIControlError):
            model.invoke("hello")
        self.assertEqual(calls, [])
        self.assertEqual(self.capacity.snapshot("alice")["running"], 0)

    def test_supplier_retry_is_two_distinct_ledger_entries(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(503, json={"error": {"message": "busy", "type": "server_error"}}) if len(calls) == 1 else model_response()
        with patch("app.services.governed_model.sleep"):
            self.model(handler, governance_retries=1).invoke("hello")
        result = self.ledger.summary(user_id="alice")
        self.assertEqual(len(calls), 2)
        self.assertEqual({row["status"] for row in result["calls"]}, {"unknown", "settled"})
        self.assertEqual(result["used_tokens"], 15)
        self.assertGreater(result["uncertain_tokens"], 0)

    def test_missing_usage_remains_unknown(self):
        self.model(lambda _: model_response(usage=False)).invoke("hello")
        result = self.ledger.summary(user_id="alice")
        self.assertIsNone(result["calls"][0]["total_tokens"])
        self.assertGreater(result["uncertain_tokens"], 0)

    async def test_cancel_in_flight_keeps_unknown_cost_and_releases_slot(self):
        started = asyncio.Event()
        async def handler(request):
            started.set()
            await asyncio.sleep(10)
            return model_response()
        task = asyncio.create_task(self.model(lambda _: model_response(), handler).ainvoke("hello"))
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.capacity.snapshot("alice")["running"], 0)
        self.assertEqual(self.ledger.summary(user_id="alice")["calls"][0]["status"], "unknown")

    async def test_cancel_while_queued_does_not_charge(self):
        self.capacity.acquire("alice", "occupied")
        model = self.model(lambda _: model_response())
        task = asyncio.create_task(model.ainvoke("hello"))
        for _ in range(30):
            if self.capacity.snapshot("alice")["waiting"]:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.capacity.snapshot("alice")["waiting"], 0)
        self.assertEqual(self.ledger.summary(user_id="alice")["calls"], [])

    async def test_cancel_awaiting_sync_thread_does_not_release_live_network_slot(self):
        from threading import Event
        entered, finish = Event(), Event()
        def handler(request):
            entered.set()
            finish.wait(5)
            return model_response()
        model = self.model(handler)
        task = asyncio.create_task(asyncio.to_thread(model.invoke, "hello"))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.capacity.snapshot("alice")["running"], 1)
        finally:
            finish.set()
        for _ in range(100):
            if self.capacity.snapshot("alice")["running"] == 0:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(self.capacity.snapshot("alice")["running"], 0)
        self.assertEqual(self.ledger.summary(user_id="alice")["used_tokens"], 15)

    async def test_queue_timeout_has_no_model_bill(self):
        self.capacity.acquire("alice", "occupied")
        with patch.object(settings, "ai_queue_wait_seconds", 0.02), self.assertRaises(AIControlError) as caught:
            await self.model(lambda _: model_response()).ainvoke("hello")
        self.assertEqual(caught.exception.code, "ai_queue_timeout")
        self.assertEqual(self.ledger.summary(user_id="alice")["calls"], [])

    async def test_control_rejection_does_not_fallback(self):
        from app.services.llm import ProviderFailoverRunnable
        primary = AsyncMock()
        primary.ainvoke.side_effect = AIControlError("quota", "no budget")
        backup = AsyncMock()
        with self.assertRaises(AIControlError):
            await ProviderFailoverRunnable([("a", primary), ("b", backup)]).ainvoke("question")
        backup.ainvoke.assert_not_called()

    @staticmethod
    def streaming_response():
        chunks = [
            {"id": "s", "object": "chat.completion.chunk", "created": 1, "model": "fake",
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hello"}, "finish_reason": None}]},
            {"id": "s", "object": "chat.completion.chunk", "created": 1, "model": "fake",
             "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        ]
        content = "".join(f"data: {json.dumps(row)}\n\n" for row in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    def test_stream_usage_is_settled_once(self):
        result = list(self.model(lambda _: self.streaming_response()).stream("hello"))
        self.assertEqual("".join(row.content for row in result), "hello")
        summary = self.ledger.summary(user_id="alice")
        self.assertEqual(len(summary["calls"]), 1)
        self.assertEqual(summary["used_tokens"], 15)

    async def test_async_invoke_with_streaming_enabled_is_settled_once(self):
        model = self.model(lambda _: self.streaming_response(), streaming=True)
        result = await model.ainvoke("hello")
        self.assertEqual(result.content, "hello")
        summary = self.ledger.summary(user_id="alice")
        self.assertEqual(len(summary["calls"]), 1)
        self.assertEqual(summary["used_tokens"], 15)


if __name__ == "__main__":
    unittest.main()

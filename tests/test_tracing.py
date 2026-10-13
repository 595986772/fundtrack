"""追踪验收：不用收费模型，不访问真实账户，验证跨进程、脱敏和失败边界。"""

import asyncio
from contextlib import ExitStack
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
import httpx
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import StatusCode

from app.config import settings
from app.database import Database
from app.services import tracing
from app.services.ai_capacity import MemoryAICapacity
from app.services.ai_governance_context import ai_execution_scope
from app.services.ai_usage import AIUsageService
from app.services.background_jobs import BackgroundJobService
from app.services.governed_model import GovernedChatOpenAI
from app.services.governed_model import async_model_permit
from app.services.job_delivery import JobDeliveryService
from app.services.job_store import SqlJobStore
from app.services.job_runtime import JobRunner
from app.services.reliable_job_runtime import run_reliable_job
from app.services.reliable_jobs import database_now
from tests.test_ai_governance import model_response
from tests.test_portfolio_research_graph import build_graph_service, PORTFOLIO_ID
from tests.test_reliable_jobs import CapturingReliableExecutor


class TracingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(TemporaryDirectory()))
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider(resource=Resource.create({"service.name": "trace-test"}))
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.stack.callback(self.provider.shutdown)
        self.stack.enter_context(patch.object(tracing, "_provider", self.provider))
        self.stack.enter_context(patch.object(tracing, "_pid", os.getpid()))
        for key, value in {
            "trace_enabled": True, "agent_task_authorization_enabled": False,
            "ai_governance_enabled": False, "reliable_research_jobs_enabled": True,
            "trace_otlp_endpoint": "", "langfuse_base_url": "",
        }.items():
            self.stack.enter_context(patch.object(settings, key, value))

    def spans(self, name=None):
        spans = self.exporter.get_finished_spans()
        return [span for span in spans if name is None or span.name == name]

    def database(self):
        db = Database(f"sqlite:///{self.root / 'jobs.db'}")
        self.stack.callback(db.engine.dispose)
        return db

    def test_metadata_inheritance_and_error_redaction(self):
        with tracing.trace_span("request", root=True, attributes={"job.id": "job-1"}):
            carrier = tracing.capture_trace_context()
            try:
                with tracing.trace_span("model", attributes={"langfuse.observation.type": "generation"}):
                    with tracing.trace_span("validation"):
                        raise ValueError("SECRET question, holdings and API key")
            except ValueError:
                pass
        self.assertIsNone(tracing.current_trace_id())
        validation = self.spans("validation")[0]
        self.assertEqual(validation.attributes["job.id"], "job-1")
        self.assertNotIn("langfuse.observation.type", validation.attributes)
        self.assertEqual(validation.status.status_code, StatusCode.ERROR)
        self.assertEqual(validation.attributes["error.type"], "ValueError")
        self.assertEqual(set(carrier), {"traceparent", "attributes"})
        self.assertNotIn("SECRET", "".join(span.to_json() for span in self.spans()))
        self.assertFalse(validation.events)

    def test_disabled_tracing_is_noop(self):
        with patch.object(settings, "trace_enabled", False):
            with tracing.trace_span("hidden"):
                self.assertEqual(tracing.capture_trace_context(), {})
                tracing.mark_error("hidden")
        self.assertEqual(self.spans(), [])

    def test_parent_sampling_survives_durable_carrier(self):
        provider = TracerProvider(sampler=ParentBased(TraceIdRatioBased(0)))
        self.addCleanup(provider.shutdown)
        with patch.object(tracing, "_provider", provider):
            with tracing.trace_span("unsampled", root=True):
                carrier = tracing.capture_trace_context()
        with tracing.trace_span("restored", carrier=carrier) as span:
            self.assertFalse(span.is_recording())
        self.assertEqual(self.spans(), [])

    def test_http_stream_lives_until_final_chunk_and_ignores_public_parent(self):
        app = FastAPI()
        app.add_middleware(tracing.TraceHTTPMiddleware)

        @app.get("/chat/{conversation}")
        async def chat(conversation: str):
            async def chunks():
                with tracing.trace_span("stream.generate"):
                    yield "data: one\n\n"
                    await asyncio.sleep(0)
                    yield "data: two\n\n"
            return StreamingResponse(chunks(), media_type="text/event-stream")

        fake_id = "1" * 32
        response = TestClient(app).get("/chat/PRIVATE-ID?key=SECRET", headers={
            "traceparent": f"00-{fake_id}-{'2' * 16}-01", "baggage": "secret=SECRET"})
        server = self.spans("GET /chat/{conversation}")[0]
        child = self.spans("stream.generate")[0]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-trace-id"], f"{server.context.trace_id:032x}")
        self.assertNotEqual(response.headers["x-trace-id"], fake_id)
        self.assertEqual(child.parent.span_id, server.context.span_id)
        self.assertGreaterEqual(server.end_time, child.end_time)
        self.assertNotIn("PRIVATE-ID", server.to_json())
        self.assertNotIn("SECRET", server.to_json())

    def test_graph_parallel_branches_and_completed_checkpoint(self):
        graph = build_graph_service()
        with tracing.trace_span("request", root=True):
            report = graph.run(run_id="trace-graph", user_id="chen", portfolio_id=PORTFOLIO_ID)
        workflow = self.spans("research.workflow")[0]
        trace_id = f"{workflow.context.trace_id:032x}"
        self.assertEqual(report["graph_trace"]["trace_id"], trace_id)
        self.assertTrue(all(span.context.trace_id == workflow.context.trace_id for span in self.spans()))
        reads = [span for span in self.spans() if span.name.startswith("research.retrieve.")]
        self.assertEqual(len(reads), 5)
        self.assertEqual(self.spans("research.compose_report")[0].parent.span_id, workflow.context.span_id)
        self.exporter.clear()
        restored = graph.run(run_id="trace-graph", user_id="chen", portfolio_id=PORTFOLIO_ID)
        self.assertTrue(restored["graph_trace"]["resumed_from_checkpoint"])
        # 已完成报告不会伪造一次新的生成或检索耗时。
        self.assertFalse(self.spans("research.interpret_with_llm"))
        self.assertFalse(self.spans("research.snapshot"))

    def test_committee_roles_are_separate_children_with_prompt_versions(self):
        from tests.test_portfolio_research_committee import CommitteeModel
        graph, model = build_graph_service(), CommitteeModel(parallel=True)
        with patch("app.services.portfolio_research_committee.get_chat_model", side_effect=model.create):
            with tracing.trace_span("job", root=True):
                graph.run(run_id="committee-trace", user_id="chen", portfolio_id=PORTFOLIO_ID,
                    research_mode="committee")
        workflow = self.spans("research.workflow")[0]
        roles = [self.spans("research." + name)[0] for name in (
            "committee_opportunity", "committee_risk", "committee_opportunity_review",
            "committee_risk_review", "committee_synthesis")]
        self.assertTrue(all(span.parent.span_id == workflow.context.span_id for span in roles))
        self.assertEqual(roles[0].attributes["research.result_status"], "accepted")
        calls = self.spans("research.committee_model")
        self.assertEqual(len(calls), 5)
        self.assertEqual({span.parent.span_id for span in calls}, {span.context.span_id for span in roles})
        self.assertTrue(all(span.attributes["prompt.version"] == "portfolio-committee-v1" for span in calls))
        self.assertTrue(self.spans("research.validate"))

    def test_retried_job_keeps_trace_but_records_two_attempts(self):
        db = self.database()
        store, executor = SqlJobStore(db), CapturingReliableExecutor()
        service = BackgroundJobService(store=store, executor=executor)
        self.stack.callback(service.shutdown)
        with tracing.trace_span("request", root=True):
            service.start_job(user_id="chen", title="test", job_id="retry-trace",
                job_type="portfolio_research", portfolio_id=PORTFOLIO_ID, max_retries=1)
        delivery = JobDeliveryService(store, executor)
        delivery.tick()
        with patch.object(JobRunner, "_run_portfolio_research", side_effect=[TimeoutError("SECRET"), {"ok": True}]):
            run_reliable_job(store, user_id="chen", job_id="retry-trace", dispatch_id=executor.calls[0]["dispatch_id"])
            with db.session() as session:
                future = database_now(session) + timedelta(seconds=5)
            with patch("app.services.reliable_jobs.database_now", return_value=future):
                delivery.tick()
                result = run_reliable_job(store, user_id="chen", job_id="retry-trace", dispatch_id=executor.calls[1]["dispatch_id"])
        self.assertEqual(result.status, "completed")
        attempts = self.spans("job.execute")
        self.assertEqual([span.attributes["job.attempt"] for span in attempts], [1, 2])
        self.assertEqual(attempts[0].context.trace_id, attempts[1].context.trace_id)
        self.assertEqual(attempts[0].status.status_code, StatusCode.ERROR)
        self.assertNotIn("job.initial_queue_ms", attempts[1].attributes)

    def test_async_cancel_preserves_cleanup_and_usage_unknown(self):
        # 取消仍要结算，不能因为增加观测就跳过原有 finally。
        from app.services.governed_model import ModelPermit
        with patch.object(ModelPermit, "rate"), patch.object(ModelPermit, "poll", return_value=True), \
                patch.object(ModelPermit, "reserve"), patch.object(ModelPermit, "start"), \
                patch.object(ModelPermit, "finish") as finish:
            async def run():
                with tracing.trace_span("stream", root=True):
                    async with async_model_permit(provider="fake", model="fake", payload={}, timeout=1):
                        raise asyncio.CancelledError()
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(run())
        finish.assert_called_once()
        generation = self.spans("ai.generate")[0]
        self.assertTrue(generation.attributes["execution.cancelled"])
        self.assertFalse(generation.attributes["ai.usage_known"])
        self.assertTrue(self.spans("ai.settle"))

    def test_local_trace_inspector_renders_tree(self):
        from scripts.inspect_agent_trace import load_spans, describe
        with tracing.trace_span("root", root=True):
            with tracing.trace_span("child", attributes={"tool.name": "calculator_tool"}):
                pass
        trace_id = f"{self.spans()[0].context.trace_id:032x}"
        file = self.root / "spans-test.jsonl"
        file.write_text("\n".join(json.dumps(json.loads(span.to_json())) for span in self.spans()), encoding="utf-8")
        result = describe(load_spans(self.root, trace_id))
        self.assertTrue(result.startswith("root"))
        self.assertIn("\n  child", result)
        self.assertIn("tool.name=calculator_tool", result)

    def test_job_context_survives_sql_and_new_worker_process(self):
        """API 已返回，Outbox 换上下文，Worker 再换进程，仍然是同一个 trace_id。"""
        db = self.database()
        store = SqlJobStore(db)
        executor = CapturingReliableExecutor()
        service = BackgroundJobService(store=store, executor=executor)
        self.stack.callback(service.shutdown)
        app = FastAPI()
        app.add_middleware(tracing.TraceHTTPMiddleware)
        @app.post("/research")
        def submit():
            return service.start_job(user_id="chen", title="SECRET", job_id="trace-job",
                job_type="portfolio_research", portfolio_id=PORTFOLIO_ID, idempotency_key="same")
        response = TestClient(app).post("/research")
        first = response.json()
        self.assertEqual(first["job"]["trace_id"], response.headers["x-trace-id"])
        with tracing.trace_span("another_request", root=True):
            duplicate = service.start_job(user_id="chen", title="SECRET", job_id="different",
                job_type="portfolio_research", portfolio_id=PORTFOLIO_ID, idempotency_key="same")
        self.assertFalse(duplicate["accepted"])
        self.assertEqual(first["job"]["trace_id"], duplicate["job"]["trace_id"])
        self.assertNotIn("_trace_context", first["job"]["payload"])
        JobDeliveryService(store, executor).tick()
        dispatch = executor.calls[0]["dispatch_id"]
        # 真实启动第二个 Python 进程，不能靠同进程 ContextVar 碰巧传播成功。
        program = '''
import sys
from unittest.mock import patch
from app.database import Database
from app.services.job_store import SqlJobStore
from app.services.job_runtime import JobRunner
from app.services.reliable_job_runtime import run_reliable_job
from app.services.tracing import shutdown_tracing
from tests.test_portfolio_research_graph import build_graph_service, PORTFOLIO_ID
db = Database(sys.argv[1])
def research(runner, record, started_at):
    return build_graph_service().run(run_id=record.id, user_id="chen", portfolio_id=PORTFOLIO_ID)
try:
    with patch.object(JobRunner, "_run_portfolio_research", research):
        record = run_reliable_job(SqlJobStore(db), user_id="chen", job_id="trace-job", dispatch_id=sys.argv[2])
        assert record.status == "completed"
finally:
    shutdown_tracing()
    db.engine.dispose()
'''
        environment = {**os.environ, "TRACE_ENABLED": "true", "TRACE_LOCAL_ENABLED": "true",
            "TRACE_LOCAL_DIR": str(self.root / "traces"), "TRACE_SAMPLE_RATE": "1",
            "TRACE_OTLP_ENDPOINT": "", "LANGFUSE_BASE_URL": "", "AGENT_TASK_AUTHORIZATION_ENABLED": "false",
            "AI_GOVERNANCE_ENABLED": "false", "RELIABLE_RESEARCH_JOBS_ENABLED": "true"}
        run = subprocess.run([sys.executable, "-c", program, str(db.engine.url), dispatch],
            env=environment, capture_output=True, text=True, timeout=90)
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        rows = [json.loads(line) for path in (self.root / "traces").glob("*.jsonl")
                for line in path.read_text().splitlines()]
        execute = next(row for row in rows if row["name"] == "job.execute")
        create = self.spans("job.create")[0]
        self.assertEqual(create.parent.span_id, self.spans("POST /research")[0].context.span_id)
        self.assertEqual(execute["context"]["trace_id"], "0x" + first["job"]["trace_id"])
        self.assertEqual(execute["parent_id"], f"0x{create.context.span_id:016x}")
        self.assertEqual(self.spans("job.dispatch")[0].context.trace_id, create.context.trace_id)
        self.assertEqual(execute["attributes"]["job.attempt"], 1)
        self.assertGreaterEqual(execute["attributes"]["job.initial_queue_ms"], 0)
        self.assertTrue(any(row["name"] == "job.save_result" for row in rows))
        self.assertNotIn("SECRET", json.dumps(rows))

    def test_model_retry_usage_and_sensitive_prompt_not_exported(self):
        db = self.database()
        ledger = AIUsageService(db)
        self.stack.enter_context(patch("app.services.governed_model.get_ai_usage_service", return_value=ledger))
        self.stack.enter_context(patch("app.services.governed_model.get_ai_capacity", return_value=MemoryAICapacity()))
        requests = []
        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(503, json={"error": {"message": "SECRET provider message", "type": "server_error"}})
            return model_response("SECRET model answer")
        client = self.stack.enter_context(httpx.Client(transport=httpx.MockTransport(handler)))
        model = GovernedChatOpenAI(model="offline", api_key="offline-placeholder", base_url="https://model.invalid/v1",
            http_client=client, governance_retries=1)
        with ai_execution_scope("chen", "trace-model", "test"), tracing.trace_span("agent", root=True):
            with patch("app.services.governed_model.sleep"):
                model.invoke("SECRET question")
        generations = self.spans("ai.generate")
        self.assertEqual(len(generations), 2)
        self.assertEqual([span.attributes["ai.attempt"] for span in generations], [1, 2])
        self.assertEqual(generations[0].status.status_code, StatusCode.ERROR)
        self.assertFalse(generations[0].attributes["ai.usage_known"])
        self.assertNotIn("gen_ai.usage.input_tokens", generations[0].attributes)
        self.assertEqual(generations[1].attributes["gen_ai.usage.input_tokens"], 10)
        self.assertEqual(len(self.spans("ai.settle")), 2)
        self.assertNotIn("SECRET", "".join(span.to_json() for span in self.spans()))

    def test_otlp_payload_and_langfuse_auth_contract(self):
        """跑真实 protobuf 编码和 SDK 导出，网络边界用假接收端替代，不发到外网。"""
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
        with patch.object(settings, "langfuse_base_url", "https://trace.invalid"), \
                patch.object(settings, "langfuse_public_key", "public-test"), \
                patch.object(settings, "langfuse_secret_key", "private-test"):
            endpoint, headers = tracing._remote_export_options()
        self.assertEqual(endpoint, "https://trace.invalid/api/public/otel/v1/traces")
        self.assertTrue(headers["Authorization"].startswith("Basic "))
        session = Mock()
        session.post.return_value = Mock(ok=True, status_code=200)
        remote = OTLPSpanExporter(endpoint=endpoint, headers=headers, session=session)
        self.addCleanup(remote.shutdown)
        with tracing.trace_span("otlp-test", root=True):
            pass
        self.assertEqual(remote.export(self.spans()), SpanExportResult.SUCCESS)
        request = ExportTraceServiceRequest()
        request.ParseFromString(session.post.call_args.kwargs["data"])
        self.assertEqual(request.resource_spans[0].scope_spans[0].spans[0].name, "otlp-test")
        self.assertNotIn("private-test", str(request))

    def test_export_failure_does_not_break_business(self):
        with patch.object(self.exporter, "export", side_effect=ConnectionError("offline")):
            with self.assertLogs("opentelemetry.sdk.trace.export", level="ERROR"):
                with tracing.trace_span("still-finishes"):
                    result = 42
        self.assertEqual(result, 42)


if __name__ == "__main__":
    unittest.main()

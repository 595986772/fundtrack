"""正式运行配置、就绪语义和 Cookie 写请求的关键回归测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.database import Database
from app.main import app
from app.models import BackgroundJobModel
from app.services.auth import require_admin
from app.services.deployment import validate_production_settings
from app.services.deployment_health import dependency_status, runtime_diagnostics
from app.services.openfga_authorization import OpenFGAPolicy, OpenFGAPolicyError


class ProductionRuntimeTests(unittest.TestCase):
    def test_unknown_deployment_mode_fails_startup(self) -> None:
        with patch.object(settings, "deployment_mode", "prod"):
            with self.assertRaisesRegex(RuntimeError, "DEPLOYMENT_MODE"):
                validate_production_settings()

    def test_production_startup_rejects_insecure_auth(self) -> None:
        production = {
            "deployment_mode": "production", "public_origin": "https://fund.example",
            "public_host": "fund.example", "auth_required": True,
            "auth_cookie_secure": True, "database_auto_create_schema": False,
            "database_url": "postgresql+psycopg://app:example@db/fundscope",
            "job_execution_mode": "celery", "ai_control_backend": "redis",
            "agent_task_authorization_enabled": True, "openfga_token": "example",
            "deepseek_api_key": "example",
        }
        with patch.multiple(settings, **production):
            validate_production_settings()
            with patch.object(settings, "auth_required", False):
                with self.assertRaisesRegex(RuntimeError, "AUTH_REQUIRED"):
                    validate_production_settings()
            with patch.object(settings, "public_origin", "http://fund.example"):
                with self.assertRaisesRegex(RuntimeError, "PUBLIC_ORIGIN"):
                    validate_production_settings()

    def test_cookie_writes_require_same_origin_in_production(self) -> None:
        client = TestClient(app)
        client.cookies.set(settings.auth_cookie_name, "example-session")
        with patch.object(settings, "deployment_mode", "production"), patch.object(
            settings, "public_origin", "https://fund.example"
        ), patch("app.routers.auth.auth_service.logout"):
            denied = client.post("/api/v1/auth/logout", headers={
                "Origin": "https://other.example",
            })
            self.assertEqual(denied.status_code, 403)
            self.assertIn("X-Request-ID", denied.headers)
            self.assertEqual(client.post("/api/v1/auth/logout").status_code, 403)
            self.assertEqual(client.post("/api/v1/auth/logout", headers={
                "Origin": "https://fund.example",
            }).status_code, 200)

    def test_readiness_reports_dependency_failure(self) -> None:
        client = TestClient(app)
        with patch("app.routers.health.dependency_status", return_value={
            "database": "ok", "broker": "unavailable",
        }):
            response = client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["checks"]["broker"], "unavailable")
        self.assertEqual(client.get("/livez").status_code, 200)

    def test_database_probe_and_queue_metrics(self) -> None:
        with TemporaryDirectory() as directory:
            database = Database(f"sqlite:///{Path(directory) / 'health.db'}")
            database.create_schema()
            now = datetime.now(timezone.utc)
            with database.session() as session, session.begin():
                session.add(BackgroundJobModel(
                    user_id="test", id="queued", title="Queued", status="queued",
                    created_at=now - timedelta(seconds=35), updated_at=now,
                ))
                session.add(BackgroundJobModel(
                    user_id="test", id="running", title="Running", status="running",
                    created_at=now - timedelta(seconds=20), updated_at=now,
                ))
            with patch("app.services.deployment_health.runtime_database", database), patch.multiple(
                settings, job_execution_mode="local", ai_control_backend="memory",
                agent_task_authorization_enabled=False,
            ):
                self.assertEqual(dependency_status(), {"database": "ok"})
                metrics = runtime_diagnostics()
                self.assertEqual(metrics["queued_jobs"], 1)
                self.assertEqual(metrics["running_jobs"], 1)
                self.assertGreaterEqual(metrics["oldest_queued_seconds"], 30)
                with patch.object(database.engine, "connect", side_effect=OSError):
                    self.assertEqual(dependency_status()["database"], "unavailable")

    def test_missing_worker_makes_celery_runtime_unready(self) -> None:
        with TemporaryDirectory() as directory:
            database = Database(f"sqlite:///{Path(directory) / 'health.db'}")
            with patch("app.services.deployment_health.runtime_database", database), patch.multiple(
                settings, job_execution_mode="celery", ai_control_backend="memory",
                agent_task_authorization_enabled=False,
            ), patch("redis.Redis.from_url") as redis_factory, patch(
                "app.services.deployment_health.responding_workers", return_value=0,
            ):
                redis_factory.return_value.ping.return_value = True
                checks = dependency_status()
            self.assertEqual(checks["database"], "ok")
            self.assertEqual(checks["broker"], "ok")
            self.assertEqual(checks["worker"], "unavailable")

    def test_openfga_probe_checks_configured_model(self) -> None:
        policy = OpenFGAPolicy("http://localhost:8080", "store_1", "model_1", "token")
        with patch.object(policy._client, "request", return_value={
            "authorization_model": {"id": "model_1"},
        }):
            policy.check_available()
        with patch.object(policy._client, "request", return_value={
            "authorization_model": {"id": "other_model"},
        }):
            with self.assertRaises(OpenFGAPolicyError):
                policy.check_available()

    def test_operations_details_require_admin(self) -> None:
        client = TestClient(app)
        with patch.object(settings, "auth_required", True):
            self.assertEqual(client.get("/api/v1/ops/runtime").status_code, 401)
        app.dependency_overrides[require_admin] = lambda: object()
        try:
            with patch("app.routers.health.runtime_diagnostics", return_value={
                "queued_jobs": 2, "running_jobs": 1,
                "oldest_queued_seconds": 45, "responding_workers": 1,
            }):
                response = client.get("/api/v1/ops/runtime")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["queued_jobs"], 2)
        finally:
            app.dependency_overrides.pop(require_admin, None)


if __name__ == "__main__":
    unittest.main()

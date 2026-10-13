"""灾备测试：离线单测 + 显式启用的真实 PostgreSQL/Docker 隔离恢复演练。"""

import io
import json
import os
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from scripts.recovery_audit import LEDGER_TABLES, audit_ledger
from scripts.backup_restore import (ARTIFACTS, FORMAT, Compose, RecoveryError, archive_inventory,
    backup, business_audit, file_hash, maintenance_lock, preflight, quiesced, restore_drill, verify_bundle, write_json)


def ledger_fixture():
    tables = {name: [] for name in LEDGER_TABLES}
    scope = {"user_id": "test-user", "portfolio_id": "test-portfolio"}
    tables["fund_portfolios"] = [{"user_id": "test-user", "id": "test-portfolio"}]
    tables["fund_portfolio_holdings"] = [{**scope, "fund_code": "110020", "position_mode": "actual", "units": 80, "cost_amount": 80}]
    tables["fund_portfolio_transactions"] = [
        {**scope, "id": "buy", "fund_code": "110020", "transaction_type": "buy", "trade_date": "2025-01-02",
         "created_at": "2025-01-02T12:00:00+00:00", "units": 100, "unit_price": 1, "fee": 0, "exact_values": None},
        {**scope, "id": "sell", "fund_code": "110020", "transaction_type": "sell", "trade_date": "2025-02-03",
         "created_at": "2025-02-03T12:00:00+00:00", "units": 20, "unit_price": 1.5, "fee": 0, "exact_values": None},
    ]
    return tables


NAVS = {"110020": {"nav_date": "2025-02-04", "unit_nav": 1.8}}


class RecoveryAuditTests(unittest.TestCase):
    def test_replays_units_cost_and_pnl_at_backup_nav(self):
        result = audit_ledger(ledger_fixture(), NAVS)
        self.assertTrue(result["passed"])
        fund = result["portfolios"][0]["funds"][0]
        self.assertEqual((fund["units"], fund["cost_amount"], fund["realized_pnl"], fund["total_pnl"]), ("80.0000", "80.00", "10.00", "74.00"))
        self.assertNotIn("test-user", json.dumps(result))

    def test_missing_nav_is_unknown_and_never_zero(self):
        result = audit_ledger(ledger_fixture(), {})
        self.assertTrue(result["passed"])
        self.assertFalse(result["valuation_complete"])
        self.assertIsNone(result["portfolios"][0]["funds"][0]["total_pnl"])

    def test_tampered_projection_oversell_and_orphan_are_detected(self):
        for mutation, code in ((lambda t: t["fund_portfolio_holdings"][0].update(units=90), "units_mismatch"),
                (lambda t: t["fund_portfolio_holdings"][0].update(cost_amount=1), "cost_mismatch"),
                (lambda t: t["fund_portfolio_transactions"][1].update(units=101), "ledger_replay_failed"),
                (lambda t: t["fund_portfolios"].clear(), "orphan_record")):
            tables = ledger_fixture()
            mutation(tables)
            result = audit_ledger(tables, NAVS)
            self.assertFalse(result["passed"])
            self.assertIn(code, [r["code"] for r in result["issues"]])

    def test_correction_and_close_position_preserve_realized_pnl(self):
        tables = ledger_fixture()
        sale = tables["fund_portfolio_transactions"][1]
        tables["fund_portfolio_transactions"].append({**sale, "id": "replacement", "units": 100})
        tables["portfolio_transaction_corrections"] = [{"user_id": "test-user", "portfolio_id": "test-portfolio",
            "transaction_id": "sell", "replacement_transaction_id": "replacement"}]
        tables["fund_portfolio_holdings"] = []
        result = audit_ledger(tables, {})
        self.assertTrue(result["passed"])
        self.assertEqual(result["portfolios"][0]["funds"][0]["realized_pnl"], "50.00")
        self.assertEqual(result["portfolios"][0]["transactions"], 3)

    def test_pending_sell_and_bad_correction_chain_are_reported(self):
        tables = ledger_fixture()
        tables["portfolio_trade_orders"] = [{"user_id": "test-user", "portfolio_id": "test-portfolio", "fund_code": "110020",
            "status": "cancel_requested", "transaction_type": "sell", "requested_units": "90"}]
        self.assertIn("reserved_units_exceed_holdings", [r["code"] for r in audit_ledger(tables, NAVS)["issues"]])
        tables["portfolio_transaction_corrections"] = [{"user_id": "test-user", "portfolio_id": "test-portfolio", "transaction_id": "sell", "replacement_transaction_id": "sell"}]
        self.assertIn("correction_cycle", [r["code"] for r in audit_ledger(tables, NAVS)["issues"]])

    def test_allocation_without_ledger_is_explicitly_not_applicable(self):
        tables = ledger_fixture()
        tables["fund_portfolio_transactions"] = []
        tables["fund_portfolio_holdings"][0]["position_mode"] = "allocation"
        self.assertEqual(audit_ledger(tables, NAVS)["portfolios"][0]["status"], "allocation_not_applicable")


class BackupSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def archive(self, name="docs/test.md", *, link=False):
        path = self.root / "documents.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            member = tarfile.TarInfo(name)
            member.size = 4
            if link:
                member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
            archive.addfile(member, io.BytesIO(b"test"))
        return path

    def test_archive_rejects_traversal_links_and_secret_directories(self):
        for name, link in (("../escape", False), ("/absolute", False), ("docs/link", True), ("xhs_browser_profile/cookie", False), (".env", False)):
            with self.subTest(name=name), self.assertRaises(RecoveryError):
                archive_inventory(self.archive(name, link=link))

    def test_bundle_hashes_detect_corruption_before_docker_is_used(self):
        docs = archive_inventory(self.archive())
        for name in ARTIFACTS:
            if name != "documents.tar.gz":
                (self.root / name).write_bytes(b"test")
        manifest = {"format": FORMAT, "status": "complete", "documents": docs, "data_root": {"uid": 10001, "gid": 10001, "mode": 0o755},
            "artifacts": {name: {"sha256": file_hash(self.root / name)} for name in ARTIFACTS}}
        write_json(self.root / "manifest.json", manifest)
        verify_bundle(self.root)
        (self.root / "business.dump").write_bytes(b"corrupt")
        with patch("scripts.backup_restore.command") as docker, self.assertRaises(RecoveryError):
            restore_drill(self.root)
        docker.assert_not_called()

    def test_backup_requires_explicit_maintenance_opt_in(self):
        with patch("scripts.backup_restore.preflight") as check, self.assertRaises(RecoveryError):
            backup(Mock(), self.root, stop_writers=False)
        check.assert_not_called()

    def test_failure_resumes_only_originally_running_writers(self):
        source = Mock()
        source.run.side_effect = [b"postgres\nopenfga-postgres\napi\nworker\n", b"", b"", b"postgres\nopenfga-postgres\n", b""]
        with self.assertRaises(ValueError):
            with quiesced(source):
                raise ValueError("injected failure")
        self.assertEqual(source.run.call_args.args, ("start", "worker", "api"))

    def test_recovery_hold_blocks_api_and_worker_startup(self):
        from app.services.deployment import ensure_recovery_released
        with patch("app.services.deployment.Path.exists", return_value=True), self.assertRaises(RuntimeError):
            ensure_recovery_released()

    def test_maintenance_lock_rejects_overlapping_backup(self):
        with patch("scripts.backup_restore.ROOT", self.root):
            with maintenance_lock("fixture"):
                with self.assertRaises(RecoveryError):
                    with maintenance_lock("fixture"):
                        self.fail("并发维护不应获得锁")

    def test_report_write_failure_still_cleans_up_new_environment(self):
        manifest = {"backup_id": "fixture", "recovery_point_utc": "2025-01-01T00:00:00Z"}
        with patch("scripts.backup_restore.verify_bundle", return_value=manifest), \
                patch("scripts.backup_restore.command", return_value=b""), \
                patch("scripts.backup_restore.Compose") as factory, \
                patch("scripts.backup_restore.write_json", side_effect=OSError("fixture")):
            target = factory.return_value
            target.run.side_effect = [RecoveryError("fixture"), b""]
            with self.assertRaises(OSError):
                restore_drill(self.root)
            target.run.assert_called_with("down", "-v", "--remove-orphans", timeout=120)

    def test_backup_rejects_application_using_a_different_database(self):
        source = Mock()
        source.run.return_value = b"fixture-container"
        info = [{"Config": {"Env": ["DATABASE_URL=sqlite:///data/local.db"]}}]
        with patch("scripts.backup_restore.command", return_value=json.dumps(info).encode()), self.assertRaises(RecoveryError):
            preflight(source)
        source.sql.assert_not_called()


@unittest.skipUnless(os.getenv("RUN_RECOVERY_DOCKER_TEST") == "1", "需要显式启用隔离 Docker 演练")
class DockerRecoveryTests(unittest.TestCase):
    def test_real_dump_restore_files_accounting_and_quarantine(self):
        """只操作随机测试项目，PostgreSQL 真执行；API/Worker 用不做业务的占位进程。"""
        import yaml
        from sqlalchemy.schema import CreateTable
        from sqlalchemy.dialects import postgresql
        from app.database import Base
        import app.models  # 注册表，不实例化业务 Service
        from scripts.backup_restore import RECOVERY_COMPOSE
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = yaml.safe_load(RECOVERY_COMPOSE.read_text())
            config["services"].pop("files")
            for service in ("api", "worker", "openfga", "proxy"):
                config["services"][service] = {"image": "postgres:16-alpine", "entrypoint": ["sh", "-c", "trap 'exit 0' TERM; while :; do sleep 1 & wait $!; done"],
                    "volumes": ["documents:/app/data"], "networks": ["isolated"],
                    "environment": {"CHROMA_PERSIST_DIR": "/app/data/chroma", "FUND_REPORT_STORAGE_DIR": "/app/data/fund_reports", "DATABASE_URL": "postgresql://fundscope:isolated-test-password@postgres:5432/fundscope"}}
            compose_file = root / "source.json"
            compose_file.write_text(json.dumps(config))
            source = Compose(compose_file, "fundscope-backuptest-" + uuid4().hex[:10],
                env={**os.environ, "RECOVERY_DB_PASSWORD": "isolated-test-password"})
            try:
                source.run("up", "-d", "--wait", "--wait-timeout", "90", timeout=150)
                ddl = ";\n".join(str(CreateTable(table).compile(dialect=postgresql.dialect())) for table in Base.metadata.sorted_tables)
                source.sql("postgres", ddl + ";")
                self.seed(source)
                source.sql("openfga-postgres", "CREATE TABLE auth_fixture(id serial PRIMARY KEY, model jsonb); INSERT INTO auth_fixture(model) VALUES ('{\"scope\":\"test-only\"}')")
                source.run("exec", "-T", "api", "sh", "-c",
                    'mkdir -p /app/data/docs /app/data/fund_reports /app/data/schedules /app/data/xhs_browser_profile; '
                    'printf "reference" > /app/data/docs/original.md; printf "{}" > /app/data/schedules/test.json; '
                    'printf "fixture-pdf-bytes" > /app/data/fund_reports/test-report.pdf; '
                    'printf "private-cookie" > /app/data/xhs_browser_profile/cookie; chown -R 10001:10001 /app/data')
                original = business_audit(source)
                bundle = backup(source, root / "independent-backup", stop_writers=True)
                manifest = verify_bundle(bundle)
                self.assertIn("docs/original.md", manifest["documents"])
                self.assertNotIn("xhs_browser_profile/cookie", manifest["documents"])
                self.assertEqual(business_audit(source), original)
                # 真实恢复后先校验所有表，再隔离旧任务。保留副本检查，然后明确清理。
                report_path, report = restore_drill(bundle, keep=True)
                recovered = Compose(RECOVERY_COMPOSE, report["project"], env={**os.environ, "RECOVERY_DB_PASSWORD": "unused-for-local-socket"})
                try:
                    self.assertTrue(report["passed"], report)
                    self.assertEqual(recovered.sql("postgres", "SELECT status FROM background_jobs"), "cancelled")
                    self.assertEqual(recovered.sql("postgres", "SELECT status FROM agent_task_grants"), "revoked")
                    self.assertEqual(recovered.sql("postgres", "SELECT count(*) FROM auth_sessions"), "0")
                    self.assertEqual(recovered.sql("postgres", "SELECT status FROM job_outbox"), "closed")
                    self.assertEqual(recovered.sql("postgres", "SELECT execution_version FROM reliable_job_states"), "2")
                    self.assertEqual(recovered.sql("postgres", "SELECT status FROM ai_model_calls"), "unknown")
                    recovered.run("run", "--rm", "--no-deps", "-T", "--entrypoint", "sh", "files", "-c",
                        'test -f /restore-data/RECOVERY_HOLD.json && test ! -e /restore-data/schedules && test -f /restore-data/recovery-disabled-schedules/test.json')
                    recovered.run("run", "--rm", "--no-deps", "-T", "--user", "10001:10001", "--entrypoint", "sh", "files", "-c",
                        'test -w /restore-data && test -w /restore-data/fund_reports && test -r /restore-data/fund_reports/test-report.pdf')
                    self.assertEqual(source.sql("postgres", "SELECT status FROM background_jobs"), "running")
                    self.assertTrue(report["quarantined"])
                    self.assertEqual(report["audit"]["ledger"], original["ledger"])
                    # 可保留一份无凭据的合成数据演练报告，供项目文档引用实际耗时。
                    if os.getenv("RECOVERY_TEST_REPORT"):
                        output = Path(os.environ["RECOVERY_TEST_REPORT"])
                        output.parent.mkdir(parents=True, exist_ok=True)
                        write_json(output, {**report, "kept": False, "fixture_only": True})
                finally:
                    recovered.run("down", "-v", "--remove-orphans")
            finally:
                source.run("down", "-v", "--remove-orphans")

    @staticmethod
    def seed(db):
        # 常量合成数据，不复制用户账户、持仓或供应商配置。
        db.sql("postgres", """
INSERT INTO fund_portfolios VALUES ('test-user','test-portfolio','fixture',now(),now());
INSERT INTO fund_portfolio_holdings VALUES ('test-user','test-portfolio','110020',100,'actual',80,80,1,'2025-01-02',now(),now());
INSERT INTO fund_portfolio_transactions(user_id,id,portfolio_id,fund_code,transaction_type,trade_date,units,unit_price,fee,note,created_at) VALUES
 ('test-user','buy','test-portfolio','110020','buy','2025-01-02',100,1,0,'fixture','2025-01-02'),
 ('test-user','sell','test-portfolio','110020','sell','2025-02-03',20,1.5,0,'fixture','2025-02-03');
INSERT INTO fund_nav_snapshots(fund_code,nav_date,unit_nav,source,fetched_at) VALUES ('110020','2025-02-04',1.8,'fixture',now());
INSERT INTO fund_report_documents(id,fund_code,period,title,published_at,source_url,content_hash,pages,facts,created_at)
 VALUES ('test-report','110020','2025-Q1','fixture','2025-04-01','https://example.invalid/fixture.pdf',encode(sha256(convert_to('fixture-pdf-bytes','UTF8')),'hex'),'[]','[]',now());
INSERT INTO background_jobs(user_id,id,title,job_type,status,execution_backend,current_step,total_steps,progress_message,payload,cancel_requested,attempt_count,max_retries,timeout_seconds,created_at,updated_at)
 VALUES ('test-user','old-job','fixture','portfolio_research','running','celery',1,8,'fixture','{}',false,1,0,300,now(),now());
INSERT INTO job_outbox(id,user_id,job_id,execution_version,status,available_at,dispatch_attempts,created_at,updated_at)
 VALUES ('outbox','test-user','old-job',1,'sent',now(),1,now(),now());
INSERT INTO reliable_job_states(user_id,job_id,execution_version,recovery_count,failure_retry_count,max_recoveries,checkpoint_threads,updated_at)
 VALUES ('test-user','old-job',1,0,0,2,'[]',now());
INSERT INTO agent_task_grants(id,user_id,run_id,agent_id,source,status,portfolio_ids,capabilities,policy_store_id,policy_model_id,created_at,expires_at)
 VALUES ('grant','test-user','old-job','agent','fixture','active','[]','[]','store','model',now(),now()+interval '1 day');
INSERT INTO auth_sessions VALUES ('fake-hash','test-user',now(),now()+interval '1 day',now());
INSERT INTO ai_model_calls(id,user_id,run_id,day_key,source,provider,model,status,reserved_tokens,created_at,expires_at)
 VALUES ('call','test-user','old-job','2025-02-04','fixture','fixture','fixture','unknown',100,now(),now());
CREATE TABLE checkpoint_fixture(id text PRIMARY KEY, state bytea);
INSERT INTO checkpoint_fixture VALUES ('old-job',decode('010203','hex'));
""")


if __name__ == "__main__":
    unittest.main()

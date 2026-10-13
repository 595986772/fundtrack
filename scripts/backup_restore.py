"""正式 Compose 的冷备份与隔离恢复演练，操作范围始终由显式参数决定。

不直接解析或输出 .env，由 Compose 读取指定配置；不在命令行拼数据库密码。
不启动模型或消费队列。生产备份需要
--stop-writers 授权短暂维护；恢复只创建随机命名的新项目，不能指定覆盖生产目标。
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import subprocess
import tarfile
from time import monotonic
from uuid import uuid4
from urllib.parse import urlsplit

from scripts.recovery_audit import LEDGER_TABLES, audit_ledger

ROOT = Path(__file__).resolve().parents[1]
RECOVERY_COMPOSE = ROOT / "docker-compose.recovery.yml"
FORMAT = "fundscope-logical-backup-v1"
ARTIFACTS = ("business.dump", "authorization.dump", "documents.tar.gz", "baseline.json")
# 备份业务文件，不把浏览器登录状态、.env、运行日志或临时下载也打包给恢复环境。
DATA_ROOTS = ("docs", "fund_reports", "chroma", "memory", "tasks", "schedules",
    "agent_notes", "agent_context", "social_media", "openfga.json")
WRITERS = ("proxy", "api", "worker", "openfga")


class RecoveryError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    # 备份包含用户账务和登录表，即使不含明文 API Key 也必须限制本地权限。
    with open(path, "x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)


def file_hash(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def data_root_metadata(value):
    """只接受普通 UID/GID/权限位，不从清单拼接任意 shell 指令。"""
    if not isinstance(value, dict) or set(value) != {"uid", "gid", "mode"}:
        raise RecoveryError("缺少资料卷根目录权限信息")
    if any(type(number) is not int or number < 0 for number in value.values()) or value["mode"] > 0o777:
        raise RecoveryError("资料卷权限信息无效")
    return value


def command(args, *, env=None, output=None, input_file=None, input_bytes=None, timeout=900):
    """二进制 dump 直接落盘，不把大文件放到内存；错误只输出步骤名，不回显凭据。"""
    try:
        result = subprocess.run(args, env=env, stdout=output or subprocess.PIPE,
            stdin=input_file, input=input_bytes, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError(f"运行外部工具失败：{type(exc).__name__}") from None
    if result.returncode:
        raise RecoveryError(f"{args[0]} 操作失败（退出码 {result.returncode}）；检查容器状态，未把原始错误正文写入报告")
    return result.stdout or b""


class Compose:
    """只围绕现有 Compose 服务操作，数据库凭据在其自身容器内读取。"""
    def __init__(self, path, project, env_file=None, env=None):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,80}", project):
            raise RecoveryError("Compose 项目名无效")
        self.project, self.env = project, env
        self.base = ["docker", "compose", "-f", str(Path(path).resolve()), "-p", project]
        if env_file:
            self.base += ["--env-file", str(Path(env_file).resolve())]

    def run(self, *args, **kwargs):
        return command([*self.base, *args], env=self.env, **kwargs)

    def sql(self, service, query):
        # psql 不加载用户启动文件；固定时区使恢复前后的时间戳指纹一致。
        shell = 'export PGPASSWORD="$POSTGRES_PASSWORD" PGOPTIONS="-c timezone=UTC"; exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"'
        return self.run("exec", "-T", service, "sh", "-c", shell, "psql", "-c", query).decode().strip()

    def dump(self, service, path):
        with open(path, "xb") as stream:
            os.chmod(path, 0o600)
            self.run("exec", "-T", service, "sh", "-c",
                'export PGPASSWORD="$POSTGRES_PASSWORD"; exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-acl', output=stream)

    def restore(self, service, path):
        with open(path, "rb") as stream:
            self.run("exec", "-T", service, "sh", "-c",
                'export PGPASSWORD="$POSTGRES_PASSWORD"; exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error --single-transaction --no-owner --no-acl', input_file=stream)


def quote_identifier(value):
    return '"' + value.replace('"', '""') + '"'


def inventory(database, service):
    """比较所有用户表的行数和内容指纹，兼顾 OpenFGA、Checkpoint 与费用账本。

    在数据库内计算每行 SHA-256，只导出哈希，不把密码哈希、Prompt 等抄进核对报告。
    排序后再做表级哈希，恢复后物理行顺序改变也不影响结果。
    """
    listing = database.sql(service, "SELECT COALESCE(json_agg(t), '[]'::json) FROM (SELECT schemaname, tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema') ORDER BY schemaname, tablename) t")
    tables = {}
    for table in json.loads(listing):
        schema, name = table["schemaname"], table["tablename"]
        qualified = f"{quote_identifier(schema)}.{quote_identifier(name)}"
        hashes = database.sql(service, f"COPY (SELECT encode(sha256(convert_to(row_to_json(t)::text,'UTF8')),'hex') FROM {qualified} t ORDER BY 1) TO STDOUT")
        tables[f"{schema}.{name}"] = {"rows": len(hashes.splitlines()) if hashes else 0,
            "sha256": hashlib.sha256(hashes.encode()).hexdigest()}
    sequences = {}
    for sequence in json.loads(database.sql(service, "SELECT COALESCE(json_agg(t), '[]'::json) FROM (SELECT sequence_schema, sequence_name FROM information_schema.sequences) t")):
        schema, name = sequence["sequence_schema"], sequence["sequence_name"]
        sequences[f"{schema}.{name}"] = database.sql(service,
            f"SELECT row_to_json(t) FROM (SELECT last_value,is_called FROM {quote_identifier(schema)}.{quote_identifier(name)}) t")
    return {"tables": tables, "sequences": sequences}


def read_rows(database, table, suffix=""):
    return json.loads(database.sql("postgres", f"SELECT COALESCE(json_agg(t), '[]'::json) FROM (SELECT * FROM {quote_identifier(table)} {suffix}) t"))


def business_audit(database):
    tables = {table: read_rows(database, table) for table in LEDGER_TABLES}
    navs = json.loads(database.sql("postgres", "SELECT COALESCE(json_agg(t),'[]'::json) FROM (SELECT DISTINCT ON (fund_code) fund_code,nav_date,unit_nav FROM fund_nav_snapshots ORDER BY fund_code,nav_date DESC) t"))
    ledger = audit_ledger(tables, {r["fund_code"]: r for r in navs})
    # 恢复后不能相信旧租约/旧授权仍有效；这里只清点，真正隔离在单独事务中完成。
    outstanding = json.loads(database.sql("postgres", "SELECT COALESCE(json_agg(t),'[]'::json) FROM (SELECT id,job_type,status FROM background_jobs WHERE status NOT IN ('completed','failed','cancelled') ORDER BY id) t"))
    uncertain = database.sql("postgres", "SELECT count(*) FROM ai_model_calls WHERE status IN ('in_flight','unknown','reserved')")
    return {"ledger": ledger, "unfinished_jobs": outstanding, "usage_needs_review_count": int(uncertain)}


def archive_inventory(path):
    """恢复前校验路径和内容；拒绝软硬链接、设备文件和穿越目录，绝不盲目解压。"""
    result = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] not in DATA_ROOTS:
                raise RecoveryError("资料归档包含非业务目录或非法路径")
            if not member.isdir() and not member.isfile():
                raise RecoveryError("资料归档不允许链接或特殊文件")
            if member.isfile():
                key = name.as_posix()
                if key in result:
                    raise RecoveryError("资料归档包含重复文件")
                with archive.extractfile(member) as stream:
                    result[key] = {"bytes": member.size, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
    return result


def verify_bundle(directory):
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink():
        raise RecoveryError("备份清单不能是链接")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT or manifest.get("status") != "complete" or set(manifest.get("artifacts", {})) != set(ARTIFACTS):
        raise RecoveryError("备份不完整或格式不受支持")
    data_root_metadata(manifest.get("data_root"))
    for name in ARTIFACTS:
        path = directory / name
        if path.is_symlink() or not path.is_file() or file_hash(path) != manifest["artifacts"][name]["sha256"]:
            raise RecoveryError(f"备份文件校验失败：{name}")
    if archive_inventory(directory / "documents.tar.gz") != manifest["documents"]:
        raise RecoveryError("原始资料清单不匹配")
    return manifest


def preflight(source):
    """只支持正式 Compose 的共享数据卷布局，不能默默漏掉容器层中的季报。"""
    volumes, images = [], {}
    for service in ("api", "worker"):
        ids = source.run("ps", "-aq", service).decode().split()
        if len(ids) != 1:
            raise RecoveryError("本版备份要求一个 API 和一个 Worker 容器；请检查正式 Compose")
        info = json.loads(command(["docker", "inspect", ids[0]]))[0]
        values = dict(item.split("=", 1) for item in info["Config"].get("Env", []) if "=" in item)
        # 不能只看到 postgres 容器就备份它：必须确认应用实际使用的正是这个库。
        database_url = urlsplit(values.get("DATABASE_URL", ""))
        if (database_url.scheme.split("+")[0] != "postgresql" or database_url.hostname != "postgres"
                or database_url.path != "/fundscope" or database_url.port not in {None, 5432}):
            raise RecoveryError("应用数据库不符合正式 Compose 布局，拒绝备份无关的数据库")
        expected = {"CHROMA_PERSIST_DIR": "/app/data/chroma", "FUND_REPORT_STORAGE_DIR": "/app/data/fund_reports"}
        if any(values.get(key) != value for key, value in expected.items()):
            raise RecoveryError("原始资料路径未固定到共享卷；先按恢复说明迁移旧资料并更新正式配置")
        if (values.get("LANGGRAPH_CHECKPOINT_DATABASE_URL") or values.get("DATABASE_URL")) != values.get("DATABASE_URL"):
            raise RecoveryError("本版备份不支持独立 Checkpoint 数据库")
        if values.get("LANGGRAPH_CHECKPOINT_BACKEND", "auto") not in {"auto", "postgres"}:
            raise RecoveryError("正式备份要求 Checkpoint 持久化在业务 PostgreSQL 中")
        for key, paths in {"OPENFGA_CONFIG_PATH": {"data/openfga.json", "/app/data/openfga.json"},
                "SOCIAL_MEDIA_DIR": {"data/social_media", "/app/data/social_media"}}.items():
            if key in values and values[key] not in paths:
                raise RecoveryError(f"{key} 位于备份范围外，请使用正式共享卷布局")
        mounts = [m for m in info["Mounts"] if m["Destination"] == "/app/data" and m["Type"] == "volume"]
        if len(mounts) != 1:
            raise RecoveryError("/app/data 必须使用命名卷")
        volumes.append(mounts[0]["Name"])
        images[service] = info["Image"]
    if volumes[0] != volumes[1]:
        raise RecoveryError("API 与 Worker 没有共享同一份资料卷")
    for service in ("postgres", "openfga-postgres"):
        if int(source.sql(service, "SHOW server_version_num")) // 10000 != 16:
            raise RecoveryError("隔离恢复镜像为 PostgreSQL 16，源库版本不匹配")
    return volumes[0], images


@contextmanager
def maintenance_lock(project):
    """同一检出目录只允许一个维护操作，防止第二次备份提前重启写入服务。"""
    directory = ROOT / "backups"
    directory.mkdir(mode=0o700, exist_ok=True)
    with open(directory / f".{project}.lock", "a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecoveryError("同一项目的备份维护正在运行，请勿并发执行") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def quiesced(source):
    """只重启原本在运行的服务；失败或 Ctrl-C 也尝试结束维护，不创建新实例。"""
    running = set(source.run("ps", "--status", "running", "--services").decode().split())
    if not {"postgres", "openfga-postgres"} <= running:
        raise RecoveryError("业务库和授权库必须已运行")
    stopped = [name for name in WRITERS if name in running]
    try:
        for name in stopped:
            source.run("stop", "-t", "60", name, timeout=90)
        remaining = set(source.run("ps", "--status", "running", "--services").decode().split())
        if remaining.intersection(WRITERS):
            raise RecoveryError("写入服务未完全停止")
        yield
    finally:
        if stopped:
            source.run("start", *reversed(stopped), timeout=180)


def backup(source, destination, *, stop_writers):
    if not stop_writers:
        raise RecoveryError("需要 --stop-writers：备份期间会短暂暂停 API、Worker 和授权写入")
    volume, images = preflight(source)
    destination = Path(destination).resolve()
    if destination.is_relative_to(ROOT / "data") or destination.is_relative_to(ROOT / "app/data"):
        raise RecoveryError("备份目录不能放在被备份的业务数据目录中")
    destination.mkdir(parents=True, exist_ok=True)
    identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    partial = destination / (".partial-" + identifier)
    partial.mkdir(mode=0o700)
    started = monotonic()
    with maintenance_lock(source.project), quiesced(source):
        point = utc_now()
        baseline = {"business": inventory(source, "postgres"), "authorization": inventory(source, "openfga-postgres"),
            "audit": business_audit(source)}
        source.dump("postgres", partial / "business.dump")
        source.dump("openfga-postgres", partial / "authorization.dump")
        # PostgreSQL 镜像自带 tar，无需额外下载工具。只读挂载数据，禁止网络。
        with open(partial / "documents.tar.gz", "xb") as stream:
            os.chmod(stream.name, 0o600)
            command(["docker", "run", "--rm", "--network", "none", "--read-only",
                "--mount", f"type=volume,src={volume},dst=/data,readonly", "--entrypoint", "sh", "postgres:16-alpine",
                "-c", 'cd /data; for p do if [ -e "$p" ]; then printf "%s\\n" "$p"; fi; done | tar -czf - -T -', "archive", *DATA_ROOTS], output=stream)
        documents = archive_inventory(partial / "documents.tar.gz")
        # 只打包选定的子目录时 tar 不会保留卷根目录属主；正式镜像用 UID 10001，必须单独记录。
        permissions = command(["docker", "run", "--rm", "--network", "none", "--read-only",
            "--mount", f"type=volume,src={volume},dst=/data,readonly", "--entrypoint", "stat", "postgres:16-alpine",
            "-c", "%u %g %a", "/data"]).decode().split()
        data_root = data_root_metadata({"uid": int(permissions[0]), "gid": int(permissions[1]), "mode": int(permissions[2], 8)})
        reports = read_rows(source, "fund_report_documents")
        missing = [r["id"] for r in reports if f"fund_reports/{r['id']}.pdf" not in documents]
        mismatched = [r["id"] for r in reports if f"fund_reports/{r['id']}.pdf" in documents
            and documents[f"fund_reports/{r['id']}.pdf"]["sha256"] != r["content_hash"]]
        # 数据库原文仍能查询不代表原始 PDF 没丢；清单明确记录，验收不冒充通过。
        baseline["missing_report_files"] = missing
        baseline["mismatched_report_files"] = mismatched
        # 两个数据库并非同一个事务快照；停写后再次比对，发现额外写入就拒绝发布备份。
        if inventory(source, "postgres") != baseline["business"] or inventory(source, "openfga-postgres") != baseline["authorization"]:
            raise RecoveryError("维护期间数据库仍发生变化；检查其他写入者后重新备份")
        write_json(partial / "baseline.json", baseline)
        finished = utc_now()
    manifest = {"format": FORMAT, "status": "complete", "backup_id": identifier,
        "source_project": source.project, "source_images": images, "recovery_point_utc": point,
        "capture_finished_utc": finished, "backup_seconds": round(monotonic() - started, 3),
        "consistency": "application-writers-stopped", "documents": documents, "data_root": data_root,
        "excluded": ["redis-queue", "browser-login-profile", "traces", "deployment-secrets"],
        "artifacts": {name: {"sha256": file_hash(partial / name), "bytes": (partial / name).stat().st_size} for name in ARTIFACTS}}
    write_json(partial / "manifest.json", manifest)
    final = destination / identifier
    partial.rename(final)
    return final


def quarantine(database):
    """先核对原样恢复的数据，再只对恢复副本隔离旧执行，不修改基金账本/费用账本。"""
    database.sql("postgres", """BEGIN;
UPDATE background_jobs SET status='cancelled', cancel_requested=true, completed_at=now(), updated_at=now(),
 progress_message='恢复隔离：须核对外部执行结果后重新提交' WHERE status NOT IN ('completed','failed','cancelled');
UPDATE job_outbox SET status='closed', sender_token=NULL, updated_at=now() WHERE status NOT IN ('closed','consumed');
UPDATE reliable_job_states SET execution_version=execution_version+1, owner_token=NULL, lease_expires_at=NULL, updated_at=now();
UPDATE agent_task_grants SET status='revoked', revoked_at=now(), revoke_reason='恢复隔离' WHERE status='active';
DELETE FROM auth_sessions;
UPDATE agent_chat_executions SET status='failed', error='恢复隔离', updated_at=now() WHERE status='running';
UPDATE portfolio_daily_briefing_policies SET enabled=false, updated_at=now();
UPDATE portfolio_monitoring_policies SET enabled=false, updated_at=now();
COMMIT;""")
    database.run("run", "--rm", "--no-deps", "-T", "--entrypoint", "sh", "files", "-c",
        'set -eu; cd /restore-data; if [ -d schedules ]; then mv schedules recovery-disabled-schedules; fi; '
        'umask 077; printf "%s\\n" "Recovered copy: review report before enabling workloads" > RECOVERY_HOLD.json')


def restore_drill(directory, *, keep=False):
    """不接受恢复目标参数，杜绝把生产项目误传进 pg_restore。"""
    directory = Path(directory).resolve()
    manifest = verify_bundle(directory)
    project = "fundscope-restore-" + uuid4().hex[:12]
    env = {**os.environ, "RECOVERY_DB_PASSWORD": secrets.token_urlsafe(32)}
    restored = Compose(RECOVERY_COMPOSE, project, env=env)
    existing = command(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"])
    volumes = command(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"])
    if existing.strip() or volumes.strip():
        raise RecoveryError("恢复项目名已存在，拒绝复用")
    started = monotonic()
    report = {"backup_id": manifest["backup_id"], "project": project, "passed": False,
        "recovery_point_utc": manifest["recovery_point_utc"], "started_at": utc_now(),
        "workloads_started": False, "kept": keep}
    stage = "start_isolated_databases"
    try:
        restored.run("up", "-d", "--wait", "--wait-timeout", "90", "postgres", "openfga-postgres", timeout=150)
        # 一开始就封住业务启动，即使后续 pg_restore 失败，保留副本也不能直接上线。
        restored.run("run", "--rm", "--no-deps", "-T", "--entrypoint", "sh", "files", "-c",
            'umask 077; printf "%s\\n" "Recovery verification pending" > /restore-data/RECOVERY_HOLD.json')
        stage = "restore_business_database"
        restored.restore("postgres", directory / "business.dump")
        stage = "restore_authorization_database"
        restored.restore("openfga-postgres", directory / "authorization.dump")
        stage = "restore_documents"
        with open(directory / "documents.tar.gz", "rb") as stream:
            restored.run("run", "--rm", "--no-deps", "-T", "--entrypoint", "tar", "files", "-xzf", "-", "-C", "/restore-data", input_file=stream)
        root_meta = data_root_metadata(manifest["data_root"])
        restored.run("run", "--rm", "--no-deps", "-T", "--entrypoint", "sh", "files", "-c",
            'chown "$1:$2" /restore-data && chmod "$3" /restore-data', "permissions",
            str(root_meta["uid"]), str(root_meta["gid"]), format(root_meta["mode"], "o"))
        restored_permissions = restored.run("run", "--rm", "--no-deps", "-T", "--entrypoint", "stat", "files",
            "-c", "%u %g %a", "/restore-data").decode().split()
        baseline = json.loads((directory / "baseline.json").read_text())
        stage = "verify_databases_and_ledger"
        business, authorization = inventory(restored, "postgres"), inventory(restored, "openfga-postgres")
        audit = business_audit(restored)
        # 再读一遍解压后的资料，证明不只是输入包哈希正确、实际恢复也完整。
        stage = "verify_restored_documents"
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            roundtrip = Path(tmp) / "restored.tar.gz"
            with open(roundtrip, "wb") as stream:
                restored.run("run", "--rm", "--no-deps", "-T", "--entrypoint", "sh", "files", "-c",
                    'cd /restore-data; for p do if [ -e "$p" ]; then printf "%s\\n" "$p"; fi; done | tar -czf - -T -', "archive", *DATA_ROOTS, output=stream)
            files_match = archive_inventory(roundtrip) == manifest["documents"]
        checks = {"business_rows_and_sequences": business == baseline["business"],
            "authorization_rows_and_sequences": authorization == baseline["authorization"],
            "ledger_matches_backup": audit["ledger"] == baseline["audit"]["ledger"],
            "ledger_replay_consistent": audit["ledger"]["passed"],
            "valuation_complete": audit["ledger"]["valuation_complete"], "restored_documents": files_match,
            "data_root_permissions": restored_permissions == [str(root_meta["uid"]), str(root_meta["gid"]), format(root_meta["mode"], "o")],
            "report_originals_present": not baseline["missing_report_files"],
            "report_originals_match_database": not baseline["mismatched_report_files"]}
        report.update(checks=checks, audit=audit)
        stage = "quarantine_old_execution"
        quarantine(restored)
        # 隔离不能“修好”账本：必须证明其没有修改交易和财务投影。
        checks["quarantine_keeps_ledger"] = business_audit(restored)["ledger"] == audit["ledger"]
        report["quarantined"] = True
        report["passed"] = all(checks.values())
    except Exception as exc:
        # 不保存 subprocess.stderr 或连接串，失败报告仍可交给他人检查。
        report["error_type"] = type(exc).__name__
        report["failed_stage"] = stage
    finally:
        report["restore_and_verify_seconds"] = round(monotonic() - started, 3)
        report["finished_at"] = utc_now()
        path = directory / f"drill-{project}.json"
        try:
            write_json(path, report)
        finally:
            if not keep:
                # 报告落盘失败也必须清理；只销毁本函数创建的随机项目。
                restored.run("down", "-v", "--remove-orphans", timeout=120)
    return path, report


def main():
    parser = argparse.ArgumentParser(description="FundScope 逻辑备份、校验与隔离恢复")
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("backup")
    create.add_argument("--compose-file", default=str(ROOT / "docker-compose.prod.yml"))
    create.add_argument("--project", default="fundscope-prod")
    create.add_argument("--env-file", required=True)
    create.add_argument("--destination", required=True)
    create.add_argument("--stop-writers", action="store_true")
    for name in ("verify", "restore-drill"):
        item = sub.add_parser(name)
        item.add_argument("bundle")
        if name == "restore-drill":
            item.add_argument("--keep", action="store_true", help="保留隔离容器用于排查，不启动业务")
    args = parser.parse_args()
    try:
        if args.action == "backup":
            output = backup(Compose(args.compose_file, args.project, args.env_file), args.destination, stop_writers=args.stop_writers)
            print(json.dumps({"backup": str(output), "status": "complete"}, ensure_ascii=False))
        elif args.action == "verify":
            manifest = verify_bundle(args.bundle)
            print(json.dumps({"backup_id": manifest["backup_id"], "files_verified": True}))
        else:
            output, report = restore_drill(args.bundle, keep=args.keep)
            print(json.dumps({"report": str(output), "passed": report["passed"], "seconds": report["restore_and_verify_seconds"]}))
            return 0 if report["passed"] else 1
    except (RecoveryError, OSError, ValueError, tarfile.TarError) as exc:
        print(f"操作未完成：{str(exc) if isinstance(exc, RecoveryError) else type(exc).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

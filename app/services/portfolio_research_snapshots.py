"""组合研究不可变输入快照。

LangGraph checkpoint 回答“工作流运行到了哪里”，但它不会自动保证不同节点读取的是
同一批业务数据。本模块在图首次执行前保存一份完整输入包，并只提供 create/get：没有
update，也没有“刷新当前快照”。因此同一个 Job 重试、Worker 重启或 checkpoint 恢复
时，只能继续消费原来的 ``snapshot_id``。

第一版冻结的是五个研究分支交给模型汇合层的结构化输入，而不是复制整套基金数据库。
这样既能保证最终报告可回放，也不会把日频曲线和新闻全文重复写入快照表。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
import math
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import Database, runtime_database
from app.models import PortfolioResearchInputSnapshotModel
from app.services.agent_authorization import guard_agent_portfolio


SNAPSHOT_SCHEMA_VERSION = "portfolio-research-input-v1"
REQUIRED_SNAPSHOT_SECTIONS = frozenset(
    {
        "portfolio",
        "assessment",
        "quant_risk",
        "performance_snapshot",
        "market_context",
        "decision_history",
    }
)


class PortfolioResearchSnapshotError(RuntimeError):
    """快照不存在、身份不匹配或内容已经损坏。"""


class PortfolioResearchSnapshotStore(Protocol):
    """研究图只依赖这三个动作，测试可以换成内存实现。"""

    def create_once(
        self,
        *,
        run_id: str,
        user_id: str,
        portfolio_id: str,
        period_months: int,
        graph_version: str,
        capture_started_at: datetime,
        source_manifest: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...

    def get_for_run(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> dict[str, Any] | None: ...

    def get(
        self,
        *,
        user_id: str,
        snapshot_id: str,
    ) -> dict[str, Any]: ...


def _json_value(value: Any) -> Any:
    """把 Service 输出规范成稳定、可哈希、可写 SQL JSON 的值。"""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        # 金融库偶尔会在样本不足时产生 nan/inf；快照不能保存非标准 JSON。
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_value(item) for item in value), key=str)
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    return str(value)


def _content_hash(value: Any) -> str:
    """同一份规范化 JSON 始终得到同一个 SHA-256。"""

    encoded = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _prepare_snapshot(
    *,
    snapshot_id: str,
    run_id: str,
    user_id: str,
    portfolio_id: str,
    period_months: int,
    graph_version: str,
    capture_started_at: datetime,
    captured_at: datetime,
    source_manifest: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """集中生成 SQL 和内存实现共用的不可变记录。"""

    normalized_payload = _json_value(payload)
    if not isinstance(normalized_payload, dict):
        raise PortfolioResearchSnapshotError("研究快照 payload 必须是对象。")

    required_sections = REQUIRED_SNAPSHOT_SECTIONS
    missing = sorted(required_sections.difference(normalized_payload))
    if missing:
        raise PortfolioResearchSnapshotError(
            f"研究快照缺少输入分区：{', '.join(missing)}"
        )

    normalized_manifest = _json_value(source_manifest)
    if not isinstance(normalized_manifest, dict):
        normalized_manifest = {}
    # 分区哈希让以后做“结论为何改变”时能先判断到底是哪一路输入发生变化。
    normalized_manifest["section_hashes"] = {
        key: _content_hash(normalized_payload[key])
        for key in sorted(required_sections)
    }
    normalized_manifest["capture_scope"] = "graph_node_inputs"

    return {
        "id": snapshot_id,
        "run_id": run_id,
        "user_id": user_id,
        "portfolio_id": portfolio_id,
        "period_months": period_months,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "graph_version": graph_version,
        "content_hash": _content_hash(normalized_payload),
        "capture_started_at": capture_started_at,
        "captured_at": captured_at,
        "source_manifest": normalized_manifest,
        "payload": normalized_payload,
    }


def public_snapshot_metadata(snapshot: dict[str, Any]) -> dict[str, Any]:
    """报告只公开审计元数据，不复制完整冻结输入。"""

    started = snapshot["capture_started_at"]
    captured = snapshot["captured_at"]
    if isinstance(started, str):
        started = datetime.fromisoformat(started)
    if isinstance(captured, str):
        captured = datetime.fromisoformat(captured)
    # SQLite 的 DateTime(timezone=True) 重新读出后仍可能成为 naive datetime；
    # 本项目所有持久化时间都约定为 UTC，因此在做时长计算前统一补回时区。
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    else:
        started = started.astimezone(timezone.utc)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    else:
        captured = captured.astimezone(timezone.utc)
    duration_ms = max(0.0, (captured - started).total_seconds() * 1000)
    return {
        "snapshot_id": snapshot["id"],
        "schema_version": snapshot["schema_version"],
        "graph_version": snapshot["graph_version"],
        "content_hash": snapshot["content_hash"],
        "capture_started_at": started.isoformat(),
        "captured_at": captured.isoformat(),
        "capture_duration_ms": round(duration_ms, 2),
        "source_manifest": deepcopy(snapshot["source_manifest"]),
        "immutable": True,
    }


class PortfolioResearchSnapshotService:
    """使用 SQLite/PostgreSQL 保存快照；没有更新与删除接口。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def create_once(
        self,
        *,
        run_id: str,
        user_id: str,
        portfolio_id: str,
        period_months: int,
        graph_version: str,
        capture_started_at: datetime,
        source_manifest: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """同一用户和 run_id 只保存首份输入；并发失败者读取数据库赢家。"""

        guard_agent_portfolio(user_id, portfolio_id, action="portfolio:research-run")
        existing = self.get_for_run(user_id=user_id, run_id=run_id)
        if existing is not None:
            self._validate_identity(
                existing,
                portfolio_id=portfolio_id,
                period_months=period_months,
                graph_version=graph_version,
            )
            return existing

        now = datetime.now(timezone.utc)
        prepared = _prepare_snapshot(
            snapshot_id=f"research_snapshot_{uuid4().hex[:16]}",
            run_id=run_id,
            user_id=user_id,
            portfolio_id=portfolio_id,
            period_months=period_months,
            graph_version=graph_version,
            capture_started_at=capture_started_at,
            captured_at=now,
            source_manifest=source_manifest,
            payload=payload,
        )
        model = PortfolioResearchInputSnapshotModel(**prepared)
        try:
            with self.database.session() as session, session.begin():
                session.add(model)
        except IntegrityError:
            # 两个 Worker 同时首次领取同一个 Job 时，唯一约束决定哪份快照生效。
            winner = self.get_for_run(user_id=user_id, run_id=run_id)
            if winner is None:
                raise
            self._validate_identity(
                winner,
                portfolio_id=portfolio_id,
                period_months=period_months,
                graph_version=graph_version,
            )
            return winner
        return self._to_dict(model)

    def get_for_run(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> dict[str, Any] | None:
        statement = select(PortfolioResearchInputSnapshotModel).where(
            PortfolioResearchInputSnapshotModel.user_id == user_id,
            PortfolioResearchInputSnapshotModel.run_id == run_id,
        )
        with self.database.session() as session:
            model = session.scalar(statement)
        if model is None:
            return None
        # run_id 和 snapshot_id 都不能替代组合权限，必须检查落库关联后才返回内容。
        guard_agent_portfolio(model.user_id, model.portfolio_id)
        snapshot = self._to_dict(model)
        self._verify_hash(snapshot)
        return snapshot

    def get(
        self,
        *,
        user_id: str,
        snapshot_id: str,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            model = session.get(
                PortfolioResearchInputSnapshotModel,
                {"user_id": user_id, "id": snapshot_id},
            )
        if model is None:
            raise PortfolioResearchSnapshotError(
                f"研究输入快照不存在：{snapshot_id}"
            )
        guard_agent_portfolio(model.user_id, model.portfolio_id)
        snapshot = self._to_dict(model)
        self._verify_hash(snapshot)
        return snapshot

    @staticmethod
    def _to_dict(model: PortfolioResearchInputSnapshotModel) -> dict[str, Any]:
        return {
            "id": model.id,
            "run_id": model.run_id,
            "user_id": model.user_id,
            "portfolio_id": model.portfolio_id,
            "period_months": model.period_months,
            "schema_version": model.schema_version,
            "graph_version": model.graph_version,
            "content_hash": model.content_hash,
            "capture_started_at": model.capture_started_at,
            "captured_at": model.captured_at,
            "source_manifest": deepcopy(model.source_manifest or {}),
            "payload": deepcopy(model.payload or {}),
        }

    @staticmethod
    def _validate_identity(
        snapshot: dict[str, Any],
        *,
        portfolio_id: str,
        period_months: int,
        graph_version: str,
    ) -> None:
        expected = {
            "portfolio_id": portfolio_id,
            "period_months": period_months,
            "graph_version": graph_version,
        }
        for key, value in expected.items():
            if snapshot.get(key) != value:
                raise PortfolioResearchSnapshotError(
                    f"已有研究快照的 {key} 与本次请求不一致。"
                )

    @staticmethod
    def _verify_hash(snapshot: dict[str, Any]) -> None:
        payload = snapshot.get("payload")
        if _content_hash(payload) != snapshot.get("content_hash"):
            raise PortfolioResearchSnapshotError("研究输入快照内容哈希校验失败。")
        if not isinstance(payload, dict):
            raise PortfolioResearchSnapshotError("研究输入快照 payload 已损坏。")

        # 变化审计依赖 section_hashes 判断是哪一路输入改变，因此不能只验证总哈希。
        # 即使数据库中的 source_manifest 被单独改写，也必须在读取阶段检测出来。
        expected_section_hashes = {
            section: _content_hash(payload.get(section))
            for section in sorted(REQUIRED_SNAPSHOT_SECTIONS)
        }
        actual_section_hashes = (
            (snapshot.get("source_manifest") or {}).get("section_hashes") or {}
        )
        if actual_section_hashes != expected_section_hashes:
            raise PortfolioResearchSnapshotError(
                "研究输入快照分区哈希校验失败。"
            )


class InMemoryPortfolioResearchSnapshotStore:
    """不访问项目数据库的测试实现，语义与 SQL Store 保持一致。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._by_id: dict[tuple[str, str], dict[str, Any]] = {}
        self._run_index: dict[tuple[str, str], str] = {}

    def create_once(
        self,
        *,
        run_id: str,
        user_id: str,
        portfolio_id: str,
        period_months: int,
        graph_version: str,
        capture_started_at: datetime,
        source_manifest: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        guard_agent_portfolio(user_id, portfolio_id, action="portfolio:research-run")
        with self._lock:
            existing = self.get_for_run(user_id=user_id, run_id=run_id)
            if existing is not None:
                PortfolioResearchSnapshotService._validate_identity(
                    existing,
                    portfolio_id=portfolio_id,
                    period_months=period_months,
                    graph_version=graph_version,
                )
                return existing
            prepared = _prepare_snapshot(
                snapshot_id=f"research_snapshot_{uuid4().hex[:16]}",
                run_id=run_id,
                user_id=user_id,
                portfolio_id=portfolio_id,
                period_months=period_months,
                graph_version=graph_version,
                capture_started_at=capture_started_at,
                captured_at=datetime.now(timezone.utc),
                source_manifest=source_manifest,
                payload=payload,
            )
            self._by_id[(user_id, prepared["id"])] = deepcopy(prepared)
            self._run_index[(user_id, run_id)] = prepared["id"]
            return deepcopy(prepared)

    def get_for_run(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            snapshot_id = self._run_index.get((user_id, run_id))
            if snapshot_id is None:
                return None
            return self.get(user_id=user_id, snapshot_id=snapshot_id)

    def get(
        self,
        *,
        user_id: str,
        snapshot_id: str,
    ) -> dict[str, Any]:
        with self._lock:
            snapshot = self._by_id.get((user_id, snapshot_id))
            if snapshot is None:
                raise PortfolioResearchSnapshotError(
                    f"研究输入快照不存在：{snapshot_id}"
                )
            guard_agent_portfolio(snapshot["user_id"], snapshot["portfolio_id"])
            PortfolioResearchSnapshotService._verify_hash(snapshot)
            return deepcopy(snapshot)


portfolio_research_snapshot_service = PortfolioResearchSnapshotService()

"""不可变组合研究输入快照的 SQL 边界测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import func, select

from app.database import Database
from app.models import PortfolioResearchInputSnapshotModel
from app.services.portfolio_research_snapshots import (
    PortfolioResearchSnapshotError,
    PortfolioResearchSnapshotService,
    public_snapshot_metadata,
)


def snapshot_payload(*, volatility: float = 8.5) -> dict:
    """返回快照 Store 要求的六个输入分区。"""

    return {
        "portfolio": {
            "id": "portfolio_0123456789abcdef",
            "name": "快照测试组合",
            "holdings": [{"fund_code": "110020", "weight_pct": 100.0}],
        },
        "assessment": {
            "assessment_date": "2026-09-01",
            "state_code": "balanced",
        },
        "quant_risk": {
            "period_end": "2026-09-01",
            "annualized_volatility_pct": volatility,
        },
        "performance_snapshot": {
            "period_end": "2026-09-01",
            "twr_pct": 3.2,
        },
        "market_context": {
            "data_date": "2026-09-01",
            "events": [],
        },
        "decision_history": {
            "decision_count": 0,
            "decisions": [],
        },
    }


class PortfolioResearchSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "snapshots.db"
        self.database = Database(f"sqlite:///{database_path}")
        self.service = PortfolioResearchSnapshotService(self.database)

    def tearDown(self) -> None:
        self.database.engine.dispose()
        self.temporary_directory.cleanup()

    def create(self, *, payload: dict | None = None, months: int = 12) -> dict:
        return self.service.create_once(
            run_id="research-run-1",
            user_id="chen",
            portfolio_id="portfolio_0123456789abcdef",
            period_months=months,
            graph_version="portfolio-research-v6",
            capture_started_at=datetime.now(timezone.utc),
            source_manifest={"source_dates": {"market_data_date": "2026-09-01"}},
            payload=payload or snapshot_payload(),
        )

    def test_same_run_reuses_first_payload_and_public_metadata_is_bounded(self) -> None:
        first = self.create()
        replayed = self.create(payload=snapshot_payload(volatility=99.0))

        self.assertEqual(replayed["id"], first["id"])
        self.assertEqual(replayed["content_hash"], first["content_hash"])
        self.assertEqual(
            replayed["payload"]["quant_risk"]["annualized_volatility_pct"],
            8.5,
        )
        self.assertEqual(
            len(replayed["source_manifest"]["section_hashes"]),
            6,
        )

        metadata = public_snapshot_metadata(replayed)
        self.assertTrue(metadata["immutable"])
        self.assertNotIn("payload", metadata)
        self.assertEqual(metadata["content_hash"], first["content_hash"])

        with self.database.session() as session:
            row_count = session.scalar(
                select(func.count()).select_from(
                    PortfolioResearchInputSnapshotModel
                )
            )
        self.assertEqual(row_count, 1)

    def test_snapshot_is_user_scoped_and_rejects_identity_reuse(self) -> None:
        snapshot = self.create()

        with self.assertRaisesRegex(
            PortfolioResearchSnapshotError,
            "不存在",
        ):
            self.service.get(
                user_id="another-user",
                snapshot_id=snapshot["id"],
            )
        with self.assertRaisesRegex(
            PortfolioResearchSnapshotError,
            "period_months",
        ):
            self.create(months=36)

    def test_public_metadata_normalizes_sqlite_naive_and_utc_datetimes(self) -> None:
        snapshot = self.create()
        reloaded = self.service.get(user_id="chen", snapshot_id=snapshot["id"])
        # 模拟跨进程重用：SQLite 读出的 started 可能没有时区，新进程生成的
        # captured 则带 UTC。公开元数据必须仍能安全计算采集时长。
        mixed = {
            **reloaded,
            "captured_at": datetime.now(timezone.utc),
        }

        metadata = public_snapshot_metadata(mixed)

        self.assertGreaterEqual(metadata["capture_duration_ms"], 0)
        self.assertTrue(metadata["capture_started_at"].endswith("+00:00"))
        self.assertTrue(metadata["captured_at"].endswith("+00:00"))

    def test_hash_check_detects_payload_tampering(self) -> None:
        snapshot = self.create()
        with self.database.session() as session, session.begin():
            model = session.get(
                PortfolioResearchInputSnapshotModel,
                {"user_id": "chen", "id": snapshot["id"]},
            )
            assert model is not None
            tampered = dict(model.payload)
            tampered["quant_risk"] = {
                **tampered["quant_risk"],
                "annualized_volatility_pct": 99.0,
            }
            model.payload = tampered

        with self.assertRaisesRegex(
            PortfolioResearchSnapshotError,
            "哈希校验失败",
        ):
            self.service.get(user_id="chen", snapshot_id=snapshot["id"])

    def test_hash_check_detects_section_manifest_tampering(self) -> None:
        snapshot = self.create()
        with self.database.session() as session, session.begin():
            model = session.get(
                PortfolioResearchInputSnapshotModel,
                {"user_id": "chen", "id": snapshot["id"]},
            )
            assert model is not None
            manifest = dict(model.source_manifest)
            section_hashes = dict(manifest["section_hashes"])
            section_hashes["quant_risk"] = "0" * 64
            model.source_manifest = {
                **manifest,
                "section_hashes": section_hashes,
            }

        with self.assertRaisesRegex(
            PortfolioResearchSnapshotError,
            "分区哈希校验失败",
        ):
            self.service.get(user_id="chen", snapshot_id=snapshot["id"])


if __name__ == "__main__":
    unittest.main()

"""add market hotspot snapshots

Revision ID: ab83d6f4e912
Revises: 9f4a2b6c8d10
Create Date: 2026-09-03
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ab83d6f4e912"
down_revision: Union[str, Sequence[str], None] = "9f4a2b6c8d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """保存每次情报刷新后的热点状态，供趋势比较和时间线回放。"""

    op.create_table(
        "market_hotspot_snapshots",
        sa.Column("cluster_id", sa.String(length=80), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_cutoff", sa.Date(), nullable=False),
        sa.Column("representative_event_id", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("event_start", sa.Date(), nullable=False),
        sa.Column("event_end", sa.Date(), nullable=False),
        sa.Column("impact_direction", sa.String(length=20), nullable=False),
        sa.Column("impact_horizon", sa.String(length=20), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("source_names", sa.JSON(), nullable=False),
        sa.Column("source_levels", sa.JSON(), nullable=False),
        sa.Column("affected_fund_codes", sa.JSON(), nullable=False),
        sa.Column("themes", sa.JSON(), nullable=False),
        sa.Column("evidence_items", sa.JSON(), nullable=False),
        sa.Column("heat_score", sa.Float(), nullable=False),
        sa.Column("heat_level", sa.String(length=20), nullable=False),
        sa.Column("freshness_status", sa.String(length=20), nullable=False),
        sa.Column("freshness_score", sa.Float(), nullable=False),
        sa.Column("algorithm_version", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("cluster_id", "observed_at"),
    )
    op.create_index(
        "ix_market_hotspot_snapshots_observed",
        "market_hotspot_snapshots",
        ["observed_at"],
        unique=False,
    )
    op.create_index(
        "ix_market_hotspot_snapshots_cluster_observed",
        "market_hotspot_snapshots",
        ["cluster_id", "observed_at"],
        unique=False,
    )
    op.create_index(
        "ix_market_hotspot_snapshots_heat",
        "market_hotspot_snapshots",
        ["heat_score"],
        unique=False,
    )


def downgrade() -> None:
    """删除热点历史层，不影响原始事件、Embedding 和当前聚类。"""

    op.drop_index(
        "ix_market_hotspot_snapshots_heat",
        table_name="market_hotspot_snapshots",
    )
    op.drop_index(
        "ix_market_hotspot_snapshots_cluster_observed",
        table_name="market_hotspot_snapshots",
    )
    op.drop_index(
        "ix_market_hotspot_snapshots_observed",
        table_name="market_hotspot_snapshots",
    )
    op.drop_table("market_hotspot_snapshots")

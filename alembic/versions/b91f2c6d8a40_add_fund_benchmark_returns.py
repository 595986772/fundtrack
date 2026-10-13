"""add fund benchmark returns

Revision ID: b91f2c6d8a40
Revises: a72e51c4d903
Create Date: 2026-08-31 22:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b91f2c6d8a40"
down_revision: Union[str, Sequence[str], None] = "a72e51c4d903"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """保存基金专属业绩基准日收益，以及对应的来源和覆盖元数据。"""

    op.create_table(
        "fund_benchmark_returns",
        sa.Column("fund_code", sa.String(length=6), nullable=False),
        sa.Column("benchmark_date", sa.Date(), nullable=False),
        sa.Column("daily_return_pct", sa.Float(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("fund_code", "benchmark_date"),
    )
    op.create_index(
        "ix_fund_benchmark_returns_fetched",
        "fund_benchmark_returns",
        ["fetched_at"],
        unique=False,
    )
    op.create_index(
        "ix_fund_benchmark_returns_fund_date",
        "fund_benchmark_returns",
        ["fund_code", "benchmark_date"],
        unique=False,
    )

    op.create_table(
        "fund_benchmark_syncs",
        sa.Column("fund_code", sa.String(length=6), nullable=False),
        sa.Column("benchmark_name", sa.String(length=500), nullable=False),
        sa.Column("benchmark_status", sa.String(length=20), nullable=False),
        sa.Column("methodology", sa.Text(), nullable=False),
        sa.Column("coverage_start", sa.Date(), nullable=True),
        sa.Column("coverage_end", sa.Date(), nullable=True),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=500), nullable=True),
        sa.Column("source_urls", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("fund_code"),
    )
    op.create_index(
        "ix_fund_benchmark_syncs_fetched",
        "fund_benchmark_syncs",
        ["fetched_at"],
        unique=False,
    )
    op.create_index(
        "ix_fund_benchmark_syncs_status",
        "fund_benchmark_syncs",
        ["benchmark_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fund_benchmark_syncs_status",
        table_name="fund_benchmark_syncs",
    )
    op.drop_index(
        "ix_fund_benchmark_syncs_fetched",
        table_name="fund_benchmark_syncs",
    )
    op.drop_table("fund_benchmark_syncs")
    op.drop_index(
        "ix_fund_benchmark_returns_fund_date",
        table_name="fund_benchmark_returns",
    )
    op.drop_index(
        "ix_fund_benchmark_returns_fetched",
        table_name="fund_benchmark_returns",
    )
    op.drop_table("fund_benchmark_returns")

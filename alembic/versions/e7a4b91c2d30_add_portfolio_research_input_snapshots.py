"""add portfolio research input snapshots

Revision ID: e7a4b91c2d30
Revises: f3c8d7a1e204
Create Date: 2026-09-01 16:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7a4b91c2d30"
down_revision: Union[str, Sequence[str], None] = "f3c8d7a1e204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """保存每次研究真正消费的输入，独立于 Job 和 checkpoint 生命周期。"""

    op.create_table(
        "portfolio_research_input_snapshots",
        sa.Column("user_id", sa.String(length=50), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("portfolio_id", sa.String(length=64), nullable=False),
        sa.Column("period_months", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=50), nullable=False),
        sa.Column("graph_version", sa.String(length=50), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "capture_started_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_manifest", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "id"),
        sa.UniqueConstraint(
            "user_id",
            "run_id",
            name="uq_portfolio_research_snapshots_user_run",
        ),
    )
    op.create_index(
        "ix_portfolio_research_snapshots_portfolio_captured",
        "portfolio_research_input_snapshots",
        ["user_id", "portfolio_id", "captured_at"],
        unique=False,
    )
    op.create_index(
        "ix_portfolio_research_snapshots_content_hash",
        "portfolio_research_input_snapshots",
        ["content_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_research_snapshots_content_hash",
        table_name="portfolio_research_input_snapshots",
    )
    op.drop_index(
        "ix_portfolio_research_snapshots_portfolio_captured",
        table_name="portfolio_research_input_snapshots",
    )
    op.drop_table("portfolio_research_input_snapshots")

"""add portfolio decisions

Revision ID: f3c8d7a1e204
Revises: b91f2c6d8a40
Create Date: 2026-08-31 23:10:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3c8d7a1e204"
down_revision: Union[str, Sequence[str], None] = "b91f2c6d8a40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建可冻结研究快照、支持后续复盘的投资决策日志。"""

    op.create_table(
        "portfolio_decisions",
        sa.Column("user_id", sa.String(length=50), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("portfolio_id", sa.String(length=64), nullable=False),
        sa.Column("decision_date", sa.Date(), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("thesis", sa.Text(), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("research_job_id", sa.String(length=64), nullable=True),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("review_note", sa.Text(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "id"),
    )
    op.create_index(
        "ix_portfolio_decisions_portfolio_date",
        "portfolio_decisions",
        ["user_id", "portfolio_id", "decision_date"],
        unique=False,
    )
    op.create_index(
        "ix_portfolio_decisions_target_date",
        "portfolio_decisions",
        ["user_id", "target_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_decisions_target_date",
        table_name="portfolio_decisions",
    )
    op.drop_index(
        "ix_portfolio_decisions_portfolio_date",
        table_name="portfolio_decisions",
    )
    op.drop_table("portfolio_decisions")

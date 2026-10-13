"""add fund corporate actions

Revision ID: a72e51c4d903
Revises: 4ac91e2d7b60
Create Date: 2026-08-31 20:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a72e51c4d903"
down_revision: Union[str, Sequence[str], None] = "4ac91e2d7b60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增公司行为事实表，以及区分“空数据”和“未同步”的覆盖表。"""

    op.create_table(
        "fund_corporate_actions",
        sa.Column("fund_code", sa.String(length=6), nullable=False),
        sa.Column("action_date", sa.Date(), nullable=False),
        sa.Column("action_type", sa.String(length=20), nullable=False),
        sa.Column("cash_dividend_per_unit", sa.Float(), nullable=True),
        sa.Column("split_ratio", sa.Float(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=160), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("fund_code", "action_date", "action_type"),
    )
    op.create_index(
        "ix_fund_corporate_actions_fetched",
        "fund_corporate_actions",
        ["fetched_at"],
        unique=False,
    )
    op.create_index(
        "ix_fund_corporate_actions_fund_date",
        "fund_corporate_actions",
        ["fund_code", "action_date"],
        unique=False,
    )

    op.create_table(
        "fund_corporate_action_syncs",
        sa.Column("fund_code", sa.String(length=6), nullable=False),
        sa.Column("coverage_start", sa.Date(), nullable=False),
        sa.Column("coverage_end", sa.Date(), nullable=False),
        sa.Column("action_count", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=160), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("fund_code"),
    )
    op.create_index(
        "ix_fund_corporate_action_syncs_fetched",
        "fund_corporate_action_syncs",
        ["fetched_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fund_corporate_action_syncs_fetched",
        table_name="fund_corporate_action_syncs",
    )
    op.drop_table("fund_corporate_action_syncs")
    op.drop_index(
        "ix_fund_corporate_actions_fund_date",
        table_name="fund_corporate_actions",
    )
    op.drop_index(
        "ix_fund_corporate_actions_fetched",
        table_name="fund_corporate_actions",
    )
    op.drop_table("fund_corporate_actions")

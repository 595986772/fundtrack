"""add fund disclosure holdings

Revision ID: 6e1c9c28d40a
Revises: 927df65b2a3d
Create Date: 2026-08-30 11:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6e1c9c28d40a"
down_revision: Union[str, Sequence[str], None] = "927df65b2a3d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建披露明细和同步元数据三张表。"""

    op.create_table(
        "fund_disclosure_syncs",
        sa.Column("fund_code", sa.String(length=6), nullable=False),
        sa.Column("stock_report_date", sa.Date(), nullable=True),
        sa.Column("bond_report_date", sa.Date(), nullable=True),
        sa.Column("industry_report_date", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=160), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("fund_code"),
    )
    op.create_index(
        "ix_fund_disclosure_syncs_fetched",
        "fund_disclosure_syncs",
        ["fetched_at"],
        unique=False,
    )

    op.create_table(
        "fund_industry_allocations",
        sa.Column("fund_code", sa.String(length=6), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("industry_name", sa.String(length=160), nullable=False),
        sa.Column("weight_pct", sa.Float(), nullable=False),
        sa.Column("market_value_10k", sa.Float(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=160), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("fund_code", "report_date", "industry_name"),
    )
    op.create_index(
        "ix_fund_industry_allocations_fetched",
        "fund_industry_allocations",
        ["fetched_at"],
        unique=False,
    )
    op.create_index(
        "ix_fund_industry_allocations_fund_report",
        "fund_industry_allocations",
        ["fund_code", "report_date"],
        unique=False,
    )

    op.create_table(
        "fund_security_holdings",
        sa.Column("fund_code", sa.String(length=6), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("asset_type", sa.String(length=10), nullable=False),
        sa.Column("security_code", sa.String(length=30), nullable=False),
        sa.Column("security_name", sa.String(length=160), nullable=False),
        sa.Column("weight_pct", sa.Float(), nullable=False),
        sa.Column("market_value_10k", sa.Float(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=160), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "fund_code",
            "report_date",
            "asset_type",
            "security_code",
        ),
    )
    op.create_index(
        "ix_fund_security_holdings_fetched",
        "fund_security_holdings",
        ["fetched_at"],
        unique=False,
    )
    op.create_index(
        "ix_fund_security_holdings_fund_report",
        "fund_security_holdings",
        ["fund_code", "report_date"],
        unique=False,
    )


def downgrade() -> None:
    """按依赖相反顺序删除本次新增表。"""

    op.drop_index(
        "ix_fund_security_holdings_fund_report",
        table_name="fund_security_holdings",
    )
    op.drop_index(
        "ix_fund_security_holdings_fetched",
        table_name="fund_security_holdings",
    )
    op.drop_table("fund_security_holdings")

    op.drop_index(
        "ix_fund_industry_allocations_fund_report",
        table_name="fund_industry_allocations",
    )
    op.drop_index(
        "ix_fund_industry_allocations_fetched",
        table_name="fund_industry_allocations",
    )
    op.drop_table("fund_industry_allocations")

    op.drop_index(
        "ix_fund_disclosure_syncs_fetched",
        table_name="fund_disclosure_syncs",
    )
    op.drop_table("fund_disclosure_syncs")

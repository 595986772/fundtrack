"""add ETF capital snapshots and institutional trades

Revision ID: 9f4a2b6c8d10
Revises: 7d3e8a41c2f0
Create Date: 2026-09-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9f4a2b6c8d10"
down_revision: Union[str, Sequence[str], None] = "7d3e8a41c2f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增大资金模块的两张原始事实表。"""

    op.create_table(
        "etf_capital_snapshots",
        sa.Column("etf_code", sa.String(length=6), nullable=False),
        sa.Column("data_date", sa.Date(), nullable=False),
        sa.Column("etf_name", sa.String(length=120), nullable=False),
        sa.Column("exchange", sa.String(length=10), nullable=False),
        sa.Column("topic_code", sa.String(length=40), nullable=False),
        sa.Column("topic_label", sa.String(length=60), nullable=False),
        sa.Column("topic_kind", sa.String(length=20), nullable=False),
        sa.Column("unit_price", sa.Float(), nullable=False),
        sa.Column("price_basis", sa.String(length=30), nullable=False),
        sa.Column("shares", sa.Float(), nullable=False),
        sa.Column("estimated_scale_cny", sa.Float(), nullable=False),
        sa.Column("turnover_cny", sa.Float(), nullable=True),
        sa.Column("provider_main_net_inflow_cny", sa.Float(), nullable=True),
        sa.Column("source_level", sa.String(length=1), nullable=False),
        sa.Column("source", sa.String(length=160), nullable=False),
        sa.Column("source_url", sa.String(length=1000), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("etf_code", "data_date"),
    )
    op.create_index(
        "ix_etf_capital_snapshots_date",
        "etf_capital_snapshots",
        ["data_date"],
        unique=False,
    )
    op.create_index(
        "ix_etf_capital_snapshots_topic",
        "etf_capital_snapshots",
        ["topic_code", "data_date"],
        unique=False,
    )
    op.create_index(
        "ix_etf_capital_snapshots_fetched",
        "etf_capital_snapshots",
        ["fetched_at"],
        unique=False,
    )

    op.create_table(
        "institutional_trades",
        sa.Column("stock_code", sa.String(length=10), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("stock_name", sa.String(length=120), nullable=False),
        sa.Column("close", sa.Float(), nullable=True),
        sa.Column("daily_return_pct", sa.Float(), nullable=True),
        sa.Column("buyer_count", sa.Integer(), nullable=False),
        sa.Column("seller_count", sa.Integer(), nullable=False),
        sa.Column("buy_cny", sa.Float(), nullable=False),
        sa.Column("sell_cny", sa.Float(), nullable=False),
        sa.Column("net_buy_cny", sa.Float(), nullable=False),
        sa.Column("market_turnover_cny", sa.Float(), nullable=True),
        sa.Column("net_buy_ratio_pct", sa.Float(), nullable=True),
        sa.Column("turnover_rate_pct", sa.Float(), nullable=True),
        sa.Column("listing_reason", sa.Text(), nullable=False),
        sa.Column("source_level", sa.String(length=1), nullable=False),
        sa.Column("source", sa.String(length=160), nullable=False),
        sa.Column("source_url", sa.String(length=1000), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("stock_code", "trade_date"),
    )
    op.create_index(
        "ix_institutional_trades_date",
        "institutional_trades",
        ["trade_date"],
        unique=False,
    )
    op.create_index(
        "ix_institutional_trades_fetched",
        "institutional_trades",
        ["fetched_at"],
        unique=False,
    )


def downgrade() -> None:
    """按依赖反序删除机构席位和 ETF 快照表。"""

    op.drop_index(
        "ix_institutional_trades_fetched",
        table_name="institutional_trades",
    )
    op.drop_index(
        "ix_institutional_trades_date",
        table_name="institutional_trades",
    )
    op.drop_table("institutional_trades")

    op.drop_index(
        "ix_etf_capital_snapshots_fetched",
        table_name="etf_capital_snapshots",
    )
    op.drop_index(
        "ix_etf_capital_snapshots_topic",
        table_name="etf_capital_snapshots",
    )
    op.drop_index(
        "ix_etf_capital_snapshots_date",
        table_name="etf_capital_snapshots",
    )
    op.drop_table("etf_capital_snapshots")

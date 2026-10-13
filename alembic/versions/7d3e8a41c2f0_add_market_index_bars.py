"""add market index bars

Revision ID: 7d3e8a41c2f0
Revises: 2f6e4a9c1b73
Create Date: 2026-09-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7d3e8a41c2f0"
down_revision: Union[str, Sequence[str], None] = "2f6e4a9c1b73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增按指数和交易日规范化保存的日线行情表。"""

    op.create_table(
        "market_index_bars",
        sa.Column("index_code", sa.String(length=20), nullable=False),
        sa.Column("data_date", sa.Date(), nullable=False),
        sa.Column("index_name", sa.String(length=80), nullable=False),
        sa.Column("market_segment", sa.String(length=40), nullable=False),
        sa.Column("provider_symbol", sa.String(length=20), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("index_code", "data_date"),
    )
    op.create_index(
        "ix_market_index_bars_date",
        "market_index_bars",
        ["data_date"],
        unique=False,
    )
    op.create_index(
        "ix_market_index_bars_fetched",
        "market_index_bars",
        ["fetched_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_market_index_bars_fetched", table_name="market_index_bars")
    op.drop_index("ix_market_index_bars_date", table_name="market_index_bars")
    op.drop_table("market_index_bars")

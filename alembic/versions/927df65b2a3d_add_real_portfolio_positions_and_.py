"""add real portfolio positions and transactions

Revision ID: 927df65b2a3d
Revises: 0b5f47e97943
Create Date: 2026-08-30 00:23:17.600615
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "927df65b2a3d"
down_revision: Union[str, Sequence[str], None] = "0b5f47e97943"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fund_portfolio_transactions",
        sa.Column("user_id", sa.String(length=50), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("portfolio_id", sa.String(length=64), nullable=False),
        sa.Column("fund_code", sa.String(length=6), nullable=False),
        sa.Column("transaction_type", sa.String(length=10), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("units", sa.Float(), nullable=False),
        sa.Column("unit_price", sa.Float(), nullable=False),
        sa.Column("fee", sa.Float(), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "id"),
    )
    with op.batch_alter_table(
        "fund_portfolio_transactions", schema=None
    ) as batch_op:
        batch_op.create_index(
            "ix_fund_portfolio_transactions_fund",
            ["user_id", "portfolio_id", "fund_code", "trade_date"],
            unique=False,
        )
        batch_op.create_index(
            "ix_fund_portfolio_transactions_portfolio_date",
            ["user_id", "portfolio_id", "trade_date"],
            unique=False,
        )

    with op.batch_alter_table("fund_portfolio_holdings", schema=None) as batch_op:
        # 历史组合没有交易流水，升级后明确保留为旧版目标权重模式。
        batch_op.add_column(
            sa.Column(
                "position_mode",
                sa.String(length=20),
                nullable=False,
                server_default="allocation",
            )
        )
        batch_op.add_column(sa.Column("units", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("cost_amount", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("average_cost_nav", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("first_purchase_date", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("fund_portfolio_holdings", schema=None) as batch_op:
        batch_op.drop_column("first_purchase_date")
        batch_op.drop_column("average_cost_nav")
        batch_op.drop_column("cost_amount")
        batch_op.drop_column("units")
        batch_op.drop_column("position_mode")

    with op.batch_alter_table(
        "fund_portfolio_transactions", schema=None
    ) as batch_op:
        batch_op.drop_index("ix_fund_portfolio_transactions_portfolio_date")
        batch_op.drop_index("ix_fund_portfolio_transactions_fund")

    op.drop_table("fund_portfolio_transactions")

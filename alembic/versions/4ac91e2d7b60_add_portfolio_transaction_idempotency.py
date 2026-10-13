"""add portfolio transaction idempotency

Revision ID: 4ac91e2d7b60
Revises: d8f2a61c3b74
Create Date: 2026-08-31 18:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4ac91e2d7b60"
down_revision: Union[str, Sequence[str], None] = "d8f2a61c3b74"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """历史流水保留 NULL；只有新交易 API 请求必须携带稳定业务 key。"""

    with op.batch_alter_table(
        "fund_portfolio_transactions",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column("idempotency_key", sa.String(length=100), nullable=True)
        )
        batch_op.create_index(
            "uq_fund_portfolio_transactions_idempotency",
            ["user_id", "portfolio_id", "idempotency_key"],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "fund_portfolio_transactions",
        schema=None,
    ) as batch_op:
        batch_op.drop_index("uq_fund_portfolio_transactions_idempotency")
        batch_op.drop_column("idempotency_key")

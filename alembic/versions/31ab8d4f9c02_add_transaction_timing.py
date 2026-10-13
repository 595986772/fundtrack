"""保存交易时间估算，旧流水保留 NULL。

Revision ID: 31ab8d4f9c02
Revises: b57d8e20af16
"""
from alembic import op
import sqlalchemy as sa

revision = "31ab8d4f9c02"
down_revision = "b57d8e20af16"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("fund_portfolio_transactions", sa.Column("timing", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("fund_portfolio_transactions") as batch:
        batch.drop_column("timing")

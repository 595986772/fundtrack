"""外部交易登记、不可变更正记录与精确十进制流水。

只追加表和列，不重写历史交易，也不把已有 FLOAT 假装成更高精度的原始回单。
"""
from alembic import op
import sqlalchemy as sa

revision = "aa19c8e7b602"
down_revision = "92b7d631ea04"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("fund_portfolio_transactions", sa.Column("exact_values", sa.JSON(), nullable=True))
    op.create_table("portfolio_trade_orders",
        sa.Column("user_id", sa.String(50), primary_key=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("portfolio_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("fund_code", sa.String(6), nullable=False),
        sa.Column("transaction_type", sa.String(10), nullable=False),
        sa.Column("requested_amount", sa.String(40)),
        sa.Column("requested_units", sa.String(40)),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("note", sa.String(200), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("transaction_id", sa.String(64)),
        sa.Column("confirmation_date", sa.Date()),
        sa.Column("settlement_date", sa.Date()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("uq_portfolio_trade_order_key", "portfolio_trade_orders", ["user_id", "portfolio_id", "idempotency_key"], unique=True)
    op.create_index("ix_portfolio_trade_order_state", "portfolio_trade_orders", ["user_id", "portfolio_id", "status"])
    op.create_table("portfolio_trade_order_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(50), nullable=False),
        sa.Column("portfolio_id", sa.String(64), nullable=False),
        sa.Column("order_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("from_status", sa.String(24), nullable=False),
        sa.Column("to_status", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("uq_portfolio_trade_event_key", "portfolio_trade_order_events", ["user_id", "portfolio_id", "idempotency_key"], unique=True)
    op.create_index("ix_portfolio_trade_event_order", "portfolio_trade_order_events", ["user_id", "order_id", "created_at"])
    op.create_table("portfolio_transaction_corrections",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(50), nullable=False),
        sa.Column("portfolio_id", sa.String(64), nullable=False),
        sa.Column("transaction_id", sa.String(64), nullable=False),
        sa.Column("replacement_transaction_id", sa.String(64)),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("uq_portfolio_correction_original", "portfolio_transaction_corrections", ["user_id", "transaction_id"], unique=True)
    op.create_index("uq_portfolio_correction_key", "portfolio_transaction_corrections", ["user_id", "portfolio_id", "idempotency_key"], unique=True)


def downgrade():
    for table in ("portfolio_transaction_corrections", "portfolio_trade_order_events", "portfolio_trade_orders"):
        op.drop_table(table)
    op.drop_column("fund_portfolio_transactions", "exact_values")

"""保存约束草案及不可变确认结果。"""
from alembic import op
import sqlalchemy as sa

revision = "e218f307c941"
down_revision = "d104a78e6b20"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("portfolio_allocation_drafts",
        sa.Column("user_id", sa.String(50), primary_key=True),
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("portfolio_id", sa.String(40), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("draft_hash", sa.String(64), nullable=False),
        sa.Column("draft", sa.JSON(), nullable=False),
        sa.Column("confirmation_hash", sa.String(64), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_allocation_idempotency"))
    op.create_index("ix_allocation_history", "portfolio_allocation_drafts", ["user_id", "portfolio_id", "created_at"])


def downgrade():
    op.drop_table("portfolio_allocation_drafts")

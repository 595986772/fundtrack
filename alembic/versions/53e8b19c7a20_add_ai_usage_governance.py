"""新增 AI Token 额度桶和逐次调用账本，不修改历史业务数据。"""
from alembic import op
import sqlalchemy as sa

revision = "53e8b19c7a20"
down_revision = "42b7f9d610c3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("ai_usage_buckets",
        sa.Column("user_id", sa.String(80), primary_key=True),
        sa.Column("scope_key", sa.String(100), primary_key=True),
        sa.Column("used_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("uncertain_tokens", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("ai_model_calls",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(80), nullable=False),
        sa.Column("run_id", sa.String(100), nullable=False),
        sa.Column("day_key", sa.String(20), nullable=False),
        sa.Column("source", sa.String(60), nullable=False),
        sa.Column("provider", sa.String(60), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_ai_calls_user_time", "ai_model_calls", ["user_id", "created_at"])
    op.create_index("ix_ai_calls_pending", "ai_model_calls", ["user_id", "status", "expires_at"])


def downgrade():
    op.drop_table("ai_model_calls")
    op.drop_table("ai_usage_buckets")

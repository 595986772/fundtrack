"""组合投研 Outbox 与执行租约，不回填或重跑历史任务。"""
from alembic import op
import sqlalchemy as sa

revision = "78dfe213b904"
down_revision = "53e8b19c7a20"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("reliable_job_states",
        sa.Column("user_id", sa.String(50), primary_key=True),
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("execution_version", sa.Integer(), nullable=False),
        sa.Column("owner_token", sa.String(32), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_count", sa.Integer(), nullable=False),
        sa.Column("failure_retry_count", sa.Integer(), nullable=False),
        sa.Column("max_recoveries", sa.Integer(), nullable=False),
        sa.Column("checkpoint_threads", sa.JSON(), nullable=False),
        sa.Column("last_recovery_reason", sa.String(200), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_reliable_job_states_lease_expires_at", "reliable_job_states", ["lease_expires_at"])
    op.create_table("job_outbox",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(50), nullable=False),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("execution_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sender_token", sa.String(32), nullable=True),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "job_id", "execution_version", name="uq_job_outbox_version"))
    op.create_index("ix_job_outbox_due", "job_outbox", ["status", "available_at"])


def downgrade():
    op.drop_table("job_outbox")
    op.drop_table("reliable_job_states")

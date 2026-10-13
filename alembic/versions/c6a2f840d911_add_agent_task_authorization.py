"""可撤销的 Agent 任务授权与不含业务正文的审计日志。"""

from alembic import op
import sqlalchemy as sa

revision = "c6a2f840d911"
down_revision = "aa19c8e7b602"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("agent_task_grants",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(50), nullable=False),
        sa.Column("run_id", sa.String(100), nullable=False),
        sa.Column("agent_id", sa.String(100), nullable=False),
        sa.Column("parent_grant_id", sa.String(64)),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("portfolio_ids", sa.JSON(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("policy_store_id", sa.String(64), nullable=False),
        sa.Column("policy_model_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoke_reason", sa.String(200)),
        sa.UniqueConstraint("user_id", "run_id", "agent_id", name="uq_agent_task_grant_identity"))
    op.create_index("ix_agent_task_grant_user_time", "agent_task_grants", ["user_id", "created_at"])
    op.create_index("ix_agent_task_grants_parent_grant_id", "agent_task_grants", ["parent_grant_id"])
    op.create_table("agent_authorization_audits",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(50), nullable=False),
        sa.Column("grant_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("resource_id", sa.String(100), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_agent_authz_audit_grant_time", "agent_authorization_audits", ["grant_id", "created_at"])


def downgrade():
    op.drop_table("agent_authorization_audits")
    op.drop_table("agent_task_grants")

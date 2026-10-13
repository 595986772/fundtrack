"""组合每日简报策略；历史 Job 和报告不迁移、不重跑。"""
from alembic import op
import sqlalchemy as sa

revision = "92b7d631ea04"
down_revision = "78dfe213b904"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("portfolio_daily_briefing_policies",
        sa.Column("user_id", sa.String(50), primary_key=True),
        sa.Column("portfolio_id", sa.String(64), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_error", sa.String(200), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_portfolio_daily_briefing_policies_enabled", "portfolio_daily_briefing_policies", ["enabled"])


def downgrade():
    op.drop_table("portfolio_daily_briefing_policies")

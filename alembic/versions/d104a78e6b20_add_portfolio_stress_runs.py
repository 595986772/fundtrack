"""保存压力测试冻结输入及不可变方案；不改动用户持仓。"""
from alembic import op
import sqlalchemy as sa

revision = "d104a78e6b20"
down_revision = "c93e80b2d714"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "portfolio_stress_runs",
        sa.Column("user_id", sa.String(50), primary_key=True),
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("portfolio_id", sa.String(40), nullable=False),
        sa.Column("root_id", sa.String(40), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("frozen_inputs", sa.JSON(), nullable=True),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_portfolio_stress_idempotency"),
    )
    op.create_index("ix_portfolio_stress_history", "portfolio_stress_runs", ["user_id", "portfolio_id", "created_at"])


def downgrade():
    op.drop_table("portfolio_stress_runs")

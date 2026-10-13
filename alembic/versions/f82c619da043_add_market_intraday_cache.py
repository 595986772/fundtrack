"""新增独立盘中行情缓存，不修改每日市场快照。"""
from alembic import op
import sqlalchemy as sa

revision = "f82c619da043"
down_revision = "e218f307c941"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "market_intraday_cache",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_table("market_intraday_cache")

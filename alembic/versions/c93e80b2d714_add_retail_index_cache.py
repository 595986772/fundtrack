"""新增民间指数缓存；保留现有市场数据表和全部历史数据。"""
from alembic import op
import sqlalchemy as sa

revision = "c93e80b2d714"
down_revision = "b62a1d9e4073"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "retail_index_cache",
        sa.Column("indicator_id", sa.String(40), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
    )


def downgrade():
    op.drop_table("retail_index_cache")

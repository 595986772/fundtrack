"""保留成交额采样历史，与最新快照和日线行情分表存储。"""
from alembic import op
import sqlalchemy as sa

revision = "b57d8e20af16"
down_revision = "f82c619da043"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "market_turnover_points",
        sa.Column("series_id", sa.String(40), primary_key=True),
        sa.Column("quote_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("amount_cny", sa.Float(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("security_count", sa.Integer(), nullable=True),
    )
    op.create_index("ix_market_turnover_points_trade_date", "market_turnover_points", ["trade_date"])


def downgrade():
    op.drop_index("ix_market_turnover_points_trade_date", table_name="market_turnover_points")
    op.drop_table("market_turnover_points")

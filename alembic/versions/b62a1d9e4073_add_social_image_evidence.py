"""add social image evidence and AI reports

Revision ID: b62a1d9e4073
Revises: ab83d6f4e912
"""
from alembic import op
import sqlalchemy as sa

revision = "b62a1d9e4073"
down_revision = "ab83d6f4e912"
branch_labels = None
depends_on = None


def upgrade():
    # 纯新增迁移，旧分析详情为空，由服务层兼容读取。
    op.add_column("social_signal_analyses", sa.Column("details", sa.JSON(), nullable=True))
    op.create_table("social_post_media",
        sa.Column("media_id", sa.String(64), primary_key=True),
        sa.Column("post_id", sa.String(100), nullable=False),
        sa.Column("image_index", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("storage_name", sa.String(80), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recognition", sa.JSON(), nullable=False))
    op.create_index("ix_social_post_media_post_id", "social_post_media", ["post_id"])
    op.create_table("social_signal_reports",
        sa.Column("report_id", sa.String(64), primary_key=True),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("social_signal_reports")
    op.drop_index("ix_social_post_media_post_id", table_name="social_post_media")
    op.drop_table("social_post_media")
    op.drop_column("social_signal_analyses", "details")

"""保存公开基金季报及可追溯的原文页，不改变已有持仓和知识库。

Revision ID: 42b7f9d610c3
Revises: 31ab8d4f9c02
"""
from alembic import op
import sqlalchemy as sa

revision = "42b7f9d610c3"
down_revision = "31ab8d4f9c02"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("fund_report_documents",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("fund_code", sa.String(6), nullable=False),
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("title", sa.String(250), nullable=False),
        sa.Column("published_at", sa.String(10), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("pages", sa.JSON(), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("index_version", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_fund_report_period", "fund_report_documents", ["fund_code", "period"])


def downgrade():
    op.drop_table("fund_report_documents")

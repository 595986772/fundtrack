"""add social investment signals

Revision ID: d8f2a61c3b74
Revises: c41e7d2a9f10
Create Date: 2026-08-31 10:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8f2a61c3b74"
down_revision: Union[str, Sequence[str], None] = "c41e7d2a9f10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """保存社交原文和可替换的 DeepSeek 结构化抽取结果。"""

    op.create_table(
        "social_investment_posts",
        sa.Column("post_id", sa.String(length=100), nullable=False),
        sa.Column(
            "platform",
            sa.String(length=30),
            nullable=False,
            server_default="xiaohongshu",
        ),
        sa.Column("author_id", sa.String(length=120), nullable=False),
        sa.Column("author_name", sa.String(length=160), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("content_excerpt", sa.Text(), nullable=False),
        sa.Column("source_url", sa.String(length=1200), nullable=False),
        sa.Column("matched_keyword", sa.String(length=160), nullable=False),
        sa.Column("like_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comment_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("collect_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content_hash", sa.String(length=71), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("post_id"),
        sa.UniqueConstraint(
            "content_hash",
            name="uq_social_investment_posts_content_hash",
        ),
    )
    op.create_index(
        "ix_social_investment_posts_published",
        "social_investment_posts",
        ["published_at"],
        unique=False,
    )
    op.create_index(
        "ix_social_investment_posts_author",
        "social_investment_posts",
        ["author_id", "published_at"],
        unique=False,
    )
    op.create_index(
        "ix_social_investment_posts_fetched",
        "social_investment_posts",
        ["fetched_at"],
        unique=False,
    )

    op.create_table(
        "social_signal_analyses",
        sa.Column("post_id", sa.String(length=100), nullable=False),
        sa.Column("input_hash", sa.String(length=71), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column(
            "fund_codes",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "fund_names",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "themes",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("action_scope", sa.String(length=20), nullable=False),
        sa.Column("position_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "is_self_reported",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "commercial_likelihood",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "uncertainties",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("post_id"),
    )
    op.create_index(
        "ix_social_signal_analyses_status",
        "social_signal_analyses",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_social_signal_analyses_analyzed",
        "social_signal_analyses",
        ["analyzed_at"],
        unique=False,
    )


def downgrade() -> None:
    """先删除模型派生结果，再删除对应的原始帖子。"""

    op.drop_index(
        "ix_social_signal_analyses_analyzed",
        table_name="social_signal_analyses",
    )
    op.drop_index(
        "ix_social_signal_analyses_status",
        table_name="social_signal_analyses",
    )
    op.drop_table("social_signal_analyses")

    op.drop_index(
        "ix_social_investment_posts_fetched",
        table_name="social_investment_posts",
    )
    op.drop_index(
        "ix_social_investment_posts_author",
        table_name="social_investment_posts",
    )
    op.drop_index(
        "ix_social_investment_posts_published",
        table_name="social_investment_posts",
    )
    op.drop_table("social_investment_posts")

"""add market topic keyword expansions

Revision ID: 2f6e4a9c1b73
Revises: e7a4b91c2d30
Create Date: 2026-09-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2f6e4a9c1b73"
down_revision: Union[str, Sequence[str], None] = "e7a4b91c2d30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """保存固定词典、模型补充词和最终采集词，供 API 与 Worker 共享。"""

    op.create_table(
        "market_topic_keyword_expansions",
        sa.Column("topic", sa.String(length=40), nullable=False),
        sa.Column("input_hash", sa.String(length=71), nullable=False),
        sa.Column("dictionary_terms", sa.JSON(), nullable=False),
        sa.Column("model_terms", sa.JSON(), nullable=False),
        sa.Column("final_terms", sa.JSON(), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("expanded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("topic"),
    )
    op.create_index(
        "ix_market_topic_expansions_expanded",
        "market_topic_keyword_expansions",
        ["expanded_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_market_topic_expansions_expanded",
        table_name="market_topic_keyword_expansions",
    )
    op.drop_table("market_topic_keyword_expansions")

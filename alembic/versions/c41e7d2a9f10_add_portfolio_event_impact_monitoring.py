"""add portfolio event impact monitoring

Revision ID: c41e7d2a9f10
Revises: 6e1c9c28d40a
Create Date: 2026-08-30 16:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c41e7d2a9f10"
down_revision: Union[str, Sequence[str], None] = "6e1c9c28d40a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """为监控策略和风险快照补充持仓事件归因字段。"""

    with op.batch_alter_table(
        "portfolio_monitoring_policies",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "event_impact_exposure_threshold_pct",
                sa.Float(),
                nullable=False,
                server_default="5.0",
            )
        )

    with op.batch_alter_table(
        "portfolio_risk_snapshots",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "event_impacts",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )


def downgrade() -> None:
    """移除本轮新增字段，不修改历史预警记录。"""

    with op.batch_alter_table(
        "portfolio_risk_snapshots",
        schema=None,
    ) as batch_op:
        batch_op.drop_column("event_impacts")

    with op.batch_alter_table(
        "portfolio_monitoring_policies",
        schema=None,
    ) as batch_op:
        batch_op.drop_column("event_impact_exposure_threshold_pct")

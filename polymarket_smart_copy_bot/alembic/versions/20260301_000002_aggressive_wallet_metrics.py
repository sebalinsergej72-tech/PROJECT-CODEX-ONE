"""add aggressive wallet metrics

Revision ID: 20260301_000002
Revises: 20260301_000001
Create Date: 2026-03-01 15:45:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260301_000002"
down_revision = "20260301_000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wallet_scores", sa.Column("trade_count_90d", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("wallet_scores", sa.Column("total_volume_90d", sa.Float(), nullable=False, server_default="0"))
    op.add_column("wallet_scores", sa.Column("profit_factor", sa.Float(), nullable=False, server_default="0"))
    op.add_column("wallet_scores", sa.Column("avg_position_size", sa.Float(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("wallet_scores", "avg_position_size")
    op.drop_column("wallet_scores", "profit_factor")
    op.drop_column("wallet_scores", "total_volume_90d")
    op.drop_column("wallet_scores", "trade_count_90d")

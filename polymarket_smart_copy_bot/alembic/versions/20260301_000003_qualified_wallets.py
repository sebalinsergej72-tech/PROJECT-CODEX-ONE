"""add qualified wallets discovery table

Revision ID: 20260301_000003
Revises: 20260301_000002
Create Date: 2026-03-01 19:15:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260301_000003"
down_revision = "20260301_000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "qualified_wallets",
        sa.Column("address", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("win_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("trades_90d", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("profit_factor", sa.Float(), nullable=False, server_default="0"),
        sa.Column("avg_size", sa.Float(), nullable=False, server_default="0"),
        sa.Column("niche", sa.String(length=64), nullable=True),
        sa.Column("last_trade_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("address"),
    )
    op.create_index("ix_qualified_wallets_enabled", "qualified_wallets", ["enabled"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_qualified_wallets_enabled", table_name="qualified_wallets")
    op.drop_table("qualified_wallets")

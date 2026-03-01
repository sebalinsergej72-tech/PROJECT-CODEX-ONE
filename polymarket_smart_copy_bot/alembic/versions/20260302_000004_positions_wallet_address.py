"""add wallet_address to positions for live wallet exposure accounting

Revision ID: 20260302_000004
Revises: 20260301_000003
Create Date: 2026-03-02 00:00:04.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260302_000004"
down_revision = "20260301_000003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("wallet_address", sa.String(length=128), nullable=False, server_default="unknown"),
    )
    op.create_index("ix_positions_wallet_address", "positions", ["wallet_address"], unique=False)
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.alter_column("positions", "wallet_address", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_positions_wallet_address", table_name="positions")
    op.drop_column("positions", "wallet_address")

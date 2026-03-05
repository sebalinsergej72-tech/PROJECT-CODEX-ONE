"""Add order lifecycle columns to copied_trades

New columns for order status tracking: order_id, fill_mode,
filled_size_usd, filled_price_cents, slippage_bps,
submitted_at, filled_at, ttl_expires_at.
Also widens status column from VARCHAR(16) to VARCHAR(24).

Revision ID: 20260305_000008
Revises: 20260305_000007
Create Date: 2026-03-05 21:35:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260305_000008"
down_revision = "20260305_000007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("copied_trades") as batch_op:
        batch_op.add_column(sa.Column("order_id", sa.String(256), nullable=True))
        batch_op.add_column(sa.Column("fill_mode", sa.String(16), nullable=True))
        batch_op.add_column(sa.Column("filled_size_usd", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("filled_price_cents", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("slippage_bps", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("ttl_expires_at", sa.DateTime(timezone=True), nullable=True))
        # Widen status column: VARCHAR(16) → VARCHAR(24)
        batch_op.alter_column(
            "status",
            existing_type=sa.String(16),
            type_=sa.String(24),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("copied_trades") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.String(24),
            type_=sa.String(16),
            existing_nullable=False,
        )
        batch_op.drop_column("ttl_expires_at")
        batch_op.drop_column("filled_at")
        batch_op.drop_column("submitted_at")
        batch_op.drop_column("slippage_bps")
        batch_op.drop_column("filled_price_cents")
        batch_op.drop_column("filled_size_usd")
        batch_op.drop_column("fill_mode")
        batch_op.drop_column("order_id")

"""add copied trade order lifecycle and fill tracking fields

Revision ID: 20260306_000005
Revises: 20260302_000004
Create Date: 2026-03-06 00:00:05.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260306_000005"
down_revision = "20260302_000004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("copied_trades", sa.Column("order_id", sa.String(length=128), nullable=True))
    op.add_column("copied_trades", sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("copied_trades", sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("copied_trades", sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "copied_trades",
        sa.Column("filled_quantity", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "copied_trades",
        sa.Column("filled_size_usd", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "copied_trades",
        sa.Column("filled_price_cents", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index("ix_copied_trades_order_id", "copied_trades", ["order_id"], unique=False)

    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.alter_column("copied_trades", "filled_quantity", server_default=None)
        op.alter_column("copied_trades", "filled_size_usd", server_default=None)
        op.alter_column("copied_trades", "filled_price_cents", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_copied_trades_order_id", table_name="copied_trades")
    op.drop_column("copied_trades", "filled_price_cents")
    op.drop_column("copied_trades", "filled_size_usd")
    op.drop_column("copied_trades", "filled_quantity")
    op.drop_column("copied_trades", "canceled_at")
    op.drop_column("copied_trades", "filled_at")
    op.drop_column("copied_trades", "submitted_at")
    op.drop_column("copied_trades", "order_id")

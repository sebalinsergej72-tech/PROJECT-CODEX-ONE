"""Add missing order lifecycle columns (canceled_at, filled_quantity, order_id index)

Revision ID: 20260306_000009
Revises: 20260305_000008
Create Date: 2026-03-06 08:30:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260306_000009"
down_revision = "20260305_000008"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = [c["name"] for c in insp.get_columns(table)]
    return column in cols


def _index_exists(table: str, index_name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    indexes = [idx["name"] for idx in insp.get_indexes(table)]
    return index_name in indexes


def upgrade() -> None:
    # Add canceled_at if missing
    if not _column_exists("copied_trades", "canceled_at"):
        op.add_column(
            "copied_trades",
            sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        )

    # Add filled_quantity if missing
    if not _column_exists("copied_trades", "filled_quantity"):
        op.add_column(
            "copied_trades",
            sa.Column("filled_quantity", sa.Float(), nullable=False, server_default="0"),
        )
        bind = op.get_bind()
        if bind.dialect.name != "sqlite":
            op.alter_column("copied_trades", "filled_quantity", server_default=None)

    # Add index on order_id if missing
    if not _index_exists("copied_trades", "ix_copied_trades_order_id"):
        op.create_index(
            "ix_copied_trades_order_id", "copied_trades", ["order_id"], unique=False
        )

    # Fix nullability: filled_size_usd, filled_price_cents should be NOT NULL
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        # Set NULL values to 0 before altering
        bind.execute(sa.text(
            "UPDATE copied_trades SET filled_size_usd = 0 WHERE filled_size_usd IS NULL"
        ))
        bind.execute(sa.text(
            "UPDATE copied_trades SET filled_price_cents = 0 WHERE filled_price_cents IS NULL"
        ))
        op.alter_column(
            "copied_trades", "filled_size_usd",
            existing_type=sa.Float(),
            nullable=False,
            server_default="0",
        )
        op.alter_column(
            "copied_trades", "filled_price_cents",
            existing_type=sa.Float(),
            nullable=False,
            server_default="0",
        )
        # Remove server defaults after setting NOT NULL
        op.alter_column("copied_trades", "filled_size_usd", server_default=None)
        op.alter_column("copied_trades", "filled_price_cents", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.alter_column(
            "copied_trades", "filled_price_cents",
            existing_type=sa.Float(),
            nullable=True,
        )
        op.alter_column(
            "copied_trades", "filled_size_usd",
            existing_type=sa.Float(),
            nullable=True,
        )

    if _index_exists("copied_trades", "ix_copied_trades_order_id"):
        op.drop_index("ix_copied_trades_order_id", table_name="copied_trades")

    if _column_exists("copied_trades", "filled_quantity"):
        op.drop_column("copied_trades", "filled_quantity")

    if _column_exists("copied_trades", "canceled_at"):
        op.drop_column("copied_trades", "canceled_at")

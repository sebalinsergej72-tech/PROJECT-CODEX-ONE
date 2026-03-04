"""market info cache for human-readable market names and categories

Revision ID: 20260304_000005
Revises: 20260302_000004
Create Date: 2026-03-04 00:00:05.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260304_000005"
down_revision = "20260302_000004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_info",
        sa.Column("market_id", sa.String(length=128), nullable=False),
        sa.Column("question", sa.Text(), nullable=False, server_default=""),
        sa.Column("category", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("market_id"),
    )


def downgrade() -> None:
    op.drop_table("market_info")

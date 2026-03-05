"""Re-clear market_info cache after fetch_market_info rewrite

Migration 000006 deleted the cache, but the old buggy fetch_market_info
may have immediately re-populated it with mismatched data before the fixed
code deployed.  This migration purges the cache a second time so that the
new CLOB-API-first logic re-fetches all market titles correctly.

Revision ID: 20260305_000007
Revises: 20260305_000006
Create Date: 2026-03-05 00:00:07.000000
"""
from __future__ import annotations

from alembic import op


revision = "20260305_000007"
down_revision = "20260305_000006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM market_info")


def downgrade() -> None:
    pass

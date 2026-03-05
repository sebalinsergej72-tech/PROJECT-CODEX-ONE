"""Clear corrupted market_info cache entries

All market_info rows were incorrectly populated with the same title due to a
bug in fetch_market_info that took the first item from an unfiltered list
response instead of matching by conditionId.  Deleting all rows forces correct
re-population on the next portfolio-refresh cycle.

Revision ID: 20260305_000006
Revises: 20260304_000005
Create Date: 2026-03-05 00:00:06.000000
"""
from __future__ import annotations

from alembic import op


revision = "20260305_000006"
down_revision = "20260304_000005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Purge all cached market-info rows.  They will be re-fetched automatically
    # by _enrich_with_market_info on the next portfolio refresh cycle.
    op.execute("DELETE FROM market_info")


def downgrade() -> None:
    # Nothing to restore – the cache is rebuilt from the live API on demand.
    pass

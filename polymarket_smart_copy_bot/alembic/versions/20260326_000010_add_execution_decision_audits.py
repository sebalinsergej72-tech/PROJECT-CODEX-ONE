"""Add execution decision audit table for shadow mode parity checks

Revision ID: 20260326_000010
Revises: 20260306_000009
Create Date: 2026-03-26 12:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260326_000010"
down_revision = "20260306_000009"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return table in insp.get_table_names()


def upgrade() -> None:
    if _table_exists("execution_decision_audits"):
        return

    op.create_table(
        "execution_decision_audits",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("external_trade_id", sa.String(length=256), nullable=True),
        sa.Column("wallet_address", sa.String(length=128), nullable=True),
        sa.Column("market_id", sa.String(length=128), nullable=True),
        sa.Column("token_id", sa.String(length=128), nullable=True),
        sa.Column("python_status", sa.String(length=64), nullable=True),
        sa.Column("rust_status", sa.String(length=64), nullable=True),
        sa.Column("python_reason", sa.Text(), nullable=True),
        sa.Column("rust_reason", sa.Text(), nullable=True),
        sa.Column("python_requested_price_cents", sa.Float(), nullable=True),
        sa.Column("rust_requested_price_cents", sa.Float(), nullable=True),
        sa.Column("python_slippage_bps", sa.Float(), nullable=True),
        sa.Column("rust_slippage_bps", sa.Float(), nullable=True),
        sa.Column("python_signal_count", sa.Integer(), nullable=True),
        sa.Column("rust_signal_count", sa.Integer(), nullable=True),
        sa.Column("overlap_signal_count", sa.Integer(), nullable=True),
        sa.Column("python_latency_ms", sa.Float(), nullable=True),
        sa.Column("rust_latency_ms", sa.Float(), nullable=True),
        sa.Column("decision_delta_ms", sa.Float(), nullable=True),
        sa.Column("matched", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_execution_decision_audits_stage",
        "execution_decision_audits",
        ["stage"],
        unique=False,
    )
    op.create_index(
        "ix_execution_decision_audits_external_trade_id",
        "execution_decision_audits",
        ["external_trade_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_decision_audits_wallet_address",
        "execution_decision_audits",
        ["wallet_address"],
        unique=False,
    )
    op.create_index(
        "ix_execution_decision_audits_market_id",
        "execution_decision_audits",
        ["market_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_decision_audits_token_id",
        "execution_decision_audits",
        ["token_id"],
        unique=False,
    )


def downgrade() -> None:
    if not _table_exists("execution_decision_audits"):
        return
    op.drop_index("ix_execution_decision_audits_token_id", table_name="execution_decision_audits")
    op.drop_index("ix_execution_decision_audits_market_id", table_name="execution_decision_audits")
    op.drop_index("ix_execution_decision_audits_wallet_address", table_name="execution_decision_audits")
    op.drop_index("ix_execution_decision_audits_external_trade_id", table_name="execution_decision_audits")
    op.drop_index("ix_execution_decision_audits_stage", table_name="execution_decision_audits")
    op.drop_table("execution_decision_audits")

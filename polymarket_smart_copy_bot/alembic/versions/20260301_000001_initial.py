"""initial schema

Revision ID: 20260301_000001
Revises:
Create Date: 2026-03-01 00:00:01.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260301_000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wallet_scores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("wallet_address", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=True),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("win_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("roi_30d", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_volume_30d", sa.Float(), nullable=False, server_default="0"),
        sa.Column("trade_count_30d", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("qualified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("last_trade_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wallet_address", name="uq_wallet_scores_wallet_address"),
    )
    op.create_index("ix_wallet_scores_wallet_address", "wallet_scores", ["wallet_address"], unique=False)

    op.create_table(
        "blacklisted_wallets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("wallet_address", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wallet_address", name="uq_blacklisted_wallets_wallet_address"),
    )
    op.create_index(
        "ix_blacklisted_wallets_wallet_address", "blacklisted_wallets", ["wallet_address"], unique=False
    )

    op.create_table(
        "copied_trades",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("external_trade_id", sa.String(length=256), nullable=False),
        sa.Column("wallet_address", sa.String(length=128), nullable=False),
        sa.Column("market_id", sa.String(length=128), nullable=False),
        sa.Column("token_id", sa.String(length=128), nullable=True),
        sa.Column("outcome", sa.String(length=128), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("price_cents", sa.Float(), nullable=False),
        sa.Column("size_usd", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("tx_hash", sa.String(length=256), nullable=True),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("copied_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_trade_id", name="uq_copied_trades_external_trade_id"),
    )
    op.create_index("ix_copied_trades_external_trade_id", "copied_trades", ["external_trade_id"], unique=False)
    op.create_index("ix_copied_trades_wallet_address", "copied_trades", ["wallet_address"], unique=False)
    op.create_index("ix_copied_trades_market_id", "copied_trades", ["market_id"], unique=False)

    op.create_table(
        "manual_approvals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trade_id", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=True),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["trade_id"], ["copied_trades.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.String(length=128), nullable=False),
        sa.Column("token_id", sa.String(length=128), nullable=True),
        sa.Column("outcome", sa.String(length=128), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False, server_default="0"),
        sa.Column("avg_price_cents", sa.Float(), nullable=False, server_default="0"),
        sa.Column("invested_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("current_price_cents", sa.Float(), nullable=False, server_default="0"),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("is_open", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_positions_market_id", "positions", ["market_id"], unique=False)

    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("total_equity_usd", sa.Float(), nullable=False),
        sa.Column("available_cash_usd", sa.Float(), nullable=False),
        sa.Column("exposure_usd", sa.Float(), nullable=False),
        sa.Column("daily_pnl_usd", sa.Float(), nullable=False),
        sa.Column("cumulative_pnl_usd", sa.Float(), nullable=False),
        sa.Column("open_positions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "bot_runtime_state",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("bot_runtime_state")
    op.drop_table("portfolio_snapshots")
    op.drop_index("ix_positions_market_id", table_name="positions")
    op.drop_table("positions")
    op.drop_table("manual_approvals")
    op.drop_index("ix_copied_trades_market_id", table_name="copied_trades")
    op.drop_index("ix_copied_trades_wallet_address", table_name="copied_trades")
    op.drop_index("ix_copied_trades_external_trade_id", table_name="copied_trades")
    op.drop_table("copied_trades")
    op.drop_index("ix_blacklisted_wallets_wallet_address", table_name="blacklisted_wallets")
    op.drop_table("blacklisted_wallets")
    op.drop_index("ix_wallet_scores_wallet_address", table_name="wallet_scores")
    op.drop_table("wallet_scores")

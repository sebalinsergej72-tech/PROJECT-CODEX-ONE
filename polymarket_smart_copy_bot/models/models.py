from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class Base(DeclarativeBase):
    """Base declarative model."""


class TradeStatus(str, Enum):
    PENDING = "pending"
    EXECUTED = "executed"
    SKIPPED = "skipped"
    FAILED = "failed"


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class WalletScore(Base):
    __tablename__ = "wallet_scores"
    __table_args__ = (UniqueConstraint("wallet_address", name="uq_wallet_scores_wallet_address"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    roi_30d: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_volume_30d: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trade_count_30d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trade_count_90d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_volume_90d: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    profit_factor: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_position_size: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    qualified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_trade_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class BlacklistedWallet(Base):
    __tablename__ = "blacklisted_wallets"
    __table_args__ = (UniqueConstraint("wallet_address", name="uq_blacklisted_wallets_wallet_address"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CopiedTrade(Base):
    __tablename__ = "copied_trades"
    __table_args__ = (UniqueConstraint("external_trade_id", name="uq_copied_trades_external_trade_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_trade_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    wallet_address: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    market_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    token_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[str] = mapped_column(String(128), nullable=False)
    side: Mapped[TradeSide] = mapped_column(String(8), nullable=False)
    price_cents: Mapped[float] = mapped_column(Float, nullable=False)
    size_usd: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[TradeStatus] = mapped_column(String(16), nullable=False, default=TradeStatus.PENDING.value)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    copied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    approvals: Mapped[list[ManualApproval]] = relationship(back_populates="trade")


class ManualApproval(Base):
    __tablename__ = "manual_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("copied_trades.id", ondelete="CASCADE"), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    trade: Mapped[CopiedTrade] = relationship(back_populates="approvals")


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(String(128), nullable=False, index=True, default="unknown")
    market_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    token_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[str] = mapped_column(String(128), nullable=False)
    side: Mapped[TradeSide] = mapped_column(String(8), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_price_cents: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    invested_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    current_price_cents: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unrealized_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    total_equity_usd: Mapped[float] = mapped_column(Float, nullable=False)
    available_cash_usd: Mapped[float] = mapped_column(Float, nullable=False)
    exposure_usd: Mapped[float] = mapped_column(Float, nullable=False)
    daily_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False)
    cumulative_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class BotRuntimeState(Base):
    __tablename__ = "bot_runtime_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

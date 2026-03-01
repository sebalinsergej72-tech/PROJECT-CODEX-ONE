from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from models.models import Base, utcnow


class QualifiedWallet(Base):
    """Discovered wallet candidate with computed performance metrics."""

    __tablename__ = "qualified_wallets"

    address: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trades_90d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    profit_factor: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_size: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    niche: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_trade_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.trade_executor import TradeExecutor
from core.trade_monitor import TradeIntent
from models.models import Base, Position, TradeSide


def _intent(*, side: str, price_cents: float, size_usd: float) -> TradeIntent:
    return TradeIntent(
        external_trade_id=f"ext-{side}-{price_cents}-{size_usd}",
        wallet_address="0x1111111111111111111111111111111111111111",
        wallet_score=1.0,
        wallet_win_rate=0.7,
        wallet_profit_factor=2.0,
        wallet_avg_position_size=700.0,
        market_id="mkt-1",
        token_id="tok-1",
        outcome="Yes",
        side=side,
        source_price_cents=price_cents,
        source_size_usd=size_usd,
        is_short_term=False,
    )


async def _run_with_session(callback) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with session_factory() as session:
            await callback(session)
            await session.commit()
    finally:
        await engine.dispose()


def test_upsert_position_same_side_averages() -> None:
    async def _case(session: AsyncSession) -> None:
        await TradeExecutor._upsert_position(session, _intent(side="buy", price_cents=40.0, size_usd=8.0), 8.0)
        await TradeExecutor._upsert_position(session, _intent(side="buy", price_cents=60.0, size_usd=6.0), 6.0)

        row = (await session.execute(select(Position))).scalar_one()
        assert row.is_open is True
        assert row.side == TradeSide.BUY.value
        assert row.quantity == pytest.approx(30.0, rel=1e-6)
        assert row.avg_price_cents == pytest.approx(46.6666667, rel=1e-6)
        assert row.invested_usd == pytest.approx(14.0, rel=1e-6)
        assert row.realized_pnl_usd == pytest.approx(0.0, rel=1e-6)

    asyncio.run(_run_with_session(_case))


def test_upsert_position_partial_close_long() -> None:
    async def _case(session: AsyncSession) -> None:
        await TradeExecutor._upsert_position(session, _intent(side="buy", price_cents=50.0, size_usd=10.0), 10.0)
        await TradeExecutor._upsert_position(session, _intent(side="sell", price_cents=70.0, size_usd=5.0), 5.0)

        row = (await session.execute(select(Position))).scalar_one()
        assert row.is_open is True
        assert row.side == TradeSide.BUY.value
        assert row.quantity == pytest.approx(12.857142857, rel=1e-6)
        assert row.invested_usd == pytest.approx(6.4286, rel=1e-4)
        assert row.realized_pnl_usd == pytest.approx(1.4286, rel=1e-4)

    asyncio.run(_run_with_session(_case))


def test_upsert_position_exact_close_sets_closed() -> None:
    async def _case(session: AsyncSession) -> None:
        await TradeExecutor._upsert_position(session, _intent(side="sell", price_cents=80.0, size_usd=8.0), 8.0)
        await TradeExecutor._upsert_position(session, _intent(side="buy", price_cents=60.0, size_usd=6.0), 6.0)

        row = (await session.execute(select(Position))).scalar_one()
        assert row.is_open is False
        assert row.quantity == pytest.approx(0.0, rel=1e-6)
        assert row.invested_usd == pytest.approx(0.0, rel=1e-6)
        assert row.realized_pnl_usd == pytest.approx(2.0, rel=1e-6)
        assert row.closed_at is not None

    asyncio.run(_run_with_session(_case))


def test_upsert_position_flip_after_full_close() -> None:
    async def _case(session: AsyncSession) -> None:
        await TradeExecutor._upsert_position(session, _intent(side="buy", price_cents=50.0, size_usd=10.0), 10.0)
        await TradeExecutor._upsert_position(session, _intent(side="sell", price_cents=40.0, size_usd=20.0), 20.0)

        row = (await session.execute(select(Position))).scalar_one()
        assert row.is_open is True
        assert row.side == TradeSide.SELL.value
        assert row.avg_price_cents == pytest.approx(40.0, rel=1e-6)
        assert row.quantity == pytest.approx(30.0, rel=1e-6)
        assert row.invested_usd == pytest.approx(12.0, rel=1e-6)
        assert row.realized_pnl_usd == pytest.approx(-2.0, rel=1e-6)

    asyncio.run(_run_with_session(_case))

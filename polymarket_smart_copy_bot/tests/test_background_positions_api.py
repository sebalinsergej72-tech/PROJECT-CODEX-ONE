from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.background_tasks import BackgroundOrchestrator
from models.models import Base, Position, TradeSide


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


def test_fetch_positions_prefers_account_sync_in_live_mode() -> None:
    class _DummyPortfolioTracker:
        ACCOUNT_SYNC_WALLET = "account_sync"

    async def _case(session: AsyncSession) -> None:
        orchestrator = BackgroundOrchestrator.__new__(BackgroundOrchestrator)
        orchestrator._dry_run = False
        orchestrator.portfolio_tracker = _DummyPortfolioTracker()

        session.add_all(
            [
                Position(
                    wallet_address="0xsource-wallet",
                    market_id="mkt-legacy",
                    token_id="tok-legacy",
                    outcome="Yes",
                    side=TradeSide.BUY.value,
                    quantity=10.0,
                    avg_price_cents=50.0,
                    current_price_cents=50.0,
                    invested_usd=5.0,
                    is_open=True,
                ),
                Position(
                    wallet_address="account_sync",
                    market_id="mkt-live-1",
                    token_id="tok-live-1",
                    outcome="Yes",
                    side=TradeSide.BUY.value,
                    quantity=2.0,
                    avg_price_cents=50.0,
                    current_price_cents=50.0,
                    invested_usd=1.0,
                    is_open=True,
                ),
                Position(
                    wallet_address="account_sync",
                    market_id="mkt-live-2",
                    token_id="tok-live-2",
                    outcome="No",
                    side=TradeSide.BUY.value,
                    quantity=3.0,
                    avg_price_cents=66.67,
                    current_price_cents=66.67,
                    invested_usd=2.0,
                    is_open=True,
                ),
            ]
        )
        await session.flush()

        rows = await BackgroundOrchestrator._fetch_positions(
            orchestrator,
            session,
            open_only=True,
            limit=50,
        )

        assert len(rows) == 2
        assert {row["market_id"] for row in rows} == {"mkt-live-1", "mkt-live-2"}
        assert {row["wallet_address"] for row in rows} == {"account_sync"}

    asyncio.run(_run_with_session(_case))


def test_fetch_positions_keeps_local_rows_in_dry_run() -> None:
    class _DummyPortfolioTracker:
        ACCOUNT_SYNC_WALLET = "account_sync"

    async def _case(session: AsyncSession) -> None:
        orchestrator = BackgroundOrchestrator.__new__(BackgroundOrchestrator)
        orchestrator._dry_run = True
        orchestrator.portfolio_tracker = _DummyPortfolioTracker()

        session.add_all(
            [
                Position(
                    wallet_address="0xsource-wallet",
                    market_id="mkt-legacy",
                    token_id="tok-legacy",
                    outcome="Yes",
                    side=TradeSide.BUY.value,
                    quantity=10.0,
                    avg_price_cents=50.0,
                    current_price_cents=50.0,
                    invested_usd=5.0,
                    is_open=True,
                ),
                Position(
                    wallet_address="account_sync",
                    market_id="mkt-sync",
                    token_id="tok-sync",
                    outcome="No",
                    side=TradeSide.BUY.value,
                    quantity=3.0,
                    avg_price_cents=66.67,
                    current_price_cents=66.67,
                    invested_usd=2.0,
                    is_open=True,
                ),
            ]
        )
        await session.flush()

        rows = await BackgroundOrchestrator._fetch_positions(
            orchestrator,
            session,
            open_only=True,
            limit=50,
        )

        assert len(rows) == 2
        assert {row["market_id"] for row in rows} == {"mkt-legacy", "mkt-sync"}

    asyncio.run(_run_with_session(_case))

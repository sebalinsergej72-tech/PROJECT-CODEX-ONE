from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

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


def test_count_open_positions_matches_live_positions_filters() -> None:
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
                    market_id="mkt-dust",
                    token_id="tok-dust",
                    outcome="Yes",
                    side=TradeSide.BUY.value,
                    quantity=1.0,
                    avg_price_cents=1.0,
                    current_price_cents=1.0,
                    invested_usd=0.01,
                    is_open=True,
                ),
            ]
        )
        await session.flush()

        count = await BackgroundOrchestrator._count_open_positions(orchestrator, session)

        assert count == 1

    asyncio.run(_run_with_session(_case))


def test_get_status_prefers_polymarket_positions_count_for_open_positions() -> None:
    async def _case() -> None:
        orchestrator = BackgroundOrchestrator.__new__(BackgroundOrchestrator)
        orchestrator._dry_run = False
        orchestrator._risk_mode = "aggressive"
        orchestrator._price_filter_enabled = False
        orchestrator._high_conviction_boost_enabled = True
        orchestrator._discovery_autoadd_enabled = True
        orchestrator._trading_enabled = True
        orchestrator.scheduler = type("Scheduler", (), {"running": True, "get_jobs": lambda self: []})()
        orchestrator._tracked_wallets_count = 12
        orchestrator._last_portfolio_state = type(
            "State",
            (),
            {
                "open_positions": 11,
                "total_equity_usd": 23.69,
                "positions_value_usd": 11.74,
                "exposure_usd": 11.74,
                "available_cash_usd": 11.95,
                "open_order_reserve_usd": 0.0,
                "daily_pnl_usd": 4.22,
                "daily_drawdown_pct": 0.0,
                "cumulative_pnl_usd": -45.0,
            },
        )()
        orchestrator._last_exchange_balances = {
            "positions_count": 8,
            "total_balance_usd": 23.69,
            "positions_value_usd": 11.74,
        }
        orchestrator._live_started_at = None
        orchestrator.last_wallet_refresh_at = None
        orchestrator.last_trade_scan_at = None
        orchestrator.last_trade_reconcile_at = None
        orchestrator.last_portfolio_refresh_at = None
        orchestrator.last_capital_recalc_at = None
        orchestrator.last_stale_order_cleanup_at = None
        orchestrator._last_stale_order_cleanup = {}
        orchestrator.wallet_discovery = type("WD", (), {"last_result": None})()
        orchestrator._refresh_portfolio_state_from_db = AsyncMock()
        orchestrator._get_seed_wallets_stats = AsyncMock(return_value={})
        orchestrator._iso = BackgroundOrchestrator._iso

        payload = await BackgroundOrchestrator.get_status(orchestrator)

        assert payload["open_positions"] == 8

    asyncio.run(_case())


def test_get_status_uses_in_memory_snapshot_without_db_refresh() -> None:
    async def _case() -> None:
        orchestrator = BackgroundOrchestrator.__new__(BackgroundOrchestrator)
        orchestrator._dry_run = False
        orchestrator._risk_mode = "aggressive"
        orchestrator._price_filter_enabled = False
        orchestrator._high_conviction_boost_enabled = True
        orchestrator._discovery_autoadd_enabled = True
        orchestrator._trading_enabled = True
        orchestrator.scheduler = type("Scheduler", (), {"running": True, "get_jobs": lambda self: []})()
        orchestrator._tracked_wallets_count = 3
        orchestrator._last_portfolio_state = type(
            "State",
            (),
            {
                "open_positions": 2,
                "total_equity_usd": 10.0,
                "positions_value_usd": 2.0,
                "exposure_usd": 2.0,
                "available_cash_usd": 8.0,
                "open_order_reserve_usd": 0.0,
                "daily_pnl_usd": 0.0,
                "daily_drawdown_pct": 0.0,
                "cumulative_pnl_usd": 0.0,
            },
        )()
        orchestrator._last_exchange_balances = {}
        orchestrator._live_started_at = None
        orchestrator.last_wallet_refresh_at = None
        orchestrator.last_trade_scan_at = None
        orchestrator.last_trade_reconcile_at = None
        orchestrator.last_portfolio_refresh_at = None
        orchestrator.last_capital_recalc_at = None
        orchestrator.last_stale_order_cleanup_at = None
        orchestrator._last_stale_order_cleanup = {}
        orchestrator.wallet_discovery = type("WD", (), {"last_result": None})()
        orchestrator._refresh_portfolio_state_from_db = AsyncMock()
        orchestrator._get_seed_wallets_stats = AsyncMock(return_value={"total": 99})
        orchestrator._last_seed_wallet_stats_at = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
        orchestrator._cached_seed_wallet_stats = {"total": 1}
        orchestrator._iso = BackgroundOrchestrator._iso

        await BackgroundOrchestrator.get_status(orchestrator)
        await BackgroundOrchestrator.get_status(orchestrator)

        assert orchestrator._refresh_portfolio_state_from_db.await_count == 0
        assert orchestrator._get_seed_wallets_stats.await_count == 0

    asyncio.run(_case())

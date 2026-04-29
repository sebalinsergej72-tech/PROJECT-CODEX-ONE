from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.portfolio_tracker import PortfolioTracker
from models.models import Base, BotRuntimeState, PortfolioSnapshot
from utils.helpers import utc_now


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


class _ZeroBalanceClient:
    def is_dry_run(self) -> bool:
        return False

    async def fetch_account_balance_usd(self) -> float:
        return 0.0

    async def fetch_live_account_balances(self) -> dict:
        return {
            "source": "polymarket",
            "free_balance_usd": 0.0,
            "net_free_balance_usd": 0.0,
            "open_orders_reserved_usd": 0.0,
            "positions_value_usd": 0.0,
            "total_balance_usd": 0.0,
            "positions_count": 0,
            "open_orders_count": 0,
        }


def test_recalculate_capital_base_preserves_previous_positive_equity_on_zero_api_balance() -> None:
    async def _case(session: AsyncSession) -> None:
        tracker = PortfolioTracker(_ZeroBalanceClient())
        session.add(
            PortfolioSnapshot(
                total_equity_usd=41.5108,
                available_cash_usd=41.5108,
                exposure_usd=0.0,
                daily_pnl_usd=0.0,
                cumulative_pnl_usd=-27.2089,
                open_positions=0,
                taken_at=utc_now(),
            )
        )
        session.add(BotRuntimeState(key=PortfolioTracker.CAPITAL_BASE_KEY, value="0.0"))
        await session.flush()

        capital = await tracker.recalculate_capital_base(session, risk_mode="aggressive")

        assert capital == 41.5108
        stored = (
            await session.execute(
                select(BotRuntimeState.value).where(BotRuntimeState.key == PortfolioTracker.CAPITAL_BASE_KEY)
            )
        ).scalar_one()
        assert stored == "41.5108"

    asyncio.run(_run_with_session(_case))


def test_calculate_state_uses_previous_positive_equity_when_live_total_is_zero() -> None:
    async def _case(session: AsyncSession) -> None:
        tracker = PortfolioTracker(_ZeroBalanceClient())
        session.add(
            PortfolioSnapshot(
                total_equity_usd=41.5108,
                available_cash_usd=41.5108,
                exposure_usd=0.0,
                daily_pnl_usd=0.0,
                cumulative_pnl_usd=-27.2089,
                open_positions=0,
                taken_at=utc_now(),
            )
        )
        session.add(BotRuntimeState(key=PortfolioTracker.CAPITAL_BASE_KEY, value="0.0"))
        await session.flush()

        state = await tracker.calculate_state(session, risk_mode="aggressive")

        assert state.total_equity_usd == 41.5108
        assert state.available_cash_usd == 41.5108
        assert state.reported_free_balance_usd == 41.5108

    asyncio.run(_run_with_session(_case))

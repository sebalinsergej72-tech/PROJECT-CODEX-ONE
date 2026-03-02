from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import RiskMode, settings
from core.risk_manager import PortfolioState
from data.polymarket_client import PolymarketClient
from models.models import BotRuntimeState, PortfolioSnapshot, Position, TradeSide


class PortfolioTracker:
    """Tracks open positions and persists portfolio snapshots for status/PnL."""

    CAPITAL_BASE_KEY = "capital_base_usd"
    INITIAL_CAPITAL_KEY = "initial_capital_usd"

    def __init__(self, polymarket_client: PolymarketClient) -> None:
        self.polymarket_client = polymarket_client

    async def restore_open_positions(self, session: AsyncSession) -> int:
        query = select(func.count()).select_from(Position).where(Position.is_open.is_(True))
        count = int((await session.execute(query)).scalar_one())

        base = await self._get_runtime_float(session, self.CAPITAL_BASE_KEY)
        if base is None:
            base = settings.default_starting_equity
            await self._set_runtime_float(session, self.CAPITAL_BASE_KEY, base)

        initial_capital = await self._get_runtime_float(session, self.INITIAL_CAPITAL_KEY)
        if initial_capital is None:
            await self._set_runtime_float(session, self.INITIAL_CAPITAL_KEY, settings.default_starting_equity)

        logger.info("Recovered {} open positions from DB", count)
        return count

    async def mark_to_market(self, session: AsyncSession) -> None:
        query = select(Position).where(Position.is_open.is_(True))
        positions = (await session.execute(query)).scalars().all()

        for position in positions:
            latest_price = await self.polymarket_client.fetch_market_mid_price(
                market_id=position.market_id,
                token_id=position.token_id,
            )
            if latest_price is None:
                continue

            position.current_price_cents = latest_price
            price_delta = (latest_price - position.avg_price_cents) / 100
            if position.side == TradeSide.SELL.value:
                price_delta *= -1
            position.unrealized_pnl_usd = round(price_delta * position.quantity, 4)
            position.updated_at = datetime.now(tz=timezone.utc)

    async def recalculate_capital_base(self, session: AsyncSession, risk_mode: RiskMode) -> float:
        """Recalculate base capital from API and apply auto-reinvest when enabled."""

        initial_capital = await self._get_runtime_float(session, self.INITIAL_CAPITAL_KEY)
        if initial_capital is None:
            await self._set_runtime_float(session, self.INITIAL_CAPITAL_KEY, settings.default_starting_equity)

        current_base = await self._get_runtime_float(session, self.CAPITAL_BASE_KEY)
        if current_base is None:
            current_base = settings.default_starting_equity

        api_balance = await self.polymarket_client.fetch_account_balance_usd()
        if api_balance is not None and api_balance > 0:
            # Live API balance is authoritative and should override stale runtime values.
            current_base = api_balance
        elif settings.auto_reinvest and risk_mode == "aggressive":
            # Only fallback to state-based reinvest when API balance is unavailable.
            state = await self.calculate_state(session, risk_mode=risk_mode)
            current_base = max(current_base, state.total_equity_usd)

        await self._set_runtime_float(session, self.CAPITAL_BASE_KEY, current_base)
        return current_base

    async def calculate_state(self, session: AsyncSession, risk_mode: RiskMode) -> PortfolioState:
        positions_query = select(Position).where(Position.is_open.is_(True))
        positions = (await session.execute(positions_query)).scalars().all()

        exposure = sum(position.invested_usd for position in positions)
        unrealized = sum(position.unrealized_pnl_usd for position in positions)
        realized = sum(position.realized_pnl_usd for position in positions)

        capital_base = await self._get_runtime_float(session, self.CAPITAL_BASE_KEY)
        if capital_base is None:
            capital_base = settings.default_starting_equity

        initial_capital = await self._get_runtime_float(session, self.INITIAL_CAPITAL_KEY)
        if initial_capital is None:
            initial_capital = settings.default_starting_equity

        current_delta = realized + unrealized
        total_equity = capital_base + current_delta
        cumulative_pnl = total_equity - initial_capital
        available_cash = max(total_equity - exposure, 0.0)

        daily_pnl, daily_drawdown_pct = await self._compute_daily_drawdown(session, total_equity)

        return PortfolioState(
            total_equity_usd=round(total_equity, 4),
            available_cash_usd=round(available_cash, 4),
            exposure_usd=round(exposure, 4),
            daily_pnl_usd=round(daily_pnl, 4),
            cumulative_pnl_usd=round(cumulative_pnl, 4),
            open_positions=len(positions),
            daily_drawdown_pct=round(daily_drawdown_pct, 6),
        )

    async def record_snapshot(self, session: AsyncSession, risk_mode: RiskMode) -> PortfolioState:
        state = await self.calculate_state(session, risk_mode=risk_mode)
        snapshot = PortfolioSnapshot(
            total_equity_usd=state.total_equity_usd,
            available_cash_usd=state.available_cash_usd,
            exposure_usd=state.exposure_usd,
            daily_pnl_usd=state.daily_pnl_usd,
            cumulative_pnl_usd=state.cumulative_pnl_usd,
            open_positions=state.open_positions,
        )
        session.add(snapshot)
        return state

    async def close_all_positions(self, session: AsyncSession, reason: str) -> int:
        query = select(Position).where(Position.is_open.is_(True))
        positions = (await session.execute(query)).scalars().all()

        closed = 0
        now = datetime.now(tz=timezone.utc)
        for position in positions:
            position.realized_pnl_usd += position.unrealized_pnl_usd
            position.unrealized_pnl_usd = 0.0
            position.is_open = False
            position.closed_at = now
            position.updated_at = now
            closed += 1

        if closed > 0:
            logger.warning("Closed {} positions due to {}", closed, reason)
        return closed

    async def _compute_daily_drawdown(self, session: AsyncSession, total_equity: float) -> tuple[float, float]:
        since = datetime.now(tz=timezone.utc) - timedelta(hours=24)
        query = (
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.taken_at >= since)
            .order_by(PortfolioSnapshot.taken_at.asc())
            .limit(1)
        )
        first = (await session.execute(query)).scalar_one_or_none()
        if first is None:
            return 0.0, 0.0

        daily_pnl = total_equity - first.total_equity_usd
        if daily_pnl >= 0:
            return daily_pnl, 0.0

        reference = max(first.total_equity_usd, 1.0)
        drawdown = abs(daily_pnl) / reference
        return daily_pnl, drawdown

    async def _get_runtime_float(self, session: AsyncSession, key: str) -> float | None:
        row = (await session.execute(select(BotRuntimeState).where(BotRuntimeState.key == key))).scalar_one_or_none()
        if row is None:
            return None
        try:
            return float(row.value)
        except ValueError:
            return None

    async def _set_runtime_float(self, session: AsyncSession, key: str, value: float) -> None:
        row = (await session.execute(select(BotRuntimeState).where(BotRuntimeState.key == key))).scalar_one_or_none()
        now = datetime.now(tz=timezone.utc)
        if row is None:
            session.add(BotRuntimeState(key=key, value=str(value), updated_at=now))
            return
        row.value = str(value)
        row.updated_at = now

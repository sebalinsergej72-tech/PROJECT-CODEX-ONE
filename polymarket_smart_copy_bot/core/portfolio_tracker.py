from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import RiskMode, settings
from core.risk_manager import PortfolioState
from data.polymarket_client import PolymarketClient, WalletOpenPosition
from models.models import BotRuntimeState, PortfolioSnapshot, Position, TradeSide


class PortfolioTracker:
    """Tracks open positions and persists portfolio snapshots for status/PnL."""

    CAPITAL_BASE_KEY = "capital_base_usd"
    INITIAL_CAPITAL_KEY = "initial_capital_usd"
    ACCOUNT_SYNC_WALLET = "account_sync"

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

    async def sync_account_open_positions(self, session: AsyncSession) -> tuple[int, int]:
        """Sync account open positions from Polymarket into DB.

        Returns:
            tuple[synced_new, closed_stale]
        """

        if self.polymarket_client.is_dry_run():
            return (0, 0)

        remote = await self.polymarket_client.fetch_account_open_positions(limit=200)
        if remote is None:
            logger.warning("Account position sync skipped: fetch_account_open_positions returned None (API failure)")
            return (0, 0)

        logger.debug("Account position sync: {} remote positions fetched from API", len(remote))

        remote_by_key = {self._position_key(row.market_id, row.token_id, row.outcome): row for row in remote}
        remote_market_token = {
            self._market_token_key(row.market_id, row.token_id)
            for row in remote
        }
        # Fallback lookup for positions whose token_id was not stored in DB:
        # match by (market_id, outcome) so they aren't incorrectly closed.
        remote_market_outcome = {
            f"{row.market_id.strip().lower()}|{row.outcome.strip().lower()}"
            for row in remote
        }

        local_rows = (
            await session.execute(
                select(Position).where(
                    Position.is_open.is_(True),
                )
            )
        ).scalars().all()
        local_by_key: dict[str, list[Position]] = {}
        for row in local_rows:
            key = self._position_key(row.market_id, row.token_id, row.outcome)
            local_by_key.setdefault(key, []).append(row)

        now = datetime.now(tz=timezone.utc)
        synced_new = 0
        dedup_closed = 0
        price_fallback_applied = 0

        # Run all price fallback calls concurrently (was sequential, blocking scheduler)
        positions_needing_fallback = [
            rp for rp in remote_by_key.values()
            if rp.quantity > 0 and rp.invested_usd > 0
            and (rp.current_price_cents <= 0.0001 or rp.current_value_usd <= 0.0001)
        ]
        if positions_needing_fallback:
            sem = asyncio.Semaphore(5)

            async def _do_fallback(pos: WalletOpenPosition) -> bool:
                async with sem:
                    return await self._apply_price_fallback_if_needed(pos)

            fallback_results = await asyncio.gather(
                *[_do_fallback(pos) for pos in positions_needing_fallback],
                return_exceptions=True,
            )
            price_fallback_applied = sum(
                1 for r in fallback_results if r is True
            )

        for key, remote_pos in remote_by_key.items():
            matched_rows = local_by_key.get(key, [])
            if not matched_rows:
                session.add(self._build_account_sync_position(remote_pos, now=now))
                synced_new += 1
                continue

            primary = next(
                (row for row in matched_rows if row.wallet_address == self.ACCOUNT_SYNC_WALLET),
                matched_rows[0],
            )
            self._apply_remote_snapshot(primary, remote_pos, now=now)

            # If duplicate DB rows exist for the same market/token/outcome key,
            # keep one authoritative row and close extras to avoid double counting.
            for row in matched_rows:
                if row.id == primary.id:
                    continue
                row.realized_pnl_usd = round(row.realized_pnl_usd + row.unrealized_pnl_usd, 4)
                row.unrealized_pnl_usd = 0.0
                row.is_open = False
                row.closed_at = now
                row.updated_at = now
                dedup_closed += 1

        stale_account_rows = (
            await session.execute(
                select(Position).where(
                    Position.is_open.is_(True),
                    Position.wallet_address == self.ACCOUNT_SYNC_WALLET,
                )
            )
        ).scalars().all()
        closed_stale = 0
        for row in stale_account_rows:
            key = self._position_key(row.market_id, row.token_id, row.outcome)
            if key in remote_by_key:
                continue
            # Fallback: if token_id is missing, match by market_id + outcome only
            if not row.token_id:
                mo_key = f"{row.market_id.strip().lower()}|{row.outcome.strip().lower()}"
                if mo_key in remote_market_outcome:
                    continue
            row.realized_pnl_usd = round(row.realized_pnl_usd + row.unrealized_pnl_usd, 4)
            row.unrealized_pnl_usd = 0.0
            row.is_open = False
            row.closed_at = now
            row.updated_at = now
            closed_stale += 1

        orphan_closed = 0
        copied_open_rows = (
            await session.execute(
                select(Position).where(
                    Position.is_open.is_(True),
                    Position.wallet_address != self.ACCOUNT_SYNC_WALLET,
                )
            )
        ).scalars().all()
        for row in copied_open_rows:
            mt_key = self._market_token_key(row.market_id, row.token_id)
            if mt_key in remote_market_token:
                continue
            # No fallback for copied positions: if the exact market+token key
            # doesn't match a remote position, the position is stale.
            # The account_sync row (phase 1/2) already represents the real position;
            # keeping old copied rows with token_id=None alive causes ghost positions.
            row.realized_pnl_usd = round(row.realized_pnl_usd + row.unrealized_pnl_usd, 4)
            row.unrealized_pnl_usd = 0.0
            row.is_open = False
            row.closed_at = now
            row.updated_at = now
            orphan_closed += 1

        if synced_new or closed_stale or orphan_closed or dedup_closed or price_fallback_applied:
            logger.info(
                "Account position sync applied: new_open={} closed_stale={} orphan_closed={} dedup_closed={} price_fallback={}",
                synced_new,
                closed_stale,
                orphan_closed,
                dedup_closed,
                price_fallback_applied,
            )
        return (synced_new, closed_stale + orphan_closed + dedup_closed)

    async def clear_paper_positions(self, session: AsyncSession) -> int:
        """Close and zero-out all paper positions when switching from DRY RUN to LIVE."""
        query = select(Position).where(Position.is_open.is_(True))
        positions = (await session.execute(query)).scalars().all()

        closed = 0
        now = datetime.now(tz=timezone.utc)
        for position in positions:
            position.is_open = False
            position.closed_at = now
            position.updated_at = now
            position.quantity = 0.0
            position.invested_usd = 0.0
            position.unrealized_pnl_usd = 0.0
            position.realized_pnl_usd = 0.0
            closed += 1

        if closed > 0:
            logger.info("Cleared {} paper positions before switching to LIVE", closed)
        return closed

    async def mark_to_market(self, session: AsyncSession) -> None:
        # In LIVE mode we keep PnL/prices authoritative from account position sync
        # (`currentValue` / `cashPnl` from data-api) to match Polymarket UI.
        if not self.polymarket_client.is_dry_run():
            return

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

    @classmethod
    def _apply_remote_snapshot(cls, row: Position, remote: WalletOpenPosition, *, now: datetime) -> None:
        row.quantity = max(float(remote.quantity), 0.0)
        row.avg_price_cents = max(float(remote.avg_price_cents), 0.0)
        row.current_price_cents = max(float(remote.current_price_cents), 0.0)
        row.invested_usd = max(float(remote.invested_usd), 0.0)
        row.unrealized_pnl_usd = float(remote.unrealized_pnl_usd)
        # Reset realized PnL when syncing from API: the remote data is
        # authoritative and already accounts for partial closes in
        # invested_usd / unrealized_pnl_usd.  Stale realized_pnl values
        # from previous local netting would otherwise double-count PnL.
        row.realized_pnl_usd = 0.0
        # Preserve the original wallet_address when it belongs to a real
        # copied-from wallet.  Overwriting it with ``account_sync`` would
        # prevent mirror-close reconciliation from detecting that the source
        # wallet closed a position (reconciliation ignores account_sync rows).
        if row.wallet_address in (cls.ACCOUNT_SYNC_WALLET, "unknown", ""):
            row.wallet_address = cls.ACCOUNT_SYNC_WALLET
        row.is_open = True
        row.closed_at = None
        row.updated_at = now

    async def _apply_price_fallback_if_needed(self, row: WalletOpenPosition) -> bool:
        """Patch clearly stale zero-valued marks from data-api with market fallback.

        Some positions from /positions intermittently return `curPrice=0` and
        `currentValue=0` even for active markets visible in Polymarket UI.
        In that case we fallback to market mid/last price to avoid fake -100% U-PnL.
        """

        if row.quantity <= 0 or row.invested_usd <= 0:
            return False

        # Use data-api values whenever they are non-zero.
        if row.current_price_cents > 0.0001 and row.current_value_usd > 0.0001:
            return False

        latest_price = await self.polymarket_client.fetch_market_mid_price(
            market_id=row.market_id,
            token_id=row.token_id,
        )
        if latest_price is None or latest_price <= 0:
            return False

        row.current_price_cents = latest_price
        row.current_value_usd = max((row.quantity * latest_price) / 100, 0.0)
        row.unrealized_pnl_usd = round(row.current_value_usd - row.invested_usd, 4)
        return True

    async def update_capital_base(self, session: AsyncSession, balance: float) -> None:
        """Update capital base directly from a fresh API balance value."""
        await self._set_runtime_float(session, self.CAPITAL_BASE_KEY, balance)

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

    async def reset_pnl_baseline(self, session: AsyncSession, baseline_usd: float) -> None:
        """Reset capital and PnL baseline (used when switching engine modes)."""

        baseline = max(float(baseline_usd), 0.0)
        await self._set_runtime_float(session, self.CAPITAL_BASE_KEY, baseline)
        await self._set_runtime_float(session, self.INITIAL_CAPITAL_KEY, baseline)
        # Prevent stale 24h reference snapshots from previous mode from skewing daily PnL.
        await session.execute(delete(PortfolioSnapshot))

    async def calculate_state(self, session: AsyncSession, risk_mode: RiskMode) -> PortfolioState:
        open_positions_query = select(Position).where(Position.is_open.is_(True))
        positions = (await session.execute(open_positions_query)).scalars().all()

        exposure = sum(position.invested_usd for position in positions)
        unrealized = sum(position.unrealized_pnl_usd for position in positions)

        # Sum realized PnL from ALL positions (open AND closed) so that profits
        # from closed trades are never lost from the equity calculation.
        all_realized_query = select(func.coalesce(func.sum(Position.realized_pnl_usd), 0.0))
        total_realized = float((await session.execute(all_realized_query)).scalar_one())

        capital_base = await self._get_runtime_float(session, self.CAPITAL_BASE_KEY)
        if capital_base is None:
            capital_base = settings.default_starting_equity

        initial_capital = await self._get_runtime_float(session, self.INITIAL_CAPITAL_KEY)
        if initial_capital is None:
            initial_capital = settings.default_starting_equity

        # In DRY_RUN, `capital_base` is the fixed starting equity.
        # Total equity = starting capital + all realized PnL + current unrealized PnL.
        # In LIVE mode, `capital_base` comes from CLOB collateral balance (cash component),
        # which already includes realized PnL, so we only add position market value.
        if self.polymarket_client.is_dry_run():
            current_delta = total_realized + unrealized
            total_equity = capital_base + current_delta
            available_cash = max(total_equity - exposure, 0.0)
        else:
            # Mark-to-market equity = cash collateral + current position value.
            # Current position value = invested_cost + unrealized_pnl.
            total_equity = capital_base + exposure + unrealized
            available_cash = max(capital_base, 0.0)

        cumulative_pnl = total_equity - initial_capital

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

    @classmethod
    def _position_key(cls, market_id: str, token_id: str | None, outcome: str) -> str:
        token = (token_id or "").strip().lower()
        return f"{market_id.strip().lower()}|{token}|{outcome.strip().lower()}"

    @classmethod
    def _market_token_key(cls, market_id: str, token_id: str | None) -> str:
        token = (token_id or "").strip().lower()
        return f"{market_id.strip().lower()}|{token}"

    @classmethod
    def _build_account_sync_position(cls, row: WalletOpenPosition, *, now: datetime) -> Position:
        side = TradeSide.BUY.value
        return Position(
            wallet_address=cls.ACCOUNT_SYNC_WALLET,
            market_id=row.market_id,
            token_id=row.token_id,
            outcome=row.outcome,
            side=side,
            quantity=max(float(row.quantity), 0.0),
            avg_price_cents=max(float(row.avg_price_cents), 0.0),
            invested_usd=max(float(row.invested_usd), 0.0),
            current_price_cents=max(float(row.current_price_cents), 0.0),
            unrealized_pnl_usd=float(row.unrealized_pnl_usd),
            is_open=True,
            opened_at=now,
            updated_at=now,
        )

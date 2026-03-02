from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import RiskMode
from core.portfolio_tracker import PortfolioTracker
from core.risk_manager import PortfolioState, RiskManager
from core.trade_monitor import TradeIntent
from data.polymarket_client import OrderRequest, PolymarketClient
from models.models import CopiedTrade, ManualApproval, Position, TradeSide, TradeStatus
from utils.notifications import NotificationService


class TradeExecutor:
    """Executes approved copy-trades and persists trade/position state."""

    def __init__(
        self,
        polymarket_client: PolymarketClient,
        risk_manager: RiskManager,
        notifications: NotificationService,
        portfolio_tracker: PortfolioTracker,
    ) -> None:
        self.polymarket_client = polymarket_client
        self.risk_manager = risk_manager
        self.notifications = notifications
        self.portfolio_tracker = portfolio_tracker

    async def execute_intent(
        self,
        session: AsyncSession,
        intent: TradeIntent,
        portfolio_state: PortfolioState,
        *,
        risk_mode: RiskMode,
        price_filter_enabled: bool,
        high_conviction_boost_enabled: bool,
    ) -> None:
        await self.execute_copy_trade(
            session,
            intent,
            portfolio_state,
            risk_mode=risk_mode,
            price_filter_enabled=price_filter_enabled,
            high_conviction_boost_enabled=high_conviction_boost_enabled,
        )

    async def execute_copy_trade(
        self,
        session: AsyncSession,
        intent: TradeIntent,
        portfolio_state: PortfolioState,
        *,
        risk_mode: RiskMode,
        price_filter_enabled: bool,
        high_conviction_boost_enabled: bool,
    ) -> None:
        intent_side = intent.side.lower()
        copied_trade = CopiedTrade(
            external_trade_id=intent.external_trade_id,
            wallet_address=intent.wallet_address,
            market_id=intent.market_id,
            token_id=intent.token_id,
            outcome=intent.outcome,
            side=intent_side,
            price_cents=intent.source_price_cents,
            size_usd=intent.source_size_usd,
            status=TradeStatus.PENDING.value,
            source_timestamp=datetime.now(tz=timezone.utc),
        )
        try:
            async with session.begin_nested():
                session.add(copied_trade)
                await session.flush()
        except IntegrityError as exc:
            # Cross-worker race: the same external trade can be inserted concurrently.
            if self._is_duplicate_external_trade(exc):
                logger.debug("Duplicate trade ignored: {}", intent.external_trade_id)
                return
            raise

        existing_position = await self._find_open_position(
            session,
            wallet_address=intent.wallet_address,
            market_id=intent.market_id,
            outcome=intent.outcome,
            token_id=intent.token_id,
        )
        is_mirror_close = existing_position is not None and existing_position.side != intent_side

        target_size_usd = 0.0
        wallet_multiplier = 1.0
        kelly_fraction = 0.0
        requires_manual_confirmation = False

        if is_mirror_close and existing_position is not None:
            target_size_usd = self._derive_close_size_usd(
                position=existing_position,
                source_size_usd=intent.source_size_usd,
                execution_price_cents=intent.source_price_cents,
            )
            if target_size_usd <= 0:
                copied_trade.status = TradeStatus.SKIPPED.value
                copied_trade.reason = "mirror_close_zero_size"
                logger.info("Trade {} skipped: mirror_close_zero_size", intent.external_trade_id)
                return
        else:
            wallet_current_exposure = await self._wallet_current_exposure_usd(session, intent.wallet_address)

            decision = self.risk_manager.evaluate_trade(
                source_price_cents=intent.source_price_cents,
                source_size_usd=intent.source_size_usd,
                wallet_score=intent.wallet_score,
                wallet_win_rate=intent.wallet_win_rate,
                wallet_profit_factor=intent.wallet_profit_factor,
                wallet_avg_trade_size_usd=intent.wallet_avg_position_size,
                wallet_current_exposure_usd=wallet_current_exposure,
                portfolio=portfolio_state,
                risk_mode=risk_mode,
                price_filter_enabled=price_filter_enabled,
                high_conviction_boost_enabled=high_conviction_boost_enabled,
            )

            if not decision.allowed:
                copied_trade.status = TradeStatus.SKIPPED.value
                copied_trade.reason = decision.reason
                logger.info("Trade {} skipped: {}", intent.external_trade_id, decision.reason)
                return

            target_size_usd = decision.target_size_usd
            wallet_multiplier = decision.wallet_multiplier
            kelly_fraction = decision.kelly_fraction
            requires_manual_confirmation = decision.requires_manual_confirmation

        if requires_manual_confirmation:
            approved = await self._request_manual_confirmation(session, copied_trade, target_size_usd)
            if not approved:
                copied_trade.status = TradeStatus.SKIPPED.value
                copied_trade.reason = "manual_rejection_or_timeout"
                return

        # Persist the bot-calculated intended size even if execution later fails.
        copied_trade.size_usd = target_size_usd

        result = await self.polymarket_client.place_order(
            OrderRequest(
                token_id=intent.token_id or "",
                side=intent.side,
                price_cents=intent.source_price_cents,
                size_usd=target_size_usd,
                market_id=intent.market_id,
                outcome=intent.outcome,
            )
        )

        if not result.success:
            error_text = str(result.error or "")
            if error_text.startswith("orderbook_not_found:"):
                copied_trade.status = TradeStatus.SKIPPED.value
                copied_trade.reason = "orderbook_not_found"
                logger.info(
                    "Trade {} skipped: orderbook not found for token_id={}",
                    intent.external_trade_id,
                    intent.token_id,
                )
                return
            if error_text == "insufficient_balance_allowance":
                copied_trade.status = TradeStatus.SKIPPED.value
                copied_trade.reason = "insufficient_balance_allowance"
                logger.warning("Trade {} skipped: insufficient balance/allowance", intent.external_trade_id)
                return

            copied_trade.status = TradeStatus.FAILED.value
            copied_trade.reason = result.error
            await self.notifications.send_message(
                f"[FAILED] {intent.market_id} {intent.outcome} {intent.side.upper()} | reason={result.error}"
            )
            return

        copied_trade.status = TradeStatus.EXECUTED.value
        copied_trade.tx_hash = result.tx_hash
        if is_mirror_close:
            copied_trade.reason = f"executed mirror_close mode={risk_mode}"
        else:
            copied_trade.reason = f"executed mode={risk_mode} mult={wallet_multiplier:.2f} kelly={kelly_fraction:.3f}"

        await self._upsert_position(session, intent, target_size_usd)
        await self.notifications.send_message(
            f"[EXECUTED:{risk_mode}] {intent.market_id} {intent.outcome} {intent.side.upper()} | "
            f"${target_size_usd:.2f} (mult={wallet_multiplier:.2f}, kelly={kelly_fraction:.3f})"
        )

    async def _request_manual_confirmation(
        self,
        session: AsyncSession,
        copied_trade: CopiedTrade,
        size_usd: float,
    ) -> bool:
        expires_at = datetime.now(tz=timezone.utc) + timedelta(minutes=3)
        approval = ManualApproval(
            trade_id=copied_trade.id,
            expires_at=expires_at,
        )
        session.add(approval)
        await session.flush()

        title = "Large Copy Trade Approval"
        body = (
            f"Market: {copied_trade.market_id}\n"
            f"Outcome: {copied_trade.outcome}\n"
            f"Side: {copied_trade.side.upper()}\n"
            f"Size: ${size_usd:.2f}\n"
            f"Wallet: {copied_trade.wallet_address}"
        )
        approved = await self.notifications.request_manual_approval(title=title, body=body, timeout_seconds=180)

        approval.approved = approved
        approval.approved_by = "telegram"
        approval.decision_at = datetime.now(tz=timezone.utc)

        return approved

    @staticmethod
    async def _wallet_current_exposure_usd(session: AsyncSession, wallet_address: str) -> float:
        query = (
            select(func.coalesce(func.sum(Position.invested_usd), 0.0))
            .where(Position.wallet_address == wallet_address)
            .where(Position.is_open.is_(True))
        )
        value = (await session.execute(query)).scalar_one()
        return float(value or 0.0)

    @staticmethod
    async def _find_open_position(
        session: AsyncSession,
        *,
        wallet_address: str,
        market_id: str,
        outcome: str,
        token_id: str | None,
    ) -> Position | None:
        query = select(Position).where(
            Position.wallet_address == wallet_address,
            Position.market_id == market_id,
            Position.outcome == outcome,
            Position.is_open.is_(True),
        )
        if token_id:
            query = query.where((Position.token_id == token_id) | (Position.token_id.is_(None)))
        query = query.order_by(Position.opened_at.asc()).limit(1)
        return (await session.execute(query)).scalar_one_or_none()

    @staticmethod
    def _derive_close_size_usd(*, position: Position, source_size_usd: float, execution_price_cents: float) -> float:
        exec_price = max(execution_price_cents / 100.0, 0.01)
        full_close_notional = max(position.quantity, 0.0) * exec_price
        requested = max(source_size_usd, 0.0)
        if requested <= 0:
            requested = full_close_notional
        return round(min(full_close_notional, requested), 2)

    @staticmethod
    async def _upsert_position(session: AsyncSession, intent: TradeIntent, executed_size_usd: float) -> None:
        query = select(Position).where(
            Position.wallet_address == intent.wallet_address,
            Position.market_id == intent.market_id,
            Position.outcome == intent.outcome,
            Position.is_open.is_(True),
        )
        position = (await session.execute(query)).scalar_one_or_none()

        execution_price_usd = max(intent.source_price_cents / 100.0, 0.01)
        quantity_delta = executed_size_usd / execution_price_usd
        side = intent.side.lower()
        now = datetime.now(tz=timezone.utc)

        if position is None:
            position = Position(
                wallet_address=intent.wallet_address,
                market_id=intent.market_id,
                token_id=intent.token_id,
                outcome=intent.outcome,
                side=side,
                quantity=quantity_delta,
                avg_price_cents=intent.source_price_cents,
                invested_usd=round(executed_size_usd, 4),
                current_price_cents=intent.source_price_cents,
                unrealized_pnl_usd=0.0,
                updated_at=now,
            )
            session.add(position)
            return

        if intent.token_id and position.token_id != intent.token_id:
            position.token_id = intent.token_id

        if position.side == side:
            new_invested = position.invested_usd + executed_size_usd
            new_quantity = position.quantity + quantity_delta
            if new_quantity <= 0:
                position.is_open = False
                position.closed_at = now
                position.quantity = 0.0
                position.invested_usd = 0.0
                position.unrealized_pnl_usd = 0.0
                position.updated_at = now
                return

            position.avg_price_cents = (
                (position.avg_price_cents * position.quantity + intent.source_price_cents * quantity_delta) / new_quantity
            )
            position.quantity = new_quantity
            position.invested_usd = round(new_invested, 4)
            position.current_price_cents = intent.source_price_cents
            position.closed_at = None
            position.updated_at = now
            return

        # Mirror-close netting: opposite-side trade reduces, closes or flips the position.
        close_qty = min(position.quantity, quantity_delta)
        entry_price_usd = max(position.avg_price_cents / 100.0, 0.01)
        if position.side == TradeSide.BUY.value:
            realized_delta = (execution_price_usd - entry_price_usd) * close_qty
        else:
            realized_delta = (entry_price_usd - execution_price_usd) * close_qty

        position.realized_pnl_usd = round(position.realized_pnl_usd + realized_delta, 4)

        remaining_qty = position.quantity - close_qty
        residual_incoming_qty = quantity_delta - close_qty

        if remaining_qty <= 1e-9 and residual_incoming_qty <= 1e-9:
            position.is_open = False
            position.closed_at = now
            position.quantity = 0.0
            position.invested_usd = 0.0
            position.current_price_cents = intent.source_price_cents
            position.unrealized_pnl_usd = 0.0
            position.updated_at = now
            return

        if remaining_qty > 1e-9:
            position.quantity = remaining_qty
            position.invested_usd = round(remaining_qty * entry_price_usd, 4)
            position.current_price_cents = intent.source_price_cents
            price_delta = (intent.source_price_cents - position.avg_price_cents) / 100.0
            if position.side == TradeSide.SELL.value:
                price_delta *= -1
            position.unrealized_pnl_usd = round(price_delta * remaining_qty, 4)
            position.is_open = True
            position.closed_at = None
            position.updated_at = now
            return

        # Incoming opposite side is larger than current position => flip to new side.
        position.side = TradeSide.BUY.value if side == TradeSide.BUY.value else TradeSide.SELL.value
        position.quantity = residual_incoming_qty
        position.avg_price_cents = intent.source_price_cents
        position.invested_usd = round(residual_incoming_qty * execution_price_usd, 4)
        position.current_price_cents = intent.source_price_cents
        position.unrealized_pnl_usd = 0.0
        position.is_open = True
        position.closed_at = None
        position.updated_at = now

    @staticmethod
    def _is_duplicate_external_trade(exc: IntegrityError) -> bool:
        text = str(exc).lower()
        return (
            "uq_copied_trades_external_trade_id" in text
            or "duplicate key value" in text
            or "external_trade_id" in text
            or "unique constraint failed: copied_trades.external_trade_id" in text
        )

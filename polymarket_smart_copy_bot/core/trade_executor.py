from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import FillMode, RiskMode, settings
from core.portfolio_tracker import PortfolioTracker
from core.risk_manager import PortfolioState, RiskManager
from core.trade_monitor import TradeIntent
from data.polymarket_client import OrderRequest, PolymarketClient
from models.models import CopiedTrade, ManualApproval, Position, TradeSide, TradeStatus
from utils.notifications import NotificationService


@dataclass(slots=True)
class MarketPostCheckResult:
    exceeded: bool
    market_exposure_usd: float
    market_cap_usd: float
    overflow_usd: float
    trimmed_usd: float
    trim_error: str | None = None


@dataclass(slots=True)
class FillReconcileResult:
    status: TradeStatus
    newly_filled_usd: float = 0.0
    newly_filled_quantity: float = 0.0
    fill_price_cents: float = 0.0
    order_open: bool | None = None
    latest_fill_at: datetime | None = None


@dataclass(slots=True)
class ExecutionPlan:
    order_request: OrderRequest
    requested_price_cents: float
    requested_slippage_bps: float
    order_type: str


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
        fill_mode: FillMode = "conservative",
        price_filter_enabled: bool,
        high_conviction_boost_enabled: bool,
    ) -> None:
        await self.execute_copy_trade(
            session,
            intent,
            portfolio_state,
            risk_mode=risk_mode,
            fill_mode=fill_mode,
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
        fill_mode: FillMode = "conservative",
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

            precheck_size = await self._apply_market_position_precheck(
                session=session,
                market_id=intent.market_id,
                requested_size_usd=target_size_usd,
                portfolio_state=portfolio_state,
                risk_mode=risk_mode,
            )
            if precheck_size <= 0:
                copied_trade.status = TradeStatus.SKIPPED.value
                copied_trade.reason = "market_position_cap_reached"
                logger.info("Trade {} skipped: market_position_cap_reached", intent.external_trade_id)
                return
            if precheck_size < target_size_usd:
                logger.warning(
                    "Trade {} resized by market cap pre-check: requested=${:.2f} allowed=${:.2f} market={}",
                    intent.external_trade_id,
                    target_size_usd,
                    precheck_size,
                    intent.market_id,
                )
                target_size_usd = precheck_size
            min_size = self._minimum_position_size(portfolio_state=portfolio_state, risk_mode=risk_mode)
            if target_size_usd < min_size:
                copied_trade.status = TradeStatus.SKIPPED.value
                copied_trade.reason = "below_min_position_size_after_market_cap"
                logger.info("Trade {} skipped: below_min_position_size_after_market_cap", intent.external_trade_id)
                return

        if requires_manual_confirmation:
            approved = await self._request_manual_confirmation(session, copied_trade, target_size_usd)
            if not approved:
                copied_trade.status = TradeStatus.SKIPPED.value
                copied_trade.reason = "manual_rejection_or_timeout"
                return

        # Persist the bot-calculated intended size even if execution later fails.
        copied_trade.size_usd = target_size_usd

        execution_plan = self._build_execution_plan(
            intent=intent,
            target_size_usd=target_size_usd,
            risk_mode=risk_mode,
        )
        if execution_plan is None:
            copied_trade.status = TradeStatus.SKIPPED.value
            copied_trade.reason = "slippage_above_hard_limit"
            logger.warning("Trade {} skipped: slippage_above_hard_limit", intent.external_trade_id)
            return

        result = await self.polymarket_client.place_order(
            execution_plan.order_request
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

        copied_trade.status = TradeStatus.SUBMITTED.value
        copied_trade.order_id = result.order_id
        copied_trade.tx_hash = result.tx_hash
        copied_trade.submitted_at = datetime.now(tz=timezone.utc)
        if is_mirror_close:
            copied_trade.reason = (
                f"submitted mirror_close mode={risk_mode} type={execution_plan.order_type} "
                f"limit={execution_plan.requested_price_cents:.2f}c slip={execution_plan.requested_slippage_bps:.2f}bps"
            )
        else:
            copied_trade.reason = (
                f"submitted mode={risk_mode} type={execution_plan.order_type} "
                f"limit={execution_plan.requested_price_cents:.2f}c slip={execution_plan.requested_slippage_bps:.2f}bps "
                f"mult={wallet_multiplier:.2f} kelly={kelly_fraction:.3f}"
            )

        if self.polymarket_client.is_dry_run():
            reconcile_result = await self._apply_dry_run_fill(
                session=session,
                copied_trade=copied_trade,
                intent=intent,
                target_size_usd=target_size_usd,
            )
        else:
            reconcile_result = await self._reconcile_trade_fill_state(
                session=session,
                copied_trade=copied_trade,
                intent=intent,
            )

        if reconcile_result.newly_filled_usd > 0:
            post_check = await self._post_check_market_position_cap(
                session=session,
                intent=intent,
                executed_size_usd=reconcile_result.newly_filled_usd,
                portfolio_state=portfolio_state,
                risk_mode=risk_mode,
            )
            if post_check.exceeded:
                overflow_note = (
                    f"postcheck_exceeded market={intent.market_id} "
                    f"exposure=${post_check.market_exposure_usd:.2f} cap=${post_check.market_cap_usd:.2f} "
                    f"overflow=${post_check.overflow_usd:.2f} trimmed=${post_check.trimmed_usd:.2f}"
                )
                if post_check.trim_error:
                    overflow_note = f"{overflow_note} trim_error={post_check.trim_error}"
                copied_trade.reason = f"{copied_trade.reason} | {overflow_note}"
                logger.error("{}", overflow_note)
                await self.notifications.send_message(f"[RISK] {overflow_note}")
            await self._refresh_live_capital_base(session)

        if copied_trade.status == TradeStatus.SUBMITTED.value:
            await self.notifications.send_message(
                f"[SUBMITTED:{risk_mode}] {intent.market_id} {intent.outcome} {intent.side.upper()} | "
                f"${target_size_usd:.2f}"
            )
            return

        if copied_trade.status in {TradeStatus.PARTIAL.value, TradeStatus.FILLED.value}:
            actual_slippage_bps = self.risk_manager.compute_slippage_bps(
                source_price_cents=intent.source_price_cents,
                execution_price_cents=max(copied_trade.filled_price_cents, 0.0),
                side=intent.side.lower(),
            )
            logger.info(
                "Filled at {:.2f}c (slippage {:+.2f} bps) trade={} status={}",
                copied_trade.filled_price_cents,
                actual_slippage_bps,
                copied_trade.external_trade_id,
                copied_trade.status,
            )
            await self.notifications.send_message(
                f"[{copied_trade.status.upper()}:{risk_mode}] {intent.market_id} {intent.outcome} {intent.side.upper()} | "
                f"${copied_trade.filled_size_usd:.2f} @ {copied_trade.filled_price_cents:.2f}c "
                f"(slippage {actual_slippage_bps:+.2f}bps)"
            )
            return

    async def reconcile_open_trade_states(self, session: AsyncSession) -> None:
        """Reconcile submitted/partial live trades against authoritative CLOB fills."""

        if self.polymarket_client.is_dry_run():
            return

        query = (
            select(CopiedTrade)
            .where(CopiedTrade.status.in_([TradeStatus.SUBMITTED.value, TradeStatus.PARTIAL.value]))
            .order_by(CopiedTrade.copied_at.asc())
        )
        rows = (await session.execute(query)).scalars().all()
        for copied_trade in rows:
            intent = self._intent_from_copied_trade(copied_trade)
            if intent is None:
                continue
            await self._reconcile_trade_fill_state(
                session=session,
                copied_trade=copied_trade,
                intent=intent,
            )

    async def mark_canceled_orders(
        self,
        session: AsyncSession,
        *,
        order_ids: list[str],
        expired: bool = False,
    ) -> int:
        """Sync stale cleanup results back into copied trade status rows."""

        normalized_ids = [order_id.strip() for order_id in order_ids if order_id and order_id.strip()]
        if not normalized_ids:
            return 0

        query = select(CopiedTrade).where(CopiedTrade.order_id.in_(normalized_ids))
        rows = (await session.execute(query)).scalars().all()
        now = datetime.now(tz=timezone.utc)
        changed = 0
        next_status = TradeStatus.EXPIRED.value if expired else TradeStatus.CANCELED.value
        for row in rows:
            if row.status in {TradeStatus.FILLED.value, TradeStatus.FAILED.value, TradeStatus.SKIPPED.value}:
                continue
            row.status = next_status
            row.canceled_at = now
            row.reason = f"{next_status}_by_cleanup"
            changed += 1
        return changed

    async def _refresh_live_capital_base(self, session: AsyncSession) -> None:
        """Refresh runtime capital base immediately after successful LIVE fills."""

        if self.polymarket_client.is_dry_run():
            return
        live_balance = await self.polymarket_client.fetch_account_balance_usd()
        if live_balance is None or live_balance < 0:
            return
        await self.portfolio_tracker.update_capital_base(session, live_balance)

    def _build_execution_plan(
        self,
        *,
        intent: TradeIntent,
        target_size_usd: float,
        risk_mode: RiskMode,
    ) -> ExecutionPlan | None:
        requested_price_cents = round(float(intent.source_price_cents), 4)
        requested_slippage_bps = 0.0
        order_type = "GTC"

        if risk_mode == "aggressive":
            requested_slippage_bps = max(float(settings.max_slippage_bps), 0.0)
            side = intent.side.lower()
            if side == TradeSide.BUY.value:
                requested_price_cents = round(
                    float(intent.source_price_cents) * (1.0 + (requested_slippage_bps / 10_000.0)),
                    4,
                )
            else:
                requested_price_cents = round(
                    float(intent.source_price_cents) * (1.0 - (requested_slippage_bps / 10_000.0)),
                    4,
                )
            allowed, actual_bps = self.risk_manager.can_accept_slippage(
                source_price_cents=float(intent.source_price_cents),
                execution_price_cents=requested_price_cents,
                side=side,
                risk_mode=risk_mode,
            )
            if not allowed:
                logger.warning(
                    "Trade {} rejected by slippage guard: source={:.2f}c execution={:.2f}c bps={:.2f}",
                    intent.external_trade_id,
                    intent.source_price_cents,
                    requested_price_cents,
                    actual_bps,
                )
                return None
            requested_slippage_bps = round(actual_bps, 4)
            # SAFETY: safe aggressive fill via immediate-or-cancel semantics.
            order_type = "FAK"

        return ExecutionPlan(
            order_request=OrderRequest(
                token_id=intent.token_id or "",
                side=intent.side,
                price_cents=requested_price_cents,
                size_usd=target_size_usd,
                market_id=intent.market_id,
                outcome=intent.outcome,
                order_type=order_type,
            ),
            requested_price_cents=requested_price_cents,
            requested_slippage_bps=requested_slippage_bps,
            order_type=order_type,
        )

    async def _apply_market_position_precheck(
        self,
        *,
        session: AsyncSession,
        market_id: str,
        requested_size_usd: float,
        portfolio_state: PortfolioState,
        risk_mode: RiskMode,
    ) -> float:
        """Hard-limit requested order size by remaining per-market capacity."""

        market_cap = self._market_position_cap_usd(portfolio_state=portfolio_state, risk_mode=risk_mode)
        current_market_exposure = await self._market_open_exposure_usd(session, market_id)
        pending_market_exposure = await self._market_pending_order_exposure_usd(session, market_id)
        remaining_capacity = max(market_cap - current_market_exposure - pending_market_exposure, 0.0)
        return round(min(requested_size_usd, remaining_capacity), 2)

    @staticmethod
    async def _market_pending_order_exposure_usd(session: AsyncSession, market_id: str) -> float:
        """Reserve market cap for bot-submitted orders that have not fully filled yet."""

        query = select(CopiedTrade).where(
            CopiedTrade.market_id == market_id,
            CopiedTrade.status.in_([TradeStatus.SUBMITTED.value, TradeStatus.PARTIAL.value]),
        )
        rows = (await session.execute(query)).scalars().all()
        pending_usd = 0.0
        for row in rows:
            remaining = max(float(row.size_usd or 0.0) - float(row.filled_size_usd or 0.0), 0.0)
            pending_usd += remaining
        return round(pending_usd, 4)

    async def _post_check_market_position_cap(
        self,
        *,
        session: AsyncSession,
        intent: TradeIntent,
        executed_size_usd: float,
        portfolio_state: PortfolioState,
        risk_mode: RiskMode,
    ) -> MarketPostCheckResult:
        """Verify hard per-market cap against account-level open positions after order execution."""

        if not settings.postcheck_market_position_hard_limit:
            return MarketPostCheckResult(False, 0.0, 0.0, 0.0, 0.0)

        market_cap = self._market_position_cap_usd(portfolio_state=portfolio_state, risk_mode=risk_mode)
        remote_positions = await self.polymarket_client.fetch_account_open_positions(limit=300)
        if remote_positions is None:
            return MarketPostCheckResult(False, 0.0, market_cap, 0.0, 0.0, "remote_positions_unavailable")

        market_exposure = sum(
            max(position.invested_usd, 0.0)
            for position in remote_positions
            if position.market_id == intent.market_id
        )
        tolerance = max(settings.postcheck_market_cap_tolerance_usd, 0.0)
        overflow = market_exposure - market_cap
        if overflow <= tolerance:
            return MarketPostCheckResult(False, market_exposure, market_cap, 0.0, 0.0)

        trim_target = round(min(max(overflow, 0.0), max(executed_size_usd, 0.0)), 2)
        trimmed_usd = 0.0
        trim_error: str | None = None
        if trim_target > 0 and intent.token_id:
            trim_side = TradeSide.SELL.value if intent.side.lower() == TradeSide.BUY.value else TradeSide.BUY.value
            trim_result = await self.polymarket_client.place_order(
                OrderRequest(
                    token_id=intent.token_id,
                    side=trim_side,
                    price_cents=intent.source_price_cents,
                    size_usd=trim_target,
                    market_id=intent.market_id,
                    outcome=intent.outcome,
                )
            )
            if trim_result.success:
                trimmed_usd = trim_target
                trim_intent = TradeIntent(
                    external_trade_id=f"{intent.external_trade_id}:postcheck_trim",
                    wallet_address=intent.wallet_address,
                    wallet_score=intent.wallet_score,
                    wallet_win_rate=intent.wallet_win_rate,
                    wallet_profit_factor=intent.wallet_profit_factor,
                    wallet_avg_position_size=intent.wallet_avg_position_size,
                    market_id=intent.market_id,
                    token_id=intent.token_id,
                    outcome=intent.outcome,
                    side=trim_side,
                    source_price_cents=intent.source_price_cents,
                    source_size_usd=trim_target,
                    is_short_term=intent.is_short_term,
                )
                await self._upsert_position(session, trim_intent, trim_target)
            else:
                trim_error = trim_result.error or "trim_failed"
        elif trim_target <= 0:
            trim_error = "no_trim_required"
        else:
            trim_error = "missing_token_id"

        return MarketPostCheckResult(
            exceeded=True,
            market_exposure_usd=round(market_exposure, 4),
            market_cap_usd=round(market_cap, 4),
            overflow_usd=round(max(overflow, 0.0), 4),
            trimmed_usd=round(trimmed_usd, 4),
            trim_error=trim_error,
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
        # Also match account_sync positions to find mirror-close candidates
        # after live position sync has reassigned the wallet address.
        query = select(Position).where(
            Position.wallet_address.in_([wallet_address, PortfolioTracker.ACCOUNT_SYNC_WALLET]),
            Position.market_id == market_id,
            Position.outcome == outcome,
            Position.is_open.is_(True),
        )
        if token_id:
            query = query.where((Position.token_id == token_id) | (Position.token_id.is_(None)))
        query = query.order_by(Position.opened_at.asc()).limit(1)
        return (await session.execute(query)).scalar_one_or_none()

    async def _market_open_exposure_usd(self, session: AsyncSession, market_id: str) -> float:
        """Return open exposure for a market, preferring account-sync rows when available."""

        account_sync_wallet = self.portfolio_tracker.ACCOUNT_SYNC_WALLET
        synced_query = (
            select(func.coalesce(func.sum(Position.invested_usd), 0.0))
            .where(Position.market_id == market_id)
            .where(Position.wallet_address == account_sync_wallet)
            .where(Position.is_open.is_(True))
        )
        synced_value = float((await session.execute(synced_query)).scalar_one() or 0.0)
        if synced_value > 0:
            return synced_value

        fallback_query = (
            select(func.coalesce(func.sum(Position.invested_usd), 0.0))
            .where(Position.market_id == market_id)
            .where(Position.wallet_address != account_sync_wallet)
            .where(Position.is_open.is_(True))
        )
        fallback_value = float((await session.execute(fallback_query)).scalar_one() or 0.0)
        return fallback_value

    @staticmethod
    def _market_position_cap_usd(*, portfolio_state: PortfolioState, risk_mode: RiskMode) -> float:
        equity = max(portfolio_state.total_equity_usd, 1.0)
        if risk_mode == "aggressive":
            return equity * settings.max_per_position_pct
        return equity * settings.conservative_max_per_position_pct

    @staticmethod
    def _minimum_position_size(*, portfolio_state: PortfolioState, risk_mode: RiskMode) -> float:
        if risk_mode == "aggressive":
            return 1.5 if portfolio_state.total_equity_usd < 150 else 2.0
        return 2.0

    @staticmethod
    def _derive_close_size_usd(*, position: Position, source_size_usd: float, execution_price_cents: float) -> float:
        exec_price = max(execution_price_cents / 100.0, 0.01)
        full_close_notional = max(position.quantity, 0.0) * exec_price
        requested = max(source_size_usd, 0.0)
        if requested <= 0:
            requested = full_close_notional
        return round(min(full_close_notional, requested), 2)

    async def _apply_dry_run_fill(
        self,
        *,
        session: AsyncSession,
        copied_trade: CopiedTrade,
        intent: TradeIntent,
        target_size_usd: float,
    ) -> FillReconcileResult:
        """DRY_RUN still simulates immediate fills, but uses the new status model."""

        fill_quantity = target_size_usd / max(intent.source_price_cents / 100.0, 0.01)
        copied_trade.status = TradeStatus.FILLED.value
        copied_trade.filled_at = datetime.now(tz=timezone.utc)
        copied_trade.filled_quantity = round(fill_quantity, 8)
        copied_trade.filled_size_usd = round(target_size_usd, 4)
        copied_trade.filled_price_cents = round(intent.source_price_cents, 4)
        copied_trade.reason = f"filled dry_run @ {intent.source_price_cents:.2f}c"
        await self._upsert_position(session, intent, target_size_usd)
        return FillReconcileResult(
            status=TradeStatus.FILLED,
            newly_filled_usd=round(target_size_usd, 4),
            newly_filled_quantity=round(fill_quantity, 8),
            fill_price_cents=round(intent.source_price_cents, 4),
            order_open=False,
            latest_fill_at=copied_trade.filled_at,
        )

    async def _reconcile_trade_fill_state(
        self,
        *,
        session: AsyncSession,
        copied_trade: CopiedTrade,
        intent: TradeIntent,
    ) -> FillReconcileResult:
        """SAFETY: confirm live order state from authoritative Polymarket fills."""

        after_ts = copied_trade.submitted_at or copied_trade.copied_at or datetime.now(tz=timezone.utc)
        fills = await self.polymarket_client.fetch_account_fills(
            after_ts=after_ts,
            market_id=copied_trade.market_id,
            token_id=copied_trade.token_id,
            side=copied_trade.side,
            order_id=copied_trade.order_id,
            limit=100,
        )

        total_quantity = 0.0
        total_size_usd = 0.0
        latest_fill_at: datetime | None = None
        if fills:
            for fill in fills:
                total_quantity += max(fill.size_shares, 0.0)
                total_size_usd += max(fill.size_usd, 0.0)
                if latest_fill_at is None or fill.traded_at > latest_fill_at:
                    latest_fill_at = fill.traded_at

        delta_quantity = max(total_quantity - float(copied_trade.filled_quantity or 0.0), 0.0)
        delta_size_usd = max(total_size_usd - float(copied_trade.filled_size_usd or 0.0), 0.0)
        delta_price_cents = 0.0
        if delta_quantity > 0 and delta_size_usd > 0:
            delta_price_cents = round((delta_size_usd / delta_quantity) * 100.0, 4)

        if delta_quantity > 0 and delta_size_usd > 0:
            fill_intent = self._intent_with_fill(intent, price_cents=delta_price_cents, size_usd=delta_size_usd)
            await self._upsert_position(session, fill_intent, delta_size_usd)

        order_open = None
        if copied_trade.order_id:
            order_open = await self.polymarket_client.is_order_open(copied_trade.order_id)

        copied_trade.filled_quantity = round(total_quantity, 8)
        copied_trade.filled_size_usd = round(total_size_usd, 4)
        copied_trade.filled_price_cents = round((total_size_usd / total_quantity) * 100.0, 4) if total_quantity > 0 else 0.0
        copied_trade.filled_at = latest_fill_at

        size_tolerance = max(min(copied_trade.size_usd * 0.01, 0.1), 0.01)
        fully_filled = total_size_usd + size_tolerance >= copied_trade.size_usd

        if total_size_usd <= 0:
            if order_open is False:
                copied_trade.status = TradeStatus.CANCELED.value
                copied_trade.canceled_at = datetime.now(tz=timezone.utc)
                copied_trade.reason = "canceled_without_fill"
                return FillReconcileResult(status=TradeStatus.CANCELED, order_open=order_open)
            copied_trade.status = TradeStatus.SUBMITTED.value
            copied_trade.reason = copied_trade.reason or "submitted_waiting_fill"
            return FillReconcileResult(status=TradeStatus.SUBMITTED, order_open=order_open)

        if fully_filled:
            copied_trade.status = TradeStatus.FILLED.value
            copied_trade.reason = f"filled @ {copied_trade.filled_price_cents:.2f}c"
            return FillReconcileResult(
                status=TradeStatus.FILLED,
                newly_filled_usd=round(delta_size_usd, 4),
                newly_filled_quantity=round(delta_quantity, 8),
                fill_price_cents=delta_price_cents or copied_trade.filled_price_cents,
                order_open=order_open,
                latest_fill_at=latest_fill_at,
            )

        copied_trade.status = TradeStatus.PARTIAL.value
        copied_trade.reason = f"partial_fill ${copied_trade.filled_size_usd:.2f} @ {copied_trade.filled_price_cents:.2f}c"
        if order_open is False:
            copied_trade.canceled_at = datetime.now(tz=timezone.utc)
            copied_trade.reason = f"{copied_trade.reason} | remainder_canceled"
        return FillReconcileResult(
            status=TradeStatus.PARTIAL,
            newly_filled_usd=round(delta_size_usd, 4),
            newly_filled_quantity=round(delta_quantity, 8),
            fill_price_cents=delta_price_cents or copied_trade.filled_price_cents,
            order_open=order_open,
            latest_fill_at=latest_fill_at,
        )

    @staticmethod
    def _intent_from_copied_trade(copied_trade: CopiedTrade) -> TradeIntent | None:
        if not copied_trade.token_id:
            return None
        return TradeIntent(
            external_trade_id=copied_trade.external_trade_id,
            wallet_address=copied_trade.wallet_address,
            wallet_score=0.0,
            wallet_win_rate=0.0,
            wallet_profit_factor=0.0,
            wallet_avg_position_size=0.0,
            market_id=copied_trade.market_id,
            token_id=copied_trade.token_id,
            outcome=copied_trade.outcome,
            side=copied_trade.side,
            source_price_cents=copied_trade.price_cents,
            source_size_usd=copied_trade.size_usd,
            is_short_term=False,
        )

    @staticmethod
    def _intent_with_fill(intent: TradeIntent, *, price_cents: float, size_usd: float) -> TradeIntent:
        return TradeIntent(
            external_trade_id=intent.external_trade_id,
            wallet_address=intent.wallet_address,
            wallet_score=intent.wallet_score,
            wallet_win_rate=intent.wallet_win_rate,
            wallet_profit_factor=intent.wallet_profit_factor,
            wallet_avg_position_size=intent.wallet_avg_position_size,
            market_id=intent.market_id,
            token_id=intent.token_id,
            outcome=intent.outcome,
            side=intent.side,
            source_price_cents=price_cents,
            source_size_usd=size_usd,
            is_short_term=intent.is_short_term,
        )

    @staticmethod
    async def _upsert_position(session: AsyncSession, intent: TradeIntent, executed_size_usd: float) -> None:
        # Search for an existing open position matching this market/outcome.
        # In LIVE mode, account sync may have reassigned the wallet to
        # "account_sync", so we look for both the original wallet and the
        # sync sentinel to avoid creating duplicate position rows.
        query = select(Position).where(
            Position.wallet_address.in_([intent.wallet_address, PortfolioTracker.ACCOUNT_SYNC_WALLET]),
            Position.market_id == intent.market_id,
            Position.outcome == intent.outcome,
            Position.is_open.is_(True),
        ).order_by(Position.opened_at.asc()).limit(1)
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

        # Assign the real source wallet so mirror-close reconciliation can track
        # this position.  Without this, positions created by account_sync first
        # would never be matched for mirror-close.
        if position.wallet_address in (PortfolioTracker.ACCOUNT_SYNC_WALLET, "unknown", ""):
            position.wallet_address = intent.wallet_address

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

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import perf_counter

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import FillMode, RiskMode, settings
from contracts.execution_sidecar import (
    SidecarExecutionPlanRequest,
    SidecarExecutionPlanResponse,
    SidecarFillReconcileRequest,
    SidecarFillReconcileResponse,
    SidecarFillRow,
    SidecarOrderRequest,
    SidecarOrderbookLevel,
    SidecarOrderbookSnapshot,
)
from core.portfolio_tracker import PortfolioTracker
from core.risk_manager import PortfolioState, RiskManager
from core.trade_monitor import TradeIntent
from data.polymarket_client import OrderRequest, OrderbookSnapshot, PolymarketClient
from models.models import CopiedTrade, ExecutionDecisionAudit, ManualApproval, Position, TradeSide, TradeStatus
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


@dataclass(slots=True)
class ExecutionPlanningOutcome:
    plan: ExecutionPlan | None
    reason: str | None = None


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
        self._inflight_external_trade_ids: set[str] = set()

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
    ) -> str | None:
        return await self.execute_copy_trade(
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
    ) -> str | None:
        if intent.external_trade_id in self._inflight_external_trade_ids:
            logger.debug("Duplicate in-flight trade ignored: {}", intent.external_trade_id)
            return None

        self._inflight_external_trade_ids.add(intent.external_trade_id)
        intent_side = intent.side.lower()
        copied_trade = self._build_copied_trade(intent)
        try:
            return await self._execute_copy_trade_impl(
                session=session,
                intent=intent,
                copied_trade=copied_trade,
                portfolio_state=portfolio_state,
                risk_mode=risk_mode,
                fill_mode=fill_mode,
                price_filter_enabled=price_filter_enabled,
                high_conviction_boost_enabled=high_conviction_boost_enabled,
            )
        finally:
            self._inflight_external_trade_ids.discard(intent.external_trade_id)

    async def _execute_copy_trade_impl(
        self,
        *,
        session: AsyncSession,
        intent: TradeIntent,
        copied_trade: CopiedTrade,
        portfolio_state: PortfolioState,
        risk_mode: RiskMode,
        fill_mode: FillMode,
        price_filter_enabled: bool,
        high_conviction_boost_enabled: bool,
    ) -> str | None:
        intent_side = intent.side.lower()

        existing_position = await self._find_open_position(
            session,
            wallet_address=intent.wallet_address,
            market_id=intent.market_id,
            outcome=intent.outcome,
            token_id=intent.token_id,
        )
        is_mirror_close = existing_position is not None and existing_position.side != intent_side

        if intent_side == TradeSide.SELL.value and existing_position is None:
            copied_trade.status = TradeStatus.SKIPPED.value
            copied_trade.reason = "sell_without_confirmed_position"
            self._stage_copied_trade(session, copied_trade)
            logger.info("Trade {} skipped: sell_without_confirmed_position", intent.external_trade_id)
            return copied_trade.status

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
                self._stage_copied_trade(session, copied_trade)
                logger.info("Trade {} skipped: mirror_close_zero_size", intent.external_trade_id)
                return copied_trade.status
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
                self._stage_copied_trade(session, copied_trade)
                logger.info("Trade {} skipped: {}", intent.external_trade_id, decision.reason)
                return copied_trade.status

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
                self._stage_copied_trade(session, copied_trade)
                logger.info("Trade {} skipped: market_position_cap_reached", intent.external_trade_id)
                return copied_trade.status
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
                self._stage_copied_trade(session, copied_trade)
                logger.info("Trade {} skipped: below_min_position_size_after_market_cap", intent.external_trade_id)
                return copied_trade.status

        if intent_side == TradeSide.SELL.value and existing_position is not None:
            target_size_usd, sell_reason = self._validate_sell_size_against_position(
                position=existing_position,
                requested_size_usd=target_size_usd,
                execution_price_cents=intent.source_price_cents,
            )
            if sell_reason is not None:
                copied_trade.status = TradeStatus.SKIPPED.value
                copied_trade.reason = sell_reason
                self._stage_copied_trade(session, copied_trade)
                logger.info("Trade {} skipped: {}", intent.external_trade_id, sell_reason)
                return copied_trade.status

        if requires_manual_confirmation:
            try:
                await self._ensure_copied_trade_persisted(session, copied_trade)
            except IntegrityError as exc:
                if self._is_duplicate_external_trade(exc):
                    logger.debug("Duplicate trade ignored: {}", intent.external_trade_id)
                    return None
                raise
            approved = await self._request_manual_confirmation(session, copied_trade, target_size_usd)
            if not approved:
                copied_trade.status = TradeStatus.SKIPPED.value
                copied_trade.reason = "manual_rejection_or_timeout"
                return copied_trade.status

        orderbook = intent.market_snapshot
        if orderbook is None:
            orderbook = await self.polymarket_client.fetch_orderbook(intent.token_id)

        # Persist the bot-calculated intended size even if execution later fails.
        copied_trade.size_usd = target_size_usd

        planning_outcome = await self._plan_execution(
            session=session,
            intent=intent,
            target_size_usd=target_size_usd,
            risk_mode=risk_mode,
            orderbook=orderbook,
        )
        if planning_outcome.plan is None:
            copied_trade.status = TradeStatus.SKIPPED.value
            copied_trade.reason = planning_outcome.reason or "execution_plan_rejected"
            self._stage_copied_trade(session, copied_trade)
            logger.info("Trade {} skipped: {}", intent.external_trade_id, copied_trade.reason)
            return copied_trade.status
        execution_plan = planning_outcome.plan

        result = await self.polymarket_client.place_order(
            execution_plan.order_request
        )
        self._stage_copied_trade(session, copied_trade)

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
                return copied_trade.status
            if error_text == "insufficient_balance_allowance":
                copied_trade.status = TradeStatus.SKIPPED.value
                copied_trade.reason = "insufficient_balance_allowance"
                logger.warning("Trade {} skipped: insufficient balance/allowance", intent.external_trade_id)
                return copied_trade.status
            if error_text == "invalid_price":
                copied_trade.status = TradeStatus.SKIPPED.value
                copied_trade.reason = "invalid_price"
                logger.info("Trade {} skipped: invalid price", intent.external_trade_id)
                return copied_trade.status

            copied_trade.status = TradeStatus.FAILED.value
            copied_trade.reason = result.error
            await self.notifications.send_message(
                f"[FAILED] {intent.market_id} {intent.outcome} {intent.side.upper()} | reason={result.error}"
            )
            return copied_trade.status

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
        elif execution_plan.order_type == "GTC":
            copied_trade.ttl_expires_at = copied_trade.submitted_at + timedelta(
                seconds=max(30, settings.aggressive_reprice_total_ttl_seconds)
            )
            copied_trade.reason = f"{copied_trade.reason} | awaiting_fill_monitor"
            reconcile_result = FillReconcileResult(
                status=TradeStatus.SUBMITTED,
                order_open=True,
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
            return copied_trade.status

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
            return copied_trade.status

        return copied_trade.status

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

    async def maybe_reprice_submitted_gtc_buy(
        self,
        *,
        copied_trade: CopiedTrade,
        now: datetime,
    ) -> bool:
        """Reprice one live aggressive BUY order once within the existing hard cap."""

        if self.polymarket_client.is_dry_run():
            return False
        if copied_trade.status != TradeStatus.SUBMITTED.value:
            return False
        if copied_trade.side != TradeSide.BUY.value:
            return False
        if not copied_trade.order_id or not copied_trade.token_id or not copied_trade.submitted_at:
            return False
        if float(copied_trade.filled_size_usd or 0.0) > 0 or float(copied_trade.filled_quantity or 0.0) > 0:
            return False

        reason_text = copied_trade.reason or ""
        if "repriced_once" in reason_text:
            return False

        reprice_after = copied_trade.submitted_at + timedelta(
            seconds=max(5, settings.aggressive_reprice_delay_seconds)
        )
        if now < reprice_after:
            return False
        if copied_trade.ttl_expires_at and now >= copied_trade.ttl_expires_at:
            return False

        reprice_plan = self._build_reprice_plan(copied_trade=copied_trade)
        if reprice_plan is None:
            copied_trade.status = TradeStatus.SKIPPED.value
            copied_trade.reason = f"{reason_text} | reprice_rejected_by_slippage_guard"
            return True

        cancelled = await self.polymarket_client.cancel_order(copied_trade.order_id)
        if not cancelled:
            copied_trade.reason = f"{reason_text} | reprice_cancel_failed"
            return False

        result = await self.polymarket_client.place_order(reprice_plan.order_request)
        if not result.success:
            error_text = str(result.error or "")
            if error_text.startswith("orderbook_not_found:"):
                copied_trade.status = TradeStatus.SKIPPED.value
                copied_trade.reason = "orderbook_not_found"
            elif error_text == "insufficient_balance_allowance":
                copied_trade.status = TradeStatus.SKIPPED.value
                copied_trade.reason = "insufficient_balance_allowance"
            else:
                copied_trade.status = TradeStatus.FAILED.value
                copied_trade.reason = result.error
            copied_trade.canceled_at = now
            return True

        copied_trade.order_id = result.order_id
        copied_trade.tx_hash = result.tx_hash
        copied_trade.reason = (
            f"{reason_text} | repriced_once "
            f"limit={reprice_plan.requested_price_cents:.2f}c slip={reprice_plan.requested_slippage_bps:.2f}bps"
        )
        return True

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

    async def _plan_execution(
        self,
        *,
        session: AsyncSession | None = None,
        intent: TradeIntent,
        target_size_usd: float,
        risk_mode: RiskMode,
        orderbook: OrderbookSnapshot | None,
    ) -> ExecutionPlanningOutcome:
        shadow_enabled = bool(
            session is not None
            and settings.execution_sidecar_shadow_mode_enabled
            and settings.execution_sidecar_enabled
            and settings.execution_sidecar_execution_plan_enabled
        )
        rust_started = perf_counter()
        sidecar_plan = await self._build_execution_plan_via_sidecar(
            intent=intent,
            target_size_usd=target_size_usd,
            risk_mode=risk_mode,
            orderbook=orderbook,
        )
        rust_elapsed_ms = round((perf_counter() - rust_started) * 1000.0, 4)
        python_outcome: ExecutionPlanningOutcome | None = None
        python_elapsed_ms: float | None = None
        if shadow_enabled:
            python_started = perf_counter()
            python_outcome = self._plan_execution_python_path(
                intent=intent,
                target_size_usd=target_size_usd,
                risk_mode=risk_mode,
                orderbook=orderbook,
            )
            python_elapsed_ms = round((perf_counter() - python_started) * 1000.0, 4)
            await self._record_execution_plan_audit(
                session=session,
                intent=intent,
                python_outcome=python_outcome,
                python_latency_ms=python_elapsed_ms,
                sidecar_plan=sidecar_plan,
                rust_latency_ms=rust_elapsed_ms,
            )
        if sidecar_plan is not None:
            if not sidecar_plan.allowed:
                return ExecutionPlanningOutcome(plan=None, reason=sidecar_plan.reason or "execution_plan_rejected")
            converted = self._execution_plan_from_sidecar_response(sidecar_plan)
            if converted is not None:
                return ExecutionPlanningOutcome(plan=converted)
            logger.warning(
                "Execution sidecar returned incomplete plan for {}, falling back to Python path",
                intent.external_trade_id,
            )

        if python_outcome is not None:
            return python_outcome
        return self._plan_execution_python_path(
            intent=intent,
            target_size_usd=target_size_usd,
            risk_mode=risk_mode,
            orderbook=orderbook,
        )

    def _plan_execution_python_path(
        self,
        *,
        intent: TradeIntent,
        target_size_usd: float,
        risk_mode: RiskMode,
        orderbook: OrderbookSnapshot | None,
    ) -> ExecutionPlanningOutcome:
        tradable, tradability_reason = self._check_market_tradability(
            intent=intent,
            orderbook=orderbook,
            target_size_usd=target_size_usd,
        )
        if not tradable:
            return ExecutionPlanningOutcome(plan=None, reason=tradability_reason)

        execution_plan = self._build_execution_plan(
            intent=intent,
            target_size_usd=target_size_usd,
            risk_mode=risk_mode,
        )
        if execution_plan is None:
            return ExecutionPlanningOutcome(plan=None, reason="slippage_above_hard_limit")
        return ExecutionPlanningOutcome(plan=execution_plan)

    async def _build_execution_plan_via_sidecar(
        self,
        *,
        intent: TradeIntent,
        target_size_usd: float,
        risk_mode: RiskMode,
        orderbook: OrderbookSnapshot | None,
    ) -> SidecarExecutionPlanResponse | None:
        if not (
            settings.execution_sidecar_enabled
            and settings.execution_sidecar_execution_plan_enabled
        ):
            return None
        request = SidecarExecutionPlanRequest(
            external_trade_id=intent.external_trade_id,
            source_price_cents=float(intent.source_price_cents),
            target_size_usd=float(target_size_usd),
            risk_mode=risk_mode,
            max_slippage_bps=float(settings.max_slippage_bps),
            max_allowed_slippage_bps=float(settings.max_allowed_slippage_bps),
            min_valid_price=float(settings.min_valid_price),
            min_orderbook_liquidity_usd=float(settings.min_orderbook_liquidity_usd),
            liquidity_buffer_multiplier=float(settings.liquidity_buffer_multiplier),
            max_price_deviation_pct=float(settings.max_price_deviation_pct),
            min_absolute_price_deviation_cents=float(settings.min_absolute_price_deviation_cents),
            orderbook=self._sidecar_orderbook_snapshot(orderbook),
            order=SidecarOrderRequest(
                token_id=intent.token_id or "",
                side=intent.side,
                price_cents=float(intent.source_price_cents),
                size_usd=float(target_size_usd),
                market_id=intent.market_id,
                outcome=intent.outcome,
                order_type="GTC",
            ),
        )
        return await self.polymarket_client.build_execution_plan_via_sidecar(request)

    @staticmethod
    def _execution_plan_from_sidecar_response(
        response: SidecarExecutionPlanResponse,
    ) -> ExecutionPlan | None:
        echoed = response.echoed_order
        if (
            echoed is None
            or response.requested_price_cents is None
            or response.requested_slippage_bps is None
            or response.order_type is None
        ):
            return None
        return ExecutionPlan(
            order_request=OrderRequest(
                token_id=echoed.token_id,
                side=echoed.side,
                price_cents=echoed.price_cents,
                size_usd=echoed.size_usd,
                market_id=echoed.market_id,
                outcome=echoed.outcome,
                order_type=echoed.order_type,
            ),
            requested_price_cents=response.requested_price_cents,
            requested_slippage_bps=response.requested_slippage_bps,
            order_type=response.order_type,
        )

    async def _record_execution_plan_audit(
        self,
        *,
        session: AsyncSession,
        intent: TradeIntent,
        python_outcome: ExecutionPlanningOutcome,
        python_latency_ms: float | None,
        sidecar_plan: SidecarExecutionPlanResponse | None,
        rust_latency_ms: float | None,
    ) -> None:
        python_status, python_reason, python_price, python_slippage = self._execution_outcome_audit_fields(python_outcome)
        rust_status, rust_reason, rust_price, rust_slippage = self._sidecar_plan_audit_fields(sidecar_plan)
        session.add(
            ExecutionDecisionAudit(
                stage="execution_plan",
                external_trade_id=intent.external_trade_id,
                wallet_address=intent.wallet_address,
                market_id=intent.market_id,
                token_id=intent.token_id,
                python_status=python_status,
                rust_status=rust_status,
                python_reason=python_reason,
                rust_reason=rust_reason,
                python_requested_price_cents=python_price,
                rust_requested_price_cents=rust_price,
                python_slippage_bps=python_slippage,
                rust_slippage_bps=rust_slippage,
                python_latency_ms=python_latency_ms,
                rust_latency_ms=rust_latency_ms,
                decision_delta_ms=(
                    round((rust_latency_ms or 0.0) - (python_latency_ms or 0.0), 4)
                    if python_latency_ms is not None and rust_latency_ms is not None
                    else None
                ),
                matched=self._execution_plan_match(python_outcome=python_outcome, sidecar_plan=sidecar_plan),
            )
        )

    @staticmethod
    def _execution_outcome_audit_fields(
        outcome: ExecutionPlanningOutcome,
    ) -> tuple[str, str | None, float | None, float | None]:
        if outcome.plan is None:
            return ("rejected", outcome.reason, None, None)
        return (
            "allowed",
            outcome.reason,
            outcome.plan.requested_price_cents,
            outcome.plan.requested_slippage_bps,
        )

    @staticmethod
    def _sidecar_plan_audit_fields(
        plan: SidecarExecutionPlanResponse | None,
    ) -> tuple[str, str | None, float | None, float | None]:
        if plan is None:
            return ("unavailable", "sidecar_unavailable_or_fallback", None, None)
        if not plan.allowed:
            return ("rejected", plan.reason, None, plan.requested_slippage_bps)
        return (
            "allowed",
            plan.reason,
            plan.requested_price_cents,
            plan.requested_slippage_bps,
        )

    @classmethod
    def _execution_plan_match(
        cls,
        *,
        python_outcome: ExecutionPlanningOutcome,
        sidecar_plan: SidecarExecutionPlanResponse | None,
    ) -> bool | None:
        if sidecar_plan is None:
            return None
        python_status, python_reason, python_price, python_slippage = cls._execution_outcome_audit_fields(python_outcome)
        rust_status, rust_reason, rust_price, rust_slippage = cls._sidecar_plan_audit_fields(sidecar_plan)
        if python_status != rust_status:
            return False
        if python_status != "allowed":
            return (python_reason or "") == (rust_reason or "")
        if python_price is None or rust_price is None or python_slippage is None or rust_slippage is None:
            return False
        return (
            abs(python_price - rust_price) <= 1e-6
            and abs(python_slippage - rust_slippage) <= 1e-6
        )

    @staticmethod
    def _sidecar_orderbook_snapshot(orderbook: OrderbookSnapshot | None) -> SidecarOrderbookSnapshot | None:
        if orderbook is None:
            return None
        return SidecarOrderbookSnapshot(
            bids=[SidecarOrderbookLevel(price=level.price, size=level.size) for level in orderbook.bids],
            asks=[SidecarOrderbookLevel(price=level.price, size=level.size) for level in orderbook.asks],
            best_bid=orderbook.best_bid,
            best_ask=orderbook.best_ask,
        )

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
            # Let aggressive BUY orders rest briefly on the book instead of
            # failing immediately on thin markets. SELL mirror-closes keep
            # immediate semantics to avoid lingering stale exits.
            order_type = "GTC" if side == TradeSide.BUY.value else "FAK"

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

    def _check_market_tradability(
        self,
        *,
        intent: TradeIntent,
        orderbook: OrderbookSnapshot | None,
        target_size_usd: float,
    ) -> tuple[bool, str | None]:
        if orderbook is None:
            return False, "no_orderbook"

        side = intent.side.lower()
        levels = orderbook.asks[:5] if side == TradeSide.BUY.value else orderbook.bids[:5]
        available_usd = sum(level.price * level.size for level in levels)
        required_usd = max(
            target_size_usd * settings.liquidity_buffer_multiplier,
            settings.min_orderbook_liquidity_usd,
        )
        if available_usd < required_usd:
            return False, f"low_liquidity:{available_usd:.2f}<{required_usd:.2f}"

        current_price = orderbook.best_ask if side == TradeSide.BUY.value else orderbook.best_bid
        if current_price is None:
            return False, "no_price_available"

        source_price = max(intent.source_price_cents / 100.0, settings.min_valid_price)
        deviation = abs(current_price - source_price) / source_price
        absolute_deviation_cents = abs((current_price * 100.0) - float(intent.source_price_cents))
        if (
            deviation > settings.max_price_deviation_pct
            and absolute_deviation_cents > settings.min_absolute_price_deviation_cents
        ):
            return False, f"price_moved:{deviation:.1%}"

        return True, None

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

    async def _ensure_copied_trade_persisted(
        self,
        session: AsyncSession,
        copied_trade: CopiedTrade,
    ) -> None:
        async with session.begin_nested():
            session.add(copied_trade)
            await session.flush()

    @staticmethod
    def _stage_copied_trade(session: AsyncSession, copied_trade: CopiedTrade) -> None:
        session.add(copied_trade)

    @staticmethod
    def _build_copied_trade(intent: TradeIntent) -> CopiedTrade:
        return CopiedTrade(
            external_trade_id=intent.external_trade_id,
            wallet_address=intent.wallet_address,
            market_id=intent.market_id,
            token_id=intent.token_id,
            outcome=intent.outcome,
            side=intent.side.lower(),
            price_cents=intent.source_price_cents,
            size_usd=intent.source_size_usd,
            status=TradeStatus.PENDING.value,
            source_timestamp=datetime.now(tz=timezone.utc),
        )

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
        if not settings.enforce_min_trade_size:
            return 0.0
        return max(settings.min_trade_size_usd, 0.0)

    @staticmethod
    def _derive_close_size_usd(*, position: Position, source_size_usd: float, execution_price_cents: float) -> float:
        exec_price = max(execution_price_cents / 100.0, 0.01)
        full_close_notional = max(position.quantity, 0.0) * exec_price
        requested = max(source_size_usd, 0.0)
        if requested <= 0:
            requested = full_close_notional
        return round(min(full_close_notional, requested), 2)

    @staticmethod
    def _validate_sell_size_against_position(
        *,
        position: Position,
        requested_size_usd: float,
        execution_price_cents: float,
    ) -> tuple[float, str | None]:
        available_size_usd = round(max(position.quantity, 0.0) * max(execution_price_cents / 100.0, 0.01), 2)
        if available_size_usd <= 0:
            return 0.0, "no_position_to_sell"

        trimmed_size = min(max(requested_size_usd, 0.0), available_size_usd)
        if trimmed_size < settings.min_trade_size_usd:
            return 0.0, "residual_too_small"
        return round(trimmed_size, 2), None

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

        order_open = None
        if copied_trade.order_id:
            order_open = await self.polymarket_client.is_order_open(copied_trade.order_id)

        sidecar_reconcile = await self._reconcile_fills_via_sidecar(
            copied_trade=copied_trade,
            fills=fills or [],
            order_open=order_open,
        )
        if sidecar_reconcile is not None:
            delta_quantity = sidecar_reconcile.delta_quantity
            delta_size_usd = sidecar_reconcile.delta_size_usd
            delta_price_cents = sidecar_reconcile.delta_price_cents
            latest_fill_at = (
                datetime.fromtimestamp(sidecar_reconcile.latest_fill_ts, tz=timezone.utc)
                if sidecar_reconcile.latest_fill_ts is not None
                else None
            )
            total_quantity = sidecar_reconcile.total_quantity
            total_size_usd = sidecar_reconcile.total_size_usd

        if delta_quantity > 0 and delta_size_usd > 0:
            fill_intent = self._intent_with_fill(intent, price_cents=delta_price_cents, size_usd=delta_size_usd)
            await self._upsert_position(session, fill_intent, delta_size_usd)

        copied_trade.filled_quantity = round(total_quantity, 8)
        copied_trade.filled_size_usd = round(total_size_usd, 4)
        copied_trade.filled_price_cents = round((total_size_usd / total_quantity) * 100.0, 4) if total_quantity > 0 else 0.0
        copied_trade.filled_at = latest_fill_at

        if sidecar_reconcile is not None:
            copied_trade.status = sidecar_reconcile.status
            copied_trade.reason = sidecar_reconcile.reason
            if copied_trade.status == TradeStatus.CANCELED.value:
                copied_trade.canceled_at = datetime.now(tz=timezone.utc)
            return FillReconcileResult(
                status=TradeStatus(copied_trade.status),
                newly_filled_usd=round(delta_size_usd, 4),
                newly_filled_quantity=round(delta_quantity, 8),
                fill_price_cents=delta_price_cents or copied_trade.filled_price_cents,
                order_open=sidecar_reconcile.order_open,
                latest_fill_at=latest_fill_at,
            )

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

    async def _reconcile_fills_via_sidecar(
        self,
        *,
        copied_trade: CopiedTrade,
        fills: list[Any],
        order_open: bool | None,
    ) -> SidecarFillReconcileResponse | None:
        if not (
            settings.execution_sidecar_enabled
            and settings.execution_sidecar_fill_reconcile_enabled
        ):
            return None
        request = SidecarFillReconcileRequest(
            copied_trade_size_usd=float(copied_trade.size_usd or 0.0),
            current_filled_quantity=float(copied_trade.filled_quantity or 0.0),
            current_filled_size_usd=float(copied_trade.filled_size_usd or 0.0),
            fills=[
                SidecarFillRow(
                    order_id=fill.order_id,
                    market_id=fill.market_id,
                    token_id=fill.token_id,
                    side=fill.side,
                    price_cents=float(fill.price_cents),
                    size_shares=float(fill.size_shares),
                    size_usd=float(fill.size_usd),
                    traded_at_ts=float(fill.traded_at.timestamp()),
                )
                for fill in fills
            ],
            order_open=order_open,
        )
        return await self.polymarket_client.reconcile_fills_via_sidecar(request)

    def _build_reprice_plan(self, *, copied_trade: CopiedTrade) -> ExecutionPlan | None:
        requested_slippage_bps = max(float(settings.max_allowed_slippage_bps), 0.0)
        requested_price_cents = round(
            float(copied_trade.price_cents) * (1.0 + (requested_slippage_bps / 10_000.0)),
            4,
        )
        allowed, actual_bps = self.risk_manager.can_accept_slippage(
            source_price_cents=float(copied_trade.price_cents),
            execution_price_cents=requested_price_cents,
            side=TradeSide.BUY.value,
            risk_mode="aggressive",
        )
        if not allowed:
            return None
        return ExecutionPlan(
            order_request=OrderRequest(
                token_id=copied_trade.token_id or "",
                side=TradeSide.BUY.value,
                price_cents=requested_price_cents,
                size_usd=round(max(float(copied_trade.size_usd or 0.0), 0.01), 2),
                market_id=copied_trade.market_id,
                outcome=copied_trade.outcome,
                order_type="GTC",
            ),
            requested_price_cents=requested_price_cents,
            requested_slippage_bps=round(actual_bps, 4),
            order_type="GTC",
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

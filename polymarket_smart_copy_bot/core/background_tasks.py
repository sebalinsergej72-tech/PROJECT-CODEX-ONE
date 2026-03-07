from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import FillMode, RiskMode, settings
from core.portfolio_tracker import PortfolioTracker
from core.risk_manager import PortfolioState, RiskManager
from core.trade_executor import TradeExecutor
from core.trade_monitor import TradeMonitor, WalletThrottler
from core.wallet_discovery import WalletDiscovery
from data.database import AsyncSessionFactory, get_scheduler_jobstore_url
from data.polymarket_client import PolymarketClient
from models.models import BotRuntimeState, CopiedTrade, MarketInfo, Position, TradeStatus, WalletScore
from utils.notifications import NotificationService

_ORCHESTRATOR_INSTANCE: "BackgroundOrchestrator | None" = None
T = TypeVar("T")


async def scheduled_wallet_score_refresh() -> None:
    """Backward-compatible scheduler entrypoint for persisted jobs."""

    if _ORCHESTRATOR_INSTANCE is None:
        logger.warning("Wallet discovery job skipped: orchestrator is not initialized")
        return
    await _ORCHESTRATOR_INSTANCE._run_wallet_discovery_job()


async def scheduled_trade_monitor() -> None:
    if _ORCHESTRATOR_INSTANCE is None:
        logger.warning("Trade monitor job skipped: orchestrator is not initialized")
        return
    await _ORCHESTRATOR_INSTANCE._run_trade_monitor_job()


async def scheduled_portfolio_refresh() -> None:
    if _ORCHESTRATOR_INSTANCE is None:
        logger.warning("Portfolio refresh job skipped: orchestrator is not initialized")
        return
    await _ORCHESTRATOR_INSTANCE._run_portfolio_refresh_job()


async def scheduled_capital_recalc() -> None:
    if _ORCHESTRATOR_INSTANCE is None:
        logger.warning("Capital recalc job skipped: orchestrator is not initialized")
        return
    await _ORCHESTRATOR_INSTANCE._run_capital_recalc_job()


async def scheduled_stale_order_cleanup() -> None:
    if _ORCHESTRATOR_INSTANCE is None:
        logger.warning("Stale order cleanup job skipped: orchestrator is not initialized")
        return
    await _ORCHESTRATOR_INSTANCE._run_stale_order_cleanup_job(expired_by_ttl=True)


# IMPROVED: status model — order fill monitoring entrypoint
async def scheduled_order_fill_monitor() -> None:
    if _ORCHESTRATOR_INSTANCE is None:
        logger.warning("Order fill monitor job skipped: orchestrator is not initialized")
        return
    await _ORCHESTRATOR_INSTANCE._run_order_fill_monitor_job()


class BackgroundOrchestrator:
    """Owns scheduler lifecycle and all bot background jobs."""

    RUNTIME_KEY_MODE = "runtime_risk_mode"
    RUNTIME_KEY_PRICE_FILTER = "runtime_price_filter_enabled"
    RUNTIME_KEY_BOOST = "runtime_high_conviction_boost"
    RUNTIME_KEY_CONSERVATIVE_SUGGESTED = "runtime_conservative_suggested"
    RUNTIME_KEY_DISCOVERY_AUTOADD = "runtime_discovery_autoadd"
    RUNTIME_KEY_TRADING_ENABLED = "runtime_trading_enabled"
    RUNTIME_KEY_DRY_RUN = "runtime_dry_run"
    RUNTIME_KEY_LIVE_STARTED_AT = "runtime_live_started_at"

    def __init__(self) -> None:
        self.polymarket_client = PolymarketClient()
        self.notifications = NotificationService(
            token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            alert_chat_id=settings.telegram_alert_chat_id,
        )

        self._risk_mode: RiskMode = settings.risk_mode
        self._dry_run: bool = settings.dry_run
        self._live_started_at: datetime | None = None
        self._price_filter_enabled: bool = not settings.disable_price_filter
        self._high_conviction_boost_enabled: bool = True
        self._discovery_autoadd_enabled: bool = settings.discovery_autoadd_default
        self._trading_enabled: bool = True

        self.wallet_discovery = WalletDiscovery(self.polymarket_client, self.notifications)
        self.trade_monitor = TradeMonitor(
            self.polymarket_client,
            risk_mode_provider=self.get_risk_mode,
            price_filter_provider=self.is_price_filter_enabled,
            short_term_provider=self.is_short_term_enabled,
        )
        self.risk_manager = RiskManager()
        self.portfolio_tracker = PortfolioTracker(self.polymarket_client)
        self.trade_executor = TradeExecutor(
            polymarket_client=self.polymarket_client,
            risk_manager=self.risk_manager,
            notifications=self.notifications,
            portfolio_tracker=self.portfolio_tracker,
        )

        self.scheduler = AsyncIOScheduler(
            timezone=settings.timezone,
            jobstores={"default": SQLAlchemyJobStore(url=get_scheduler_jobstore_url())},
        )
        self.polymarket_client.set_dry_run(self._dry_run)

        self.started_at = datetime.now(tz=timezone.utc)
        self.last_wallet_refresh_at: datetime | None = None
        self.last_trade_scan_at: datetime | None = None
        self.last_portfolio_refresh_at: datetime | None = None
        self.last_capital_recalc_at: datetime | None = None
        self.last_stale_order_cleanup_at: datetime | None = None
        self._last_stale_order_cleanup: dict[str, Any] = {
            "ok": True,
            "dry_run": self._dry_run,
            "scanned": 0,
            "stale": 0,
            "cancelled": 0,
            "failed": 0,
            "failed_ids": [],
        }
        self._tracked_wallets_count = 0
        self._last_portfolio_state = PortfolioState(
            total_equity_usd=settings.default_starting_equity,
            available_cash_usd=settings.default_starting_equity,
            exposure_usd=0.0,
            daily_pnl_usd=0.0,
            cumulative_pnl_usd=0.0,
            open_positions=0,
            positions_value_usd=0.0,
            open_order_reserve_usd=0.0,
            reported_free_balance_usd=settings.default_starting_equity,
            daily_drawdown_pct=0.0,
        )
        self._last_exchange_balances: dict[str, Any] = {
            "source": "unknown",
            "free_balance_usd": None,
            "net_free_balance_usd": None,
            "open_orders_reserved_usd": None,
            "positions_value_usd": None,
            "total_balance_usd": None,
            "positions_count": 0,
            "open_orders_count": 0,
            "updated_at": None,
        }
        self._started = False
        self._bootstrap_task: asyncio.Task[None] | None = None

    @property
    def tracked_wallets_count(self) -> int:
        return self._tracked_wallets_count

    def get_risk_mode(self) -> RiskMode:
        return self._risk_mode

    def is_price_filter_enabled(self) -> bool:
        return self._price_filter_enabled

    def is_short_term_enabled(self) -> bool:
        return settings.enable_short_term_markets

    def is_high_conviction_boost_enabled(self) -> bool:
        return self._high_conviction_boost_enabled

    def is_trading_enabled(self) -> bool:
        return self._trading_enabled

    def is_dry_run(self) -> bool:
        return self._dry_run

    async def start(self) -> None:
        global _ORCHESTRATOR_INSTANCE
        if self._started:
            return

        await self.polymarket_client.start()
        await self._in_session("load_runtime_state", self._load_runtime_state)

        self.notifications.register_status_provider(self.get_status)
        self.notifications.register_pnl_provider(self.get_pnl_status)
        self.notifications.register_controls(
            mode_setter=self.set_mode,
            trading_setter=self.set_trading_enabled,
            boost_setter=self.set_boost,
            price_filter_setter=self.set_price_filter,
            top_wallets_provider=self.get_top_wallets,
            add_wallet_handler=self.add_wallet,
            remove_wallet_handler=self.remove_wallet,
            discovery_status_provider=self.get_discovery_status,
            autoadd_setter=self.set_autoadd,
            trades_provider=self.get_recent_trades,
            positions_provider=lambda open_only, limit: self.get_positions(open_only=open_only, limit=limit),
        )
        try:
            await self.notifications.start()
        except Exception:
            logger.exception("Telegram initialization failed; continuing without Telegram")

        _ORCHESTRATOR_INSTANCE = self
        self._register_jobs()
        self.scheduler.start()

        self._bootstrap_task = asyncio.create_task(self._bootstrap_jobs(), name="orchestrator-bootstrap")

        self._started = True
        if self._risk_mode == "aggressive":
            logger.warning(
                "🚀 AGGRESSIVE MODE ENABLED | Capital: ${:.2f} | Risk per wallet: {:.0f}% | Kelly x{:.1f}",
                self._last_portfolio_state.total_equity_usd,
                settings.max_per_wallet_pct * 100,
                settings.kelly_multiplier,
            )
        logger.info("Background orchestrator started")

    async def stop(self) -> None:
        global _ORCHESTRATOR_INSTANCE
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

        if self._bootstrap_task is not None:
            self._bootstrap_task.cancel()
            try:
                await self._bootstrap_task
            except asyncio.CancelledError:
                pass
            finally:
                self._bootstrap_task = None

        await self.notifications.stop()
        await self.polymarket_client.stop()

        if _ORCHESTRATOR_INSTANCE is self:
            _ORCHESTRATOR_INSTANCE = None

        self._started = False
        logger.info("Background orchestrator stopped")

    def _register_jobs(self) -> None:
        self.scheduler.add_job(
            scheduled_wallet_score_refresh,
            trigger="interval",
            hours=settings.wallet_score_refresh_hours,
            id="wallet_score_refresh",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=180,
        )
        self.scheduler.add_job(
            scheduled_trade_monitor,
            trigger="interval",
            seconds=settings.trade_monitor_interval_seconds,
            id="trade_monitor",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
        )
        self.scheduler.add_job(
            scheduled_portfolio_refresh,
            trigger="interval",
            seconds=settings.portfolio_refresh_seconds,
            id="portfolio_refresh",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=120,
        )
        self.scheduler.add_job(
            scheduled_capital_recalc,
            trigger="interval",
            minutes=settings.capital_recalc_interval_minutes,
            id="capital_recalc",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=120,
        )
        self.scheduler.add_job(
            scheduled_stale_order_cleanup,
            trigger="interval",
            seconds=settings.stale_order_cleanup_interval_seconds,
            id="stale_order_cleanup",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=120,
        )
        # IMPROVED: status model — poll submitted orders for fills
        self.scheduler.add_job(
            scheduled_order_fill_monitor,
            trigger="interval",
            seconds=30,
            id="order_fill_monitor",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
        )

    async def _run_wallet_discovery_job(self) -> None:
        await self._in_session("wallet_discovery", self._wallet_discovery_refresh)

    async def _run_trade_monitor_job(self) -> None:
        await self._in_session("trade_monitor", self._trade_monitor_scan)

    async def _run_portfolio_refresh_job(self) -> None:
        await self._in_session("portfolio_refresh", self._portfolio_refresh)

    async def _run_capital_recalc_job(self) -> None:
        await self._in_session("capital_recalc", self._capital_recalc)

    async def _run_stale_order_cleanup_job(self, *, expired_by_ttl: bool) -> None:
        await self._in_session(
            "stale_order_cleanup",
            lambda session: self._stale_order_cleanup(session, expired_by_ttl=expired_by_ttl),
        )

    async def _run_order_fill_monitor_job(self) -> None:
        await self._in_session("order_fill_monitor", self._order_fill_monitor)

    async def _bootstrap_jobs(self) -> None:
        try:
            await self._in_session("restore_open_positions", self._restore_open_positions)
            await self._in_session("seed_wallets", self._seed_wallets)
            await self._run_wallet_discovery_job()
            await self._run_portfolio_refresh_job()
            # Purge stale positions on every startup to clean up any
            # ghost positions (e.g. from dry-run mode) that the regular
            # sync cycle might have missed.
            if not self._dry_run:
                await self.purge_stale_positions()
            await self._run_capital_recalc_job()
            await self._run_trade_monitor_job()
            await self._run_stale_order_cleanup_job(expired_by_ttl=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Bootstrap jobs failed")

    async def _in_session(self, job_name: str, callback: Callable[[AsyncSession], Awaitable[T]]) -> T | None:
        async with AsyncSessionFactory() as session:
            try:
                result = await callback(session)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                logger.exception("Job {} failed", job_name)
                return None

    async def _seed_wallets(self, session: AsyncSession) -> None:
        await self.wallet_discovery.import_seed_wallets(session, risk_mode=self._risk_mode)
        self._tracked_wallets_count = await self.wallet_discovery.count_enabled_wallets(session)

    async def _wallet_discovery_refresh(self, session: AsyncSession) -> None:
        result = await self.wallet_discovery.discover_and_score(
            session,
            auto_add=self._discovery_autoadd_enabled,
            risk_mode=self._risk_mode,
        )
        self._tracked_wallets_count = await self.wallet_discovery.count_enabled_wallets(session)
        self.last_wallet_refresh_at = datetime.now(tz=timezone.utc)

        report = result.report or self.wallet_discovery.format_result_report(result)
        logger.info("\n{}", report)
        try:
            await self.notifications.send_message(report)
        except Exception:
            logger.exception("Failed to send discovery report to Telegram")

        if result.approvals_requested > 0:
            await self.notifications.send_message(
                f"Discovery: requested approvals={result.approvals_requested}, approved={result.approvals_granted}, skipped={result.approvals_skipped}"
            )

    async def _trade_monitor_scan(self, session: AsyncSession) -> None:
        await self.trade_executor.reconcile_open_trade_states(session)
        if not self._dry_run:
            await self.portfolio_tracker.sync_account_open_positions(session)

        if not self._trading_enabled:
            self.last_trade_scan_at = datetime.now(tz=timezone.utc)
            return

        portfolio = await self.portfolio_tracker.calculate_state(session, risk_mode=self._risk_mode)

        if self.risk_manager.should_trigger_drawdown_stop(portfolio, self._risk_mode):
            closed = await self.portfolio_tracker.close_all_positions(session, reason="drawdown_stop")
            if closed > 0:
                await self.notifications.send_message(
                    f"[RISK] Drawdown stop triggered ({portfolio.daily_drawdown_pct * 100:.2f}%). Closed {closed} positions."
                )
            self._last_portfolio_state = await self.portfolio_tracker.record_snapshot(session, risk_mode=self._risk_mode)
            self.last_trade_scan_at = datetime.now(tz=timezone.utc)
            return

        intents = await self.trade_monitor.scan_for_trade_intents(session)
        if not intents:
            self.last_trade_scan_at = datetime.now(tz=timezone.utc)
            return

        throttler = WalletThrottler()
        for intent in intents:
            if throttler.is_throttled(intent.wallet_address):
                logger.info("Wallet {} throttled for this scan cycle", intent.wallet_address[:10])
                continue

            status = await self.trade_executor.execute_intent(
                session,
                intent,
                portfolio,
                risk_mode=self._risk_mode,
                fill_mode=settings.fill_mode,  # SAFETY: safe aggressive fill
                price_filter_enabled=self._price_filter_enabled,
                high_conviction_boost_enabled=self._high_conviction_boost_enabled,
            )
            if status in {TradeStatus.FILLED.value, TradeStatus.PARTIAL.value, TradeStatus.SUBMITTED.value}:
                throttler.record_success(intent.wallet_address)
            elif status is not None:
                throttler.record_failure(intent.wallet_address)
            portfolio = await self.portfolio_tracker.calculate_state(session, risk_mode=self._risk_mode)

        self.last_trade_scan_at = datetime.now(tz=timezone.utc)

    async def _portfolio_refresh(self, session: AsyncSession) -> None:
        await self.trade_executor.reconcile_open_trade_states(session)
        if not self._dry_run:
            api_balance = await self.polymarket_client.fetch_account_balance_usd()
            if api_balance is not None and api_balance >= 0:
                await self.portfolio_tracker.update_capital_base(session, api_balance)

        await self.portfolio_tracker.sync_account_open_positions(session)
        await self.portfolio_tracker.mark_to_market(session)

        self._last_portfolio_state = await self.portfolio_tracker.record_snapshot(session, risk_mode=self._risk_mode)
        self.last_portfolio_refresh_at = datetime.now(tz=timezone.utc)
        await self._refresh_exchange_balances()

        if self._risk_mode == "aggressive" and self._last_portfolio_state.total_equity_usd >= 300:
            suggested = await self._get_runtime_bool(session, self.RUNTIME_KEY_CONSERVATIVE_SUGGESTED)
            if not suggested:
                await self.notifications.send_message(
                    "Capital reached $300+. Consider switching to safer settings: /mode conservative"
                )
                await self._set_runtime_bool(session, self.RUNTIME_KEY_CONSERVATIVE_SUGGESTED, True)

    async def _capital_recalc(self, session: AsyncSession) -> None:
        capital = await self.portfolio_tracker.recalculate_capital_base(session, risk_mode=self._risk_mode)
        self.last_capital_recalc_at = datetime.now(tz=timezone.utc)
        logger.info("Capital base recalculated: ${:.2f}", capital)

    async def _stale_order_cleanup(self, session: AsyncSession, *, expired_by_ttl: bool) -> None:
        tracked_order_ids = {
            str(order_id).strip()
            for order_id in (
                await session.execute(
                    select(CopiedTrade.order_id).where(
                        CopiedTrade.order_id.is_not(None),
                        CopiedTrade.status.in_([TradeStatus.SUBMITTED.value, TradeStatus.PARTIAL.value]),
                    )
                )
            ).scalars().all()
            if str(order_id).strip()
        }
        ttl_seconds = (
            max(60, settings.aggressive_fill_ttl_seconds)
            if self._risk_mode == "aggressive"
            else max(60, settings.stale_order_ttl_minutes * 60)
        )
        result = await self.polymarket_client.cancel_stale_orders(
            stale_after_seconds=ttl_seconds,
            max_cancel=max(1, settings.stale_order_cancel_batch),
            allowed_order_ids=tracked_order_ids,
        )
        result["ttl_seconds"] = ttl_seconds
        result["tracked_order_ids"] = len(tracked_order_ids)
        result["sync_status"] = TradeStatus.EXPIRED.value if expired_by_ttl else TradeStatus.CANCELED.value
        self._last_stale_order_cleanup = result
        self.last_stale_order_cleanup_at = datetime.now(tz=timezone.utc)

        if not result.get("ok", False):
            logger.warning("Stale order cleanup failed: {}", result)
            return

        scanned = int(result.get("scanned", 0) or 0)
        stale = int(result.get("stale", 0) or 0)
        cancelled = int(result.get("cancelled", 0) or 0)
        failed = int(result.get("failed", 0) or 0)
        cancelled_ids = [
            str(order_id).strip()
            for order_id in result.get("cancelled_ids", []) or []
            if str(order_id).strip()
        ]
        if cancelled_ids:
            synced = await self.trade_executor.mark_canceled_orders(
                session,
                order_ids=cancelled_ids,
                expired=expired_by_ttl,
            )
            logger.info(
                "Synced {} stale order statuses back into copied_trades as {}",
                synced,
                TradeStatus.EXPIRED.value if expired_by_ttl else TradeStatus.CANCELED.value,
            )
        if cancelled or failed:
            logger.warning(
                "Stale order cleanup: scanned={} stale={} cancelled={} failed={}",
                scanned,
                stale,
                cancelled,
                failed,
            )
        else:
            logger.debug("Stale order cleanup: scanned={} stale={} cancelled=0 failed=0", scanned, stale)

    # IMPROVED: status model — poll submitted orders for fill confirmation
    async def _order_fill_monitor(self, session: AsyncSession) -> None:
        """Check pending GTC orders for fills. Update status and create positions on fill."""

        query = (
            select(CopiedTrade)
            .where(CopiedTrade.status.in_([
                TradeStatus.SUBMITTED.value,
                TradeStatus.PARTIAL.value,
            ]))
            .where(CopiedTrade.order_id.isnot(None))
        )
        pending_trades = list((await session.execute(query)).scalars().all())
        if not pending_trades:
            return

        now = datetime.now(tz=timezone.utc)
        filled_count = 0
        expired_count = 0

        for trade in pending_trades:
            # Check TTL expiry first
            if trade.ttl_expires_at and now >= trade.ttl_expires_at:
                cancelled = await self.polymarket_client.cancel_order(trade.order_id)
                trade.status = TradeStatus.EXPIRED.value
                trade.reason = f"{trade.reason or ''} | expired_ttl"
                expired_count += 1
                logger.info(
                    "Order {} expired (TTL) for trade {} | cancelled={}",
                    trade.order_id,
                    trade.external_trade_id,
                    cancelled,
                )
                continue

            # Poll order status from CLOB
            order_data = await self.polymarket_client.get_order_status(trade.order_id)
            if order_data is None:
                continue

            order_status = str(order_data.get("status", "")).upper()
            size_matched = float(order_data.get("size_matched", 0) or order_data.get("sizeMatched", 0) or 0)
            original_size = float(order_data.get("original_size", 0) or order_data.get("originalSize", 0) or trade.size_usd or 0)
            price = float(order_data.get("price", 0) or 0)

            if order_status in ("MATCHED", "FILLED", "CLOSED"):
                trade.status = TradeStatus.FILLED.value
                trade.filled_at = now
                trade.filled_size_usd = size_matched if size_matched > 0 else trade.size_usd
                if price > 0:
                    from utils.helpers import ensure_price_in_cents
                    trade.filled_price_cents = ensure_price_in_cents(price)
                else:
                    trade.filled_price_cents = trade.price_cents

                # Calculate slippage
                if trade.price_cents and trade.price_cents > 0 and trade.filled_price_cents:
                    slippage = RiskManager.compute_slippage_bps(
                        source_price_cents=trade.price_cents,
                        execution_price_cents=trade.filled_price_cents,
                        side=trade.side if trade.side in ("buy", "sell") else "buy",
                    )
                    trade.slippage_bps = round(slippage, 2)
                else:
                    slippage = 0.0

                trade.reason = f"{trade.reason or ''} | filled (slippage {slippage:+.1f}bps)"
                filled_count += 1

                # SAFETY: safe aggressive fill — detailed fill log
                logger.info(
                    "Filled at {:.2f}c (slippage {:+.1f} bps) | {} {} ${:.2f} order_id={} [GTC polled]",
                    trade.filled_price_cents or 0,
                    slippage,
                    trade.market_id,
                    trade.side.upper() if trade.side else "?",
                    trade.filled_size_usd or 0,
                    trade.order_id,
                )

                # Upsert position for the filled amount
                from core.trade_monitor import TradeIntent
                fill_intent = TradeIntent(
                    external_trade_id=trade.external_trade_id,
                    wallet_address=trade.wallet_address,
                    wallet_score=0.0,
                    wallet_win_rate=0.0,
                    wallet_profit_factor=1.0,
                    wallet_avg_position_size=0.0,
                    market_id=trade.market_id,
                    token_id=trade.token_id,
                    outcome=trade.outcome,
                    side=trade.side,
                    source_price_cents=trade.filled_price_cents or trade.price_cents,
                    source_size_usd=trade.filled_size_usd or trade.size_usd,
                    is_short_term=False,
                )
                await self.trade_executor._upsert_position(
                    session, fill_intent, trade.filled_size_usd or trade.size_usd
                )

            elif order_status in ("CANCELLED", "CANCELED"):
                trade.status = TradeStatus.CANCELED.value
                trade.reason = f"{trade.reason or ''} | cancelled_by_exchange"
                logger.info("Order {} cancelled by exchange for trade {}", trade.order_id, trade.external_trade_id)

            elif size_matched > 0 and original_size > 0 and size_matched < original_size:
                trade.status = TradeStatus.PARTIAL.value
                trade.filled_size_usd = size_matched
                trade.reason = f"{trade.reason or ''} | partial ({size_matched:.2f}/{original_size:.2f})"
            elif await self.trade_executor.maybe_reprice_submitted_gtc_buy(copied_trade=trade, now=now):
                logger.info("Repriced live BUY order for trade {}", trade.external_trade_id)

        if filled_count or expired_count:
            logger.info(
                "Order fill monitor: {} pending checked, {} filled, {} expired",
                len(pending_trades),
                filled_count,
                expired_count,
            )

    async def _restore_open_positions(self, session: AsyncSession) -> None:
        await self.portfolio_tracker.restore_open_positions(session)
        await self.portfolio_tracker.sync_account_open_positions(session)

    async def _load_runtime_state(self, session: AsyncSession) -> None:
        mode_raw = await self._get_runtime_text(session, self.RUNTIME_KEY_MODE)
        if mode_raw in {"aggressive", "conservative"}:
            self._risk_mode = mode_raw

        pf = await self._get_runtime_bool(session, self.RUNTIME_KEY_PRICE_FILTER)
        if pf is not None:
            self._price_filter_enabled = pf

        boost = await self._get_runtime_bool(session, self.RUNTIME_KEY_BOOST)
        if boost is not None:
            self._high_conviction_boost_enabled = boost

        autoadd = await self._get_runtime_bool(session, self.RUNTIME_KEY_DISCOVERY_AUTOADD)
        if autoadd is not None:
            self._discovery_autoadd_enabled = autoadd

        trading_enabled = await self._get_runtime_bool(session, self.RUNTIME_KEY_TRADING_ENABLED)
        if trading_enabled is not None:
            self._trading_enabled = trading_enabled

        runtime_dry_run = await self._get_runtime_bool(session, self.RUNTIME_KEY_DRY_RUN)
        if runtime_dry_run is not None:
            self._dry_run = runtime_dry_run
            self.polymarket_client.set_dry_run(runtime_dry_run)

        live_started_raw = await self._get_runtime_text(session, self.RUNTIME_KEY_LIVE_STARTED_AT)
        self._live_started_at = self._parse_iso_datetime(live_started_raw)
        if not self._dry_run and self._live_started_at is None:
            self._live_started_at = datetime.now(tz=timezone.utc)
            await self._set_runtime_text(session, self.RUNTIME_KEY_LIVE_STARTED_AT, self._live_started_at.isoformat())

    async def set_mode(self, mode: RiskMode) -> dict:
        self._risk_mode = mode
        await self._in_session(
            "set_mode",
            lambda session: self._set_runtime_text(session, self.RUNTIME_KEY_MODE, mode),
        )
        return await self.get_status()

    async def set_boost(self, enabled: bool) -> dict:
        self._high_conviction_boost_enabled = enabled
        await self._in_session(
            "set_boost",
            lambda session: self._set_runtime_bool(session, self.RUNTIME_KEY_BOOST, enabled),
        )
        return await self.get_status()

    async def set_price_filter(self, enabled: bool) -> dict:
        self._price_filter_enabled = enabled
        await self._in_session(
            "set_price_filter",
            lambda session: self._set_runtime_bool(session, self.RUNTIME_KEY_PRICE_FILTER, enabled),
        )
        return await self.get_status()

    async def set_autoadd(self, enabled: bool) -> dict:
        self._discovery_autoadd_enabled = enabled
        await self._in_session(
            "set_discovery_autoadd",
            lambda session: self._set_runtime_bool(session, self.RUNTIME_KEY_DISCOVERY_AUTOADD, enabled),
        )
        return await self.get_status()

    async def set_trading_enabled(self, enabled: bool) -> dict:
        self._trading_enabled = enabled
        await self._in_session(
            "set_trading_enabled",
            lambda session: self._set_runtime_bool(session, self.RUNTIME_KEY_TRADING_ENABLED, enabled),
        )
        if enabled:
            await self.notifications.send_message("Trading is ENABLED from dashboard/API.")
        else:
            await self.notifications.send_message("Trading is PAUSED from dashboard/API.")
        return await self.get_status()

    async def set_dry_run(self, enabled: bool) -> dict:
        previous = self._dry_run
        self._dry_run = enabled
        self.polymarket_client.set_dry_run(enabled)
        await self._in_session(
            "set_dry_run",
            lambda session: self._set_runtime_bool(session, self.RUNTIME_KEY_DRY_RUN, enabled),
        )
        if previous and not enabled:
            self._live_started_at = datetime.now(tz=timezone.utc)
            await self._in_session(
                "set_live_started_at",
                lambda session: self._set_runtime_text(
                    session,
                    self.RUNTIME_KEY_LIVE_STARTED_AT,
                    self._live_started_at.isoformat(),
                ),
            )
            live_balance = await self.polymarket_client.fetch_account_balance_usd()
            baseline = (
                float(live_balance)
                if live_balance is not None and live_balance > 0
                else max(self._last_portfolio_state.total_equity_usd, settings.default_starting_equity)
            )
            await self._in_session(
                "clear_paper_positions",
                lambda session: self.portfolio_tracker.clear_paper_positions(session),
            )
            await self._in_session(
                "reset_live_pnl_baseline",
                lambda session: self.portfolio_tracker.reset_pnl_baseline(session, baseline),
            )
            logger.info("Live PnL baseline reset to ${:.4f}", baseline)
        engine = "PAPER (DRY RUN)" if enabled else "LIVE"
        await self.notifications.send_message(f"Engine mode switched to {engine} from dashboard/API.")
        return await self.get_status()

    async def check_polymarket_credentials(self) -> dict[str, Any]:
        return await self.polymarket_client.diagnose_live_credentials()

    def issue_telegram_dashboard_session(self, init_data: str) -> dict[str, Any]:
        return self.notifications.issue_dashboard_session(init_data)

    def has_valid_dashboard_session(self, token: str | None) -> bool:
        return self.notifications.has_valid_dashboard_session(token)

    async def get_discovery_status(self) -> dict[str, Any]:
        top = await self.get_top_wallets(limit=10)
        result = self.wallet_discovery.last_result
        counters = result.counters if result else None
        return {
            "autoadd": self._discovery_autoadd_enabled,
            "last_run_at": result.ran_at.isoformat() if result else self._iso(self.last_wallet_refresh_at),
            "scanned_candidates": result.scanned_candidates if result else 0,
            "passed_filters": result.passed_filters if result else 0,
            "counters": {
                "total_candidates": counters.total_candidates if counters else 0,
                "passed_trades": counters.passed_trades if counters else 0,
                "passed_win_rate": counters.passed_win_rate if counters else 0,
                "passed_profit_factor": counters.passed_profit_factor if counters else 0,
                "passed_avg_size": counters.passed_avg_size if counters else 0,
                "passed_recency": counters.passed_recency if counters else 0,
                "passed_consecutive_losses": counters.passed_consecutive_losses if counters else 0,
                "passed_wallet_age": counters.passed_wallet_age if counters else 0,
                "passed_all_filters": counters.passed_all_filters if counters else 0,
            },
            "rejected_reasons": result.rejected_reasons if result else {},
            "stored_top": result.stored_top if result else len(top),
            "enabled_wallets": self._tracked_wallets_count,
            "mode": result.mode if result else self._risk_mode,
            "report": result.report if result else "",
            "approvals_requested": result.approvals_requested if result else 0,
            "approvals_granted": result.approvals_granted if result else 0,
            "approvals_skipped": result.approvals_skipped if result else 0,
            "top": top,
        }

    async def get_top_wallets(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self._in_session(
            "top_wallets",
            lambda session: self.wallet_discovery.get_top_wallets(session, limit=limit),
        )
        if not rows:
            return []
        return [
            {
                "address": row.address,
                "name": row.name,
                "score": row.score,
                "win_rate": row.win_rate,
                "trades_90d": row.trades_90d,
                "profit_factor": row.profit_factor,
                "avg_size": row.avg_size,
                "niche": row.niche,
                "enabled": row.enabled,
                "last_trade_ts": self._iso(row.last_trade_ts),
            }
            for row in rows
        ]

    async def add_wallet(self, address: str) -> dict:
        normalized = address.strip().lower()
        if not self._is_valid_address(normalized):
            return {"ok": False, "error": "invalid_address", "address": address}

        await self._in_session(
            "add_wallet",
            lambda session: self.wallet_discovery.add_wallet(session, normalized),
        )
        self._tracked_wallets_count = await self._enabled_wallet_count()
        return {"ok": True, "address": normalized, "enabled_wallets": self._tracked_wallets_count}

    async def remove_wallet(self, address: str) -> dict:
        normalized = address.strip().lower()
        if not self._is_valid_address(normalized):
            return {"ok": False, "error": "invalid_address", "address": address}

        removed = await self._in_session(
            "remove_wallet",
            lambda session: self.wallet_discovery.remove_wallet(session, normalized),
        )
        self._tracked_wallets_count = await self._enabled_wallet_count()
        return {"ok": bool(removed), "address": normalized, "enabled_wallets": self._tracked_wallets_count}

    async def _enabled_wallet_count(self) -> int:
        value = await self._in_session(
            "enabled_wallet_count",
            self.wallet_discovery.count_enabled_wallets,
        )
        return int(value or 0)

    async def get_status(self) -> dict:
        await self._refresh_portfolio_state_from_db()

        # Seed wallet scoring stats from WalletScore table
        seed_wallets_stats = await self._get_seed_wallets_stats()

        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append(
                {
                    "id": job.id,
                    "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                }
            )

        if self._risk_mode == "aggressive":
            risk_params = {
                "max_per_wallet_pct": settings.max_per_wallet_pct,
                "max_per_position_pct": settings.max_per_position_pct,
                "max_total_exposure_pct": settings.max_total_exposure_pct,
                "kelly_multiplier": settings.kelly_multiplier,
                "drawdown_stop_pct": settings.drawdown_stop_pct,
            }
        else:
            risk_params = {
                "max_per_wallet_pct": settings.conservative_max_per_wallet_pct,
                "max_per_position_pct": settings.conservative_max_per_position_pct,
                "max_total_exposure_pct": settings.conservative_max_total_exposure_pct,
                "kelly_multiplier": 0.0,
                "drawdown_stop_pct": 0.12,
            }

        # LIVE dashboard values should be anchored to Polymarket account balances
        # when they are available.
        display_total_equity = self._last_portfolio_state.total_equity_usd
        display_exposure = self._last_portfolio_state.positions_value_usd or self._last_portfolio_state.exposure_usd
        if not self._dry_run:
            pm_total = self._last_exchange_balances.get("total_balance_usd")
            pm_positions_value = self._last_exchange_balances.get("positions_value_usd")
            if isinstance(pm_total, (int, float)):
                display_total_equity = round(float(pm_total), 4)
            if isinstance(pm_positions_value, (int, float)):
                display_exposure = round(float(pm_positions_value), 4)

        return {
            "dry_run": self._dry_run,
            "risk_mode": self._risk_mode,
            "price_filter_enabled": self._price_filter_enabled,
            "short_term_markets_enabled": settings.enable_short_term_markets,
            "high_conviction_boost_enabled": self._high_conviction_boost_enabled,
            "discovery_autoadd": self._discovery_autoadd_enabled,
            "trading_enabled": self._trading_enabled,
            "scheduler_running": self.scheduler.running,
            "tracked_wallets": self._tracked_wallets_count,
            "open_positions": self._last_portfolio_state.open_positions,
            "total_equity_usd": display_total_equity,
            "exposure_usd": display_exposure,
            "available_cash_usd": self._last_portfolio_state.available_cash_usd,
            "open_order_reserve_usd": self._last_portfolio_state.open_order_reserve_usd,
            "daily_pnl_usd": self._last_portfolio_state.daily_pnl_usd,
            "daily_drawdown_pct": self._last_portfolio_state.daily_drawdown_pct,
            "cumulative_pnl_usd": self._last_portfolio_state.cumulative_pnl_usd,
            "live_started_at": self._iso(self._live_started_at),
            "last_wallet_refresh_at": self._iso(self.last_wallet_refresh_at),
            "last_trade_scan_at": self._iso(self.last_trade_scan_at),
            "last_portfolio_refresh_at": self._iso(self.last_portfolio_refresh_at),
            "last_capital_recalc_at": self._iso(self.last_capital_recalc_at),
            "last_stale_order_cleanup_at": self._iso(self.last_stale_order_cleanup_at),
            "last_stale_order_cleanup": self._last_stale_order_cleanup,
            "account_balances": self._last_exchange_balances,
            "discovery_filter_stats": (
                self.wallet_discovery.last_result.rejected_reasons if self.wallet_discovery.last_result else {}
            ),
            "discovery_scanned_candidates": (
                self.wallet_discovery.last_result.scanned_candidates if self.wallet_discovery.last_result else 0
            ),
            "discovery_passed_filters": (
                self.wallet_discovery.last_result.passed_filters if self.wallet_discovery.last_result else 0
            ),
            "last_discovery_stats": (
                {
                    "mode": self.wallet_discovery.last_result.mode,
                    "ran_at": self.wallet_discovery.last_result.ran_at.isoformat(),
                    "total_candidates": self.wallet_discovery.last_result.counters.total_candidates,
                    "passed_trades": self.wallet_discovery.last_result.counters.passed_trades,
                    "passed_win_rate": self.wallet_discovery.last_result.counters.passed_win_rate,
                    "passed_profit_factor": self.wallet_discovery.last_result.counters.passed_profit_factor,
                    "passed_avg_size": self.wallet_discovery.last_result.counters.passed_avg_size,
                    "passed_recency": self.wallet_discovery.last_result.counters.passed_recency,
                    "passed_consecutive_losses": self.wallet_discovery.last_result.counters.passed_consecutive_losses,
                    "passed_wallet_age": self.wallet_discovery.last_result.counters.passed_wallet_age,
                    "passed_all_filters": self.wallet_discovery.last_result.counters.passed_all_filters,
                    "stored_top": self.wallet_discovery.last_result.stored_top,
                    "enabled_wallets": self.wallet_discovery.last_result.enabled_wallets,
                    "report": self.wallet_discovery.last_result.report,
                }
                if self.wallet_discovery.last_result
                else {
                    "mode": self._risk_mode,
                    "ran_at": None,
                    "total_candidates": 0,
                    "passed_trades": 0,
                    "passed_win_rate": 0,
                    "passed_profit_factor": 0,
                    "passed_avg_size": 0,
                    "passed_recency": 0,
                    "passed_consecutive_losses": 0,
                    "passed_wallet_age": 0,
                    "passed_all_filters": 0,
                    "stored_top": 0,
                    "enabled_wallets": self._tracked_wallets_count,
                    "report": "",
                }
            ),
            "risk_params": risk_params,
            "jobs": jobs,
            "seed_wallets": seed_wallets_stats,
        }

    async def _get_seed_wallets_stats(self) -> dict:
        """Query QualifiedWallet table for wallet scoring stats."""
        try:
            from models.qualified_wallet import QualifiedWallet
            async with AsyncSessionFactory() as session:
                rows = (await session.execute(select(QualifiedWallet))).scalars().all()
                total = len(rows)
                enabled = sum(1 for r in rows if r.enabled)
                disabled = total - enabled

                # Score distribution for enabled wallets
                scores = [r.score for r in rows if r.enabled]
                avg_score = sum(scores) / len(scores) if scores else 0.0
                avg_wr = sum(r.win_rate for r in rows if r.enabled) / enabled if enabled else 0.0
                avg_pf = sum(r.profit_factor for r in rows if r.enabled) / enabled if enabled else 0.0

                # Metric ranges for rejected/disabled wallets
                reasons: dict[str, int] = {}
                for r in rows:
                    if not r.enabled:
                        if r.win_rate < 0.58:
                            reasons["low_win_rate"] = reasons.get("low_win_rate", 0) + 1
                        elif r.trades_90d < 80:
                            reasons["low_trades"] = reasons.get("low_trades", 0) + 1
                        elif r.profit_factor < 1.40:
                            reasons["low_pf"] = reasons.get("low_pf", 0) + 1
                        elif r.avg_size < 250:
                            reasons["low_avg_size"] = reasons.get("low_avg_size", 0) + 1
                        else:
                            reasons["other"] = reasons.get("other", 0) + 1

                return {
                    "total": total,
                    "enabled": enabled,
                    "disabled": disabled,
                    "avg_score": round(avg_score, 4),
                    "avg_win_rate": round(avg_wr, 4),
                    "avg_profit_factor": round(avg_pf, 4),
                    "disable_reasons": reasons,
                }
        except Exception:
            logger.exception("Failed to get seed wallet stats")
            return {"total": 0, "enabled": 0, "disabled": 0, "avg_score": 0, "avg_win_rate": 0, "avg_profit_factor": 0, "disable_reasons": {}}

    async def get_pnl_status(self) -> dict:
        await self._refresh_portfolio_state_from_db()
        return {
            "total_equity_usd": self._last_portfolio_state.total_equity_usd,
            "exposure_usd": self._last_portfolio_state.exposure_usd,
            "daily_pnl_usd": self._last_portfolio_state.daily_pnl_usd,
            "daily_drawdown_pct": self._last_portfolio_state.daily_drawdown_pct,
            "cumulative_pnl_usd": self._last_portfolio_state.cumulative_pnl_usd,
        }

    async def _refresh_portfolio_state_from_db(self) -> None:
        """Keep status responses consistent with DB after restarts and new trades."""

        state = await self._in_session(
            "status_portfolio_state",
            lambda session: self.portfolio_tracker.calculate_state(session, risk_mode=self._risk_mode),
        )
        if state is not None:
            self._last_portfolio_state = state

    async def _refresh_exchange_balances(self) -> None:
        """Refresh live balances directly from Polymarket for dashboard display."""

        try:
            balances = await self.polymarket_client.fetch_live_account_balances()
        except Exception:
            logger.exception("Failed to refresh live account balances from Polymarket")
            return

        now_iso = self._iso(datetime.now(tz=timezone.utc))
        self._last_exchange_balances = {
            "source": balances.get("source", "unknown"),
            "free_balance_usd": balances.get("free_balance_usd"),
            "net_free_balance_usd": balances.get("net_free_balance_usd"),
            "open_orders_reserved_usd": balances.get("open_orders_reserved_usd"),
            "positions_value_usd": balances.get("positions_value_usd"),
            "total_balance_usd": balances.get("total_balance_usd"),
            "positions_count": balances.get("positions_count", 0),
            "open_orders_count": balances.get("open_orders_count", 0),
            "updated_at": now_iso,
        }

    async def run_trade_cycle_now(self) -> dict[str, Any]:
        if not self._trading_enabled:
            return {"ok": False, "reason": "trading_paused"}
        await self._run_trade_monitor_job()
        return {"ok": True, "last_trade_scan_at": self._iso(self.last_trade_scan_at)}

    async def run_capital_recalc_now(self) -> dict[str, Any]:
        await self._run_capital_recalc_job()
        await self._refresh_portfolio_state_from_db()
        return {
            "ok": True,
            "last_capital_recalc_at": self._iso(self.last_capital_recalc_at),
            "total_equity_usd": self._last_portfolio_state.total_equity_usd,
        }

    async def run_portfolio_refresh_now(self) -> dict[str, Any]:
        await self._run_portfolio_refresh_job()
        await self._refresh_portfolio_state_from_db()
        return {
            "ok": True,
            "last_portfolio_refresh_at": self._iso(self.last_portfolio_refresh_at),
        }

    async def purge_stale_positions(self) -> dict[str, Any]:
        """Close all DB open positions NOT confirmed by the current Polymarket account.

        Useful for cleaning up dry-run leftovers or orphaned positions after a
        mode switch.  The purge is skipped if Polymarket returns no positions at all
        (to protect against accidental mass-close on a transient API failure).
        """
        if self._dry_run:
            return {"ok": False, "reason": "dry_run_mode", "purged": 0}

        remote = await self.polymarket_client.fetch_account_open_positions(limit=500)
        if remote is None:
            return {"ok": False, "reason": "api_unavailable", "purged": 0}

        # Build two lookup sets:
        # 1. Exact: market_id | token_id | outcome
        # 2. Fallback: market_id | outcome  (used when local token_id is None)
        remote_keys: set[str] = set()
        remote_market_outcome_keys: set[str] = set()
        for rp in remote:
            token = (rp.token_id or "").strip().lower()
            key = f"{rp.market_id.strip().lower()}|{token}|{rp.outcome.strip().lower()}"
            remote_keys.add(key)
            mo_key = f"{rp.market_id.strip().lower()}|{rp.outcome.strip().lower()}"
            remote_market_outcome_keys.add(mo_key)

        # If Polymarket returned zero positions, only purge when we explicitly know
        # the account is empty (remote is an empty list, not None which means failure).
        # We still run the purge when the list is legitimately empty.

        async def _do_purge(session: AsyncSession) -> int:
            open_rows = (
                await session.execute(select(Position).where(Position.is_open.is_(True)))
            ).scalars().all()
            now = datetime.now(tz=timezone.utc)
            purged = 0
            for row in open_rows:
                token = (row.token_id or "").strip().lower()
                key = f"{row.market_id.strip().lower()}|{token}|{row.outcome.strip().lower()}"
                if key in remote_keys:
                    continue
                # For account_sync positions with missing token_id, use the
                # (market_id, outcome) fallback to avoid purging positions whose
                # token_id was simply never persisted.
                if row.wallet_address == "account_sync" and not row.token_id:
                    mo_key = f"{row.market_id.strip().lower()}|{row.outcome.strip().lower()}"
                    if mo_key in remote_market_outcome_keys:
                        continue
                # For copied positions (non-account_sync), no fallback: if the
                # exact key doesn't match remote, the position is stale.
                row.realized_pnl_usd = round(row.realized_pnl_usd + row.unrealized_pnl_usd, 4)
                row.unrealized_pnl_usd = 0.0
                row.is_open = False
                row.closed_at = now
                row.updated_at = now
                purged += 1
            return purged

        purged = await self._in_session("purge_stale_positions", _do_purge)
        purged = purged or 0
        if purged:
            logger.info("Purged {} stale/unconfirmed open positions", purged)
            await self._refresh_portfolio_state_from_db()
        return {"ok": True, "purged": purged, "remote_confirmed": len(remote)}

    async def manual_close_position(self, position_id: int) -> dict:
        """Manually force close an open position by placing a market-equivalent sell order."""
        if self._dry_run:
            return {"success": False, "error": "Cannot manually close positions in dry_run mode."}

        async with self._db_session() as session:
            query = select(Position).where(Position.id == position_id, Position.is_open.is_(True))
            position = (await session.execute(query)).scalar_one_or_none()
            if not position:
                return {"success": False, "error": f"Open position {position_id} not found."}

            if not position.token_id or position.quantity <= 0:
                return {"success": False, "error": "Position missing token_id or has 0 quantity."}

            # To sell out effectively we place a sell order for the current holdings.
            # Using price 0 or minimal valid price (e.g. 0.1 cents) to act like a market order or limit at bottom.
            from data.polymarket_client import OrderRequest
            from core.trade_monitor import TradeIntent
            from datetime import datetime, timezone
            
            # Use size_usd = quantity * 1.0 (assuming maximum payout per share is 1)
            # Or in this bot's notation size is just token quantity if price is unknown,
            # but PolymarketClient expects size_usd (size to buy/sell).
            
            # Execute directly via the client to ensure it goes through immediately.
            sell_request = OrderRequest(
                token_id=position.token_id,
                side="sell",
                price_cents=0.1,  # Minimum valid price tick
                size_usd=position.quantity, 
                market_id=position.market_id,
                outcome=position.outcome,
            )
            
            logger.info("Manual Close requested for Position {}: Sell {} shares of {}/{}", 
                        position_id, position.quantity, position.market_id, position.outcome)
            
            result = await self.polymarket_client.place_order(sell_request)
            
            if not result.success:
                logger.error("Manual close failed for Position {}: {}", position_id, result.error)
                return {"success": False, "error": f"API Error: {result.error}"}
            
            # If successful, mark position as closed
            position.is_open = False
            position.closed_at = datetime.now(tz=timezone.utc)
            position.realized_pnl_usd = position.unrealized_pnl_usd
            position.unrealized_pnl_usd = 0.0
            await session.commit()
            
            await self.notifications.send_message(
                f"🛑 <b>Manual Close</b>\n{position.market_id} | {position.outcome}\nQuantity: {position.quantity:.2f}",
                parse_mode="HTML",
            )
            
            # Kick off async sync of balance / portfolios
            asyncio.create_task(self.run_portfolio_refresh_now())
            
            return {
                "success": True, 
                "tx_hash": result.tx_hash,
                "order_id": result.order_id
            }

    async def run_discovery_now(self) -> dict[str, Any]:
        await self._run_wallet_discovery_job()
        stats = await self.get_discovery_status()
        return {
            "ok": True,
            "last_wallet_refresh_at": self._iso(self.last_wallet_refresh_at),
            "discovery": stats,
        }

    async def run_stale_order_cleanup_now(self) -> dict[str, Any]:
        await self._run_stale_order_cleanup_job(expired_by_ttl=False)
        return {
            "ok": bool(self._last_stale_order_cleanup.get("ok", False)),
            "last_stale_order_cleanup_at": self._iso(self.last_stale_order_cleanup_at),
            "cleanup": self._last_stale_order_cleanup,
        }

    async def get_recent_trades(self, limit: int = 20) -> list[dict[str, Any]]:
        clamped_limit = max(1, min(limit, 200))
        rows = await self._in_session(
            "recent_trades",
            lambda session: self._fetch_recent_trades(session, clamped_limit),
        )
        return rows or []

    async def get_open_positions_count(self) -> int:
        """Return total count of open positions (no limit applied)."""
        value = await self._in_session(
            "open_positions_count",
            lambda session: self._count_open_positions(session),
        )
        return int(value or 0)

    async def get_positions(self, *, open_only: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        clamped_limit = max(1, min(limit, 200))
        rows = await self._in_session(
            "positions",
            lambda session: self._fetch_positions(session, open_only=open_only, limit=clamped_limit),
        )
        return rows or []

    async def get_open_orders(self, *, limit: int = 100) -> list[dict[str, Any]]:
        clamped_limit = max(1, min(limit, 200))
        rows = await self.polymarket_client.fetch_open_orders()
        if rows is None:
            return []

        rows = sorted(rows, key=lambda row: row.created_at, reverse=True)[:clamped_limit]
        order_ids = [row.order_id for row in rows if row.order_id]
        linked = (
            await self._in_session(
                "open_order_trade_meta",
                lambda session: self._fetch_open_order_trade_meta(session, order_ids),
            )
            if order_ids
            else {}
        ) or {}

        return [
            {
                "order_id": row.order_id,
                "market_id": row.market_id,
                "token_id": row.token_id,
                "side": row.side,
                "price_cents": row.price_cents,
                "size_shares": row.size,
                "notional_usd_estimate": max((row.price_cents / 100.0) * row.size, 0.0),
                "created_at": self._iso(row.created_at),
                "trade_status": linked.get(row.order_id, {}).get("trade_status"),
                "wallet_address": linked.get(row.order_id, {}).get("wallet_address"),
                "outcome": linked.get(row.order_id, {}).get("outcome"),
            }
            for row in rows
        ]

    async def _fetch_recent_trades(self, session: AsyncSession, limit: int) -> list[dict[str, Any]]:
        query = select(CopiedTrade).order_by(CopiedTrade.copied_at.desc()).limit(limit)
        rows = (await session.execute(query)).scalars().all()
        result = [
            {
                "id": row.id,
                "external_trade_id": row.external_trade_id,
                "wallet_address": row.wallet_address,
                "market_id": row.market_id,
                "token_id": row.token_id,
                "outcome": row.outcome,
                "side": row.side,
                "price_cents": row.price_cents,
                "size_usd": row.size_usd,
                "status": row.status,
                "reason": row.reason,
                "order_id": row.order_id,
                "tx_hash": row.tx_hash,
                "submitted_at": self._iso(row.submitted_at),
                "filled_at": self._iso(row.filled_at),
                "canceled_at": self._iso(row.canceled_at),
                "filled_quantity": row.filled_quantity,
                "filled_size_usd": row.filled_size_usd,
                "filled_price_cents": row.filled_price_cents,
                "source_timestamp": self._iso(row.source_timestamp),
                "copied_at": self._iso(row.copied_at),
            }
            for row in rows
        ]
        return await self._enrich_with_market_info(session, result)

    @staticmethod
    async def _count_open_positions(session: AsyncSession) -> int:
        query = select(func.count()).select_from(Position).where(Position.is_open.is_(True))
        return int((await session.execute(query)).scalar_one())

    async def _fetch_positions(self, session: AsyncSession, *, open_only: bool, limit: int) -> list[dict[str, Any]]:
        base_filters = []
        if open_only:
            base_filters.append(Position.is_open.is_(True))
            base_filters.append(Position.quantity > 0)
            base_filters.append(Position.invested_usd > 0)

        live_sync_exists = False
        if not self._dry_run:
            live_sync_query = select(Position.id).where(
                Position.wallet_address == self.portfolio_tracker.ACCOUNT_SYNC_WALLET,
            )
            if open_only:
                live_sync_query = live_sync_query.where(Position.is_open.is_(True))
            live_sync_exists = (await session.execute(live_sync_query.limit(1))).scalar_one_or_none() is not None

        query = select(Position)
        for condition in base_filters:
            query = query.where(condition)
        if live_sync_exists:
            query = query.where(Position.wallet_address == self.portfolio_tracker.ACCOUNT_SYNC_WALLET)

        query = query.order_by(Position.updated_at.desc()).limit(limit)
        rows = (await session.execute(query)).scalars().all()

        # When listing open positions, filter out dust (tiny leftover shares
        # worth less than the display threshold).  Keeps the dashboard clean
        # and aligned with what Polymarket UI shows.
        from core.portfolio_tracker import PortfolioTracker
        dust_threshold = PortfolioTracker.DUST_VALUE_THRESHOLD_USD
        if open_only:
            rows = [
                r for r in rows
                if (r.quantity * r.current_price_cents / 100) >= dust_threshold
            ]

        result = [
            {
                "id": row.id,
                "wallet_address": row.wallet_address,
                "market_id": row.market_id,
                "token_id": row.token_id,
                "outcome": row.outcome,
                "side": row.side,
                "quantity": row.quantity,
                "avg_price_cents": row.avg_price_cents,
                "invested_usd": row.invested_usd,
                "current_price_cents": row.current_price_cents,
                "realized_pnl_usd": row.realized_pnl_usd,
                "unrealized_pnl_usd": row.unrealized_pnl_usd,
                "is_open": row.is_open,
                "opened_at": self._iso(row.opened_at),
                "updated_at": self._iso(row.updated_at),
                "closed_at": self._iso(row.closed_at),
            }
            for row in rows
        ]
        return await self._enrich_with_market_info(session, result)

    async def _enrich_with_market_info(
        self,
        session: AsyncSession,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Add ``market_title`` and ``market_category`` to each row dict.

        Uses the ``market_info`` cache table.  Missing or stale (>24 h)
        entries are fetched from the Polymarket Gamma API and stored.
        """

        if not rows:
            return rows

        # 1. Collect unique market_ids
        market_ids = list({r["market_id"] for r in rows if r.get("market_id")})
        if not market_ids:
            for r in rows:
                r.setdefault("market_title", "")
                r.setdefault("market_category", "")
            return rows

        # 2. Batch cache lookup
        cached_rows = (
            await session.execute(select(MarketInfo).where(MarketInfo.market_id.in_(market_ids)))
        ).scalars().all()
        cache: dict[str, MarketInfo] = {c.market_id: c for c in cached_rows}

        # 3. Find missing / stale entries (TTL 24 h) or entries with empty question
        #    (can happen when Gamma API previously returned an unhandled list response)
        now = datetime.now(tz=timezone.utc)
        ttl = timedelta(hours=24)
        to_fetch = [
            mid for mid in market_ids
            if mid not in cache
            or not cache[mid].question          # empty from a previous failed fetch
            or (now - cache[mid].fetched_at) > ttl
        ]

        # 4. Fetch from Gamma API with concurrency limit
        if to_fetch:
            sem = asyncio.Semaphore(5)

            async def _fetch_one(mid: str) -> tuple[str, str, str]:
                async with sem:
                    try:
                        info = await self.polymarket_client.fetch_market_info(mid)
                        return mid, info.question, info.category
                    except Exception:
                        logger.debug("Market info fetch failed for {}", mid)
                        return mid, "", ""

            results = await asyncio.gather(*[_fetch_one(mid) for mid in to_fetch])

            # 5. Upsert cache
            for mid, question, category in results:
                existing = cache.get(mid)
                if existing is not None:
                    existing.question = question or existing.question
                    existing.category = category or existing.category
                    existing.fetched_at = now
                else:
                    entry = MarketInfo(
                        market_id=mid,
                        question=question,
                        category=category,
                        fetched_at=now,
                    )
                    session.add(entry)
                    cache[mid] = entry

        # 6. Enrich rows
        for r in rows:
            cached = cache.get(r.get("market_id", ""))
            r["market_title"] = (cached.question if cached else "") or ""
            r["market_category"] = (cached.category if cached else "") or ""

        return rows

    async def _fetch_open_order_trade_meta(
        self,
        session: AsyncSession,
        order_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        if not order_ids:
            return {}

        query = (
            select(CopiedTrade)
            .where(CopiedTrade.order_id.in_(order_ids))
            .order_by(CopiedTrade.copied_at.desc())
        )
        rows = (await session.execute(query)).scalars().all()

        linked: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not row.order_id or row.order_id in linked:
                continue
            linked[row.order_id] = {
                # IMPROVED: expose local lifecycle state next to the real exchange open order.
                "trade_status": row.status,
                "wallet_address": row.wallet_address,
                "outcome": row.outcome,
            }
        return linked

    async def _get_runtime_text(self, session: AsyncSession, key: str) -> str | None:
        row = (await session.execute(select(BotRuntimeState).where(BotRuntimeState.key == key))).scalar_one_or_none()
        return row.value if row else None

    async def _set_runtime_text(self, session: AsyncSession, key: str, value: str) -> None:
        row = (await session.execute(select(BotRuntimeState).where(BotRuntimeState.key == key))).scalar_one_or_none()
        now = datetime.now(tz=timezone.utc)
        if row is None:
            session.add(BotRuntimeState(key=key, value=value, updated_at=now))
            return
        row.value = value
        row.updated_at = now

    async def _get_runtime_bool(self, session: AsyncSession, key: str) -> bool | None:
        raw = await self._get_runtime_text(session, key)
        if raw is None:
            return None
        return raw.lower() in {"1", "true", "on", "yes"}

    async def _set_runtime_bool(self, session: AsyncSession, key: str, value: bool) -> None:
        await self._set_runtime_text(session, key, "true" if value else "false")

    @staticmethod
    def _is_valid_address(address: str) -> bool:
        return address.startswith("0x") and len(address) == 42

    @staticmethod
    def _iso(dt: datetime | None) -> str | None:
        return dt.isoformat() if dt else None

    @staticmethod
    def _parse_iso_datetime(raw: str | None) -> datetime | None:
        if not raw:
            return None
        text = raw.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import RiskMode, settings
from core.portfolio_tracker import PortfolioTracker
from core.risk_manager import PortfolioState, RiskManager
from core.trade_executor import TradeExecutor
from core.trade_monitor import TradeMonitor
from core.wallet_discovery import WalletDiscovery
from data.database import AsyncSessionFactory, get_scheduler_jobstore_url
from data.polymarket_client import PolymarketClient
from models.models import BotRuntimeState, CopiedTrade, Position
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


class BackgroundOrchestrator:
    """Owns scheduler lifecycle and all bot background jobs."""

    RUNTIME_KEY_MODE = "runtime_risk_mode"
    RUNTIME_KEY_PRICE_FILTER = "runtime_price_filter_enabled"
    RUNTIME_KEY_BOOST = "runtime_high_conviction_boost"
    RUNTIME_KEY_CONSERVATIVE_SUGGESTED = "runtime_conservative_suggested"
    RUNTIME_KEY_DISCOVERY_AUTOADD = "runtime_discovery_autoadd"
    RUNTIME_KEY_TRADING_ENABLED = "runtime_trading_enabled"
    RUNTIME_KEY_DRY_RUN = "runtime_dry_run"

    def __init__(self) -> None:
        self.polymarket_client = PolymarketClient()
        self.notifications = NotificationService(
            token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )

        self._risk_mode: RiskMode = settings.risk_mode
        self._dry_run: bool = settings.dry_run
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
        self._tracked_wallets_count = 0
        self._last_portfolio_state = PortfolioState(
            total_equity_usd=settings.default_starting_equity,
            available_cash_usd=settings.default_starting_equity,
            exposure_usd=0.0,
            daily_pnl_usd=0.0,
            cumulative_pnl_usd=0.0,
            open_positions=0,
            daily_drawdown_pct=0.0,
        )
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

    async def _run_wallet_discovery_job(self) -> None:
        await self._in_session("wallet_discovery", self._wallet_discovery_refresh)

    async def _run_trade_monitor_job(self) -> None:
        await self._in_session("trade_monitor", self._trade_monitor_scan)

    async def _run_portfolio_refresh_job(self) -> None:
        await self._in_session("portfolio_refresh", self._portfolio_refresh)

    async def _run_capital_recalc_job(self) -> None:
        await self._in_session("capital_recalc", self._capital_recalc)

    async def _bootstrap_jobs(self) -> None:
        try:
            await self._in_session("restore_open_positions", self._restore_open_positions)
            await self._in_session("seed_wallets", self._seed_wallets)
            await self._run_wallet_discovery_job()
            await self._run_portfolio_refresh_job()
            await self._run_capital_recalc_job()
            await self._run_trade_monitor_job()
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

        for intent in intents:
            await self.trade_executor.execute_intent(
                session,
                intent,
                portfolio,
                risk_mode=self._risk_mode,
                price_filter_enabled=self._price_filter_enabled,
                high_conviction_boost_enabled=self._high_conviction_boost_enabled,
            )
            portfolio = await self.portfolio_tracker.calculate_state(session, risk_mode=self._risk_mode)

        self.last_trade_scan_at = datetime.now(tz=timezone.utc)

    async def _portfolio_refresh(self, session: AsyncSession) -> None:
        await self.portfolio_tracker.mark_to_market(session)
        self._last_portfolio_state = await self.portfolio_tracker.record_snapshot(session, risk_mode=self._risk_mode)
        self.last_portfolio_refresh_at = datetime.now(tz=timezone.utc)

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

    async def _restore_open_positions(self, session: AsyncSession) -> None:
        await self.portfolio_tracker.restore_open_positions(session)

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
        self._dry_run = enabled
        self.polymarket_client.set_dry_run(enabled)
        await self._in_session(
            "set_dry_run",
            lambda session: self._set_runtime_bool(session, self.RUNTIME_KEY_DRY_RUN, enabled),
        )
        engine = "PAPER (DRY RUN)" if enabled else "LIVE"
        await self.notifications.send_message(f"Engine mode switched to {engine} from dashboard/API.")
        return await self.get_status()

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
            "used_seed_fallback": result.used_seed_fallback if result else False,
            "seed_fallback_wallets": result.seed_fallback_wallets if result else 0,
            "used_discovered_fallback": result.used_discovered_fallback if result else False,
            "discovered_fallback_wallets": result.discovered_fallback_wallets if result else 0,
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
            "total_equity_usd": self._last_portfolio_state.total_equity_usd,
            "exposure_usd": self._last_portfolio_state.exposure_usd,
            "daily_pnl_usd": self._last_portfolio_state.daily_pnl_usd,
            "daily_drawdown_pct": self._last_portfolio_state.daily_drawdown_pct,
            "cumulative_pnl_usd": self._last_portfolio_state.cumulative_pnl_usd,
            "last_wallet_refresh_at": self._iso(self.last_wallet_refresh_at),
            "last_trade_scan_at": self._iso(self.last_trade_scan_at),
            "last_portfolio_refresh_at": self._iso(self.last_portfolio_refresh_at),
            "last_capital_recalc_at": self._iso(self.last_capital_recalc_at),
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
                    "used_seed_fallback": self.wallet_discovery.last_result.used_seed_fallback,
                    "seed_fallback_wallets": self.wallet_discovery.last_result.seed_fallback_wallets,
                    "used_discovered_fallback": self.wallet_discovery.last_result.used_discovered_fallback,
                    "discovered_fallback_wallets": self.wallet_discovery.last_result.discovered_fallback_wallets,
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
                    "used_seed_fallback": False,
                    "seed_fallback_wallets": 0,
                    "used_discovered_fallback": False,
                    "discovered_fallback_wallets": 0,
                    "report": "",
                }
            ),
            "risk_params": risk_params,
            "jobs": jobs,
        }

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

    async def run_trade_cycle_now(self) -> dict[str, Any]:
        if not self._trading_enabled:
            return {"ok": False, "reason": "trading_paused"}
        await self._run_trade_monitor_job()
        return {"ok": True, "last_trade_scan_at": self._iso(self.last_trade_scan_at)}

    async def get_recent_trades(self, limit: int = 20) -> list[dict[str, Any]]:
        clamped_limit = max(1, min(limit, 200))
        rows = await self._in_session(
            "recent_trades",
            lambda session: self._fetch_recent_trades(session, clamped_limit),
        )
        return rows or []

    async def get_positions(self, *, open_only: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        clamped_limit = max(1, min(limit, 200))
        rows = await self._in_session(
            "positions",
            lambda session: self._fetch_positions(session, open_only=open_only, limit=clamped_limit),
        )
        return rows or []

    async def _fetch_recent_trades(self, session: AsyncSession, limit: int) -> list[dict[str, Any]]:
        query = select(CopiedTrade).order_by(CopiedTrade.copied_at.desc()).limit(limit)
        rows = (await session.execute(query)).scalars().all()
        return [
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
                "tx_hash": row.tx_hash,
                "source_timestamp": self._iso(row.source_timestamp),
                "copied_at": self._iso(row.copied_at),
            }
            for row in rows
        ]

    async def _fetch_positions(self, session: AsyncSession, *, open_only: bool, limit: int) -> list[dict[str, Any]]:
        query = select(Position).order_by(Position.updated_at.desc()).limit(limit)
        if open_only:
            query = query.where(Position.is_open.is_(True))

        rows = (await session.execute(query)).scalars().all()
        return [
            {
                "id": row.id,
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

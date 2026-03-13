from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from data.polymarket_client import PolymarketClient, WalletOpenPosition, WalletTradeSignal
from models.models import CopiedTrade, Position, TradeSide, TradeStatus
from models.qualified_wallet import QualifiedWallet


@dataclass(slots=True)
class TradeIntent:
    external_trade_id: str
    wallet_address: str
    wallet_score: float
    wallet_win_rate: float
    wallet_profit_factor: float
    wallet_avg_position_size: float
    market_id: str
    token_id: str | None
    outcome: str
    side: str
    source_price_cents: float
    source_size_usd: float
    is_short_term: bool
    market_slug: str | None = None
    market_category: str | None = None


class WalletThrottler:
    def __init__(self) -> None:
        self.cycle_failures: defaultdict[str, int] = defaultdict(int)

    def record_failure(self, wallet_address: str) -> None:
        self.cycle_failures[wallet_address.lower()] += 1

    def record_success(self, wallet_address: str) -> None:
        self.cycle_failures[wallet_address.lower()] = 0

    def is_throttled(self, wallet_address: str) -> bool:
        return self.cycle_failures[wallet_address.lower()] >= settings.max_consecutive_wallet_failures_per_cycle

    def reset_cycle(self) -> None:
        self.cycle_failures.clear()


class TradeMonitor:
    """Monitors qualified wallets and extracts copy-trade candidates."""

    ACCOUNT_SYNC_WALLET = "account_sync"
    MARKET_BLACKLIST_PATTERNS = (
        re.compile(r".*-1h$", re.IGNORECASE),
        re.compile(r".*-1d$", re.IGNORECASE),
        re.compile(r".*hourly.*", re.IGNORECASE),
        re.compile(r".*minute.*", re.IGNORECASE),
    )
    CATEGORY_BLACKLIST = {"short_term_crypto"}

    def __init__(
        self,
        polymarket_client: PolymarketClient,
        *,
        risk_mode_provider: Callable[[], str],
        price_filter_provider: Callable[[], bool],
        short_term_provider: Callable[[], bool],
    ) -> None:
        self.polymarket_client = polymarket_client
        self._risk_mode_provider = risk_mode_provider
        self._price_filter_provider = price_filter_provider
        self._short_term_provider = short_term_provider

    async def scan_for_trade_intents(self, session: AsyncSession) -> list[TradeIntent]:
        fresh = await self.scan_for_fresh_trade_intents(session)
        reconcile = await self.scan_for_reconcile_intents(session)
        intents = [*fresh, *reconcile]
        intents.sort(key=lambda intent: intent.wallet_score, reverse=True)
        return intents[:60]

    async def scan_for_fresh_trade_intents(self, session: AsyncSession) -> list[TradeIntent]:
        risk_mode = self._risk_mode_provider()
        qualified_wallets = await self._load_enabled_wallets(session, risk_mode=risk_mode)
        if not qualified_wallets:
            logger.debug("No qualified wallets to monitor")
            return []

        existing_ids = await self._fetch_existing_trade_ids(session)
        cooldown_markets, cooldown_tokens = await self._fetch_market_cooldowns(session)
        sellable_positions = await self._fetch_local_sellable_positions(session)
        intents: list[TradeIntent] = []
        trade_tasks = [
            self.polymarket_client.fetch_wallet_trades(
                wallet.address,
                limit=max(1, settings.trade_monitor_signal_fetch_limit),
            )
            for wallet in qualified_wallets
        ]
        fetched_trade_batches = await asyncio.gather(*trade_tasks, return_exceptions=True)

        for wallet, trade_batch in zip(
            qualified_wallets,
            fetched_trade_batches,
            strict=True,
        ):
            if isinstance(trade_batch, Exception):
                logger.warning("Failed to fetch trades for {}: {}", wallet.address, trade_batch)
                continue

            wallet_intents = self._signals_to_intents(
                wallet,
                trade_batch,
                existing_ids,
                cooldown_markets=cooldown_markets,
                cooldown_tokens=cooldown_tokens,
                sellable_positions=sellable_positions,
                price_filter_enabled=self._price_filter_provider(),
                short_term_enabled=self._short_term_provider(),
            )
            intents.extend(wallet_intents)

        intents.sort(key=lambda intent: intent.wallet_score, reverse=True)
        return intents[:60]

    async def scan_for_reconcile_intents(self, session: AsyncSession) -> list[TradeIntent]:
        risk_mode = self._risk_mode_provider()
        qualified_wallets = await self._load_enabled_wallets(session, risk_mode=risk_mode)
        qualified_by_address = {wallet.address.lower(): wallet for wallet in qualified_wallets}
        local_open_by_wallet = await self._fetch_local_open_positions(session)
        if not local_open_by_wallet and not qualified_by_address:
            return []

        existing_ids = await self._fetch_existing_trade_ids(session)
        now = datetime.now(tz=timezone.utc)
        reconcile_cutoff = now - timedelta(minutes=2)
        wallets_for_reconcile = sorted(set(local_open_by_wallet.keys()) | set(qualified_by_address.keys()))
        intents: list[TradeIntent] = []
        position_tasks = [
            self.polymarket_client.fetch_wallet_open_positions(wallet_address, limit=200)
            for wallet_address in wallets_for_reconcile
        ]
        fetched_position_batches = await asyncio.gather(*position_tasks, return_exceptions=True)

        for wallet_address, pos_batch in zip(wallets_for_reconcile, fetched_position_batches, strict=True):
            if isinstance(pos_batch, Exception):
                logger.warning("Failed to fetch source positions for {}: {}", wallet_address, pos_batch)
                continue

            if pos_batch is None:
                # API failure signaled by client method; skip reconciliation to avoid false close.
                continue

            wallet_meta = qualified_by_address.get(wallet_address)
            wallet_score = float(wallet_meta.score) if wallet_meta else 0.0
            wallet_win_rate = float(wallet_meta.win_rate) if wallet_meta else 0.0
            wallet_profit_factor = float(wallet_meta.profit_factor) if wallet_meta else 1.0
            wallet_avg_size = float(wallet_meta.avg_size) if wallet_meta else 0.0

            reconcile_intents = self._build_reconcile_close_intents(
                wallet_address=wallet_address,
                wallet_score=wallet_score,
                wallet_win_rate=wallet_win_rate,
                wallet_profit_factor=wallet_profit_factor,
                wallet_avg_position_size=wallet_avg_size,
                source_open_positions=pos_batch,
                local_open_positions=local_open_by_wallet.get(wallet_address, []),
                existing_ids=existing_ids,
                reconcile_cutoff=reconcile_cutoff,
                now=now,
            )
            intents.extend(reconcile_intents)

        intents.sort(key=lambda intent: intent.wallet_score, reverse=True)
        return intents[:60]

    @staticmethod
    async def _load_enabled_wallets(
        session: AsyncSession,
        *,
        risk_mode: str,
    ) -> list[QualifiedWallet]:
        limit = settings.max_wallets_aggressive if risk_mode == "aggressive" else settings.max_qualified_wallets
        query = (
            select(QualifiedWallet)
            .where(QualifiedWallet.enabled.is_(True))
            .order_by(QualifiedWallet.score.desc())
            .limit(limit)
        )
        return list((await session.execute(query)).scalars().all())

    @staticmethod
    async def _fetch_existing_trade_ids(session: AsyncSession) -> set[str]:
        query = select(CopiedTrade.external_trade_id)
        rows = (await session.execute(query)).scalars().all()
        return set(rows)

    @staticmethod
    async def _fetch_market_cooldowns(
        session: AsyncSession,
    ) -> tuple[set[str], set[str]]:
        max_cooldown_minutes = max(
            0,
            settings.price_moved_market_cooldown_minutes,
            settings.no_orderbook_market_cooldown_minutes,
            settings.low_liquidity_market_cooldown_minutes,
        )
        if max_cooldown_minutes <= 0:
            return set(), set()

        now = datetime.now(tz=timezone.utc)
        cutoff = now - timedelta(minutes=max_cooldown_minutes)
        query = select(CopiedTrade).where(
            CopiedTrade.status == TradeStatus.SKIPPED.value,
            CopiedTrade.copied_at >= cutoff,
        )
        rows = (await session.execute(query)).scalars().all()
        cooldown_markets: set[str] = set()
        cooldown_tokens: set[str] = set()
        for trade in rows:
            if not TradeMonitor._is_trade_on_market_cooldown(trade, now=now):
                continue
            if trade.market_id:
                cooldown_markets.add(str(trade.market_id).strip().lower())
            if trade.token_id:
                cooldown_tokens.add(str(trade.token_id).strip().lower())
        return cooldown_markets, cooldown_tokens

    @staticmethod
    async def _fetch_local_open_positions(
        session: AsyncSession,
    ) -> dict[str, list[Position]]:
        query = select(Position).where(
            Position.is_open.is_(True),
            Position.wallet_address != TradeMonitor.ACCOUNT_SYNC_WALLET,
        )
        rows = (await session.execute(query)).scalars().all()
        grouped: dict[str, list[Position]] = {}
        for row in rows:
            grouped.setdefault(row.wallet_address.lower(), []).append(row)
        return grouped

    @staticmethod
    async def _fetch_local_sellable_positions(
        session: AsyncSession,
    ) -> dict[str, Position]:
        query = select(Position).where(Position.is_open.is_(True))
        rows = (await session.execute(query)).scalars().all()
        sellable: dict[str, Position] = {}
        for row in rows:
            key = TradeMonitor._position_key(row.market_id, row.token_id, row.outcome)
            current = sellable.get(key)
            if current is None or row.quantity > current.quantity:
                sellable[key] = row
        return sellable

    # Maximum trade intents per wallet per scan cycle to prevent
    # rapid-fire copying when a wallet spams many trades at once.
    MAX_INTENTS_PER_WALLET = 2

    @staticmethod
    def _signals_to_intents(
        wallet: QualifiedWallet,
        signals: list[WalletTradeSignal],
        existing_ids: set[str],
        *,
        cooldown_markets: set[str],
        cooldown_tokens: set[str],
        sellable_positions: dict[str, Position],
        price_filter_enabled: bool,
        short_term_enabled: bool,
    ) -> list[TradeIntent]:
        intents: list[TradeIntent] = []

        for signal in signals:
            if signal.external_trade_id in existing_ids:
                continue

            if TradeMonitor._is_signal_on_price_moved_cooldown(
                signal,
                cooldown_markets=cooldown_markets,
                cooldown_tokens=cooldown_tokens,
            ):
                continue

            if signal.side.lower() == TradeSide.SELL.value:
                sellable_position = TradeMonitor._matching_sellable_position(signal, sellable_positions)
                if sellable_position is None:
                    continue
                if TradeMonitor._is_sell_signal_residual_too_small(signal, sellable_position):
                    continue

            is_short = TradeMonitor._is_short_term_signal(signal)
            if is_short and not short_term_enabled:
                continue

            if not TradeMonitor._is_market_tradeable(signal=signal, is_short_term=is_short):
                continue

            if price_filter_enabled and not (settings.price_min_cents <= signal.price_cents <= settings.price_max_cents):
                continue

            intents.append(
                TradeIntent(
                    external_trade_id=signal.external_trade_id,
                    wallet_address=signal.wallet_address,
                    wallet_score=wallet.score,
                    wallet_win_rate=wallet.win_rate,
                    wallet_profit_factor=float(wallet.profit_factor or 1.0),
                    wallet_avg_position_size=float(wallet.avg_size or 0.0),
                    market_id=signal.market_id,
                    token_id=signal.token_id,
                    outcome=signal.outcome,
                    side=signal.side,
                    source_price_cents=signal.price_cents,
                    source_size_usd=signal.size_usd,
                    is_short_term=is_short,
                    market_slug=signal.market_slug,
                    market_category=signal.market_category,
                )
            )

            if len(intents) >= TradeMonitor.MAX_INTENTS_PER_WALLET:
                break

        return intents

    @classmethod
    def _build_reconcile_close_intents(
        cls,
        *,
        wallet_address: str,
        wallet_score: float,
        wallet_win_rate: float,
        wallet_profit_factor: float,
        wallet_avg_position_size: float,
        source_open_positions: list[WalletOpenPosition],
        local_open_positions: list[Position],
        existing_ids: set[str],
        reconcile_cutoff: datetime,
        now: datetime,
    ) -> list[TradeIntent]:
        if not local_open_positions:
            return []

        source_keys = {
            cls._position_key(row.market_id, row.token_id, row.outcome)
            for row in source_open_positions
            if row.quantity > 0
        }
        intents: list[TradeIntent] = []

        for local in local_open_positions:
            # Use opened_at here: updated_at is touched by mark-to-market refresh and would
            # otherwise suppress reconciliation closes indefinitely.
            opened_at = local.opened_at
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
            if opened_at > reconcile_cutoff:
                continue

            local_key = cls._position_key(local.market_id, local.token_id, local.outcome)
            if local_key in source_keys:
                continue

            opposite_side = TradeSide.SELL.value if local.side == TradeSide.BUY.value else TradeSide.BUY.value
            price_cents = local.current_price_cents if local.current_price_cents > 0 else local.avg_price_cents
            if price_cents <= 0:
                continue

            external_trade_id = (
                f"reconcile_close:{wallet_address}:{local.id}:{int(now.timestamp() // 300)}"
            )
            if external_trade_id in existing_ids:
                continue

            intents.append(
                TradeIntent(
                    external_trade_id=external_trade_id,
                    wallet_address=local.wallet_address,
                    wallet_score=wallet_score + 1000.0,
                    wallet_win_rate=wallet_win_rate,
                    wallet_profit_factor=wallet_profit_factor,
                    wallet_avg_position_size=wallet_avg_position_size,
                    market_id=local.market_id,
                    token_id=local.token_id,
                    outcome=local.outcome,
                    side=opposite_side,
                    source_price_cents=price_cents,
                    # 0 triggers full-close sizing in TradeExecutor._derive_close_size_usd
                    source_size_usd=0.0,
                    is_short_term=False,
                    market_slug=None,
                    market_category=None,
                )
            )
        return intents

    @staticmethod
    def _position_key(market_id: str, token_id: str | None, outcome: str) -> str:
        token = (token_id or "").strip().lower()
        return f"{market_id.strip().lower()}|{token}|{outcome.strip().lower()}"

    @staticmethod
    def _is_short_term_signal(signal: WalletTradeSignal) -> bool:
        text = f"{signal.market_id} {signal.outcome} {signal.market_slug or ''}".lower()
        return any(token in text for token in ("5 min", "5m", "15 min", "15m", "hourly", "1h"))

    @staticmethod
    def _is_signal_on_price_moved_cooldown(
        signal: WalletTradeSignal,
        *,
        cooldown_markets: set[str],
        cooldown_tokens: set[str],
    ) -> bool:
        token_key = (signal.token_id or "").strip().lower()
        market_key = (signal.market_id or "").strip().lower()
        if token_key and token_key in cooldown_tokens:
            return True
        return bool(market_key and market_key in cooldown_markets)

    @staticmethod
    def _is_trade_on_market_cooldown(trade: CopiedTrade, *, now: datetime) -> bool:
        reason = (trade.reason or "").strip().lower()
        cooldown_minutes = 0
        if reason.startswith("price_moved:"):
            cooldown_minutes = settings.price_moved_market_cooldown_minutes
        elif reason == "no_orderbook":
            cooldown_minutes = settings.no_orderbook_market_cooldown_minutes
        elif reason.startswith("low_liquidity:"):
            cooldown_minutes = settings.low_liquidity_market_cooldown_minutes
        if cooldown_minutes <= 0 or trade.copied_at is None:
            return False
        copied_at = trade.copied_at
        if copied_at.tzinfo is None:
            copied_at = copied_at.replace(tzinfo=timezone.utc)
        return copied_at >= now - timedelta(minutes=cooldown_minutes)

    @staticmethod
    def _matching_sellable_position(
        signal: WalletTradeSignal,
        sellable_positions: dict[str, Position],
    ) -> Position | None:
        key = TradeMonitor._position_key(signal.market_id, signal.token_id, signal.outcome)
        return sellable_positions.get(key)

    @staticmethod
    def _is_sell_signal_residual_too_small(signal: WalletTradeSignal, position: Position) -> bool:
        execution_price = max(signal.price_cents / 100.0, settings.min_valid_price)
        available_size_usd = max(position.quantity, 0.0) * execution_price
        requested_size_usd = max(signal.size_usd, 0.0)
        if requested_size_usd <= 0:
            requested_size_usd = available_size_usd
        trimmed = min(available_size_usd, requested_size_usd)
        return trimmed < settings.min_trade_size_usd

    @classmethod
    def _is_market_tradeable(cls, *, signal: WalletTradeSignal, is_short_term: bool) -> bool:
        category = (signal.market_category or "").strip().lower()
        slug = (signal.market_slug or signal.market_id or "").strip().lower()
        if category in cls.CATEGORY_BLACKLIST:
            return False
        if is_short_term and "crypto" in category:
            return False
        return not any(pattern.match(slug) for pattern in cls.MARKET_BLACKLIST_PATTERNS)

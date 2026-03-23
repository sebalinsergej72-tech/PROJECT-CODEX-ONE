from __future__ import annotations

import asyncio
import hashlib
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from data.polymarket_client import OrderbookSnapshot, PolymarketClient, WalletOpenPosition, WalletTradeSignal
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
    market_snapshot: OrderbookSnapshot | None = None


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
        self._cold_wallet_cursor = 0

    async def scan_for_trade_intents(self, session: AsyncSession) -> list[TradeIntent]:
        fresh = await self.scan_for_fresh_trade_intents(session)
        reconcile = await self.scan_for_reconcile_intents(session)
        intents = [*fresh, *reconcile]
        intents.sort(key=lambda intent: intent.wallet_score, reverse=True)
        await self._attach_market_snapshots(intents)
        return intents[:60]

    async def scan_for_fresh_trade_intents(self, session: AsyncSession) -> list[TradeIntent]:
        risk_mode = self._risk_mode_provider()
        qualified_wallets = await self._load_enabled_wallets(session, risk_mode=risk_mode)
        if not qualified_wallets:
            logger.debug("No qualified wallets to monitor")
            return []

        recent_signal_scores = await self._fetch_recent_wallet_signal_scores(session, qualified_wallets)
        scan_wallets = self._select_wallets_for_fresh_scan(
            qualified_wallets,
            recent_signal_scores=recent_signal_scores,
        )
        existing_ids = await self._fetch_existing_trade_ids(session)
        cooldown_markets, cooldown_tokens = await self._fetch_market_cooldowns(session)
        recent_buy_locks = await self._fetch_recent_buy_locks(session)
        sellable_positions = await self._fetch_local_sellable_positions(session)
        intents: list[TradeIntent] = []
        trade_tasks = [
            self.polymarket_client.fetch_wallet_trades(
                wallet.address,
                limit=max(1, settings.trade_monitor_signal_fetch_limit),
            )
            for wallet in scan_wallets
        ]
        fetched_trade_batches = await asyncio.gather(*trade_tasks, return_exceptions=True)

        for wallet, trade_batch in zip(
            scan_wallets,
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
                recent_buy_locks=recent_buy_locks,
                sellable_positions=sellable_positions,
                price_filter_enabled=self._price_filter_provider(),
                short_term_enabled=self._short_term_provider(),
            )
            intents.extend(wallet_intents)

        intents.sort(key=lambda intent: intent.wallet_score, reverse=True)
        await self._attach_market_snapshots(intents)
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
        await self._attach_market_snapshots(intents)
        return intents[:60]

    async def _prime_market_data(self, intents: list[TradeIntent]) -> None:
        prime_market_data = getattr(self.polymarket_client, "prime_market_data", None)
        if not callable(prime_market_data):
            return
        token_ids = [intent.token_id for intent in intents if intent.token_id]
        if not token_ids:
            return
        try:
            await prime_market_data(token_ids)
        except Exception as exc:
            logger.debug("Market data prewarm failed: {}", exc)

    async def _attach_market_snapshots(self, intents: list[TradeIntent]) -> None:
        if not intents:
            return
        await self._prime_market_data(intents)
        get_cached_orderbook = getattr(self.polymarket_client, "get_cached_orderbook", None)
        if not callable(get_cached_orderbook):
            return
        for intent in intents:
            if not intent.token_id:
                continue
            try:
                intent.market_snapshot = get_cached_orderbook(intent.token_id)
            except Exception as exc:
                logger.debug("Failed to read cached market snapshot for {}: {}", intent.token_id, exc)

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
    async def _fetch_recent_wallet_signal_scores(
        session: AsyncSession,
        wallets: list[QualifiedWallet],
    ) -> dict[str, float]:
        if not wallets:
            return {}

        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=max(1, settings.trade_monitor_hot_signal_window_hours))
        wallet_addresses = [wallet.address.lower() for wallet in wallets]
        query = select(CopiedTrade).where(
            CopiedTrade.wallet_address.in_(wallet_addresses),
            CopiedTrade.copied_at >= cutoff,
        )
        rows = (await session.execute(query)).scalars().all()
        scores: dict[str, float] = defaultdict(float)
        for row in rows:
            address = row.wallet_address.lower()
            reason = (row.reason or "").strip().lower()
            status = (row.status or "").strip().lower()
            if status in {TradeStatus.SUBMITTED.value, TradeStatus.FILLED.value, TradeStatus.PARTIAL.value}:
                scores[address] += 4.0
            elif reason.startswith("price_moved:") or reason == "slippage_above_hard_limit":
                scores[address] += 2.0
            elif reason in {"no_orderbook", "sell_without_confirmed_position", "residual_too_small"}:
                scores[address] += 0.0
            elif reason.startswith("low_liquidity:"):
                scores[address] += 0.0
            else:
                scores[address] += 1.0
        return dict(scores)

    def _select_wallets_for_fresh_scan(
        self,
        wallets: list[QualifiedWallet],
        *,
        recent_signal_scores: dict[str, float],
    ) -> list[QualifiedWallet]:
        if len(wallets) <= 1:
            return wallets

        now = datetime.now(tz=timezone.utc)
        ranked_wallets = sorted(
            wallets,
            key=lambda wallet: self._wallet_polling_rank(
                wallet,
                recent_signal_score=recent_signal_scores.get(wallet.address.lower(), 0.0),
                now=now,
            ),
            reverse=True,
        )

        hot_target = max(1, settings.trade_monitor_hot_wallet_target)
        cold_batch_size = max(0, settings.trade_monitor_cold_wallet_batch_size)
        hot_wallets = ranked_wallets[: min(hot_target, len(ranked_wallets))]
        cold_wallets = ranked_wallets[len(hot_wallets) :]
        if not cold_wallets or cold_batch_size <= 0:
            return hot_wallets

        batch_size = min(cold_batch_size, len(cold_wallets))
        start = self._cold_wallet_cursor % len(cold_wallets)
        cold_batch = [cold_wallets[(start + idx) % len(cold_wallets)] for idx in range(batch_size)]
        self._cold_wallet_cursor = (start + batch_size) % len(cold_wallets)
        return [*hot_wallets, *cold_batch]

    @staticmethod
    def _wallet_polling_rank(
        wallet: QualifiedWallet,
        *,
        recent_signal_score: float,
        now: datetime,
    ) -> tuple[int, float, float, float]:
        hot_trade_fresh = 0
        last_trade_ts = TradeMonitor._normalize_wallet_timestamp(wallet.last_trade_ts)
        if last_trade_ts is not None:
            age_seconds = (now - last_trade_ts).total_seconds()
            if age_seconds <= max(1, settings.trade_monitor_hot_trade_freshness_hours) * 3600:
                hot_trade_fresh = 1
            recency_score = -age_seconds
        else:
            recency_score = float("-inf")

        return (
            hot_trade_fresh,
            float(recent_signal_score),
            recency_score,
            float(wallet.score),
        )

    @staticmethod
    def _normalize_wallet_timestamp(ts: datetime | None) -> datetime | None:
        if ts is None:
            return None
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts

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
    async def _fetch_recent_buy_locks(session: AsyncSession) -> set[str]:
        cooldown_seconds = max(0, settings.repeat_buy_cooldown_seconds)
        if cooldown_seconds <= 0:
            return set()

        cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=cooldown_seconds)
        query = select(CopiedTrade).where(
            CopiedTrade.side == TradeSide.BUY.value,
            CopiedTrade.copied_at >= cutoff,
        )
        rows = (await session.execute(query)).scalars().all()
        locks: set[str] = set()
        for trade in rows:
            if not TradeMonitor._should_lock_repeat_buy(trade):
                continue
            locks.add(
                TradeMonitor._buy_lock_key(
                    wallet_address=trade.wallet_address,
                    market_id=trade.market_id,
                    token_id=trade.token_id,
                    outcome=trade.outcome,
                )
            )
        return locks

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
        recent_buy_locks: set[str],
        sellable_positions: dict[str, Position],
        price_filter_enabled: bool,
        short_term_enabled: bool,
    ) -> list[TradeIntent]:
        intents: list[TradeIntent] = []
        normalized_signals = TradeMonitor._aggregate_signals(signals, existing_ids=existing_ids)

        for signal in normalized_signals:
            if signal.external_trade_id in existing_ids:
                continue

            if TradeMonitor._is_signal_on_price_moved_cooldown(
                signal,
                cooldown_markets=cooldown_markets,
                cooldown_tokens=cooldown_tokens,
            ):
                continue

            buy_lock_key = ""
            if signal.side.lower() == TradeSide.BUY.value:
                buy_lock_key = TradeMonitor._buy_lock_key(
                    wallet_address=signal.wallet_address,
                    market_id=signal.market_id,
                    token_id=signal.token_id,
                    outcome=signal.outcome,
                )
                if buy_lock_key in recent_buy_locks:
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
            if buy_lock_key:
                recent_buy_locks.add(buy_lock_key)

            if len(intents) >= TradeMonitor.MAX_INTENTS_PER_WALLET:
                break

        return intents

    @staticmethod
    def _aggregate_signals(
        signals: list[WalletTradeSignal],
        *,
        existing_ids: set[str],
    ) -> list[WalletTradeSignal]:
        if not settings.burst_aggregation_enabled or len(signals) <= 1:
            return signals

        window_seconds = max(0, settings.burst_aggregation_window_seconds)
        max_trades = max(1, settings.burst_aggregation_max_trades)
        if window_seconds <= 0:
            return signals

        aggregated: list[WalletTradeSignal] = []
        cluster: list[WalletTradeSignal] = []

        def flush() -> None:
            nonlocal cluster
            if not cluster:
                return
            aggregated.append(TradeMonitor._merge_signal_cluster(cluster))
            cluster = []

        for signal in signals:
            if signal.external_trade_id in existing_ids:
                flush()
                aggregated.append(signal)
                continue

            if not cluster:
                cluster = [signal]
                continue

            previous = cluster[-1]
            same_burst = (
                signal.wallet_address.lower() == previous.wallet_address.lower()
                and signal.market_id == previous.market_id
                and (signal.token_id or "") == (previous.token_id or "")
                and signal.outcome.lower() == previous.outcome.lower()
                and signal.side.lower() == previous.side.lower()
                and abs((cluster[0].traded_at - signal.traded_at).total_seconds()) <= window_seconds
                and len(cluster) < max_trades
            )
            if same_burst:
                cluster.append(signal)
                continue

            flush()
            cluster = [signal]

        flush()
        return aggregated

    @staticmethod
    def _merge_signal_cluster(cluster: list[WalletTradeSignal]) -> WalletTradeSignal:
        if len(cluster) == 1:
            return cluster[0]

        first = cluster[0]
        last = cluster[-1]
        total_size_usd = sum(max(signal.size_usd, 0.0) for signal in cluster)
        if total_size_usd > 0:
            weighted_price = sum(signal.price_cents * signal.size_usd for signal in cluster) / total_size_usd
        else:
            weighted_price = first.price_cents
        realized_profit = sum(signal.profit_usd or 0.0 for signal in cluster)
        external_trade_id = TradeMonitor._build_aggregated_external_trade_id(
            first=first,
            last=last,
            cluster=cluster,
        )
        return WalletTradeSignal(
            external_trade_id=external_trade_id,
            wallet_address=first.wallet_address,
            market_id=first.market_id,
            token_id=first.token_id,
            outcome=first.outcome,
            side=first.side,
            price_cents=weighted_price,
            size_usd=total_size_usd,
            traded_at=first.traded_at,
            profit_usd=realized_profit,
            market_slug=first.market_slug,
            market_category=first.market_category,
        )

    @staticmethod
    def _build_aggregated_external_trade_id(
        *,
        first: WalletTradeSignal,
        last: WalletTradeSignal,
        cluster: list[WalletTradeSignal],
    ) -> str:
        wallet_hint = first.wallet_address.lower()[:10]
        market_hint = first.market_id[:12]
        token_hint = (first.token_id or "none")[:12]
        cluster_signature = "|".join(signal.external_trade_id for signal in cluster)
        payload = (
            f"{first.wallet_address.lower()}|{first.market_id}|{first.token_id or ''}|"
            f"{first.outcome.lower()}|{first.side.lower()}|{len(cluster)}|"
            f"{first.traded_at.isoformat()}|{last.traded_at.isoformat()}|{cluster_signature}"
        )
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]
        return (
            f"agg:{wallet_hint}:{market_hint}:{token_hint}:"
            f"{first.side.lower()}:{len(cluster)}:{digest}"
        )

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
            if cls._is_reconcile_position_residual_too_small(local, price_cents=price_cents):
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
    def _should_lock_repeat_buy(trade: CopiedTrade) -> bool:
        if (trade.side or "").lower() != TradeSide.BUY.value:
            return False
        status = (trade.status or "").lower()
        if status in {
            TradeStatus.SUBMITTED.value,
            TradeStatus.PARTIAL.value,
            TradeStatus.FILLED.value,
        }:
            return True
        if trade.order_id:
            return True
        return float(trade.filled_size_usd or 0.0) > 0 or float(trade.filled_quantity or 0.0) > 0

    @staticmethod
    def _buy_lock_key(
        *,
        wallet_address: str,
        market_id: str,
        token_id: str | None,
        outcome: str,
    ) -> str:
        return (
            f"{wallet_address.strip().lower()}|"
            f"{market_id.strip().lower()}|"
            f"{(token_id or '').strip().lower()}|"
            f"{outcome.strip().lower()}"
        )

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

    @staticmethod
    def _is_reconcile_position_residual_too_small(position: Position, *, price_cents: float) -> bool:
        execution_price = max(price_cents / 100.0, settings.min_valid_price)
        available_size_usd = max(position.quantity, 0.0) * execution_price
        return available_size_usd < settings.min_trade_size_usd

    @classmethod
    def _is_market_tradeable(cls, *, signal: WalletTradeSignal, is_short_term: bool) -> bool:
        category = (signal.market_category or "").strip().lower()
        slug = (signal.market_slug or signal.market_id or "").strip().lower()
        if category in cls.CATEGORY_BLACKLIST:
            return False
        if is_short_term and "crypto" in category:
            return False
        return not any(pattern.match(slug) for pattern in cls.MARKET_BLACKLIST_PATTERNS)

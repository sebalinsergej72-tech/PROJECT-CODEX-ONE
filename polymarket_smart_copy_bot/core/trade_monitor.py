from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from data.polymarket_client import PolymarketClient, WalletOpenPosition, WalletTradeSignal
from models.models import CopiedTrade, Position, TradeSide
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


class TradeMonitor:
    """Monitors qualified wallets and extracts copy-trade candidates."""

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
        risk_mode = self._risk_mode_provider()
        limit = settings.max_wallets_aggressive if risk_mode == "aggressive" else settings.max_qualified_wallets
        query = (
            select(QualifiedWallet)
            .where(QualifiedWallet.enabled.is_(True))
            .order_by(QualifiedWallet.score.desc())
            .limit(limit)
        )
        qualified_wallets = list((await session.execute(query)).scalars().all())
        if not qualified_wallets:
            logger.debug("No qualified wallets to monitor")
            return []

        existing_ids = await self._fetch_existing_trade_ids(session)
        intents: list[TradeIntent] = []
        now = datetime.now(tz=timezone.utc)
        reconcile_cutoff = now - timedelta(minutes=2)

        trade_tasks = [self.polymarket_client.fetch_wallet_trades(wallet.address, limit=20) for wallet in qualified_wallets]
        position_tasks = [
            self.polymarket_client.fetch_wallet_open_positions(wallet.address, limit=200)
            for wallet in qualified_wallets
        ]
        fetched_trade_batches = await asyncio.gather(*trade_tasks, return_exceptions=True)
        fetched_position_batches = await asyncio.gather(*position_tasks, return_exceptions=True)
        local_open_by_wallet = await self._fetch_local_open_positions(
            session,
            [wallet.address for wallet in qualified_wallets],
        )

        for wallet, trade_batch, pos_batch in zip(
            qualified_wallets,
            fetched_trade_batches,
            fetched_position_batches,
            strict=True,
        ):
            if isinstance(trade_batch, Exception):
                logger.warning("Failed to fetch trades for {}: {}", wallet.address, trade_batch)
                continue

            wallet_intents = self._signals_to_intents(
                wallet,
                trade_batch,
                existing_ids,
                price_filter_enabled=self._price_filter_provider(),
                short_term_enabled=self._short_term_provider(),
            )
            intents.extend(wallet_intents)

            if isinstance(pos_batch, Exception):
                logger.warning("Failed to fetch source positions for {}: {}", wallet.address, pos_batch)
                continue

            if pos_batch is None:
                # API failure signaled by client method; skip reconciliation to avoid false close.
                continue

            reconcile_intents = self._build_reconcile_close_intents(
                wallet=wallet,
                source_open_positions=pos_batch,
                local_open_positions=local_open_by_wallet.get(wallet.address.lower(), []),
                existing_ids=existing_ids,
                reconcile_cutoff=reconcile_cutoff,
                now=now,
            )
            intents.extend(reconcile_intents)

        intents.sort(key=lambda intent: intent.wallet_score, reverse=True)
        return intents[:60]

    @staticmethod
    async def _fetch_existing_trade_ids(session: AsyncSession) -> set[str]:
        query = select(CopiedTrade.external_trade_id)
        rows = (await session.execute(query)).scalars().all()
        return set(rows)

    @staticmethod
    async def _fetch_local_open_positions(
        session: AsyncSession,
        wallet_addresses: list[str],
    ) -> dict[str, list[Position]]:
        if not wallet_addresses:
            return {}
        normalized = [w.lower() for w in wallet_addresses if w]
        if not normalized:
            return {}
        query = select(Position).where(
            Position.is_open.is_(True),
            Position.wallet_address.in_(normalized),
        )
        rows = (await session.execute(query)).scalars().all()
        grouped: dict[str, list[Position]] = {}
        for row in rows:
            grouped.setdefault(row.wallet_address.lower(), []).append(row)
        return grouped

    @staticmethod
    def _signals_to_intents(
        wallet: QualifiedWallet,
        signals: list[WalletTradeSignal],
        existing_ids: set[str],
        *,
        price_filter_enabled: bool,
        short_term_enabled: bool,
    ) -> list[TradeIntent]:
        intents: list[TradeIntent] = []

        for signal in signals:
            if signal.external_trade_id in existing_ids:
                continue

            is_short = TradeMonitor._is_short_term_signal(signal)
            if is_short and not short_term_enabled:
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
                )
            )

        return intents

    @classmethod
    def _build_reconcile_close_intents(
        cls,
        *,
        wallet: QualifiedWallet,
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
            if local.updated_at > reconcile_cutoff:
                continue

            local_key = cls._position_key(local.market_id, local.token_id, local.outcome)
            if local_key in source_keys:
                continue

            opposite_side = TradeSide.SELL.value if local.side == TradeSide.BUY.value else TradeSide.BUY.value
            price_cents = local.current_price_cents if local.current_price_cents > 0 else local.avg_price_cents
            if price_cents <= 0:
                continue

            external_trade_id = (
                f"reconcile_close:{wallet.address.lower()}:{local.id}:{int(now.timestamp() // 300)}"
            )
            if external_trade_id in existing_ids:
                continue

            intents.append(
                TradeIntent(
                    external_trade_id=external_trade_id,
                    wallet_address=local.wallet_address,
                    wallet_score=wallet.score + 1000.0,
                    wallet_win_rate=wallet.win_rate,
                    wallet_profit_factor=float(wallet.profit_factor or 1.0),
                    wallet_avg_position_size=float(wallet.avg_size or 0.0),
                    market_id=local.market_id,
                    token_id=local.token_id,
                    outcome=local.outcome,
                    side=opposite_side,
                    source_price_cents=price_cents,
                    # 0 triggers full-close sizing in TradeExecutor._derive_close_size_usd
                    source_size_usd=0.0,
                    is_short_term=False,
                )
            )
        return intents

    @staticmethod
    def _position_key(market_id: str, token_id: str | None, outcome: str) -> str:
        token = (token_id or "").strip().lower()
        return f"{market_id.strip().lower()}|{token}|{outcome.strip().lower()}"

    @staticmethod
    def _is_short_term_signal(signal: WalletTradeSignal) -> bool:
        text = f"{signal.market_id} {signal.outcome}".lower()
        return any(token in text for token in ("5 min", "5m", "15 min", "15m", "hourly", "1h"))

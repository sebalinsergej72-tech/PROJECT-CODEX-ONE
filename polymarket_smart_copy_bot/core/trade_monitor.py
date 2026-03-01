from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from data.polymarket_client import PolymarketClient, WalletTradeSignal
from models.models import CopiedTrade
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

        tasks = [self.polymarket_client.fetch_wallet_trades(wallet.address, limit=20) for wallet in qualified_wallets]
        fetched_batches = await asyncio.gather(*tasks, return_exceptions=True)

        for wallet, batch in zip(qualified_wallets, fetched_batches, strict=True):
            if isinstance(batch, Exception):
                logger.warning("Failed to fetch trades for {}: {}", wallet.address, batch)
                continue

            wallet_intents = self._signals_to_intents(
                wallet,
                batch,
                existing_ids,
                price_filter_enabled=self._price_filter_provider(),
                short_term_enabled=self._short_term_provider(),
            )
            intents.extend(wallet_intents)

        intents.sort(key=lambda intent: intent.wallet_score, reverse=True)
        return intents[:60]

    @staticmethod
    async def _fetch_existing_trade_ids(session: AsyncSession) -> set[str]:
        query = select(CopiedTrade.external_trade_id)
        rows = (await session.execute(query)).scalars().all()
        return set(rows)

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

    @staticmethod
    def _is_short_term_signal(signal: WalletTradeSignal) -> bool:
        text = f"{signal.market_id} {signal.outcome}".lower()
        return any(token in text for token in ("5 min", "5m", "15 min", "15m", "hourly", "1h"))

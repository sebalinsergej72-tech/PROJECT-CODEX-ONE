from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import settings
from core.background_tasks import BackgroundOrchestrator
from core.trade_monitor import TradeIntent, TradeMonitor
from data.polymarket_client import OrderbookLevel, OrderbookSnapshot, WalletOpenPosition, WalletTradeSignal
from models.models import Base, CopiedTrade, ExecutionDecisionAudit, Position, TradeSide, TradeStatus
from models.qualified_wallet import QualifiedWallet
from utils.helpers import utc_now


async def _run_with_session(callback) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with session_factory() as session:
            await callback(session)
            await session.commit()
    finally:
        await engine.dispose()


class _DummyPolymarketClient:
    def __init__(self) -> None:
        self.trade_limits: list[int] = []
        self.trade_calls = 0
        self.position_calls = 0

    async def fetch_wallet_trades(self, wallet_address: str, limit: int = 30) -> list[WalletTradeSignal]:
        self.trade_calls += 1
        self.trade_limits.append(limit)
        return [
            WalletTradeSignal(
                external_trade_id="trade-1",
                wallet_address=wallet_address,
                market_id="market-1",
                token_id="token-1",
                outcome="Yes",
                side="buy",
                price_cents=55.0,
                size_usd=12.0,
                traded_at=utc_now(),
                market_slug="safe-market",
                market_category="Politics",
            )
        ]

    async def fetch_wallet_open_positions(self, wallet_address: str, limit: int = 200) -> list[WalletOpenPosition]:
        self.position_calls += 1
        return []


class _DummySellPolymarketClient(_DummyPolymarketClient):
    async def fetch_wallet_trades(self, wallet_address: str, limit: int = 30) -> list[WalletTradeSignal]:
        self.trade_calls += 1
        self.trade_limits.append(limit)
        return [
            WalletTradeSignal(
                external_trade_id="sell-trade-1",
                wallet_address=wallet_address,
                market_id="market-sell-1",
                token_id="token-sell-1",
                outcome="Yes",
                side="sell",
                price_cents=55.0,
                size_usd=12.0,
                traded_at=utc_now(),
                market_slug="safe-market",
                market_category="Politics",
            )
        ]


class _BurstPolymarketClient(_DummyPolymarketClient):
    async def fetch_wallet_trades(self, wallet_address: str, limit: int = 30) -> list[WalletTradeSignal]:
        self.trade_calls += 1
        self.trade_limits.append(limit)
        now = utc_now()
        return [
            WalletTradeSignal(
                external_trade_id="burst-1",
                wallet_address=wallet_address,
                market_id="market-burst",
                token_id="token-burst",
                outcome="Yes",
                side="buy",
                price_cents=55.0,
                size_usd=10.0,
                traded_at=now,
                market_slug="burst-market",
                market_category="Politics",
            ),
            WalletTradeSignal(
                external_trade_id="burst-2",
                wallet_address=wallet_address,
                market_id="market-burst",
                token_id="token-burst",
                outcome="Yes",
                side="buy",
                price_cents=57.0,
                size_usd=20.0,
                traded_at=now - timedelta(seconds=5),
                market_slug="burst-market",
                market_category="Politics",
            ),
            WalletTradeSignal(
                external_trade_id="other-1",
                wallet_address=wallet_address,
                market_id="market-other",
                token_id="token-other",
                outcome="Yes",
                side="buy",
                price_cents=60.0,
                size_usd=15.0,
                traded_at=now - timedelta(seconds=40),
                market_slug="other-market",
                market_category="Politics",
            ),
        ]


class _HotLanePolymarketClient(_DummyPolymarketClient):
    def __init__(self) -> None:
        super().__init__()
        self.fetched_wallets: list[str] = []

    async def fetch_wallet_trades(self, wallet_address: str, limit: int = 30) -> list[WalletTradeSignal]:
        self.trade_calls += 1
        self.trade_limits.append(limit)
        self.fetched_wallets.append(wallet_address.lower())
        return []


class _WarmSnapshotPolymarketClient(_DummyPolymarketClient):
    def __init__(self) -> None:
        super().__init__()
        self.primed_token_ids: list[str] = []
        self.registered_hot_token_ids: list[str] = []
        self.snapshot = OrderbookSnapshot(
            bids=[OrderbookLevel(price=0.54, size=100.0)],
            asks=[OrderbookLevel(price=0.55, size=100.0)],
            best_bid=0.54,
            best_ask=0.55,
        )

    async def register_hot_markets(self, token_ids: list[str | None], *, source: str, priority: int = 0) -> int:
        self.registered_hot_token_ids.extend(str(token_id) for token_id in token_ids if token_id)
        return len(self.registered_hot_token_ids)

    async def prime_market_data(self, token_ids: list[str | None]) -> None:
        self.primed_token_ids.extend(str(token_id) for token_id in token_ids if token_id)

    def get_cached_orderbook(self, token_id: str | None) -> OrderbookSnapshot | None:
        if token_id == "token-1":
            return self.snapshot
        return None


class _UnknownCategoryPolymarketClient(_DummyPolymarketClient):
    async def fetch_wallet_trades(self, wallet_address: str, limit: int = 30) -> list[WalletTradeSignal]:
        self.trade_calls += 1
        self.trade_limits.append(limit)
        return [
            WalletTradeSignal(
                external_trade_id="unknown-category-trade",
                wallet_address=wallet_address,
                market_id="market-unknown",
                token_id="token-unknown",
                outcome="Yes",
                side="buy",
                price_cents=55.0,
                size_usd=12.0,
                traded_at=utc_now(),
                market_slug="safe-market",
                market_category=None,
            )
        ]


class _InferredSportsCategoryPolymarketClient(_DummyPolymarketClient):
    async def fetch_wallet_trades(self, wallet_address: str, limit: int = 30) -> list[WalletTradeSignal]:
        self.trade_calls += 1
        self.trade_limits.append(limit)
        return [
            WalletTradeSignal(
                external_trade_id="inferred-sports-trade",
                wallet_address=wallet_address,
                market_id="market-sports",
                token_id="token-sports",
                outcome="Over",
                side="buy",
                price_cents=50.0,
                size_usd=12.0,
                traded_at=utc_now(),
                market_slug="nba-lal-hou-2026-04-26-total-205pt5",
                market_category=None,
            )
        ]


class _SidecarHotScanPolymarketClient(_DummyPolymarketClient):
    def __init__(self) -> None:
        super().__init__()
        self.sidecar_scan_calls = 0

    async def scan_hot_wallet_trades_via_sidecar(
        self,
        wallet_addresses: list[str],
        *,
        signal_limit: int,
    ) -> dict[str, list[WalletTradeSignal]] | None:
        self.sidecar_scan_calls += 1
        assert signal_limit == settings.trade_monitor_signal_fetch_limit
        return {
            wallet_addresses[0].lower(): [
                WalletTradeSignal(
                    external_trade_id="sidecar-trade-1",
                    wallet_address=wallet_addresses[0],
                    market_id="market-sidecar",
                    token_id="token-sidecar",
                    outcome="Yes",
                    side="buy",
                    price_cents=61.0,
                    size_usd=14.0,
                    traded_at=utc_now(),
                    market_slug="sidecar-market",
                    market_category="Politics",
                )
            ]
        }

    async def fetch_wallet_trades(self, wallet_address: str, limit: int = 30) -> list[WalletTradeSignal]:
        raise AssertionError("Per-wallet Python polling should not run when sidecar hot scan succeeds")


class _ShadowHotScanPolymarketClient(_DummyPolymarketClient):
    def __init__(self) -> None:
        super().__init__()
        self.sidecar_scan_calls = 0

    async def scan_hot_wallet_trades_via_sidecar(
        self,
        wallet_addresses: list[str],
        *,
        signal_limit: int,
    ) -> dict[str, list[WalletTradeSignal]] | None:
        self.sidecar_scan_calls += 1
        wallet_address = wallet_addresses[0]
        signal = WalletTradeSignal(
            external_trade_id="shadow-trade-1",
            wallet_address=wallet_address,
            market_id="market-shadow",
            token_id="token-shadow",
            outcome="Yes",
            side="buy",
            price_cents=62.0,
            size_usd=12.0,
            traded_at=utc_now(),
            market_slug="shadow-market",
            market_category="Politics",
        )
        return {wallet_address.lower(): [signal]}

    async def fetch_wallet_trades(self, wallet_address: str, limit: int = 30) -> list[WalletTradeSignal]:
        return [
            WalletTradeSignal(
                external_trade_id="shadow-trade-1",
                wallet_address=wallet_address,
                market_id="market-shadow",
                token_id="token-shadow",
                outcome="Yes",
                side="buy",
                price_cents=62.0,
                size_usd=12.0,
                traded_at=utc_now(),
                market_slug="shadow-market",
                market_category="Politics",
            )
        ]


def test_fast_trade_scan_uses_hot_limit_and_skips_reconcile_fetch() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _DummyPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        session.add(
            QualifiedWallet(
                address="0x1111111111111111111111111111111111111111",
                name="fast",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,politics",
                enabled=True,
            )
        )
        await session.flush()

        intents = await monitor.scan_for_fresh_trade_intents(session)

        assert len(intents) == 1
        assert client.trade_calls == 1
        assert client.trade_limits == [settings.trade_monitor_signal_fetch_limit]
        assert client.position_calls == 0

    asyncio.run(_run_with_session(_case))


def test_fast_trade_scan_skips_recent_repeat_buy_on_same_market() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _DummyPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        wallet_address = "0x1111111111111111111111111111111111111111"
        session.add(
            QualifiedWallet(
                address=wallet_address,
                name="repeat-buy-wallet",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,politics",
                enabled=True,
            )
        )
        session.add(
            CopiedTrade(
                external_trade_id="recent-submitted-buy",
                wallet_address=wallet_address,
                market_id="market-1",
                token_id="token-1",
                outcome="Yes",
                side="buy",
                price_cents=55.0,
                size_usd=12.0,
                status=TradeStatus.SUBMITTED.value,
                copied_at=utc_now(),
                submitted_at=utc_now(),
                order_id="order-1",
            )
        )
        await session.flush()

        intents = await monitor.scan_for_fresh_trade_intents(session)

        assert intents == []

    asyncio.run(_run_with_session(_case))


def test_signals_to_intents_only_keeps_first_repeat_buy_in_same_scan() -> None:
    wallet = QualifiedWallet(
        address="0x1111111111111111111111111111111111111111",
        name="repeat-scan-wallet",
        score=100.0,
        win_rate=0.7,
        trades_90d=150,
        profit_factor=2.0,
        avg_size=1000.0,
        niche="overall,politics",
        enabled=True,
    )
    now = utc_now()
    signals = [
        WalletTradeSignal(
            external_trade_id="repeat-buy-1",
            wallet_address=wallet.address,
            market_id="market-repeat",
            token_id="token-repeat",
            outcome="Yes",
            side="buy",
            price_cents=55.0,
            size_usd=10.0,
            traded_at=now,
            market_slug="repeat-market",
            market_category="Politics",
        ),
        WalletTradeSignal(
            external_trade_id="repeat-buy-2",
            wallet_address=wallet.address,
            market_id="market-repeat",
            token_id="token-repeat",
            outcome="Yes",
            side="buy",
            price_cents=56.0,
            size_usd=11.0,
            traded_at=now - timedelta(seconds=45),
            market_slug="repeat-market",
            market_category="Politics",
        ),
    ]

    intents = TradeMonitor._signals_to_intents(
        wallet,
        signals,
        set(),
        cooldown_markets=set(),
        cooldown_tokens=set(),
        recent_buy_locks=set(),
        wallet_failure_cooldowns=set(),
        wallet_uncopyable_pauses=set(),
        wallet_recent_buy_counts={},
        sellable_positions={},
        price_filter_enabled=False,
        short_term_enabled=True,
    )

    assert [intent.external_trade_id for intent in intents] == ["repeat-buy-1"]


def test_fast_trade_scan_prioritizes_hot_wallets_each_cycle() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _HotLanePolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        hot_1 = "0x1111111111111111111111111111111111111111"
        hot_2 = "0x2222222222222222222222222222222222222222"
        cold_1 = "0x3333333333333333333333333333333333333333"
        cold_2 = "0x4444444444444444444444444444444444444444"
        now = utc_now()
        session.add_all(
            [
                QualifiedWallet(
                    address=hot_1,
                    name="hot-1",
                    score=120.0,
                    win_rate=0.7,
                    trades_90d=150,
                    profit_factor=2.0,
                    avg_size=1000.0,
                    niche="overall,politics",
                    enabled=True,
                    last_trade_ts=now - timedelta(hours=1),
                ),
                QualifiedWallet(
                    address=hot_2,
                    name="hot-2",
                    score=110.0,
                    win_rate=0.69,
                    trades_90d=140,
                    profit_factor=1.9,
                    avg_size=900.0,
                    niche="overall,politics",
                    enabled=True,
                    last_trade_ts=now - timedelta(hours=2),
                ),
                QualifiedWallet(
                    address=cold_1,
                    name="cold-1",
                    score=105.0,
                    win_rate=0.68,
                    trades_90d=130,
                    profit_factor=1.8,
                    avg_size=850.0,
                    niche="overall,politics",
                    enabled=True,
                    last_trade_ts=now - timedelta(days=3),
                ),
                QualifiedWallet(
                    address=cold_2,
                    name="cold-2",
                    score=95.0,
                    win_rate=0.67,
                    trades_90d=125,
                    profit_factor=1.75,
                    avg_size=800.0,
                    niche="overall,politics",
                    enabled=True,
                    last_trade_ts=now - timedelta(days=4),
                ),
            ]
        )
        await session.flush()

        original_hot_target = settings.trade_monitor_hot_wallet_target
        original_cold_batch_size = settings.trade_monitor_cold_wallet_batch_size
        original_hot_freshness = settings.trade_monitor_hot_trade_freshness_hours
        try:
            settings.trade_monitor_hot_wallet_target = 2
            settings.trade_monitor_cold_wallet_batch_size = 1
            settings.trade_monitor_hot_trade_freshness_hours = 24

            await monitor.scan_for_fresh_trade_intents(session)
            first_cycle = list(client.fetched_wallets)
            client.fetched_wallets.clear()

            await monitor.scan_for_fresh_trade_intents(session)
            second_cycle = list(client.fetched_wallets)

            assert first_cycle[:2] == [hot_1.lower(), hot_2.lower()]
            assert second_cycle[:2] == [hot_1.lower(), hot_2.lower()]
            assert first_cycle[2] == cold_1.lower()
            assert second_cycle[2] == cold_2.lower()
        finally:
            settings.trade_monitor_hot_wallet_target = original_hot_target
            settings.trade_monitor_cold_wallet_batch_size = original_cold_batch_size
            settings.trade_monitor_hot_trade_freshness_hours = original_hot_freshness

    asyncio.run(_run_with_session(_case))


def test_fast_trade_scan_skips_wallet_on_copyability_failure_cooldown() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _DummyPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        wallet_address = "0x1111111111111111111111111111111111111111"
        session.add(
            QualifiedWallet(
                address=wallet_address,
                name="cooldown-wallet",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,politics",
                enabled=True,
            )
        )
        now = utc_now()
        for idx in range(7):
            session.add(
                CopiedTrade(
                    external_trade_id=f"fail-{idx}",
                    wallet_address=wallet_address,
                    market_id=f"market-{idx}",
                    token_id=f"token-{idx}",
                    outcome="Yes",
                    side="buy",
                    price_cents=55.0,
                    size_usd=12.0,
                    status=TradeStatus.SKIPPED.value,
                    reason="price_moved:50.0%",
                    copied_at=now - timedelta(minutes=idx),
                )
            )
        for idx in range(3):
            session.add(
                CopiedTrade(
                    external_trade_id=f"ok-{idx}",
                    wallet_address=wallet_address,
                    market_id=f"market-ok-{idx}",
                    token_id=f"token-ok-{idx}",
                    outcome="Yes",
                    side="buy",
                    price_cents=55.0,
                    size_usd=12.0,
                    status=TradeStatus.FILLED.value,
                    copied_at=now - timedelta(minutes=10 + idx),
                    filled_at=now - timedelta(minutes=10 + idx),
                )
            )
        await session.flush()

        intents = await monitor.scan_for_fresh_trade_intents(session)

        assert intents == []

    asyncio.run(_run_with_session(_case))


def test_fast_trade_scan_caps_wallet_buy_burst_in_window() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _DummyPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        wallet_address = "0x1111111111111111111111111111111111111111"
        session.add(
            QualifiedWallet(
                address=wallet_address,
                name="burst-cap-wallet",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,politics",
                enabled=True,
            )
        )
        now = utc_now()
        for idx in range(3):
            session.add(
                CopiedTrade(
                    external_trade_id=f"recent-buy-{idx}",
                    wallet_address=wallet_address,
                    market_id=f"recent-market-{idx}",
                    token_id=f"recent-token-{idx}",
                    outcome="Yes",
                    side="buy",
                    price_cents=55.0,
                    size_usd=12.0,
                    status=TradeStatus.SKIPPED.value,
                    reason="low_liquidity:0.00<10.00",
                    copied_at=now - timedelta(minutes=5 + idx),
                )
            )
        await session.flush()

        intents = await monitor.scan_for_fresh_trade_intents(session)

        assert intents == []

    asyncio.run(_run_with_session(_case))


def test_fast_trade_scan_pauses_uncopyable_wallet_with_zero_recent_fills() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _DummyPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        wallet_address = "0x1111111111111111111111111111111111111111"
        session.add(
            QualifiedWallet(
                address=wallet_address,
                name="uncopyable-wallet",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,politics",
                enabled=True,
            )
        )
        now = utc_now()
        original_min = settings.wallet_uncopyable_min_buy_intents
        try:
            settings.wallet_uncopyable_min_buy_intents = 4
            for idx in range(4):
                session.add(
                    CopiedTrade(
                        external_trade_id=f"uncopyable-{idx}",
                        wallet_address=wallet_address,
                        market_id=f"market-{idx}",
                        token_id=f"token-{idx}",
                        outcome="Yes",
                        side="buy",
                        price_cents=55.0,
                        size_usd=12.0,
                        status=TradeStatus.SKIPPED.value,
                        reason="price_moved:50.0%",
                        copied_at=now - timedelta(hours=idx),
                    )
                )
            await session.flush()

            intents = await monitor.scan_for_fresh_trade_intents(session)

            assert intents == []
        finally:
            settings.wallet_uncopyable_min_buy_intents = original_min

    asyncio.run(_run_with_session(_case))


def test_fast_trade_scan_blocks_unknown_category_buys() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _UnknownCategoryPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        session.add(
            QualifiedWallet(
                address="0x1111111111111111111111111111111111111111",
                name="unknown-category",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,politics",
                enabled=True,
            )
        )
        await session.flush()

        intents = await monitor.scan_for_fresh_trade_intents(session)

        assert intents == []

    asyncio.run(_run_with_session(_case))


def test_fast_trade_scan_allows_inferred_sports_category_buys() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _InferredSportsCategoryPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        session.add(
            QualifiedWallet(
                address="0x1111111111111111111111111111111111111111",
                name="inferred-sports",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,sports",
                enabled=True,
            )
        )
        await session.flush()

        intents = await monitor.scan_for_fresh_trade_intents(session)

        assert len(intents) == 1
        assert intents[0].external_trade_id == "inferred-sports-trade"
        assert intents[0].market_category == "sports"

    asyncio.run(_run_with_session(_case))


def test_fast_trade_scan_attaches_warm_market_snapshot_to_intent() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _WarmSnapshotPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        session.add(
            QualifiedWallet(
                address="0x1111111111111111111111111111111111111111",
                name="warm",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,politics",
                enabled=True,
            )
        )
        await session.flush()

        intents = await monitor.scan_for_fresh_trade_intents(session)

        assert len(intents) == 1
        assert client.registered_hot_token_ids == ["token-1"]
        assert client.primed_token_ids == ["token-1"]
        assert intents[0].market_snapshot is client.snapshot

    asyncio.run(_run_with_session(_case))


def test_fast_trade_scan_uses_sidecar_hot_wallet_batch_before_python_polling() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _SidecarHotScanPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        session.add(
            QualifiedWallet(
                address="0x1111111111111111111111111111111111111111",
                name="sidecar-hot",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,politics",
                enabled=True,
            )
        )
        await session.flush()

        intents = await monitor.scan_for_fresh_trade_intents(session)

        assert client.sidecar_scan_calls == 1
        assert len(intents) == 1
        assert intents[0].external_trade_id == "sidecar-trade-1"
        assert intents[0].market_id == "market-sidecar"

    asyncio.run(_run_with_session(_case))


def test_fast_trade_scan_writes_shadow_audit_rows() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _ShadowHotScanPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        session.add(
            QualifiedWallet(
                address="0x1111111111111111111111111111111111111111",
                name="shadow-hot",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,politics",
                enabled=True,
            )
        )
        await session.flush()

        original_shadow = settings.execution_sidecar_shadow_mode_enabled
        try:
            settings.execution_sidecar_shadow_mode_enabled = True
            intents = await monitor.scan_for_fresh_trade_intents(session)
        finally:
            settings.execution_sidecar_shadow_mode_enabled = original_shadow

        assert len(intents) == 1
        rows = (await session.execute(select(ExecutionDecisionAudit))).scalars().all()
        assert len(rows) == 1
        assert rows[0].stage == "hot_wallet_scan"
        assert rows[0].wallet_address == "0x1111111111111111111111111111111111111111"
        assert rows[0].matched is True
        assert rows[0].rust_signal_count == 1
        assert rows[0].python_signal_count == 1

    asyncio.run(_run_with_session(_case))


def test_fast_trade_scan_aggregates_burst_trades_on_same_market() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _BurstPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        session.add(
            QualifiedWallet(
                address="0x1111111111111111111111111111111111111111",
                name="burst",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,politics",
                enabled=True,
            )
        )
        await session.flush()

        intents = await monitor.scan_for_fresh_trade_intents(session)

        assert len(intents) == 2
        burst_intent = next(intent for intent in intents if intent.market_id == "market-burst")
        assert burst_intent.external_trade_id.startswith("agg:")
        assert len(burst_intent.external_trade_id) <= 256
        assert burst_intent.source_size_usd == 30.0
        assert round(burst_intent.source_price_cents, 4) == round((55.0 * 10.0 + 57.0 * 20.0) / 30.0, 4)

    asyncio.run(_run_with_session(_case))


def test_fast_trade_scan_skips_signal_on_recent_price_moved_cooldown() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _DummyPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        wallet_address = "0x1111111111111111111111111111111111111111"
        session.add(
            QualifiedWallet(
                address=wallet_address,
                name="fast",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,politics",
                enabled=True,
            )
        )
        session.add(
            CopiedTrade(
                external_trade_id="old-price-moved",
                wallet_address=wallet_address,
                market_id="market-1",
                token_id="token-1",
                outcome="Yes",
                side=TradeSide.BUY.value,
                price_cents=55.0,
                size_usd=12.0,
                status=TradeStatus.SKIPPED.value,
                reason="price_moved:12.0%",
                copied_at=utc_now(),
            )
        )
        await session.flush()

        intents = await monitor.scan_for_fresh_trade_intents(session)

        assert intents == []

    asyncio.run(_run_with_session(_case))


def test_fast_trade_scan_allows_signal_after_price_moved_cooldown_expires() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _DummyPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        wallet_address = "0x1111111111111111111111111111111111111111"
        session.add(
            QualifiedWallet(
                address=wallet_address,
                name="fast",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,politics",
                enabled=True,
            )
        )
        session.add(
            CopiedTrade(
                external_trade_id="expired-price-moved",
                wallet_address=wallet_address,
                market_id="market-1",
                token_id="token-1",
                outcome="Yes",
                side=TradeSide.BUY.value,
                price_cents=55.0,
                size_usd=12.0,
                status=TradeStatus.SKIPPED.value,
                reason="price_moved:12.0%",
                copied_at=utc_now() - timedelta(minutes=settings.price_moved_market_cooldown_minutes + 1),
            )
        )
        await session.flush()

        intents = await monitor.scan_for_fresh_trade_intents(session)

        assert len(intents) == 1
        assert intents[0].market_id == "market-1"

    asyncio.run(_run_with_session(_case))


def test_fast_trade_scan_skips_signal_on_recent_no_orderbook_cooldown() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _DummyPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        wallet_address = "0x1111111111111111111111111111111111111111"
        session.add(
            QualifiedWallet(
                address=wallet_address,
                name="fast",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,politics",
                enabled=True,
            )
        )
        session.add(
            CopiedTrade(
                external_trade_id="old-no-orderbook",
                wallet_address=wallet_address,
                market_id="market-1",
                token_id="token-1",
                outcome="Yes",
                side=TradeSide.BUY.value,
                price_cents=55.0,
                size_usd=12.0,
                status=TradeStatus.SKIPPED.value,
                reason="no_orderbook",
                copied_at=utc_now(),
            )
        )
        await session.flush()

        intents = await monitor.scan_for_fresh_trade_intents(session)

        assert intents == []

    asyncio.run(_run_with_session(_case))


def test_fast_trade_scan_skips_signal_on_recent_low_liquidity_cooldown() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _DummyPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        wallet_address = "0x1111111111111111111111111111111111111111"
        session.add(
            QualifiedWallet(
                address=wallet_address,
                name="fast",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,politics",
                enabled=True,
            )
        )
        session.add(
            CopiedTrade(
                external_trade_id="old-low-liquidity",
                wallet_address=wallet_address,
                market_id="market-1",
                token_id="token-1",
                outcome="Yes",
                side=TradeSide.BUY.value,
                price_cents=55.0,
                size_usd=12.0,
                status=TradeStatus.SKIPPED.value,
                reason="low_liquidity:1.20<15.00",
                copied_at=utc_now(),
            )
        )
        await session.flush()

        intents = await monitor.scan_for_fresh_trade_intents(session)

        assert intents == []

    asyncio.run(_run_with_session(_case))


def test_fast_trade_scan_skips_sell_signal_without_local_position() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _DummySellPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        session.add(
            QualifiedWallet(
                address="0x1111111111111111111111111111111111111111",
                name="sell",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,politics",
                enabled=True,
            )
        )
        await session.flush()

        intents = await monitor.scan_for_fresh_trade_intents(session)

        assert intents == []

    asyncio.run(_run_with_session(_case))


def test_fast_trade_scan_skips_sell_signal_with_residual_too_small_position() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _DummySellPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        session.add(
            QualifiedWallet(
                address="0x1111111111111111111111111111111111111111",
                name="sell",
                score=100.0,
                win_rate=0.7,
                trades_90d=150,
                profit_factor=2.0,
                avg_size=1000.0,
                niche="overall,politics",
                enabled=True,
            )
        )
        session.add(
            Position(
                wallet_address="account_sync",
                market_id="market-sell-1",
                token_id="token-sell-1",
                outcome="Yes",
                side=TradeSide.BUY.value,
                quantity=0.01,
                avg_price_cents=55.0,
                current_price_cents=55.0,
                invested_usd=0.01,
                is_open=True,
                opened_at=utc_now() - timedelta(minutes=10),
            )
        )
        await session.flush()

        intents = await monitor.scan_for_fresh_trade_intents(session)

        assert intents == []

    asyncio.run(_run_with_session(_case))


def test_reconcile_scan_fetches_positions_without_trade_fetch() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _DummyPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        wallet_address = "0x2222222222222222222222222222222222222222"
        session.add(
            QualifiedWallet(
                address=wallet_address,
                name="reconcile",
                score=150.0,
                win_rate=0.75,
                trades_90d=180,
                profit_factor=2.2,
                avg_size=1500.0,
                niche="overall,sports",
                enabled=True,
            )
        )
        session.add(
            Position(
                wallet_address=wallet_address,
                market_id="market-2",
                token_id="token-2",
                outcome="Yes",
                side=TradeSide.BUY.value,
                quantity=10.0,
                avg_price_cents=50.0,
                current_price_cents=52.0,
                invested_usd=5.0,
                is_open=True,
                opened_at=utc_now() - timedelta(minutes=10),
            )
        )
        await session.flush()

        intents = await monitor.scan_for_reconcile_intents(session)

        assert len(intents) == 1
        assert intents[0].side == TradeSide.SELL.value
        assert client.trade_calls == 0
        assert client.position_calls == 1

    asyncio.run(_run_with_session(_case))


def test_reconcile_scan_skips_dust_residual_positions() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _DummyPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        wallet_address = "0x2222222222222222222222222222222222222222"
        session.add(
            QualifiedWallet(
                address=wallet_address,
                name="reconcile-dust",
                score=150.0,
                win_rate=0.75,
                trades_90d=180,
                profit_factor=2.2,
                avg_size=1500.0,
                niche="overall,sports",
                enabled=True,
            )
        )
        session.add(
            Position(
                wallet_address=wallet_address,
                market_id="market-dust",
                token_id="token-dust",
                outcome="Yes",
                side=TradeSide.BUY.value,
                quantity=0.01,
                avg_price_cents=50.0,
                current_price_cents=52.0,
                invested_usd=0.01,
                is_open=True,
                opened_at=utc_now() - timedelta(minutes=10),
            )
        )
        await session.flush()

        intents = await monitor.scan_for_reconcile_intents(session)

        assert intents == []
        assert client.trade_calls == 0
        assert client.position_calls == 1

    asyncio.run(_run_with_session(_case))


def test_reconcile_scan_creates_take_profit_exit_intent() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _DummyPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        wallet_address = TradeMonitor.ACCOUNT_SYNC_WALLET
        session.add(
            Position(
                wallet_address=wallet_address,
                market_id="market-profit",
                token_id="token-profit",
                outcome="Yes",
                side=TradeSide.BUY.value,
                quantity=10.0,
                avg_price_cents=50.0,
                current_price_cents=62.0,
                invested_usd=5.0,
                is_open=True,
                opened_at=utc_now() - timedelta(minutes=30),
            )
        )
        await session.flush()

        intents = await monitor.scan_for_reconcile_intents(session)

        assert len(intents) == 1
        assert intents[0].external_trade_id.startswith("take_profit:")
        assert intents[0].side == TradeSide.SELL.value
        assert intents[0].source_price_cents == 62.0
        assert client.position_calls == 0

    asyncio.run(_run_with_session(_case))


def test_reconcile_scan_creates_stop_loss_exit_intent() -> None:
    async def _case(session: AsyncSession) -> None:
        client = _DummyPolymarketClient()
        monitor = TradeMonitor(
            client,
            risk_mode_provider=lambda: "aggressive",
            price_filter_provider=lambda: False,
            short_term_provider=lambda: True,
        )
        wallet_address = TradeMonitor.ACCOUNT_SYNC_WALLET
        session.add(
            Position(
                wallet_address=wallet_address,
                market_id="market-loss",
                token_id="token-loss",
                outcome="Yes",
                side=TradeSide.BUY.value,
                quantity=10.0,
                avg_price_cents=50.0,
                current_price_cents=42.0,
                invested_usd=5.0,
                is_open=True,
                opened_at=utc_now() - timedelta(minutes=30),
            )
        )
        await session.flush()

        intents = await monitor.scan_for_reconcile_intents(session)

        assert len(intents) == 1
        assert intents[0].external_trade_id.startswith("stop_loss:")
        assert intents[0].side == TradeSide.SELL.value
        assert intents[0].source_price_cents == 42.0
        assert client.position_calls == 0

    asyncio.run(_run_with_session(_case))


def test_trade_monitor_scan_does_not_sync_account_positions() -> None:
    class _DummyRiskManager:
        def should_trigger_drawdown_stop(self, portfolio, risk_mode) -> bool:
            return False

    class _DummyPortfolioTracker:
        async def calculate_state(self, session, risk_mode):
            return object()

    class _DummyTradeMonitor:
        async def scan_for_fresh_trade_intents(self, session):
            return []

    async def _unexpected_sync(session, *, force):
        raise AssertionError("fast trade monitor must not sync account positions")

    async def _case() -> None:
        orchestrator = BackgroundOrchestrator.__new__(BackgroundOrchestrator)
        orchestrator._trading_enabled = True
        orchestrator._risk_mode = "aggressive"
        orchestrator.last_trade_scan_at = None
        orchestrator._last_portfolio_state = None
        orchestrator.risk_manager = _DummyRiskManager()
        orchestrator.portfolio_tracker = _DummyPortfolioTracker()
        orchestrator.trade_monitor = _DummyTradeMonitor()
        orchestrator._maybe_sync_account_positions = _unexpected_sync

        await BackgroundOrchestrator._trade_monitor_scan(orchestrator, None)

        assert orchestrator.last_trade_scan_at is not None

    asyncio.run(_case())


def test_trade_reconcile_scan_uses_ttl_gated_account_sync() -> None:
    class _DummyRiskManager:
        pass

    class _DummyPortfolioTracker:
        async def calculate_state(self, session, risk_mode):
            return object()

    class _DummyTradeMonitor:
        async def scan_for_reconcile_intents(self, session):
            return []

    async def _case() -> None:
        calls: list[bool] = []

        async def _sync(session, *, force):
            calls.append(force)

        orchestrator = BackgroundOrchestrator.__new__(BackgroundOrchestrator)
        orchestrator._trading_enabled = True
        orchestrator._risk_mode = "aggressive"
        orchestrator.last_trade_reconcile_at = None
        orchestrator.risk_manager = _DummyRiskManager()
        orchestrator.portfolio_tracker = _DummyPortfolioTracker()
        orchestrator.trade_monitor = _DummyTradeMonitor()
        orchestrator._maybe_sync_account_positions = _sync

        await BackgroundOrchestrator._trade_reconcile_scan(orchestrator, None)

        assert calls == [False]
        assert orchestrator.last_trade_reconcile_at is not None

    asyncio.run(_case())


def test_portfolio_refresh_uses_ttl_gated_account_sync() -> None:
    class _DummyTradeExecutor:
        async def reconcile_open_trade_states(self, session) -> None:
            return None

    class _DummyPolymarketClient:
        async def fetch_account_balance_usd(self):
            return 10.0

    class _DummyPortfolioTracker:
        async def update_capital_base(self, session, balance):
            return None

        async def mark_to_market(self, session):
            return None

        async def record_snapshot(self, session, risk_mode):
            class _Snapshot:
                total_equity_usd = 0.0

            return _Snapshot()

    class _DummyNotifications:
        async def send_message(self, text: str) -> None:
            return None

    async def _case() -> None:
        calls: list[bool] = []

        async def _sync(session, *, force):
            calls.append(force)

        orchestrator = BackgroundOrchestrator.__new__(BackgroundOrchestrator)
        orchestrator._dry_run = False
        orchestrator._risk_mode = "aggressive"
        orchestrator._last_portfolio_state = None
        orchestrator.last_portfolio_refresh_at = None
        orchestrator._last_account_sync_at = None
        orchestrator.trade_executor = _DummyTradeExecutor()
        orchestrator.polymarket_client = _DummyPolymarketClient()
        orchestrator.portfolio_tracker = _DummyPortfolioTracker()
        orchestrator.notifications = _DummyNotifications()
        orchestrator._maybe_sync_account_positions = _sync
        orchestrator._refresh_exchange_balances = lambda: asyncio.sleep(0)  # type: ignore[method-assign]

        await BackgroundOrchestrator._portfolio_refresh(orchestrator, None)

        assert calls == []
        assert orchestrator.last_portfolio_refresh_at is not None

    asyncio.run(_case())


def test_execute_trade_intents_prioritizes_fresh_buys_and_recalculates_only_after_executed_status() -> None:
    class _DummyTradeExecutor:
        def __init__(self) -> None:
            self.execution_order: list[str] = []
            self.status_by_trade_id = {
                "buy-fast-1": TradeStatus.SKIPPED.value,
                "buy-fast-2": TradeStatus.SUBMITTED.value,
                "reconcile_close:sell-1": TradeStatus.SKIPPED.value,
            }

        async def execute_intent(
            self,
            session,
            intent,
            portfolio,
            *,
            risk_mode,
            fill_mode,
            price_filter_enabled,
            high_conviction_boost_enabled,
        ):
            self.execution_order.append(intent.external_trade_id)
            return self.status_by_trade_id[intent.external_trade_id]

    class _DummyPortfolioTracker:
        def __init__(self) -> None:
            self.calculate_calls = 0

        async def calculate_state(self, session, risk_mode):
            self.calculate_calls += 1
            return object()

    async def _case() -> None:
        orchestrator = BackgroundOrchestrator.__new__(BackgroundOrchestrator)
        orchestrator._risk_mode = "aggressive"
        orchestrator._price_filter_enabled = True
        orchestrator._high_conviction_boost_enabled = True
        orchestrator.trade_executor = _DummyTradeExecutor()
        orchestrator.portfolio_tracker = _DummyPortfolioTracker()

        intents = [
            TradeIntent(
                external_trade_id="reconcile_close:sell-1",
                wallet_address="0x3333333333333333333333333333333333333333",
                wallet_score=80.0,
                wallet_win_rate=0.7,
                wallet_profit_factor=2.0,
                wallet_avg_position_size=1000.0,
                market_id="market-sell",
                token_id="token-sell",
                outcome="Yes",
                side="sell",
                source_price_cents=55.0,
                source_size_usd=10.0,
                is_short_term=False,
            ),
            TradeIntent(
                external_trade_id="buy-fast-1",
                wallet_address="0x1111111111111111111111111111111111111111",
                wallet_score=100.0,
                wallet_win_rate=0.7,
                wallet_profit_factor=2.0,
                wallet_avg_position_size=1000.0,
                market_id="market-buy-1",
                token_id="token-buy-1",
                outcome="Yes",
                side="buy",
                source_price_cents=55.0,
                source_size_usd=10.0,
                is_short_term=False,
            ),
            TradeIntent(
                external_trade_id="buy-fast-2",
                wallet_address="0x2222222222222222222222222222222222222222",
                wallet_score=95.0,
                wallet_win_rate=0.7,
                wallet_profit_factor=2.0,
                wallet_avg_position_size=1000.0,
                market_id="market-buy-2",
                token_id="token-buy-2",
                outcome="Yes",
                side="buy",
                source_price_cents=55.0,
                source_size_usd=10.0,
                is_short_term=False,
            ),
        ]

        await BackgroundOrchestrator._execute_trade_intents(
            orchestrator,
            None,
            portfolio=object(),
            intents=intents,
        )

        assert orchestrator.trade_executor.execution_order == [
            "buy-fast-1",
            "buy-fast-2",
            "reconcile_close:sell-1",
        ]
        assert orchestrator.portfolio_tracker.calculate_calls == 1

    asyncio.run(_case())

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import settings
from core.background_tasks import BackgroundOrchestrator
from core.trade_monitor import TradeMonitor
from data.polymarket_client import WalletOpenPosition, WalletTradeSignal
from models.models import Base, Position, TradeSide
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


def test_trade_reconcile_scan_still_forces_account_sync() -> None:
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

        assert calls == [True]
        assert orchestrator.last_trade_reconcile_at is not None

    asyncio.run(_case())

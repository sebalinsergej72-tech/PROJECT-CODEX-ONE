from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.trade_executor import TradeExecutor
from core.risk_manager import PortfolioState, RiskDecision
from core.trade_monitor import TradeIntent
from config.settings import settings
from data.polymarket_client import OrderbookLevel, OrderbookSnapshot
from models.models import Base, CopiedTrade, Position, TradeSide, TradeStatus


def _intent(*, side: str, price_cents: float, size_usd: float) -> TradeIntent:
    return TradeIntent(
        external_trade_id=f"ext-{side}-{price_cents}-{size_usd}",
        wallet_address="0x1111111111111111111111111111111111111111",
        wallet_score=1.0,
        wallet_win_rate=0.7,
        wallet_profit_factor=2.0,
        wallet_avg_position_size=700.0,
        market_id="mkt-1",
        token_id="tok-1",
        outcome="Yes",
        side=side,
        source_price_cents=price_cents,
        source_size_usd=size_usd,
        is_short_term=False,
    )


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


def test_upsert_position_same_side_averages() -> None:
    async def _case(session: AsyncSession) -> None:
        await TradeExecutor._upsert_position(session, _intent(side="buy", price_cents=40.0, size_usd=8.0), 8.0)
        await TradeExecutor._upsert_position(session, _intent(side="buy", price_cents=60.0, size_usd=6.0), 6.0)

        row = (await session.execute(select(Position))).scalar_one()
        assert row.is_open is True
        assert row.side == TradeSide.BUY.value
        assert row.quantity == pytest.approx(30.0, rel=1e-6)
        assert row.avg_price_cents == pytest.approx(46.6666667, rel=1e-6)
        assert row.invested_usd == pytest.approx(14.0, rel=1e-6)
        assert row.realized_pnl_usd == pytest.approx(0.0, rel=1e-6)

    asyncio.run(_run_with_session(_case))


def test_upsert_position_partial_close_long() -> None:
    async def _case(session: AsyncSession) -> None:
        await TradeExecutor._upsert_position(session, _intent(side="buy", price_cents=50.0, size_usd=10.0), 10.0)
        await TradeExecutor._upsert_position(session, _intent(side="sell", price_cents=70.0, size_usd=5.0), 5.0)

        row = (await session.execute(select(Position))).scalar_one()
        assert row.is_open is True
        assert row.side == TradeSide.BUY.value
        assert row.quantity == pytest.approx(12.857142857, rel=1e-6)
        assert row.invested_usd == pytest.approx(6.4286, rel=1e-4)
        assert row.realized_pnl_usd == pytest.approx(1.4286, rel=1e-4)

    asyncio.run(_run_with_session(_case))


def test_upsert_position_exact_close_sets_closed() -> None:
    async def _case(session: AsyncSession) -> None:
        await TradeExecutor._upsert_position(session, _intent(side="sell", price_cents=80.0, size_usd=8.0), 8.0)
        await TradeExecutor._upsert_position(session, _intent(side="buy", price_cents=60.0, size_usd=6.0), 6.0)

        row = (await session.execute(select(Position))).scalar_one()
        assert row.is_open is False
        assert row.quantity == pytest.approx(0.0, rel=1e-6)
        assert row.invested_usd == pytest.approx(0.0, rel=1e-6)
        assert row.realized_pnl_usd == pytest.approx(2.0, rel=1e-6)
        assert row.closed_at is not None

    asyncio.run(_run_with_session(_case))


def test_upsert_position_flip_after_full_close() -> None:
    async def _case(session: AsyncSession) -> None:
        await TradeExecutor._upsert_position(session, _intent(side="buy", price_cents=50.0, size_usd=10.0), 10.0)
        await TradeExecutor._upsert_position(session, _intent(side="sell", price_cents=40.0, size_usd=20.0), 20.0)

        row = (await session.execute(select(Position))).scalar_one()
        assert row.is_open is True
        assert row.side == TradeSide.SELL.value
        assert row.avg_price_cents == pytest.approx(40.0, rel=1e-6)
        assert row.quantity == pytest.approx(30.0, rel=1e-6)
        assert row.invested_usd == pytest.approx(12.0, rel=1e-6)
        assert row.realized_pnl_usd == pytest.approx(-2.0, rel=1e-6)

    asyncio.run(_run_with_session(_case))


def test_market_position_precheck_counts_pending_submitted_orders() -> None:
    class _DummyClient:
        pass

    class _DummyPortfolioTracker:
        ACCOUNT_SYNC_WALLET = "account_sync"

    async def _case(session: AsyncSession) -> None:
        executor = TradeExecutor(_DummyClient(), _DummyClient(), _DummyClient(), _DummyPortfolioTracker())
        session.add(
            CopiedTrade(
                external_trade_id="ext-submitted-1",
                wallet_address="0xabc",
                market_id="mkt-1",
                token_id="tok-1",
                outcome="Yes",
                side=TradeSide.BUY.value,
                price_cents=55.0,
                size_usd=4.0,
                filled_size_usd=1.5,
                status=TradeStatus.PARTIAL.value,
            )
        )
        session.add(
            CopiedTrade(
                external_trade_id="ext-submitted-2",
                wallet_address="0xdef",
                market_id="mkt-1",
                token_id="tok-2",
                outcome="Yes",
                side=TradeSide.BUY.value,
                price_cents=44.0,
                size_usd=3.0,
                filled_size_usd=0.0,
                status=TradeStatus.SUBMITTED.value,
            )
        )
        await session.flush()

        portfolio = PortfolioState(
            total_equity_usd=100.0,
            available_cash_usd=100.0,
            exposure_usd=0.0,
            daily_pnl_usd=0.0,
            cumulative_pnl_usd=0.0,
            open_positions=0,
        )
        allowed = await executor._apply_market_position_precheck(
            session=session,
            market_id="mkt-1",
            requested_size_usd=10.0,
            portfolio_state=portfolio,
            risk_mode="aggressive",
        )

        # aggressive market cap = 10% of $100 = $10
        # pending reserve = (4.0 - 1.5) + 3.0 = 5.5
        assert allowed == pytest.approx(4.5, rel=1e-6)

    asyncio.run(_run_with_session(_case))


def test_build_execution_plan_uses_gtc_for_aggressive_buy() -> None:
    class _DummyClient:
        pass

    class _DummyPortfolioTracker:
        ACCOUNT_SYNC_WALLET = "account_sync"

    executor = TradeExecutor(_DummyClient(), _DummyClient(), _DummyClient(), _DummyPortfolioTracker())
    executor.risk_manager.can_accept_slippage = lambda **kwargs: (True, 5.0)  # type: ignore[method-assign]

    plan = executor._build_execution_plan(
        intent=_intent(side="buy", price_cents=60.0, size_usd=7.0),
        target_size_usd=7.0,
        risk_mode="aggressive",
    )

    assert plan is not None
    assert plan.order_type == "GTC"
    assert plan.order_request.order_type == "GTC"


def test_build_execution_plan_keeps_fak_for_aggressive_sell() -> None:
    class _DummyClient:
        pass

    class _DummyPortfolioTracker:
        ACCOUNT_SYNC_WALLET = "account_sync"

    executor = TradeExecutor(_DummyClient(), _DummyClient(), _DummyClient(), _DummyPortfolioTracker())
    executor.risk_manager.can_accept_slippage = lambda **kwargs: (True, 5.0)  # type: ignore[method-assign]

    plan = executor._build_execution_plan(
        intent=_intent(side="sell", price_cents=60.0, size_usd=7.0),
        target_size_usd=7.0,
        risk_mode="aggressive",
    )

    assert plan is not None
    assert plan.order_type == "FAK"
    assert plan.order_request.order_type == "FAK"


def test_execute_copy_trade_keeps_live_gtc_submitted_for_fill_monitor() -> None:
    class _DummyPolymarketClient:
        async def fetch_orderbook(self, token_id):
            return OrderbookSnapshot(
                bids=[OrderbookLevel(price=0.59, size=100.0)],
                asks=[OrderbookLevel(price=0.60, size=100.0)],
                best_bid=0.59,
                best_ask=0.60,
            )

        async def place_order(self, request):
            return type("OrderResult", (), {"success": True, "order_id": "ord-1", "tx_hash": "tx-1", "error": None})()

        def is_dry_run(self) -> bool:
            return False

    class _DummyRiskManager:
        def evaluate_trade(self, **kwargs):
            return RiskDecision(
                allowed=True,
                reason="ok",
                target_size_usd=7.0,
                requires_manual_confirmation=False,
                wallet_multiplier=1.0,
                kelly_fraction=0.0,
            )

        def can_accept_slippage(self, **kwargs):
            return True, 5.0

        @staticmethod
        def compute_slippage_bps(**kwargs):
            return 0.0

    class _DummyNotifications:
        async def send_message(self, text: str) -> None:
            return None

    class _DummyPortfolioTracker:
        ACCOUNT_SYNC_WALLET = "account_sync"

    async def _case(session: AsyncSession) -> None:
        executor = TradeExecutor(
            _DummyPolymarketClient(),
            _DummyRiskManager(),
            _DummyNotifications(),
            _DummyPortfolioTracker(),
        )

        async def _unexpected_reconcile(**kwargs):
            raise AssertionError("GTC live order should wait for fill monitor")

        executor._reconcile_trade_fill_state = _unexpected_reconcile  # type: ignore[method-assign]

        portfolio = PortfolioState(
            total_equity_usd=100.0,
            available_cash_usd=100.0,
            exposure_usd=0.0,
            daily_pnl_usd=0.0,
            cumulative_pnl_usd=0.0,
            open_positions=0,
        )

        await executor.execute_copy_trade(
            session,
            _intent(side="buy", price_cents=60.0, size_usd=7.0),
            portfolio,
            risk_mode="aggressive",
            fill_mode="conservative",
            price_filter_enabled=False,
            high_conviction_boost_enabled=False,
        )

        row = (await session.execute(select(CopiedTrade))).scalar_one()
        assert row.status == TradeStatus.SUBMITTED.value
        assert row.order_id == "ord-1"
        assert row.ttl_expires_at is not None
        assert "awaiting_fill_monitor" in (row.reason or "")

    asyncio.run(_run_with_session(_case))


def test_execute_copy_trade_uses_intent_market_snapshot_before_fetching_orderbook() -> None:
    class _DummyPolymarketClient:
        def __init__(self) -> None:
            self.fetch_orderbook_calls = 0

        async def fetch_orderbook(self, token_id):
            self.fetch_orderbook_calls += 1
            raise AssertionError("fetch_orderbook should not run when market_snapshot is already attached")

        async def place_order(self, request):
            return type("OrderResult", (), {"success": True, "order_id": "ord-1", "tx_hash": "tx-1", "error": None})()

        def is_dry_run(self) -> bool:
            return False

    class _DummyRiskManager:
        def evaluate_trade(self, **kwargs):
            return RiskDecision(
                allowed=True,
                reason="ok",
                target_size_usd=7.0,
                requires_manual_confirmation=False,
                wallet_multiplier=1.0,
                kelly_fraction=0.0,
            )

        def can_accept_slippage(self, **kwargs):
            return True, 5.0

        @staticmethod
        def compute_slippage_bps(**kwargs):
            return 0.0

    class _DummyNotifications:
        async def send_message(self, text: str) -> None:
            return None

    class _DummyPortfolioTracker:
        ACCOUNT_SYNC_WALLET = "account_sync"

    async def _case(session: AsyncSession) -> None:
        client = _DummyPolymarketClient()
        executor = TradeExecutor(
            client,
            _DummyRiskManager(),
            _DummyNotifications(),
            _DummyPortfolioTracker(),
        )
        intent = _intent(side="buy", price_cents=60.0, size_usd=7.0)
        intent.market_snapshot = OrderbookSnapshot(
            bids=[OrderbookLevel(price=0.59, size=100.0)],
            asks=[OrderbookLevel(price=0.60, size=100.0)],
            best_bid=0.59,
            best_ask=0.60,
        )
        portfolio = PortfolioState(
            total_equity_usd=100.0,
            available_cash_usd=100.0,
            exposure_usd=0.0,
            daily_pnl_usd=0.0,
            cumulative_pnl_usd=0.0,
            open_positions=0,
        )

        status = await executor.execute_copy_trade(
            session,
            intent,
            portfolio,
            risk_mode="aggressive",
            fill_mode="conservative",
            price_filter_enabled=False,
            high_conviction_boost_enabled=False,
        )

        assert status == TradeStatus.SUBMITTED.value
        assert client.fetch_orderbook_calls == 0

    asyncio.run(_run_with_session(_case))


def test_execute_copy_trade_defers_persistence_until_after_place_order() -> None:
    class _DummyPolymarketClient:
        def __init__(self, session: AsyncSession) -> None:
            self.session = session

        async def fetch_orderbook(self, token_id):
            return OrderbookSnapshot(
                bids=[OrderbookLevel(price=0.59, size=100.0)],
                asks=[OrderbookLevel(price=0.60, size=100.0)],
                best_bid=0.59,
                best_ask=0.60,
            )

        async def place_order(self, request):
            before_submit = (await self.session.execute(select(func.count()).select_from(CopiedTrade))).scalar_one()
            assert before_submit == 0
            return type("OrderResult", (), {"success": True, "order_id": "ord-deferred", "tx_hash": "tx-deferred", "error": None})()

        def is_dry_run(self) -> bool:
            return False

    class _DummyRiskManager:
        def evaluate_trade(self, **kwargs):
            return RiskDecision(
                allowed=True,
                reason="ok",
                target_size_usd=7.0,
                requires_manual_confirmation=False,
                wallet_multiplier=1.0,
                kelly_fraction=0.0,
            )

        def can_accept_slippage(self, **kwargs):
            return True, 5.0

        @staticmethod
        def compute_slippage_bps(**kwargs):
            return 0.0

    class _DummyNotifications:
        async def send_message(self, text: str) -> None:
            return None

    class _DummyPortfolioTracker:
        ACCOUNT_SYNC_WALLET = "account_sync"

    async def _case(session: AsyncSession) -> None:
        executor = TradeExecutor(
            _DummyPolymarketClient(session),
            _DummyRiskManager(),
            _DummyNotifications(),
            _DummyPortfolioTracker(),
        )

        async def _unexpected_reconcile(**kwargs):
            raise AssertionError("GTC live order should wait for fill monitor")

        executor._reconcile_trade_fill_state = _unexpected_reconcile  # type: ignore[method-assign]

        portfolio = PortfolioState(
            total_equity_usd=100.0,
            available_cash_usd=100.0,
            exposure_usd=0.0,
            daily_pnl_usd=0.0,
            cumulative_pnl_usd=0.0,
            open_positions=0,
        )

        await executor.execute_copy_trade(
            session,
            _intent(side="buy", price_cents=60.0, size_usd=7.0),
            portfolio,
            risk_mode="aggressive",
            fill_mode="conservative",
            price_filter_enabled=False,
            high_conviction_boost_enabled=False,
        )

        row = (await session.execute(select(CopiedTrade))).scalar_one()
        assert row.status == TradeStatus.SUBMITTED.value
        assert row.order_id == "ord-deferred"

    asyncio.run(_run_with_session(_case))


def test_execute_copy_trade_skips_on_low_liquidity() -> None:
    class _DummyPolymarketClient:
        async def fetch_orderbook(self, token_id):
            return OrderbookSnapshot(
                bids=[OrderbookLevel(price=0.59, size=2.0)],
                asks=[OrderbookLevel(price=0.60, size=2.0)],
                best_bid=0.59,
                best_ask=0.60,
            )

        async def place_order(self, request):
            raise AssertionError("place_order should not be called when liquidity is too low")

        def is_dry_run(self) -> bool:
            return False

    class _DummyRiskManager:
        def evaluate_trade(self, **kwargs):
            return RiskDecision(True, "ok", 7.0, False, 1.0, 0.0)

        def can_accept_slippage(self, **kwargs):
            return True, 5.0

    class _DummyNotifications:
        async def send_message(self, text: str) -> None:
            return None

    class _DummyPortfolioTracker:
        ACCOUNT_SYNC_WALLET = "account_sync"

    async def _case(session: AsyncSession) -> None:
        executor = TradeExecutor(
            _DummyPolymarketClient(),
            _DummyRiskManager(),
            _DummyNotifications(),
            _DummyPortfolioTracker(),
        )

        portfolio = PortfolioState(100.0, 100.0, 0.0, 0.0, 0.0, 0)
        await executor.execute_copy_trade(
            session,
            _intent(side="buy", price_cents=60.0, size_usd=7.0),
            portfolio,
            risk_mode="aggressive",
            fill_mode="conservative",
            price_filter_enabled=False,
            high_conviction_boost_enabled=False,
        )

        row = (await session.execute(select(CopiedTrade))).scalar_one()
        assert row.status == TradeStatus.SKIPPED.value
        assert row.reason == "low_liquidity:1.20<10.00"

    asyncio.run(_run_with_session(_case))


def test_execute_copy_trade_allows_small_absolute_move_on_low_price_market() -> None:
    class _DummyPolymarketClient:
        async def fetch_orderbook(self, token_id):
            return OrderbookSnapshot(
                bids=[OrderbookLevel(price=0.108, size=200.0)],
                asks=[OrderbookLevel(price=0.108, size=200.0)],
                best_bid=0.108,
                best_ask=0.108,
            )

        async def place_order(self, request):
            return type("OrderResult", (), {"success": True, "order_id": "ord-low", "tx_hash": "tx-low", "error": None})()

        def is_dry_run(self) -> bool:
            return False

    class _DummyRiskManager:
        def evaluate_trade(self, **kwargs):
            return RiskDecision(True, "ok", 7.0, False, 1.0, 0.0)

        def can_accept_slippage(self, **kwargs):
            return True, 35.0

        @staticmethod
        def compute_slippage_bps(**kwargs):
            return 0.0

    class _DummyNotifications:
        async def send_message(self, text: str) -> None:
            return None

    class _DummyPortfolioTracker:
        ACCOUNT_SYNC_WALLET = "account_sync"

    async def _case(session: AsyncSession) -> None:
        executor = TradeExecutor(
            _DummyPolymarketClient(),
            _DummyRiskManager(),
            _DummyNotifications(),
            _DummyPortfolioTracker(),
        )

        portfolio = PortfolioState(100.0, 100.0, 0.0, 0.0, 0.0, 0)
        await executor.execute_copy_trade(
            session,
            _intent(side="buy", price_cents=10.0, size_usd=7.0),
            portfolio,
            risk_mode="aggressive",
            fill_mode="conservative",
            price_filter_enabled=False,
            high_conviction_boost_enabled=False,
        )

        row = (await session.execute(select(CopiedTrade))).scalar_one()
        assert row.status == TradeStatus.SUBMITTED.value
        assert row.order_id == "ord-low"

    asyncio.run(_run_with_session(_case))


def test_execute_copy_trade_still_skips_large_price_move() -> None:
    class _DummyPolymarketClient:
        async def fetch_orderbook(self, token_id):
            return OrderbookSnapshot(
                bids=[OrderbookLevel(price=0.170, size=200.0)],
                asks=[OrderbookLevel(price=0.170, size=200.0)],
                best_bid=0.170,
                best_ask=0.170,
            )

        async def place_order(self, request):
            raise AssertionError("place_order should not be called when price moved too far")

        def is_dry_run(self) -> bool:
            return False

    class _DummyRiskManager:
        def evaluate_trade(self, **kwargs):
            return RiskDecision(True, "ok", 7.0, False, 1.0, 0.0)

        def can_accept_slippage(self, **kwargs):
            return True, 35.0

    class _DummyNotifications:
        async def send_message(self, text: str) -> None:
            return None

    class _DummyPortfolioTracker:
        ACCOUNT_SYNC_WALLET = "account_sync"

    async def _case(session: AsyncSession) -> None:
        executor = TradeExecutor(
            _DummyPolymarketClient(),
            _DummyRiskManager(),
            _DummyNotifications(),
            _DummyPortfolioTracker(),
        )

        portfolio = PortfolioState(100.0, 100.0, 0.0, 0.0, 0.0, 0)
        await executor.execute_copy_trade(
            session,
            _intent(side="buy", price_cents=10.0, size_usd=7.0),
            portfolio,
            risk_mode="aggressive",
            fill_mode="conservative",
            price_filter_enabled=False,
            high_conviction_boost_enabled=False,
        )

        row = (await session.execute(select(CopiedTrade))).scalar_one()
        assert row.status == TradeStatus.SKIPPED.value
        assert row.reason == "price_moved:70.0%"

    asyncio.run(_run_with_session(_case))


def test_validate_sell_size_trims_to_available_position() -> None:
    position = Position(
        wallet_address="0xabc",
        market_id="mkt-1",
        token_id="tok-1",
        outcome="Yes",
        side=TradeSide.BUY.value,
        quantity=5.0,
        avg_price_cents=50.0,
        invested_usd=2.5,
        current_price_cents=60.0,
        realized_pnl_usd=0.0,
        unrealized_pnl_usd=0.0,
        is_open=True,
    )

    trimmed, reason = TradeExecutor._validate_sell_size_against_position(
        position=position,
        requested_size_usd=9.0,
        execution_price_cents=60.0,
    )

    assert trimmed == pytest.approx(3.0, rel=1e-6)
    assert reason is None


def test_minimum_position_size_is_disabled_when_min_trade_guard_is_off() -> None:
    original = settings.enforce_min_trade_size
    settings.enforce_min_trade_size = False
    try:
        minimum = TradeExecutor._minimum_position_size(
            portfolio_state=PortfolioState(100.0, 50.0, 0.0, 0.0, 0.0, 0),
            risk_mode="aggressive",
        )
    finally:
        settings.enforce_min_trade_size = original

    assert minimum == 0.0


def test_execute_copy_trade_skips_sell_without_confirmed_position() -> None:
    class _DummyPolymarketClient:
        async def place_order(self, request):
            raise AssertionError("sell without confirmed position should not place a live order")

        def is_dry_run(self) -> bool:
            return False

    class _DummyRiskManager:
        def evaluate_trade(self, **kwargs):
            raise AssertionError("sell without confirmed position should short-circuit before risk evaluation")

    class _DummyNotifications:
        async def send_message(self, text: str) -> None:
            return None

    class _DummyPortfolioTracker:
        ACCOUNT_SYNC_WALLET = "account_sync"

    async def _case(session: AsyncSession) -> None:
        executor = TradeExecutor(
            _DummyPolymarketClient(),
            _DummyRiskManager(),
            _DummyNotifications(),
            _DummyPortfolioTracker(),
        )
        portfolio = PortfolioState(
            total_equity_usd=100.0,
            available_cash_usd=100.0,
            exposure_usd=0.0,
            daily_pnl_usd=0.0,
            cumulative_pnl_usd=0.0,
            open_positions=0,
        )

        await executor.execute_copy_trade(
            session,
            _intent(side="sell", price_cents=60.0, size_usd=7.0),
            portfolio,
            risk_mode="aggressive",
            fill_mode="conservative",
            price_filter_enabled=False,
            high_conviction_boost_enabled=False,
        )

        row = (await session.execute(select(CopiedTrade))).scalar_one()
        assert row.status == TradeStatus.SKIPPED.value
        assert row.reason == "sell_without_confirmed_position"

    asyncio.run(_run_with_session(_case))


def test_maybe_reprice_submitted_gtc_buy_replaces_order_once() -> None:
    class _DummyPolymarketClient:
        def __init__(self) -> None:
            self.cancelled_order_ids: list[str] = []
            self.placed_requests = []

        def is_dry_run(self) -> bool:
            return False

        async def cancel_order(self, order_id: str) -> bool:
            self.cancelled_order_ids.append(order_id)
            return True

        async def place_order(self, request):
            self.placed_requests.append(request)
            return type(
                "OrderResult",
                (),
                {"success": True, "order_id": "ord-2", "tx_hash": "tx-2", "error": None},
            )()

    class _DummyRiskManager:
        def can_accept_slippage(self, **kwargs):
            return True, 15.0

    class _DummyNotifications:
        async def send_message(self, text: str) -> None:
            return None

    class _DummyPortfolioTracker:
        ACCOUNT_SYNC_WALLET = "account_sync"

    client = _DummyPolymarketClient()
    executor = TradeExecutor(client, _DummyRiskManager(), _DummyNotifications(), _DummyPortfolioTracker())
    now = datetime.now(tz=timezone.utc)
    copied_trade = CopiedTrade(
        external_trade_id="ext-buy-reprice",
        wallet_address="0xabc",
        market_id="mkt-1",
        token_id="tok-1",
        outcome="Yes",
        side=TradeSide.BUY.value,
        price_cents=60.0,
        size_usd=7.0,
        status=TradeStatus.SUBMITTED.value,
        order_id="ord-1",
        tx_hash="tx-1",
        submitted_at=now - timedelta(seconds=30),
        ttl_expires_at=now + timedelta(seconds=30),
        reason="submitted mode=aggressive type=GTC limit=60.03c slip=5.00bps | awaiting_fill_monitor",
    )

    repriced = asyncio.run(executor.maybe_reprice_submitted_gtc_buy(copied_trade=copied_trade, now=now))

    assert repriced is True
    assert client.cancelled_order_ids == ["ord-1"]
    assert len(client.placed_requests) == 1
    assert client.placed_requests[0].order_type == "GTC"
    expected_price = copied_trade.price_cents * (1 + settings.max_allowed_slippage_bps / 10_000.0)
    assert client.placed_requests[0].price_cents == pytest.approx(expected_price, rel=1e-6)
    assert copied_trade.order_id == "ord-2"
    assert "repriced_once" in (copied_trade.reason or "")

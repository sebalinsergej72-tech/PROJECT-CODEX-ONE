from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import settings
from core.wallet_discovery import CandidateSeed, CopyabilityStats, DiscoveryThresholds, WalletDiscovery
from models.models import Base
from models.qualified_wallet import QualifiedWallet
from utils.helpers import utc_now


class _DummyNotifications:
    enabled = False


class _DummyPolymarketClient:
    async def get_user_trades(self, wallet_address: str, limit: int = 500):
        now = utc_now()
        rows = []
        for idx in range(10):
            rows.append(
                {
                    "timestamp": (now - timedelta(days=idx)).isoformat(),
                    "size": 50_000.0,
                    "pnl": 500.0,
                }
            )
        return rows

    async def get_user_activity(self, wallet_address: str, limit: int = 500):
        now = utc_now()
        return [{"timestamp": (now - timedelta(days=60)).isoformat()}]


class _HintOnlyPolymarketClient:
    async def get_user_trades(self, wallet_address: str, limit: int = 500):
        return []

    async def get_user_activity(self, wallet_address: str, limit: int = 500):
        now = utc_now()
        return [{"timestamp": (now - timedelta(days=90)).isoformat()}]


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


def test_tradability_penalty_thresholds() -> None:
    assert WalletDiscovery._tradability_penalty(None) == 0.0
    assert WalletDiscovery._tradability_penalty((3, 0)) == 0.0
    assert WalletDiscovery._tradability_penalty((10, 1)) == 100.0
    assert WalletDiscovery._tradability_penalty((10, 2)) == 60.0
    assert WalletDiscovery._tradability_penalty((10, 4)) == 30.0
    assert WalletDiscovery._tradability_penalty((10, 6)) == 10.0
    assert WalletDiscovery._tradability_penalty((10, 8)) == 0.0


def test_tradability_penalty_grows_for_uncopyable_reason_rates() -> None:
    clean = CopyabilityStats(attempts=10, fills=8, no_orderbook=0, low_liquidity=0, price_moved=0, hard_slippage=0)
    toxic = CopyabilityStats(attempts=10, fills=8, no_orderbook=4, low_liquidity=0, price_moved=3, hard_slippage=0)

    assert WalletDiscovery._tradability_penalty(clean) == 0.0
    assert WalletDiscovery._tradability_penalty(toxic) == 60.0


def test_score_caps_avg_size_component() -> None:
    discovery = WalletDiscovery(_DummyPolymarketClient(), _DummyNotifications())
    seed = CandidateSeed(
        address="0x1111111111111111111111111111111111111111",
        name="whale",
        niches={"sports"},
        monthly_pnl_pct=0.20,
        trades_90d_hint=200,
        trades_30d_hint=50,
        profit_factor_hint=2.5,
        avg_size_hint=50_000.0,
        consecutive_losses_hint=0,
        wallet_age_days_hint=60,
    )
    thresholds = DiscoveryThresholds(
        min_trades_90d=120,
        min_win_rate=0.65,
        min_profit_factor=1.55,
        min_avg_size=350.0,
        max_days_since_last_trade=7,
        max_consecutive_losses=5,
        min_wallet_age_days=30,
    )

    wallet, reserve_wallet, reason, _ = asyncio.run(
        discovery._score_single_wallet(
            seed,
            utc_now(),
            thresholds,
            discovery._reserve_thresholds_for_mode("aggressive"),
            None,
        )
    )

    assert reason is None
    assert wallet is not None
    assert reserve_wallet is None
    assert wallet.score == pytest.approx(278.5, rel=1e-3)
    assert wallet.score < 300.0


def test_softened_reserve_thresholds_capture_near_pass_wallet() -> None:
    discovery = WalletDiscovery(_HintOnlyPolymarketClient(), _DummyNotifications())
    seed = CandidateSeed(
        address="0x2222222222222222222222222222222222222222",
        name="near-pass",
        niches={"politics"},
        monthly_pnl_pct=0.08,
        win_rate_hint=0.63,
        trades_90d_hint=100,
        trades_30d_hint=18,
        profit_factor_hint=1.50,
        avg_size_hint=300.0,
        last_trade_ts_hint=utc_now() - timedelta(days=1),
        consecutive_losses_hint=0,
        wallet_age_days_hint=90,
    )
    thresholds = DiscoveryThresholds(
        min_trades_90d=120,
        min_win_rate=0.65,
        min_profit_factor=1.55,
        min_avg_size=350.0,
        max_days_since_last_trade=7,
        max_consecutive_losses=5,
        min_wallet_age_days=30,
    )
    reserve_thresholds = discovery._reserve_thresholds_for_mode("aggressive")

    wallet, reserve_wallet, reason, progress = asyncio.run(
        discovery._score_single_wallet(
            seed,
            utc_now(),
            thresholds,
            reserve_thresholds,
            None,
        )
    )

    assert wallet is None
    assert reserve_wallet is not None
    assert reason == "min_trades_90d"
    assert progress.passed_recency is True
    assert progress.passed_wallet_age is True
    assert progress.passed_trades is False


def test_discovery_promotes_reserve_wallets_when_live_pool_is_short() -> None:
    strict_a = CandidateSeed(
        address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        name="strict-a",
        niches={"politics"},
        monthly_pnl_pct=0.10,
        win_rate_hint=0.72,
        trades_90d_hint=150,
        trades_30d_hint=30,
        profit_factor_hint=1.9,
        avg_size_hint=800.0,
        last_trade_ts_hint=utc_now() - timedelta(days=1),
        consecutive_losses_hint=0,
        wallet_age_days_hint=120,
    )
    strict_b = CandidateSeed(
        address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        name="strict-b",
        niches={"business"},
        monthly_pnl_pct=0.09,
        win_rate_hint=0.70,
        trades_90d_hint=145,
        trades_30d_hint=28,
        profit_factor_hint=1.8,
        avg_size_hint=700.0,
        last_trade_ts_hint=utc_now() - timedelta(days=1),
        consecutive_losses_hint=0,
        wallet_age_days_hint=120,
    )
    reserve_a = CandidateSeed(
        address="0xcccccccccccccccccccccccccccccccccccccccc",
        name="reserve-a",
        niches={"crypto"},
        monthly_pnl_pct=0.07,
        win_rate_hint=0.63,
        trades_90d_hint=95,
        trades_30d_hint=16,
        profit_factor_hint=1.48,
        avg_size_hint=320.0,
        last_trade_ts_hint=utc_now() - timedelta(days=1),
        consecutive_losses_hint=0,
        wallet_age_days_hint=90,
    )
    reserve_b = CandidateSeed(
        address="0xdddddddddddddddddddddddddddddddddddddddd",
        name="reserve-b",
        niches={"sports"},
        monthly_pnl_pct=0.06,
        win_rate_hint=0.62,
        trades_90d_hint=95,
        trades_30d_hint=14,
        profit_factor_hint=1.46,
        avg_size_hint=280.0,
        last_trade_ts_hint=utc_now() - timedelta(days=1),
        consecutive_losses_hint=0,
        wallet_age_days_hint=90,
    )

    async def _case(session: AsyncSession) -> None:
        discovery = WalletDiscovery(_HintOnlyPolymarketClient(), _DummyNotifications())
        original_limit = settings.max_wallets_aggressive
        original_reserve_target = settings.reserve_wallet_pool_target
        try:
            settings.max_wallets_aggressive = 3
            settings.reserve_wallet_pool_target = 2

            async def _fetch_candidate_seeds():
                return {
                    strict_a.address: strict_a,
                    strict_b.address: strict_b,
                    reserve_a.address: reserve_a,
                    reserve_b.address: reserve_b,
                }

            discovery._fetch_candidate_seeds = _fetch_candidate_seeds

            result = await discovery.discover_and_score(session, auto_add=True, risk_mode="aggressive")

            rows = (
                await session.execute(
                    select(QualifiedWallet).order_by(QualifiedWallet.address.asc())
                )
            ).scalars().all()
        finally:
            settings.max_wallets_aggressive = original_limit
            settings.reserve_wallet_pool_target = original_reserve_target

        enabled = {row.address for row in rows if row.enabled}
        stored = {row.address for row in rows}

        assert result.counters.passed_all_filters == 2
        assert result.counters.reserve_eligible == 2
        assert result.reserve_wallets == 2
        assert result.reserve_promoted == 1
        assert result.enabled_wallets == 3
        assert strict_a.address in enabled
        assert strict_b.address in enabled
        assert reserve_a.address in enabled or reserve_b.address in enabled
        assert stored == {
            strict_a.address,
            strict_b.address,
            reserve_a.address,
            reserve_b.address,
        }

    asyncio.run(_run_with_session(_case))


def test_discovery_disables_stale_strict_wallet_and_uses_fresh_reserve_for_live_pool() -> None:
    stale_strict = CandidateSeed(
        address="0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        name="stale-strict",
        niches={"politics"},
        monthly_pnl_pct=0.12,
        win_rate_hint=0.74,
        trades_90d_hint=180,
        trades_30d_hint=35,
        profit_factor_hint=1.95,
        avg_size_hint=900.0,
        last_trade_ts_hint=utc_now() - timedelta(days=5),
        consecutive_losses_hint=0,
        wallet_age_days_hint=120,
    )
    fresh_strict = CandidateSeed(
        address="0xffffffffffffffffffffffffffffffffffffffff",
        name="fresh-strict",
        niches={"business"},
        monthly_pnl_pct=0.10,
        win_rate_hint=0.72,
        trades_90d_hint=160,
        trades_30d_hint=28,
        profit_factor_hint=1.85,
        avg_size_hint=800.0,
        last_trade_ts_hint=utc_now() - timedelta(days=1),
        consecutive_losses_hint=0,
        wallet_age_days_hint=120,
    )
    reserve_fresh = CandidateSeed(
        address="0x9999999999999999999999999999999999999999",
        name="reserve-fresh",
        niches={"sports"},
        monthly_pnl_pct=0.08,
        win_rate_hint=0.63,
        trades_90d_hint=95,
        trades_30d_hint=18,
        profit_factor_hint=1.48,
        avg_size_hint=300.0,
        last_trade_ts_hint=utc_now() - timedelta(days=1),
        consecutive_losses_hint=0,
        wallet_age_days_hint=90,
    )

    async def _case(session: AsyncSession) -> None:
        discovery = WalletDiscovery(_HintOnlyPolymarketClient(), _DummyNotifications())
        original_limit = settings.max_wallets_aggressive
        original_reserve_target = settings.reserve_wallet_pool_target
        original_live_freshness = settings.live_pool_max_days_since_last_trade_aggressive
        try:
            settings.max_wallets_aggressive = 2
            settings.reserve_wallet_pool_target = 2
            settings.live_pool_max_days_since_last_trade_aggressive = 2

            async def _fetch_candidate_seeds():
                return {
                    stale_strict.address: stale_strict,
                    fresh_strict.address: fresh_strict,
                    reserve_fresh.address: reserve_fresh,
                }

            discovery._fetch_candidate_seeds = _fetch_candidate_seeds

            result = await discovery.discover_and_score(session, auto_add=True, risk_mode="aggressive")
            rows = (
                await session.execute(select(QualifiedWallet).order_by(QualifiedWallet.address.asc()))
            ).scalars().all()
        finally:
            settings.max_wallets_aggressive = original_limit
            settings.reserve_wallet_pool_target = original_reserve_target
            settings.live_pool_max_days_since_last_trade_aggressive = original_live_freshness

        enabled = {row.address for row in rows if row.enabled}
        stale_row = next(row for row in rows if row.address == stale_strict.address)

        assert result.counters.passed_all_filters == 2
        assert result.counters.reserve_eligible == 1
        assert result.enabled_wallets == 2
        assert fresh_strict.address in enabled
        assert reserve_fresh.address in enabled
        assert stale_strict.address not in enabled
        assert stale_row.last_trade_ts is not None

    asyncio.run(_run_with_session(_case))


def test_discovery_live_pool_keeps_two_non_sports_wallets_when_available() -> None:
    sports_a = CandidateSeed(
        address="0x1111111111111111111111111111111111111111",
        name="sports-a",
        niches={"sports"},
        monthly_pnl_pct=0.11,
        win_rate_hint=0.70,
        trades_90d_hint=180,
        trades_30d_hint=30,
        profit_factor_hint=1.8,
        avg_size_hint=1200.0,
        last_trade_ts_hint=utc_now() - timedelta(days=1),
        consecutive_losses_hint=0,
        wallet_age_days_hint=120,
    )
    sports_b = CandidateSeed(
        address="0x2222222222222222222222222222222222222222",
        name="sports-b",
        niches={"sports"},
        monthly_pnl_pct=0.10,
        win_rate_hint=0.69,
        trades_90d_hint=170,
        trades_30d_hint=28,
        profit_factor_hint=1.75,
        avg_size_hint=1100.0,
        last_trade_ts_hint=utc_now() - timedelta(days=1),
        consecutive_losses_hint=0,
        wallet_age_days_hint=120,
    )
    sports_c = CandidateSeed(
        address="0x3333333333333333333333333333333333333333",
        name="sports-c",
        niches={"sports"},
        monthly_pnl_pct=0.095,
        win_rate_hint=0.68,
        trades_90d_hint=165,
        trades_30d_hint=27,
        profit_factor_hint=1.7,
        avg_size_hint=1000.0,
        last_trade_ts_hint=utc_now() - timedelta(days=1),
        consecutive_losses_hint=0,
        wallet_age_days_hint=120,
    )
    politics_a = CandidateSeed(
        address="0x4444444444444444444444444444444444444444",
        name="politics-a",
        niches={"politics"},
        monthly_pnl_pct=0.085,
        win_rate_hint=0.67,
        trades_90d_hint=150,
        trades_30d_hint=24,
        profit_factor_hint=1.6,
        avg_size_hint=700.0,
        last_trade_ts_hint=utc_now() - timedelta(days=1),
        consecutive_losses_hint=0,
        wallet_age_days_hint=120,
    )
    politics_b = CandidateSeed(
        address="0x5555555555555555555555555555555555555555",
        name="politics-b",
        niches={"politics"},
        monthly_pnl_pct=0.08,
        win_rate_hint=0.66,
        trades_90d_hint=145,
        trades_30d_hint=22,
        profit_factor_hint=1.55,
        avg_size_hint=650.0,
        last_trade_ts_hint=utc_now() - timedelta(days=1),
        consecutive_losses_hint=0,
        wallet_age_days_hint=120,
    )

    async def _case(session: AsyncSession) -> None:
        discovery = WalletDiscovery(_HintOnlyPolymarketClient(), _DummyNotifications())
        original_limit = settings.max_wallets_aggressive
        original_min_non_sports = settings.live_pool_min_non_sports_aggressive
        try:
            settings.max_wallets_aggressive = 4
            settings.live_pool_min_non_sports_aggressive = 2

            async def _fetch_candidate_seeds():
                return {
                    sports_a.address: sports_a,
                    sports_b.address: sports_b,
                    sports_c.address: sports_c,
                    politics_a.address: politics_a,
                    politics_b.address: politics_b,
                }

            discovery._fetch_candidate_seeds = _fetch_candidate_seeds
            await discovery.discover_and_score(session, auto_add=True, risk_mode="aggressive")
            rows = (
                await session.execute(select(QualifiedWallet).order_by(QualifiedWallet.address.asc()))
            ).scalars().all()
        finally:
            settings.max_wallets_aggressive = original_limit
            settings.live_pool_min_non_sports_aggressive = original_min_non_sports

        enabled = [row for row in rows if row.enabled]
        enabled_addresses = {row.address for row in enabled}
        non_sports_enabled = [
            row for row in enabled if "sports" not in {part.strip().lower() for part in (row.niche or "").split(",")}
        ]

        assert len(enabled) == 4
        assert len(non_sports_enabled) >= 2
        assert politics_a.address in enabled_addresses
        assert politics_b.address in enabled_addresses

    asyncio.run(_run_with_session(_case))

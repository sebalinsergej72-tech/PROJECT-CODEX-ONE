from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import settings
from core.wallet_discovery import CandidateSeed, DiscoveryThresholds, WalletDiscovery
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
        trades_90d_hint=105,
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

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from core.wallet_discovery import CandidateSeed, DiscoveryThresholds, WalletDiscovery
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

    wallet, reason, _ = asyncio.run(
        discovery._score_single_wallet(
            seed,
            utc_now(),
            thresholds,
            None,
        )
    )

    assert reason is None
    assert wallet is not None
    assert wallet.score == pytest.approx(278.5, rel=1e-3)
    assert wallet.score < 300.0

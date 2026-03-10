from __future__ import annotations

from core.risk_manager import PortfolioState, RiskManager
from config.settings import settings


def _portfolio() -> PortfolioState:
    return PortfolioState(
        total_equity_usd=100.0,
        available_cash_usd=100.0,
        exposure_usd=0.0,
        daily_pnl_usd=0.0,
        cumulative_pnl_usd=0.0,
        open_positions=0,
    )


def test_aggressive_risk_allows_trade_below_minimum_size_when_guard_disabled() -> None:
    decision = RiskManager().evaluate_trade(
        source_price_cents=60.0,
        source_size_usd=0.5,
        wallet_score=1.0,
        wallet_win_rate=0.45,
        wallet_profit_factor=1.01,
        wallet_avg_trade_size_usd=100.0,
        wallet_current_exposure_usd=0.0,
        portfolio=_portfolio(),
        risk_mode="aggressive",
        price_filter_enabled=False,
        high_conviction_boost_enabled=False,
    )

    assert decision.allowed is True
    assert decision.reason == "ok"
    assert decision.target_size_usd > 0.0


def test_aggressive_risk_rejects_trade_below_minimum_size_when_guard_enabled() -> None:
    original = settings.enforce_min_trade_size
    settings.enforce_min_trade_size = True
    try:
        decision = RiskManager().evaluate_trade(
            source_price_cents=60.0,
            source_size_usd=0.5,
            wallet_score=1.0,
            wallet_win_rate=0.45,
            wallet_profit_factor=1.01,
            wallet_avg_trade_size_usd=100.0,
            wallet_current_exposure_usd=0.0,
            portfolio=_portfolio(),
            risk_mode="aggressive",
            price_filter_enabled=False,
            high_conviction_boost_enabled=False,
        )
    finally:
        settings.enforce_min_trade_size = original

    assert decision.allowed is False
    assert decision.reason == "size_below_minimum"


def test_aggressive_risk_ignores_available_cash_when_flag_disabled() -> None:
    portfolio = PortfolioState(
        total_equity_usd=100.0,
        available_cash_usd=0.25,
        exposure_usd=0.0,
        daily_pnl_usd=0.0,
        cumulative_pnl_usd=0.0,
        open_positions=0,
    )
    decision = RiskManager().evaluate_trade(
        source_price_cents=60.0,
        source_size_usd=10.0,
        wallet_score=1.0,
        wallet_win_rate=0.7,
        wallet_profit_factor=1.8,
        wallet_avg_trade_size_usd=100.0,
        wallet_current_exposure_usd=0.0,
        portfolio=portfolio,
        risk_mode="aggressive",
        price_filter_enabled=False,
        high_conviction_boost_enabled=False,
    )

    assert decision.allowed is True
    assert decision.target_size_usd > portfolio.available_cash_usd


def test_aggressive_risk_respects_available_cash_when_flag_enabled() -> None:
    original = settings.ignore_available_cash_for_sizing
    settings.ignore_available_cash_for_sizing = False
    try:
        portfolio = PortfolioState(
            total_equity_usd=100.0,
            available_cash_usd=0.25,
            exposure_usd=0.0,
            daily_pnl_usd=0.0,
            cumulative_pnl_usd=0.0,
            open_positions=0,
        )
        decision = RiskManager().evaluate_trade(
            source_price_cents=60.0,
            source_size_usd=10.0,
            wallet_score=1.0,
            wallet_win_rate=0.7,
            wallet_profit_factor=1.8,
            wallet_avg_trade_size_usd=100.0,
            wallet_current_exposure_usd=0.0,
            portfolio=portfolio,
            risk_mode="aggressive",
            price_filter_enabled=False,
            high_conviction_boost_enabled=False,
        )
    finally:
        settings.ignore_available_cash_for_sizing = original

    assert decision.allowed is True
    assert decision.target_size_usd == portfolio.available_cash_usd


def test_aggressive_wallet_multiplier_is_capped_more_conservatively() -> None:
    decision = RiskManager().evaluate_trade(
        source_price_cents=60.0,
        source_size_usd=10.0,
        wallet_score=999.0,
        wallet_win_rate=0.8,
        wallet_profit_factor=4.0,
        wallet_avg_trade_size_usd=10.0,
        wallet_current_exposure_usd=0.0,
        portfolio=_portfolio(),
        risk_mode="aggressive",
        price_filter_enabled=False,
        high_conviction_boost_enabled=False,
    )

    assert decision.allowed is True
    assert decision.wallet_multiplier <= 2.0
    assert decision.kelly_fraction <= 0.15

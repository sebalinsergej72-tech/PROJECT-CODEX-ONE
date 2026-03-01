from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from config.settings import RiskMode, settings


@dataclass(slots=True)
class PortfolioState:
    total_equity_usd: float
    available_cash_usd: float
    exposure_usd: float
    daily_pnl_usd: float
    cumulative_pnl_usd: float
    open_positions: int
    daily_drawdown_pct: float = 0.0


@dataclass(slots=True)
class RiskDecision:
    allowed: bool
    reason: str
    target_size_usd: float
    requires_manual_confirmation: bool
    wallet_multiplier: float = 1.0
    kelly_fraction: float = 0.0


class RiskManager:
    """Applies conservative or aggressive risk controls for selective copy-trading."""

    def evaluate_trade(
        self,
        *,
        source_price_cents: float,
        source_size_usd: float,
        wallet_score: float,
        wallet_win_rate: float,
        wallet_profit_factor: float,
        wallet_avg_trade_size_usd: float,
        wallet_current_exposure_usd: float,
        portfolio: PortfolioState,
        risk_mode: RiskMode,
        price_filter_enabled: bool,
        high_conviction_boost_enabled: bool,
    ) -> RiskDecision:
        if self._drawdown_stop_triggered(portfolio, risk_mode):
            return RiskDecision(False, "drawdown_stop_triggered", 0.0, False)

        if price_filter_enabled and not self._price_filter(source_price_cents):
            return RiskDecision(
                allowed=False,
                reason=f"price_out_of_range_{settings.price_min_cents}_{settings.price_max_cents}",
                target_size_usd=0.0,
                requires_manual_confirmation=False,
            )

        if risk_mode == "aggressive":
            return self._evaluate_aggressive(
                source_size_usd=source_size_usd,
                wallet_score=wallet_score,
                wallet_win_rate=wallet_win_rate,
                wallet_profit_factor=wallet_profit_factor,
                wallet_avg_trade_size_usd=wallet_avg_trade_size_usd,
                wallet_current_exposure_usd=wallet_current_exposure_usd,
                portfolio=portfolio,
                high_conviction_boost_enabled=high_conviction_boost_enabled,
            )

        return self._evaluate_conservative(
            source_size_usd=source_size_usd,
            portfolio=portfolio,
            wallet_current_exposure_usd=wallet_current_exposure_usd,
        )

    def should_trigger_drawdown_stop(self, portfolio: PortfolioState, risk_mode: RiskMode) -> bool:
        return self._drawdown_stop_triggered(portfolio, risk_mode)

    def _evaluate_aggressive(
        self,
        *,
        source_size_usd: float,
        wallet_score: float,
        wallet_win_rate: float,
        wallet_profit_factor: float,
        wallet_avg_trade_size_usd: float,
        wallet_current_exposure_usd: float,
        portfolio: PortfolioState,
        high_conviction_boost_enabled: bool,
    ) -> RiskDecision:
        capital = max(portfolio.total_equity_usd, 1.0)

        per_wallet_cap = capital * settings.max_per_wallet_pct
        per_position_cap = capital * settings.max_per_position_pct
        total_exposure_cap = capital * settings.max_total_exposure_pct

        remaining_wallet_capacity = per_wallet_cap - wallet_current_exposure_usd
        remaining_total_capacity = total_exposure_cap - portfolio.exposure_usd
        if remaining_wallet_capacity <= 0:
            return RiskDecision(False, "wallet_exposure_limit", 0.0, False)
        if remaining_total_capacity <= 0:
            return RiskDecision(False, "total_exposure_limit", 0.0, False)

        wallet_multiplier = 0.8 + min(max(wallet_score, 0.0), 1.5) * 0.7
        if (
            high_conviction_boost_enabled
            and wallet_avg_trade_size_usd > 0
            and source_size_usd > (wallet_avg_trade_size_usd * 2)
        ):
            wallet_multiplier *= settings.high_conviction_multiplier

        kelly_fraction = self._light_kelly_fraction(
            win_rate=wallet_win_rate,
            profit_factor=wallet_profit_factor,
            multiplier=settings.kelly_multiplier,
        )

        proportional_leg = source_size_usd * wallet_multiplier
        kelly_leg = capital * kelly_fraction
        raw_target_size = proportional_leg + kelly_leg

        min_position_size = 1.5 if capital < 150 else 2.0
        target_size = min(
            raw_target_size,
            per_position_cap,
            remaining_wallet_capacity,
            remaining_total_capacity,
            portfolio.available_cash_usd,
        )

        if target_size < min_position_size:
            if min_position_size <= min(
                per_position_cap,
                remaining_wallet_capacity,
                remaining_total_capacity,
                portfolio.available_cash_usd,
            ):
                target_size = min_position_size
            else:
                return RiskDecision(False, "below_min_position_size", 0.0, False)

        requires_manual = target_size >= settings.manual_confirmation_usd
        return RiskDecision(
            allowed=True,
            reason="ok",
            target_size_usd=round(target_size, 2),
            requires_manual_confirmation=requires_manual,
            wallet_multiplier=round(wallet_multiplier, 4),
            kelly_fraction=round(kelly_fraction, 6),
        )

    def _evaluate_conservative(
        self,
        *,
        source_size_usd: float,
        portfolio: PortfolioState,
        wallet_current_exposure_usd: float,
    ) -> RiskDecision:
        capital = max(portfolio.total_equity_usd, 1.0)
        per_wallet_cap = capital * settings.conservative_max_per_wallet_pct
        per_position_cap = capital * settings.conservative_max_per_position_pct
        exposure_cap = capital * settings.conservative_max_total_exposure_pct

        remaining_wallet = per_wallet_cap - wallet_current_exposure_usd
        remaining_exposure = exposure_cap - portfolio.exposure_usd
        if remaining_wallet <= 0:
            return RiskDecision(False, "wallet_exposure_limit", 0.0, False)
        if remaining_exposure <= 0:
            return RiskDecision(False, "total_exposure_limit", 0.0, False)

        target_size = min(
            source_size_usd * 0.15,
            per_position_cap,
            remaining_wallet,
            remaining_exposure,
            portfolio.available_cash_usd,
        )

        if target_size < 2.0:
            return RiskDecision(False, "below_min_position_size", 0.0, False)

        return RiskDecision(
            allowed=True,
            reason="ok",
            target_size_usd=round(target_size, 2),
            requires_manual_confirmation=target_size >= settings.manual_confirmation_usd,
            wallet_multiplier=0.15,
            kelly_fraction=0.0,
        )

    @staticmethod
    def _light_kelly_fraction(*, win_rate: float, profit_factor: float, multiplier: float) -> float:
        p = min(max(win_rate, 0.0), 0.99)
        q = 1 - p
        b = min(max(profit_factor, 1.01), 4.0)

        kelly = max(((b * p) - q) / b, 0.0)
        # Light Kelly to reduce overbetting noise, then apply strategy multiplier.
        return min(kelly * 0.25 * multiplier, 0.22)

    @staticmethod
    def _drawdown_stop_triggered(portfolio: PortfolioState, risk_mode: RiskMode) -> bool:
        stop = settings.drawdown_stop_pct if risk_mode == "aggressive" else 0.12
        return portfolio.daily_drawdown_pct >= stop

    @staticmethod
    def _price_filter(price_cents: float) -> bool:
        return settings.price_min_cents <= price_cents <= settings.price_max_cents

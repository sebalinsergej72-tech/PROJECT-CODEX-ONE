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
    positions_value_usd: float = 0.0
    open_order_reserve_usd: float = 0.0
    reported_free_balance_usd: float = 0.0
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
        if settings.enable_drawdown_guards:
            if self._global_drawdown_triggered(portfolio, risk_mode):
                return RiskDecision(False, "global_drawdown_limit_reached", 0.0, False)

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
        if not settings.enable_drawdown_guards:
            return False
        return self._global_drawdown_triggered(portfolio, risk_mode) or self._drawdown_stop_triggered(portfolio, risk_mode)

    # SAFETY: safe aggressive fill
    def can_accept_slippage(
        self,
        *,
        source_price_cents: float,
        execution_price_cents: float,
        side: Literal["buy", "sell"],
        risk_mode: RiskMode,
    ) -> tuple[bool, float]:
        """Validate slippage against hard risk guardrails."""

        slippage_bps = self.compute_slippage_bps(
            source_price_cents=source_price_cents,
            execution_price_cents=execution_price_cents,
            side=side,
        )
        if risk_mode != "aggressive":
            # Conservative mode should not intentionally add slippage.
            return slippage_bps <= 0.0, slippage_bps
        return slippage_bps <= settings.max_allowed_slippage_bps, slippage_bps

    @staticmethod
    def compute_slippage_bps(
        *,
        source_price_cents: float,
        execution_price_cents: float,
        side: Literal["buy", "sell"],
    ) -> float:
        """Return positive bps for worse-than-source execution."""

        source = max(source_price_cents, 0.0001)
        execution = max(execution_price_cents, 0.0001)
        if side == "buy":
            return max(((execution - source) / source) * 10_000.0, 0.0)
        return max(((source - execution) / source) * 10_000.0, 0.0)

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
            return RiskDecision(
                False,
                (
                    "wallet_exposure_limit"
                    f":exposure={wallet_current_exposure_usd:.2f}"
                    f":cap={per_wallet_cap:.2f}"
                    f":cash={portfolio.available_cash_usd:.2f}"
                ),
                0.0,
                False,
            )
        if remaining_total_capacity <= 0:
            return RiskDecision(
                False,
                (
                    "total_exposure_limit"
                    f":exposure={portfolio.exposure_usd:.2f}"
                    f":cap={total_exposure_cap:.2f}"
                    f":cash={portfolio.available_cash_usd:.2f}"
                ),
                0.0,
                False,
            )

        wallet_multiplier = 0.8 + min(max(wallet_score, 0.0), 1.5) * 0.6
        if (
            high_conviction_boost_enabled
            and wallet_avg_trade_size_usd > 0
            and source_size_usd > (wallet_avg_trade_size_usd * 2)
        ):
            wallet_multiplier *= settings.high_conviction_multiplier

        # IMPROVED: Strict safety cap to ensure multiplier isn't blown up to dangerous levels
        wallet_multiplier = min(wallet_multiplier, settings.max_wallet_multiplier)

        kelly_fraction = self._light_kelly_fraction(
            win_rate=wallet_win_rate,
            profit_factor=wallet_profit_factor,
            multiplier=settings.kelly_multiplier,
        )

        proportional_leg = source_size_usd * wallet_multiplier
        kelly_leg = capital * kelly_fraction
        raw_target_size = proportional_leg + kelly_leg

        caps = [
            raw_target_size,
            per_position_cap,
            remaining_wallet_capacity,
            remaining_total_capacity,
        ]
        if not settings.ignore_available_cash_for_sizing:
            caps.append(portfolio.available_cash_usd)
        target_size = min(caps)

        if settings.enforce_min_trade_size and target_size < settings.min_trade_size_usd:
            return RiskDecision(False, "size_below_minimum", 0.0, False)

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
            return RiskDecision(
                False,
                (
                    "wallet_exposure_limit"
                    f":exposure={wallet_current_exposure_usd:.2f}"
                    f":cap={per_wallet_cap:.2f}"
                    f":cash={portfolio.available_cash_usd:.2f}"
                ),
                0.0,
                False,
            )
        if remaining_exposure <= 0:
            return RiskDecision(
                False,
                (
                    "total_exposure_limit"
                    f":exposure={portfolio.exposure_usd:.2f}"
                    f":cap={exposure_cap:.2f}"
                    f":cash={portfolio.available_cash_usd:.2f}"
                ),
                0.0,
                False,
            )

        target_size = min(
            source_size_usd * 0.15,
            per_position_cap,
            remaining_wallet,
            remaining_exposure,
        )
        if not settings.ignore_available_cash_for_sizing:
            target_size = min(target_size, portfolio.available_cash_usd)

        if settings.enforce_min_trade_size and target_size < settings.min_trade_size_usd:
            return RiskDecision(False, "size_below_minimum", 0.0, False)

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
        b = min(max(profit_factor, 1.01), 2.5)

        kelly = max(((b * p) - q) / b, 0.0)
        # Light Kelly to reduce overbetting noise, then apply strategy multiplier.
        return min(kelly * settings.kelly_fraction_scale * multiplier, settings.max_kelly_bet_pct)

    @staticmethod
    def _global_drawdown_triggered(portfolio: PortfolioState, risk_mode: RiskMode) -> bool:
        # IMPROVED: Prevent all trading if all-time capital falls below dangerous thresholds
        if portfolio.cumulative_pnl_usd >= 0:
            return False
            
        initial_equity = portfolio.total_equity_usd - portfolio.cumulative_pnl_usd
        if initial_equity <= 0:
            return False
            
        global_drawdown_pct = abs(portfolio.cumulative_pnl_usd) / initial_equity
        limit = 0.20 if risk_mode == "aggressive" else 0.15
        
        return global_drawdown_pct >= limit

    @staticmethod
    def _drawdown_stop_triggered(portfolio: PortfolioState, risk_mode: RiskMode) -> bool:
        stop = settings.drawdown_stop_pct if risk_mode == "aggressive" else 0.12
        return portfolio.daily_drawdown_pct >= stop

    @staticmethod
    def _price_filter(price_cents: float) -> bool:
        return settings.price_min_cents <= price_cents <= settings.price_max_cents

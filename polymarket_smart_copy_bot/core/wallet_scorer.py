from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from statistics import median

import yaml
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import RiskMode, settings
from data.polymarket_client import PolymarketClient, WalletTradeSignal
from models.models import BlacklistedWallet, WalletScore
from utils.helpers import WalletConfig, load_wallets, utc_now


@dataclass(slots=True)
class WalletMetrics:
    wallet: WalletConfig
    score: float
    win_rate: float
    roi_30d: float
    total_volume_30d: float
    trade_count_30d: int
    trade_count_90d: int
    total_volume_90d: float
    profit_factor: float
    avg_position_size: float
    qualified: bool
    reason: str
    # SAFETY: realistic thresholds — extra fields for protection
    account_age_days: int = 0
    consecutive_losses: int = 0


class WalletScorer:
    """Scores wallets and enforces strict qualification thresholds."""

    def __init__(self, polymarket_client: PolymarketClient) -> None:
        self.polymarket_client = polymarket_client

    async def refresh_scores(self, session: AsyncSession) -> int:
        wallets = load_wallets(settings.resolved_wallets_config_path)
        if not wallets:
            logger.warning("No wallets configured for scoring")
            return 0

        blocked = await self._build_blacklist(session)
        candidates: list[WalletMetrics] = []

        for wallet in wallets:
            if wallet.address in blocked:
                candidates.append(
                    WalletMetrics(
                        wallet=wallet,
                        score=0.0,
                        win_rate=0.0,
                        roi_30d=0.0,
                        total_volume_30d=0.0,
                        trade_count_30d=0,
                        trade_count_90d=0,
                        total_volume_90d=0.0,
                        profit_factor=0.0,
                        avg_position_size=0.0,
                        qualified=False,
                        reason="blacklisted",
                    )
                )
                continue

            signals = await self.polymarket_client.fetch_wallet_trades(wallet.address, limit=500)
            candidates.append(self._compute_metrics(wallet, signals))

        # Persist all metrics first.
        for metrics in candidates:
            await self._upsert_wallet_score(session, metrics)

        selected_addresses = self._select_wallets_by_mode(
            candidates, risk_mode=settings.risk_mode,
        )

        query = select(WalletScore).where(WalletScore.wallet_address.in_([m.wallet.address for m in candidates]))
        rows = (await session.execute(query)).scalars().all()
        for row in rows:
            row.qualified = row.wallet_address in selected_addresses
            if row.qualified and row.reason != "qualified":
                row.reason = f"selected_{settings.risk_mode}_mode"

        logger.info("Wallet scoring refresh completed: {} tracked", len(selected_addresses))
        return len(selected_addresses)

    async def get_qualified_wallets(self, session: AsyncSession, risk_mode: RiskMode) -> list[WalletScore]:
        limit = settings.max_wallets_aggressive if risk_mode == "aggressive" else settings.max_qualified_wallets
        query = (
            select(WalletScore)
            .where(WalletScore.qualified.is_(True))
            .order_by(WalletScore.score.desc())
            .limit(limit)
        )
        rows = (await session.execute(query)).scalars().all()
        return list(rows)

    async def _build_blacklist(self, session: AsyncSession) -> set[str]:
        query = select(BlacklistedWallet.wallet_address).where(BlacklistedWallet.active.is_(True))
        db_blocked = set((await session.execute(query)).scalars().all())

        yaml_blocked: set[str] = set()
        path = settings.resolved_wallets_config_path
        if path.exists():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            yaml_blocked = {str(x) for x in raw.get("blacklist", [])}

        return db_blocked | yaml_blocked

    @staticmethod
    def _compute_metrics(wallet: WalletConfig, signals: list[WalletTradeSignal]) -> WalletMetrics:
        now = utc_now()
        cutoff_30 = now - timedelta(days=30)
        cutoff_90 = now - timedelta(days=90)

        recent_30 = [signal for signal in signals if signal.traded_at >= cutoff_30]
        recent_90 = [signal for signal in signals if signal.traded_at >= cutoff_90]

        trade_count_30 = len(recent_30)
        trade_count_90 = len(recent_90)
        total_volume_30 = sum(signal.size_usd for signal in recent_30)
        total_volume_90 = sum(signal.size_usd for signal in recent_90)

        pnl_known_90 = [signal.profit_usd for signal in recent_90 if signal.profit_usd is not None]
        if pnl_known_90:
            wins = sum(1 for pnl in pnl_known_90 if pnl > 0)
            win_rate = wins / len(pnl_known_90)
            gross_profit = sum(p for p in pnl_known_90 if p > 0)
            gross_loss = abs(sum(p for p in pnl_known_90 if p < 0))
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else 2.5
        else:
            win_rate = 0.0
            profit_factor = 0.0

        # IMPROVED: Properly calculating active 30-day ROI to reward recent profitability
        total_pnl_30 = sum(p for p in (signal.profit_usd for signal in recent_30) if p is not None)
        roi_30d = total_pnl_30 / total_volume_30 if total_volume_30 > 0 else 0.0

        avg_position_size = total_volume_90 / trade_count_90 if trade_count_90 > 0 else 0.0

        # SAFETY: prevent lucky run wallets — compute account age from earliest signal
        if signals:
            earliest_trade = min(s.traded_at for s in signals)
            account_age_days = max(int((now - earliest_trade).days), 0)
        else:
            account_age_days = 0

        # SAFETY: prevent lucky run wallets — compute max consecutive losses
        consecutive_losses = WalletScorer._compute_consecutive_losses(recent_90)

        score = WalletScorer._score_wallet(
            win_rate=win_rate,
            trade_count_90d=trade_count_90,
            profit_factor=profit_factor,
            avg_position_size=avg_position_size,
            roi_30d=roi_30d,
            base_weight=wallet.base_weight,
        )

        qualified, reason = WalletScorer._qualified_reason(
            win_rate=win_rate,
            trade_count_30d=trade_count_30,
            trade_count_90d=trade_count_90,
            profit_factor=profit_factor,
            avg_position_size=avg_position_size,
            total_volume_90d=total_volume_90,
            account_age_days=account_age_days,
            consecutive_losses=consecutive_losses,
            score=score,
            pnl_values_90d=pnl_known_90,
        )

        return WalletMetrics(
            wallet=wallet,
            score=round(score, 6),
            win_rate=round(win_rate, 6),
            roi_30d=round(roi_30d, 6),
            total_volume_30d=round(total_volume_30, 2),
            trade_count_30d=trade_count_30,
            trade_count_90d=trade_count_90,
            total_volume_90d=round(total_volume_90, 2),
            profit_factor=round(profit_factor, 6),
            avg_position_size=round(avg_position_size, 2),
            qualified=qualified,
            reason=reason,
            account_age_days=account_age_days,
            consecutive_losses=consecutive_losses,
        )

    @staticmethod
    def _score_wallet(
        *,
        win_rate: float,
        trade_count_90d: int,
        profit_factor: float,
        avg_position_size: float,
        roi_30d: float,
        base_weight: float,
    ) -> float:
        # IMPROVED: Scaled to more realistic Polymarket performance bounds
        win_component = min(max((win_rate - 0.5) / 0.3, 0.0), 1.0)
        trade_component = min(trade_count_90d / 200, 1.0)
        pf_component = min(max((profit_factor - 1.0) / 1.0, 0.0), 1.0)
        size_component = min(avg_position_size / 1000, 1.0)
        
        # IMPROVED: Explicit 30-day ROI component (up to 50% ROI yields max component score)
        roi_component = min(max(roi_30d / 0.50, 0.0), 1.0)

        # IMPROVED: Re-balanced weights to sum exactly to 1.0
        raw = (
            0.30 * win_component
            + 0.30 * pf_component
            + 0.15 * trade_component
            + 0.15 * roi_component
            + 0.10 * size_component
        )
        return raw * base_weight

    @staticmethod
    def _qualified_reason(
        *,
        win_rate: float,
        trade_count_30d: int,
        trade_count_90d: int,
        profit_factor: float,
        avg_position_size: float,
        total_volume_90d: float,
        account_age_days: int,
        consecutive_losses: int,
        score: float,
        pnl_values_90d: list[float] | None = None,
    ) -> tuple[bool, str]:
        # SAFETY: realistic thresholds — balanced golden mean for Polymarket
        if win_rate < 0.58:
            return False, "win_rate_below_58pct"

        # SAFETY: prevent lucky run wallets — account must be at least 60 days old
        if account_age_days < 60:
            return False, f"account_too_young_{account_age_days}d"

        # IMPROVED: Strict protection against "one-hit wonders" (requires verified recent 30d activity)
        if trade_count_30d < 25:
            return False, "too_inactive_recently_30d"

        if trade_count_90d < 80:
            return False, "trade_count_below_80_90d"

        if profit_factor < 1.40:
            return False, "profit_factor_below_1_40"

        if avg_position_size <= 250:
            return False, "avg_position_below_250"

        # SAFETY: prevent lucky run wallets — high volume + low trade count = variance
        if total_volume_90d > 50_000 and trade_count_90d < 100:
            return False, "high_variance_lucky_run"

        # SAFETY: prevent lucky run wallets — max consecutive losses guard
        if consecutive_losses > 5:
            return False, f"consecutive_losses_{consecutive_losses}"

        # SAFETY: detect lottery traders — median(pnl) < 0 with positive mean
        if pnl_values_90d and len(pnl_values_90d) >= 10:
            median_pnl = median(pnl_values_90d)
            mean_pnl = sum(pnl_values_90d) / len(pnl_values_90d)
            if median_pnl < 0 and mean_pnl > 0:
                return False, "lottery_trader_median_negative"

        if score < 0.40:
            return False, "score_below_0_40"

        return True, "qualified"

    @staticmethod
    def _select_wallets_by_mode(metrics: list[WalletMetrics], risk_mode: RiskMode) -> set[str]:
        # IMPROVED: Safely separate and sort wallets to guarantee qualified ones get priority
        sorted_metrics = sorted(metrics, key=lambda m: m.score, reverse=True)

        qualified = [m for m in sorted_metrics if m.qualified]
        unqualified = [m for m in sorted_metrics if not m.qualified]

        if risk_mode == "aggressive":
            min_required = settings.min_wallets_aggressive
            max_allowed = settings.max_wallets_aggressive
        else:
            min_required = settings.min_qualified_wallets
            max_allowed = settings.max_qualified_wallets

        # 1. Take top qualified wallets up to the maximum limit
        selected = qualified[:max_allowed]

        # 2. If we haven't reached the minimum required, fallback to the top unqualified wallets
        #    SAFETY: realistic thresholds — never add wallets younger than 60 days as fallback
        shortfall = min_required - len(selected)
        if shortfall > 0:
            eligible_fallback = [m for m in unqualified if m.account_age_days >= 60]
            selected.extend(eligible_fallback[:shortfall])

        return {m.wallet.address for m in selected}

    @staticmethod
    def _compute_consecutive_losses(signals: list[WalletTradeSignal]) -> int:
        """Compute max consecutive losses from a list of signals (sorted by time)."""
        # SAFETY: prevent lucky run wallets
        sorted_signals = sorted(signals, key=lambda s: s.traded_at, reverse=True)
        max_consec = 0
        current_streak = 0
        for signal in sorted_signals:
            if signal.profit_usd is not None and signal.profit_usd < 0:
                current_streak += 1
                max_consec = max(max_consec, current_streak)
            else:
                current_streak = 0
        return max_consec

    async def _upsert_wallet_score(self, session: AsyncSession, metrics: WalletMetrics) -> WalletScore:
        query = select(WalletScore).where(WalletScore.wallet_address == metrics.wallet.address)
        row = (await session.execute(query)).scalar_one_or_none()

        if row is None:
            row = WalletScore(wallet_address=metrics.wallet.address)
            session.add(row)

        row.label = metrics.wallet.label
        row.score = metrics.score
        row.win_rate = metrics.win_rate
        row.roi_30d = metrics.roi_30d
        row.total_volume_30d = metrics.total_volume_30d
        row.trade_count_30d = metrics.trade_count_30d
        row.trade_count_90d = metrics.trade_count_90d
        row.total_volume_90d = metrics.total_volume_90d
        row.profit_factor = metrics.profit_factor
        row.avg_position_size = metrics.avg_position_size
        row.reason = metrics.reason
        row.updated_at = utc_now()

        return row

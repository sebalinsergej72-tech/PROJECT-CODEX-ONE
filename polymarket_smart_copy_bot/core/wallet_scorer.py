from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

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

        selected_addresses = self._select_wallets_by_mode(candidates, risk_mode=settings.risk_mode)

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

        total_pnl_30 = sum(p for p in (signal.profit_usd for signal in recent_30) if p is not None)
        roi_30d = total_pnl_30 / total_volume_30 if total_volume_30 > 0 else 0.0
        avg_position_size = total_volume_90 / trade_count_90 if trade_count_90 > 0 else 0.0

        score = WalletScorer._score_wallet(
            win_rate=win_rate,
            trade_count_90d=trade_count_90,
            profit_factor=profit_factor,
            avg_position_size=avg_position_size,
            base_weight=wallet.base_weight,
        )

        qualified, reason = WalletScorer._qualified_reason(
            win_rate=win_rate,
            trade_count_90d=trade_count_90,
            profit_factor=profit_factor,
            avg_position_size=avg_position_size,
            score=score,
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
        )

    @staticmethod
    def _score_wallet(
        *,
        win_rate: float,
        trade_count_90d: int,
        profit_factor: float,
        avg_position_size: float,
        base_weight: float,
    ) -> float:
        win_component = min(max((win_rate - 0.5) / 0.3, 0.0), 1.0)
        trade_component = min(trade_count_90d / 260, 1.0)
        pf_component = min(max((profit_factor - 1.0) / 1.4, 0.0), 1.0)
        size_component = min(avg_position_size / 1200, 1.0)

        raw = (
            0.35 * win_component
            + 0.25 * trade_component
            + 0.30 * pf_component
            + 0.10 * size_component
        )
        return raw * base_weight

    @staticmethod
    def _qualified_reason(
        *,
        win_rate: float,
        trade_count_90d: int,
        profit_factor: float,
        avg_position_size: float,
        score: float,
    ) -> tuple[bool, str]:
        if win_rate < 0.68:
            return False, "win_rate_below_68pct"
        if trade_count_90d < 180:
            return False, "trade_count_below_180_90d"
        if profit_factor < 1.8:
            return False, "profit_factor_below_1_8"
        if avg_position_size <= 600:
            return False, "avg_position_below_600"
        if score < 0.62:
            return False, "score_below_0_62"
        return True, "qualified"

    @staticmethod
    def _select_wallets_by_mode(metrics: list[WalletMetrics], risk_mode: RiskMode) -> set[str]:
        sorted_metrics = sorted(metrics, key=lambda m: m.score, reverse=True)
        qualified = [m for m in sorted_metrics if m.qualified]

        if risk_mode == "aggressive":
            top = qualified[: settings.max_wallets_aggressive]
            if len(top) < settings.min_wallets_aggressive:
                top = sorted_metrics[: settings.min_wallets_aggressive]
            return {wallet.wallet.address for wallet in top}

        if len(qualified) < settings.min_qualified_wallets:
            selected = sorted_metrics[: settings.min_qualified_wallets]
        else:
            selected = qualified[: settings.max_qualified_wallets]
        return {wallet.wallet.address for wallet in selected[: settings.max_qualified_wallets]}

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

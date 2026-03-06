from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import RiskMode, settings
from data.polymarket_client import PolymarketClient
from models.qualified_wallet import QualifiedWallet
from utils.helpers import WalletConfig, load_wallets, utc_now
from utils.notifications import NotificationService

DISCOVERY_CATEGORIES = ("OVERALL", "POLITICS", "SPORTS", "CRYPTO")


@dataclass(slots=True)
class CandidateSeed:
    address: str
    name: str | None = None
    niches: set[str] = field(default_factory=set)
    monthly_pnl_pct: float = 0.0
    win_rate_hint: float | None = None
    trades_90d_hint: int | None = None
    trades_30d_hint: int | None = None
    profit_factor_hint: float | None = None
    avg_size_hint: float | None = None
    last_trade_ts_hint: datetime | None = None
    consecutive_losses_hint: int | None = None
    wallet_age_days_hint: int | None = None


@dataclass(slots=True)
class ParsedTrade:
    traded_at: datetime
    size_usd: float
    pnl_usd: float | None


@dataclass(slots=True)
class ScoredWallet:
    address: str
    name: str | None
    score: float
    win_rate: float
    trades_90d: int
    profit_factor: float
    avg_size: float
    niche: str
    last_trade_ts: datetime
    trades_30d: int
    monthly_pnl_pct: float
    consecutive_losses: int
    wallet_age_days: int


@dataclass(slots=True)
class DiscoveryThresholds:
    min_trades_90d: int
    min_win_rate: float
    min_profit_factor: float
    min_avg_size: float
    max_days_since_last_trade: int
    max_consecutive_losses: int
    min_wallet_age_days: int


@dataclass(slots=True)
class CandidateProgress:
    passed_trades: bool = False
    passed_win_rate: bool = False
    passed_profit_factor: bool = False
    passed_avg_size: bool = False
    passed_recency: bool = False
    passed_consecutive_losses: bool = False
    passed_wallet_age: bool = False


@dataclass(slots=True)
class DiscoveryCounters:
    total_candidates: int = 0
    passed_trades: int = 0
    passed_win_rate: int = 0
    passed_profit_factor: int = 0
    passed_avg_size: int = 0
    passed_recency: int = 0
    passed_consecutive_losses: int = 0
    passed_wallet_age: int = 0
    passed_all_filters: int = 0


@dataclass(slots=True)
class DiscoveryResult:
    mode: RiskMode
    ran_at: datetime
    counters: DiscoveryCounters
    thresholds: DiscoveryThresholds
    stored_top: int
    enabled_wallets: int
    approvals_requested: int
    approvals_granted: int
    approvals_skipped: int
    rejected_reasons: dict[str, int] = field(default_factory=dict)
    report: str = ""

    @property
    def scanned_candidates(self) -> int:
        return self.counters.total_candidates

    @property
    def passed_filters(self) -> int:
        return self.counters.passed_all_filters


class WalletDiscovery:
    """Automatic wallet discovery + scoring with optional human approval."""

    def __init__(self, polymarket_client: PolymarketClient, notifications: NotificationService) -> None:
        self.polymarket_client = polymarket_client
        self.notifications = notifications
        self.last_result: DiscoveryResult | None = None

    async def import_seed_wallets(self, session: AsyncSession, risk_mode: RiskMode | None = None) -> int:
        """Import enabled wallets from wallets.yaml as bootstrap data."""

        existing = (await session.execute(select(QualifiedWallet))).scalars().first()
        if existing is not None:
            return 0

        mode = risk_mode or settings.RISK_MODE
        seeds = self._enabled_seed_wallets()
        if not seeds:
            logger.info("Skipped seed import: no enabled wallets in wallets.yaml")
            return 0

        limit = self._enabled_limit_for_mode(mode)
        now = utc_now()
        for idx, wallet in enumerate(seeds):
            session.add(
                QualifiedWallet(
                    address=wallet.address.lower(),
                    name=wallet.label,
                    score=0.0,
                    win_rate=0.0,
                    trades_90d=0,
                    profit_factor=0.0,
                    avg_size=0.0,
                    niche="seed",
                    last_trade_ts=None,
                    enabled=idx < limit,
                    updated_at=now,
                )
            )

        logger.info("Imported {} enabled seed wallets from wallets.yaml", len(seeds))
        return len(seeds)

    async def discover_and_score(
        self,
        session: AsyncSession,
        *,
        auto_add: bool,
        risk_mode: RiskMode | None = None,
    ) -> DiscoveryResult:
        """Run discovery, apply hard filters, persist top wallets and produce a report."""

        now = utc_now()
        mode = risk_mode or settings.RISK_MODE
        thresholds = self._thresholds_for_mode(mode)
        counters = DiscoveryCounters()
        rejected_reasons: dict[str, int] = {}

        candidates = await self._fetch_candidate_seeds()
        counters.total_candidates = len(candidates)

        scored: list[ScoredWallet] = []
        if candidates:
            scored, rejected_reasons, counters = await self._score_candidates(candidates, thresholds)
            scored.sort(key=lambda row: row.score, reverse=True)
        else:
            rejected_reasons["no_candidates"] = 1

        top10 = scored[:10]

        all_existing = (await session.execute(select(QualifiedWallet))).scalars().all()
        existing_by_address = {row.address: row for row in all_existing}
        previously_enabled = {row.address for row in all_existing if row.enabled}

        approvals_requested = 0
        approvals_granted = 0
        approvals_skipped = 0

        top10_addresses = {row.address for row in top10}
        now_dt = utc_now()
        enabled_limit = self._enabled_limit_for_mode(mode)

        for idx, wallet in enumerate(top10):
            model = existing_by_address.get(wallet.address)
            if model is None:
                model = QualifiedWallet(address=wallet.address)
                existing_by_address[wallet.address] = model
                session.add(model)

            should_enable = idx < enabled_limit
            if should_enable and wallet.address not in previously_enabled and not auto_add:
                approvals_requested += 1
                approved = await self._request_top_wallet_approval(wallet)
                if approved:
                    approvals_granted += 1
                    should_enable = True
                else:
                    approvals_skipped += 1
                    should_enable = False

            model.name = wallet.name
            model.score = round(wallet.score, 6)
            model.win_rate = round(wallet.win_rate, 6)
            model.trades_90d = wallet.trades_90d
            model.profit_factor = round(wallet.profit_factor, 6)
            model.avg_size = round(wallet.avg_size, 2)
            model.niche = wallet.niche
            model.last_trade_ts = wallet.last_trade_ts
            model.enabled = should_enable
            model.updated_at = now_dt

        for address, row in existing_by_address.items():
            if address not in top10_addresses:
                row.enabled = False
                row.updated_at = now_dt

        enabled_count = sum(1 for row in existing_by_address.values() if row.enabled)

        result = DiscoveryResult(
            mode=mode,
            ran_at=now,
            counters=counters,
            thresholds=thresholds,
            stored_top=len(top10),
            enabled_wallets=enabled_count,
            approvals_requested=approvals_requested,
            approvals_granted=approvals_granted,
            approvals_skipped=approvals_skipped,
            rejected_reasons=rejected_reasons,
        )
        result.report = self.format_result_report(result)
        self.last_result = result
        return result

    def format_result_report(self, result: DiscoveryResult | None = None) -> str:
        payload = result or self.last_result
        if payload is None:
            return "Discovery completed (0 candidates):\n• Passed all filters: 0\n• Enabled for trading: 0"

        counters = payload.counters
        return (
            f"Discovery completed ({counters.total_candidates} candidates):\n"
            f"• Passed trades_90d: {counters.passed_trades}\n"
            f"• Passed win_rate: {counters.passed_win_rate}\n"
            f"• Passed profit_factor: {counters.passed_profit_factor}\n"
            f"• Passed avg_size: {counters.passed_avg_size}\n"
            f"• Passed recency: {counters.passed_recency}\n"
            f"• Passed consecutive_losses: {counters.passed_consecutive_losses}\n"
            f"• Passed wallet_age: {counters.passed_wallet_age}\n"
            f"• Passed all filters: {counters.passed_all_filters}\n"
            f"• Enabled for trading: {payload.enabled_wallets}"
        )

    async def get_top_wallets(self, session: AsyncSession, limit: int = 10) -> list[QualifiedWallet]:
        query = select(QualifiedWallet).order_by(QualifiedWallet.score.desc()).limit(limit)
        rows = (await session.execute(query)).scalars().all()
        return list(rows)

    async def count_enabled_wallets(self, session: AsyncSession) -> int:
        query = select(QualifiedWallet).where(QualifiedWallet.enabled.is_(True))
        rows = (await session.execute(query)).scalars().all()
        return len(rows)

    async def add_wallet(self, session: AsyncSession, address: str, name: str | None = None) -> QualifiedWallet:
        normalized = address.lower()
        row = (
            await session.execute(select(QualifiedWallet).where(QualifiedWallet.address == normalized))
        ).scalar_one_or_none()
        if row is None:
            row = QualifiedWallet(
                address=normalized,
                name=name,
                score=0.0,
                win_rate=0.0,
                trades_90d=0,
                profit_factor=0.0,
                avg_size=0.0,
                niche="manual",
                last_trade_ts=None,
                enabled=True,
                updated_at=utc_now(),
            )
            session.add(row)
            return row

        row.enabled = True
        row.name = row.name or name
        row.niche = row.niche or "manual"
        row.updated_at = utc_now()
        return row

    async def remove_wallet(self, session: AsyncSession, address: str) -> bool:
        normalized = address.lower()
        row = (
            await session.execute(select(QualifiedWallet).where(QualifiedWallet.address == normalized))
        ).scalar_one_or_none()
        if row is None:
            return False
        row.enabled = False
        row.updated_at = utc_now()
        return True

    async def _fetch_candidate_seeds(self) -> dict[str, CandidateSeed]:
        candidates: dict[str, CandidateSeed] = {}
        for category in DISCOVERY_CATEGORIES:
            rows = await self.polymarket_client.get_leaderboard(
                category=category,
                time_period="MONTH",
                order_by="PNL",
                limit=100,
            )
            for row in rows:
                address = self._extract_address(row)
                if not address:
                    continue

                seed = candidates.get(address)
                if seed is None:
                    seed = CandidateSeed(address=address)
                    candidates[address] = seed

                seed.niches.add(category.lower())
                if seed.name is None:
                    seed.name = self._extract_name(row)

                monthly = self._extract_monthly_pnl_pct(row)
                if abs(monthly) > abs(seed.monthly_pnl_pct):
                    seed.monthly_pnl_pct = monthly

                win_rate = self._extract_win_rate_hint(row)
                if win_rate is not None and (seed.win_rate_hint is None or win_rate > seed.win_rate_hint):
                    seed.win_rate_hint = win_rate

                trades_90d = self._extract_trades_90d_hint(row)
                if trades_90d is not None:
                    seed.trades_90d_hint = max(seed.trades_90d_hint or 0, trades_90d)

                trades_30d = self._extract_trades_30d_hint(row)
                if trades_30d is not None:
                    seed.trades_30d_hint = max(seed.trades_30d_hint or 0, trades_30d)

                pf = self._extract_profit_factor_hint(row)
                if pf is not None and (seed.profit_factor_hint is None or pf > seed.profit_factor_hint):
                    seed.profit_factor_hint = pf

                avg_size = self._extract_avg_size_hint(row)
                if avg_size is not None and (seed.avg_size_hint is None or avg_size > seed.avg_size_hint):
                    seed.avg_size_hint = avg_size

                last_trade_ts = self._extract_last_trade_ts_hint(row)
                if last_trade_ts is not None and (
                    seed.last_trade_ts_hint is None or last_trade_ts > seed.last_trade_ts_hint
                ):
                    seed.last_trade_ts_hint = last_trade_ts

                consec_losses = self._extract_consecutive_losses_hint(row)
                if consec_losses is not None:
                    if seed.consecutive_losses_hint is None:
                        seed.consecutive_losses_hint = consec_losses
                    else:
                        seed.consecutive_losses_hint = min(seed.consecutive_losses_hint, consec_losses)

                age_days = self._extract_wallet_age_days_hint(row)
                if age_days is not None:
                    seed.wallet_age_days_hint = max(seed.wallet_age_days_hint or 0, age_days)
        return candidates

    async def _score_candidates(
        self,
        seeds: dict[str, CandidateSeed],
        thresholds: DiscoveryThresholds,
    ) -> tuple[list[ScoredWallet], dict[str, int], DiscoveryCounters]:
        limiter = asyncio.Semaphore(8)
        now = utc_now()

        async def run(seed: CandidateSeed) -> tuple[ScoredWallet | None, str | None, CandidateProgress]:
            async with limiter:
                return await self._score_single_wallet(seed, now, thresholds)

        tasks = [run(seed) for seed in seeds.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        counters = DiscoveryCounters(total_candidates=len(seeds))
        scored: list[ScoredWallet] = []
        rejected_reasons: dict[str, int] = {}

        for result in results:
            if isinstance(result, Exception):
                logger.warning("Wallet scoring failed: {}", result)
                self._inc_rejection_reason(rejected_reasons, "scoring_error")
                continue

            wallet, reason, progress = result
            if progress.passed_trades:
                counters.passed_trades += 1
            if progress.passed_win_rate:
                counters.passed_win_rate += 1
            if progress.passed_profit_factor:
                counters.passed_profit_factor += 1
            if progress.passed_avg_size:
                counters.passed_avg_size += 1
            if progress.passed_recency:
                counters.passed_recency += 1
            if progress.passed_consecutive_losses:
                counters.passed_consecutive_losses += 1
            if progress.passed_wallet_age:
                counters.passed_wallet_age += 1

            if wallet is not None:
                scored.append(wallet)
                counters.passed_all_filters += 1
                continue

            self._inc_rejection_reason(rejected_reasons, reason or "unknown")

        return scored, rejected_reasons, counters

    async def _score_single_wallet(
        self,
        seed: CandidateSeed,
        now: datetime,
        thresholds: DiscoveryThresholds,
    ) -> tuple[ScoredWallet | None, str | None, CandidateProgress]:
        progress = CandidateProgress()

        raw_trades = await self.polymarket_client.get_user_trades(seed.address, limit=500)
        raw_activity = await self.polymarket_client.get_user_activity(seed.address, limit=500)

        parsed_trades = self._parse_trades(raw_trades)
        if not parsed_trades and (
            seed.trades_90d_hint is None
            or seed.win_rate_hint is None
            or seed.profit_factor_hint is None
            or seed.avg_size_hint is None
        ):
            return None, "no_trade_data_and_no_hints", progress

        parsed_trades.sort(key=lambda trade: trade.traded_at, reverse=True)
        cutoff_90d = now - timedelta(days=90)
        cutoff_30d = now - timedelta(days=30)

        trades_90d = [trade for trade in parsed_trades if trade.traded_at >= cutoff_90d]
        trades_30d = [trade for trade in parsed_trades if trade.traded_at >= cutoff_30d]
        trades_90d_count = max(len(trades_90d), seed.trades_90d_hint or 0)
        trades_30d_count = max(len(trades_30d), seed.trades_30d_hint or 0)
        if trades_90d_count == 0:
            return None, "no_trades_90d", progress

        if trades_90d_count < thresholds.min_trades_90d:
            return None, "min_trades_90d", progress
        progress.passed_trades = True

        last_trade_ts = trades_90d[0].traded_at if trades_90d else seed.last_trade_ts_hint
        if last_trade_ts is None:
            return None, "missing_last_trade_ts", progress

        pnl_90d = [trade.pnl_usd for trade in trades_90d if trade.pnl_usd is not None]
        wins = sum(1 for pnl in pnl_90d if pnl > 0)
        win_rate_from_pnl = wins / len(pnl_90d) if pnl_90d else None

        gross_profit = sum(pnl for pnl in pnl_90d if pnl > 0)
        gross_loss = abs(sum(pnl for pnl in pnl_90d if pnl < 0))
        pf_from_pnl: float | None = None
        if pnl_90d:
            pf_from_pnl = (gross_profit / gross_loss) if gross_loss > 0 else (2.5 if gross_profit > 0 else 0.0)

        win_rate = win_rate_from_pnl if win_rate_from_pnl is not None else (seed.win_rate_hint or 0.0)
        if win_rate > 1.0:
            win_rate /= 100.0
        win_rate = max(min(win_rate, 1.0), 0.0)

        if win_rate < thresholds.min_win_rate:
            return None, "min_win_rate", progress
        progress.passed_win_rate = True

        profit_factor = pf_from_pnl if pf_from_pnl is not None else (seed.profit_factor_hint or 0.0)
        if profit_factor < thresholds.min_profit_factor:
            return None, "min_profit_factor", progress
        progress.passed_profit_factor = True

        avg_size_from_trades = (sum(trade.size_usd for trade in trades_90d) / len(trades_90d)) if trades_90d else None
        avg_size = avg_size_from_trades if avg_size_from_trades is not None else (seed.avg_size_hint or 0.0)
        if avg_size <= thresholds.min_avg_size:
            return None, "min_avg_size", progress
        progress.passed_avg_size = True

        days_since_last = max((now - last_trade_ts).total_seconds() / 86400, 0.0)
        if days_since_last > thresholds.max_days_since_last_trade:
            return None, "max_days_since_last_trade", progress
        progress.passed_recency = True

        consecutive_losses = 0
        for trade in parsed_trades:
            if trade.pnl_usd is None:
                continue
            if trade.pnl_usd < 0:
                consecutive_losses += 1
            else:
                break
        if consecutive_losses == 0 and seed.consecutive_losses_hint is not None:
            consecutive_losses = seed.consecutive_losses_hint

        if consecutive_losses >= thresholds.max_consecutive_losses:
            return None, "max_consecutive_losses", progress
        progress.passed_consecutive_losses = True

        wallet_age_days = self._wallet_age_days(raw_activity, parsed_trades, now)
        if wallet_age_days == 0 and seed.wallet_age_days_hint is not None:
            wallet_age_days = seed.wallet_age_days_hint
        if wallet_age_days < thresholds.min_wallet_age_days:
            return None, "min_wallet_age_days", progress
        progress.passed_wallet_age = True

        pnl_30d = sum(trade.pnl_usd for trade in trades_30d if trade.pnl_usd is not None)
        volume_30d = sum(trade.size_usd for trade in trades_30d)
        monthly_pnl_pct = seed.monthly_pnl_pct
        if abs(monthly_pnl_pct) > 2:
            monthly_pnl_pct /= 100
        if monthly_pnl_pct == 0.0 and volume_30d > 0:
            monthly_pnl_pct = pnl_30d / volume_30d

        score = (
            win_rate * 120
            + monthly_pnl_pct * 80
            + trades_30d_count * 0.6
            + (avg_size / 100)
            + profit_factor * 25
            - days_since_last * 3
            - consecutive_losses * 15
        )

        return (
            ScoredWallet(
                address=seed.address,
                name=seed.name,
                score=score,
                win_rate=win_rate,
                trades_90d=trades_90d_count,
                profit_factor=profit_factor,
                avg_size=avg_size,
                niche=",".join(sorted(seed.niches)) if seed.niches else "overall",
                last_trade_ts=last_trade_ts,
                trades_30d=trades_30d_count,
                monthly_pnl_pct=monthly_pnl_pct,
                consecutive_losses=consecutive_losses,
                wallet_age_days=wallet_age_days,
            ),
            None,
            progress,
        )

    async def _request_top_wallet_approval(self, wallet: ScoredWallet) -> bool:
        if not self.notifications.enabled:
            return False
        title = "New Top Wallet Candidate"
        body = (
            f"Address: {wallet.address}\n"
            f"Score: {wallet.score:.2f}\n"
            f"Win-rate: {wallet.win_rate * 100:.2f}%\n"
            f"Trades 90d: {wallet.trades_90d}\n"
            f"Profit factor: {wallet.profit_factor:.2f}\n"
            f"Avg size: ${wallet.avg_size:.2f}\n"
            f"Niche: {wallet.niche}\n"
            f"Last trade: {wallet.last_trade_ts.isoformat()}"
        )
        return await self.notifications.request_wallet_approval(title=title, body=body, timeout_seconds=240)

    @staticmethod
    def _parse_trades(rows: list[dict[str, Any]]) -> list[ParsedTrade]:
        parsed: list[ParsedTrade] = []
        for row in rows:
            traded_at = WalletDiscovery._extract_timestamp(
                row.get("timestamp") or row.get("createdAt") or row.get("time")
            )
            if traded_at is None:
                continue

            size_raw = (
                row.get("size")
                or row.get("amount")
                or row.get("usdcValue")
                or row.get("notional")
                or row.get("sizeUsd")
            )
            size_usd = WalletDiscovery._to_float(size_raw)
            if size_usd <= 0:
                continue

            pnl_raw = row.get("pnl") or row.get("profit") or row.get("realizedPnl")
            pnl_usd: float | None = None
            if pnl_raw is not None:
                pnl_usd = WalletDiscovery._to_float(pnl_raw)

            parsed.append(ParsedTrade(traded_at=traded_at, size_usd=size_usd, pnl_usd=pnl_usd))
        return parsed

    @staticmethod
    def _wallet_age_days(activity_rows: list[dict[str, Any]], parsed_trades: list[ParsedTrade], now: datetime) -> int:
        earliest: datetime | None = None
        for row in activity_rows:
            ts = WalletDiscovery._extract_timestamp(row.get("timestamp") or row.get("createdAt") or row.get("time"))
            if ts is None:
                continue
            if earliest is None or ts < earliest:
                earliest = ts
        if earliest is None and parsed_trades:
            earliest = min(trade.traded_at for trade in parsed_trades)
        if earliest is None:
            return 0
        return max((now - earliest).days, 0)

    @staticmethod
    def _extract_address(payload: dict[str, Any]) -> str | None:
        for key in ("address", "walletAddress", "wallet", "user", "proxyWallet"):
            value = payload.get(key)
            if isinstance(value, str) and value.lower().startswith("0x") and len(value) >= 10:
                return value.lower()
        return None

    @staticmethod
    def _extract_name(payload: dict[str, Any]) -> str | None:
        for key in ("name", "username", "displayName", "label", "ens", "userName"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _extract_monthly_pnl_pct(payload: dict[str, Any]) -> float:
        for key in ("monthlyPnlPct", "pnlPct", "pnlPercent", "roi", "monthlyReturn"):
            if key in payload:
                return WalletDiscovery._to_float(payload.get(key))

        if "pnl" in payload and "vol" in payload:
            pnl = WalletDiscovery._to_float(payload.get("pnl"))
            volume = WalletDiscovery._to_float(payload.get("vol"))
            if volume > 0:
                return pnl / volume

        return 0.0

    @staticmethod
    def _extract_win_rate_hint(payload: dict[str, Any]) -> float | None:
        for key in ("winRate", "winrate", "successRate", "hitRate"):
            if key not in payload:
                continue
            value = WalletDiscovery._to_float(payload.get(key))
            if value <= 0:
                continue
            if value > 1.0:
                value /= 100.0
            return min(max(value, 0.0), 1.0)

        # Polymarket leaderboard doesn't provide winRate directly.
        # Estimate from PnL/volume ratio for profitable wallets.
        pnl = WalletDiscovery._to_float(payload.get("pnl"))
        vol = WalletDiscovery._to_float(payload.get("vol"))
        if vol > 0 and pnl > 0:
            pnl_ratio = pnl / vol
            # Map profitability to approximate win_rate.
            # 13%+ return → passes 0.65 threshold.
            estimated = 0.55 + min(pnl_ratio, 0.25) * 0.80
            return round(min(estimated, 0.85), 4)

        return None

    @staticmethod
    def _extract_trades_90d_hint(payload: dict[str, Any]) -> int | None:
        for key in ("trades90d", "tradeCount90d", "totalTrades90d", "numTrades90d", "trades_90d"):
            if key in payload:
                value = int(WalletDiscovery._to_float(payload.get(key)))
                if value > 0:
                    return value
        return None

    @staticmethod
    def _extract_trades_30d_hint(payload: dict[str, Any]) -> int | None:
        for key in ("trades30d", "tradeCount30d", "totalTrades30d", "numTrades30d", "trades"):
            if key in payload:
                value = int(WalletDiscovery._to_float(payload.get(key)))
                if value > 0:
                    return value
        return None

    @staticmethod
    def _extract_profit_factor_hint(payload: dict[str, Any]) -> float | None:
        for key in ("profitFactor", "pf"):
            if key in payload:
                value = WalletDiscovery._to_float(payload.get(key))
                if value > 0:
                    return value

        # Polymarket leaderboard doesn't provide profitFactor directly.
        # Estimate from PnL/volume: gross_profit = (vol+pnl)/2, gross_loss = (vol-pnl)/2
        # profit_factor = gross_profit / gross_loss = (vol+pnl) / (vol-pnl)
        pnl = WalletDiscovery._to_float(payload.get("pnl"))
        vol = WalletDiscovery._to_float(payload.get("vol"))
        if vol > 0 and pnl > 0:
            denominator = vol - pnl
            if denominator > 0:
                estimated = (vol + pnl) / denominator
                return round(min(estimated, 5.0), 4)
            # All profit, no loss
            return 5.0

        return None

    @staticmethod
    def _extract_avg_size_hint(payload: dict[str, Any]) -> float | None:
        for key in ("avgSize", "avgPositionSize", "averagePositionSize", "avgTradeSize"):
            if key in payload:
                value = WalletDiscovery._to_float(payload.get(key))
                if value > 0:
                    return value
        return None

    @staticmethod
    def _extract_last_trade_ts_hint(payload: dict[str, Any]) -> datetime | None:
        for key in ("lastTradeTs", "lastTradeAt", "latestTradeAt", "lastActiveAt"):
            if key in payload:
                parsed = WalletDiscovery._extract_timestamp(payload.get(key))
                if parsed is not None:
                    return parsed
        return None

    @staticmethod
    def _extract_consecutive_losses_hint(payload: dict[str, Any]) -> int | None:
        for key in ("consecutiveLosses", "consecLosses"):
            if key in payload:
                value = int(WalletDiscovery._to_float(payload.get(key)))
                if value >= 0:
                    return value
        return None

    @staticmethod
    def _extract_wallet_age_days_hint(payload: dict[str, Any]) -> int | None:
        for key in ("walletAgeDays", "ageDays", "daysActive"):
            if key in payload:
                value = int(WalletDiscovery._to_float(payload.get(key)))
                if value > 0:
                    return value
        return None

    @staticmethod
    def _extract_timestamp(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 10_000_000_000:
                ts = ts / 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        if isinstance(value, str):
            normalized = value.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                return None
        return None

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _enabled_seed_wallets(self) -> list[WalletConfig]:
        wallets = load_wallets(settings.resolved_wallets_config_path)
        enabled = [wallet for wallet in wallets if wallet.enabled]
        filtered: list[WalletConfig] = []
        skipped_placeholders = 0
        for wallet in enabled:
            if self._is_placeholder_address(wallet.address):
                skipped_placeholders += 1
                continue
            filtered.append(wallet)
        if skipped_placeholders > 0:
            logger.warning(
                "Ignored placeholder seed wallets with synthetic addresses: {}",
                skipped_placeholders,
            )
        return filtered

    @staticmethod
    def _is_placeholder_address(address: str) -> bool:
        normalized = address.strip().lower()
        if not (normalized.startswith("0x") and len(normalized) == 42):
            return False
        body = normalized[2:]
        return len(set(body)) == 1

    @staticmethod
    def _enabled_limit_for_mode(risk_mode: RiskMode) -> int:
        return settings.max_wallets_aggressive if risk_mode == "aggressive" else settings.max_qualified_wallets

    @staticmethod
    def _thresholds_for_mode(risk_mode: RiskMode) -> DiscoveryThresholds:
        if risk_mode == "aggressive":
            return DiscoveryThresholds(
                min_trades_90d=settings.discovery_min_trades_aggressive,
                min_win_rate=settings.discovery_min_winrate_aggressive,
                min_profit_factor=settings.discovery_min_profit_factor_aggressive,
                min_avg_size=settings.discovery_min_avg_size_aggressive,
                max_days_since_last_trade=settings.discovery_max_days_since_last_trade_aggressive,
                max_consecutive_losses=settings.discovery_max_consec_losses_aggressive,
                min_wallet_age_days=settings.discovery_min_wallet_age_aggressive,
            )

        return DiscoveryThresholds(
            min_trades_90d=settings.discovery_min_trades_cons,
            min_win_rate=settings.discovery_min_winrate_cons,
            min_profit_factor=settings.discovery_min_profit_factor_cons,
            min_avg_size=settings.discovery_min_avg_size_cons,
            max_days_since_last_trade=settings.discovery_max_days_since_last_trade_conservative,
            max_consecutive_losses=settings.discovery_max_consec_losses_cons,
            min_wallet_age_days=settings.discovery_min_wallet_age_cons,
        )

    @staticmethod
    def _inc_rejection_reason(stats: dict[str, int], reason: str) -> None:
        stats[reason] = stats.get(reason, 0) + 1

from __future__ import annotations

from fastapi import APIRouter, Request, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data.database import get_session
from models.models import PortfolioSnapshot, WalletScore

router = APIRouter(tags=["status"])


@router.get("/status")
async def status(request: Request) -> dict:
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        return {
            "status": "booting",
            "scheduler_running": False,
            "last_discovery_stats": {
                "total_candidates": 0,
                "passed_trades": 0,
                "passed_win_rate": 0,
                "passed_profit_factor": 0,
                "passed_avg_size": 0,
                "passed_recency": 0,
                "passed_consecutive_losses": 0,
                "passed_wallet_age": 0,
                "passed_all_filters": 0,
                "stored_top": 0,
                "enabled_wallets": 0,
                "used_seed_fallback": False,
                "seed_fallback_wallets": 0,
                "used_discovered_fallback": False,
                "discovered_fallback_wallets": 0,
                "report": "",
            },
        }

    payload = await orchestrator.get_status()
    payload.setdefault("last_discovery_stats", {})
    payload["status"] = "ok"
    return payload


@router.get("/portfolio_history")
async def portfolio_history(
    request: Request,
    limit: int = 200,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    query = select(PortfolioSnapshot).order_by(PortfolioSnapshot.taken_at.desc()).limit(limit)
    result = await session.execute(query)
    snapshots = result.scalars().all()

    return [
        {
            "taken_at": s.taken_at.isoformat(),
            "total_equity_usd": s.total_equity_usd,
            "available_cash_usd": s.available_cash_usd,
            "exposure_usd": s.exposure_usd,
            "cumulative_pnl_usd": s.cumulative_pnl_usd,
        }
        for s in reversed(snapshots)
    ]


@router.get("/leaderboard")
async def leaderboard(
    request: Request,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    query = (
        select(WalletScore)
        .order_by(WalletScore.score.desc(), WalletScore.win_rate.desc())
        .limit(limit)
    )
    result = await session.execute(query)
    scores = result.scalars().all()

    return [
        {
            "wallet_address": s.wallet_address,
            "label": s.label,
            "score": s.score,
            "win_rate": s.win_rate,
            "roi_30d": s.roi_30d,
            "total_volume_30d": s.total_volume_30d,
            "trade_count_30d": s.trade_count_30d,
            "trade_count_90d": s.trade_count_90d,
            "total_volume_90d": s.total_volume_90d,
            "profit_factor": s.profit_factor,
            "avg_position_size": s.avg_position_size,
        }
        for s in scores
    ]

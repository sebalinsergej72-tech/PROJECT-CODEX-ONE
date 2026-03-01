from __future__ import annotations

from fastapi import APIRouter, Request

from utils.helpers import format_uptime

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict:
    orchestrator = getattr(request.app.state, "orchestrator", None)
    uptime = format_uptime(getattr(request.app.state, "started_at", None))
    tracked_wallets = 0
    if orchestrator is not None:
        tracked_wallets = orchestrator.tracked_wallets_count

    return {
        "status": "healthy",
        "uptime": uptime,
        "tracked_wallets": tracked_wallets,
    }

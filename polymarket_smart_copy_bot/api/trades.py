from __future__ import annotations

from fastapi import APIRouter, Query, Request

router = APIRouter(tags=["trades"])


@router.get("/trades")
async def trades(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        return {"status": "booting", "count": 0, "trades": []}

    rows = await orchestrator.get_recent_trades(limit=limit)
    return {
        "status": "ok",
        "count": len(rows),
        "trades": rows,
    }

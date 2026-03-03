from __future__ import annotations

from fastapi import APIRouter, Query, Request

router = APIRouter(tags=["positions"])


@router.get("/positions")
async def positions(
    request: Request,
    open_only: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        return {
            "status": "booting",
            "open_only": open_only,
            "count": 0,
            "total_count": 0,
            "positions": [],
        }

    rows = await orchestrator.get_positions(open_only=open_only, limit=limit)
    total = await orchestrator.get_open_positions_count() if open_only else len(rows)
    return {
        "status": "ok",
        "open_only": open_only,
        "count": len(rows),
        "total_count": total,
        "positions": rows,
    }

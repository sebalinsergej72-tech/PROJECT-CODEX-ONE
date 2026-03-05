from __future__ import annotations

from fastapi import APIRouter, Query, Request

router = APIRouter(tags=["open-orders"])


@router.get("/orders/open")
async def open_orders(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        return {"status": "booting", "count": 0, "orders": []}

    rows = await orchestrator.get_open_orders(limit=limit)
    return {
        "status": "ok",
        "count": len(rows),
        "orders": rows,
    }

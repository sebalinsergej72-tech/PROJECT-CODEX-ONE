from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["status"])


@router.get("/status")
async def status(request: Request) -> dict:
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        return {"status": "booting", "scheduler_running": False}

    payload = await orchestrator.get_status()
    payload["status"] = "ok"
    return payload

from __future__ import annotations

from fastapi import APIRouter, Request

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
                "report": "",
            },
        }

    payload = await orchestrator.get_status()
    payload.setdefault("last_discovery_stats", {})
    payload["status"] = "ok"
    return payload

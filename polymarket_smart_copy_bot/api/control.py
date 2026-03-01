from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter, Header, HTTPException, Request

from config.settings import settings

router = APIRouter(tags=["control"])


class TradingControlRequest(BaseModel):
    enabled: bool
    run_now: bool = Field(default=True)


def _assert_write_access(token: str | None) -> None:
    required = settings.dashboard_write_token
    if not required:
        return
    if token != required:
        raise HTTPException(status_code=401, detail="Invalid dashboard write token")


@router.post("/control/trading")
async def control_trading(
    payload: TradingControlRequest,
    request: Request,
    x_dashboard_token: str | None = Header(default=None),
) -> dict:
    _assert_write_access(x_dashboard_token)

    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator is not ready")

    status = await orchestrator.set_trading_enabled(payload.enabled)
    cycle = None
    if payload.enabled and payload.run_now:
        cycle = await orchestrator.run_trade_cycle_now()

    return {
        "status": "ok",
        "trading_enabled": status.get("trading_enabled", payload.enabled),
        "run_now_result": cycle,
    }


@router.post("/control/trading/start")
async def start_trading(
    request: Request,
    x_dashboard_token: str | None = Header(default=None),
) -> dict:
    return await control_trading(
        TradingControlRequest(enabled=True, run_now=True),
        request,
        x_dashboard_token,
    )


@router.post("/control/trading/stop")
async def stop_trading(
    request: Request,
    x_dashboard_token: str | None = Header(default=None),
) -> dict:
    return await control_trading(
        TradingControlRequest(enabled=False, run_now=False),
        request,
        x_dashboard_token,
    )

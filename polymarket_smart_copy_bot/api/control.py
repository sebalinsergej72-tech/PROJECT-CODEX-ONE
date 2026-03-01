from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from fastapi import APIRouter, Header, HTTPException, Request

from config.settings import settings

router = APIRouter(tags=["control"])


class TradingControlRequest(BaseModel):
    enabled: bool
    run_now: bool = Field(default=True)


class ModeControlRequest(BaseModel):
    mode: Literal["aggressive", "conservative"]


class BooleanControlRequest(BaseModel):
    enabled: bool


def _assert_write_access(token: str | None) -> None:
    required = settings.dashboard_write_token
    if not required:
        return
    if token != required:
        raise HTTPException(status_code=401, detail="Invalid dashboard write token")


def _get_orchestrator(request: Request):
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator is not ready")
    return orchestrator


@router.post("/control/trading")
async def control_trading(
    payload: TradingControlRequest,
    request: Request,
    x_dashboard_token: str | None = Header(default=None),
) -> dict:
    _assert_write_access(x_dashboard_token)
    orchestrator = _get_orchestrator(request)

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


@router.post("/control/mode")
async def control_mode(
    payload: ModeControlRequest,
    request: Request,
    x_dashboard_token: str | None = Header(default=None),
) -> dict:
    _assert_write_access(x_dashboard_token)
    orchestrator = _get_orchestrator(request)
    status = await orchestrator.set_mode(payload.mode)
    return {"status": "ok", "risk_mode": status.get("risk_mode", payload.mode)}


@router.post("/control/boost")
async def control_boost(
    payload: BooleanControlRequest,
    request: Request,
    x_dashboard_token: str | None = Header(default=None),
) -> dict:
    _assert_write_access(x_dashboard_token)
    orchestrator = _get_orchestrator(request)
    status = await orchestrator.set_boost(payload.enabled)
    return {
        "status": "ok",
        "high_conviction_boost_enabled": status.get("high_conviction_boost_enabled", payload.enabled),
    }


@router.post("/control/price-filter")
async def control_price_filter(
    payload: BooleanControlRequest,
    request: Request,
    x_dashboard_token: str | None = Header(default=None),
) -> dict:
    _assert_write_access(x_dashboard_token)
    orchestrator = _get_orchestrator(request)
    status = await orchestrator.set_price_filter(payload.enabled)
    return {
        "status": "ok",
        "price_filter_enabled": status.get("price_filter_enabled", payload.enabled),
    }


@router.post("/control/autoadd")
async def control_autoadd(
    payload: BooleanControlRequest,
    request: Request,
    x_dashboard_token: str | None = Header(default=None),
) -> dict:
    _assert_write_access(x_dashboard_token)
    orchestrator = _get_orchestrator(request)
    status = await orchestrator.set_autoadd(payload.enabled)
    return {"status": "ok", "discovery_autoadd": status.get("discovery_autoadd", payload.enabled)}

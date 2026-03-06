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


class EngineControlRequest(BaseModel):
    dry_run: bool


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


@router.post("/control/engine")
async def control_engine(
    payload: EngineControlRequest,
    request: Request,
    x_dashboard_token: str | None = Header(default=None),
) -> dict:
    _assert_write_access(x_dashboard_token)
    orchestrator = _get_orchestrator(request)
    status = await orchestrator.set_dry_run(payload.dry_run)
    return {
        "status": "ok",
        "dry_run": status.get("dry_run", payload.dry_run),
        "engine": "paper" if status.get("dry_run", payload.dry_run) else "live",
    }


@router.post("/control/polymarket/check")
async def control_polymarket_check(
    request: Request,
    x_dashboard_token: str | None = Header(default=None),
) -> dict:
    _assert_write_access(x_dashboard_token)
    orchestrator = _get_orchestrator(request)
    result = await orchestrator.check_polymarket_credentials()
    return {"status": "ok", **result}


@router.post("/control/discovery/run")
async def control_discovery_run(
    request: Request,
    x_dashboard_token: str | None = Header(default=None),
) -> dict:
    _assert_write_access(x_dashboard_token)
    orchestrator = _get_orchestrator(request)
    result = await orchestrator.run_discovery_now()
    return {"status": "ok", **result}


@router.post("/control/capital/recalc")
async def control_capital_recalc(
    request: Request,
    x_dashboard_token: str | None = Header(default=None),
) -> dict:
    _assert_write_access(x_dashboard_token)
    orchestrator = _get_orchestrator(request)
    result = await orchestrator.run_capital_recalc_now()
    return {"status": "ok", **result}


@router.post("/control/portfolio/refresh")
async def control_portfolio_refresh(
    request: Request,
    x_dashboard_token: str | None = Header(default=None),
) -> dict:
    _assert_write_access(x_dashboard_token)
    orchestrator = _get_orchestrator(request)
    result = await orchestrator.run_portfolio_refresh_now()
    return {"status": "ok", **result}


@router.post("/control/orders/cleanup")
async def control_stale_orders_cleanup(
    request: Request,
    x_dashboard_token: str | None = Header(default=None),
) -> dict:
    _assert_write_access(x_dashboard_token)
    orchestrator = _get_orchestrator(request)
    result = await orchestrator.run_stale_order_cleanup_now()
    return {"status": "ok", **result}


@router.post("/control/positions/purge")
async def control_purge_positions(
    request: Request,
    x_dashboard_token: str | None = Header(default=None),
) -> dict:
    """Close all DB open positions not confirmed by the current Polymarket account.

    Useful for removing dry-run leftovers or orphaned positions after a mode switch.
    """
    _assert_write_access(x_dashboard_token)
    orchestrator = _get_orchestrator(request)
    result = await orchestrator.purge_stale_positions()
    return {"status": "ok", **result}


@router.post("/control/positions/{position_id}/close")
async def control_close_position(
    position_id: int,
    request: Request,
    x_dashboard_token: str | None = Header(default=None),
) -> dict:
    _assert_write_access(x_dashboard_token)
    orchestrator = _get_orchestrator(request)
    result = await orchestrator.manual_close_position(position_id)
    if not result.get("success"):
        raise HTTPException(
            status_code=400, 
            detail=result.get("error", "Failed to close position manually")
        )
    return {"status": "ok", **result}

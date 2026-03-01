from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.control import router as control_router
from api.dashboard import router as dashboard_router
from config.settings import settings


class DummyOrchestrator:
    def __init__(self) -> None:
        self._enabled = True

    async def set_trading_enabled(self, enabled: bool) -> dict:
        self._enabled = enabled
        return {"trading_enabled": self._enabled}

    async def run_trade_cycle_now(self) -> dict:
        return {"ok": True, "last_trade_scan_at": "2026-03-01T00:00:00+00:00"}


def test_dashboard_page_renders() -> None:
    app = FastAPI()
    app.include_router(dashboard_router)

    with TestClient(app) as client:
        response = client.get("/dashboard")

    assert response.status_code == 200
    assert "Polymarket Smart Copy Bot" in response.text
    assert "Start Trading" in response.text
    assert "Stop Trading" in response.text


def test_control_trading_toggles() -> None:
    app = FastAPI()
    app.include_router(control_router)
    app.state.orchestrator = DummyOrchestrator()

    with TestClient(app) as client:
        response_stop = client.post("/control/trading", json={"enabled": False, "run_now": False})
        response_start = client.post("/control/trading", json={"enabled": True, "run_now": True})

    assert response_stop.status_code == 200
    assert response_stop.json()["trading_enabled"] is False

    assert response_start.status_code == 200
    assert response_start.json()["trading_enabled"] is True
    assert response_start.json()["run_now_result"]["ok"] is True


def test_control_trading_token_guard() -> None:
    app = FastAPI()
    app.include_router(control_router)
    app.state.orchestrator = DummyOrchestrator()

    original = settings.dashboard_write_token
    settings.dashboard_write_token = "secret-token"
    try:
        with TestClient(app) as client:
            unauthorized = client.post("/control/trading", json={"enabled": False, "run_now": False})
            authorized = client.post(
                "/control/trading",
                headers={"X-Dashboard-Token": "secret-token"},
                json={"enabled": False, "run_now": False},
            )
    finally:
        settings.dashboard_write_token = original

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200

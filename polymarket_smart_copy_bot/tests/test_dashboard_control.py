from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.control import router as control_router
from api.dashboard import router as dashboard_router
from config.settings import settings


class DummyOrchestrator:
    def __init__(self) -> None:
        self._enabled = True
        self._dry_run = True
        self._mode = "aggressive"
        self._boost = True
        self._price_filter = False
        self._autoadd = False
        self._cleanup_runs = 0
        self._dashboard_sessions: set[str] = {"telegram-session-token"}

    async def set_trading_enabled(self, enabled: bool) -> dict:
        self._enabled = enabled
        return {"trading_enabled": self._enabled}

    async def run_trade_cycle_now(self) -> dict:
        return {"ok": True, "last_trade_scan_at": "2026-03-01T00:00:00+00:00"}

    async def set_mode(self, mode: str) -> dict:
        self._mode = mode
        return {"risk_mode": self._mode}

    async def set_boost(self, enabled: bool) -> dict:
        self._boost = enabled
        return {"high_conviction_boost_enabled": self._boost}

    async def set_price_filter(self, enabled: bool) -> dict:
        self._price_filter = enabled
        return {"price_filter_enabled": self._price_filter}

    async def set_autoadd(self, enabled: bool) -> dict:
        self._autoadd = enabled
        return {"discovery_autoadd": self._autoadd}

    async def set_dry_run(self, enabled: bool) -> dict:
        self._dry_run = enabled
        return {"dry_run": self._dry_run}

    async def run_stale_order_cleanup_now(self) -> dict:
        self._cleanup_runs += 1
        return {
            "ok": True,
            "last_stale_order_cleanup_at": "2026-03-06T00:00:00+00:00",
            "cleanup": {
                "ok": True,
                "cancelled": 1,
                "sync_status": "canceled",
                "tracked_order_ids": 1,
            },
        }

    def has_valid_dashboard_session(self, token: str | None) -> bool:
        return bool(token and token in self._dashboard_sessions)

    def issue_telegram_dashboard_session(self, init_data: str) -> dict:
        if init_data == "forbidden":
            raise PermissionError("telegram_user_not_allowed")
        if init_data == "bad":
            raise ValueError("telegram_hash_invalid")
        return {
            "dashboard_token": "telegram-session-token",
            "expires_at": "2026-03-07T00:30:00+00:00",
            "user_id": 123456,
        }


def test_dashboard_page_renders() -> None:
    app = FastAPI()
    app.include_router(dashboard_router)

    with TestClient(app) as client:
        response = client.get("/dashboard")

    assert response.status_code == 200
    # Dashboard is served as a built SPA entrypoint (bundle mounted under
    # /dashboard-static), so we validate shell markers instead of button text.
    assert "id=\"root\"" in response.text
    assert "/dashboard-static/assets/" in response.text


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


def test_control_runtime_switches() -> None:
    app = FastAPI()
    app.include_router(control_router)
    app.state.orchestrator = DummyOrchestrator()

    with TestClient(app) as client:
        mode = client.post("/control/mode", json={"mode": "conservative"})
        boost = client.post("/control/boost", json={"enabled": False})
        price_filter = client.post("/control/price-filter", json={"enabled": True})
        autoadd = client.post("/control/autoadd", json={"enabled": True})

    assert mode.status_code == 200
    assert mode.json()["risk_mode"] == "conservative"
    assert boost.status_code == 200
    assert boost.json()["high_conviction_boost_enabled"] is False
    assert price_filter.status_code == 200
    assert price_filter.json()["price_filter_enabled"] is True
    assert autoadd.status_code == 200
    assert autoadd.json()["discovery_autoadd"] is True


def test_control_accepts_telegram_dashboard_session_token() -> None:
    app = FastAPI()
    app.include_router(control_router)
    app.state.orchestrator = DummyOrchestrator()

    original = settings.dashboard_write_token
    settings.dashboard_write_token = "secret-token"
    try:
        with TestClient(app) as client:
            authorized = client.post(
                "/control/trading",
                headers={"X-Dashboard-Token": "telegram-session-token"},
                json={"enabled": False, "run_now": False},
            )
    finally:
        settings.dashboard_write_token = original

    assert authorized.status_code == 200
    assert authorized.json()["trading_enabled"] is False


def test_telegram_webapp_auth_endpoint() -> None:
    app = FastAPI()
    app.include_router(control_router)
    app.state.orchestrator = DummyOrchestrator()

    with TestClient(app) as client:
        ok = client.post("/control/telegram-webapp/auth", json={"init_data": "ok"})
        bad = client.post("/control/telegram-webapp/auth", json={"init_data": "bad"})
        forbidden = client.post("/control/telegram-webapp/auth", json={"init_data": "forbidden"})

    assert ok.status_code == 200
    assert ok.json()["dashboard_token"] == "telegram-session-token"
    assert bad.status_code == 401
    assert forbidden.status_code == 403


def test_control_engine_switch() -> None:
    app = FastAPI()
    app.include_router(control_router)
    app.state.orchestrator = DummyOrchestrator()

    with TestClient(app) as client:
        live = client.post("/control/engine", json={"dry_run": False})
        paper = client.post("/control/engine", json={"dry_run": True})

    assert live.status_code == 200
    assert live.json()["dry_run"] is False
    assert live.json()["engine"] == "live"
    assert paper.status_code == 200
    assert paper.json()["dry_run"] is True
    assert paper.json()["engine"] == "paper"


def test_control_cleanup_endpoint() -> None:
    app = FastAPI()
    app.include_router(control_router)
    app.state.orchestrator = DummyOrchestrator()

    with TestClient(app) as client:
        response = client.post("/control/orders/cleanup")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["ok"] is True
    assert payload["cleanup"]["sync_status"] == "canceled"

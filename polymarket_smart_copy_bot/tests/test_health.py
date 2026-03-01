from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.health import router as health_router


class DummyOrchestrator:
    tracked_wallets_count = 8


def test_health_endpoint_shape() -> None:
    app = FastAPI()
    app.include_router(health_router)
    app.state.orchestrator = DummyOrchestrator()
    app.state.started_at = datetime.now(tz=timezone.utc)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "healthy"
    assert payload["tracked_wallets"] == 8
    assert isinstance(payload["uptime"], str)

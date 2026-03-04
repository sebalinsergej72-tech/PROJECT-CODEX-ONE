from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.positions import router as positions_router
from api.trades import router as trades_router


class DummyOrchestrator:
    async def get_recent_trades(self, limit: int = 20) -> list[dict]:
        return [
            {
                "id": 1,
                "external_trade_id": "t-1",
                "wallet_address": "0xabc",
                "market_id": "m-1",
                "outcome": "YES",
                "side": "buy",
                "price_cents": 44.5,
                "size_usd": 3.5,
                "status": "executed",
                "copied_at": "2026-03-01T12:00:00+00:00",
            }
        ][:limit]

    async def get_open_positions_count(self) -> int:
        return 1

    async def get_positions(self, *, open_only: bool = True, limit: int = 100) -> list[dict]:
        rows = [
            {
                "id": 1,
                "market_id": "m-1",
                "outcome": "YES",
                "is_open": True,
                "invested_usd": 3.5,
                "unrealized_pnl_usd": 0.4,
                "realized_pnl_usd": 0.0,
            },
            {
                "id": 2,
                "market_id": "m-2",
                "outcome": "NO",
                "is_open": False,
                "invested_usd": 2.0,
                "unrealized_pnl_usd": 0.0,
                "realized_pnl_usd": -0.3,
            },
        ]
        if open_only:
            rows = [row for row in rows if row["is_open"]]
        return rows[:limit]


def test_trades_endpoint_shape() -> None:
    app = FastAPI()
    app.include_router(trades_router)
    app.state.orchestrator = DummyOrchestrator()

    with TestClient(app) as client:
        response = client.get("/trades?limit=10")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["count"] == 1
    assert payload["trades"][0]["external_trade_id"] == "t-1"


def test_positions_endpoint_open_only_filter() -> None:
    app = FastAPI()
    app.include_router(positions_router)
    app.state.orchestrator = DummyOrchestrator()

    with TestClient(app) as client:
        response_open = client.get("/positions?open_only=true")
        response_all = client.get("/positions?open_only=false")

    assert response_open.status_code == 200
    open_payload = response_open.json()
    assert open_payload["status"] == "ok"
    assert open_payload["open_only"] is True
    assert open_payload["count"] == 1

    assert response_all.status_code == 200
    all_payload = response_all.json()
    assert all_payload["status"] == "ok"
    assert all_payload["open_only"] is False
    assert all_payload["count"] == 2

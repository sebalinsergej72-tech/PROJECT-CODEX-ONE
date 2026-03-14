from __future__ import annotations

import asyncio
from typing import Any

from data.polymarket_client import PolymarketClient


def test_extract_token_price_from_mid() -> None:
    payload: dict[str, Any] = {"mid": "0.745"}
    assert PolymarketClient._extract_token_price(payload) == 0.745


def test_extract_token_price_from_book_midpoint() -> None:
    payload: dict[str, Any] = {
        "bids": [{"price": "0.41"}],
        "asks": [{"price": "0.61"}],
    }
    assert PolymarketClient._extract_token_price(payload) == 0.51


def test_fetch_market_mid_price_uses_token_id_endpoints_first() -> None:
    client = PolymarketClient()

    async def _fake_request(method: str, url: str, **kwargs: Any) -> Any:
        if url.endswith("/midpoint"):
            return {"mid": "0.745"}
        raise AssertionError(f"Unexpected URL call in test: {url}")

    client._request_json = _fake_request  # type: ignore[method-assign]

    price = asyncio.run(client.fetch_market_mid_price("dummy-market", "dummy-token"))
    assert price == 74.5


def test_market_ws_book_payload_updates_orderbook_cache() -> None:
    client = PolymarketClient()

    client._handle_market_ws_payload(
        {
            "event_type": "book",
            "asset_id": "token-1",
            "bids": [{"price": "0.41", "size": "12"}],
            "asks": [{"price": "0.43", "size": "9"}],
        }
    )

    snapshot = client._get_market_ws_snapshot("token-1")
    assert snapshot is not None
    assert snapshot.best_bid == 0.41
    assert snapshot.best_ask == 0.43
    assert snapshot.bids[0].size == 12.0
    assert snapshot.asks[0].size == 9.0


def test_fetch_orderbook_prefers_fresh_market_ws_snapshot() -> None:
    client = PolymarketClient()
    client._store_market_ws_snapshot(
        "token-2",
        PolymarketClient._parse_orderbook_snapshot(
            {
                "bids": [{"price": "0.55", "size": "4"}],
                "asks": [{"price": "0.57", "size": "8"}],
            }
        ),
    )

    async def _boom(*_: Any, **__: Any) -> Any:
        raise AssertionError("REST fallback should not be used when websocket cache is fresh")

    client._request_json = _boom  # type: ignore[method-assign]

    snapshot = asyncio.run(client.fetch_orderbook("token-2"))
    assert snapshot is not None
    assert snapshot.best_bid == 0.55
    assert snapshot.best_ask == 0.57

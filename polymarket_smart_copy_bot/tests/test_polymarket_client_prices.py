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

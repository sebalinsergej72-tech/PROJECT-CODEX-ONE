from __future__ import annotations

import asyncio
from typing import Any

from config.settings import settings
from data.execution_sidecar_client import ExecutionSidecarClient
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


def test_fetch_orderbook_uses_execution_sidecar_before_rest(monkeypatch) -> None:
    monkeypatch.setattr(settings, "execution_sidecar_enabled", True)
    monkeypatch.setattr(settings, "execution_sidecar_market_data_enabled", True)
    client = PolymarketClient()

    class FakeSidecar:
        async def fetch_orderbook(self, token_id: str):
            assert token_id == "token-sidecar"
            return PolymarketClient._parse_orderbook_snapshot(
                {
                    "bids": [{"price": "0.60", "size": "6"}],
                    "asks": [{"price": "0.62", "size": "9"}],
                }
            )

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    client._execution_sidecar = FakeSidecar()

    async def _boom(*_: Any, **__: Any) -> Any:
        raise AssertionError("REST fallback should not be used when sidecar returns a snapshot")

    client._request_json = _boom  # type: ignore[method-assign]

    snapshot = asyncio.run(client.fetch_orderbook("token-sidecar"))
    assert snapshot is not None
    assert snapshot.best_bid == 0.60
    assert snapshot.best_ask == 0.62


def test_prime_market_data_backfills_local_cache_from_execution_sidecar(monkeypatch) -> None:
    monkeypatch.setattr(settings, "execution_sidecar_enabled", True)
    monkeypatch.setattr(settings, "execution_sidecar_market_data_enabled", True)
    monkeypatch.setattr(settings, "execution_sidecar_executable_snapshot_enabled", True)
    monkeypatch.setattr(settings, "polymarket_market_ws_enabled", False)
    client = PolymarketClient()

    class FakeSidecar:
        async def prime_market_data(self, token_ids: list[str]):
            assert token_ids == ["token-a"]
            return {
                "token-a": PolymarketClient._parse_orderbook_snapshot(
                    {
                        "bids": [{"price": "0.21", "size": "30"}],
                        "asks": [{"price": "0.24", "size": "30"}],
                    }
                )
            }

        async def prime_executable_market_data(self, token_ids: list[str]):
            assert token_ids == ["token-a"]
            return {
                "token-a": await self.fetch_executable_snapshot("token-a"),
            }

        async def fetch_executable_snapshot(self, token_id: str):
            assert token_id == "token-a"
            return client._build_executable_snapshot_from_orderbook(
                PolymarketClient._parse_orderbook_snapshot(
                    {
                        "bids": [{"price": "0.21", "size": "30"}],
                        "asks": [{"price": "0.24", "size": "30"}],
                    }
                )
            )

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    client._execution_sidecar = FakeSidecar()

    asyncio.run(client.prime_market_data(["token-a"]))
    snapshot = client.get_cached_orderbook("token-a")
    assert snapshot is not None
    assert snapshot.best_bid == 0.21
    assert snapshot.best_ask == 0.24
    executable = client.get_cached_executable_snapshot("token-a")
    assert executable is not None
    assert executable.best_bid == 0.21
    assert executable.buy_vwap_5usd is not None


def test_fetch_executable_snapshot_uses_execution_sidecar(monkeypatch) -> None:
    monkeypatch.setattr(settings, "execution_sidecar_enabled", True)
    monkeypatch.setattr(settings, "execution_sidecar_market_data_enabled", True)
    monkeypatch.setattr(settings, "execution_sidecar_executable_snapshot_enabled", True)
    client = PolymarketClient()

    class FakeSidecar:
        async def fetch_executable_snapshot(self, token_id: str):
            assert token_id == "token-vwap"
            return client._build_executable_snapshot_from_orderbook(
                PolymarketClient._parse_orderbook_snapshot(
                    {
                        "bids": [{"price": "0.44", "size": "10"}],
                        "asks": [{"price": "0.46", "size": "20"}],
                    }
                )
            )

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    client._execution_sidecar = FakeSidecar()
    snapshot = asyncio.run(client.fetch_executable_snapshot("token-vwap"))
    assert snapshot is not None
    assert snapshot.best_ask == 0.46
    assert snapshot.buy_vwap_5usd == 0.46


def test_store_market_ws_snapshot_updates_executable_cache() -> None:
    client = PolymarketClient()
    client._store_market_ws_snapshot(
        "token-exec",
        PolymarketClient._parse_orderbook_snapshot(
            {
                "bids": [{"price": "0.33", "size": "20"}],
                "asks": [{"price": "0.35", "size": "30"}],
            }
        ),
    )
    snapshot = client.get_cached_executable_snapshot("token-exec")
    assert snapshot is not None
    assert snapshot.best_bid == 0.33
    assert snapshot.sell_vwap_5usd == 0.33


def test_execution_sidecar_push_message_updates_cache() -> None:
    client = ExecutionSidecarClient()
    client._apply_push_message(
        '{"type":"snapshot","token_id":"token-push","snapshot":{"best_bid":0.41,"best_ask":0.43,"buy_vwap_5usd":0.43,"buy_vwap_10usd":0.43,"buy_vwap_25usd":null,"sell_vwap_5usd":0.41,"sell_vwap_10usd":0.41,"sell_vwap_25usd":null,"top5_ask_liquidity_usd":12.0,"top5_bid_liquidity_usd":11.0,"has_book":true,"last_update_ts":1700000000.0,"snapshot_age_ms":5}}'
    )
    snapshot = client.get_cached_executable_snapshot("token-push")
    assert snapshot is not None
    assert snapshot.best_ask == 0.43
    assert snapshot.buy_vwap_5usd == 0.43


def test_polymarket_client_reads_cached_push_snapshot_from_sidecar(monkeypatch) -> None:
    monkeypatch.setattr(settings, "execution_sidecar_enabled", True)
    client = PolymarketClient()

    class FakeSidecar:
        def get_cached_executable_snapshot(self, token_id: str):
            assert token_id == "token-sidecar-push"
            return client._build_executable_snapshot_from_orderbook(
                PolymarketClient._parse_orderbook_snapshot(
                    {
                        "bids": [{"price": "0.55", "size": "20"}],
                        "asks": [{"price": "0.57", "size": "20"}],
                    }
                )
            )

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    client._execution_sidecar = FakeSidecar()
    snapshot = client.get_cached_executable_snapshot("token-sidecar-push")
    assert snapshot is not None
    assert snapshot.best_bid == 0.55

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from config.settings import settings
from data.polymarket_client import OrderRequest, PolymarketClient


class _FakeClobClient:
    def __init__(self) -> None:
        self.market_order_args = None
        self.limit_order_args = None
        self.posted_order = None
        self.posted_type = None

    def create_market_order(self, order_args):
        self.market_order_args = order_args
        return {"kind": "market", "amount": order_args.amount, "price": order_args.price}

    def create_order(self, order_args):
        self.limit_order_args = order_args
        return {"kind": "limit", "size": order_args.size, "price": order_args.price}

    def post_order(self, signed_order, order_type):
        self.posted_order = signed_order
        self.posted_type = order_type
        return {"success": True, "orderID": "test-order-id"}


def test_place_order_sync_uses_market_order_builder_for_buy_fok() -> None:
    client = PolymarketClient()
    fake = _FakeClobClient()
    client._ensure_clob_client = lambda: fake  # type: ignore[method-assign]

    result = client._place_order_sync(
        OrderRequest(
            token_id="token-1",
            side="buy",
            price_cents=71.0,
            size_usd=7.01,
            market_id="market-1",
            outcome="Yes",
            order_type="FOK",
        )
    )

    assert result["success"] is True
    assert fake.market_order_args is not None
    assert fake.market_order_args.amount == 7.01
    assert fake.market_order_args.price == 0.71
    assert fake.limit_order_args is None
    assert fake.posted_order["kind"] == "market"


def test_place_order_sync_uses_limit_order_builder_for_buy_gtc() -> None:
    client = PolymarketClient()
    fake = _FakeClobClient()
    client._ensure_clob_client = lambda: fake  # type: ignore[method-assign]

    result = client._place_order_sync(
        OrderRequest(
            token_id="token-gtc",
            side="buy",
            price_cents=61.0,
            size_usd=7.01,
            market_id="market-gtc",
            outcome="Yes",
            order_type="GTC",
        )
    )

    assert result["success"] is True
    assert fake.limit_order_args is not None
    assert fake.limit_order_args.price == 0.61
    assert fake.limit_order_args.size == 11.49
    assert fake.market_order_args is None
    assert fake.posted_order["kind"] == "limit"


def test_place_order_sync_uses_limit_order_builder_for_sell() -> None:
    client = PolymarketClient()
    fake = _FakeClobClient()
    client._ensure_clob_client = lambda: fake  # type: ignore[method-assign]

    result = client._place_order_sync(
        OrderRequest(
            token_id="token-2",
            side="sell",
            price_cents=63.0,
            size_usd=6.3,
            market_id="market-2",
            outcome="No",
            order_type="IOC",
        )
    )

    assert result["success"] is True
    assert fake.limit_order_args is not None
    assert fake.limit_order_args.price == 0.63
    assert fake.market_order_args is None
    assert fake.posted_order["kind"] == "limit"


def test_place_order_sync_rejects_invalid_low_price() -> None:
    client = PolymarketClient()
    fake = _FakeClobClient()
    client._ensure_clob_client = lambda: fake  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="invalid_price"):
        client._place_order_sync(
            OrderRequest(
                token_id="token-low",
                side="buy",
                price_cents=0.1,
                size_usd=5.0,
                market_id="market-low",
                outcome="Yes",
                order_type="GTC",
            )
        )

    assert fake.market_order_args is None
    assert fake.limit_order_args is None


def test_fetch_open_orders_prefers_sidecar_authenticated_reads(monkeypatch) -> None:
    class _FakeSidecar:
        async def fetch_open_orders(self, *, market_id=None, token_id=None):
            assert market_id == "market-1"
            assert token_id == "token-1"
            return [
                {
                    "id": "ord-1",
                    "market": "market-1",
                    "asset_id": "token-1",
                    "side": "buy",
                    "price": "0.61",
                    "size": "10",
                    "createdAt": datetime(2026, 3, 26, tzinfo=timezone.utc).isoformat(),
                }
            ]

    async def _case() -> None:
        client = PolymarketClient()
        client._dry_run = False
        client._execution_sidecar = _FakeSidecar()
        client._fetch_open_orders_sync = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("python path should not run"))  # type: ignore[method-assign]

        original_sidecar = settings.execution_sidecar_enabled
        original_reads = settings.execution_sidecar_authenticated_reads_enabled
        original_key = settings.polymarket_private_key
        try:
            settings.execution_sidecar_enabled = True
            settings.execution_sidecar_authenticated_reads_enabled = True
            settings.polymarket_private_key = "0xabc"
            rows = await client.fetch_open_orders(market_id="market-1", token_id="token-1")
            assert rows is not None
            assert len(rows) == 1
            assert rows[0].order_id == "ord-1"
        finally:
            settings.execution_sidecar_enabled = original_sidecar
            settings.execution_sidecar_authenticated_reads_enabled = original_reads
            settings.polymarket_private_key = original_key

    asyncio.run(_case())

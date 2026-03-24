from __future__ import annotations

from typing import Any

import aiohttp

from config.settings import settings
from contracts.execution_sidecar import (
    SidecarExecutionPlanRequest,
    SidecarExecutionPlanResponse,
    SidecarOrderbookSnapshot,
    SidecarPrimeMarketDataRequest,
    SidecarPrimeMarketDataResponse,
)
from data.polymarket_client import OrderbookLevel, OrderbookSnapshot


class ExecutionSidecarClient:
    """Thin async client for the Rust execution sidecar."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._base_url = settings.execution_sidecar_base_url.rstrip("/")

    async def start(self) -> None:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=max(0.1, settings.execution_sidecar_timeout_seconds))
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def health(self) -> dict[str, Any]:
        session = await self._ensure_session()
        async with session.get(f"{self._base_url}/health") as response:
            response.raise_for_status()
            return await response.json()

    async def prime_market_data(self, token_ids: list[str]) -> dict[str, OrderbookSnapshot | None]:
        normalized = [token_id.strip() for token_id in token_ids if token_id and token_id.strip()]
        if not normalized:
            return {}
        payload = SidecarPrimeMarketDataRequest(token_ids=normalized)
        session = await self._ensure_session()
        async with session.post(
            f"{self._base_url}/v1/market/prime",
            json=payload.model_dump(mode="json"),
        ) as response:
            response.raise_for_status()
            raw = await response.json()
        parsed = SidecarPrimeMarketDataResponse.model_validate(raw)
        return {
            token_id: self._to_orderbook_snapshot(snapshot)
            for token_id, snapshot in parsed.snapshots.items()
        }

    async def fetch_orderbook(self, token_id: str) -> OrderbookSnapshot | None:
        normalized = token_id.strip()
        if not normalized:
            return None
        session = await self._ensure_session()
        async with session.get(f"{self._base_url}/v1/market/snapshot/{normalized}") as response:
            if response.status == 404:
                return None
            response.raise_for_status()
            raw = await response.json()
        snapshot = SidecarOrderbookSnapshot.model_validate(raw)
        return self._to_orderbook_snapshot(snapshot)

    async def build_execution_plan(
        self,
        request: SidecarExecutionPlanRequest,
    ) -> SidecarExecutionPlanResponse:
        session = await self._ensure_session()
        async with session.post(
            f"{self._base_url}/v1/execute/plan",
            json=request.model_dump(mode="json"),
        ) as response:
            response.raise_for_status()
            raw = await response.json()
        return SidecarExecutionPlanResponse.model_validate(raw)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            await self.start()
        assert self._session is not None
        return self._session

    @staticmethod
    def _to_orderbook_snapshot(snapshot: SidecarOrderbookSnapshot | None) -> OrderbookSnapshot | None:
        if snapshot is None:
            return None
        return OrderbookSnapshot(
            bids=[OrderbookLevel(price=level.price, size=level.size) for level in snapshot.bids],
            asks=[OrderbookLevel(price=level.price, size=level.size) for level in snapshot.asks],
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
        )

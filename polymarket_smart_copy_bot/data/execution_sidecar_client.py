from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from loguru import logger

from config.settings import settings
from contracts.execution_sidecar import (
    SidecarAuthenticatedFillsResponse,
    SidecarAuthenticatedOpenOrdersResponse,
    SidecarAuthenticatedOrderStatusResponse,
    SidecarExecutableSnapshot,
    SidecarExecutionPlanRequest,
    SidecarExecutionPlanResponse,
    SidecarFillReconcileRequest,
    SidecarFillReconcileResponse,
    SidecarHotWalletScanRequest,
    SidecarHotWalletScanResponse,
    SidecarOrderbookSnapshot,
    SidecarPrimeExecutableMarketDataResponse,
    SidecarPrimeMarketDataRequest,
    SidecarPrimeMarketDataResponse,
    SidecarRegisterHotMarketsRequest,
    SidecarRegisterHotMarketsResponse,
)
from data.polymarket_client import ExecutableSnapshot, OrderbookLevel, OrderbookSnapshot, WalletTradeSignal


class ExecutionSidecarClient:
    """Thin async client for the Rust execution sidecar."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._base_url = settings.execution_sidecar_base_url.rstrip("/")
        self._push_task: asyncio.Task[None] | None = None
        self._push_stop = asyncio.Event()
        self._push_writer: asyncio.StreamWriter | None = None
        self._push_send_lock = asyncio.Lock()
        self._push_subscriptions: set[str] = set()
        self._push_snapshots: dict[str, ExecutableSnapshot] = {}
        self._push_snapshot_updated_at: dict[str, datetime] = {}

    async def start(self) -> None:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=max(0.1, settings.execution_sidecar_timeout_seconds))
            self._session = aiohttp.ClientSession(timeout=timeout)
        if settings.execution_sidecar_push_enabled and self._push_task is None:
            self._push_stop.clear()
            self._push_task = asyncio.create_task(
                self._run_push_stream_loop(),
                name="execution-sidecar-push-stream",
            )

    async def stop(self) -> None:
        self._push_stop.set()
        if self._push_writer is not None:
            self._push_writer.close()
            with contextlib.suppress(Exception):
                await self._push_writer.wait_closed()
            self._push_writer = None
        if self._push_task is not None:
            self._push_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._push_task
            self._push_task = None
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

    async def prime_executable_market_data(self, token_ids: list[str]) -> dict[str, ExecutableSnapshot | None]:
        normalized = [token_id.strip() for token_id in token_ids if token_id and token_id.strip()]
        if not normalized:
            return {}
        await self.subscribe_executable_snapshots(normalized)
        payload = SidecarPrimeMarketDataRequest(token_ids=normalized)
        session = await self._ensure_session()
        async with session.post(
            f"{self._base_url}/v1/market/executable/prime",
            json=payload.model_dump(mode="json"),
        ) as response:
            response.raise_for_status()
            raw = await response.json()
        parsed = SidecarPrimeExecutableMarketDataResponse.model_validate(raw)
        return {
            token_id: self._to_executable_snapshot(snapshot)
            for token_id, snapshot in parsed.snapshots.items()
        }

    async def register_hot_markets(
        self,
        token_ids: list[str],
        *,
        ttl_seconds: int | None = None,
        source: str | None = None,
        priority: int | None = None,
    ) -> int:
        normalized = [token_id.strip() for token_id in token_ids if token_id and token_id.strip()]
        if not normalized:
            return 0
        payload = SidecarRegisterHotMarketsRequest(
            token_ids=normalized,
            ttl_seconds=ttl_seconds,
            source=source,
            priority=priority,
        )
        session = await self._ensure_session()
        async with session.post(
            f"{self._base_url}/v1/market/register_hot",
            json=payload.model_dump(mode="json"),
        ) as response:
            response.raise_for_status()
            raw = await response.json()
        parsed = SidecarRegisterHotMarketsResponse.model_validate(raw)
        await self.subscribe_executable_snapshots(normalized)
        return int(parsed.accepted)

    async def scan_hot_wallet_trades(
        self,
        wallet_addresses: list[str],
        *,
        signal_limit: int,
        hot_market_ttl_seconds: int | None = None,
    ) -> dict[str, list[WalletTradeSignal]]:
        normalized = [wallet_address.strip() for wallet_address in wallet_addresses if wallet_address and wallet_address.strip()]
        if not normalized:
            return {}
        payload = SidecarHotWalletScanRequest(
            wallets=[{"wallet_address": wallet_address} for wallet_address in normalized],
            signal_limit=signal_limit,
            hot_market_ttl_seconds=hot_market_ttl_seconds,
        )
        session = await self._ensure_session()
        async with session.post(
            f"{self._base_url}/v1/signals/hot_scan",
            json=payload.model_dump(mode="json"),
        ) as response:
            response.raise_for_status()
            raw = await response.json()
        parsed = SidecarHotWalletScanResponse.model_validate(raw)
        return {
            wallet_address.lower(): [
                WalletTradeSignal(
                    external_trade_id=row.external_trade_id,
                    wallet_address=row.wallet_address,
                    market_id=row.market_id,
                    token_id=row.token_id,
                    outcome=row.outcome,
                    side=row.side,
                    price_cents=row.price_cents,
                    size_usd=row.size_usd,
                    traded_at=datetime.fromtimestamp(row.traded_at_ts, tz=timezone.utc),
                    profit_usd=row.profit_usd,
                    market_slug=row.market_slug,
                    market_category=row.market_category,
                )
                for row in rows
            ]
            for wallet_address, rows in parsed.signals.items()
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

    async def fetch_executable_snapshot(self, token_id: str) -> ExecutableSnapshot | None:
        normalized = token_id.strip()
        if not normalized:
            return None
        cached = self.get_cached_executable_snapshot(normalized)
        if cached is not None:
            return cached
        await self.subscribe_executable_snapshots([normalized])
        session = await self._ensure_session()
        async with session.get(f"{self._base_url}/v1/market/executable/{normalized}") as response:
            if response.status == 404:
                return None
            response.raise_for_status()
            raw = await response.json()
        snapshot = SidecarExecutableSnapshot.model_validate(raw)
        return self._to_executable_snapshot(snapshot)

    async def subscribe_executable_snapshots(self, token_ids: list[str]) -> None:
        normalized = {
            token_id.strip()
            for token_id in token_ids
            if token_id and token_id.strip()
        }
        if not normalized:
            return
        self._push_subscriptions.update(normalized)
        if not settings.execution_sidecar_push_enabled:
            return
        await self._send_push_subscriptions()

    def get_cached_executable_snapshot(self, token_id: str) -> ExecutableSnapshot | None:
        normalized = token_id.strip()
        if not normalized:
            return None
        updated_at = self._push_snapshot_updated_at.get(normalized)
        snapshot = self._push_snapshots.get(normalized)
        if updated_at is None or snapshot is None:
            return None
        ttl = timedelta(seconds=max(1, settings.polymarket_market_ws_cache_ttl_seconds))
        if datetime.now(tz=timezone.utc) - updated_at > ttl:
            return None
        return snapshot

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

    async def reconcile_fills(
        self,
        request: SidecarFillReconcileRequest,
    ) -> SidecarFillReconcileResponse:
        session = await self._ensure_session()
        async with session.post(
            f"{self._base_url}/v1/fills/reconcile",
            json=request.model_dump(mode="json"),
        ) as response:
            response.raise_for_status()
            raw = await response.json()
        return SidecarFillReconcileResponse.model_validate(raw)

    async def fetch_open_orders(
        self,
        *,
        market_id: str | None = None,
        token_id: str | None = None,
    ) -> list[dict[str, Any]]:
        session = await self._ensure_session()
        params: dict[str, str] = {}
        if market_id:
            params["market_id"] = market_id
        if token_id:
            params["token_id"] = token_id
        async with session.get(
            f"{self._base_url}/v1/orders/open",
            params=params or None,
        ) as response:
            response.raise_for_status()
            raw = await response.json()
        parsed = SidecarAuthenticatedOpenOrdersResponse.model_validate(raw)
        return list(parsed.rows)

    async def fetch_order_status(self, order_id: str) -> dict[str, Any] | None:
        normalized = order_id.strip()
        if not normalized:
            return None
        session = await self._ensure_session()
        async with session.get(f"{self._base_url}/v1/orders/{normalized}/status") as response:
            if response.status == 404:
                return None
            response.raise_for_status()
            raw = await response.json()
        parsed = SidecarAuthenticatedOrderStatusResponse.model_validate(raw)
        return parsed.payload

    async def fetch_account_fills(
        self,
        *,
        after_ts: datetime,
        market_id: str | None = None,
        token_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        session = await self._ensure_session()
        params: dict[str, str] = {
            "after_ts": str(after_ts.timestamp()),
            "limit": str(max(1, min(limit, 500))),
        }
        if market_id:
            params["market_id"] = market_id
        if token_id:
            params["token_id"] = token_id
        async with session.get(
            f"{self._base_url}/v1/fills/account",
            params=params,
        ) as response:
            response.raise_for_status()
            raw = await response.json()
        parsed = SidecarAuthenticatedFillsResponse.model_validate(raw)
        return list(parsed.rows)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            await self.start()
        assert self._session is not None
        return self._session

    async def _run_push_stream_loop(self) -> None:
        while not self._push_stop.is_set():
            try:
                reader, writer = await asyncio.open_connection(
                    settings.execution_sidecar_push_host,
                    settings.execution_sidecar_push_port,
                )
                self._push_writer = writer
                await self._send_push_subscriptions()
                while not self._push_stop.is_set():
                    line = await reader.readline()
                    if not line:
                        break
                    self._apply_push_message(line.decode("utf-8").strip())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Execution sidecar push stream disconnected: {}", exc)
            finally:
                if self._push_writer is not None:
                    self._push_writer.close()
                    with contextlib.suppress(Exception):
                        await self._push_writer.wait_closed()
                    self._push_writer = None
            if not self._push_stop.is_set():
                await asyncio.sleep(max(0.1, settings.execution_sidecar_push_reconnect_seconds))

    async def _send_push_subscriptions(self) -> None:
        writer = self._push_writer
        if writer is None or writer.is_closing():
            return
        payload = {
            "type": "subscribe",
            "token_ids": sorted(self._push_subscriptions),
        }
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._push_send_lock:
            writer.write(data)
            await writer.drain()

    def _apply_push_message(self, raw: str) -> None:
        if not raw:
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Execution sidecar push stream sent invalid JSON")
            return
        if str(payload.get("type") or "").lower() != "snapshot":
            return
        token_id = str(payload.get("token_id") or "").strip()
        if not token_id:
            return
        snapshot_payload = payload.get("snapshot")
        if snapshot_payload is None:
            self._push_snapshots.pop(token_id, None)
            self._push_snapshot_updated_at.pop(token_id, None)
            return
        snapshot = SidecarExecutableSnapshot.model_validate(snapshot_payload)
        self._push_snapshots[token_id] = self._to_executable_snapshot(snapshot)
        self._push_snapshot_updated_at[token_id] = datetime.now(tz=timezone.utc)

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

    @staticmethod
    def _to_executable_snapshot(snapshot: SidecarExecutableSnapshot | None) -> ExecutableSnapshot | None:
        if snapshot is None:
            return None
        return ExecutableSnapshot(
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            buy_vwap_5usd=snapshot.buy_vwap_5usd,
            buy_vwap_10usd=snapshot.buy_vwap_10usd,
            buy_vwap_25usd=snapshot.buy_vwap_25usd,
            sell_vwap_5usd=snapshot.sell_vwap_5usd,
            sell_vwap_10usd=snapshot.sell_vwap_10usd,
            sell_vwap_25usd=snapshot.sell_vwap_25usd,
            top5_ask_liquidity_usd=snapshot.top5_ask_liquidity_usd,
            top5_bid_liquidity_usd=snapshot.top5_bid_liquidity_usd,
            has_book=snapshot.has_book,
            last_update_ts=snapshot.last_update_ts,
            snapshot_age_ms=snapshot.snapshot_age_ms,
        )

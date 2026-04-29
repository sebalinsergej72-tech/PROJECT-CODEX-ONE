from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import aiohttp
import certifi
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import settings
from utils.helpers import ensure_price_in_cents


@dataclass(slots=True)
class WalletTradeSignal:
    external_trade_id: str
    wallet_address: str
    market_id: str
    token_id: str | None
    outcome: str
    side: str
    price_cents: float
    size_usd: float
    traded_at: datetime
    profit_usd: float | None = None
    market_slug: str | None = None
    market_category: str | None = None


@dataclass(slots=True)
class WalletOpenPosition:
    wallet_address: str
    market_id: str
    token_id: str | None
    outcome: str
    quantity: float
    avg_price_cents: float
    current_price_cents: float
    invested_usd: float
    current_value_usd: float
    unrealized_pnl_usd: float


@dataclass(slots=True)
class MarketInfoData:
    """Minimal market metadata from the Gamma API."""

    market_id: str
    question: str
    category: str


@dataclass(slots=True)
class OrderRequest:
    token_id: str
    side: str
    price_cents: float
    size_usd: float
    market_id: str
    outcome: str
    order_type: str = "GTC"  # IMPROVED: status model — "GTC" or "FOK"


@dataclass(slots=True)
class OrderResult:
    success: bool
    order_id: str | None
    tx_hash: str | None
    error: str | None = None


PUSD_TOKEN_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
USDCE_TOKEN_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_TOKEN_ADDRESS = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
CTF_EXCHANGE_ADDRESS = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_CTF_EXCHANGE_ADDRESS = "0xe2222d279d744050d28e00520010520000310F59"
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
POLYGON_RPC_FALLBACK_URLS = (
    "https://polygon.drpc.org",
    "https://polygon.llamarpc.com",
    "https://1rpc.io/matic",
)


@dataclass(slots=True)
class OpenOrderInfo:
    order_id: str
    market_id: str | None
    token_id: str | None
    side: str
    price_cents: float
    size: float
    created_at: datetime
    raw: dict[str, Any]


@dataclass(slots=True)
class FillInfo:
    trade_id: str
    order_id: str | None
    market_id: str | None
    token_id: str | None
    side: str
    price_cents: float
    size_shares: float
    size_usd: float
    traded_at: datetime
    raw: dict[str, Any]


@dataclass(slots=True)
class OrderbookLevel:
    price: float
    size: float


@dataclass(slots=True)
class OrderbookSnapshot:
    bids: list[OrderbookLevel]
    asks: list[OrderbookLevel]
    best_bid: float | None
    best_ask: float | None


@dataclass(slots=True)
class ExecutableSnapshot:
    best_bid: float | None
    best_ask: float | None
    buy_vwap_5usd: float | None
    buy_vwap_10usd: float | None
    buy_vwap_25usd: float | None
    sell_vwap_5usd: float | None
    sell_vwap_10usd: float | None
    sell_vwap_25usd: float | None
    top5_ask_liquidity_usd: float
    top5_bid_liquidity_usd: float
    has_book: bool
    last_update_ts: float | None = None
    snapshot_age_ms: int | None = None


class PolymarketClient:
    """Polymarket data + order execution client with retry and timeout guards."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._clob_client: Any = None
        self._clob_creds_source: str | None = None
        self._dry_run = settings.dry_run
        self._missing_orderbooks: set[str] = set()
        self._market_ws_task: asyncio.Task[None] | None = None
        self._market_ws_ping_task: asyncio.Task[None] | None = None
        self._market_ws: aiohttp.ClientWebSocketResponse | None = None
        self._market_ws_stop = asyncio.Event()
        self._market_ws_wakeup = asyncio.Event()
        self._market_ws_desired_assets: set[str] = set()
        self._market_ws_subscribed_assets: set[str] = set()
        self._market_ws_books: dict[str, dict[str, dict[float, float]]] = {}
        self._market_ws_book_updated_at: dict[str, datetime] = {}
        self._executable_snapshots: dict[str, ExecutableSnapshot] = {}
        self._executable_snapshot_updated_at: dict[str, datetime] = {}
        self._market_ws_waiters: dict[str, asyncio.Event] = {}
        self._market_ws_lock = asyncio.Lock()
        self._market_ws_send_lock = asyncio.Lock()
        self._execution_sidecar: Any | None = None
        if settings.execution_sidecar_enabled:
            from data.execution_sidecar_client import ExecutionSidecarClient

            self._execution_sidecar = ExecutionSidecarClient()

    def set_dry_run(self, enabled: bool) -> None:
        self._dry_run = enabled

    def is_dry_run(self) -> bool:
        return self._dry_run

    async def start(self) -> None:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=20)
            if settings.polymarket_verify_ssl:
                ssl_context = ssl.create_default_context(cafile=certifi.where())
                connector = aiohttp.TCPConnector(ssl=ssl_context)
            else:
                logger.warning("POLYMARKET_VERIFY_SSL=false: TLS verification disabled for Polymarket HTTP calls")
                connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        if settings.polymarket_market_ws_enabled and self._market_ws_task is None:
            self._market_ws_stop.clear()
            self._market_ws_task = asyncio.create_task(
                self._run_market_ws_loop(),
                name="polymarket-market-ws",
            )
        if self._execution_sidecar is not None:
            await self._execution_sidecar.start()

    async def stop(self) -> None:
        self._market_ws_stop.set()
        self._market_ws_wakeup.set()
        if self._market_ws_ping_task is not None:
            self._market_ws_ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._market_ws_ping_task
            self._market_ws_ping_task = None
        if self._market_ws is not None:
            with contextlib.suppress(Exception):
                await self._market_ws.close()
            self._market_ws = None
        if self._market_ws_task is not None:
            self._market_ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._market_ws_task
            self._market_ws_task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self._execution_sidecar is not None:
            await self._execution_sidecar.stop()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            await self.start()
        assert self._session is not None
        return self._session

    @retry(wait=wait_exponential(multiplier=1, min=1, max=12), stop=stop_after_attempt(4), reraise=True)
    async def _request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        session = await self._ensure_session()
        async with session.request(method, url, **kwargs) as response:
            response.raise_for_status()
            return await response.json()

    async def get_leaderboard(
        self,
        *,
        category: str,
        time_period: str = "MONTH",
        order_by: str = "PNL",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch leaderboard rows from Polymarket data-api."""

        url = f"{settings.polymarket_data_api_host.rstrip('/')}/v1/leaderboard"
        params = {
            "category": category,
            "timePeriod": time_period,
            "orderBy": order_by,
            "limit": limit,
        }
        try:
            raw = await self._request_json("GET", url, params=params)
        except Exception as exc:
            logger.warning("Leaderboard fetch failed for category {}: {}", category, exc)
            return []
        return self._coerce_rows(raw)

    async def get_user_trades(self, wallet_address: str, limit: int = 500) -> list[dict[str, Any]]:
        """Fetch raw user trades from data-api, falling back to gamma endpoint."""

        normalized_wallet = self._normalize_address(wallet_address)
        data_api_url = f"{settings.polymarket_data_api_host.rstrip('/')}/v1/trades"
        params_variants = (
            {"user": wallet_address, "limit": limit},
            {"address": wallet_address, "limit": limit},
            {"walletAddress": wallet_address, "limit": limit},
        )

        for params in params_variants:
            try:
                raw = await self._request_json("GET", data_api_url, params=params)
            except Exception:
                continue
            rows = self._coerce_rows(raw)
            filtered = self._filter_rows_by_wallet(rows, normalized_wallet)
            if filtered:
                return filtered[:limit]

        gamma_url = f"{settings.polymarket_gamma_host.rstrip('/')}/trades"
        try:
            raw = await self._request_json("GET", gamma_url, params={"user": wallet_address, "limit": limit})
        except Exception as exc:
            logger.warning("Failed to fetch raw trades for {}: {}", wallet_address, exc)
            return []
        rows = self._coerce_rows(raw)
        filtered = self._filter_rows_by_wallet(rows, normalized_wallet)
        return filtered[:limit]

    async def get_user_activity(self, wallet_address: str, limit: int = 500) -> list[dict[str, Any]]:
        """Fetch raw user activity from data-api."""

        normalized_wallet = self._normalize_address(wallet_address)
        url = f"{settings.polymarket_data_api_host.rstrip('/')}/v1/activity"
        params_variants = (
            {"user": wallet_address, "limit": limit},
            {"address": wallet_address, "limit": limit},
            {"walletAddress": wallet_address, "limit": limit},
        )
        for params in params_variants:
            try:
                raw = await self._request_json("GET", url, params=params)
            except Exception:
                continue
            rows = self._coerce_rows(raw)
            filtered = self._filter_rows_by_wallet(rows, normalized_wallet)
            if filtered:
                return filtered[:limit]
        return []

    async def fetch_wallet_trades(self, wallet_address: str, limit: int = 30) -> list[WalletTradeSignal]:
        """Load recent trades for a tracked wallet from Polymarket data API."""
        rows = await self.get_user_trades(wallet_address, limit=limit)
        if not rows:
            return []

        signals: list[WalletTradeSignal] = []
        for item in rows:
            if not isinstance(item, dict):
                continue

            trade_id = str(item.get("id") or item.get("tradeID") or item.get("tradeId") or "")
            if not trade_id:
                fallback_id = (
                    f"{item.get('transactionHash', '')}:"
                    f"{item.get('asset', '')}:"
                    f"{item.get('conditionId', '')}:"
                    f"{item.get('timestamp', '')}:"
                    f"{item.get('side', '')}:"
                    f"{item.get('size', '')}"
                )
                trade_id = fallback_id

            market_id = str(item.get("market") or item.get("marketId") or item.get("conditionId") or "")
            if not market_id:
                continue

            token_id = item.get("tokenID") or item.get("tokenId") or item.get("asset")
            outcome = str(item.get("outcome") or item.get("title") or "UNKNOWN")
            side = str(item.get("side") or item.get("type") or "buy").lower()

            raw_price = float(item.get("price") or item.get("pricePaid") or item.get("avgPrice") or 0.0)
            raw_size = float(
                item.get("size")
                or item.get("amount")
                or item.get("usdcValue")
                or item.get("notional")
                or item.get("sizeUsd")
                or 0.0
            )
            profit_usd = item.get("pnl") or item.get("profit") or item.get("realizedPnl")
            try:
                profit_value = float(profit_usd) if profit_usd is not None else None
            except (TypeError, ValueError):
                profit_value = None

            ts_value = item.get("timestamp") or item.get("createdAt") or item.get("time")
            traded_at = self._parse_timestamp(ts_value)

            if raw_price <= 0 or raw_size <= 0:
                continue

            signals.append(
                WalletTradeSignal(
                    external_trade_id=trade_id,
                    wallet_address=wallet_address,
                    market_id=market_id,
                    token_id=str(token_id) if token_id is not None else None,
                    outcome=outcome,
                    side=side,
                    price_cents=ensure_price_in_cents(raw_price),
                    size_usd=raw_size,
                    traded_at=traded_at,
                    profit_usd=profit_value,
                    market_slug=str(
                        item.get("marketSlug") or item.get("market_slug") or item.get("slug") or ""
                    ).strip()
                    or None,
                    market_category=str(
                        item.get("marketCategory") or item.get("category") or ""
                    ).strip()
                    or None,
                )
            )

        signals.sort(key=lambda x: x.traded_at, reverse=True)
        return signals

    async def fetch_wallet_open_positions(
        self,
        wallet_address: str,
        *,
        limit: int = 200,
        size_threshold: float = 0.1,
    ) -> list[WalletOpenPosition] | None:
        """Fetch wallet open positions from data-api.

        Returns:
            list[WalletOpenPosition]: Successful fetch (can be empty).
            None: Upstream/API failure, caller should avoid reconciliation decisions.
        """

        normalized_wallet = self._normalize_address(wallet_address)
        if normalized_wallet is None:
            return []

        url = f"{settings.polymarket_data_api_host.rstrip('/')}/positions"
        params = {
            "user": normalized_wallet,
            "sizeThreshold": str(size_threshold),
            "limit": max(1, min(limit, 500)),
            "offset": 0,
        }
        try:
            raw = await self._request_json("GET", url, params=params)
        except Exception as exc:
            logger.warning("Failed to fetch open positions for {}: {}", normalized_wallet, exc)
            return None

        rows = self._coerce_rows(raw)
        positions: list[WalletOpenPosition] = []
        for row in rows:
            parsed = self._parse_open_position_row(row, default_wallet=normalized_wallet)
            if parsed is None:
                continue
            if parsed.quantity <= 0:
                continue
            positions.append(parsed)
        return positions

    async def fetch_account_open_positions(self, *, limit: int = 200) -> list[WalletOpenPosition] | None:
        """Fetch open positions for the bot account (proxy/signer funder address)."""

        target = self._resolve_account_address()
        if target is None:
            return None
        return await self.fetch_wallet_open_positions(target, limit=limit, size_threshold=0.0)

    async def fetch_market_mid_price(self, market_id: str, token_id: str | None = None) -> float | None:
        """Fetch current market price in cents for mark-to-market and risk controls."""

        if token_id:
            token_candidates = [
                (f"{settings.polymarket_host.rstrip('/')}/midpoint", {"token_id": token_id}),
                (f"{settings.polymarket_host.rstrip('/')}/last-trade-price", {"token_id": token_id}),
                (f"{settings.polymarket_host.rstrip('/')}/book", {"token_id": token_id}),
            ]
            for url, params in token_candidates:
                try:
                    raw = await self._request_json("GET", url, params=params)
                except Exception:
                    continue

                price = self._extract_token_price(raw)
                if price is not None:
                    return ensure_price_in_cents(price)

        candidate_urls = [
            f"{settings.polymarket_gamma_host.rstrip('/')}/markets/{market_id}",
            f"{settings.polymarket_gamma_host.rstrip('/')}/events/{market_id}",
        ]

        for url in candidate_urls:
            try:
                raw = await self._request_json("GET", url)
            except Exception:
                continue

            price = self._extract_price(raw, token_id=token_id)
            if price is not None:
                return ensure_price_in_cents(price)

        return None

    async def fetch_orderbook(self, token_id: str | None) -> OrderbookSnapshot | None:
        """Fetch a token orderbook snapshot from the CLOB REST API."""

        normalized_token = str(token_id or "").strip()
        if not normalized_token:
            return None
        if normalized_token in self._missing_orderbooks:
            return None

        cached = self._get_market_ws_snapshot(normalized_token)
        if cached is not None:
            return cached

        if settings.execution_sidecar_enabled and settings.execution_sidecar_market_data_enabled and self._execution_sidecar is not None:
            try:
                snapshot = await self._execution_sidecar.fetch_orderbook(normalized_token)
            except Exception as exc:
                logger.debug("Execution sidecar orderbook fetch failed for {}: {}", normalized_token, exc)
            else:
                if snapshot is not None:
                    self._store_market_ws_snapshot(normalized_token, snapshot)
                    return snapshot

        if settings.polymarket_market_ws_enabled:
            await self.prime_market_data([normalized_token])
            cached = await self._wait_for_market_ws_snapshot(
                normalized_token,
                timeout_seconds=settings.polymarket_market_ws_bootstrap_timeout_seconds,
            )
            if cached is not None:
                return cached

        url = f"{settings.polymarket_host.rstrip('/')}/book"
        try:
            raw = await self._request_json("GET", url, params={"token_id": normalized_token})
        except Exception as exc:
            text = str(exc).lower()
            if "orderbook" in text and "does not exist" in text:
                self._missing_orderbooks.add(normalized_token)
                return None
            logger.warning("Failed to fetch orderbook for token {}: {}", normalized_token, exc)
            return None

        snapshot = self._parse_orderbook_snapshot(raw)
        if snapshot is None:
            logger.warning("Failed to parse orderbook for token {}", normalized_token)
            return None
        self._store_market_ws_snapshot(normalized_token, snapshot)
        return snapshot

    def get_cached_orderbook(self, token_id: str | None) -> OrderbookSnapshot | None:
        normalized_token = str(token_id or "").strip()
        if not normalized_token:
            return None
        return self._get_market_ws_snapshot(normalized_token)

    async def fetch_executable_snapshot(self, token_id: str | None) -> ExecutableSnapshot | None:
        normalized_token = str(token_id or "").strip()
        if not normalized_token:
            return None

        await self.register_hot_markets([normalized_token], source="fetch_executable_snapshot", priority=80)
        cached = self.get_cached_executable_snapshot(normalized_token)
        if cached is not None:
            return cached

        if (
            settings.execution_sidecar_enabled
            and settings.execution_sidecar_market_data_enabled
            and settings.execution_sidecar_executable_snapshot_enabled
            and self._execution_sidecar is not None
        ):
            try:
                snapshot = await self._execution_sidecar.fetch_executable_snapshot(normalized_token)
            except Exception as exc:
                logger.debug("Execution sidecar executable snapshot fetch failed for {}: {}", normalized_token, exc)
            else:
                if snapshot is not None:
                    self._store_executable_snapshot(normalized_token, snapshot)
                    return snapshot

        orderbook = self.get_cached_orderbook(normalized_token)
        if orderbook is None:
            orderbook = await self.fetch_orderbook(normalized_token)
        if orderbook is None:
            return None
        updated_at = self._market_ws_book_updated_at.get(normalized_token, datetime.now(tz=timezone.utc))
        snapshot = self._build_executable_snapshot_from_orderbook(orderbook, updated_at=updated_at)
        self._store_executable_snapshot(normalized_token, snapshot, updated_at=updated_at)
        return snapshot

    def get_cached_executable_snapshot(self, token_id: str | None) -> ExecutableSnapshot | None:
        normalized_token = str(token_id or "").strip()
        if not normalized_token:
            return None
        updated_at = self._executable_snapshot_updated_at.get(normalized_token)
        snapshot = self._executable_snapshots.get(normalized_token)
        ttl = timedelta(seconds=max(1, settings.polymarket_market_ws_cache_ttl_seconds))
        if updated_at is not None and snapshot is not None and datetime.now(tz=timezone.utc) - updated_at <= ttl:
            return snapshot
        if self._execution_sidecar is not None:
            with contextlib.suppress(Exception):
                pushed = self._execution_sidecar.get_cached_executable_snapshot(normalized_token)
                if pushed is not None:
                    self._store_executable_snapshot(normalized_token, pushed)
                    return pushed
        return None

    async def prime_market_data(self, token_ids: list[str | None]) -> None:
        normalized = {
            str(token_id).strip()
            for token_id in token_ids
            if str(token_id or "").strip() and str(token_id).strip() not in self._missing_orderbooks
        }
        if not normalized:
            return
        if settings.execution_sidecar_enabled and settings.execution_sidecar_market_data_enabled and self._execution_sidecar is not None:
            try:
                snapshots = await self._execution_sidecar.prime_market_data(sorted(normalized))
            except Exception as exc:
                logger.debug("Execution sidecar market prime failed: {}", exc)
            else:
                for token_id, snapshot in snapshots.items():
                    if snapshot is not None:
                        self._store_market_ws_snapshot(token_id, snapshot)
                missing = [token_id for token_id, snapshot in snapshots.items() if snapshot is None]
                for token_id in missing:
                    self._missing_orderbooks.add(token_id)
                if settings.execution_sidecar_executable_snapshot_enabled:
                    with contextlib.suppress(Exception):
                        await self.prime_executable_market_data(sorted(normalized))
                # If every requested token already has a snapshot, do not do extra Python-side priming.
                if snapshots and all(token_id in snapshots and snapshots[token_id] is not None for token_id in normalized):
                    return
        if not settings.polymarket_market_ws_enabled:
            return
        async with self._market_ws_lock:
            newly_desired = normalized - self._market_ws_desired_assets
            if not newly_desired:
                return
            self._market_ws_desired_assets.update(newly_desired)
            for token_id in newly_desired:
                self._market_ws_waiters.setdefault(token_id, asyncio.Event())
            self._market_ws_wakeup.set()
        await self._send_market_ws_subscription(sorted(newly_desired))

    async def prime_executable_market_data(self, token_ids: list[str | None]) -> None:
        normalized = sorted({
            str(token_id).strip()
            for token_id in token_ids
            if str(token_id or "").strip()
        })
        if not normalized:
            return
        await self.register_hot_markets(normalized, source="prime_executable_market_data", priority=90)
        if not (
            settings.execution_sidecar_enabled
            and settings.execution_sidecar_market_data_enabled
            and settings.execution_sidecar_executable_snapshot_enabled
            and self._execution_sidecar is not None
        ):
            return
        try:
            snapshots = await self._execution_sidecar.prime_executable_market_data(normalized)
        except Exception as exc:
            logger.debug("Execution sidecar executable market prime failed: {}", exc)
            return
        for token_id, snapshot in snapshots.items():
            if snapshot is not None:
                self._store_executable_snapshot(token_id, snapshot)

    async def register_hot_markets(self, token_ids: list[str | None], *, source: str, priority: int = 0) -> int:
        normalized = sorted({
            str(token_id).strip()
            for token_id in token_ids
            if str(token_id or "").strip()
        })
        if not normalized:
            return 0
        if not (
            settings.execution_sidecar_enabled
            and settings.execution_sidecar_hot_market_registry_enabled
            and self._execution_sidecar is not None
        ):
            return 0
        try:
            return await self._execution_sidecar.register_hot_markets(
                normalized,
                ttl_seconds=settings.execution_sidecar_hot_market_ttl_seconds,
                source=source,
                priority=priority,
            )
        except Exception as exc:
            logger.debug("Execution sidecar hot market registration failed: {}", exc)
            return 0

    async def scan_hot_wallet_trades_via_sidecar(
        self,
        wallet_addresses: list[str],
        *,
        signal_limit: int,
    ) -> dict[str, list[WalletTradeSignal]] | None:
        normalized = [wallet_address.strip() for wallet_address in wallet_addresses if wallet_address and wallet_address.strip()]
        if not normalized:
            return {}
        if not (
            settings.execution_sidecar_enabled
            and settings.execution_sidecar_hot_signal_ingest_enabled
            and self._execution_sidecar is not None
        ):
            return None
        try:
            return await self._execution_sidecar.scan_hot_wallet_trades(
                normalized,
                signal_limit=signal_limit,
                hot_market_ttl_seconds=settings.execution_sidecar_hot_market_ttl_seconds,
            )
        except Exception as exc:
            logger.debug("Execution sidecar hot wallet scan failed: {}", exc)
            return None

    async def build_execution_plan_via_sidecar(self, request: Any) -> Any | None:
        if not (
            settings.execution_sidecar_enabled
            and settings.execution_sidecar_execution_plan_enabled
            and self._execution_sidecar is not None
        ):
            return None
        try:
            return await self._execution_sidecar.build_execution_plan(request)
        except Exception as exc:
            logger.debug("Execution sidecar plan build failed for {}: {}", request.external_trade_id, exc)
            return None

    async def reconcile_fills_via_sidecar(self, request: Any) -> Any | None:
        if not (
            settings.execution_sidecar_enabled
            and settings.execution_sidecar_fill_reconcile_enabled
            and self._execution_sidecar is not None
        ):
            return None
        try:
            return await self._execution_sidecar.reconcile_fills(request)
        except Exception as exc:
            logger.debug("Execution sidecar fill reconcile failed: {}", exc)
            return None

    async def _run_market_ws_loop(self) -> None:
        while not self._market_ws_stop.is_set():
            if not self._market_ws_desired_assets:
                self._market_ws_wakeup.clear()
                try:
                    await asyncio.wait_for(self._market_ws_wakeup.wait(), timeout=1.0)
                except TimeoutError:
                    continue
                if self._market_ws_stop.is_set():
                    break

            session = await self._ensure_session()
            try:
                async with session.ws_connect(
                    settings.polymarket_market_ws_url,
                    heartbeat=None,
                    autoping=True,
                ) as websocket:
                    self._market_ws = websocket
                    self._market_ws_subscribed_assets.clear()
                    self._market_ws_ping_task = asyncio.create_task(
                        self._run_market_ws_ping_loop(websocket),
                        name="polymarket-market-ws-ping",
                    )
                    await self._send_market_ws_subscription(list(self._market_ws_desired_assets), initial_dump=True)
                    async for message in websocket:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            self._handle_market_ws_message(message.data)
                            continue
                        if message.type in {
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Polymarket market websocket disconnected: {}", exc)
            finally:
                if self._market_ws_ping_task is not None:
                    self._market_ws_ping_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._market_ws_ping_task
                    self._market_ws_ping_task = None
                self._market_ws = None
                self._market_ws_subscribed_assets.clear()

            if not self._market_ws_stop.is_set():
                await asyncio.sleep(settings.polymarket_market_ws_reconnect_seconds)

    async def _run_market_ws_ping_loop(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        while not self._market_ws_stop.is_set() and not websocket.closed:
            await asyncio.sleep(max(1, settings.polymarket_market_ws_ping_seconds))
            async with self._market_ws_send_lock:
                if websocket.closed:
                    return
                await websocket.send_str("PING")

    async def _send_market_ws_subscription(self, token_ids: list[str], *, initial_dump: bool = False) -> None:
        if not token_ids:
            return
        websocket = self._market_ws
        if websocket is None or websocket.closed:
            return
        token_ids = [token_id for token_id in token_ids if token_id not in self._market_ws_subscribed_assets]
        if not token_ids:
            return
        payload = {
            "assets_ids": token_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }
        if initial_dump:
            payload["initial_dump"] = True
        async with self._market_ws_send_lock:
            if websocket.closed:
                return
            await websocket.send_json(payload)
        self._market_ws_subscribed_assets.update(token_ids)

    def _handle_market_ws_message(self, message: str) -> None:
        if not message or message == "PONG":
            return
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            logger.debug("Ignoring non-JSON market ws message: {}", message[:200])
            return
        self._handle_market_ws_payload(payload)

    def _handle_market_ws_payload(self, payload: Any) -> None:
        if isinstance(payload, list):
            for item in payload:
                self._handle_market_ws_payload(item)
            return
        if not isinstance(payload, dict):
            return

        event_type = str(payload.get("event_type") or payload.get("type") or "").lower()
        asset_id = str(payload.get("asset_id") or payload.get("asset") or payload.get("token_id") or "").strip()

        if event_type == "book" and asset_id:
            snapshot = self._parse_orderbook_snapshot(payload)
            if snapshot is not None:
                self._store_market_ws_snapshot(asset_id, snapshot)
            return

        if event_type == "price_change" and asset_id:
            self._apply_market_ws_price_change(asset_id, payload)
            return

        if event_type == "best_bid_ask" and asset_id:
            self._apply_market_ws_best_bid_ask(asset_id, payload)

    def _store_market_ws_snapshot(self, token_id: str, snapshot: OrderbookSnapshot) -> None:
        normalized_token = str(token_id).strip()
        updated_at = datetime.now(tz=timezone.utc)
        self._market_ws_books[normalized_token] = {
            "bids": {level.price: level.size for level in snapshot.bids},
            "asks": {level.price: level.size for level in snapshot.asks},
        }
        self._market_ws_book_updated_at[normalized_token] = updated_at
        self._store_executable_snapshot(
            normalized_token,
            self._build_executable_snapshot_from_orderbook(snapshot, updated_at=updated_at),
            updated_at=updated_at,
        )
        waiter = self._market_ws_waiters.get(normalized_token)
        if waiter is not None:
            waiter.set()

    def _apply_market_ws_price_change(self, token_id: str, payload: dict[str, Any]) -> None:
        book = self._market_ws_books.setdefault(token_id, {"bids": {}, "asks": {}})
        updates = payload.get("changes") or payload.get("price_changes") or payload.get("priceChanges") or [payload]
        if not isinstance(updates, list):
            updates = [updates]
        updated = False
        for change in updates:
            if not isinstance(change, dict):
                continue
            side = str(change.get("side") or change.get("book_side") or "").lower()
            price_raw = change.get("price") or change.get("p")
            size_raw = (
                change.get("size")
                or change.get("quantity")
                or change.get("amount")
                or change.get("shares")
            )
            try:
                price = float(price_raw)
                size = float(size_raw)
            except (TypeError, ValueError):
                continue
            if price > 1.0:
                price /= 100.0
            price = self._validate_price(price)
            if price is None:
                continue
            side_key = "bids" if side in {"buy", "bid", "bids"} else "asks"
            if size <= 0:
                book[side_key].pop(price, None)
            else:
                book[side_key][price] = size
            updated = True
        if updated:
            updated_at = datetime.now(tz=timezone.utc)
            self._market_ws_book_updated_at[token_id] = updated_at
            snapshot = self._build_snapshot_from_market_ws_book(book)
            if snapshot is not None:
                self._store_executable_snapshot(
                    token_id,
                    self._build_executable_snapshot_from_orderbook(snapshot, updated_at=updated_at),
                    updated_at=updated_at,
                )
            waiter = self._market_ws_waiters.get(token_id)
            if waiter is not None:
                waiter.set()

    def _apply_market_ws_best_bid_ask(self, token_id: str, payload: dict[str, Any]) -> None:
        book = self._market_ws_books.setdefault(token_id, {"bids": {}, "asks": {}})
        best_bid = self._normalize_ws_price(payload.get("best_bid") or payload.get("bestBid"))
        best_ask = self._normalize_ws_price(payload.get("best_ask") or payload.get("bestAsk"))
        bid_size = self._parse_float(payload.get("best_bid_size") or payload.get("bestBidSize")) or 0.0
        ask_size = self._parse_float(payload.get("best_ask_size") or payload.get("bestAskSize")) or 0.0
        updated = False
        if best_bid is not None:
            if bid_size <= 0:
                book["bids"].pop(best_bid, None)
            else:
                book["bids"][best_bid] = bid_size
            updated = True
        if best_ask is not None:
            if ask_size <= 0:
                book["asks"].pop(best_ask, None)
            else:
                book["asks"][best_ask] = ask_size
            updated = True
        if updated:
            updated_at = datetime.now(tz=timezone.utc)
            self._market_ws_book_updated_at[token_id] = updated_at
            snapshot = self._build_snapshot_from_market_ws_book(book)
            if snapshot is not None:
                self._store_executable_snapshot(
                    token_id,
                    self._build_executable_snapshot_from_orderbook(snapshot, updated_at=updated_at),
                    updated_at=updated_at,
                )
            waiter = self._market_ws_waiters.get(token_id)
            if waiter is not None:
                waiter.set()

    def _get_market_ws_snapshot(self, token_id: str) -> OrderbookSnapshot | None:
        updated_at = self._market_ws_book_updated_at.get(token_id)
        book = self._market_ws_books.get(token_id)
        if updated_at is None or book is None:
            return None
        ttl = timedelta(seconds=max(1, settings.polymarket_market_ws_cache_ttl_seconds))
        if datetime.now(tz=timezone.utc) - updated_at > ttl:
            return None
        return self._build_snapshot_from_market_ws_book(book)

    def _store_executable_snapshot(
        self,
        token_id: str,
        snapshot: ExecutableSnapshot,
        *,
        updated_at: datetime | None = None,
    ) -> None:
        normalized_token = str(token_id).strip()
        self._executable_snapshots[normalized_token] = snapshot
        self._executable_snapshot_updated_at[normalized_token] = updated_at or datetime.now(tz=timezone.utc)

    @classmethod
    def _build_executable_snapshot_from_orderbook(
        cls,
        orderbook: OrderbookSnapshot,
        *,
        updated_at: datetime | None = None,
    ) -> ExecutableSnapshot:
        top5_ask_liquidity_usd = round(sum(level.price * level.size for level in orderbook.asks[:5]), 4)
        top5_bid_liquidity_usd = round(sum(level.price * level.size for level in orderbook.bids[:5]), 4)
        timestamp = updated_at or datetime.now(tz=timezone.utc)
        return ExecutableSnapshot(
            best_bid=orderbook.best_bid,
            best_ask=orderbook.best_ask,
            buy_vwap_5usd=cls._compute_orderbook_vwap(orderbook.asks, 5.0),
            buy_vwap_10usd=cls._compute_orderbook_vwap(orderbook.asks, 10.0),
            buy_vwap_25usd=cls._compute_orderbook_vwap(orderbook.asks, 25.0),
            sell_vwap_5usd=cls._compute_orderbook_vwap(orderbook.bids, 5.0),
            sell_vwap_10usd=cls._compute_orderbook_vwap(orderbook.bids, 10.0),
            sell_vwap_25usd=cls._compute_orderbook_vwap(orderbook.bids, 25.0),
            top5_ask_liquidity_usd=top5_ask_liquidity_usd,
            top5_bid_liquidity_usd=top5_bid_liquidity_usd,
            has_book=bool(orderbook.bids or orderbook.asks),
            last_update_ts=timestamp.timestamp(),
            snapshot_age_ms=0,
        )

    @staticmethod
    def _compute_orderbook_vwap(levels: list[OrderbookLevel], target_usd: float) -> float | None:
        remaining_usd = float(target_usd)
        total_size = 0.0
        total_usd = 0.0
        for level in levels:
            if remaining_usd <= 0:
                break
            level_usd = level.price * level.size
            if level.price <= 0 or level.size <= 0 or level_usd <= 0:
                continue
            take_usd = min(level_usd, remaining_usd)
            take_size = take_usd / level.price
            total_usd += take_usd
            total_size += take_size
            remaining_usd -= take_usd
        if remaining_usd > 1e-9 or total_size <= 0:
            return None
        return round(total_usd / total_size, 4)

    async def _wait_for_market_ws_snapshot(
        self,
        token_id: str,
        *,
        timeout_seconds: float,
    ) -> OrderbookSnapshot | None:
        cached = self._get_market_ws_snapshot(token_id)
        if cached is not None:
            return cached
        if timeout_seconds <= 0:
            return None
        waiter = self._market_ws_waiters.setdefault(token_id, asyncio.Event())
        waiter.clear()
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return None
        return self._get_market_ws_snapshot(token_id)

    @classmethod
    def _build_snapshot_from_market_ws_book(cls, book: dict[str, dict[float, float]]) -> OrderbookSnapshot | None:
        bids = [
            OrderbookLevel(price=price, size=size)
            for price, size in sorted(book.get("bids", {}).items(), key=lambda item: item[0], reverse=True)
            if price > 0 and size > 0
        ]
        asks = [
            OrderbookLevel(price=price, size=size)
            for price, size in sorted(book.get("asks", {}).items(), key=lambda item: item[0])
            if price > 0 and size > 0
        ]
        if not bids and not asks:
            return None
        return OrderbookSnapshot(
            bids=bids,
            asks=asks,
            best_bid=bids[0].price if bids else None,
            best_ask=asks[0].price if asks else None,
        )

    @classmethod
    def _normalize_ws_price(cls, value: Any) -> float | None:
        parsed = cls._parse_float(value)
        if parsed is None:
            return None
        if parsed > 1.0:
            parsed /= 100.0
        return cls._validate_price(parsed)

    @staticmethod
    def _parse_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    async def fetch_market_info(self, market_id: str) -> MarketInfoData:
        """Fetch human-readable market question and category.

        Tries multiple API endpoints in order of reliability:
          1. CLOB API  GET /markets/{condition_id}          – most reliable, path-based
          2. Gamma API GET /markets/{market_id}             – path-based
          3. Gamma API GET /events/{market_id}              – event-level fallback
          4. Gamma API GET /markets?condition_id={id}       – filtered list (snake_case)
          5. Gamma API GET /markets?conditionId={id}        – filtered list (camelCase)

        For list responses we match by conditionId field; a list with a single
        item is used directly (the API filtered correctly).  A multi-item list
        with no ID match is skipped so we never return a mismatched market.
        Returns empty strings on all failures so callers show the raw market_id.
        """

        clob = settings.polymarket_host.rstrip("/")
        gamma = settings.polymarket_gamma_host.rstrip("/")
        target_id = market_id.strip().lower()

        candidate_requests: list[tuple[str, dict]] = [
            # 1. CLOB REST API – accepts conditionId directly in the path
            (f"{clob}/markets/{market_id}", {}),
            # 2-3. Gamma path-based
            (f"{gamma}/markets/{market_id}", {}),
            (f"{gamma}/events/{market_id}", {}),
            # 4-5. Gamma query-param variants (may return filtered list)
            (f"{gamma}/markets", {"condition_id": market_id}),
            (f"{gamma}/markets", {"conditionId": market_id}),
        ]

        question = ""
        category = ""

        for url, params in candidate_requests:
            try:
                raw = await self._request_json("GET", url, params=params or None)
            except Exception:
                continue

            market_obj = self._extract_market_obj(raw, target_id)
            if market_obj is None:
                continue

            question = str(
                market_obj.get("question")
                or market_obj.get("groupItemTitle")
                or market_obj.get("title")
                or ""
            )
            tags = market_obj.get("tags")
            category = str(
                market_obj.get("category")
                or (tags[0] if isinstance(tags, list) and tags else "")
            )

            if question:
                logger.debug("Resolved market info for {} from {}: {}", target_id, url, question[:60])
                break

        return MarketInfoData(market_id=market_id, question=question, category=category)

    @staticmethod
    def _extract_market_obj(raw: Any, target_id: str) -> dict | None:
        """Extract the matching market dict from a raw API response.

        Returns None if the response cannot be reliably attributed to target_id
        (e.g., an unfiltered list of multiple markets).
        """
        if isinstance(raw, dict):
            # Single-dict response: use it directly (path-based endpoints)
            return raw
        if isinstance(raw, list):
            dict_items = [item for item in raw if isinstance(item, dict)]
            if not dict_items:
                return None
            # Try exact match on conditionId field
            for item in dict_items:
                cid = str(
                    item.get("conditionId") or item.get("condition_id") or ""
                ).strip().lower()
                if cid == target_id:
                    return item
            # Single item → trust that the API filtered correctly
            if len(dict_items) == 1:
                return dict_items[0]
            # Multiple items with no ID match → API ignored our filter; skip
        return None

    async def fetch_account_balance_usd(self) -> float | None:
        """Fetch current account balance for capital recalculation."""

        if self._dry_run:
            return settings.default_starting_equity

        if not settings.polymarket_private_key:
            return None

        try:
            return await asyncio.to_thread(self._fetch_account_balance_sync)
        except Exception as exc:
            logger.warning("Failed to fetch account balance: {}", exc)
            return None

    async def fetch_live_account_balances(self) -> dict[str, Any]:
        """Fetch account balances strictly from Polymarket sources.

        Returns free collateral (cash), open positions current value, open-order
        reserve, and total equity snapshot derived from Polymarket APIs.
        """

        if self._dry_run:
            return {
                "source": "dry_run",
                "free_balance_usd": None,
                "net_free_balance_usd": None,
                "open_orders_reserved_usd": None,
                "positions_value_usd": None,
                "total_balance_usd": None,
                "tradable_collateral_usd": None,
                "positions_count": 0,
                "open_orders_count": 0,
                "funding_blocker": None,
            }

        free_balance = await self.fetch_account_balance_usd()
        open_positions = await self.fetch_account_open_positions(limit=500)
        open_orders = await self.fetch_open_orders()
        onchain_funding = await self.fetch_onchain_funding_snapshot()

        positions_value: float | None = None
        positions_count = 0
        if open_positions is not None:
            # Count only meaningful positions (current value >= $0.05) to
            # match what Polymarket UI shows and exclude dust.
            positions_count = len([
                row for row in open_positions
                if row.quantity > 0 and row.current_value_usd >= 0.05
            ])
            positions_value = round(
                sum(max(float(row.current_value_usd), 0.0) for row in open_positions),
                4,
            )

        open_orders_reserved: float | None = None
        open_orders_count = 0
        if open_orders is not None:
            open_orders_count = len(open_orders)
            open_orders_reserved = round(
                sum(
                    max((float(order.price_cents) / 100.0) * max(float(order.size), 0.0), 0.0)
                    for order in open_orders
                ),
                4,
            )

        total_balance: float | None = None
        if free_balance is not None and positions_value is not None:
            total_balance = round(float(free_balance) + positions_value, 4)

        net_free_balance: float | None = None
        if free_balance is not None and open_orders_reserved is not None:
            # SAFETY: use a conservative free-cash estimate for sizing.
            net_free_balance = round(max(float(free_balance) - float(open_orders_reserved), 0.0), 4)

        funding_blocker = None
        unwrapped_usdce_balance: float | None = None
        if isinstance(onchain_funding, dict):
            funder_pusd = onchain_funding.get("funder_pusd_balance_usd")
            funder_usdce = onchain_funding.get("funder_usdce_balance_usd")
            if isinstance(funder_usdce, (int, float)):
                unwrapped_usdce_balance = round(max(float(funder_usdce), 0.0), 4)
            if (
                isinstance(funder_pusd, (int, float))
                and isinstance(funder_usdce, (int, float))
                and float(funder_pusd) <= 0.000001
                and float(funder_usdce) >= 0.01
            ):
                funding_blocker = "usdce_not_wrapped_to_pusd"
        if unwrapped_usdce_balance is not None and free_balance is not None:
            total_balance = round(
                float(free_balance)
                + float(positions_value or 0.0)
                + unwrapped_usdce_balance,
                4,
            )

        return {
            "source": "polymarket",
            "free_balance_usd": round(float(free_balance), 4) if free_balance is not None else None,
            "net_free_balance_usd": net_free_balance,
            "open_orders_reserved_usd": open_orders_reserved,
            "positions_value_usd": positions_value,
            "total_balance_usd": total_balance,
            "tradable_collateral_usd": round(float(free_balance), 4) if free_balance is not None else None,
            "unwrapped_usdce_balance_usd": unwrapped_usdce_balance,
            "positions_count": positions_count,
            "open_orders_count": open_orders_count,
            "funding_blocker": funding_blocker,
            "onchain_funding": onchain_funding,
            "balance_is_authoritative": True,
        }

    async def fetch_onchain_funding_snapshot(self) -> dict[str, Any] | None:
        """Read pUSD/USDC.e balances directly from Polygon for funding diagnostics."""

        if self._dry_run or not settings.polymarket_onchain_balance_enabled:
            return None
        if not settings.polymarket_private_key:
            return None

        try:
            return await asyncio.to_thread(self._fetch_onchain_funding_snapshot_sync)
        except Exception as exc:
            logger.warning("Failed to fetch onchain Polymarket funding snapshot: {}", exc)
            return None

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Place an order through py-clob-client-v2 or simulate in DRY_RUN mode."""

        if self._dry_run:
            simulated_id = f"dry-{int(datetime.now(tz=timezone.utc).timestamp())}"
            return OrderResult(success=True, order_id=simulated_id, tx_hash=simulated_id)

        if not settings.polymarket_private_key:
            return OrderResult(success=False, order_id=None, tx_hash=None, error="Missing POLYMARKET_PRIVATE_KEY")

        if not request.token_id:
            return OrderResult(success=False, order_id=None, tx_hash=None, error="Missing token_id for live order")

        price_decimal = self._validate_price(request.price_cents / 100.0)
        if price_decimal is None:
            return OrderResult(success=False, order_id=None, tx_hash=None, error="invalid_price")

        if request.token_id in self._missing_orderbooks:
            return OrderResult(
                success=False,
                order_id=None,
                tx_hash=None,
                error=f"orderbook_not_found:{request.token_id}",
            )

        try:
            response = await asyncio.to_thread(self._place_order_sync, request)
            if isinstance(response, dict) and response.get("success") is False:
                error = response.get("errorMsg") or response.get("error") or response.get("message") or "order_rejected"
                return OrderResult(success=False, order_id=None, tx_hash=None, error=str(error))
            order_id = str(response.get("orderID") or response.get("id") or "")
            tx_hashes = response.get("transactionsHashes") if isinstance(response, dict) else None
            tx_hash = str(
                response.get("transactionHash")
                or response.get("txHash")
                or (tx_hashes[0] if isinstance(tx_hashes, list) and tx_hashes else "")
                or order_id
            )
            return OrderResult(success=True, order_id=order_id or None, tx_hash=tx_hash or None)
        except Exception as exc:
            text = str(exc).lower()
            if "orderbook" in text and "does not exist" in text:
                self._missing_orderbooks.add(request.token_id)
                logger.warning(
                    "Token orderbook does not exist; caching as invalid token_id={} market_id={}",
                    request.token_id,
                    request.market_id,
                )
                return OrderResult(
                    success=False,
                    order_id=None,
                    tx_hash=None,
                    error=f"orderbook_not_found:{request.token_id}",
                )
            if "not enough balance / allowance" in text or "not_enough_balance" in text:
                logger.warning("Insufficient balance/allowance for token_id={} market_id={}", request.token_id, request.market_id)
                return OrderResult(
                    success=False,
                    order_id=None,
                    tx_hash=None,
                    error="insufficient_balance_allowance",
                )
            logger.exception("Order placement failed")
            return OrderResult(success=False, order_id=None, tx_hash=None, error=str(exc))

    async def fetch_open_orders(
        self,
        *,
        market_id: str | None = None,
        token_id: str | None = None,
    ) -> list[OpenOrderInfo] | None:
        """Fetch current open orders for the authenticated account."""

        if self._dry_run:
            return []
        if not settings.polymarket_private_key:
            return None

        rows: list[dict[str, Any]] | None = None
        if (
            settings.execution_sidecar_enabled
            and settings.execution_sidecar_authenticated_reads_enabled
            and self._execution_sidecar is not None
        ):
            try:
                rows = await self._execution_sidecar.fetch_open_orders(
                    market_id=market_id,
                    token_id=token_id,
                )
            except Exception as exc:
                logger.debug("Sidecar open orders fetch failed, falling back to Python path: {}", exc)
        if rows is None:
            try:
                rows = await asyncio.to_thread(self._fetch_open_orders_sync, market_id, token_id)
            except Exception as exc:
                logger.warning("Failed to fetch open orders: {}", exc)
                return None

        now = datetime.now(tz=timezone.utc)
        orders: list[OpenOrderInfo] = []
        for row in rows:
            parsed = self._parse_open_order_row(row, now=now)
            if parsed is not None:
                orders.append(parsed)
        return orders

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel one order by id."""

        if self._dry_run:
            return True
        if not settings.polymarket_private_key:
            return False
        if not order_id:
            return False
        try:
            return bool(await asyncio.to_thread(self._cancel_order_sync, order_id))
        except Exception as exc:
            logger.warning("Failed to cancel order {}: {}", order_id, exc)
            return False

    async def is_order_open(self, order_id: str) -> bool | None:
        """Return whether an order is currently open.

        Returns:
            True/False on successful fetch, None on upstream/API errors.
        """

        if self._dry_run:
            return False
        if not settings.polymarket_private_key:
            return None
        if not order_id:
            return None
        payload: dict[str, Any] | None = None
        if (
            settings.execution_sidecar_enabled
            and settings.execution_sidecar_authenticated_reads_enabled
            and self._execution_sidecar is not None
        ):
            try:
                payload = await self._execution_sidecar.fetch_order_status(order_id)
            except Exception as exc:
                logger.debug("Sidecar order status fetch failed, falling back to Python path: {}", exc)
        if payload is None:
            try:
                payload = await asyncio.to_thread(self._get_order_sync, order_id)
            except Exception as exc:
                logger.warning("Failed to fetch order {}: {}", order_id, exc)
                return None
        return self._is_open_order_payload(payload)

    async def fetch_account_fills(
        self,
        *,
        after_ts: datetime,
        market_id: str | None = None,
        token_id: str | None = None,
        side: str | None = None,
        order_id: str | None = None,
        limit: int = 200,
    ) -> list[FillInfo] | None:
        """Fetch authenticated account fills from CLOB.

        This is the authoritative source for filled/partial trade states.
        """

        if self._dry_run:
            return []
        if not settings.polymarket_private_key:
            return None

        safe_limit = max(1, min(limit, 500))
        rows: list[dict[str, Any]] | None = None
        if (
            settings.execution_sidecar_enabled
            and settings.execution_sidecar_authenticated_reads_enabled
            and self._execution_sidecar is not None
        ):
            try:
                rows = await self._execution_sidecar.fetch_account_fills(
                    after_ts=after_ts,
                    market_id=market_id,
                    token_id=token_id,
                    limit=safe_limit,
                )
            except Exception as exc:
                logger.debug("Sidecar account fills fetch failed, falling back to Python path: {}", exc)
        if rows is None:
            try:
                rows = await asyncio.to_thread(
                    self._fetch_account_fills_sync,
                    after_ts,
                    market_id,
                    token_id,
                    safe_limit,
                )
            except Exception as exc:
                logger.warning("Failed to fetch account fills: {}", exc)
                return None

        target_side = side.lower() if isinstance(side, str) else None
        target_order_id = order_id.strip() if isinstance(order_id, str) and order_id.strip() else None
        fills: list[FillInfo] = []
        for row in rows:
            parsed = self._parse_fill_row(row)
            if parsed is None:
                continue
            if parsed.traded_at < after_ts:
                continue
            if market_id and parsed.market_id != market_id:
                continue
            if token_id and parsed.token_id != token_id:
                continue
            if target_side and parsed.side != target_side:
                continue
            if target_order_id and parsed.order_id != target_order_id:
                continue
            fills.append(parsed)
            if len(fills) >= safe_limit:
                break
        fills.sort(key=lambda row: row.traded_at, reverse=True)
        return fills

    async def cancel_stale_orders(
        self,
        *,
        stale_after_seconds: int,
        max_cancel: int,
        allowed_order_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Cancel stale open orders older than the configured TTL."""

        if self._dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "scanned": 0,
                "stale": 0,
                "cancelled": 0,
                "cancelled_ids": [],
                "failed": 0,
                "failed_ids": [],
            }

        open_orders = await self.fetch_open_orders()
        if open_orders is None:
            return {
                "ok": False,
                "dry_run": False,
                "error": "open_orders_fetch_failed",
                "scanned": 0,
                "stale": 0,
                "cancelled": 0,
                "cancelled_ids": [],
                "failed": 0,
                "failed_ids": [],
            }

        now = datetime.now(tz=timezone.utc)
        threshold = max(stale_after_seconds, 60)
        allowed_ids = (
            {order_id.strip() for order_id in allowed_order_ids if order_id and order_id.strip()}
            if allowed_order_ids is not None
            else None
        )
        if allowed_ids is None:
            filtered_orders = list(open_orders)
        else:
            filtered_orders = [order for order in open_orders if order.order_id in allowed_ids]
        stale_orders = [
            order
            for order in filtered_orders
            if (now - order.created_at).total_seconds() >= threshold
        ]
        stale_orders.sort(key=lambda row: row.created_at)

        to_cancel = stale_orders[: max(1, max_cancel)]
        cancelled = 0
        cancelled_ids: list[str] = []
        failed_ids: list[str] = []
        for order in to_cancel:
            try:
                success = await asyncio.to_thread(self._cancel_order_sync, order.order_id)
            except Exception as exc:
                logger.warning("Failed to cancel stale order {}: {}", order.order_id, exc)
                success = False
            if success:
                cancelled += 1
                cancelled_ids.append(order.order_id)
            else:
                failed_ids.append(order.order_id)

        return {
            "ok": True,
            "dry_run": False,
            "scanned": len(filtered_orders),
            "tracked_open_orders": len(filtered_orders),
            "stale": len(stale_orders),
            "cancelled": cancelled,
            "cancelled_ids": cancelled_ids,
            "failed": len(failed_ids),
            "failed_ids": failed_ids,
        }

    async def diagnose_live_credentials(self) -> dict[str, Any]:
        """Validate live CLOB auth path using current runtime credentials."""

        if self._dry_run:
            return {"ok": False, "code": "engine_is_dry_run"}
        if not settings.polymarket_private_key:
            return {"ok": False, "code": "missing_private_key"}

        try:
            payload = await asyncio.to_thread(self._diagnose_live_credentials_sync)
            return {"ok": True, **payload}
        except Exception as exc:
            text = str(exc)
            lower = text.lower()
            if ("unauthorized" in lower or "invalid api key" in lower) and self._clob_client is not None:
                try:
                    payload = await asyncio.to_thread(self._diagnose_with_derived_retry_sync)
                    return {"ok": True, "code": "ok_after_derived_retry", **payload}
                except Exception as retry_exc:
                    text = str(retry_exc)
                    lower = text.lower()
            if "unauthorized" in lower or "invalid api key" in lower:
                code = "invalid_api_credentials"
            elif "not enough balance / allowance" in lower:
                code = "insufficient_balance_allowance"
            else:
                code = "diagnostic_failed"
            return {"ok": False, "code": code, "error": text}

    def _diagnose_live_credentials_sync(self) -> dict[str, Any]:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        clob_client = self._ensure_clob_client()
        response = clob_client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        balance = self._extract_collateral_balance(response)
        try:
            onchain_funding = self._fetch_onchain_funding_snapshot_sync()
        except Exception as exc:
            onchain_funding = {"error": str(exc)}
        signer_address = clob_client.signer.address() if getattr(clob_client, "signer", None) else None
        builder = getattr(clob_client, "builder", None)
        funder_address = getattr(builder, "funder", None) if builder is not None else None
        signature_type = self._builder_signature_type(builder)
        funding_blocker = self._funding_blocker_from_snapshot(onchain_funding)
        return {
            "code": "ok",
            "collateral_balance_usd": balance,
            "response_type": type(response).__name__,
            "creds_source": self._clob_creds_source or "unknown",
            "signer_address": signer_address,
            "funder_address": funder_address,
            "signature_type": signature_type,
            "funding_blocker": funding_blocker,
            "onchain_funding": onchain_funding,
        }

    def _diagnose_with_derived_retry_sync(self) -> dict[str, Any]:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        clob_client = self._ensure_clob_client()
        self._switch_to_derived_creds(clob_client)
        response = clob_client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        balance = self._extract_collateral_balance(response)
        try:
            onchain_funding = self._fetch_onchain_funding_snapshot_sync()
        except Exception as exc:
            onchain_funding = {"error": str(exc)}
        signer_address = clob_client.signer.address() if getattr(clob_client, "signer", None) else None
        builder = getattr(clob_client, "builder", None)
        funder_address = getattr(builder, "funder", None) if builder is not None else None
        signature_type = self._builder_signature_type(builder)
        return {
            "collateral_balance_usd": balance,
            "response_type": type(response).__name__,
            "creds_source": self._clob_creds_source or "unknown",
            "signer_address": signer_address,
            "funder_address": funder_address,
            "signature_type": signature_type,
            "funding_blocker": self._funding_blocker_from_snapshot(onchain_funding),
            "onchain_funding": onchain_funding,
        }

    def _place_order_sync(self, request: OrderRequest) -> dict[str, Any]:
        from py_clob_client_v2.clob_types import MarketOrderArgs, OrderArgs, OrderType
        import math

        clob_client = self._ensure_clob_client()

        side = "BUY" if request.side.lower() == "buy" else "SELL"
        price_decimal = self._validate_price(request.price_cents / 100.0)
        if price_decimal is None:
            raise ValueError("invalid_price")

        # IMPROVED: flexible fill mode — support IOC (FAK) and FOK order types
        _order_type_map = {"FOK": OrderType.FOK, "IOC": OrderType.FAK, "FAK": OrderType.FAK}
        clob_order_type = _order_type_map.get(request.order_type, OrderType.GTC)

        if side == "BUY" and clob_order_type != OrderType.GTC:
            # Use the market-order builder for taker-style BUYs so the signed
            # maker amount stays in pUSD terms with market-order precision.
            order_args = MarketOrderArgs(
                token_id=request.token_id,
                amount=round(max(request.size_usd, 0.01), 2),
                side=side,
                price=price_decimal,
                order_type=clob_order_type,
            )
            signed_order = clob_client.create_market_order(order_args)
        else:
            raw_size = max(request.size_usd / price_decimal, 1.0)
            size = math.floor(raw_size * 100) / 100
            order_args = OrderArgs(
                token_id=request.token_id,
                price=price_decimal,
                size=size,
                side=side,
            )
            signed_order = clob_client.create_order(order_args)
        try:
            return clob_client.post_order(signed_order, clob_order_type)
        except Exception as exc:
            text = str(exc).lower()
            if "unauthorized" in text or "invalid api key" in text:
                self._switch_to_derived_creds(clob_client)
                return clob_client.post_order(signed_order, clob_order_type)
            if "not enough balance / allowance" in text or "not_enough_balance" in text:
                self._refresh_collateral_allowance_sync()
                return clob_client.post_order(signed_order, clob_order_type)
            raise

    def _fetch_open_orders_sync(self, market_id: str | None, token_id: str | None) -> list[dict[str, Any]]:
        from py_clob_client_v2.clob_types import OpenOrderParams

        clob_client = self._ensure_clob_client()
        params = OpenOrderParams(
            market=market_id or None,
            asset_id=token_id or None,
        )
        response = clob_client.get_open_orders(params=params)
        if not isinstance(response, list):
            return []
        return [row for row in response if isinstance(row, dict)]

    def _get_order_sync(self, order_id: str) -> dict[str, Any] | None:
        clob_client = self._ensure_clob_client()
        response = clob_client.get_order(order_id)
        return response if isinstance(response, dict) else None

    def _fetch_account_fills_sync(
        self,
        after_ts: datetime,
        market_id: str | None,
        token_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        from py_clob_client_v2.clob_types import TradeParams

        clob_client = self._ensure_clob_client()
        after_unix = int(after_ts.timestamp())
        params = TradeParams(
            market=market_id or None,
            asset_id=token_id or None,
            after=max(after_unix, 0),
        )
        response = clob_client.get_trades(params=params)
        if not isinstance(response, list):
            return []
        rows = [row for row in response if isinstance(row, dict)]
        rows.sort(key=lambda row: self._parse_timestamp(row.get("timestamp") or row.get("createdAt")), reverse=True)
        return rows[:limit]

    def _cancel_order_sync(self, order_id: str) -> bool:
        from py_clob_client_v2.clob_types import OrderPayload

        clob_client = self._ensure_clob_client()
        if not order_id:
            return False
        response = clob_client.cancel_order(OrderPayload(orderID=order_id))
        if isinstance(response, dict):
            if str(response.get("canceled", "")).lower() == "true":
                return True
            if str(response.get("status", "")).lower() in {"ok", "success"}:
                return True
            if response.get("error"):
                return False
        # Some endpoints return non-uniform bodies; lack of exception is enough.
        return True

    # IMPROVED: status model — async wrappers for order lifecycle
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order by ID."""
        if self._dry_run or not order_id:
            return True
        try:
            return await asyncio.to_thread(self._cancel_order_sync, order_id)
        except Exception as exc:
            logger.warning("Failed to cancel order {}: {}", order_id, exc)
            return False

    async def get_order_status(self, order_id: str) -> dict | None:
        """Fetch order details from CLOB for fill status polling."""
        if self._dry_run:
            return {"status": "FILLED", "orderID": order_id}
        if not settings.polymarket_private_key or not order_id:
            return None
        try:
            return await asyncio.to_thread(self._get_order_status_sync, order_id)
        except Exception as exc:
            logger.warning("Failed to fetch order status for {}: {}", order_id, exc)
            return None

    def _get_order_status_sync(self, order_id: str) -> dict | None:
        clob_client = self._ensure_clob_client()
        response = clob_client.get_order(order_id)
        if isinstance(response, dict):
            return response
        return None

    def _fetch_account_balance_sync(self) -> float | None:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        # `get_balance_allowance` response schema can vary between API versions.
        clob_client = self._ensure_clob_client()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        response = clob_client.get_balance_allowance(params)
        balance = self._extract_collateral_balance(response)
        if balance is not None:
            return balance
        return None

    def _fetch_onchain_funding_snapshot_sync(self) -> dict[str, Any]:
        clob_client = self._ensure_clob_client()
        signer_address = clob_client.signer.address() if getattr(clob_client, "signer", None) else None
        funder_address = settings.polymarket_proxy_address or signer_address
        snapshot: dict[str, Any] = {
            "signer_address": signer_address,
            "funder_address": funder_address,
            "rpc_url": None,
        }
        for label, address in (("signer", signer_address), ("funder", funder_address)):
            if not address:
                continue
            snapshot[f"{label}_pusd_balance_usd"] = self._erc20_balance_usd(PUSD_TOKEN_ADDRESS, address)
            snapshot[f"{label}_usdce_balance_usd"] = self._erc20_balance_usd(USDCE_TOKEN_ADDRESS, address)
            snapshot[f"{label}_usdc_balance_usd"] = self._erc20_balance_usd(USDC_TOKEN_ADDRESS, address)
            snapshot[f"{label}_pusd_allowance_exchange_usd"] = self._erc20_allowance_usd(
                PUSD_TOKEN_ADDRESS,
                address,
                CTF_EXCHANGE_ADDRESS,
            )
            snapshot[f"{label}_pusd_allowance_neg_risk_exchange_usd"] = self._erc20_allowance_usd(
                PUSD_TOKEN_ADDRESS,
                address,
                NEG_RISK_CTF_EXCHANGE_ADDRESS,
            )
            snapshot[f"{label}_pusd_allowance_neg_risk_adapter_usd"] = self._erc20_allowance_usd(
                PUSD_TOKEN_ADDRESS,
                address,
                NEG_RISK_ADAPTER_ADDRESS,
            )
        snapshot["rpc_url"] = getattr(self, "_last_polygon_rpc_url", None)
        snapshot["funding_blocker"] = self._funding_blocker_from_snapshot(snapshot)
        return snapshot

    @staticmethod
    def _funding_blocker_from_snapshot(snapshot: dict[str, Any] | None) -> str | None:
        if not isinstance(snapshot, dict):
            return None
        pusd = snapshot.get("funder_pusd_balance_usd")
        usdce = snapshot.get("funder_usdce_balance_usd")
        if (
            isinstance(pusd, (int, float))
            and isinstance(usdce, (int, float))
            and float(pusd) <= 0.000001
            and float(usdce) >= 0.01
        ):
            return "usdce_not_wrapped_to_pusd"
        return None

    def _erc20_balance_usd(self, token_address: str, owner_address: str) -> float:
        data = "0x70a08231" + self._abi_address(owner_address)
        return round(self._eth_call_uint(token_address, data) / 1_000_000, 6)

    def _erc20_allowance_usd(self, token_address: str, owner_address: str, spender_address: str) -> float:
        data = "0xdd62ed3e" + self._abi_address(owner_address) + self._abi_address(spender_address)
        return round(self._eth_call_uint(token_address, data) / 1_000_000, 6)

    def _eth_call_uint(self, to_address: str, data: str) -> int:
        result = self._polygon_rpc_call(
            "eth_call",
            [{"to": to_address, "data": data}, "latest"],
        )
        return int(str(result), 16)

    def _polygon_rpc_call(self, method: str, params: list[Any]) -> Any:
        configured = str(settings.polygon_rpc_url or "").strip()
        urls = [configured] if configured else []
        urls.extend(url for url in POLYGON_RPC_FALLBACK_URLS if url and url not in urls)
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
        last_error: Exception | None = None
        for url in urls:
            try:
                request = urllib.request.Request(
                    url,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "polymarket-smart-copy-bot/1.0",
                    },
                    method="POST",
                )
                context = ssl.create_default_context(cafile=certifi.where())
                with urllib.request.urlopen(request, timeout=8, context=context) as response:
                    body = response.read().decode("utf-8")
                parsed = json.loads(body)
                if parsed.get("error"):
                    raise RuntimeError(parsed["error"])
                self._last_polygon_rpc_url = url
                return parsed.get("result")
            except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError, OSError) as exc:
                last_error = exc
                continue
        raise RuntimeError(f"Polygon RPC call failed: {last_error}")

    @staticmethod
    def _abi_address(address: str) -> str:
        return str(address).lower().removeprefix("0x").rjust(64, "0")

    def _ensure_clob_client(self) -> Any:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds
        from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2

        if self._clob_client is not None:
            return self._clob_client

        signature_type = settings.polymarket_signature_type
        if signature_type is None:
            signature_type = SignatureTypeV2.POLY_PROXY if settings.polymarket_proxy_address else SignatureTypeV2.EOA

        self._clob_client = ClobClient(
            host=settings.polymarket_host,
            key=settings.polymarket_private_key,
            chain_id=settings.polymarket_chain_id,
            signature_type=signature_type,
            funder=settings.polymarket_proxy_address or None,
            retry_on_error=True,
        )
        if (
            settings.polymarket_api_key
            and settings.polymarket_api_secret
            and settings.polymarket_api_passphrase
        ):
            self._clob_client.set_api_creds(
                ApiCreds(
                    api_key=settings.polymarket_api_key,
                    api_secret=settings.polymarket_api_secret,
                    api_passphrase=settings.polymarket_api_passphrase,
                )
            )
            self._clob_creds_source = "env"
        else:
            self._switch_to_derived_creds(self._clob_client)
        return self._clob_client

    def _refresh_collateral_allowance_sync(self) -> None:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        try:
            clob_client = self._ensure_clob_client()
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            clob_client.update_balance_allowance(params)
        except Exception as exc:
            logger.warning("Failed to refresh collateral allowance: {}", exc)

    def _switch_to_derived_creds(self, clob_client: Any) -> None:
        if hasattr(clob_client, "derive_api_key"):
            try:
                creds = clob_client.derive_api_key()
            except Exception:
                creds = clob_client.create_api_key()
        elif hasattr(clob_client, "create_or_derive_api_key"):
            creds = clob_client.create_or_derive_api_key()
        else:
            creds = clob_client.create_or_derive_api_creds()
        if creds is None:
            raise RuntimeError("Failed to create or derive Polymarket API credentials")
        clob_client.set_api_creds(creds)
        self._clob_creds_source = "derived"

    @staticmethod
    def _builder_signature_type(builder: Any) -> Any:
        if builder is None:
            return None
        return getattr(builder, "signature_type", None) or getattr(builder, "sig_type", None)

    @staticmethod
    def _extract_collateral_balance(response: Any) -> float | None:
        if isinstance(response, (int, float)):
            amount = float(response)
            return PolymarketClient._normalize_collateral_units(amount=amount, raw=response, decimals_hint=None)

        if isinstance(response, dict):
            decimals_hint = response.get("decimals") if isinstance(response.get("decimals"), int) else None
            for key in ("balance", "availableBalance", "usdcBalance", "available"):
                value = response.get(key)
                numeric = PolymarketClient._parse_numeric(value)
                if numeric is not None:
                    return PolymarketClient._normalize_collateral_units(
                        amount=numeric,
                        raw=value,
                        decimals_hint=decimals_hint,
                    )
                parsed = PolymarketClient._extract_collateral_balance(value)
                if parsed is not None:
                    return parsed
            for key in ("data", "result", "collateral", "balances", "allowance"):
                value = response.get(key)
                if value is None:
                    continue
                parsed = PolymarketClient._extract_collateral_balance(value)
                if parsed is not None:
                    return parsed

        if isinstance(response, str):
            parsed = PolymarketClient._parse_numeric(response)
            if parsed is None:
                return None
            return PolymarketClient._normalize_collateral_units(amount=parsed, raw=response, decimals_hint=None)

        if isinstance(response, list):
            for item in response:
                parsed = PolymarketClient._extract_collateral_balance(item)
                if parsed is not None:
                    return parsed
        return None

    @staticmethod
    def _parse_numeric(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return None
        return None

    @staticmethod
    def _normalize_collateral_units(amount: float, raw: Any, decimals_hint: int | None) -> float:
        # CLOB balance endpoints often return USDC in base units (6 decimals).
        if decimals_hint is not None and isinstance(raw, str) and raw.isdigit():
            return amount / (10**decimals_hint)

        if isinstance(raw, str):
            raw_clean = raw.strip()
            if raw_clean.isdigit():
                return amount / 1_000_000

        if isinstance(raw, int) and abs(raw) >= 1_000_000:
            return amount / 1_000_000

        if isinstance(raw, float) and raw.is_integer() and abs(raw) >= 1_000_000:
            return amount / 1_000_000

        return amount

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        if value is None:
            return datetime.now(tz=timezone.utc)

        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)

        if isinstance(value, str):
            normalized = value.replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(normalized)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                return datetime.now(tz=timezone.utc)

        return datetime.now(tz=timezone.utc)

    @staticmethod
    def _extract_price(payload: Any, token_id: str | None = None) -> float | None:
        if not isinstance(payload, dict):
            return None

        for key in ("lastTradePrice", "midPrice", "price", "bestAsk"):
            if key in payload and payload[key] is not None:
                return float(payload[key])

        outcomes = payload.get("outcomes")
        if isinstance(outcomes, list) and outcomes:
            if token_id is not None:
                for outcome in outcomes:
                    if str(outcome.get("tokenId")) == str(token_id):
                        if outcome.get("price") is not None:
                            return float(outcome["price"])
            first = outcomes[0]
            if isinstance(first, dict) and first.get("price") is not None:
                return float(first["price"])

        return None

    @staticmethod
    def _validate_price(price: float | None) -> float | None:
        if price is None:
            return None
        if price < settings.min_valid_price or price > settings.max_valid_price:
            return None
        return round(price, 2)

    @classmethod
    def _parse_orderbook_snapshot(cls, payload: Any) -> OrderbookSnapshot | None:
        if not isinstance(payload, dict):
            return None

        bids = cls._parse_orderbook_levels(payload.get("bids") or payload.get("buy") or [])
        asks = cls._parse_orderbook_levels(payload.get("asks") or payload.get("sell") or [])
        best_bid = bids[0].price if bids else None
        best_ask = asks[0].price if asks else None
        return OrderbookSnapshot(bids=bids, asks=asks, best_bid=best_bid, best_ask=best_ask)

    @classmethod
    def _parse_orderbook_levels(cls, payload: Any) -> list[OrderbookLevel]:
        if not isinstance(payload, list):
            return []

        levels: list[OrderbookLevel] = []
        for item in payload:
            if isinstance(item, dict):
                price_raw = item.get("price") or item.get("p")
                size_raw = item.get("size") or item.get("quantity") or item.get("amount") or item.get("shares")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                price_raw = item[0]
                size_raw = item[1]
            else:
                continue

            try:
                price = float(price_raw)
                size = float(size_raw)
            except (TypeError, ValueError):
                continue

            if price <= 0 or size <= 0:
                continue

            if price > 1.0:
                price /= 100.0

            price = cls._validate_price(price)
            if price is None:
                continue
            levels.append(OrderbookLevel(price=price, size=size))

        return levels

    @staticmethod
    def _extract_token_price(payload: Any) -> float | None:
        """Extract decimal token price (0..1) from CLOB token endpoints."""

        if not isinstance(payload, dict):
            return None

        for key in ("mid", "price", "lastTradePrice", "last_price"):
            value = payload.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue

        # /book can provide bids/asks; estimate midpoint from top levels.
        bids = payload.get("bids")
        asks = payload.get("asks")
        bid_px: float | None = None
        ask_px: float | None = None
        if isinstance(bids, list) and bids:
            first_bid = bids[0]
            if isinstance(first_bid, dict):
                try:
                    bid_px = float(first_bid.get("price"))
                except (TypeError, ValueError):
                    bid_px = None
        if isinstance(asks, list) and asks:
            first_ask = asks[0]
            if isinstance(first_ask, dict):
                try:
                    ask_px = float(first_ask.get("price"))
                except (TypeError, ValueError):
                    ask_px = None

        if bid_px is not None and ask_px is not None:
            return (bid_px + ask_px) / 2
        if bid_px is not None:
            return bid_px
        if ask_px is not None:
            return ask_px

        return None

    @staticmethod
    def _parse_open_position_row(row: dict[str, Any], *, default_wallet: str) -> WalletOpenPosition | None:
        market_id = str(row.get("conditionId") or row.get("market") or row.get("marketId") or "")
        if not market_id:
            return None

        token_id = row.get("asset") or row.get("tokenId") or row.get("tokenID")
        outcome = str(row.get("outcome") or row.get("title") or "UNKNOWN")

        quantity = PolymarketClient._parse_numeric(row.get("size")) or 0.0
        avg_price = PolymarketClient._parse_numeric(row.get("avgPrice")) or 0.0
        cur_price = PolymarketClient._parse_numeric(row.get("curPrice")) or 0.0
        invested = PolymarketClient._parse_numeric(row.get("initialValue"))
        current_value = PolymarketClient._parse_numeric(row.get("currentValue"))
        unrealized = PolymarketClient._parse_numeric(row.get("cashPnl"))

        avg_price_cents = ensure_price_in_cents(avg_price) if avg_price > 0 else 0.0
        current_price_cents = ensure_price_in_cents(cur_price) if cur_price > 0 else avg_price_cents

        if invested is None:
            invested = quantity * max(avg_price, 0.0)
        if current_value is None:
            current_value = quantity * max(cur_price, 0.0)
        if unrealized is None:
            unrealized = current_value - invested

        wallet = PolymarketClient._extract_wallet_from_row(row) or default_wallet
        return WalletOpenPosition(
            wallet_address=wallet,
            market_id=market_id,
            token_id=str(token_id) if token_id is not None else None,
            outcome=outcome,
            quantity=abs(quantity),
            avg_price_cents=avg_price_cents,
            current_price_cents=current_price_cents,
            invested_usd=max(float(invested), 0.0),
            current_value_usd=max(float(current_value), 0.0),
            unrealized_pnl_usd=float(unrealized),
        )

    @staticmethod
    def _parse_open_order_row(row: dict[str, Any], *, now: datetime) -> OpenOrderInfo | None:
        order_id = str(row.get("id") or row.get("orderID") or row.get("orderId") or "").strip()
        if not order_id:
            return None

        market_id = row.get("market") or row.get("marketId") or row.get("conditionId")
        token_id = row.get("asset_id") or row.get("asset") or row.get("tokenId") or row.get("tokenID")
        side = str(row.get("side") or row.get("type") or "").lower()

        raw_price = PolymarketClient._parse_numeric(
            row.get("price")
            or row.get("limit_price")
            or row.get("pricePaid")
        )
        price_cents = ensure_price_in_cents(raw_price) if raw_price and raw_price > 0 else 0.0
        size = PolymarketClient._parse_numeric(row.get("size") or row.get("original_size") or row.get("amount")) or 0.0

        ts_value = row.get("createdAt") or row.get("created_at") or row.get("timestamp") or row.get("placedAt")
        created_at = PolymarketClient._parse_timestamp(ts_value)
        if created_at > now:
            created_at = now

        return OpenOrderInfo(
            order_id=order_id,
            market_id=str(market_id) if market_id else None,
            token_id=str(token_id) if token_id else None,
            side=side,
            price_cents=price_cents,
            size=float(size),
            created_at=created_at,
            raw=row,
        )

    @staticmethod
    def _parse_fill_row(row: dict[str, Any]) -> FillInfo | None:
        trade_id = str(row.get("id") or row.get("tradeID") or row.get("tradeId") or "").strip()
        if not trade_id:
            return None

        token_id = row.get("asset_id") or row.get("asset") or row.get("tokenId") or row.get("tokenID")
        market_id = row.get("market") or row.get("marketId") or row.get("conditionId")
        side = str(row.get("side") or row.get("type") or "").lower()

        price = PolymarketClient._parse_numeric(row.get("price") or row.get("pricePaid") or row.get("avgPrice"))
        size_shares = PolymarketClient._parse_numeric(row.get("size") or row.get("amount") or row.get("filled_size"))
        if price is None or size_shares is None:
            return None
        if price <= 0 or size_shares <= 0:
            return None

        order_id = (
            row.get("orderID")
            or row.get("orderId")
            or row.get("order_id")
            or row.get("makerOrderID")
            or row.get("takerOrderID")
            or row.get("makerOrderId")
            or row.get("takerOrderId")
        )

        traded_at = PolymarketClient._parse_timestamp(
            row.get("timestamp") or row.get("createdAt") or row.get("time")
        )
        price_cents = ensure_price_in_cents(price)
        size_usd = float(price) * float(size_shares)

        return FillInfo(
            trade_id=trade_id,
            order_id=str(order_id) if order_id else None,
            market_id=str(market_id) if market_id else None,
            token_id=str(token_id) if token_id else None,
            side=side,
            price_cents=price_cents,
            size_shares=float(size_shares),
            size_usd=max(size_usd, 0.0),
            traded_at=traded_at,
            raw=row,
        )

    @staticmethod
    def _is_open_order_payload(payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict):
            return False

        status = str(payload.get("status") or payload.get("state") or "").lower()
        if status in {"open", "live", "active"}:
            return True
        if status in {"filled", "cancelled", "canceled", "expired", "closed", "done"}:
            return False

        remaining = PolymarketClient._parse_numeric(
            payload.get("remaining_size")
            or payload.get("remainingSize")
            or payload.get("size_open")
        )
        if remaining is not None:
            return remaining > 0

        filled = PolymarketClient._parse_numeric(
            payload.get("filled_size")
            or payload.get("filledSize")
            or payload.get("matched_size")
        )
        total = PolymarketClient._parse_numeric(
            payload.get("size")
            or payload.get("original_size")
            or payload.get("amount")
        )
        if filled is not None and total is not None:
            return filled < total
        return False

    @staticmethod
    def _coerce_rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            for key in ("data", "items", "rows", "results", "positions"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
        return []

    @staticmethod
    def _normalize_address(value: str | None) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        if normalized.startswith("0x") and len(normalized) == 42:
            return normalized
        return None

    @classmethod
    def _extract_wallet_from_row(cls, row: dict[str, Any]) -> str | None:
        for key in (
            "proxyWallet",
            "walletAddress",
            "address",
            "user",
            "owner",
            "maker",
            "taker",
        ):
            value = row.get(key)
            normalized = cls._normalize_address(value if isinstance(value, str) else None)
            if normalized:
                return normalized
        return None

    @classmethod
    def _filter_rows_by_wallet(cls, rows: list[dict[str, Any]], wallet_address: str | None) -> list[dict[str, Any]]:
        if not wallet_address:
            return []
        filtered: list[dict[str, Any]] = []
        rows_with_wallet = 0
        for row in rows:
            row_wallet = cls._extract_wallet_from_row(row)
            if row_wallet is not None:
                rows_with_wallet += 1
            if row_wallet == wallet_address:
                filtered.append(row)
        if rows_with_wallet == 0 and rows:
            # Some endpoints omit wallet fields but still honor the query wallet.
            return rows
        return filtered

    def _resolve_account_address(self) -> str | None:
        proxy = self._normalize_address(settings.polymarket_proxy_address)
        if proxy:
            return proxy
        if self._clob_client is not None and getattr(self._clob_client, "signer", None):
            try:
                return self._normalize_address(self._clob_client.signer.address())
            except Exception:
                pass

        # Fallback: derive EOA address directly from the private key without going
        # through the full CLOB credential setup (which requires a network call).
        # ClobClient.__init__ is pure-local (no network) and creates the signer.
        if settings.polymarket_private_key:
            try:
                from py_clob_client_v2.client import ClobClient
                from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2

                _tmp = ClobClient(
                    host=settings.polymarket_host,
                    key=settings.polymarket_private_key,
                    chain_id=settings.polymarket_chain_id,
                    signature_type=SignatureTypeV2.EOA,
                )
                addr = self._normalize_address(_tmp.signer.address())
                if addr:
                    logger.debug("Resolved account address from private key: {}", addr)
                return addr
            except Exception as exc:
                logger.warning("Could not derive account address from private key: {}", exc)

        return None

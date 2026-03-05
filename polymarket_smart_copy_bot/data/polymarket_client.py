from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

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


@dataclass(slots=True)
class OrderResult:
    success: bool
    order_id: str | None
    tx_hash: str | None
    error: str | None = None


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


class PolymarketClient:
    """Polymarket data + order execution client with retry and timeout guards."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._clob_client: Any = None
        self._clob_creds_source: str | None = None
        self._dry_run = settings.dry_run
        self._missing_orderbooks: set[str] = set()

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

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

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

    async def fetch_market_info(self, market_id: str) -> MarketInfoData:
        """Fetch human-readable market question and category from the Gamma API.

        The Gamma API may return either a single dict or a list of market dicts
        depending on the endpoint and conditionId format.  We handle both.

        Tries multiple URL patterns:
          1. GET /markets?conditionIds={market_id}  (returns list, most reliable)
          2. GET /markets/{market_id}               (may return list or dict)
          3. GET /events/{market_id}                (fallback)
        Returns empty strings on any API failure so callers show raw market_id.
        """

        question = ""
        category = ""

        gamma = settings.polymarket_gamma_host.rstrip("/")
        candidate_requests: list[tuple[str, dict]] = [
            # Most reliable: conditionIds query parameter returns a list
            (f"{gamma}/markets", {"conditionIds": market_id}),
            # Path-based endpoints (response may be dict or list)
            (f"{gamma}/markets/{market_id}", {}),
            (f"{gamma}/events/{market_id}", {}),
        ]

        for url, params in candidate_requests:
            try:
                raw = await self._request_json("GET", url, params=params or None)
            except Exception:
                continue

            # Normalise: Gamma may return a list or a single dict
            market_obj: dict | None = None
            if isinstance(raw, dict):
                market_obj = raw
            elif isinstance(raw, list):
                # Take first item that looks like a market
                market_obj = next((item for item in raw if isinstance(item, dict)), None)

            if market_obj is None:
                continue

            question = str(
                market_obj.get("question")
                or market_obj.get("title")
                or market_obj.get("groupItemTitle")
                or ""
            )
            tags = market_obj.get("tags")
            category = str(
                market_obj.get("category")
                or (tags[0] if isinstance(tags, list) and tags else "")
            )

            if question:
                break

        return MarketInfoData(market_id=market_id, question=question, category=category)

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

        Returns free collateral (cash), open positions current value, and total.
        """

        if self._dry_run:
            return {
                "source": "dry_run",
                "free_balance_usd": None,
                "positions_value_usd": None,
                "total_balance_usd": None,
                "positions_count": 0,
            }

        free_balance = await self.fetch_account_balance_usd()
        open_positions = await self.fetch_account_open_positions(limit=500)

        positions_value: float | None = None
        positions_count = 0
        if open_positions is not None:
            positions_count = len([row for row in open_positions if row.quantity > 0])
            positions_value = round(
                sum(max(float(row.current_value_usd), 0.0) for row in open_positions),
                4,
            )

        total_balance: float | None = None
        if free_balance is not None and positions_value is not None:
            total_balance = round(float(free_balance) + positions_value, 4)

        return {
            "source": "polymarket",
            "free_balance_usd": round(float(free_balance), 4) if free_balance is not None else None,
            "positions_value_usd": positions_value,
            "total_balance_usd": total_balance,
            "positions_count": positions_count,
        }

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Place an order through py-clob-client or simulate in DRY_RUN mode."""

        if self._dry_run:
            simulated_id = f"dry-{int(datetime.now(tz=timezone.utc).timestamp())}"
            return OrderResult(success=True, order_id=simulated_id, tx_hash=simulated_id)

        if not settings.polymarket_private_key:
            return OrderResult(success=False, order_id=None, tx_hash=None, error="Missing POLYMARKET_PRIVATE_KEY")

        if not request.token_id:
            return OrderResult(success=False, order_id=None, tx_hash=None, error="Missing token_id for live order")

        if request.token_id in self._missing_orderbooks:
            return OrderResult(
                success=False,
                order_id=None,
                tx_hash=None,
                error=f"orderbook_not_found:{request.token_id}",
            )

        try:
            response = await asyncio.to_thread(self._place_order_sync, request)
            order_id = str(response.get("orderID") or response.get("id") or "")
            tx_hash = str(response.get("transactionHash") or response.get("txHash") or order_id)
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
            if "not enough balance / allowance" in text:
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

    async def cancel_stale_orders(
        self,
        *,
        stale_after_seconds: int,
        max_cancel: int,
    ) -> dict[str, Any]:
        """Cancel stale open orders older than the configured TTL."""

        if self._dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "scanned": 0,
                "stale": 0,
                "cancelled": 0,
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
                "failed": 0,
                "failed_ids": [],
            }

        now = datetime.now(tz=timezone.utc)
        threshold = max(stale_after_seconds, 60)
        stale_orders = [
            order
            for order in open_orders
            if (now - order.created_at).total_seconds() >= threshold
        ]
        stale_orders.sort(key=lambda row: row.created_at)

        to_cancel = stale_orders[: max(1, max_cancel)]
        cancelled = 0
        failed_ids: list[str] = []
        for order in to_cancel:
            try:
                success = await asyncio.to_thread(self._cancel_order_sync, order.order_id)
            except Exception as exc:
                logger.warning("Failed to cancel stale order {}: {}", order.order_id, exc)
                success = False
            if success:
                cancelled += 1
            else:
                failed_ids.append(order.order_id)

        return {
            "ok": True,
            "dry_run": False,
            "scanned": len(open_orders),
            "stale": len(stale_orders),
            "cancelled": cancelled,
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
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        clob_client = self._ensure_clob_client()
        response = clob_client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        balance = self._extract_collateral_balance(response)
        signer_address = clob_client.signer.address() if getattr(clob_client, "signer", None) else None
        builder = getattr(clob_client, "builder", None)
        funder_address = getattr(builder, "funder", None) if builder is not None else None
        signature_type = getattr(builder, "sig_type", None) if builder is not None else None
        return {
            "code": "ok",
            "collateral_balance_usd": balance,
            "response_type": type(response).__name__,
            "creds_source": self._clob_creds_source or "unknown",
            "signer_address": signer_address,
            "funder_address": funder_address,
            "signature_type": signature_type,
        }

    def _diagnose_with_derived_retry_sync(self) -> dict[str, Any]:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        clob_client = self._ensure_clob_client()
        self._switch_to_derived_creds(clob_client)
        response = clob_client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        balance = self._extract_collateral_balance(response)
        signer_address = clob_client.signer.address() if getattr(clob_client, "signer", None) else None
        builder = getattr(clob_client, "builder", None)
        funder_address = getattr(builder, "funder", None) if builder is not None else None
        signature_type = getattr(builder, "sig_type", None) if builder is not None else None
        return {
            "collateral_balance_usd": balance,
            "response_type": type(response).__name__,
            "creds_source": self._clob_creds_source or "unknown",
            "signer_address": signer_address,
            "funder_address": funder_address,
            "signature_type": signature_type,
        }

    def _place_order_sync(self, request: OrderRequest) -> dict[str, Any]:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        clob_client = self._ensure_clob_client()

        side = BUY if request.side.lower() == "buy" else SELL
        price_decimal = max(min(request.price_cents / 100, 0.999), 0.001)
        size = max(request.size_usd / price_decimal, 1.0)

        order_args = OrderArgs(
            token_id=request.token_id,
            price=price_decimal,
            size=size,
            side=side,
        )
        signed_order = clob_client.create_order(order_args)
        try:
            return clob_client.post_order(signed_order, OrderType.GTC)
        except Exception as exc:
            text = str(exc).lower()
            if "unauthorized" in text or "invalid api key" in text:
                # Common operator error: Builder API creds provided instead of user L2 creds.
                # Fall back to signer-derived user credentials and retry once.
                self._switch_to_derived_creds(clob_client)
                return clob_client.post_order(signed_order, OrderType.GTC)
            if "not enough balance / allowance" in text:
                self._refresh_collateral_allowance_sync()
                return clob_client.post_order(signed_order, OrderType.GTC)
            raise

    def _fetch_open_orders_sync(self, market_id: str | None, token_id: str | None) -> list[dict[str, Any]]:
        from py_clob_client.clob_types import OpenOrderParams

        clob_client = self._ensure_clob_client()
        params = OpenOrderParams(
            market=market_id or None,
            asset_id=token_id or None,
        )
        response = clob_client.get_orders(params=params)
        if not isinstance(response, list):
            return []
        return [row for row in response if isinstance(row, dict)]

    def _cancel_order_sync(self, order_id: str) -> bool:
        clob_client = self._ensure_clob_client()
        if not order_id:
            return False
        response = clob_client.cancel(order_id)
        if isinstance(response, dict):
            if str(response.get("canceled", "")).lower() == "true":
                return True
            if str(response.get("status", "")).lower() in {"ok", "success"}:
                return True
            if response.get("error"):
                return False
        # Some endpoints return non-uniform bodies; lack of exception is enough.
        return True

    def _fetch_account_balance_sync(self) -> float | None:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        # `get_balance_allowance` response schema can vary between API versions.
        clob_client = self._ensure_clob_client()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        response = clob_client.get_balance_allowance(params)
        balance = self._extract_collateral_balance(response)
        if balance is not None:
            return balance
        return None

    def _ensure_clob_client(self) -> Any:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        from py_order_utils.model import EOA, POLY_PROXY

        if self._clob_client is not None:
            return self._clob_client

        signature_type = settings.polymarket_signature_type
        if signature_type is None:
            signature_type = POLY_PROXY if settings.polymarket_proxy_address else EOA

        self._clob_client = ClobClient(
            settings.polymarket_host,
            key=settings.polymarket_private_key,
            chain_id=settings.polymarket_chain_id,
            signature_type=signature_type,
            funder=settings.polymarket_proxy_address or None,
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
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        try:
            clob_client = self._ensure_clob_client()
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            clob_client.update_balance_allowance(params)
        except Exception as exc:
            logger.warning("Failed to refresh collateral allowance: {}", exc)

    def _switch_to_derived_creds(self, clob_client: Any) -> None:
        creds = clob_client.create_or_derive_api_creds()
        if creds is None:
            raise RuntimeError("Failed to create or derive Polymarket API credentials")
        clob_client.set_api_creds(creds)
        self._clob_creds_source = "derived"

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
                return None
        return None

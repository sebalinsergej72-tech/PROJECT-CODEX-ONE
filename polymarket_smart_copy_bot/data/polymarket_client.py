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


class PolymarketClient:
    """Polymarket data + order execution client with retry and timeout guards."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._clob_client: Any = None

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
            if rows:
                return rows

        gamma_url = f"{settings.polymarket_gamma_host.rstrip('/')}/trades"
        try:
            raw = await self._request_json("GET", gamma_url, params={"user": wallet_address, "limit": limit})
        except Exception as exc:
            logger.warning("Failed to fetch raw trades for {}: {}", wallet_address, exc)
            return []
        return self._coerce_rows(raw)

    async def get_user_activity(self, wallet_address: str, limit: int = 500) -> list[dict[str, Any]]:
        """Fetch raw user activity from data-api."""

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
            if rows:
                return rows
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
                continue

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

    async def fetch_market_mid_price(self, market_id: str, token_id: str | None = None) -> float | None:
        """Fetch current market price in cents for mark-to-market and risk controls."""

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

    async def fetch_account_balance_usd(self) -> float | None:
        """Fetch current account balance for capital recalculation."""

        if settings.dry_run:
            return settings.default_starting_equity

        if not settings.polymarket_private_key:
            return None

        try:
            return await asyncio.to_thread(self._fetch_account_balance_sync)
        except Exception as exc:
            logger.warning("Failed to fetch account balance: {}", exc)
            return None

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Place an order through py-clob-client or simulate in DRY_RUN mode."""

        if settings.dry_run:
            simulated_id = f"dry-{int(datetime.now(tz=timezone.utc).timestamp())}"
            return OrderResult(success=True, order_id=simulated_id, tx_hash=simulated_id)

        if not settings.polymarket_private_key:
            return OrderResult(success=False, order_id=None, tx_hash=None, error="Missing POLYMARKET_PRIVATE_KEY")

        if not request.token_id:
            return OrderResult(success=False, order_id=None, tx_hash=None, error="Missing token_id for live order")

        try:
            response = await asyncio.to_thread(self._place_order_sync, request)
            order_id = str(response.get("orderID") or response.get("id") or "")
            tx_hash = str(response.get("transactionHash") or response.get("txHash") or order_id)
            return OrderResult(success=True, order_id=order_id or None, tx_hash=tx_hash or None)
        except Exception as exc:
            logger.exception("Order placement failed")
            return OrderResult(success=False, order_id=None, tx_hash=None, error=str(exc))

    def _place_order_sync(self, request: OrderRequest) -> dict[str, Any]:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        if self._clob_client is None:
            self._clob_client = ClobClient(
                settings.polymarket_host,
                key=settings.polymarket_private_key,
                chain_id=settings.polymarket_chain_id,
            )
            if settings.polymarket_api_key:
                self._clob_client.set_api_creds({"api_key": settings.polymarket_api_key})
            else:
                creds = self._clob_client.create_or_derive_api_creds()
                self._clob_client.set_api_creds(creds)

        side = BUY if request.side.lower() == "buy" else SELL
        price_decimal = max(min(request.price_cents / 100, 0.999), 0.001)
        size = max(request.size_usd / price_decimal, 1.0)

        order_args = OrderArgs(
            token_id=request.token_id,
            price=price_decimal,
            size=size,
            side=side,
        )
        signed_order = self._clob_client.create_order(order_args)
        return self._clob_client.post_order(signed_order, OrderType.GTC)

    def _fetch_account_balance_sync(self) -> float | None:
        from py_clob_client.client import ClobClient

        if self._clob_client is None:
            self._clob_client = ClobClient(
                settings.polymarket_host,
                key=settings.polymarket_private_key,
                chain_id=settings.polymarket_chain_id,
            )
            if settings.polymarket_api_key:
                self._clob_client.set_api_creds({"api_key": settings.polymarket_api_key})
            else:
                creds = self._clob_client.create_or_derive_api_creds()
                self._clob_client.set_api_creds(creds)

        # `get_balance_allowance` response schema can vary between API versions.
        response = self._clob_client.get_balance_allowance()
        if isinstance(response, dict):
            for key in ("balance", "availableBalance", "usdcBalance"):
                if key in response and response[key] is not None:
                    return float(response[key])
        return None

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
    def _coerce_rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            for key in ("data", "items", "rows", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
        return []

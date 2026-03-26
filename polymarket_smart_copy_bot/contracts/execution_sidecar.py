from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SidecarOrderbookLevel(BaseModel):
    price: float
    size: float


class SidecarOrderbookSnapshot(BaseModel):
    bids: list[SidecarOrderbookLevel] = Field(default_factory=list)
    asks: list[SidecarOrderbookLevel] = Field(default_factory=list)
    best_bid: float | None = None
    best_ask: float | None = None


class SidecarPrimeMarketDataRequest(BaseModel):
    token_ids: list[str] = Field(default_factory=list)


class SidecarPrimeMarketDataResponse(BaseModel):
    snapshots: dict[str, SidecarOrderbookSnapshot | None] = Field(default_factory=dict)


class SidecarExecutableSnapshot(BaseModel):
    best_bid: float | None = None
    best_ask: float | None = None
    buy_vwap_5usd: float | None = None
    buy_vwap_10usd: float | None = None
    buy_vwap_25usd: float | None = None
    sell_vwap_5usd: float | None = None
    sell_vwap_10usd: float | None = None
    sell_vwap_25usd: float | None = None
    top5_ask_liquidity_usd: float = 0.0
    top5_bid_liquidity_usd: float = 0.0
    has_book: bool = False
    last_update_ts: float | None = None
    snapshot_age_ms: int | None = None


class SidecarPrimeExecutableMarketDataResponse(BaseModel):
    snapshots: dict[str, SidecarExecutableSnapshot | None] = Field(default_factory=dict)


class SidecarRegisterHotMarketsRequest(BaseModel):
    token_ids: list[str] = Field(default_factory=list)
    ttl_seconds: int | None = None
    source: str | None = None
    priority: int | None = None


class SidecarRegisterHotMarketsResponse(BaseModel):
    accepted: int = 0


class SidecarWalletScanTarget(BaseModel):
    wallet_address: str


class SidecarWalletSignalRow(BaseModel):
    external_trade_id: str
    wallet_address: str
    market_id: str
    token_id: str | None = None
    outcome: str
    side: str
    price_cents: float
    size_usd: float
    traded_at_ts: float
    profit_usd: float | None = None
    market_slug: str | None = None
    market_category: str | None = None


class SidecarHotWalletScanRequest(BaseModel):
    wallets: list[SidecarWalletScanTarget] = Field(default_factory=list)
    signal_limit: int = 30
    hot_market_ttl_seconds: int | None = None


class SidecarHotWalletScanResponse(BaseModel):
    signals: dict[str, list[SidecarWalletSignalRow]] = Field(default_factory=dict)


class SidecarOrderRequest(BaseModel):
    token_id: str
    side: str
    price_cents: float
    size_usd: float
    market_id: str
    outcome: str
    order_type: str = "GTC"


class SidecarExecutionPlanRequest(BaseModel):
    external_trade_id: str
    source_price_cents: float
    target_size_usd: float
    risk_mode: str
    max_slippage_bps: float
    max_allowed_slippage_bps: float
    min_valid_price: float = 0.01
    min_orderbook_liquidity_usd: float | None = None
    liquidity_buffer_multiplier: float | None = None
    max_price_deviation_pct: float | None = None
    min_absolute_price_deviation_cents: float | None = None
    orderbook: SidecarOrderbookSnapshot | None = None
    order: SidecarOrderRequest


class SidecarExecutionPlanResponse(BaseModel):
    allowed: bool
    reason: str | None = None
    requested_price_cents: float | None = None
    requested_slippage_bps: float | None = None
    order_type: str | None = None
    echoed_order: SidecarOrderRequest | None = None


class SidecarFillRow(BaseModel):
    order_id: str | None = None
    market_id: str | None = None
    token_id: str | None = None
    side: str
    price_cents: float
    size_shares: float
    size_usd: float
    traded_at_ts: float


class SidecarFillReconcileRequest(BaseModel):
    copied_trade_size_usd: float
    current_filled_quantity: float
    current_filled_size_usd: float
    fills: list[SidecarFillRow] = Field(default_factory=list)
    order_open: bool | None = None


class SidecarFillReconcileResponse(BaseModel):
    status: str
    reason: str
    total_quantity: float
    total_size_usd: float
    filled_price_cents: float
    delta_quantity: float
    delta_size_usd: float
    delta_price_cents: float
    latest_fill_ts: float | None = None
    order_open: bool | None = None


class SidecarAuthenticatedOpenOrdersResponse(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list)


class SidecarAuthenticatedOrderStatusResponse(BaseModel):
    payload: dict[str, Any] | None = None


class SidecarAuthenticatedFillsResponse(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list)

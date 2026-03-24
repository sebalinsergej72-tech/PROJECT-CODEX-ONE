from __future__ import annotations

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

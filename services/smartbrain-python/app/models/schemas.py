from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class IngestSnapshotRequest(BaseModel):
    symbols: list[str] | None = None
    timeframes: list[str] | None = None


class IngestSnapshotResponse(BaseModel):
    success: bool
    ingested_at: datetime
    records_written: int


class FeatureMaterializeRequest(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["BTC", "ETH"])
    timeframes: list[str] = Field(default_factory=lambda: ["1m", "5m", "15m", "1h", "4h", "1d"])


class FeatureMaterializeResponse(BaseModel):
    success: bool
    vectors_written: int


class DecisionRequest(BaseModel):
    symbol: str
    mode: Literal["paper", "live"] = "paper"


class TradeDecision(BaseModel):
    action: Literal["LONG", "SHORT", "FLAT"]
    size_pct: float
    leverage: int
    confidence: float
    sl_price: float | None = None
    tp_price: float | None = None
    reason: str


class DecisionResponse(BaseModel):
    success: bool
    decision: TradeDecision


class RiskCheckRequest(BaseModel):
    symbol: str
    mode: Literal["paper", "live"] = "paper"
    requested_leverage: int = 1


class RiskCheckResponse(BaseModel):
    allowed: bool
    max_leverage: int
    max_position_pct: float
    drawdown_estimate: float
    reasons: list[str]


class ExecutionRequest(BaseModel):
    symbol: str
    mode: Literal["paper", "live"] = "paper"
    decision: TradeDecision
    state: dict | None = None


class ExecutionResponse(BaseModel):
    success: bool
    mode: Literal["paper", "live"]
    status: str
    order_id: str | None = None
    details: dict


class AutonomousCycleRequest(BaseModel):
    symbol: str
    mode: Literal["paper", "live"] = "paper"


class AutonomousCycleResponse(BaseModel):
    success: bool
    risk: RiskCheckResponse
    decision: TradeDecision | None = None
    execution: ExecutionResponse | None = None


class ActiveSettingsResponse(BaseModel):
    enabled: bool
    paper_mode: bool
    max_leverage: int
    max_portfolio_risk: float
    max_drawdown_circuit: float
    assets_whitelist: list[str]
    emergency_stop: bool


class PerformanceRefreshResponse(BaseModel):
    refreshed: bool
    reason: str | None = None
    max_drawdown: float
    sharpe: float | None = None
    profit_factor: float | None = None
    win_rate: float | None = None
    model_version: str | None = None


class OnlineTreeUpdateResponse(BaseModel):
    success: bool
    status: Literal["updated", "skipped"]
    samples_used: int
    model_version: str | None = None
    promoted: bool = False
    rmse: float | None = None
    reason: str | None = None


class RLReplayUpdateResponse(BaseModel):
    success: bool
    status: Literal["updated", "skipped"]
    samples_used: int
    model_version: str | None = None
    promoted: bool = False
    action_mae: float | None = None
    reason: str | None = None


class RetrainResponse(BaseModel):
    success: bool
    new_version: str
    promoted: bool

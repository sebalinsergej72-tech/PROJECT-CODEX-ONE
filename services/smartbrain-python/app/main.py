from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, status

from app.config import settings
from app.models.schemas import (
    ActiveSettingsResponse,
    AutonomousCycleRequest,
    AutonomousCycleResponse,
    DecisionRequest,
    DecisionResponse,
    ExecutionRequest,
    ExecutionResponse,
    FeatureMaterializeRequest,
    FeatureMaterializeResponse,
    IngestSnapshotRequest,
    IngestSnapshotResponse,
    OnlineTreeUpdateResponse,
    PerformanceRefreshResponse,
    RetrainResponse,
    RLReplayUpdateResponse,
    RiskCheckRequest,
    RiskCheckResponse,
)
from app.orchestration.internal_scheduler import internal_scheduler
from app.services.decision_engine import decision_engine
from app.services.execution_service import execution_service
from app.services.feature_engineering import feature_engineering_service
from app.services.ingestion_service import ingestion_service
from app.services.online_learning_service import online_learning_service
from app.services.performance_service import performance_service
from app.services.risk_service import risk_service
from app.services.settings_service import settings_service
from app.services.state_service import state_service
from app.services.training_service import training_service

app = FastAPI(title="SmartBrain Service", version="0.4.0")


@app.on_event("startup")
async def on_startup() -> None:
    if settings.internal_scheduler_enabled:
        await internal_scheduler.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if settings.internal_scheduler_enabled:
        await internal_scheduler.stop()


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if settings.smartbrain_api_key and x_api_key != settings.smartbrain_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "smartbrain", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/settings/active", response_model=ActiveSettingsResponse, dependencies=[Depends(require_api_key)])
def get_active_settings() -> ActiveSettingsResponse:
    active = settings_service.get_active()
    return ActiveSettingsResponse(
        enabled=bool(active.get("enabled", False)),
        paper_mode=bool(active.get("paper_mode", True)),
        max_leverage=int(active.get("max_leverage", 3) or 3),
        max_portfolio_risk=float(active.get("max_portfolio_risk", 0.02) or 0.02),
        max_drawdown_circuit=float(active.get("max_drawdown_circuit", 0.08) or 0.08),
        assets_whitelist=list(active.get("assets_whitelist", ["BTC", "ETH"]) or ["BTC", "ETH"]),
        emergency_stop=bool(active.get("emergency_stop", False)),
    )


@app.post("/performance/refresh", response_model=PerformanceRefreshResponse, dependencies=[Depends(require_api_key)])
def refresh_performance() -> PerformanceRefreshResponse:
    result = performance_service.refresh_live()
    return PerformanceRefreshResponse(**result)


@app.post("/ingest/snapshot", response_model=IngestSnapshotResponse, dependencies=[Depends(require_api_key)])
def ingest_snapshot(payload: IngestSnapshotRequest | None = None) -> IngestSnapshotResponse:
    symbols = payload.symbols if payload else None
    timeframes = payload.timeframes if payload else None
    records = ingestion_service.ingest_snapshot(symbols=symbols, timeframes=timeframes)
    return IngestSnapshotResponse(success=True, ingested_at=datetime.now(timezone.utc), records_written=records)


@app.post("/features/materialize", response_model=FeatureMaterializeResponse, dependencies=[Depends(require_api_key)])
def materialize_features(payload: FeatureMaterializeRequest) -> FeatureMaterializeResponse:
    written = feature_engineering_service.materialize(symbols=payload.symbols, timeframes=payload.timeframes)
    return FeatureMaterializeResponse(success=True, vectors_written=written)


@app.post("/decision/run", response_model=DecisionResponse, dependencies=[Depends(require_api_key)])
def run_decision(payload: DecisionRequest) -> DecisionResponse:
    state = state_service.build_state(symbol=payload.symbol, mode=payload.mode)
    decision = decision_engine.make_decision(state=state)
    return DecisionResponse(success=True, decision=decision)


@app.post("/risk/check", response_model=RiskCheckResponse, dependencies=[Depends(require_api_key)])
def risk_check(payload: RiskCheckRequest) -> RiskCheckResponse:
    return risk_service.check(payload)


@app.post("/execution/execute", response_model=ExecutionResponse, dependencies=[Depends(require_api_key)])
def execute(payload: ExecutionRequest) -> ExecutionResponse:
    risk = risk_service.check(
        RiskCheckRequest(symbol=payload.symbol, mode=payload.mode, requested_leverage=payload.decision.leverage)
    )
    if not risk.allowed:
        return ExecutionResponse(success=False, mode=payload.mode, status="blocked_by_risk", details={"reasons": risk.reasons})

    state = payload.state if payload.state else state_service.build_state(symbol=payload.symbol, mode=payload.mode)
    return execution_service.execute(
        ExecutionRequest(symbol=payload.symbol, mode=payload.mode, decision=payload.decision, state=state),
        max_portfolio_risk=risk.max_position_pct,
    )


@app.post("/autonomous/cycle", response_model=AutonomousCycleResponse, dependencies=[Depends(require_api_key)])
def autonomous_cycle(payload: AutonomousCycleRequest) -> AutonomousCycleResponse:
    risk = risk_service.check(
        RiskCheckRequest(symbol=payload.symbol, mode=payload.mode, requested_leverage=1)
    )
    if not risk.allowed:
        return AutonomousCycleResponse(success=False, risk=risk, decision=None, execution=None)

    state = state_service.build_state(symbol=payload.symbol, mode=payload.mode)
    decision = decision_engine.make_decision(
        state=state,
        max_leverage=risk.max_leverage,
    )
    execution = execution_service.execute(
        ExecutionRequest(symbol=payload.symbol, mode=payload.mode, decision=decision, state=state),
        max_portfolio_risk=risk.max_position_pct,
    )
    return AutonomousCycleResponse(success=execution.success, risk=risk, decision=decision, execution=execution)


@app.post("/training/online-tree", response_model=OnlineTreeUpdateResponse, dependencies=[Depends(require_api_key)])
def online_tree_update() -> OnlineTreeUpdateResponse:
    return OnlineTreeUpdateResponse(**online_learning_service.run_tree_incremental_update())


@app.post("/training/rl-replay", response_model=RLReplayUpdateResponse, dependencies=[Depends(require_api_key)])
def rl_replay_update() -> RLReplayUpdateResponse:
    return RLReplayUpdateResponse(**online_learning_service.run_rl_replay_update())


@app.post("/training/daily", response_model=RetrainResponse, dependencies=[Depends(require_api_key)])
def daily_retrain() -> RetrainResponse:
    version, promoted = training_service.retrain_daily()
    return RetrainResponse(success=True, new_version=version, promoted=promoted)

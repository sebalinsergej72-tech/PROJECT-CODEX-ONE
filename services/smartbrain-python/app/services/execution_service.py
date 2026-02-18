from __future__ import annotations

from datetime import datetime, timezone

from app.models.schemas import ExecutionRequest, ExecutionResponse
from app.storage.supabase_repo import repo
from app.trading.hyperliquid_trader import hyperliquid_trader


class ExecutionService:
    @staticmethod
    def _safe_float(value: object, default: float = 0.0) -> float:
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def _estimate_immediate_reward(self, symbol: str, action: str) -> float | None:
        signal = 0.0
        if action == "LONG":
            signal = 1.0
        elif action == "SHORT":
            signal = -1.0

        if signal == 0.0:
            return 0.0

        rows = repo.select(
            "market_data",
            filters={"symbol": symbol, "timeframe": "1m", "exchange": "hyperliquid"},
            order_by="ts",
            desc=True,
            limit=2,
        )
        if len(rows) < 2:
            return None

        latest = self._safe_float(rows[0].get("close"), 0.0)
        prev = self._safe_float(rows[1].get("close"), 0.0)
        if latest <= 0 or prev <= 0:
            return None

        change = (latest - prev) / prev
        fee = 0.0006
        return signal * change - fee

    def execute(self, payload: ExecutionRequest, max_portfolio_risk: float) -> ExecutionResponse:
        if payload.decision.action == "FLAT":
            result = ExecutionResponse(
                success=True,
                mode=payload.mode,
                status="no_trade",
                order_id=None,
                details={"symbol": payload.symbol, "reason": "decision_flat"},
            )
        elif payload.mode == "paper":
            result = ExecutionResponse(
                success=True,
                mode=payload.mode,
                status="paper_filled",
                order_id=f"paper-{int(datetime.now(timezone.utc).timestamp())}",
                details={
                    "symbol": payload.symbol,
                    "action": payload.decision.action,
                    "size_pct": payload.decision.size_pct,
                    "leverage": payload.decision.leverage,
                },
            )
        else:
            order = hyperliquid_trader.place_market_order(
                symbol=payload.symbol,
                is_buy=payload.decision.action == "LONG",
                position_size_pct=payload.decision.size_pct,
                leverage=payload.decision.leverage,
                max_portfolio_risk=max_portfolio_risk,
            )
            result = ExecutionResponse(
                success=order.get("status") == "submitted",
                mode=payload.mode,
                status=order.get("status", "failed"),
                order_id=None,
                details={"symbol": payload.symbol, **order},
            )

        reward_proxy = self._estimate_immediate_reward(payload.symbol, payload.decision.action)

        repo.insert(
            "experience_replay",
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "symbol": payload.symbol,
                "mode": payload.mode,
                "state": payload.state or {},
                "action": payload.decision.model_dump(),
                "outcome_1h": None,
                "outcome_4h": None,
                "outcome_24h": None,
                "reward": reward_proxy,
            },
        )

        repo.insert(
            "smartbrain_logs",
            {
                "level": "info" if result.success else "error",
                "message": f"Execution {result.status}",
                "context": {
                    "symbol": payload.symbol,
                    "mode": payload.mode,
                    "decision": payload.decision.model_dump(),
                    "reward_proxy": reward_proxy,
                    "details": result.details,
                },
            },
        )

        return result


execution_service = ExecutionService()

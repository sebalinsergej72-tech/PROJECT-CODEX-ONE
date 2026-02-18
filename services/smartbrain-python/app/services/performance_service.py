from __future__ import annotations

import math
from datetime import datetime, timezone

from app.storage.supabase_repo import repo
from app.trading.hyperliquid_trader import hyperliquid_trader


class PerformanceService:
    def _compute_drawdown(self, equity_values: list[float]) -> float:
        peak = 0.0
        max_dd = 0.0
        for value in equity_values:
            if value > peak:
                peak = value
            if peak > 0:
                dd = (peak - value) / peak
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    def _compute_stats(self, equity_values: list[float]) -> tuple[float, float, float]:
        if len(equity_values) < 3:
            return 0.0, 0.0, 0.0

        returns: list[float] = []
        for i in range(1, len(equity_values)):
            prev = equity_values[i - 1]
            cur = equity_values[i]
            if prev > 0:
                returns.append((cur - prev) / prev)

        if len(returns) < 2:
            return 0.0, 0.0, 0.0

        mean_r = sum(returns) / len(returns)
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)) if len(returns) > 1 else 0.0
        sharpe = (mean_r / std_r) * math.sqrt(len(returns)) if std_r > 0 else 0.0

        gains = sum(r for r in returns if r > 0)
        losses = abs(sum(r for r in returns if r < 0))
        profit_factor = (gains / losses) if losses > 0 else (gains if gains > 0 else 0.0)

        wins = sum(1 for r in returns if r > 0)
        win_rate = wins / len(returns)

        return sharpe, profit_factor, win_rate

    def refresh_live(self) -> dict:
        snapshot = hyperliquid_trader.get_account_snapshot()
        account_value = float(snapshot.get("account_value", 0.0) or 0.0)
        unrealized_pnl = float(snapshot.get("unrealized_pnl", 0.0) or 0.0)

        if account_value <= 0:
            return {
                "refreshed": False,
                "reason": "account_value_not_available",
                "max_drawdown": 0.0,
            }

        repo.insert(
            "smartbrain_equity_curve",
            {
                "ts": snapshot.get("ts", datetime.now(timezone.utc).isoformat()),
                "mode": "live",
                "equity": account_value,
                "unrealized_pnl": unrealized_pnl,
                "meta": {"source": "hyperliquid_user_state"},
            },
        )

        rows = repo.select("smartbrain_equity_curve", filters={"mode": "live"}, order_by="ts", desc=False, limit=5000)
        equity_values = [float(row.get("equity", 0.0) or 0.0) for row in rows if float(row.get("equity", 0.0) or 0.0) > 0]

        max_drawdown = self._compute_drawdown(equity_values)
        sharpe, profit_factor, win_rate = self._compute_stats(equity_values)

        active_models = repo.select("smartbrain_models", filters={"is_active": True}, order_by="created_at", desc=True, limit=1)
        model_version = active_models[0]["version"] if active_models else "n/a"

        repo.insert(
            "smartbrain_performance",
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "mode": "live",
                "sharpe": sharpe,
                "profit_factor": profit_factor,
                "max_drawdown": max_drawdown,
                "win_rate": win_rate,
                "model_version": model_version,
            },
        )

        return {
            "refreshed": True,
            "max_drawdown": max_drawdown,
            "sharpe": sharpe,
            "profit_factor": profit_factor,
            "win_rate": win_rate,
            "model_version": model_version,
        }

    def latest_drawdown(self, mode: str) -> float:
        rows = repo.select("smartbrain_performance", filters={"mode": mode}, order_by="ts", desc=True, limit=1)
        if rows:
            return float(rows[0].get("max_drawdown", 0.0) or 0.0)
        return 0.0


performance_service = PerformanceService()

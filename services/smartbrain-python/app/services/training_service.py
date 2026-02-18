from __future__ import annotations

from datetime import datetime, timezone

from app.storage.supabase_repo import repo


class TrainingService:
    def _latest_sharpe(self, mode: str) -> float:
        rows = repo.select("smartbrain_performance", filters={"mode": mode}, order_by="ts", desc=True, limit=1)
        if rows:
            return float(rows[0].get("sharpe", 0.0) or 0.0)
        return 0.0

    def _trim_versions(self, keep_last: int = 5) -> None:
        rows = repo.select("smartbrain_models", filters={"model_type": "ensemble+rl"}, order_by="created_at", desc=True, limit=100)
        for stale in rows[keep_last:]:
            model_id = stale.get("id")
            if model_id:
                repo.delete("smartbrain_models", {"id": model_id})

    def retrain_daily(self) -> tuple[str, bool]:
        # Placeholder for Optuna walk-forward + ensemble/RL retrain.
        version = f"smartbrain-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

        live_sharpe = self._latest_sharpe("live")
        paper_sharpe = self._latest_sharpe("paper")
        degradation = paper_sharpe - live_sharpe

        promote = degradation > 0.3
        active_exists = bool(repo.select("smartbrain_models", filters={"model_type": "ensemble+rl", "is_active": True}, limit=1))
        if not active_exists:
            promote = True

        repo.insert(
            "smartbrain_models",
            {
                "version": version,
                "model_type": "ensemble+rl",
                "storage_path": f"models/{version}",
                "metrics": {
                    "live_sharpe": live_sharpe,
                    "paper_sharpe": paper_sharpe,
                    "degradation": degradation,
                },
                "is_active": promote,
            },
        )

        if promote:
            # Keep only latest promoted model active.
            rows = repo.select("smartbrain_models", order_by="created_at", desc=True, limit=100)
            promoted_one = False
            for row in rows:
                row_id = row.get("id")
                row_version = row.get("version")
                if not row_id:
                    continue
                should_be_active = (row_version == version) and (not promoted_one)
                if should_be_active:
                    promoted_one = True
                repo.update("smartbrain_models", {"is_active": should_be_active}, {"id": row_id})

        self._trim_versions(keep_last=5)
        return version, promote


training_service = TrainingService()

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from stable_baselines3 import SAC

from app.services.state_features import state_to_features
from app.storage.supabase_repo import repo


@dataclass
class RLAction:
    action: str
    size_pct: float
    leverage: int
    confidence: float


class RLTradingAgent:
    def __init__(self) -> None:
        self.model: SAC | None = None
        self.loaded_version: str | None = None
        self.feature_columns: list[str] = []
        self.mu: np.ndarray | None = None
        self.sigma: np.ndarray | None = None

    def _load_active_model(self) -> None:
        rows = repo.select(
            "smartbrain_models",
            filters={"model_type": "rl-sac-replay", "is_active": True},
            order_by="created_at",
            desc=True,
            limit=1,
        )
        if not rows:
            self.model = None
            self.loaded_version = None
            self.feature_columns = []
            self.mu = None
            self.sigma = None
            return

        row = rows[0]
        version = row.get("version")
        if version == self.loaded_version:
            return

        storage_path = row.get("storage_path")
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        feature_path = metrics.get("feature_path")

        if not isinstance(storage_path, str) or not Path(storage_path).exists():
            self.model = None
            self.loaded_version = None
            return

        columns: list[str] = []
        mu: np.ndarray | None = None
        sigma: np.ndarray | None = None
        if isinstance(feature_path, str) and Path(feature_path).exists():
            try:
                payload = json.loads(Path(feature_path).read_text(encoding="utf-8"))
                columns = [str(c) for c in payload.get("feature_columns", [])]
                mu = np.array(payload.get("mu", []), dtype=np.float32)
                sigma = np.array(payload.get("sigma", []), dtype=np.float32)
            except Exception:
                columns = []
                mu = None
                sigma = None

        if not columns or mu is None or sigma is None or len(columns) != len(mu) or len(columns) != len(sigma):
            self.model = None
            self.loaded_version = None
            return

        try:
            self.model = SAC.load(storage_path, device="cpu")
            self.loaded_version = str(version)
            self.feature_columns = columns
            self.mu = mu
            self.sigma = np.where(sigma < 1e-6, 1.0, sigma)
        except Exception:
            self.model = None
            self.loaded_version = None
            self.feature_columns = []
            self.mu = None
            self.sigma = None

    @staticmethod
    def _fallback() -> RLAction:
        return RLAction(action="FLAT", size_pct=0.0, leverage=1, confidence=0.5)

    def infer(self, state: dict) -> RLAction:
        self._load_active_model()
        if not self.model or not self.feature_columns or self.mu is None or self.sigma is None:
            return self._fallback()

        features = state_to_features(state)
        if not features:
            return self._fallback()

        obs = np.array([features.get(col, 0.0) for col in self.feature_columns], dtype=np.float32)
        obs = np.clip((obs - self.mu) / self.sigma, -10.0, 10.0)

        action_arr, _ = self.model.predict(obs, deterministic=True)
        signal = float(np.clip(action_arr[0], -1.0, 1.0))

        if signal > 0.15:
            action = "LONG"
        elif signal < -0.15:
            action = "SHORT"
        else:
            action = "FLAT"

        size_pct = 0.0 if action == "FLAT" else float(min(0.2, max(0.01, abs(signal) * 0.12)))
        leverage = 1 if action == "FLAT" else int(min(6, max(1, 1 + abs(signal) * 5)))
        confidence = float(min(0.95, max(0.35, abs(signal))))

        return RLAction(action=action, size_pct=size_pct, leverage=leverage, confidence=confidence)


rl_trading_agent = RLTradingAgent()

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import xgboost as xgb

from app.services.state_features import state_to_features
from app.storage.supabase_repo import repo


@dataclass
class EnsemblePrediction:
    direction_score: float
    expected_volatility: float
    confidence: float


class EnsemblePredictor:
    def __init__(self) -> None:
        self.booster: xgb.Booster | None = None
        self.feature_columns: list[str] = []
        self.loaded_version: str | None = None

    def _load_active_model(self) -> None:
        rows = repo.select(
            "smartbrain_models",
            filters={"model_type": "xgboost-online", "is_active": True},
            order_by="created_at",
            desc=True,
            limit=1,
        )
        if not rows:
            self.booster = None
            self.feature_columns = []
            self.loaded_version = None
            return

        row = rows[0]
        version = row.get("version")
        if version == self.loaded_version:
            return

        storage_path = row.get("storage_path")
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        feature_path = metrics.get("feature_path")

        if not isinstance(storage_path, str) or not Path(storage_path).exists():
            self.booster = None
            self.feature_columns = []
            self.loaded_version = None
            return

        columns: list[str] = []
        if isinstance(feature_path, str) and Path(feature_path).exists():
            try:
                payload = json.loads(Path(feature_path).read_text(encoding="utf-8"))
                if isinstance(payload, list):
                    columns = [str(col) for col in payload]
            except Exception:
                columns = []

        if not columns:
            self.booster = None
            self.feature_columns = []
            self.loaded_version = None
            return

        booster = xgb.Booster()
        booster.load_model(storage_path)

        self.booster = booster
        self.feature_columns = columns
        self.loaded_version = str(version)

    @staticmethod
    def _default_prediction() -> EnsemblePrediction:
        return EnsemblePrediction(direction_score=0.1, expected_volatility=0.02, confidence=0.55)

    def predict(self, state: dict) -> EnsemblePrediction:
        self._load_active_model()
        if not self.booster or not self.feature_columns:
            return self._default_prediction()

        features = state_to_features(state)
        if not features:
            return self._default_prediction()

        vector = np.array([[features.get(col, 0.0) for col in self.feature_columns]], dtype=np.float32)
        matrix = xgb.DMatrix(vector, feature_names=self.feature_columns)
        score = float(self.booster.predict(matrix)[0])

        direction = float(np.tanh(score * 4.0))
        confidence = float(min(0.95, max(0.2, abs(direction) + 0.2)))

        vol_hint = abs(float(features.get("fv_1m_atr", 0.02)))
        expected_volatility = float(min(0.2, max(0.001, vol_hint if vol_hint > 0 else 0.02)))

        return EnsemblePrediction(
            direction_score=direction,
            expected_volatility=expected_volatility,
            confidence=confidence,
        )


ensemble_predictor = EnsemblePredictor()

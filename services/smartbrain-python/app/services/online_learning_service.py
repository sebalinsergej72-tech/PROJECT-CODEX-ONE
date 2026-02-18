from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import gymnasium as gym
import numpy as np
import xgboost as xgb
from gymnasium import spaces
from stable_baselines3 import SAC

from app.config import settings
from app.services.state_features import state_to_features
from app.storage.supabase_repo import repo


class ReplayDatasetEnv(gym.Env):
    metadata = {"render_modes": []}

    def __init__(self, obs: np.ndarray, target_actions: np.ndarray, rewards: np.ndarray):
        super().__init__()
        self.obs = obs.astype(np.float32)
        self.target_actions = target_actions.astype(np.float32)
        self.rewards = rewards.astype(np.float32)
        self.index = 0

        self.observation_space = spaces.Box(low=-10.0, high=10.0, shape=(self.obs.shape[1],), dtype=np.float32)
        self.action_space = spaces.Box(low=-1.0, high=1.0, shape=(1,), dtype=np.float32)

    def reset(self, *, seed: int | None = None, options: dict | None = None):
        super().reset(seed=seed)
        self.index = int(self.np_random.integers(0, len(self.obs)))
        return self.obs[self.index], {}

    def step(self, action):
        value = float(np.clip(action[0], -1.0, 1.0))
        target = float(self.target_actions[self.index])
        base = float(self.rewards[self.index])

        reward = base - abs(value - target) * 0.05

        self.index = (self.index + 1) % len(self.obs)
        terminated = self.index == 0
        truncated = False
        return self.obs[self.index], reward, terminated, truncated, {}


class OnlineLearningService:
    def __init__(self) -> None:
        self.model_store = Path(settings.model_store_dir)
        self.model_store.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _action_signal(action_payload: Any) -> float:
        if isinstance(action_payload, dict):
            action = str(action_payload.get("action", "FLAT"))
            size = float(action_payload.get("size_pct", 0.0) or 0.0)
        else:
            action = "FLAT"
            size = 0.0

        if action == "LONG":
            return max(0.05, size)
        if action == "SHORT":
            return -max(0.05, size)
        return 0.0

    @staticmethod
    def _reward_value(row: dict) -> float | None:
        reward = row.get("reward")
        if isinstance(reward, (int, float)):
            return float(reward)

        for key in ("outcome_1h", "outcome_4h", "outcome_24h"):
            payload = row.get(key)
            if isinstance(payload, dict):
                for metric_key in ("reward", "pnl", "return", "value"):
                    metric = payload.get(metric_key)
                    if isinstance(metric, (int, float)):
                        return float(metric)
        return None

    def _build_training_matrix(self, min_samples: int, limit: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]] | None:
        rows = repo.select("experience_replay", order_by="ts", desc=True, limit=limit)
        rows = list(reversed(rows))

        samples: list[dict[str, float]] = []
        rewards: list[float] = []
        action_signals: list[float] = []

        for row in rows:
            state = row.get("state") if isinstance(row.get("state"), dict) else {}
            features = state_to_features(state)
            reward = self._reward_value(row)
            if not features or reward is None:
                continue

            samples.append(features)
            rewards.append(float(reward))
            action_signals.append(self._action_signal(row.get("action")))

        if len(samples) < min_samples:
            return None

        columns = sorted({key for sample in samples for key in sample.keys()})
        matrix = np.array([[sample.get(col, 0.0) for col in columns] for sample in samples], dtype=np.float32)
        matrix = np.nan_to_num(matrix, nan=0.0, posinf=0.0, neginf=0.0)

        y_reward = np.array(rewards, dtype=np.float32)
        y_action = np.array(action_signals, dtype=np.float32)
        return matrix, y_reward, y_action, columns

    def _latest_model_row(self, model_type: str, active_only: bool = False) -> dict | None:
        filters: dict[str, Any] = {"model_type": model_type}
        if active_only:
            filters["is_active"] = True

        rows = repo.select("smartbrain_models", filters=filters, order_by="created_at", desc=True, limit=1)
        return rows[0] if rows else None

    def _set_single_active_version(self, model_type: str, version: str) -> None:
        rows = repo.select("smartbrain_models", filters={"model_type": model_type}, order_by="created_at", desc=True, limit=200)
        for row in rows:
            row_id = row.get("id")
            row_version = row.get("version")
            if row_id:
                repo.update("smartbrain_models", {"is_active": row_version == version}, {"id": row_id})

    def _insert_training_log(self, level: str, message: str, context: dict) -> None:
        repo.insert("smartbrain_logs", {"level": level, "message": message, "context": context})

    def run_tree_incremental_update(self) -> dict:
        dataset = self._build_training_matrix(
            min_samples=settings.online_tree_min_samples,
            limit=settings.online_tree_window,
        )
        if dataset is None:
            return {
                "success": True,
                "status": "skipped",
                "samples_used": 0,
                "model_version": None,
                "promoted": False,
                "rmse": None,
                "reason": "not_enough_labeled_experiences",
            }

        x_matrix, y_reward, _, columns = dataset
        dtrain = xgb.DMatrix(x_matrix, label=y_reward, feature_names=columns)

        params = {
            "objective": "reg:squarederror",
            "eta": 0.05,
            "max_depth": 6,
            "subsample": 0.85,
            "colsample_bytree": 0.85,
            "eval_metric": "rmse",
        }

        active_row = self._latest_model_row("xgboost-online", active_only=True)
        existing_model_path: Path | None = None
        existing_rmse = None
        if active_row:
            if isinstance(active_row.get("metrics"), dict):
                existing_rmse = active_row["metrics"].get("rmse")
            storage_path = active_row.get("storage_path")
            if isinstance(storage_path, str):
                candidate = Path(storage_path)
                if candidate.exists():
                    existing_model_path = candidate

        booster = xgb.train(
            params=params,
            dtrain=dtrain,
            num_boost_round=settings.online_tree_boost_rounds,
            xgb_model=str(existing_model_path) if existing_model_path else None,
        )

        preds = booster.predict(dtrain)
        rmse = float(np.sqrt(np.mean((preds - y_reward) ** 2)))

        version = f"xgb-online-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        output_dir = self.model_store / "xgboost"
        output_dir.mkdir(parents=True, exist_ok=True)
        model_path = output_dir / f"{version}.json"
        features_path = output_dir / f"{version}.features.json"

        booster.save_model(model_path)
        features_path.write_text(json.dumps(columns), encoding="utf-8")

        promote = False
        if existing_rmse is None:
            promote = True
        else:
            try:
                promote = rmse <= float(existing_rmse) * 1.03
            except (TypeError, ValueError):
                promote = True

        metrics = {
            "rmse": rmse,
            "samples_used": int(len(y_reward)),
            "feature_columns": len(columns),
            "feature_path": str(features_path),
        }
        repo.insert(
            "smartbrain_models",
            {
                "version": version,
                "model_type": "xgboost-online",
                "storage_path": str(model_path),
                "metrics": metrics,
                "is_active": promote,
            },
        )

        if promote:
            self._set_single_active_version("xgboost-online", version)

        self._insert_training_log(
            "info",
            "Online tree update completed",
            {
                "version": version,
                "promoted": promote,
                "rmse": rmse,
                "samples": int(len(y_reward)),
            },
        )

        return {
            "success": True,
            "status": "updated",
            "samples_used": int(len(y_reward)),
            "model_version": version,
            "promoted": promote,
            "rmse": rmse,
            "reason": None,
        }

    def run_rl_replay_update(self) -> dict:
        dataset = self._build_training_matrix(
            min_samples=settings.rl_replay_min_samples,
            limit=settings.rl_replay_window,
        )
        if dataset is None:
            return {
                "success": True,
                "status": "skipped",
                "samples_used": 0,
                "model_version": None,
                "promoted": False,
                "action_mae": None,
                "reason": "not_enough_labeled_experiences",
            }

        x_matrix, y_reward, y_action, columns = dataset

        mu = x_matrix.mean(axis=0)
        sigma = x_matrix.std(axis=0)
        sigma = np.where(sigma < 1e-6, 1.0, sigma)
        x_norm = (x_matrix - mu) / sigma
        x_norm = np.clip(x_norm, -10.0, 10.0)

        env = ReplayDatasetEnv(obs=x_norm, target_actions=y_action, rewards=y_reward)

        active_row = self._latest_model_row("rl-sac-replay", active_only=True)
        existing_mae = None
        model: SAC | None = None
        if active_row:
            if isinstance(active_row.get("metrics"), dict):
                existing_mae = active_row["metrics"].get("action_mae")
            storage_path = active_row.get("storage_path")
            if isinstance(storage_path, str) and Path(storage_path).exists():
                try:
                    model = SAC.load(storage_path, env=env, device="cpu")
                except Exception:
                    model = None

        if model is None:
            model = SAC(
                "MlpPolicy",
                env,
                learning_rate=3e-4,
                batch_size=64,
                buffer_size=10000,
                train_freq=1,
                gradient_steps=1,
                verbose=0,
                device="cpu",
            )

        total_steps = max(settings.rl_replay_steps, len(x_norm) * 2)
        model.learn(total_timesteps=total_steps, reset_num_timesteps=False, progress_bar=False)

        predictions = []
        for obs in x_norm:
            action, _ = model.predict(obs, deterministic=True)
            predictions.append(float(np.clip(action[0], -1.0, 1.0)))

        mae = float(np.mean(np.abs(np.array(predictions) - y_action)))

        version = f"rl-replay-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        output_dir = self.model_store / "rl"
        output_dir.mkdir(parents=True, exist_ok=True)
        model_path = output_dir / f"{version}.zip"
        meta_path = output_dir / f"{version}.meta.json"

        model.save(str(model_path.with_suffix("")))
        meta_path.write_text(
            json.dumps(
                {
                    "feature_columns": columns,
                    "mu": mu.tolist(),
                    "sigma": sigma.tolist(),
                }
            ),
            encoding="utf-8",
        )

        promote = False
        if existing_mae is None:
            promote = True
        else:
            try:
                promote = mae <= float(existing_mae) * 1.03
            except (TypeError, ValueError):
                promote = True

        repo.insert(
            "smartbrain_models",
            {
                "version": version,
                "model_type": "rl-sac-replay",
                "storage_path": str(model_path),
                "metrics": {
                    "action_mae": mae,
                    "samples_used": int(len(y_action)),
                    "feature_path": str(meta_path),
                },
                "is_active": promote,
            },
        )

        if promote:
            self._set_single_active_version("rl-sac-replay", version)

        self._insert_training_log(
            "info",
            "RL replay update completed",
            {
                "version": version,
                "promoted": promote,
                "action_mae": mae,
                "samples": int(len(y_action)),
                "steps": total_steps,
            },
        )

        return {
            "success": True,
            "status": "updated",
            "samples_used": int(len(y_action)),
            "model_version": version,
            "promoted": promote,
            "action_mae": mae,
            "reason": None,
        }


online_learning_service = OnlineLearningService()

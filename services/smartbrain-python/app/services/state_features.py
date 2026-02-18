from __future__ import annotations

from datetime import datetime


def flatten_numeric(payload: dict, prefix: str = "", out: dict[str, float] | None = None) -> dict[str, float]:
    out = out or {}
    for key, value in payload.items():
        col = f"{prefix}_{key}" if prefix else str(key)
        if isinstance(value, dict):
            flatten_numeric(value, prefix=col, out=out)
        elif isinstance(value, bool):
            out[col] = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            out[col] = float(value)
    return out


def encode_timestamp_iso(iso_ts: str | None) -> dict[str, float]:
    if not iso_ts:
        return {}
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return {
            "ts_hour": float(dt.hour),
            "ts_weekday": float(dt.weekday()),
        }
    except ValueError:
        return {}


def state_to_features(state: dict) -> dict[str, float]:
    features: dict[str, float] = {}
    flatten_numeric(state, out=features)
    timestamp = state.get("timestamp") if isinstance(state, dict) else None
    features.update(encode_timestamp_iso(timestamp if isinstance(timestamp, str) else None))
    return features

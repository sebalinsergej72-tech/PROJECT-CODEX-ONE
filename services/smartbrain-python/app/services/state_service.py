from __future__ import annotations

from datetime import datetime, timezone

from app.storage.supabase_repo import repo
from app.trading.hyperliquid_trader import hyperliquid_trader


class StateService:
    def _latest_feature_rows(self, symbol: str) -> list[dict]:
        rows = repo.select("feature_vectors", filters={"symbol": symbol}, order_by="ts", desc=True, limit=24)
        dedup: dict[str, dict] = {}
        for row in rows:
            timeframe = row.get("timeframe", "1m")
            if timeframe not in dedup:
                dedup[timeframe] = row
        return list(dedup.values())

    def _latest_sentiment(self, symbol: str) -> dict:
        rows = repo.select("sentiment_scores", filters={"symbol": symbol}, order_by="ts", desc=True, limit=1)
        return rows[0] if rows else {}

    def _latest_macro(self) -> dict:
        rows = repo.select("macro_indicators", order_by="ts", desc=True, limit=1)
        return rows[0] if rows else {}

    @staticmethod
    def _numeric(value: object, default: float = 0.0) -> float:
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def build_state(self, symbol: str, mode: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        state: dict = {
            "symbol": symbol,
            "mode": mode,
            "timestamp": now,
        }

        for row in self._latest_feature_rows(symbol):
            timeframe = row.get("timeframe", "1m")
            features = row.get("features", {}) if isinstance(row.get("features"), dict) else {}
            for key, value in features.items():
                if isinstance(value, (int, float)):
                    state[f"fv_{timeframe}_{key}"] = float(value)

        sentiment = self._latest_sentiment(symbol)
        state["sentiment_score"] = self._numeric(sentiment.get("sentiment_score"))
        state["sentiment_confidence"] = self._numeric(sentiment.get("confidence"))

        macro = self._latest_macro()
        state["macro_btc_dominance"] = self._numeric(macro.get("btc_dominance"))
        state["macro_fear_greed"] = self._numeric(macro.get("fear_greed"))
        state["macro_total_market_cap"] = self._numeric(macro.get("total_market_cap"))
        state["macro_alt_corr_index"] = self._numeric(macro.get("alt_corr_index"))

        if mode == "live":
            account_snapshot = hyperliquid_trader.get_account_snapshot()
            state["portfolio_account_value"] = self._numeric(account_snapshot.get("account_value"))
            state["portfolio_unrealized_pnl"] = self._numeric(account_snapshot.get("unrealized_pnl"))
            state["portfolio_withdrawable"] = self._numeric(account_snapshot.get("withdrawable"))

        return state


state_service = StateService()

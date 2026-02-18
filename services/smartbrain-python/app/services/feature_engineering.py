from datetime import datetime, timezone

from app.storage.supabase_repo import repo


class FeatureEngineeringService:
    def materialize(self, symbols: list[str], timeframes: list[str]) -> int:
        # TODO: replace with full TA + statistical + sentiment + macro fusion features.
        written = 0
        for symbol in symbols:
            for timeframe in timeframes:
                repo.insert(
                    "feature_vectors",
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "features": {
                            "rsi": 50,
                            "atr": 0,
                            "trend_strength": 0,
                            "sentiment_decay": 0,
                        },
                        "regime": "unknown",
                    },
                )
                written += 1
        return written


feature_engineering_service = FeatureEngineeringService()

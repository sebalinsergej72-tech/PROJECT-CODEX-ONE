from __future__ import annotations

from datetime import datetime, timezone

from app.config import settings
from app.data_clients.hyperliquid_client import hyperliquid_client
from app.data_clients.market_context_client import market_context_client
from app.data_clients.sentiment_client import sentiment_client
from app.storage.supabase_repo import repo


class IngestionService:
    def ingest_snapshot(self, symbols: list[str] | None = None, timeframes: list[str] | None = None) -> int:
        symbols = symbols or settings.ingest_symbols
        timeframes = timeframes or settings.ingest_timeframes

        market_rows: list[dict] = []
        for symbol in symbols:
            for timeframe in timeframes:
                try:
                    market_rows.extend(hyperliquid_client.fetch_market_rows(symbol=symbol, timeframe=timeframe))
                except Exception as exc:
                    repo.insert(
                        "smartbrain_logs",
                        {
                            "level": "error",
                            "message": "Hyperliquid ingestion failed",
                            "context": {"symbol": symbol, "timeframe": timeframe, "error": str(exc)},
                        },
                    )

        if market_rows:
            repo.insert_many("market_data", market_rows)

        macro = market_context_client.fetch_macro_snapshot()
        repo.insert("macro_indicators", macro)

        sentiment_rows = [sentiment_client.fetch_sentiment(symbol=symbol) for symbol in symbols]
        repo.insert_many("sentiment_scores", sentiment_rows)

        repo.insert(
            "smartbrain_logs",
            {
                "level": "info",
                "message": "Snapshot ingested",
                "context": {
                    "symbols": symbols,
                    "timeframes": timeframes,
                    "market_rows": len(market_rows),
                    "ts": datetime.now(timezone.utc).isoformat(),
                },
            },
        )

        return len(market_rows) + len(sentiment_rows) + 1


ingestion_service = IngestionService()

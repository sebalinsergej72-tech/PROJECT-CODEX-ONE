from datetime import datetime, timezone


class SentimentClient:
    def fetch_sentiment(self, symbol: str) -> dict:
        # TODO: integrate CryptoPanic/NewsAPI/LunarCrush + Grok/OpenAI scoring.
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "source": "bootstrap",
            "sentiment_score": 0.0,
            "confidence": 0.1,
            "summary": "Sentiment pipeline not connected yet.",
        }


sentiment_client = SentimentClient()

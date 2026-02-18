from __future__ import annotations

from datetime import datetime, timezone

import ccxt
import httpx
import numpy as np

from app.config import settings


class MarketContextClient:
    def __init__(self) -> None:
        self.http = httpx.Client(timeout=15)
        self.binance = ccxt.binance({"enableRateLimit": True})
        self.bybit = ccxt.bybit({"enableRateLimit": True})

    @staticmethod
    def _safe_float(value: object, default: float = 0.0) -> float:
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def _fetch_fear_greed(self) -> float:
        try:
            response = self.http.get("https://api.alternative.me/fng/?limit=1")
            response.raise_for_status()
            payload = response.json()
            return self._safe_float(payload["data"][0]["value"], 50.0)
        except Exception:
            return 50.0

    def _fetch_coingecko_metrics(self) -> tuple[float, float]:
        try:
            headers = {}
            if settings.coingecko_api_key:
                headers["x-cg-pro-api-key"] = settings.coingecko_api_key
            response = self.http.get("https://api.coingecko.com/api/v3/global", headers=headers)
            response.raise_for_status()
            data = response.json().get("data", {})
            dominance = self._safe_float(data.get("market_cap_percentage", {}).get("btc"))
            total_mcap = self._safe_float(data.get("total_market_cap", {}).get("usd"))
            return dominance, total_mcap
        except Exception:
            return 0.0, 0.0

    def _fetch_alt_corr_index(self) -> float:
        symbols = ["ETH/USDT", "SOL/USDT", "XRP/USDT"]
        btc = self.binance.fetch_ohlcv("BTC/USDT", timeframe="1h", limit=48)
        if len(btc) < 10:
            return 0.0

        btc_returns = np.diff(np.log(np.array([x[4] for x in btc], dtype=float)))
        correlations: list[float] = []

        for symbol in symbols:
            try:
                ohlcv = self.bybit.fetch_ohlcv(symbol, timeframe="1h", limit=48)
                closes = np.array([x[4] for x in ohlcv], dtype=float)
                if len(closes) != len(btc):
                    continue
                returns = np.diff(np.log(closes))
                corr = float(np.corrcoef(btc_returns, returns)[0, 1])
                if not np.isnan(corr):
                    correlations.append(corr)
            except Exception:
                continue

        if not correlations:
            return 0.0
        return float(np.mean(correlations))

    def fetch_macro_snapshot(self) -> dict:
        dominance, total_mcap = self._fetch_coingecko_metrics()
        fear_greed = self._fetch_fear_greed()
        alt_corr = self._fetch_alt_corr_index()

        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "btc_dominance": dominance,
            "fear_greed": fear_greed,
            "total_market_cap": total_mcap,
            "alt_corr_index": alt_corr,
            "onchain_signal": {},
        }


market_context_client = MarketContextClient()

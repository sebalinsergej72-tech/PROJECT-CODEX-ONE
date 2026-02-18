from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from hyperliquid.info import Info
from hyperliquid.utils import constants

from app.config import settings


class HyperliquidClient:
    def __init__(self) -> None:
        base_url = constants.TESTNET_API_URL if settings.hyperliquid_use_testnet else constants.MAINNET_API_URL
        self.info = Info(base_url, skip_ws=True)

    @staticmethod
    def _to_float(value: object, default: float = 0.0) -> float:
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def _fetch_funding_and_oi(self, symbol: str) -> tuple[float, float]:
        try:
            meta, asset_ctxs = self.info.meta_and_asset_ctxs()
            universe = meta.get("universe", []) if isinstance(meta, dict) else []
            for idx, item in enumerate(universe):
                if item.get("name") == symbol and idx < len(asset_ctxs):
                    ctx = asset_ctxs[idx]
                    funding = self._to_float(ctx.get("funding"))
                    open_interest = self._to_float(ctx.get("openInterest"))
                    return funding, open_interest
        except Exception:
            pass
        return 0.0, 0.0

    def _fetch_orderbook_imbalance(self, symbol: str, depth: int = 10) -> float:
        try:
            l2 = self.info.l2_snapshot(symbol)
            levels = l2.get("levels", []) if isinstance(l2, dict) else []
            bids = levels[0][:depth] if len(levels) > 0 else []
            asks = levels[1][:depth] if len(levels) > 1 else []
            bid_vol = sum(self._to_float(level.get("sz")) for level in bids)
            ask_vol = sum(self._to_float(level.get("sz")) for level in asks)
            denom = bid_vol + ask_vol
            if denom <= 0:
                return 0.0
            return (bid_vol - ask_vol) / denom
        except Exception:
            return 0.0

    def fetch_market_rows(self, symbol: str, timeframe: str, lookback_minutes: int = 720) -> list[dict]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=lookback_minutes)

        candles = self.info.candles_snapshot(
            symbol,
            timeframe,
            int(start.timestamp() * 1000),
            int(end.timestamp() * 1000),
        )

        funding, open_interest = self._fetch_funding_and_oi(symbol)
        orderbook_imbalance = self._fetch_orderbook_imbalance(symbol=symbol)

        rows: list[dict] = []
        for candle in candles:
            ts_ms = candle.get("t")
            ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else end

            row = {
                "ts": ts.isoformat(),
                "symbol": symbol,
                "exchange": "hyperliquid",
                "timeframe": timeframe,
                "open": self._to_float(candle.get("o")),
                "high": self._to_float(candle.get("h")),
                "low": self._to_float(candle.get("l")),
                "close": self._to_float(candle.get("c")),
                "volume": self._to_float(candle.get("v")),
                "orderbook_imbalance": orderbook_imbalance,
                "funding_rate": funding,
                "open_interest": open_interest,
                "liquidations": 0.0,
            }
            if not any(math.isnan(v) for k, v in row.items() if isinstance(v, float)):
                rows.append(row)

        return rows


hyperliquid_client = HyperliquidClient()

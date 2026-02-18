from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from app.config import settings


class HyperliquidTrader:
    def __init__(self) -> None:
        base_url = constants.TESTNET_API_URL if settings.hyperliquid_use_testnet else constants.MAINNET_API_URL
        self.info = Info(base_url, skip_ws=True)
        self.exchange: Exchange | None = None

        if settings.hyperliquid_private_key and settings.hyperliquid_account_address:
            account = Account.from_key(settings.hyperliquid_private_key)
            self.exchange = Exchange(account, base_url, account_address=settings.hyperliquid_account_address)

    @staticmethod
    def _safe_float(value: object, default: float = 0.0) -> float:
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def get_user_state(self) -> dict[str, Any]:
        if not settings.hyperliquid_account_address:
            return {}
        try:
            state = self.info.user_state(settings.hyperliquid_account_address)
            return state if isinstance(state, dict) else {}
        except Exception:
            return {}

    def get_account_value(self) -> float:
        snapshot = self.get_account_snapshot()
        return snapshot["account_value"]

    def get_account_snapshot(self) -> dict[str, float | str]:
        state = self.get_user_state()
        margin_summary = state.get("marginSummary", {}) if isinstance(state, dict) else {}
        cross_summary = state.get("crossMarginSummary", {}) if isinstance(state, dict) else {}
        withdrawable = self._safe_float(state.get("withdrawable"), 0.0)

        account_value = self._safe_float(margin_summary.get("accountValue"), 0.0)
        if account_value <= 0:
            account_value = self._safe_float(cross_summary.get("accountValue"), 0.0)

        unrealized_pnl = self._safe_float(margin_summary.get("unrealizedPnl"), 0.0)
        if unrealized_pnl == 0:
            unrealized_pnl = self._safe_float(cross_summary.get("unrealizedPnl"), 0.0)

        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "account_value": account_value,
            "withdrawable": withdrawable,
            "unrealized_pnl": unrealized_pnl,
        }

    def get_mid_price(self, symbol: str) -> float:
        mids = self.info.all_mids()
        return self._safe_float(mids.get(symbol), 0.0) if isinstance(mids, dict) else 0.0

    def place_market_order(
        self,
        symbol: str,
        is_buy: bool,
        position_size_pct: float,
        leverage: int,
        max_portfolio_risk: float,
    ) -> dict:
        if not self.exchange:
            return {"status": "skipped", "reason": "exchange_not_initialized"}

        account_value = self.get_account_value()
        mid = self.get_mid_price(symbol)
        if account_value <= 0 or mid <= 0:
            return {"status": "skipped", "reason": "invalid_account_or_price"}

        notional = account_value * max_portfolio_risk * max(0.0, position_size_pct)
        size = round(notional / mid, 6)
        if size <= 0:
            return {"status": "skipped", "reason": "size_zero"}

        self.exchange.update_leverage(leverage, symbol, is_cross=True)
        response = self.exchange.market_open(symbol, is_buy, size, None, 0.01)
        return {"status": "submitted", "response": response, "size": size, "price_ref": mid}


hyperliquid_trader = HyperliquidTrader()

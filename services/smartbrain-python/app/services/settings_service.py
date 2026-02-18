from __future__ import annotations

from app.storage.supabase_repo import repo


class SettingsService:
    def defaults(self) -> dict:
        return {
            "enabled": False,
            "paper_mode": True,
            "risk_level": "medium",
            "max_leverage": 3,
            "max_portfolio_risk": 0.02,
            "max_drawdown_circuit": 0.08,
            "assets_whitelist": ["BTC", "ETH"],
            "emergency_stop": False,
        }

    def get_active(self) -> dict:
        settings_rows = repo.select("smartbrain_settings", order_by="updated_at", desc=True, limit=1)
        if settings_rows:
            row = settings_rows[0]
            # Normalize nullable arrays from SQL clients.
            if row.get("assets_whitelist") is None:
                row["assets_whitelist"] = ["BTC", "ETH"]
            return row
        return self.defaults()


settings_service = SettingsService()

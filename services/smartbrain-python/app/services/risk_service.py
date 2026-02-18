from __future__ import annotations

from app.models.schemas import RiskCheckRequest, RiskCheckResponse
from app.services.performance_service import performance_service
from app.services.settings_service import settings_service


class RiskService:
    def _estimate_drawdown(self, mode: str) -> tuple[float, bool]:
        metrics_available = True
        if mode == "live":
            refresh_result = performance_service.refresh_live()
            metrics_available = bool(refresh_result.get("refreshed", False))
        return performance_service.latest_drawdown(mode), metrics_available

    def check(self, payload: RiskCheckRequest) -> RiskCheckResponse:
        settings = settings_service.get_active()
        drawdown_estimate, metrics_available = self._estimate_drawdown(payload.mode)
        reasons: list[str] = []

        assets_whitelist = settings.get("assets_whitelist", []) or []
        if assets_whitelist and payload.symbol not in assets_whitelist:
            reasons.append("symbol_not_whitelisted")

        if settings.get("emergency_stop", False):
            reasons.append("emergency_stop_active")

        if payload.mode == "live" and settings.get("paper_mode", True):
            reasons.append("live_mode_disabled_in_settings")

        if not settings.get("enabled", False):
            reasons.append("smartbrain_disabled")

        if payload.mode == "live" and not metrics_available:
            reasons.append("live_account_metrics_unavailable")

        max_leverage = int(settings.get("max_leverage", 3) or 3)
        if payload.requested_leverage > max_leverage:
            reasons.append("leverage_over_cap")

        max_dd = float(settings.get("max_drawdown_circuit", 0.08) or 0.08)
        if drawdown_estimate >= max_dd:
            reasons.append("drawdown_circuit_breaker")

        allowed = len(reasons) == 0
        max_position_pct = float(settings.get("max_portfolio_risk", 0.02) or 0.02)

        return RiskCheckResponse(
            allowed=allowed,
            max_leverage=max_leverage,
            max_position_pct=max_position_pct,
            drawdown_estimate=drawdown_estimate,
            reasons=reasons,
        )


risk_service = RiskService()

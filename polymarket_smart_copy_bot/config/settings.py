from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[1]
RiskMode = Literal["conservative", "aggressive"]
FillMode = Literal["conservative", "aggressive"]  # IMPROVED: status model


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / "config" / ".env", BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "polymarket-smart-copy-bot"
    app_env: str = Field(default="local", alias="APP_ENV")
    debug: bool = Field(default=False, alias="DEBUG")
    dry_run: bool = Field(default=True, alias="DRY_RUN")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    timezone: str = Field(default="UTC", alias="TIMEZONE")

    port: int = Field(default=8000, alias="PORT")
    database_url: str | None = Field(default=None, alias="DATABASE_URL")

    polymarket_host: str = Field(default="https://clob.polymarket.com", alias="POLYMARKET_HOST")
    polymarket_gamma_host: str = Field(
        default="https://gamma-api.polymarket.com", alias="POLYMARKET_GAMMA_HOST"
    )
    polymarket_data_api_host: str = Field(
        default="https://data-api.polymarket.com", alias="POLYMARKET_DATA_API_HOST"
    )
    polymarket_chain_id: int = Field(default=137, alias="POLYMARKET_CHAIN_ID")
    polymarket_private_key: str | None = Field(default=None, alias="POLYMARKET_PRIVATE_KEY")
    polymarket_proxy_address: str | None = Field(default=None, alias="POLYMARKET_PROXY_ADDRESS")
    polymarket_api_key: str | None = Field(default=None, alias="POLYMARKET_API_KEY")
    polymarket_api_secret: str | None = Field(default=None, alias="POLYMARKET_API_SECRET")
    polymarket_api_passphrase: str | None = Field(default=None, alias="POLYMARKET_API_PASSPHRASE")
    polymarket_signature_type: int | None = Field(default=None, alias="POLYMARKET_SIGNATURE_TYPE")
    polymarket_verify_ssl: bool = Field(default=True, alias="POLYMARKET_VERIFY_SSL")

    risk_mode: RiskMode = Field(default="aggressive", alias="RISK_MODE")

    # SAFETY: IOC + controlled slippage — fill mode settings
    fill_mode: FillMode = Field(default="conservative", alias="FILL_MODE")
    aggressive_fill_type: str = Field(default="IOC", alias="AGGRESSIVE_FILL_TYPE")            # IOC or FOK
    max_slippage_bps: float = Field(default=5.0, alias="MAX_SLIPPAGE_BPS")                   # +0.05% default
    max_allowed_slippage_bps: float = Field(default=15.0, alias="MAX_ALLOWED_SLIPPAGE_BPS")   # hard cap 0.15%
    aggressive_fill_ttl_seconds: int = Field(default=120, alias="AGGRESSIVE_FILL_TTL_SECONDS")  # 2 minutes

    # Legacy/conservative defaults
    price_min_cents: int = Field(default=20, alias="PRICE_MIN_CENTS")
    price_max_cents: int = Field(default=80, alias="PRICE_MAX_CENTS")
    max_risk_per_trade: float = Field(default=0.065, alias="MAX_RISK_PER_TRADE")
    max_daily_risk: float = Field(default=0.07, alias="MAX_DAILY_RISK")
    min_qualified_wallets: int = Field(default=5, alias="MIN_QUALIFIED_WALLETS")
    max_qualified_wallets: int = Field(default=12, alias="MAX_QUALIFIED_WALLETS")

    # Aggressive strategy knobs
    max_wallets_aggressive: int = Field(default=10, alias="MAX_WALLETS_AGGRESSIVE")
    min_wallets_aggressive: int = Field(default=3, alias="MIN_WALLETS_AGGRESSIVE")
    max_per_wallet_pct: float = Field(default=0.15, alias="MAX_PER_WALLET_PCT")
    max_per_position_pct: float = Field(default=0.10, alias="MAX_PER_POSITION_PCT")
    max_total_exposure_pct: float = Field(default=0.65, alias="MAX_TOTAL_EXPOSURE_PCT")
    kelly_multiplier: float = Field(default=2.0, alias="KELLY_MULTIPLIER")
    drawdown_stop_pct: float = Field(default=0.25, alias="DRAWDOWN_STOP_PCT")
    enable_short_term_markets: bool = Field(default=True, alias="ENABLE_SHORT_TERM_MARKETS")
    disable_price_filter: bool = Field(default=False, alias="DISABLE_PRICE_FILTER")
    auto_reinvest: bool = Field(default=True, alias="AUTO_REINVEST")
    high_conviction_multiplier: float = Field(default=1.5, alias="HIGH_CONVICTION_MULTIPLIER")
    # SAFETY: safe aggressive fill
    max_slippage_bps: float = Field(default=5.0, alias="MAX_SLIPPAGE_BPS")
    max_allowed_slippage_bps: float = Field(default=15.0, alias="MAX_ALLOWED_SLIPPAGE_BPS")
    aggressive_fill_ttl_seconds: int = Field(default=120, alias="AGGRESSIVE_FILL_TTL_SECONDS")

    wallet_score_refresh_hours: int = Field(default=4, alias="WALLET_SCORE_REFRESH_HOURS")
    discovery_autoadd_default: bool = Field(default=True, alias="DISCOVERY_AUTOADD")
    # === DISCOVERY FILTERS ===
    # Aggressive (default)
    discovery_min_trades_aggressive: int = Field(default=120, alias="DISCOVERY_MIN_TRADES_AGGRESSIVE")
    discovery_min_winrate_aggressive: float = Field(default=0.65, alias="DISCOVERY_MIN_WINRATE_AGGRESSIVE")
    discovery_min_profit_factor_aggressive: float = Field(
        default=1.55, alias="DISCOVERY_MIN_PROFIT_FACTOR_AGGRESSIVE"
    )
    discovery_min_avg_size_aggressive: float = Field(default=350.0, alias="DISCOVERY_MIN_AVG_SIZE_AGGRESSIVE")
    discovery_max_consec_losses_aggressive: int = Field(default=5, alias="DISCOVERY_MAX_CONSEC_LOSSES_AGGRESSIVE")
    discovery_min_wallet_age_aggressive: int = Field(default=7, alias="DISCOVERY_MIN_WALLET_AGE_AGGRESSIVE")

    # Conservative
    discovery_min_trades_cons: int = Field(default=160, alias="DISCOVERY_MIN_TRADES_CONS")
    discovery_min_winrate_cons: float = Field(default=0.69, alias="DISCOVERY_MIN_WINRATE_CONS")
    discovery_min_profit_factor_cons: float = Field(default=1.75, alias="DISCOVERY_MIN_PROFIT_FACTOR_CONS")
    discovery_min_avg_size_cons: float = Field(default=550.0, alias="DISCOVERY_MIN_AVG_SIZE_CONS")
    discovery_max_consec_losses_cons: int = Field(default=4, alias="DISCOVERY_MAX_CONSEC_LOSSES_CONS")
    discovery_min_wallet_age_cons: int = Field(default=45, alias="DISCOVERY_MIN_WALLET_AGE_CONS")

    # Recency filters kept explicit by mode
    discovery_max_days_since_last_trade_aggressive: int = Field(
        default=7, alias="DISCOVERY_MAX_DAYS_SINCE_LAST_TRADE_AGGRESSIVE"
    )
    discovery_max_days_since_last_trade_conservative: int = Field(
        default=5, alias="DISCOVERY_MAX_DAYS_SINCE_LAST_TRADE_CONSERVATIVE"
    )
    trade_monitor_interval_seconds: int = Field(default=60, alias="TRADE_MONITOR_INTERVAL_SECONDS")
    portfolio_refresh_seconds: int = Field(default=60, alias="PORTFOLIO_REFRESH_SECONDS")
    capital_recalc_interval_minutes: int = Field(default=60, alias="CAPITAL_RECALC_INTERVAL_MINUTES")
    stale_order_cleanup_interval_seconds: int = Field(
        default=180, alias="STALE_ORDER_CLEANUP_INTERVAL_SECONDS"
    )
    stale_order_ttl_minutes: int = Field(default=20, alias="STALE_ORDER_TTL_MINUTES")
    stale_order_cancel_batch: int = Field(default=50, alias="STALE_ORDER_CANCEL_BATCH")
    postcheck_market_position_hard_limit: bool = Field(default=True, alias="POSTCHECK_MARKET_POSITION_HARD_LIMIT")
    postcheck_market_cap_tolerance_usd: float = Field(default=0.25, alias="POSTCHECK_MARKET_CAP_TOLERANCE_USD")

    manual_confirmation_usd: float = Field(default=250.0, alias="MANUAL_CONFIRMATION_USD")
    default_starting_equity: float = Field(default=70.0, alias="DEFAULT_STARTING_EQUITY")

    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: int | None = Field(default=None, alias="TELEGRAM_CHAT_ID")
    dashboard_write_token: str | None = Field(default=None, alias="DASHBOARD_WRITE_TOKEN")
    dashboard_refresh_seconds: int = Field(default=10, alias="DASHBOARD_REFRESH_SECONDS")
    
    railway_public_domain: str | None = Field(default=None, alias="RAILWAY_PUBLIC_DOMAIN")

    wallets_config_path: str = Field(default="config/wallets.yaml", alias="WALLETS_CONFIG_PATH")

    @property
    def bot_public_url(self) -> str:
        if self.railway_public_domain:
            return f"https://{self.railway_public_domain}"
        return "http://localhost:8000"

    @property
    def resolved_wallets_config_path(self) -> Path:
        return (BASE_DIR / self.wallets_config_path).resolve()

    @property
    def local_sqlite_path(self) -> Path:
        return (BASE_DIR / "local.db").resolve()

    @property
    def conservative_max_per_wallet_pct(self) -> float:
        return 0.07

    @property
    def conservative_max_per_position_pct(self) -> float:
        return 0.04

    @property
    def conservative_max_total_exposure_pct(self) -> float:
        return 0.35

    @property
    def RISK_MODE(self) -> RiskMode:
        return self.risk_mode

    # Backward-compatible aliases used by older modules/env names.
    @property
    def discovery_max_consecutive_losses_aggressive(self) -> int:
        return self.discovery_max_consec_losses_aggressive

    @property
    def discovery_min_wallet_age_days_aggressive(self) -> int:
        return self.discovery_min_wallet_age_aggressive

    @property
    def discovery_min_trades_conservative(self) -> int:
        return self.discovery_min_trades_cons

    @property
    def discovery_min_winrate_conservative(self) -> float:
        return self.discovery_min_winrate_cons

    @property
    def discovery_min_profit_factor_conservative(self) -> float:
        return self.discovery_min_profit_factor_cons

    @property
    def discovery_min_avg_size_conservative(self) -> float:
        return self.discovery_min_avg_size_cons

    @property
    def discovery_max_consecutive_losses_conservative(self) -> int:
        return self.discovery_max_consec_losses_cons

    @property
    def discovery_min_wallet_age_days_conservative(self) -> int:
        return self.discovery_min_wallet_age_cons

    @property
    def async_database_url(self) -> str:
        raw = self.database_url
        if not raw:
            return f"sqlite+aiosqlite:///{self.local_sqlite_path}"
        if raw.startswith("postgres://"):
            raw = raw.replace("postgres://", "postgresql://", 1)
        if raw.startswith("postgresql://") and "+asyncpg" not in raw:
            raw = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
        if raw.startswith("postgresql+asyncpg://") and "sslmode=require" in raw:
            raw = raw.replace("sslmode=require", "ssl=require")
        if raw.startswith("sqlite:///") and "+aiosqlite" not in raw:
            raw = raw.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
        return raw

    @property
    def sync_database_url(self) -> str:
        raw = self.database_url
        if not raw:
            return f"sqlite:///{self.local_sqlite_path}"
        if raw.startswith("postgres://"):
            raw = raw.replace("postgres://", "postgresql://", 1)
        if raw.startswith("postgresql+asyncpg://"):
            return raw.replace("postgresql+asyncpg://", "postgresql://", 1)
        if raw.startswith("sqlite+aiosqlite:///"):
            return raw.replace("sqlite+aiosqlite:///", "sqlite:///", 1)
        return raw

    @property
    def is_railway(self) -> bool:
        return self.app_env.lower() in {"prod", "production"} or "railway" in self.app_env.lower()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

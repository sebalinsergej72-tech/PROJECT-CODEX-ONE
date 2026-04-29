from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
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
    polymarket_market_ws_url: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        alias="POLYMARKET_MARKET_WS_URL",
    )
    polymarket_user_ws_url: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com/ws/user",
        alias="POLYMARKET_USER_WS_URL",
    )
    polymarket_chain_id: int = Field(default=137, alias="POLYMARKET_CHAIN_ID")
    polymarket_private_key: str | None = Field(default=None, alias="POLYMARKET_PRIVATE_KEY")
    polymarket_proxy_address: str | None = Field(default=None, alias="POLYMARKET_PROXY_ADDRESS")
    polymarket_api_key: str | None = Field(default=None, alias="POLYMARKET_API_KEY")
    polymarket_api_secret: str | None = Field(default=None, alias="POLYMARKET_API_SECRET")
    polymarket_api_passphrase: str | None = Field(default=None, alias="POLYMARKET_API_PASSPHRASE")
    polymarket_signature_type: int | None = Field(default=None, alias="POLYMARKET_SIGNATURE_TYPE")
    polymarket_verify_ssl: bool = Field(default=True, alias="POLYMARKET_VERIFY_SSL")
    polygon_rpc_url: str = Field(default="https://polygon.drpc.org", alias="POLYGON_RPC_URL")
    polymarket_onchain_balance_enabled: bool = Field(default=True, alias="POLYMARKET_ONCHAIN_BALANCE_ENABLED")
    polymarket_market_ws_enabled: bool = Field(default=True, alias="POLYMARKET_MARKET_WS_ENABLED")
    polymarket_market_ws_cache_ttl_seconds: int = Field(
        default=15,
        alias="POLYMARKET_MARKET_WS_CACHE_TTL_SECONDS",
    )
    polymarket_market_ws_bootstrap_timeout_seconds: float = Field(
        default=0.35,
        alias="POLYMARKET_MARKET_WS_BOOTSTRAP_TIMEOUT_SECONDS",
    )
    polymarket_market_ws_ping_seconds: int = Field(
        default=15,
        alias="POLYMARKET_MARKET_WS_PING_SECONDS",
    )
    polymarket_market_ws_reconnect_seconds: float = Field(
        default=2.0,
        alias="POLYMARKET_MARKET_WS_RECONNECT_SECONDS",
    )
    execution_sidecar_enabled: bool = Field(default=False, alias="EXECUTION_SIDECAR_ENABLED")
    execution_sidecar_base_url: str = Field(
        default="http://127.0.0.1:8787",
        alias="EXECUTION_SIDECAR_BASE_URL",
    )
    execution_sidecar_timeout_seconds: float = Field(
        default=0.75,
        alias="EXECUTION_SIDECAR_TIMEOUT_SECONDS",
    )
    execution_sidecar_push_enabled: bool = Field(
        default=True,
        alias="EXECUTION_SIDECAR_PUSH_ENABLED",
    )
    execution_sidecar_push_host: str = Field(
        default="127.0.0.1",
        alias="EXECUTION_SIDECAR_PUSH_HOST",
    )
    execution_sidecar_push_port: int = Field(
        default=8788,
        alias="EXECUTION_SIDECAR_PUSH_PORT",
    )
    execution_sidecar_push_reconnect_seconds: float = Field(
        default=1.0,
        alias="EXECUTION_SIDECAR_PUSH_RECONNECT_SECONDS",
    )
    execution_sidecar_hot_market_registry_enabled: bool = Field(
        default=True,
        alias="EXECUTION_SIDECAR_HOT_MARKET_REGISTRY_ENABLED",
    )
    execution_sidecar_hot_market_ttl_seconds: int = Field(
        default=300,
        alias="EXECUTION_SIDECAR_HOT_MARKET_TTL_SECONDS",
    )
    execution_sidecar_hot_signal_ingest_enabled: bool = Field(
        default=True,
        alias="EXECUTION_SIDECAR_HOT_SIGNAL_INGEST_ENABLED",
    )
    execution_sidecar_market_data_enabled: bool = Field(
        default=True,
        alias="EXECUTION_SIDECAR_MARKET_DATA_ENABLED",
    )
    execution_sidecar_executable_snapshot_enabled: bool = Field(
        default=True,
        alias="EXECUTION_SIDECAR_EXECUTABLE_SNAPSHOT_ENABLED",
    )
    execution_sidecar_execution_plan_enabled: bool = Field(
        default=False,
        alias="EXECUTION_SIDECAR_EXECUTION_PLAN_ENABLED",
    )
    execution_sidecar_fill_reconcile_enabled: bool = Field(
        default=False,
        alias="EXECUTION_SIDECAR_FILL_RECONCILE_ENABLED",
    )
    execution_sidecar_shadow_mode_enabled: bool = Field(
        default=False,
        alias="EXECUTION_SIDECAR_SHADOW_MODE_ENABLED",
    )
    execution_sidecar_authenticated_reads_enabled: bool = Field(
        default=True,
        alias="EXECUTION_SIDECAR_AUTHENTICATED_READS_ENABLED",
    )

    risk_mode: RiskMode = Field(default="aggressive", alias="RISK_MODE")

    # SAFETY: IOC + controlled slippage — fill mode settings
    fill_mode: FillMode = Field(default="conservative", alias="FILL_MODE")
    aggressive_fill_type: str = Field(default="IOC", alias="AGGRESSIVE_FILL_TYPE")            # IOC or FOK
    max_slippage_bps: float = Field(default=150.0, alias="MAX_SLIPPAGE_BPS")                  # +1.50% default
    max_allowed_slippage_bps: float = Field(default=150.0, alias="MAX_ALLOWED_SLIPPAGE_BPS")  # hard cap 1.50%
    aggressive_fill_ttl_seconds: int = Field(default=120, alias="AGGRESSIVE_FILL_TTL_SECONDS")  # 2 minutes
    aggressive_reprice_delay_seconds: int = Field(
        default=25,
        alias="AGGRESSIVE_REPRICE_DELAY_SECONDS",
    )
    aggressive_reprice_total_ttl_seconds: int = Field(
        default=60,
        alias="AGGRESSIVE_REPRICE_TOTAL_TTL_SECONDS",
    )
    min_valid_price: float = Field(default=0.01, alias="MIN_VALID_PRICE")
    max_valid_price: float = Field(default=0.99, alias="MAX_VALID_PRICE")
    min_orderbook_liquidity_usd: float = Field(default=10.0, alias="MIN_ORDERBOOK_LIQUIDITY_USD")
    liquidity_buffer_multiplier: float = Field(default=1.25, alias="LIQUIDITY_BUFFER_MULTIPLIER")
    max_price_deviation_pct: float = Field(default=0.08, alias="MAX_PRICE_DEVIATION_PCT")
    min_absolute_price_deviation_cents: float = Field(
        default=2.0,
        alias="MIN_ABSOLUTE_PRICE_DEVIATION_CENTS",
    )
    price_moved_market_cooldown_minutes: int = Field(
        default=30,
        alias="PRICE_MOVED_MARKET_COOLDOWN_MINUTES",
    )
    no_orderbook_market_cooldown_minutes: int = Field(
        default=30,
        alias="NO_ORDERBOOK_MARKET_COOLDOWN_MINUTES",
    )
    low_liquidity_market_cooldown_minutes: int = Field(
        default=15,
        alias="LOW_LIQUIDITY_MARKET_COOLDOWN_MINUTES",
    )
    repeat_buy_cooldown_seconds: int = Field(
        default=120,
        alias="REPEAT_BUY_COOLDOWN_SECONDS",
    )
    wallet_burst_window_minutes: int = Field(
        default=30,
        alias="WALLET_BURST_WINDOW_MINUTES",
    )
    wallet_burst_max_buy_intents: int = Field(
        default=3,
        alias="WALLET_BURST_MAX_BUY_INTENTS",
    )
    wallet_failure_cooldown_hours: int = Field(
        default=2,
        alias="WALLET_FAILURE_COOLDOWN_HOURS",
    )
    wallet_failure_cooldown_lookback_trades: int = Field(
        default=10,
        alias="WALLET_FAILURE_COOLDOWN_LOOKBACK_TRADES",
    )
    wallet_failure_cooldown_failure_threshold: int = Field(
        default=7,
        alias="WALLET_FAILURE_COOLDOWN_FAILURE_THRESHOLD",
    )
    wallet_uncopyable_pause_enabled: bool = Field(
        default=True,
        alias="WALLET_UNCOPYABLE_PAUSE_ENABLED",
    )
    wallet_uncopyable_lookback_days: int = Field(
        default=7,
        alias="WALLET_UNCOPYABLE_LOOKBACK_DAYS",
    )
    wallet_uncopyable_min_buy_intents: int = Field(
        default=100,
        alias="WALLET_UNCOPYABLE_MIN_BUY_INTENTS",
    )
    wallet_uncopyable_max_fill_rate_pct: float = Field(
        default=0.5,
        alias="WALLET_UNCOPYABLE_MAX_FILL_RATE_PCT",
    )
    block_unknown_market_category: bool = Field(
        default=True,
        alias="BLOCK_UNKNOWN_MARKET_CATEGORY",
    )
    take_profit_exit_pct: float = Field(
        default=0.18,
        alias="TAKE_PROFIT_EXIT_PCT",
    )
    stop_loss_exit_pct: float = Field(
        default=0.12,
        alias="STOP_LOSS_EXIT_PCT",
    )
    min_exit_position_age_minutes: int = Field(
        default=10,
        alias="MIN_EXIT_POSITION_AGE_MINUTES",
    )
    sell_exit_retry_cooldown_minutes: int = Field(
        default=60,
        alias="SELL_EXIT_RETRY_COOLDOWN_MINUTES",
    )
    terminal_profit_exit_hold_price_cents: float = Field(
        default=99.0,
        alias="TERMINAL_PROFIT_EXIT_HOLD_PRICE_CENTS",
    )
    protect_against_zero_live_balance: bool = Field(
        default=True,
        alias="PROTECT_AGAINST_ZERO_LIVE_BALANCE",
    )
    suspicious_zero_balance_min_previous_usd: float = Field(
        default=1.0,
        alias="SUSPICIOUS_ZERO_BALANCE_MIN_PREVIOUS_USD",
    )

    # Legacy/conservative defaults
    price_min_cents: int = Field(default=20, alias="PRICE_MIN_CENTS")
    price_max_cents: int = Field(default=80, alias="PRICE_MAX_CENTS")
    max_risk_per_trade: float = Field(default=0.065, alias="MAX_RISK_PER_TRADE")
    max_daily_risk: float = Field(default=0.07, alias="MAX_DAILY_RISK")
    min_qualified_wallets: int = Field(default=5, alias="MIN_QUALIFIED_WALLETS")
    max_qualified_wallets: int = Field(default=12, alias="MAX_QUALIFIED_WALLETS")

    # Aggressive strategy knobs
    max_wallets_aggressive: int = Field(default=20, alias="MAX_WALLETS_AGGRESSIVE")
    min_wallets_aggressive: int = Field(default=3, alias="MIN_WALLETS_AGGRESSIVE")
    live_pool_min_non_sports_aggressive: int = Field(
        default=2,
        alias="LIVE_POOL_MIN_NON_SPORTS_AGGRESSIVE",
    )
    max_per_wallet_pct: float = Field(default=0.15, alias="MAX_PER_WALLET_PCT")
    max_per_position_pct: float = Field(default=0.10, alias="MAX_PER_POSITION_PCT")
    max_total_exposure_pct: float = Field(default=0.65, alias="MAX_TOTAL_EXPOSURE_PCT")
    kelly_multiplier: float = Field(default=2.0, alias="KELLY_MULTIPLIER")
    drawdown_stop_pct: float = Field(default=0.25, alias="DRAWDOWN_STOP_PCT")
    enable_short_term_markets: bool = Field(default=True, alias="ENABLE_SHORT_TERM_MARKETS")
    disable_price_filter: bool = Field(default=False, alias="DISABLE_PRICE_FILTER")
    auto_reinvest: bool = Field(default=True, alias="AUTO_REINVEST")
    high_conviction_multiplier: float = Field(default=1.5, alias="HIGH_CONVICTION_MULTIPLIER")
    max_wallet_multiplier: float = Field(default=2.0, alias="MAX_WALLET_MULTIPLIER")
    kelly_fraction_scale: float = Field(default=0.20, alias="KELLY_FRACTION")
    max_kelly_bet_pct: float = Field(default=0.15, alias="MAX_KELLY_BET_PCT")
    min_trade_size_usd: float = Field(default=3.0, alias="MIN_TRADE_SIZE_USD")
    ignore_available_cash_for_sizing: bool = Field(
        default=True,
        alias="IGNORE_AVAILABLE_CASH_FOR_SIZING",
    )
    enforce_min_trade_size: bool = Field(
        default=False,
        alias="ENFORCE_MIN_TRADE_SIZE",
    )
    enable_drawdown_guards: bool = Field(
        default=False,
        alias="ENABLE_DRAWDOWN_GUARDS",
    )
    # SAFETY: safe aggressive fill
    max_slippage_bps: float = Field(default=150.0, alias="MAX_SLIPPAGE_BPS")
    max_allowed_slippage_bps: float = Field(default=150.0, alias="MAX_ALLOWED_SLIPPAGE_BPS")
    aggressive_fill_ttl_seconds: int = Field(default=120, alias="AGGRESSIVE_FILL_TTL_SECONDS")

    wallet_score_refresh_hours: int = Field(default=4, alias="WALLET_SCORE_REFRESH_HOURS")
    discovery_autoadd_default: bool = Field(default=True, alias="DISCOVERY_AUTOADD")
    # === DISCOVERY FILTERS ===
    # Aggressive (default)
    discovery_min_trades_aggressive: int = Field(default=100, alias="DISCOVERY_MIN_TRADES_AGGRESSIVE")
    discovery_min_winrate_aggressive: float = Field(default=0.62, alias="DISCOVERY_MIN_WINRATE_AGGRESSIVE")
    discovery_min_profit_factor_aggressive: float = Field(
        default=1.30, alias="DISCOVERY_MIN_PROFIT_FACTOR_AGGRESSIVE"
    )
    discovery_min_avg_size_aggressive: float = Field(default=200.0, alias="DISCOVERY_MIN_AVG_SIZE_AGGRESSIVE")
    discovery_max_consec_losses_aggressive: int = Field(default=5, alias="DISCOVERY_MAX_CONSEC_LOSSES_AGGRESSIVE")
    discovery_min_wallet_age_aggressive: int = Field(default=30, alias="DISCOVERY_MIN_WALLET_AGE_AGGRESSIVE")

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
    reserve_wallet_pool_target: int = Field(default=5, alias="RESERVE_WALLET_POOL_TARGET")
    reserve_min_trades_aggressive: int = Field(default=90, alias="RESERVE_MIN_TRADES_AGGRESSIVE")
    reserve_min_winrate_aggressive: float = Field(default=0.62, alias="RESERVE_MIN_WINRATE_AGGRESSIVE")
    reserve_min_profit_factor_aggressive: float = Field(
        default=1.45,
        alias="RESERVE_MIN_PROFIT_FACTOR_AGGRESSIVE",
    )
    reserve_min_avg_size_aggressive: float = Field(default=250.0, alias="RESERVE_MIN_AVG_SIZE_AGGRESSIVE")
    reserve_min_trades_cons: int = Field(default=120, alias="RESERVE_MIN_TRADES_CONS")
    reserve_min_winrate_cons: float = Field(default=0.66, alias="RESERVE_MIN_WINRATE_CONS")
    reserve_min_profit_factor_cons: float = Field(default=1.6, alias="RESERVE_MIN_PROFIT_FACTOR_CONS")
    reserve_min_avg_size_cons: float = Field(default=400.0, alias="RESERVE_MIN_AVG_SIZE_CONS")
    live_pool_max_days_since_last_trade_aggressive: int = Field(
        default=4,
        alias="LIVE_POOL_MAX_DAYS_SINCE_LAST_TRADE_AGGRESSIVE",
    )
    live_pool_max_days_since_last_trade_conservative: int = Field(
        default=3,
        alias="LIVE_POOL_MAX_DAYS_SINCE_LAST_TRADE_CONSERVATIVE",
    )
    avg_size_score_cap: float = Field(default=50.0, alias="AVG_SIZE_CAP")
    min_attempts_for_tradability_penalty: int = Field(
        default=5,
        alias="MIN_ATTEMPTS_FOR_TRADABILITY_PENALTY",
    )
    max_consecutive_wallet_failures_per_cycle: int = Field(
        default=5,
        alias="MAX_CONSECUTIVE_FAILURES",
    )
    trade_monitor_interval_seconds: int = Field(default=15, alias="TRADE_MONITOR_INTERVAL_SECONDS")
    order_fill_monitor_interval_seconds: int = Field(default=15, alias="ORDER_FILL_MONITOR_INTERVAL_SECONDS")
    trade_reconcile_interval_seconds: int = Field(default=60, alias="TRADE_RECONCILE_INTERVAL_SECONDS")
    account_sync_ttl_seconds: int = Field(default=30, alias="ACCOUNT_SYNC_TTL_SECONDS")
    trade_monitor_signal_fetch_limit: int = Field(default=8, alias="TRADE_MONITOR_SIGNAL_FETCH_LIMIT")
    trade_monitor_hot_wallet_target: int = Field(default=6, alias="TRADE_MONITOR_HOT_WALLET_TARGET")
    trade_monitor_hot_trade_freshness_hours: int = Field(
        default=24,
        alias="TRADE_MONITOR_HOT_TRADE_FRESHNESS_HOURS",
    )
    trade_monitor_hot_signal_window_hours: int = Field(
        default=24,
        alias="TRADE_MONITOR_HOT_SIGNAL_WINDOW_HOURS",
    )
    trade_monitor_cold_wallet_batch_size: int = Field(
        default=4,
        alias="TRADE_MONITOR_COLD_WALLET_BATCH_SIZE",
    )
    burst_aggregation_enabled: bool = Field(default=True, alias="BURST_AGGREGATION_ENABLED")
    burst_aggregation_window_seconds: int = Field(default=20, alias="BURST_AGGREGATION_WINDOW_SECONDS")
    burst_aggregation_max_trades: int = Field(default=8, alias="BURST_AGGREGATION_MAX_TRADES")
    portfolio_refresh_seconds: int = Field(default=120, alias="PORTFOLIO_REFRESH_SECONDS")
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
    telegram_alert_chat_id: int | None = Field(default=None, alias="TELEGRAM_ALERT_CHAT_ID")
    telegram_allowed_user_ids: list[int] = Field(default_factory=list, alias="TELEGRAM_ALLOWED_USER_IDS")
    telegram_allowed_usernames: list[str] = Field(default_factory=list, alias="TELEGRAM_ALLOWED_USERNAMES")
    telegram_webapp_session_ttl_seconds: int = Field(
        default=900,
        alias="TELEGRAM_WEBAPP_SESSION_TTL_SECONDS",
    )
    dashboard_write_token: str | None = Field(default=None, alias="DASHBOARD_WRITE_TOKEN")
    dashboard_refresh_seconds: int = Field(default=10, alias="DASHBOARD_REFRESH_SECONDS")
    
    railway_public_domain: str | None = Field(default=None, alias="RAILWAY_PUBLIC_DOMAIN")

    wallets_config_path: str = Field(default="config/wallets.yaml", alias="WALLETS_CONFIG_PATH")

    @property
    def bot_public_url(self) -> str:
        if self.railway_public_domain:
            return f"https://{self.railway_public_domain}"
        return "http://localhost:8000"

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _parse_telegram_allowed_user_ids(cls, value: object) -> object:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [int(item) for item in value if str(item).strip()]
        if isinstance(value, str):
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        return value

    @field_validator("telegram_allowed_usernames", mode="before")
    @classmethod
    def _parse_telegram_allowed_usernames(cls, value: object) -> object:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            raw_items = value
        elif isinstance(value, str):
            raw_items = value.split(",")
        else:
            return value
        cleaned: list[str] = []
        for item in raw_items:
            username = str(item).strip().lstrip("@").lower()
            if username:
                cleaned.append(username)
        return cleaned

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

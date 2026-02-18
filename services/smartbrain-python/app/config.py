from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_port: int = 8000
    smartbrain_api_key: str = "replace_me"

    supabase_url: str = ""
    supabase_service_role_key: str = ""

    hyperliquid_private_key: str = ""
    hyperliquid_account_address: str = ""
    hyperliquid_use_testnet: bool = False

    openai_api_key: str = ""
    grok_api_key: str = ""

    coingecko_api_key: str = ""
    cryptopanic_api_key: str = ""
    news_api_key: str = ""
    lunarcrush_api_key: str = ""

    ingest_symbols: list[str] = Field(default_factory=lambda: ["BTC", "ETH"])
    ingest_timeframes: list[str] = Field(default_factory=lambda: ["1m", "5m", "15m", "1h", "4h", "1d"])

    model_store_dir: str = "model_store"
    online_tree_min_samples: int = 100
    online_tree_window: int = 5000
    online_tree_boost_rounds: int = 48

    rl_replay_min_samples: int = 120
    rl_replay_window: int = 5000
    rl_replay_steps: int = 2000


settings = Settings()

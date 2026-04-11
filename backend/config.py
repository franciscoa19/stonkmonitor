"""
Central config — reads from .env, validates, and exposes typed settings.
"""
from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache


class Settings(BaseSettings):
    # --- Unusual Whales ---
    unusual_whales_api_key: str = Field(..., env="UNUSUAL_WHALES_API_KEY")

    # --- Alpaca ---
    alpaca_api_key: str = Field(..., env="ALPACA_API_KEY")
    alpaca_secret_key: str = Field(..., env="ALPACA_SECRET_KEY")
    alpaca_paper: bool = Field(True, env="ALPACA_PAPER")
    alpaca_base_url: str = Field("https://paper-api.alpaca.markets", env="ALPACA_BASE_URL")
    alpaca_data_url: str = Field("https://data.alpaca.markets", env="ALPACA_DATA_URL")

    # --- SEC-API ---
    sec_api_key: str = Field("", env="SEC_API_KEY")

    # --- Discord ---
    discord_webhook_url: str = Field("", env="DISCORD_WEBHOOK_URL")

    # --- Pushover ---
    pushover_api_token: str = Field("", env="PUSHOVER_API_TOKEN")
    pushover_user_key: str = Field("", env="PUSHOVER_USER_KEY")

    # --- Telegram ---
    telegram_bot_token: str = Field("", env="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: int = Field(0, env="TELEGRAM_CHAT_ID")

    # --- Kalshi ---
    kalshi_email: str = Field("", env="KALSHI_EMAIL")
    kalshi_password: str = Field("", env="KALSHI_PASSWORD")
    kalshi_demo: bool = Field(True, env="KALSHI_DEMO")
    kalshi_scan_interval: int = Field(300, env="KALSHI_SCAN_INTERVAL")  # seconds
    kalshi_min_edge: float = Field(0.05, env="KALSHI_MIN_EDGE")
    kalshi_max_bet_usd: float = Field(500.0, env="KALSHI_MAX_BET_USD")
    kalshi_auto_execute: bool = Field(False, env="KALSHI_AUTO_EXECUTE")  # require confirmation first

    # --- Auto-Trade ---
    auto_trade_enabled: bool = Field(True, env="AUTO_TRADE_ENABLED")
    auto_trade_max_risk_pct: float = Field(0.02, env="AUTO_TRADE_MAX_RISK_PCT")
    auto_trade_max_risk_usd: float = Field(2500.0, env="AUTO_TRADE_MAX_RISK_USD")
    auto_trade_score_threshold: float = Field(8.5, env="AUTO_TRADE_SCORE_THRESHOLD")
    auto_trade_pattern_threshold: float = Field(9.0, env="AUTO_TRADE_PATTERN_THRESHOLD")
    auto_trade_min_dte: int = Field(2, env="AUTO_TRADE_MIN_DTE")
    auto_trade_max_dte: int = Field(21, env="AUTO_TRADE_MAX_DTE")

    # --- Backend ---
    backend_host: str = Field("0.0.0.0", env="BACKEND_HOST")
    backend_port: int = Field(8000, env="BACKEND_PORT")
    cors_origins: str = Field("http://localhost:3000", env="CORS_ORIGINS")

    # --- Signal Thresholds ---
    min_premium_alert: int = Field(50000, env="MIN_PREMIUM_ALERT")
    min_darkpool_size: int = Field(100000, env="MIN_DARKPOOL_SIZE")
    iv_rank_threshold: float = Field(80.0, env="IV_RANK_THRESHOLD")
    iv_rank_low_threshold: float = Field(20.0, env="IV_RANK_LOW_THRESHOLD")
    sweep_score_threshold: float = Field(7.0, env="SWEEP_SCORE_THRESHOLD")

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()

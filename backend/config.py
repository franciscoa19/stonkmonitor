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
    kalshi_key_id: str = Field("", env="KALSHI_KEY_ID")
    kalshi_private_key: str = Field("", env="KALSHI_PRIVATE_KEY")  # PEM string or .pem file path
    kalshi_demo: bool = Field(False, env="KALSHI_DEMO")
    kalshi_scan_interval: int = Field(300, env="KALSHI_SCAN_INTERVAL")  # seconds
    kalshi_min_edge: float = Field(0.05, env="KALSHI_MIN_EDGE")
    kalshi_max_bet_usd: float = Field(500.0, env="KALSHI_MAX_BET_USD")
    kalshi_auto_execute: bool = Field(False, env="KALSHI_AUTO_EXECUTE")  # require confirmation first

    # --- Dome API (cross-platform prediction market data) ---
    dome_api_key: str = Field("", env="DOME_API_KEY")
    dome_base_url: str = Field("https://api.domeapi.io", env="DOME_BASE_URL")
    # --- Polymarket CLOB (public, no auth) ---
    polymarket_clob_url: str = Field("https://clob.polymarket.com", env="POLYMARKET_CLOB_URL")
    # --- Cross-platform arb ---
    cross_arb_min_edge: float = Field(0.05, env="CROSS_ARB_MIN_EDGE")  # 5¢ minimum spread

    # --- Auto-Trade ---
    auto_trade_enabled: bool = Field(True, env="AUTO_TRADE_ENABLED")
    auto_trade_max_risk_pct: float = Field(0.02, env="AUTO_TRADE_MAX_RISK_PCT")
    auto_trade_max_risk_usd: float = Field(2500.0, env="AUTO_TRADE_MAX_RISK_USD")
    auto_trade_score_threshold: float = Field(8.5, env="AUTO_TRADE_SCORE_THRESHOLD")
    auto_trade_pattern_threshold: float = Field(9.0, env="AUTO_TRADE_PATTERN_THRESHOLD")
    auto_trade_min_dte: int = Field(3, env="AUTO_TRADE_MIN_DTE")    # was 2 — data shows 3-7d is sweet spot
    auto_trade_max_dte: int = Field(10, env="AUTO_TRADE_MAX_DTE")   # was 21 — 7-14d+ underperforms badly

    # --- Auto-Trade Quality Filters (data-driven, see performance analysis) ---
    # 1. Puts need an exceptional signal — default requires score ≥10 (near-impossible without a pattern)
    auto_trade_put_min_score: float = Field(9.5, env="AUTO_TRADE_PUT_MIN_SCORE")
    # 2. Market regime: skip bearish trades when SPY is ripping, skip bullish when crashing
    auto_trade_regime_spy_ticker: str = Field("SPY", env="AUTO_TRADE_REGIME_SPY_TICKER")
    auto_trade_regime_bear_skip_pct: float = Field(1.5, env="AUTO_TRADE_REGIME_BEAR_SKIP_PCT")   # skip puts if SPY day-chg > +1.5%
    auto_trade_regime_bull_skip_pct: float = Field(-2.0, env="AUTO_TRADE_REGIME_BULL_SKIP_PCT")  # skip calls if SPY day-chg < -2.0%
    auto_trade_regime_trend_days: int = Field(5, env="AUTO_TRADE_REGIME_TREND_DAYS")              # look-back for 5-day trend
    # 4. Options price cap — $5-25 options have 17-32% WR; cheap options outperform
    auto_trade_max_option_price: float = Field(8.0, env="AUTO_TRADE_MAX_OPTION_PRICE")
    # 5. Per-ticker loss cooldown — don't re-trade a ticker that lost recently
    auto_trade_ticker_cooldown_hours: int = Field(72, env="AUTO_TRADE_TICKER_COOLDOWN_HOURS")
    # 6. Daily P&L circuit breaker — halt auto-trading if day loss exceeds this
    auto_trade_daily_loss_limit: float = Field(-2000.0, env="AUTO_TRADE_DAILY_LOSS_LIMIT")

    # --- Position Monitor (TP/SL) ---
    pos_monitor_interval: int = Field(120, env="POS_MONITOR_INTERVAL")       # seconds between checks
    pos_tp_pct: float = Field(80.0, env="POS_TP_PCT")         # take profit tier 1 at +80%
    pos_tp_sell_pct: float = Field(0.5, env="POS_TP_SELL_PCT") # sell 50% at TP1
    pos_tp2_pct: float = Field(175.0, env="POS_TP2_PCT")      # take profit tier 2 at +175% (fallback if trail disabled)
    pos_tp2_sell_pct: float = Field(1.0, env="POS_TP2_SELL_PCT") # sell remaining 100% at TP2
    pos_trail_after_tp: bool = Field(True, env="POS_TRAIL_AFTER_TP")    # enable trailing stop after TP1
    pos_trail_pct: float = Field(20.0, env="POS_TRAIL_PCT")    # trail 20pp below high watermark after TP1
    pos_trim_pct: float = Field(-35.0, env="POS_TRIM_PCT")    # trim at -35%
    pos_trim_sell_pct: float = Field(0.5, env="POS_TRIM_SELL_PCT")  # sell 50% at trim
    pos_sl_pct: float = Field(-40.0, env="POS_SL_PCT")        # stop loss at -40%

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

    # --- Market Open/Close Noise Filter ---
    # Extra score required above base thresholds during noisy sub-phases.
    # Set to 0 to disable a particular bump.
    open_first5_bump: float = Field(2.0, env="OPEN_FIRST5_BUMP")   # 09:30–09:35 chaos
    open_bump: float = Field(1.5, env="OPEN_BUMP")                  # 09:35–10:00 settling
    close_bump: float = Field(0.5, env="CLOSE_BUMP")                # 15:45–16:00 MOC noise

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()

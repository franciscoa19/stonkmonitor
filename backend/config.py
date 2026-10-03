"""
Central config — reads from .env, validates, and exposes typed settings.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    # --- Unusual Whales ---
    # Optional now that the earnings IV/RV edge runs on yfinance + Alpaca only.
    # Set UW_ENABLED=false to cleanly disable the UW stream + IV-rank calls
    # (no error spam) and let the key be blank — e.g. after cancelling the sub.
    # All UW plumbing stays intact behind the flag so it can be re-enabled.
    unusual_whales_api_key: str = Field("")
    uw_enabled: bool = Field(True)

    # --- Alpaca ---
    alpaca_api_key: str = Field(...)
    alpaca_secret_key: str = Field(...)
    alpaca_paper: bool = Field(True)
    alpaca_base_url: str = Field("https://paper-api.alpaca.markets")
    alpaca_data_url: str = Field("https://data.alpaca.markets")

    # --- SEC-API ---
    sec_api_key: str = Field("")

    # --- Discord ---
    discord_webhook_url: str = Field("")
    # Master mute for Discord sends (plumbing kept; flip true to re-enable).
    discord_alerts_enabled: bool = Field(True)

    # --- Pushover ---
    pushover_api_token: str = Field("")
    pushover_user_key: str = Field("")

    # --- Telegram ---
    telegram_bot_token: str = Field("")
    telegram_chat_id: int = Field(0)
    # Master mute for all Telegram sends + polling (plumbing stays intact).
    # Set false for the autonomous paper setup (no cards, no getUpdates conflict).
    telegram_alerts_enabled: bool = Field(True)

    # --- Kalshi ---
    kalshi_key_id: str = Field("")
    kalshi_private_key: str = Field("")  # PEM string or .pem file path
    kalshi_demo: bool = Field(False)
    kalshi_scan_interval: int = Field(300)  # seconds
    kalshi_min_edge: float = Field(0.05)
    kalshi_max_bet_usd: float = Field(500.0)
    kalshi_auto_execute: bool = Field(False)  # require confirmation first

    # --- Dome API (cross-platform prediction market data) ---
    dome_api_key: str = Field("")
    dome_base_url: str = Field("https://api.domeapi.io")
    # --- Polymarket CLOB (public, no auth) ---
    polymarket_clob_url: str = Field("https://clob.polymarket.com")
    # --- Cross-platform arb ---
    cross_arb_min_edge: float = Field(0.05)  # 5¢ minimum spread

    # --- Auto-Trade ---
    auto_trade_enabled: bool = Field(True)
    # Fully autonomous: execute queued trades immediately instead of waiting for a
    # Telegram/UI confirm tap. Paper only — all risk caps/filters still apply.
    auto_trade_auto_execute: bool = Field(False)
    # Master switch for the flow/pattern/insider/congress trade path (sweeps,
    # triple_confluence, etc.). Set false to pause it — e.g. after it proved to
    # be a losing edge (2026-09: triple_confluence went 0/5). Leaves the engine
    # infra intact for other signal paths.
    auto_trade_flow_enabled: bool = Field(False)

    # --- IV/RV earnings execution (Phase 2 — defined-risk short premium) ---
    # The primary edge: sell an iron condor before an earnings print and collect
    # the IV crush. Level-3 defined risk (short strangle + protective wings).
    # OFF by default — arm only after paper-testing the mechanics end to end.
    iv_exec_enabled: bool = Field(False)
    # Only act on setups this strong. SELL_PREMIUM = all 3 gates; CONSIDER = ts
    # inversion + only one other gate. CONSIDER stays alert-only by default: it
    # is useful for plumbing and research, but is not evidence of an IV/RV edge.
    iv_exec_allow_consider: bool = Field(False)
    iv_exec_entry_days_before: int = Field(2)   # enter when earnings ≤ N days out
    # 10% of equity max loss per condor (owner's call, 2026-09-22). Defined risk,
    # so the loss really is capped — but note iv_exec_max_positions multiplies it:
    # 10% x 5 concurrent = 50% of the account at risk at once, and earnings
    # condors are NOT independent (a macro gap can hit several together).
    iv_exec_risk_pct: float = Field(0.10)
    # Absolute ceiling. Must stay above equity*risk_pct or it silently binds and
    # the configured percentage quietly does nothing.
    iv_exec_max_risk_usd: float = Field(6000.0)
    iv_exec_wing_width_pct: float = Field(0.03)    # protective wing = 3% of underlying beyond short
    iv_exec_short_move_mult: float = Field(1.0)   # short strikes at 1.0× the implied move
    # 3, not 5: at 10% risk per condor this caps simultaneous exposure at 30% of
    # the account rather than 50%. Earnings condors are not independent — a macro
    # gap can push several toward max loss at once — so the concurrency limit is
    # the real portfolio control, not the per-trade cap.
    iv_exec_max_positions: int = Field(3)
    iv_exec_max_per_day: int = Field(4)               # new condors per day
    iv_exec_tp_pct: float = Field(0.5)                     # close at 50% of max credit captured
    # Expiry day: after this ET hour, close on whatever the book offers rather
    # than deferring for a better one. On an early-close session the force time
    # is moved earlier so it remains at least an hour before the real close.
    # Assignment on a short leg is worse than a poor fill, so this overrides the
    # normal RTH/two-sided checks.
    iv_exec_force_close_hour: int = Field(15)
    # On expiry day, hold instead of taking the profit target when spot is at
    # least this far inside BOTH short strikes: all four legs then expire
    # worthless for the full credit, where closing forfeits the remainder to
    # remove a risk that has largely already passed. Below this buffer it is
    # pin risk, and closing is correct.
    iv_exec_expiry_hold_buffer_pct: float = Field(0.03)
    iv_exec_min_credit: float = Field(0.10)           # skip if net credit < $0.10 (not worth it)
    iv_exec_min_dte: int = Field(1)                       # front expiry must be ≥1 DTE after earnings
    iv_exec_max_dte: int = Field(10)                      # …and ≤10 DTE (front-month only)
    # Log several hypothetical structures per earnings event (condor 0.7/1.0/1.3×,
    # iron fly, straddle) and resolve vs realized to compare expectancy. Pure
    # measurement, zero capital at risk — on by default during the paper test.
    iv_variants_log_enabled: bool = Field(True)
    # A setup only counts as a real earnings sell-premium candidate when a known
    # print is within this many days (the term-structure-inversion thesis only
    # holds near earnings). Filters far-dated / cheap-IV / ETF noise from the
    # signal, the eval log, and execution. Execution still needs ≤ entry_days_before.
    # Re-price a logged event once it is this close to the print. Implied move
    # inflates as earnings approach: COST logged 7 days out captured 1.22%, which
    # is the quiet front-month IV, not the earnings premium — and that made a
    # trade we WON look like the market underpriced the move. Re-pricing inside
    # the execution window measures what we would actually have sold.
    iv_variants_reprice_within_days: int = Field(2)
    iv_setup_max_days_to_earnings: int = Field(7)
    # Execution realism for the measurement layer (VALIDATION_SPEC §3): short
    # premium dies on fills, not on signal quality. Price variants at a fill
    # WORSE than mid — shorts at bid, longs at ask — and charge round-trip
    # commission. Never default these to zero; mid-priced results flatter.
    iv_conservative_fills: bool = Field(True)
    iv_fee_per_contract: float = Field(0.65)
    # Also log structures for near-earnings names that FAIL the gates, so the
    # gates can be tested against "sell everything indiscriminately" (spec §4
    # Baseline 2). Measurement only — ungated rows are never executed.
    iv_log_ungated_baseline: bool = Field(True)
    # Measurement-only universe. The tradeable watchlist is 78 names (~312 prints
    # a year, of which only ~50-90 pass the gates), which puts the gated arm of
    # the comparison years from its 100-event rail. The logger risks no capital,
    # so it can price far more prints than we would ever trade. These names are
    # NEVER executed — they exist purely to reach a usable sample this season.
    iv_measure_universe_enabled: bool = Field(True)
    # Market cap is only a coarse pre-filter — it does not imply *options*
    # liquidity. A live check of $2B+ reporters found none could be priced: most
    # list monthlies only (no expiry in the band after the print) or have no bid
    # on the wings. $10B+ plus the front-expiry pre-check is where yield starts.
    iv_measure_min_market_cap: float = Field(10e9)
    iv_measure_max_per_cycle: int = Field(25)        # bounded scan load
    iv_measure_interval_hours: int = Field(6)

    # Liveness rail. The equity loop writes hourly; if its last write is older
    # than this the daily report says so loudly instead of rendering a normal
    # page over a dead backend.
    heartbeat_stale_minutes: int = Field(180)

    # --- Risk throttle (anti-martingale + circuit breaker) ---
    # Percent-of-equity sizing already shrinks the next bet after a loss, but
    # only between sequential trades, only in integer qty steps, and not at all
    # across concurrent positions sized off the same equity. This throttle cuts
    # size deliberately on losses and ratchets back on wins, then halts entirely
    # on a losing streak — the case that matters is a regime change where several
    # earnings trades fail together, which is exactly when sizing should stop.
    iv_risk_throttle_enabled: bool = Field(True)
    iv_risk_loss_factor: float = Field(0.5)   # halve after a loss
    iv_risk_win_factor: float = Field(2.0)     # double back after a win (capped at 1.0)
    iv_risk_floor: float = Field(0.25)              # never size below 25% of normal
    # Halting LATCHES: it requires a human to clear. An auto-resuming breaker is
    # not a breaker — it just re-enters the same regime that tripped it.
    iv_risk_halt_streak: int = Field(3)

    # --- Daily report scheduler ---
    report_enabled: bool = Field(True)
    report_hour_et: int = Field(8)   # weekday hour (ET) for the daily check-in
    # Commit + push the daily history exports to GitHub (durable backup + the
    # bridge a cloud delivery routine reads from). Off by default.
    report_git_push: bool = Field(False)
    auto_trade_max_risk_pct: float = Field(0.02)      # 2% of equity per options trade
    auto_trade_max_risk_usd: float = Field(50000.0)   # very high — % is the real cap
    auto_trade_score_threshold: float = Field(9.0)    # raised from 8.5
    auto_trade_pattern_threshold: float = Field(9.5) # raised from 9.0
    auto_trade_min_dte: int = Field(3)    # was 2 — data shows 3-7d is sweet spot
    auto_trade_max_dte: int = Field(10)   # was 21 — 7-14d+ underperforms badly

    # --- Auto-Trade Volume Controls (prevent over-trading) ---
    auto_trade_max_trades_per_day: int = Field(3)     # max confirmed trades per day
    auto_trade_max_open_positions: int = Field(4)     # max concurrent Alpaca positions
    auto_trade_max_pending: int = Field(3)                   # max unactioned Telegram alerts at once
    auto_trade_burst_limit: int = Field(4)                  # max alerts per burst window
    auto_trade_burst_window: int = Field(600)              # burst window in seconds (10 min)

    # --- Intraday Volatility Gate ---
    # When SPY moves more than this % intraday, raise the score bar to filter noise.
    # Reuses cached regime data — zero extra API calls.
    intraday_vol_threshold: float = Field(1.5)  # SPY ±1.5% today triggers gate
    intraday_vol_bump: float = Field(1.5)            # need score ≥ threshold + 1.5 during vol

    # --- Long-Term Equity Trades (insider cluster / congress + sweep patterns) ---
    equity_long_risk_pct: float = Field(0.05)   # 5% of equity for conviction stock holds
    equity_long_target_pct: float = Field(30.0)  # TP at +30%
    equity_long_stop_pct: float = Field(10.0)      # SL at -10%

    # --- Auto-Trade Quality Filters (data-driven, see performance analysis) ---
    # 1. Puts need an exceptional signal — default requires score ≥10 (near-impossible without a pattern)
    auto_trade_put_min_score: float = Field(9.5)
    # 2. Market regime: skip bearish trades when SPY is ripping, skip bullish when crashing
    auto_trade_regime_spy_ticker: str = Field("SPY")
    auto_trade_regime_bear_skip_pct: float = Field(1.5)   # skip puts if SPY day-chg > +1.5%
    auto_trade_regime_bull_skip_pct: float = Field(-2.0)  # skip calls if SPY day-chg < -2.0%
    auto_trade_regime_trend_days: int = Field(5)              # look-back for 5-day trend
    # 4. Options price cap — $5-25 options have 17-32% WR; cheap options outperform
    auto_trade_max_option_price: float = Field(8.0)
    # 4a. Min option price — sub-$1 contracts need 5x+ moves to hit TP, mostly junk
    auto_trade_min_option_price: float = Field(1.0)
    # 4b. Moneyness cap — reject options more than this % OTM (deep OTM has ~0% win rate)
    auto_trade_max_otm_pct: float = Field(0.20)   # 20% OTM hard cap
    # 5. Per-ticker loss cooldown — don't re-trade a ticker that lost recently
    auto_trade_ticker_cooldown_hours: int = Field(72)
    # 6. Daily P&L circuit breaker — halt if day loss exceeds X% of account equity
    auto_trade_daily_loss_pct: float = Field(-0.05)    # -5% of equity
    auto_trade_daily_loss_limit: float = Field(-2000.0)  # fallback absolute cap

    # --- Position Monitor (TP/SL) ---
    pos_monitor_interval: int = Field(120)       # seconds between checks
    pos_tp_pct: float = Field(80.0)         # take profit tier 1 at +80%
    pos_tp_sell_pct: float = Field(0.5) # sell 50% at TP1
    pos_tp2_pct: float = Field(175.0)      # take profit tier 2 at +175% (fallback if trail disabled)
    pos_tp2_sell_pct: float = Field(1.0) # sell remaining 100% at TP2
    pos_trail_after_tp: bool = Field(True)    # enable trailing stop after TP1
    pos_trail_pct: float = Field(20.0)    # trail 20pp below high watermark after TP1
    pos_trim_pct: float = Field(-35.0)    # trim at -35%
    pos_trim_sell_pct: float = Field(0.5)  # sell 50% at trim
    pos_sl_pct: float = Field(-40.0)        # stop loss at -40%

    # --- Backend ---
    backend_host: str = Field("127.0.0.1")
    backend_port: int = Field(8000)
    cors_origins: str = Field("http://localhost:3000,http://127.0.0.1:3000")

    # --- Signal Thresholds ---
    min_premium_alert: int = Field(50000)
    min_darkpool_size: int = Field(100000)
    iv_rank_threshold: float = Field(80.0)
    iv_rank_low_threshold: float = Field(20.0)
    sweep_score_threshold: float = Field(8.0)  # raised from 7.0 — too noisy

    # --- Signal Freshness (drop stale filings) ---
    # Congress PTRs / insider Form 4s are gated on FILING date (when the info
    # became public), not transaction date. A filing older than this is dropped
    # so a post-downtime restart doesn't flood the feed with the whole "recent"
    # window as if it were live. Set 0 to disable the age gate.
    congress_max_age_days: int = Field(10)
    insider_max_age_days: int = Field(5)

    # Auto-add a ticker to the IV/earnings watchlist when options flow appears on
    # it AND its market cap ≥ this many dollars (0 = off). Big + liquid coverage.
    watchlist_auto_add_min_mktcap: float = Field(0)

    # --- Market Open/Close Noise Filter ---
    # Extra score required above base thresholds during noisy sub-phases.
    # Set to 0 to disable a particular bump.
    open_first5_bump: float = Field(2.0)   # 09:30–09:35 chaos
    open_bump: float = Field(1.5)                  # 09:35–10:00 settling
    close_bump: float = Field(0.5)                # 15:45–16:00 MOC noise

@lru_cache()
def get_settings() -> Settings:
    return Settings()

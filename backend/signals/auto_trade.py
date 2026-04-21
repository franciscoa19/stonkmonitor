"""
AutoTradeEngine — evaluates high-conviction signals and pattern hits,
builds specific Alpaca-ready trade suggestions (options or equity),
sizes positions based on portfolio equity, and queues them for
1-tap Telegram execution with a 5-minute expiry window.

Trigger logic:
  • Signal score >= AUTO_TRADE_SCORE_THRESHOLD  (default 9.0)
    → options sweep/golden_sweep → options trade
    → insider_buy / congress_trade bullish → short-term equity trade

  • Pattern score >= AUTO_TRADE_PATTERN_THRESHOLD (default 9.5)
    AND pattern in AUTO_TRADE_PATTERNS set
    → options trade if options evidence exists, else equity

  • Patterns insider_cluster_buy / congress_plus_sweep
    → long-term equity trade (5% sizing, +30%/-10% bracket)

Quality filters (all configurable via .env):
  1. Puts require AUTO_TRADE_PUT_MIN_SCORE (default 9.5) — data showed 4% win rate on puts
  2. Market regime: skip bearish if SPY day +1.5%+, skip bullish if SPY day -2.0%+ crash
  3. DTE window: MIN_DTE=3, MAX_DTE=10 — 3-7d is the only profitable bucket historically
  4. Options price cap: skip if ask > AUTO_TRADE_MAX_OPTION_PRICE ($8) — $5-25 entries lose badly
  4b. Moneyness: skip if option is >20% OTM vs underlying price
  5. Ticker cooldown: skip if same ticker lost within AUTO_TRADE_TICKER_COOLDOWN_HOURS (72h)
  6. Circuit breaker: halt if day's realized P&L < -5% of account equity
  7. Max trades/day: halt if confirmed trade count >= AUTO_TRADE_MAX_TRADES_PER_DAY (3)
  8. Max open positions: halt if open positions >= AUTO_TRADE_MAX_OPEN_POSITIONS (4)

Position sizing:
  • options / short equity: equity * 2% (max_risk_pct)
  • long-term equity holds:  equity * 5% (equity_long_risk_pct)
  • no hard dollar cap — % governs everything
"""
import asyncio
import aiohttp
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Patterns that qualify for auto-trade queue (options or short equity)
AUTO_TRADE_PATTERNS = {
    "triple_confluence",
    "insider_buy_plus_sweep",
    "sweep_plus_darkpool",
    "golden_sweep_cluster",
    "congress_plus_sweep",
}

# Patterns that trigger long-term equity holds (5% sizing, +30%/-10%)
EQUITY_LONG_PATTERNS = {
    "insider_cluster_buy",
    "congress_plus_sweep",    # also in AUTO_TRADE_PATTERNS — long equity preferred
}

ZERO_DTE_MIN_SCORE = 9.5   # allow 0-1 DTE only if this confident


@dataclass
class TradeSuggestion:
    id: int
    ticker: str
    trade_type: str           # "option" | "equity"
    symbol: str               # OCC symbol or equity ticker
    side: str                 # "bullish" | "bearish"
    option_type: Optional[str]
    strike: Optional[float]
    expiry: Optional[str]
    dte: Optional[int]
    qty: int
    limit_price: float
    risk_amount: float
    stop_pct: float
    target_pct: float
    score: float
    rationale: str
    expires_at: Optional[datetime] = None
    telegram_msg_id: Optional[int] = None


class AutoTradeEngine:
    def __init__(self, settings):
        self.settings = settings
        self._session: Optional[aiohttp.ClientSession] = None
        self._telegram = None
        self._db = None
        self._trader = None
        self._pending: dict[int, TradeSuggestion] = {}

        # ── Filter state ───────────────────────────────────────────────────
        # Regime cache: (spy_change_pct, spy_trend_pct, timestamp)
        self._regime_cache: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._regime_ttl: float = 300.0  # refresh every 5 min

        # Circuit breaker: tracks today's realized P&L (reset at midnight ET)
        self._daily_pnl: float = 0.0
        self._daily_pnl_date: str = ""  # "YYYY-MM-DD" of last reset

        # Ticker cooldown: ticker → timestamp of last confirmed loss
        self._ticker_loss_ts: dict[str, float] = {}

        # Cached account equity — updated every evaluate_signal() call
        # Used for % based circuit breaker and position sizing
        self._cached_equity: float = 100_000.0

        # ── Alert rate controls ────────────────────────────────────────────
        # Timestamps of every Telegram trade alert sent (pruned to rolling window)
        self._alert_timestamps: list[float] = []

    def set_dependencies(self, telegram, db, trader):
        self._telegram = telegram
        self._db = db
        self._trader = trader

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _session_get(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ── Quality filters ─────────────────────────────────────────────────────

    def record_loss(self, ticker: str, pnl: float):
        """Called by position monitor when a losing exit fires. Updates cooldown + circuit breaker."""
        if pnl < 0:
            self._ticker_loss_ts[ticker.upper()] = time.time()
            self._refresh_daily_pnl_date()
            self._daily_pnl += pnl
            logger.info(
                f"AutoTrade filters: loss recorded {ticker} ${pnl:+,.0f} | "
                f"daily_pnl=${self._daily_pnl:+,.0f} | "
                f"circuit_breaker={'OPEN' if self._circuit_breaker_active() else 'closed'}"
            )

    def record_win(self, ticker: str, pnl: float):
        """Called by position monitor when a winning exit fires. Updates circuit breaker only."""
        if pnl > 0:
            self._refresh_daily_pnl_date()
            self._daily_pnl += pnl

    def _refresh_daily_pnl_date(self):
        """Reset daily P&L counter at midnight ET."""
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        if today != self._daily_pnl_date:
            self._daily_pnl = 0.0
            self._daily_pnl_date = today

    def _circuit_breaker_active(self) -> bool:
        """True if today's realized losses exceed the daily limit.

        Uses % of equity when equity is cached (preferred), falls back
        to the absolute dollar limit as a safety net.
        """
        self._refresh_daily_pnl_date()
        # % based: -5% of account equity by default
        if self._cached_equity > 0:
            loss_pct = self._daily_pnl / self._cached_equity  # e.g. -0.04
            pct_limit = self.settings.auto_trade_daily_loss_pct  # e.g. -0.05
            if loss_pct <= pct_limit:
                return True
        # Absolute dollar fallback
        dollar_limit = self.settings.auto_trade_daily_loss_limit  # e.g. -2000
        return self._daily_pnl <= dollar_limit

    def _ticker_in_cooldown(self, ticker: str) -> bool:
        """True if ticker had a confirmed loss within the cooldown window."""
        ts = self._ticker_loss_ts.get(ticker.upper())
        if ts is None:
            return False
        hours = self.settings.auto_trade_ticker_cooldown_hours
        return (time.time() - ts) < hours * 3600

    async def _get_regime(self) -> tuple[float, float]:
        """Return (today_change_pct, trend_change_pct) for SPY.
        Cached for 5 minutes. today = (last/prev_close - 1)*100.
        trend = (last/close_N_days_ago - 1)*100.
        Returns (0, 0) on any fetch failure so we never block a trade due to data error.
        """
        now = time.time()
        day_chg, trend_chg, cached_at = self._regime_cache
        if now - cached_at < self._regime_ttl:
            return day_chg, trend_chg

        try:
            spy = self.settings.auto_trade_regime_spy_ticker
            trend_days = self.settings.auto_trade_regime_trend_days + 1  # +1 for today
            session = await self._session_get()
            headers = {
                "APCA-API-KEY-ID":     self.settings.alpaca_api_key,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            }
            # Fetch daily bars for SPY (need trend_days + a buffer for weekends)
            start = (datetime.utcnow() - timedelta(days=trend_days + 5)).strftime("%Y-%m-%d")
            url = f"https://data.alpaca.markets/v2/stocks/{spy}/bars"
            async with session.get(
                url, headers=headers,
                params={"timeframe": "1Day", "start": start, "limit": trend_days + 5, "feed": "sip"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"Regime fetch HTTP {resp.status}")
                    return 0.0, 0.0
                data = await resp.json()
                bars = data.get("bars", [])
                if len(bars) < 2:
                    return 0.0, 0.0

                last_close = float(bars[-1]["c"])
                prev_close = float(bars[-2]["c"])
                old_close  = float(bars[0]["c"])

                day_chg   = (last_close / prev_close - 1) * 100
                trend_chg = (last_close / old_close - 1) * 100

                self._regime_cache = (day_chg, trend_chg, now)
                logger.debug(
                    f"Regime: SPY day={day_chg:+.2f}% trend({trend_days}d)={trend_chg:+.2f}%"
                )
                return day_chg, trend_chg
        except Exception as e:
            logger.debug(f"Regime fetch error (non-blocking): {e}")
            return 0.0, 0.0

    async def _regime_allows(self, side: str) -> tuple[bool, str]:
        """Returns (allowed, reason). side = 'bullish' or 'bearish'."""
        day_chg, trend_chg = await self._get_regime()

        bear_skip = self.settings.auto_trade_regime_bear_skip_pct   # default +1.5
        bull_skip = self.settings.auto_trade_regime_bull_skip_pct   # default -2.0

        if side == "bearish":
            if day_chg >= bear_skip:
                return False, f"Regime block: SPY +{day_chg:.1f}% today — no bearish trades in a rip"
            if trend_chg >= bear_skip * 2:
                return False, f"Regime block: SPY +{trend_chg:.1f}% over {self.settings.auto_trade_regime_trend_days}d — bull trend"
        elif side == "bullish":
            if day_chg <= bull_skip:
                return False, f"Regime block: SPY {day_chg:.1f}% today — no bullish trades in a crash"

        return True, ""

    def effective_vol_bump(self) -> float:
        """Return the extra score points required due to intraday volatility.
        Uses the cached SPY day-change — 0.0 if cache is cold or market is calm.
        Called by handle_signal() in main.py to gate Discord/Pushover too.
        """
        day_chg = self._regime_cache[0]
        if abs(day_chg) >= self.settings.intraday_vol_threshold:
            return self.settings.intraday_vol_bump
        return 0.0

    async def _max_trades_today_check(self) -> tuple[bool, str]:
        """Returns (ok, reason). Blocks if confirmed trades today >= daily cap."""
        try:
            limit = self.settings.auto_trade_max_trades_per_day
            from zoneinfo import ZoneInfo
            today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
            # Count rows confirmed today in pending_trades
            count = await self._db.count_confirmed_today(today)
            if count >= limit:
                return False, f"Daily trade cap: {count}/{limit} trades already confirmed today"
        except Exception as e:
            logger.debug(f"Max trades check error (non-blocking): {e}")
        return True, ""

    async def _max_positions_check(self) -> tuple[bool, str]:
        """Returns (ok, reason). Blocks if open Alpaca positions >= max."""
        try:
            limit = self.settings.auto_trade_max_open_positions
            positions = self._trader.get_positions()
            open_count = len([p for p in positions if p.get("qty", 0) > 0])
            if open_count >= limit:
                return False, f"Position cap: {open_count}/{limit} positions already open"
        except Exception as e:
            logger.debug(f"Max positions check error (non-blocking): {e}")
        return True, ""

    # ── Symbol helpers ───────────────────────────────────────────────────────

    def _occ_symbol(self, ticker: str, expiry: str, opt_type: str, strike: float) -> str:
        """
        Build OCC option symbol.
        Example: SNDK + 2026-04-24 + call + 840 → SNDK260424C00840000
        """
        try:
            clean = expiry.replace("-", "")   # "20260424"
            date_part = clean[2:]             # "260424"
            cp = "C" if "call" in opt_type.lower() else "P"
            strike_int = int(round(strike * 1000))
            return f"{ticker.upper()}{date_part}{cp}{strike_int:08d}"
        except Exception as e:
            logger.warning(f"OCC symbol error: {e}")
            return ""

    def _calc_dte(self, expiry: str) -> Optional[int]:
        try:
            exp = datetime.strptime(expiry, "%Y-%m-%d")
            return (exp - datetime.utcnow()).days
        except Exception:
            return None

    # ── Market data (Alpaca) ─────────────────────────────────────────────────

    async def _get_option_quote(self, symbol: str) -> dict:
        """Get option snapshot from Alpaca data API."""
        try:
            session = await self._session_get()
            headers = {
                "APCA-API-KEY-ID":     self.settings.alpaca_api_key,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            }
            url = "https://data.alpaca.markets/v1beta1/options/snapshots"
            async with session.get(
                url, headers=headers,
                params={"symbols": symbol, "feed": "indicative"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    snap = (data.get("snapshots") or {}).get(symbol, {})
                    quote = snap.get("latestQuote") or {}
                    trade = snap.get("latestTrade") or {}
                    return {
                        "ask":  float(quote.get("ap") or 0),
                        "bid":  float(quote.get("bp") or 0),
                        "last": float(trade.get("p")  or 0),
                        "iv":   float(snap.get("impliedVolatility") or 0),
                    }
                logger.warning(f"Option quote HTTP {resp.status} for {symbol}")
        except asyncio.TimeoutError:
            logger.warning(f"Option quote timeout for {symbol}")
        except Exception as e:
            logger.warning(f"Option quote error for {symbol}: {e}")
        return {}

    async def _get_equity_price(self, ticker: str) -> float:
        """Get mid-price from Alpaca IEX feed."""
        try:
            session = await self._session_get()
            headers = {
                "APCA-API-KEY-ID":     self.settings.alpaca_api_key,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            }
            url = f"https://data.alpaca.markets/v2/stocks/{ticker}/quotes/latest"
            async with session.get(
                url, headers=headers,
                params={"feed": "iex"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    q   = data.get("quote") or {}
                    ask = float(q.get("ap") or 0)
                    bid = float(q.get("bp") or 0)
                    if ask and bid:
                        return round((ask + bid) / 2, 2)
                    return ask or bid
        except Exception as e:
            logger.warning(f"Equity quote error for {ticker}: {e}")
        return 0.0

    # ── Position sizing ──────────────────────────────────────────────────────

    def _size_options(self, equity: float, limit_price: float) -> tuple[int, float]:
        """Returns (contracts, risk_amount)."""
        max_risk = min(
            equity * self.settings.auto_trade_max_risk_pct,
            self.settings.auto_trade_max_risk_usd,
        )
        cost_per = limit_price * 100  # 1 contract = 100 shares
        if cost_per <= 0:
            return 0, 0.0
        qty = max(1, int(max_risk / cost_per))
        # If even 1 contract exceeds 1.5x max_risk, skip
        if cost_per > max_risk * 1.5:
            logger.debug(f"1 contract @ ${limit_price:.2f} = ${cost_per:.0f} > max ${max_risk:.0f}, skipping")
            return 0, 0.0
        risk = qty * cost_per
        return qty, round(risk, 2)

    def _size_equity(self, equity: float, price: float,
                     risk_pct: Optional[float] = None) -> tuple[int, float]:
        """Returns (shares, risk_amount).
        risk_pct: override default auto_trade_max_risk_pct (used for long-term trades).
        """
        pct = risk_pct if risk_pct is not None else self.settings.auto_trade_max_risk_pct
        max_risk = min(
            equity * pct,
            self.settings.auto_trade_max_risk_usd,  # very high cap — % dominates
        )
        if price <= 0:
            return 0, 0.0
        qty = max(1, int(max_risk / price))
        return qty, round(qty * price, 2)

    # ── Evaluation entry points ──────────────────────────────────────────────

    async def _pre_flight(self, ticker: str, side: str, score: float = 0.0,
                          trade_type: str = "option") -> tuple[bool, str]:
        """Run all quality filters before queuing any trade.
        Returns (ok, reason_if_blocked).
        """
        now = time.time()

        # 6. Circuit breaker (% based)
        if self._circuit_breaker_active():
            equity_pct = (self._daily_pnl / self._cached_equity * 100) if self._cached_equity else 0
            return False, (
                f"Circuit breaker: daily P&L ${self._daily_pnl:+,.0f} "
                f"({equity_pct:+.1f}% of ${self._cached_equity:,.0f} equity)"
            )

        # 5. Ticker cooldown
        if self._ticker_in_cooldown(ticker):
            hrs = self.settings.auto_trade_ticker_cooldown_hours
            return False, f"Cooldown: {ticker} lost within last {hrs}h"

        # 7. Max trades per day
        ok, reason = await self._max_trades_today_check()
        if not ok:
            return False, reason

        # 8. Max open positions
        ok, reason = await self._max_positions_check()
        if not ok:
            return False, reason

        # 9. Max pending (unactioned) alerts — don't pile up if user isn't clicking
        max_pending = self.settings.auto_trade_max_pending
        if len(self._pending) >= max_pending:
            return False, (
                f"Pending cap: {len(self._pending)} unactioned alerts already live "
                f"(max {max_pending}) — waiting for confirms/skips"
            )

        # 10. Burst limiter — max N alerts per rolling window
        burst_window = self.settings.auto_trade_burst_window   # seconds, default 600
        burst_limit  = self.settings.auto_trade_burst_limit    # default 4
        # Prune stale timestamps
        self._alert_timestamps = [t for t in self._alert_timestamps if now - t < burst_window]
        if len(self._alert_timestamps) >= burst_limit:
            oldest = self._alert_timestamps[0]
            wait_s = int(burst_window - (now - oldest))
            return False, (
                f"Burst limit: {burst_limit} alerts sent in last "
                f"{burst_window//60:.0f} min — cooling down {wait_s}s"
            )

        # 2. Market regime — fetch first so cache is warm for the vol gate below
        ok, reason = await self._regime_allows(side)
        if not ok:
            return False, reason

        # 11. Intraday volatility gate — uses freshly-warmed regime cache
        if score > 0:
            day_chg = self._regime_cache[0]   # SPY % change today (now guaranteed fresh)
            vol_threshold = self.settings.intraday_vol_threshold   # default 1.5%
            vol_bump      = self.settings.intraday_vol_bump        # default 1.5 score pts
            if abs(day_chg) >= vol_threshold:
                effective_min = self.settings.auto_trade_score_threshold + vol_bump
                if score < effective_min:
                    return False, (
                        f"Vol gate: SPY {day_chg:+.1f}% today — need score ≥ {effective_min:.1f} "
                        f"(got {score:.1f}); bump={vol_bump:+.1f} for high volatility"
                    )

        return True, ""

    async def evaluate_signal(self, signal, account: dict):
        """Called after every scored signal. Routes qualifying signals to trade builders."""
        if not self.settings.auto_trade_enabled:
            return
        if signal is None:
            return
        if signal.score < self.settings.auto_trade_score_threshold:
            return

        # Update cached equity for % based sizing / circuit breaker
        equity = float(account.get("equity", 0) or 0)
        if equity > 0:
            self._cached_equity = equity

        side = signal.side.value
        ok, reason = await self._pre_flight(signal.ticker, side, score=signal.score)
        if not ok:
            logger.info(f"Auto-trade blocked [{signal.ticker}]: {reason}")
            return

        sig_type = signal.type.value
        if sig_type in ("sweep", "golden_sweep", "options_flow"):
            await self._build_options_trade(signal, account)
        elif sig_type == "insider_buy":
            await self._build_equity_trade(
                signal.ticker, side, signal.score, account,
                rationale=f"Insider open-market purchase | {signal.description[:80]}",
            )
        elif sig_type == "congress_trade" and side == "bullish":
            await self._build_equity_trade(
                signal.ticker, "bullish", signal.score, account,
                rationale=f"Congressional buy | {signal.description[:80]}",
            )

    async def evaluate_pattern(self, pattern_name: str, ticker: str,
                               score: float, evidence: list, account: dict):
        """Called when a pattern fires. High-score qualifying patterns queue trades."""
        if not self.settings.auto_trade_enabled:
            return

        # Update cached equity for % based sizing / circuit breaker
        equity = float(account.get("equity", 0) or 0)
        if equity > 0:
            self._cached_equity = equity

        # Long-term equity patterns — different sizing and brackets, lower threshold ok
        if pattern_name in EQUITY_LONG_PATTERNS:
            if score >= self.settings.auto_trade_pattern_threshold - 0.5:
                ok, reason = await self._pre_flight(ticker, "bullish", score=score, trade_type="equity_long")
                if not ok:
                    logger.info(f"Auto-trade equity-long blocked [{ticker}]: {reason}")
                    return
                await self._build_longterm_equity_trade(
                    ticker, score, account,
                    rationale=f"Pattern: {pattern_name} | {'; '.join(evidence[:2])}",
                )
            return

        if pattern_name not in AUTO_TRADE_PATTERNS:
            return
        if score < self.settings.auto_trade_pattern_threshold:
            return

        # Determine side from evidence
        side = "bullish"
        if any("put" in e.lower() or "bearish" in e.lower() for e in evidence):
            side = "bearish"

        ok, reason = await self._pre_flight(ticker, side, score=score)
        if not ok:
            logger.info(f"Auto-trade pattern blocked [{ticker}]: {reason}")
            return

        # Check if pattern evidence includes options data → options trade
        options_ev = [e for e in evidence if any(
            kw in e.lower() for kw in ("sweep", "call", "put", "flow", "golden")
        )]

        if options_ev:
            await self._build_options_trade_from_db(ticker, score, evidence, account)
        else:
            await self._build_equity_trade(
                ticker, side, score, account,
                rationale=f"Pattern: {pattern_name} | {'; '.join(evidence[:2])}",
            )

    # ── Trade builders ───────────────────────────────────────────────────────

    async def _build_options_trade(self, signal, account: dict):
        """Build an options trade from a signal with strike + expiry."""
        if not signal.strike or not signal.expiry:
            return

        opt_type = (signal.option_type or "call").lower()

        # ── Filter 1: Puts require a higher score bar ─────────────────────
        if "put" in opt_type and signal.score < self.settings.auto_trade_put_min_score:
            logger.info(
                f"Auto-trade: {signal.ticker} PUT blocked — score {signal.score:.1f} "
                f"< put_min {self.settings.auto_trade_put_min_score:.1f}"
            )
            return

        dte = self._calc_dte(signal.expiry)
        if dte is None:
            return

        # ── Filter 3: DTE window (3–10 days is the profitable zone) ──────
        min_dte = self.settings.auto_trade_min_dte   # 3
        max_dte = self.settings.auto_trade_max_dte   # 10
        if dte < min_dte and signal.score < ZERO_DTE_MIN_SCORE:
            logger.debug(
                f"Auto-trade: {signal.ticker} DTE={dte} < {min_dte}, "
                f"score {signal.score:.1f} < {ZERO_DTE_MIN_SCORE}"
            )
            return
        if dte > max_dte:
            logger.debug(f"Auto-trade: {signal.ticker} DTE={dte} > {max_dte}, skipping")
            return

        occ = self._occ_symbol(signal.ticker, signal.expiry, opt_type, signal.strike)
        if not occ:
            return

        quote = await self._get_option_quote(occ)
        ask, bid = quote.get("ask", 0), quote.get("bid", 0)

        if ask <= 0 and bid <= 0:
            logger.warning(f"Auto-trade: no quote for {occ}")
            return

        # ── Filter 4b: Liquidity check — bid must be meaningful ───────────
        if bid < 0.05:
            logger.info(
                f"Auto-trade: {occ} blocked — bid ${bid:.2f} < $0.05 (no liquidity)"
            )
            return

        # ── Filter 4b: Moneyness — reject deep OTM options ────────────────
        underlying_price = await self._get_equity_price(signal.ticker)
        if underlying_price > 0 and signal.strike:
            otm_pct = abs(signal.strike - underlying_price) / underlying_price
            max_otm = self.settings.auto_trade_max_otm_pct  # 0.20 = 20%
            if otm_pct > max_otm:
                logger.info(
                    f"Auto-trade: {occ} blocked — {otm_pct*100:.1f}% OTM "
                    f"(strike ${signal.strike:.2f} vs underlying ${underlying_price:.2f}, "
                    f"cap {max_otm*100:.0f}%)"
                )
                return

        # Limit price logic: pay ask but not if spread is > 2.5x bid
        if ask > 0 and bid > 0 and ask > bid * 2.5:
            limit_price = round(bid * 1.15, 2)
        elif ask > 0:
            limit_price = round(ask, 2)
        else:
            limit_price = round(bid * 1.10, 2)

        # ── Filter 4: Options price cap ────────────────────────────────────
        max_price = self.settings.auto_trade_max_option_price  # $8
        if limit_price > max_price:
            logger.info(
                f"Auto-trade: {occ} blocked — price ${limit_price:.2f} > cap ${max_price:.2f}"
            )
            return

        equity = float(account.get("equity", 100_000))
        qty, risk = self._size_options(equity, limit_price)
        if qty == 0:
            return

        await self._queue(
            ticker=signal.ticker,
            trade_type="option",
            symbol=occ,
            side=signal.side.value,
            option_type=opt_type,
            strike=signal.strike,
            expiry=signal.expiry,
            dte=dte,
            qty=qty,
            limit_price=limit_price,
            risk_amount=risk,
            stop_pct=40.0,
            target_pct=80.0,
            score=signal.score,
            rationale=signal.description[:120],
        )

    async def _build_options_trade_from_db(self, ticker: str, score: float,
                                           evidence: list, account: dict):
        """For pattern-triggered trades: pull the best recent options signal from DB."""
        rows = await self._db.get_options_flow(
            ticker=ticker, min_premium=100_000, has_sweep=True, limit=1,
        )
        if not rows:
            rows = await self._db.get_options_flow(
                ticker=ticker, min_premium=50_000, limit=1,
            )
        if not rows:
            logger.debug(f"Auto-trade pattern: no recent options for {ticker}")
            return

        row = rows[0]

        # Build a minimal signal-like namespace
        class _S:
            pass
        s = _S()
        s.ticker      = ticker
        s.strike      = row.get("strike")
        s.expiry      = row.get("expiry")
        s.option_type = row.get("opt_type", "call")
        s.score       = score
        s.description = f"Pattern | {'; '.join(evidence[:2])}"

        class _Side:
            def __init__(self, v): self.value = v
        s.side = _Side("bullish" if s.option_type == "call" else "bearish")

        await self._build_options_trade(s, account)

    async def _build_equity_trade(self, ticker: str, side: str, score: float,
                                  account: dict, rationale: str = ""):
        """Build an equity (stock) trade suggestion."""
        price = await self._get_equity_price(ticker)
        if price <= 0:
            logger.warning(f"Auto-trade: no equity price for {ticker}")
            return

        limit_price = round(price * 1.005, 2)  # 0.5% above mid
        equity = float(account.get("equity", 100_000))
        qty, risk = self._size_equity(equity, limit_price)
        if qty == 0:
            return

        await self._queue(
            ticker=ticker,
            trade_type="equity",
            symbol=ticker,
            side=side,
            option_type=None,
            strike=None,
            expiry=None,
            dte=None,
            qty=qty,
            limit_price=limit_price,
            risk_amount=risk,
            stop_pct=5.0,
            target_pct=15.0,
            score=score,
            rationale=rationale[:120],
        )

    async def _build_longterm_equity_trade(self, ticker: str, score: float,
                                           account: dict, rationale: str = ""):
        """Build a long-term equity hold (insider cluster / congress + sweep patterns).

        Sized at equity_long_risk_pct (5% vs 2% for options) with a wider bracket:
        TP +30%, SL -10% — designed to ride multi-week institutional moves.
        """
        price = await self._get_equity_price(ticker)
        if price <= 0:
            logger.warning(f"Auto-trade (equity-long): no price for {ticker}")
            return

        limit_price = round(price * 1.005, 2)  # 0.5% above mid
        equity = float(account.get("equity", self._cached_equity))
        long_risk_pct = self.settings.equity_long_risk_pct   # 0.05
        qty, risk = self._size_equity(equity, limit_price, risk_pct=long_risk_pct)
        if qty == 0:
            return

        tp_pct   = self.settings.equity_long_target_pct   # 30.0
        sl_pct   = self.settings.equity_long_stop_pct     # 10.0

        await self._queue(
            ticker=ticker,
            trade_type="equity_long",
            symbol=ticker,
            side="bullish",
            option_type=None,
            strike=None,
            expiry=None,
            dte=None,
            qty=qty,
            limit_price=limit_price,
            risk_amount=risk,
            stop_pct=sl_pct,
            target_pct=tp_pct,
            score=score,
            rationale=rationale[:120],
        )
        logger.info(
            f"EQUITY-LONG QUEUED: {ticker} x{qty} @ ${limit_price:.2f} "
            f"| risk=${risk:,.0f} ({long_risk_pct*100:.0f}% equity) "
            f"| TP +{tp_pct:.0f}% SL -{sl_pct:.0f}% | score={score:.1f}"
        )

    # ── Queue management ─────────────────────────────────────────────────────

    async def _queue(self, **kwargs):
        """Persist → in-memory → Telegram alert → schedule expiry."""
        ticker = kwargs.get("ticker", "")
        symbol = kwargs.get("symbol", "")

        # Deduplicate: don't queue same contract twice
        for pend in self._pending.values():
            if pend.symbol == symbol and pend.ticker == ticker:
                logger.debug(f"Auto-trade: already pending for {symbol}")
                return

        expires_at = datetime.utcnow() + timedelta(minutes=5)

        trade_id = await self._db.save_pending_trade(expires_at=expires_at, **kwargs)
        if not trade_id:
            return

        suggestion = TradeSuggestion(
            id=trade_id,
            ticker=ticker,
            trade_type=kwargs["trade_type"],
            symbol=symbol,
            side=kwargs["side"],
            option_type=kwargs.get("option_type"),
            strike=kwargs.get("strike"),
            expiry=kwargs.get("expiry"),
            dte=kwargs.get("dte"),
            qty=kwargs["qty"],
            limit_price=kwargs["limit_price"],
            risk_amount=kwargs["risk_amount"],
            stop_pct=kwargs.get("stop_pct", 40.0),
            target_pct=kwargs.get("target_pct", 80.0),
            score=kwargs.get("score", 0.0),
            rationale=kwargs.get("rationale", ""),
            expires_at=expires_at,
        )
        self._pending[trade_id] = suggestion

        # Record timestamp for burst limiter
        self._alert_timestamps.append(time.time())

        # Fire Telegram alert
        if self._telegram and self._telegram.enabled:
            msg_id = await self._telegram.send_trade_alert({
                "id": trade_id, **kwargs,
                "expires_at": expires_at.isoformat(),
            })
            if msg_id:
                suggestion.telegram_msg_id = msg_id
                await self._db.update_pending_trade(trade_id, telegram_msg_id=msg_id)

        # Broadcast to frontend
        from api.websocket import manager
        await manager.broadcast({
            "type": "trade_queued",
            "data": {
                "id": trade_id,
                "ticker": ticker,
                "symbol": symbol,
                "trade_type": kwargs["trade_type"],
                "option_type": kwargs.get("option_type"),
                "strike": kwargs.get("strike"),
                "expiry": kwargs.get("expiry"),
                "dte": kwargs.get("dte"),
                "side": kwargs["side"],
                "qty": kwargs["qty"],
                "limit_price": kwargs["limit_price"],
                "risk_amount": kwargs["risk_amount"],
                "stop_pct": kwargs.get("stop_pct", 40),
                "target_pct": kwargs.get("target_pct", 80),
                "score": kwargs.get("score", 0),
                "rationale": kwargs.get("rationale", ""),
                "expires_at": expires_at.isoformat(),
                "status": "pending",
            }
        })

        asyncio.create_task(self._expire(trade_id))

        logger.info(
            f"TRADE QUEUED: {symbol} x{kwargs['qty']} @ ${kwargs['limit_price']:.2f} "
            f"| risk=${kwargs['risk_amount']:,.0f} | score={kwargs.get('score',0):.1f}"
        )

    async def _expire(self, trade_id: int):
        await asyncio.sleep(5 * 60)
        s = self._pending.pop(trade_id, None)
        if s is None:
            return  # already confirmed or skipped
        await self._db.update_pending_trade(trade_id, status="expired")
        if self._telegram and s.telegram_msg_id:
            await self._telegram.edit_message(
                s.telegram_msg_id,
                f"⏰ <b>EXPIRED</b> — {s.ticker} trade window closed\n"
                f"<i>Signal was not acted on within 5 minutes</i>",
            )
        logger.info(f"Trade expired: id={trade_id} {s.ticker}")

    # ── Confirm / Skip (called by Telegram callbacks + API) ──────────────────

    async def confirm_trade(self, trade_id: int, msg_id: int) -> dict:
        """User tapped EXECUTE. Place the Alpaca order."""
        s = self._pending.get(trade_id)
        if not s:
            if self._telegram and msg_id:
                await self._telegram.edit_message(
                    msg_id,
                    "⚠️ <b>Trade unavailable</b> — expired or already executed"
                )
            return {"error": "not_found"}

        # Compute bracket TP/SL prices from signal's target/stop percentages
        tp_price = round(s.limit_price * (1 + s.target_pct / 100), 2)
        sl_price = round(s.limit_price * (1 - s.stop_pct / 100), 2)

        try:
            result = self._trader.bracket_order(
                ticker=s.symbol,
                qty=s.qty,
                side="buy",
                limit_price=s.limit_price,
                take_profit_price=tp_price,
                stop_loss_price=sl_price,
            )
            # If bracket fails (e.g. options don't support it), fall back to plain limit
            if "error" in result:
                logger.warning(f"Bracket order failed, falling back to limit: {result['error']}")
                result = self._trader.limit_order(
                    ticker=s.symbol,
                    qty=s.qty,
                    side="buy",
                    limit_price=s.limit_price,
                )
        except Exception as e:
            logger.error(f"Order execution error: {e}")
            result = {"error": str(e)}

        if "error" in result:
            if self._telegram and msg_id:
                await self._telegram.edit_message(
                    msg_id,
                    f"❌ <b>ORDER FAILED</b>\n"
                    f"{s.ticker}: <code>{result['error']}</code>"
                )
            await self._db.update_pending_trade(trade_id, status="failed")
            return result

        # Success
        self._pending.pop(trade_id, None)
        order_id = result.get("id", "")
        await self._db.update_pending_trade(
            trade_id,
            status="confirmed",
            alpaca_order_id=order_id,
            executed_at=datetime.utcnow().isoformat(),
        )

        if s.trade_type == "equity_long":
            type_label = "📈 LONG-TERM EQUITY"
        elif s.option_type == "call":
            type_label = "CALL"
        elif s.option_type == "put":
            type_label = "PUT"
        else:
            type_label = ""

        if self._telegram and msg_id:
            await self._telegram.edit_message(
                msg_id,
                f"✅ <b>ORDER PLACED (BRACKET)</b>\n"
                f"{s.ticker} {type_label}  {s.qty}x @ ${s.limit_price:.2f}\n"
                f"🎯 TP: ${tp_price:.2f} (+{s.target_pct:.0f}%)  |  🛑 SL: ${sl_price:.2f} (-{s.stop_pct:.0f}%)\n"
                f"Risk: ${s.risk_amount:,.0f}\n"
                f"Order ID: <code>{order_id[:12]}</code>"
            )

        logger.info(f"Trade executed: {s.symbol} x{s.qty} @ {s.limit_price} | order={order_id}")
        return result

    async def skip_trade(self, trade_id: int, msg_id: int):
        """User tapped SKIP."""
        s = self._pending.pop(trade_id, None)
        await self._db.update_pending_trade(trade_id, status="skipped")
        if self._telegram and msg_id:
            ticker = s.ticker if s else "Trade"
            await self._telegram.edit_message(
                msg_id,
                f"❌ <b>SKIPPED</b> — {ticker} passed"
            )
        logger.info(f"Trade skipped: id={trade_id}")

    async def get_pending(self) -> list[dict]:
        return await self._db.get_pending_trades(status="pending")

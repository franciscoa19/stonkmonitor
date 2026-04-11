"""
Earnings IV/RV Scanner — adapted from trade calculator

Strategy: sell premium (straddle/strangle) before earnings when:
  1. Liquidity OK        — 30-day avg volume >= 1.5M
  2. IV expensive vs RV  — iv30 / yang_zhang_rv30 >= 1.25
  3. Term structure inverted — front-to-45d slope <= -0.00406
                              (earnings-driven front-month IV spike)

When all 3 pass → SELL setup (score 8.0+)
When ts_slope + 1 other pass → CONSIDER (score 6.0)
Signal includes expected_move from front-month straddle price

Data source: yfinance (free, no key needed, options chains + OHLC)
Run: once per 30 min per ticker to avoid rate limits
"""
import logging
import asyncio
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.interpolate import interp1d

logger = logging.getLogger(__name__)


# ── Core quant functions (ported from calculator.py) ─────────────────────────

def yang_zhang(price_data, window: int = 30, trading_periods: int = 252) -> float:
    """
    Yang-Zhang historical volatility estimator.
    Uses OHLC data — more accurate than close-to-close (handles gaps).
    Returns annualized volatility as a decimal (e.g. 0.32 = 32%).
    """
    log_ho = (price_data["High"]  / price_data["Open"]).apply(np.log)
    log_lo = (price_data["Low"]   / price_data["Open"]).apply(np.log)
    log_co = (price_data["Close"] / price_data["Open"]).apply(np.log)

    log_oc    = (price_data["Open"] / price_data["Close"].shift(1)).apply(np.log)
    log_oc_sq = log_oc ** 2
    log_cc    = (price_data["Close"] / price_data["Close"].shift(1)).apply(np.log)
    log_cc_sq = log_cc ** 2

    rs = log_ho * (log_ho - log_co) + log_lo * (log_lo - log_co)

    close_vol  = log_cc_sq.rolling(window, center=False).sum() * (1.0 / (window - 1))
    open_vol   = log_oc_sq.rolling(window, center=False).sum() * (1.0 / (window - 1))
    window_rs  = rs.rolling(window, center=False).sum()        * (1.0 / (window - 1))

    k      = 0.34 / (1.34 + (window + 1) / (window - 1))
    result = (open_vol + k * close_vol + (1 - k) * window_rs).apply(np.sqrt) * np.sqrt(trading_periods)
    return float(result.iloc[-1])


def build_term_structure(dtes: list, ivs: list):
    """Linear-interpolated IV term structure. Clamps outside observed range."""
    d = np.array(dtes)
    v = np.array(ivs)
    idx = d.argsort()
    d, v = d[idx], v[idx]

    spline = interp1d(d, v, kind="linear", fill_value="extrapolate")

    def term(dte: float) -> float:
        if dte < d[0]:  return float(v[0])
        if dte > d[-1]: return float(v[-1])
        return float(spline(dte))

    return term


def _filter_exp_dates(dates: list) -> list:
    """Keep expiries from nearest to first date >= 45 days out."""
    today   = datetime.today().date()
    cutoff  = today + timedelta(days=45)
    sorted_ = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in dates)

    for i, d in enumerate(sorted_):
        if d >= cutoff:
            arr = [x.strftime("%Y-%m-%d") for x in sorted_[: i + 1]]
            if arr and arr[0] == today.strftime("%Y-%m-%d"):
                arr = arr[1:]
            return arr if arr else []

    return []


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class EarningsSetup:
    ticker:          str
    price:           float
    avg_volume:      float
    iv30:            float       # implied vol at 30 DTE
    rv30:            float       # Yang-Zhang realized vol (30-day)
    iv30_rv30:       float       # ratio — above 1.25 is expensive premium
    ts_slope:        float       # term structure slope (neg = inverted)
    expected_move:   Optional[str]  # e.g. "4.23%"

    # Pass/fail
    vol_ok:          bool        # avg_volume >= 1.5M
    iv_expensive:    bool        # iv30_rv30 >= 1.25
    ts_inverted:     bool        # ts_slope <= -0.00406

    @property
    def passes(self) -> int:
        return sum([self.vol_ok, self.iv_expensive, self.ts_inverted])

    @property
    def recommendation(self) -> str:
        if self.vol_ok and self.iv_expensive and self.ts_inverted:
            return "SELL_PREMIUM"       # strong: all 3 pass
        if self.ts_inverted and self.passes >= 2:
            return "CONSIDER"           # marginal: ts + 1 other
        return "AVOID"

    @property
    def score(self) -> float:
        if self.recommendation == "SELL_PREMIUM": return 8.0
        if self.recommendation == "CONSIDER":     return 6.0
        return 0.0

    def to_dict(self) -> dict:
        return {
            "ticker":        self.ticker,
            "price":         round(self.price, 2),
            "avg_volume":    round(self.avg_volume),
            "iv30_pct":      round(self.iv30 * 100, 1),
            "rv30_pct":      round(self.rv30 * 100, 1),
            "iv30_rv30":     round(self.iv30_rv30, 3),
            "ts_slope":      round(self.ts_slope, 6),
            "expected_move": self.expected_move,
            "vol_ok":        self.vol_ok,
            "iv_expensive":  self.iv_expensive,
            "ts_inverted":   self.ts_inverted,
            "passes":        self.passes,
            "recommendation":self.recommendation,
            "score":         self.score,
        }


# ── Main scanner ──────────────────────────────────────────────────────────────

async def scan_ticker(ticker: str) -> Optional[EarningsSetup]:
    """
    Run the full IV/RV earnings scan for one ticker.
    Returns EarningsSetup if enough data exists, else None.
    Runs yfinance in a thread so it doesn't block the event loop.
    """
    ticker = ticker.strip().upper()
    if not ticker:
        return None

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, _compute_sync, ticker
        )
        return result
    except Exception as e:
        logger.debug(f"Earnings scan {ticker}: {e}")
        return None


def _compute_sync(ticker: str) -> Optional[EarningsSetup]:
    """Blocking yfinance computation — call via run_in_executor."""
    import yfinance as yf

    stock = yf.Ticker(ticker)

    # ── Check options exist ────────────────────────────────────────────
    try:
        all_dates = list(stock.options)
        if not all_dates:
            return None
    except Exception:
        return None

    exp_dates = _filter_exp_dates(all_dates)
    if not exp_dates:
        return None

    # ── Current price ──────────────────────────────────────────────────
    hist_1d = stock.history(period="1d")
    if hist_1d.empty:
        return None
    underlying = float(hist_1d["Close"].iloc[-1])

    # ── Build IV term structure + grab front straddle ──────────────────
    atm_ivs: dict[str, float] = {}
    straddle_price: Optional[float] = None

    for i, exp_date in enumerate(exp_dates):
        try:
            chain = stock.option_chain(exp_date)
            calls, puts = chain.calls, chain.puts
            if calls.empty or puts.empty:
                continue

            call_idx = (calls["strike"] - underlying).abs().idxmin()
            put_idx  = (puts["strike"]  - underlying).abs().idxmin()
            call_iv  = float(calls.loc[call_idx, "impliedVolatility"])
            put_iv   = float(puts.loc[put_idx,  "impliedVolatility"])
            atm_ivs[exp_date] = (call_iv + put_iv) / 2.0

            # Front month straddle for expected move
            if i == 0:
                cb = calls.loc[call_idx, "bid"]
                ca = calls.loc[call_idx, "ask"]
                pb = puts.loc[put_idx,  "bid"]
                pa = puts.loc[put_idx,  "ask"]
                if all(v is not None for v in [cb, ca, pb, pa]):
                    straddle_price = (cb + ca) / 2.0 + (pb + pa) / 2.0
        except Exception:
            continue

    if not atm_ivs:
        return None

    # ── DTE list + term spline ─────────────────────────────────────────
    today = datetime.today().date()
    dtes, ivs = [], []
    for exp, iv in atm_ivs.items():
        dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        dtes.append(dte)
        ivs.append(iv)

    if len(dtes) < 2:
        return None

    term = build_term_structure(dtes, ivs)
    iv30 = term(30)

    ts_slope = (term(45) - term(dtes[0])) / max(1, 45 - dtes[0])

    # ── Yang-Zhang realized vol ────────────────────────────────────────
    price_hist = stock.history(period="3mo")
    if len(price_hist) < 32:
        return None
    rv30 = yang_zhang(price_hist)

    iv30_rv30 = iv30 / rv30 if rv30 > 0 else 0.0

    # ── Volume ─────────────────────────────────────────────────────────
    avg_vol = float(price_hist["Volume"].rolling(30).mean().dropna().iloc[-1])

    expected_move = (
        f"{round(straddle_price / underlying * 100, 2)}%"
        if straddle_price
        else None
    )

    return EarningsSetup(
        ticker       = ticker,
        price        = underlying,
        avg_volume   = avg_vol,
        iv30         = iv30,
        rv30         = rv30,
        iv30_rv30    = iv30_rv30,
        ts_slope     = ts_slope,
        expected_move= expected_move,
        vol_ok       = avg_vol   >= 1_500_000,
        iv_expensive = iv30_rv30 >= 1.25,
        ts_inverted  = ts_slope  <= -0.00406,
    )

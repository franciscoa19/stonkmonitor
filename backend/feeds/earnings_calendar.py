"""
Earnings calendar — multi-source, date-keyed, cached.

Primary source is Nasdaq's public calendar (api.nasdaq.com/api/calendar/earnings
?date=YYYY-MM-DD): free, no key, and date-keyed — one call returns every name
reporting that day, so covering the whole watchlist for the next N days is ~N
calls once a day instead of one rate-limited yfinance call per ticker per scan.
yfinance stays as the per-ticker fallback (see earnings_scanner).

The scanner asks `get_next_earnings(ticker)`; we keep a {ticker: date} map
refreshed on a TTL. Alpha Vantage's EARNINGS_CALENDAR could slot in as a third
source here, but its free tier is 25 req/day — Nasdaq + yfinance already gives
two independent sources, so it's off unless a key is provided.
"""
import json
import time
import logging
import threading
import urllib.request
import urllib.error
from datetime import date, timedelta
from typing import Iterable, Optional

from market_time import et_today

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Accept": "application/json",
}

_HORIZON_DAYS = 60          # how far ahead to build the calendar (covers the season)
_TTL = 12 * 3600           # refresh twice a day
_cache = {"map": {}, "ts": 0.0}   # {ticker: {"date": iso, "time": str}}
_lock = threading.Lock()


def _parse_market_cap(raw) -> float:
    """Nasdaq ships market cap as a display string ('$399,729,623,000'). Returns
    0.0 for 'N/A'/blank so an unknown cap simply fails a minimum-size filter."""
    try:
        s = str(raw or "").replace("$", "").replace(",", "").strip()
        return float(s) if s else 0.0
    except (TypeError, ValueError):
        return 0.0


def _fetch_nasdaq_day(d: date) -> list[dict]:
    """Return [{symbol, time, market_cap}] for one calendar day, or [] on failure."""
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}"
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read())
        rows = ((body.get("data") or {}).get("rows")) or []
        out = []
        for row in rows:
            sym = (row.get("symbol") or "").strip().upper()
            if sym:
                out.append({"symbol": sym, "time": row.get("time") or "",
                            "market_cap": _parse_market_cap(row.get("marketCap"))})
        return out
    except Exception as e:
        logger.debug(f"nasdaq earnings {d}: {e}")
        return []


def _refresh(horizon: int = _HORIZON_DAYS) -> None:
    """Rebuild the {ticker: earliest-future date} map from Nasdaq. Polite pacing."""
    today = et_today()
    m: dict = {}
    got_any = False
    for i in range(horizon):
        d = today + timedelta(days=i)
        if d.weekday() >= 5:          # skip weekends — no US earnings
            continue
        rows = _fetch_nasdaq_day(d)
        if rows:
            got_any = True
        for row in rows:
            sym = row["symbol"]
            if sym not in m:          # dates iterate ascending → first is earliest
                m[sym] = {"date": d.isoformat(), "time": row["time"],
                          "market_cap": row["market_cap"]}
        time.sleep(0.25)              # be polite to the endpoint
    if got_any:
        _cache["map"] = m
        _cache["ts"] = time.time()
        logger.info(f"Earnings calendar refreshed: {len(m)} names over {horizon}d (Nasdaq)")
    else:
        # Total failure (blocked/offline) — keep any stale map, nudge ts so we
        # retry sooner rather than hammering.
        _cache["ts"] = time.time() - _TTL + 900
        logger.warning("Earnings calendar refresh got nothing from Nasdaq — keeping stale map")


def _ensure_fresh() -> None:
    if time.time() - _cache["ts"] < _TTL and _cache["map"]:
        return
    with _lock:
        if time.time() - _cache["ts"] < _TTL and _cache["map"]:
            return
        _refresh()


def get_next_earnings(ticker: str):
    """Nearest future earnings date (YYYY-MM-DD) for `ticker`, or None.
    Backed by the cached Nasdaq calendar; blocks only on a cold/stale refresh."""
    if not ticker:
        return None
    try:
        _ensure_fresh()
    except Exception as e:
        logger.debug(f"earnings calendar refresh failed: {e}")
        return None
    rec = _cache["map"].get(ticker.strip().upper())
    return rec["date"] if rec else None


def get_upcoming_reporters(max_days: int, min_market_cap: float = 0.0,
                           limit: Optional[int] = None,
                           exclude: Iterable[str] = ()) -> list[dict]:
    """Names reporting within `max_days`, soonest first then largest cap.

    This is the measurement-only universe: the tradeable watchlist is 78 names,
    but the *logger* risks no capital, so it can price structures for far more
    prints than we would ever trade. Sorting by proximity first keeps the sample
    near the print, where the front-month IV the thesis depends on is actually
    inflated. Market cap is a free liquidity proxy — it rides along on the
    calendar rows, so filtering costs no extra request.
    """
    try:
        _ensure_fresh()
    except Exception as e:
        logger.debug(f"earnings calendar refresh failed: {e}")
        return []
    today = et_today()
    skip = {t.strip().upper() for t in exclude}
    out = []
    for sym, rec in _cache["map"].items():
        if sym in skip:
            continue
        cap = rec.get("market_cap") or 0.0
        if cap < min_market_cap:
            continue
        try:
            days = (date.fromisoformat(rec["date"]) - today).days
        except (TypeError, ValueError):
            continue
        if 0 <= days <= max_days:
            out.append({"ticker": sym, "date": rec["date"], "days": days,
                        "report_time": rec.get("time") or "", "market_cap": cap})
    out.sort(key=lambda r: (r["days"], -r["market_cap"]))
    return out[:limit] if limit else out


def get_report_time(ticker: str):
    """'pre'|'post'|None — when in the day the print lands (from Nasdaq 'time')."""
    rec = _cache["map"].get((ticker or "").strip().upper())
    if not rec:
        return None
    t = (rec.get("time") or "").lower()
    if "pre" in t:
        return "pre"
    if "after" in t or "post" in t:
        return "post"
    return None

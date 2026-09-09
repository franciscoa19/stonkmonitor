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


def _fetch_nasdaq_day(d: date) -> list[tuple]:
    """Return [(symbol, time_str)] for one calendar day, or [] on any failure."""
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
                out.append((sym, row.get("time") or ""))
        return out
    except Exception as e:
        logger.debug(f"nasdaq earnings {d}: {e}")
        return []


def _refresh(horizon: int = _HORIZON_DAYS) -> None:
    """Rebuild the {ticker: earliest-future date} map from Nasdaq. Polite pacing."""
    today = date.today()
    m: dict = {}
    got_any = False
    for i in range(horizon):
        d = today + timedelta(days=i)
        if d.weekday() >= 5:          # skip weekends — no US earnings
            continue
        rows = _fetch_nasdaq_day(d)
        if rows:
            got_any = True
        for sym, tm in rows:
            if sym not in m:          # dates iterate ascending → first is earliest
                m[sym] = {"date": d.isoformat(), "time": tm}
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

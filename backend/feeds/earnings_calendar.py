"""
Earnings calendar — multi-source, date-keyed, cached.

Primary source is Nasdaq's public calendar (api.nasdaq.com/api/calendar/earnings
?date=YYYY-MM-DD): free, no key, and date-keyed — one call returns every name
reporting that day, so covering the whole watchlist for the next N days is ~N
calls once a day instead of one rate-limited yfinance call per ticker per scan.
yfinance stays as the per-ticker fallback (see earnings_scanner). Yahoo's bulk
calendar supplies research candidates for days Nasdaq could not cover.

The scanner reads a {ticker: record} map refreshed on a TTL.
"""
import json
import os
import time
import logging
import threading
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Iterable, Optional

import tls_trust
from market_time import ET, et_today

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Accept": "application/json",
}

_HORIZON_DAYS = 60          # how far ahead to build the calendar (covers the season)
_TTL = 12 * 3600           # refresh twice a day
_RETRY_AFTER_FAILURE = 3600  # after a refresh Nasdaq answered nothing of
_GIVE_UP_AFTER = 3          # leading days refused in a row: blocked or offline, stop asking
_MAX_ENTRY_AGE = 3 * 86400  # a date Nasdaq has not confirmed again for this long is not used
_YAHOO_PAGE_SIZE = 100
_YAHOO_MAX_PAGES = 3
# Kept across restarts: Nasdaq has refused every request since 2026-11-10 (HTTP 403),
# and the map lived only in memory. Public calendar data, owner-only file.
_CACHE_FILE = Path(__file__).resolve().parent.parent / "earnings_calendar_cache.json"
# {ticker: {"date": iso, "time": str, "market_cap": float, "seen": epoch Nasdaq last listed it}}
_cache = {"map": {}, "ts": 0.0, "loaded": False, "answered_days": frozenset()}
_lock = threading.Lock()


def _parse_market_cap(raw) -> float:
    """Nasdaq ships market cap as a display string ('$399,729,623,000'). Returns
    0.0 for 'N/A'/blank so an unknown cap simply fails a minimum-size filter."""
    try:
        s = str(raw or "").replace("$", "").replace(",", "").strip()
        return float(s) if s else 0.0
    except (TypeError, ValueError):
        return 0.0


def _fetch_nasdaq_day(d: date) -> Optional[list[dict]]:
    """[{symbol, time, market_cap}] for one calendar day ([] when nobody reports),
    or None when Nasdaq did not answer."""
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}"
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=15, context=tls_trust.context()) as r:
            body = json.loads(r.read())
        if not isinstance(body.get("data"), dict):
            return None
        rows = body["data"].get("rows") or []
        out = []
        for row in rows:
            sym = (row.get("symbol") or "").strip().upper()
            if sym:
                out.append({"symbol": sym, "time": row.get("time") or "",
                            "market_cap": _parse_market_cap(row.get("marketCap"))})
        return out
    except Exception as e:
        logger.debug(f"nasdaq earnings {d}: {e}")
        return None


def _refresh(horizon: int = _HORIZON_DAYS) -> None:
    """Update the {ticker: earliest-future date} map from Nasdaq, day by day.

    A day Nasdaq answers replaces what we knew about that day; a day it does not
    answer keeps what we had. Replacing the whole map with a partial answer shrank
    it from 1,908 names to 734 in the week before Nasdaq started refusing every
    request (2026-11-10). Polite pacing, and no hammering a source that refuses.
    """
    today = et_today()
    now = time.time()
    fresh: dict = {}
    answered: set = set()
    refused = 0
    for i in range(horizon):
        d = today + timedelta(days=i)
        if d.weekday() >= 5:          # skip weekends — no US earnings
            continue
        rows = _fetch_nasdaq_day(d)
        if rows is None:
            refused += 1
            if not answered and refused >= _GIVE_UP_AFTER:
                break                 # refused from the first day on: blocked or offline
        else:
            answered.add(d.isoformat())
            for row in rows:          # days ascend, so the first listing is the earliest
                if row["symbol"] not in fresh:
                    fresh[row["symbol"]] = {"date": d.isoformat(), "time": row["time"],
                                            "market_cap": row["market_cap"], "seen": now,
                                            "verified": True}
        time.sleep(0.25)              # be polite to the endpoint
    if not answered:
        _cache["map"] = {sym: {**rec, "seen": rec.get("seen", _cache["ts"]), "verified": False}
                         for sym, rec in _cache["map"].items()}
        _cache["answered_days"] = frozenset()
        _cache["ts"] = now - _TTL + _RETRY_AFTER_FAILURE
        logger.warning("Earnings calendar refresh got nothing from Nasdaq — keeping what we had")
        return
    kept = {}
    for sym, rec in _cache["map"].items():
        when = str(rec.get("date") or "")
        if sym not in fresh and when >= today.isoformat() and when not in answered:
            kept[sym] = {**rec, "seen": rec.get("seen", _cache["ts"]), "verified": False}
    _cache["map"] = {**kept, **fresh}
    _cache["answered_days"] = frozenset(answered)
    _cache["ts"] = now
    _save()
    logger.info(f"Earnings calendar refreshed: {len(fresh)} names from {len(answered)} days "
                f"answered, {len(kept)} kept from earlier (Nasdaq)")


def _save() -> None:
    """Write the map atomically, readable by the owner only."""
    tmp = _CACHE_FILE.with_name(_CACHE_FILE.name + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"map": _cache["map"], "ts": _cache["ts"]}, f)
        os.replace(tmp, _CACHE_FILE)
    except OSError as e:
        logger.warning(f"Earnings calendar not saved: {type(e).__name__}")


def _load() -> None:
    """The last saved map, so a restart does not begin with nothing."""
    _cache["loaded"] = True
    try:
        saved = json.loads(_CACHE_FILE.read_text())
        m, ts = saved["map"], float(saved["ts"])
        if not 0 <= ts <= time.time() or not isinstance(m, dict) or not all(
                isinstance(r, dict) and isinstance(r.get("date"), str) for r in m.values()):
            raise ValueError("unexpected shape")
    except FileNotFoundError:
        return
    except Exception as e:
        logger.warning(f"Earnings calendar cache ignored: {type(e).__name__}")
        return
    # Disk is discovery data. Only a successful refresh in this process can
    # verify the row for trading; a recent save is not a fresh confirmation.
    _cache["map"] = {sym: {**rec, "seen": rec.get("seen", ts), "verified": False}
                     for sym, rec in m.items()}
    _cache["ts"] = ts
    _cache["answered_days"] = frozenset()


def _ensure_fresh() -> None:
    if not _cache["loaded"]:
        with _lock:
            if not _cache["loaded"]:
                _load()
    if 0 <= time.time() - _cache["ts"] < _TTL:
        return
    with _lock:
        if 0 <= time.time() - _cache["ts"] < _TTL:
            return
        _refresh()


def _record(ticker: str) -> Optional[dict]:
    """A ticker's entry, if it is still ahead and Nasdaq has listed it lately."""
    rec = _cache["map"].get((ticker or "").strip().upper())
    if not rec:
        return None
    try:
        age = time.time() - float(rec.get("seen", _cache["ts"]))
        report_date = date.fromisoformat(rec["date"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return rec if 0 <= age <= _MAX_ENTRY_AGE and report_date >= et_today() else None


def get_earnings_record(ticker: str) -> Optional[dict]:
    """Return the date, timing and verification status from the same row."""
    if not ticker:
        return None
    try:
        _ensure_fresh()
    except Exception as e:
        logger.debug(f"earnings calendar refresh failed: {e}")
        return None
    rec = _record(ticker)
    if not rec:
        return None
    seen = float(rec.get("seen", _cache["ts"]))
    verified = (rec.get("verified") is True and time.time() - seen < _TTL
                and datetime.fromtimestamp(seen, ET).date() == et_today())
    return {"date": rec["date"], "timing": _normalize_report_time(rec.get("time")),
            "verified": verified}


def get_next_earnings(ticker: str):
    """Nearest upcoming date, including retained rows for discovery."""
    rec = get_earnings_record(ticker)
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
    for sym in list(_cache["map"]):
        rec = _record(sym)
        if rec is None or sym in skip:
            continue
        cap = _parse_market_cap(rec.get("market_cap"))
        if not isfinite(cap) or cap < min_market_cap:
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


def _yahoo_measurement_reporters(today: date, max_days: int, min_market_cap: float,
                                 missing: set[str], exclude: set[str]) -> list[dict]:
    """Bounded bulk discovery. Do not use this feed as proof of release timing."""
    from yfinance import Calendars

    calendar = Calendars()
    rows = []
    for page in range(_YAHOO_MAX_PAGES):
        try:
            frame = calendar.get_earnings_calendar(
                start=today, end=today + timedelta(days=max_days + 1),
                market_cap=min_market_cap or None, filter_most_active=False,
                limit=_YAHOO_PAGE_SIZE, offset=page * _YAHOO_PAGE_SIZE)
        except Exception as exc:
            logger.warning("Yahoo research calendar unavailable: %s", type(exc).__name__)
            break
        for raw_symbol, row in frame.iterrows():
            if not isinstance(raw_symbol, str):
                continue
            ticker = raw_symbol.strip().upper()
            if not ticker or ticker in exclude:
                continue
            try:
                stamp = datetime.fromisoformat(str(row.get("Event Start Date")))
                if stamp.tzinfo is None:
                    continue
                day = stamp.astimezone(ET).date()
            except (TypeError, ValueError, OverflowError):
                continue
            cap = _parse_market_cap(row.get("Marketcap"))
            if day.isoformat() not in missing or not isfinite(cap) or cap < min_market_cap:
                continue
            rows.append({"ticker": ticker, "date": day.isoformat(), "days": (day - today).days,
                         "market_cap": cap, "report_time": ""})
        if len(frame) < _YAHOO_PAGE_SIZE:
            break
    return rows


def get_measurement_reporters(max_days: int, min_market_cap: float = 0.0,
                              limit: Optional[int] = None,
                              exclude: Iterable[str] = ()) -> list[dict]:
    """Fill research discovery gaps without supplying timing to the order path."""
    skip = {ticker.strip().upper() for ticker in exclude}
    nasdaq = get_upcoming_reporters(max_days, min_market_cap, exclude=skip)
    today = et_today()
    answered = _cache["answered_days"]
    missing = {d.isoformat() for i in range(max_days + 1)
               if (d := today + timedelta(days=i)).weekday() < 5} - answered
    by_ticker = {row["ticker"]: row for row in nasdaq}
    if missing:
        yahoo = _yahoo_measurement_reporters(today, max_days, min_market_cap, missing, skip)
        yahoo_by_ticker = {}
        for row in sorted(yahoo, key=lambda r: (r["days"], -r["market_cap"])):
            yahoo_by_ticker.setdefault(row["ticker"], row)
        for row in yahoo_by_ticker.values():
            old = by_ticker.get(row["ticker"])
            if old is None or old["date"] not in answered:
                by_ticker[row["ticker"]] = row
    rows = sorted(by_ticker.values(), key=lambda r: (r["days"], -r["market_cap"], r["ticker"]))
    return rows[:limit] if limit else rows


def get_report_time(ticker: str):
    """'pre'|'post'|None — when in the day the print lands (from Nasdaq 'time')."""
    rec = _record(ticker)
    return _normalize_report_time(rec.get("time")) if rec else None


def _normalize_report_time(value):
    t = str(value or "").lower()
    if "pre" in t:
        return "pre"
    if "after" in t or "post" in t:
        return "post"
    return None

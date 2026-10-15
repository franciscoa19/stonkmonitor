"""What would different exit rules have done? A read-only replay.

Every closed condor the bot traded, and every resolved research condor, is
replayed against the five-minute price history of its stock and its four
options, under these rules:

  hold          — hold to expiry; settle at intrinsic value on the expiry close.
  policy        — the bot's rules, simplified: take profit when the buy-back
                  costs half the credit or less; otherwise close at 09:45 ET on
                  the first session after the earnings date; otherwise close at
                  15:00 ET on expiry day (12:00 on a 13:00 early close).
  breach stop   — policy, plus close when a completed stock bar ends through a
                  short strike (intrabar crossings are not reconstructed).
  loss stop     — policy, plus close once the paper loss reaches half the
                  condor's maximum loss.

Like the bot, no rule acts before 09:45 ET. All timestamps in price series are
bar COMPLETION times. A decision can only be valued from later option bars
whose entire intervals follow it; this remains a hypothetical fill, not proof
an order would execute. Early-close sessions use the project market calendar.

Nothing here trades or writes. The ledger is opened read-only and the broker is
only asked for price history (GET).

Limits, stated once:
  * Option values come from the last TRADE in each leg's five-minute bar, not
    from executable quotes. Thinly traded legs make the value stale, and a real
    exit would pay part of the spread on top. Every rule is valued the same way,
    so these are diagnostic marks, not executable results. Rules may have
    different missing-data populations: compare matched events, not just means.
  * A rule that needs a value at a time when a leg has not traded in the past
    hour waits for later bars that price every leg after the decision. If none
    exists, that rule is "no data", even when holding to expiry can be valued.
  * The current policy's "hold to expiry when comfortably inside" refinement
    on expiry day is not modelled.
  * A handful of events is an anecdote. The summary prints its sample size.

Usage (from backend/):  venv/bin/python -m backtest.stop_study [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

from market_time import ET
from feeds.uw_budget import market_close_minute
from signals.iv_variants import settlement_ready

RULES = ("hold", "policy", "breach stop", "loss stop")
OPEN_SETTLE = time(9, 45)
FORCED_CLOSE_MINUTE = 15 * 60
BAR_INTERVAL = timedelta(minutes=5)
STALE_AFTER = timedelta(minutes=60)
# The data plan refuses history that ends within the last 15 minutes (HTTP 403),
# and a request ending in the future counts as that. Ask for a little less.
RECENT_DATA_LAG = timedelta(minutes=16)


@dataclass
class Event:
    kind: str                      # "trade" | "research"
    ident: str
    ticker: str
    qty: int
    credit: float                  # per spread
    fees: float                    # per spread, round trip, $
    legs: list                     # [{symbol, side, strike, right}]
    short_put: float
    short_call: float
    width: float
    entry: datetime                # UTC
    earnings_date: Optional[date]
    expiry: date
    recorded_pnl: Optional[float]  # trades: actual $ per spread; research: hold-to-expiry
    cohort: str = ""
    results: dict = field(default_factory=dict)
    unpriced_exits: dict = field(default_factory=dict)

    @property
    def max_loss(self) -> float:
        return max((self.width - self.credit) * 100, 0.0)


# ── Market data ──────────────────────────────────────────────────────────────
class AlpacaBars:
    """Five-minute bars over the broker's data API. GET only."""

    def __init__(self, trader):
        self.trader = trader
        self.base = getattr(trader, "_data_base", "https://data.alpaca.markets")

    def _pages(self, path: str, params: dict, key: str):
        out, token = {}, None
        for _ in range(50):
            q = dict(params, **({"page_token": token} if token else {}))
            code, body = self.trader._rest("GET", f"{self.base}{path}?{urllib.parse.urlencode(q)}")
            if code != 200 or not isinstance(body, dict):
                return None
            for symbol, bars in (body.get(key) or {}).items():
                out.setdefault(symbol, []).extend(bars)
            token = body.get("next_page_token")
            if not token:
                return out
        return None                                  # incomplete pagination is not complete history

    @staticmethod
    def _series(bars, *, through: Optional[datetime] = None) -> list:
        """A bar's closing price is observable at its END, never its opening stamp."""
        out = []
        for bar in bars or []:
            try:
                available = _parse_time(bar["t"]) + BAR_INTERVAL
                price = float(bar["c"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(price) and price >= 0 and (through is None or available <= through):
                out.append((available, price))
        return sorted(out)

    def stock(self, symbol: str, start: datetime, end: datetime) -> Optional[list]:
        got = self._pages("/v2/stocks/bars", {"symbols": symbol, "timeframe": "5Min", "feed": "iex",
                                              "adjustment": "raw", "limit": 10000,
                                              "start": start.isoformat(), "end": end.isoformat()}, "bars")
        return None if got is None else self._series(got.get(symbol), through=end)

    def options(self, symbols: list, start: datetime, end: datetime) -> Optional[dict]:
        got = self._pages("/v1beta1/options/bars", {"symbols": ",".join(symbols), "timeframe": "5Min",
                                                     "limit": 10000, "start": start.isoformat(),
                                                     "end": end.isoformat()}, "bars")
        return None if got is None else {s: self._series(got.get(s), through=end) for s in symbols}


def _parse_time(stamp: str) -> datetime:
    at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("History requires an aware timestamp")
    return at.astimezone(timezone.utc)


def _utc(stamp: str) -> datetime:
    at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def occ(ticker: str, expiry: date, right: str, strike: float) -> str:
    return f"{ticker.upper()}{expiry.strftime('%y%m%d')}{right}{int(round(strike * 1000)):08d}"


# ── Valuation ────────────────────────────────────────────────────────────────
def price_at(series: list, at: datetime, *, after: Optional[datetime] = None) -> Optional[float]:
    """Completed-bar mark, at most one hour old. Fills require a whole bar
    starting at or after the decision, not a trade seen before it was made."""
    found = None
    for when, px in series:
        if when > at:
            break
        if after is not None and when - BAR_INTERVAL < after:
            continue
        found = (when, px)
    if found is None or at - found[0] > STALE_AFTER:
        return None
    return found[1]


def debit_at(ev: Event, legs: dict, at: datetime, *, after: Optional[datetime] = None) -> Optional[float]:
    """What it costs to buy the condor back at `at`, from last trades."""
    total = 0.0
    for leg in ev.legs:
        px = price_at(legs.get(leg["symbol"]) or [], at, after=after)
        if px is None:
            return None
        total += px if leg["side"] == "sell" else -px
    return total


def intrinsic(ev: Event, spot: float) -> float:
    value = 0.0
    for leg in ev.legs:
        payoff = max(spot - leg["strike"], 0.0) if leg["right"] == "C" else max(leg["strike"] - spot, 0.0)
        value += payoff if leg["side"] == "sell" else -payoff
    return min(max(value, 0.0), ev.width)


def pnl(ev: Event, debit: float) -> float:
    return round((ev.credit - debit) * 100 - ev.fees, 2)


def _et(at: datetime) -> datetime:
    return at.astimezone(ET)


def _session_close(day: date) -> Optional[datetime]:
    local = datetime.combine(day, time.min, tzinfo=ET)
    minute = market_close_minute(local)
    return None if minute is None else local + timedelta(minutes=minute)


def _decision_bars(stock: list, ev: Event, option_times=()):
    """Observation/fill cycles in the regular session. The closing bar can price
    an existing exit; no new decision is sent once that session has closed."""
    for when in sorted({at for at, _ in stock} | set(option_times)):
        local = _et(when)
        if when <= ev.entry or local.date() > ev.expiry:
            continue
        close = _session_close(local.date())
        if close is not None and OPEN_SETTLE <= local.time() and local <= close:
            yield when, local, price_at(stock, when)


def expiry_close(stock: list, ev: Event, *, now: Optional[datetime] = None) -> Optional[float]:
    close = _session_close(ev.expiry)
    if close is None or not settlement_ready(ev.expiry.isoformat(), now=now):
        return None
    for when, px in stock:
        if when == close and math.isfinite(px) and px > 0:
            return px
    return None                                      # a morning/stale bar is not a closing bar


def replay(ev: Event, stock: list, legs: dict, *, now: Optional[datetime] = None) -> dict:
    """Each rule's (pnl per spread, exit time ET, reason), or None for no data."""
    out = {}
    now = now or datetime.now(timezone.utc)
    stock = sorted((at, px) for at, px in stock if at <= now)
    legs = {symbol: sorted((at, px) for at, px in series if at <= now)
            for symbol, series in legs.items()}
    ev.unpriced_exits.clear()
    settle = expiry_close(stock, ev, now=now)
    out["hold"] = None if settle is None else (pnl(ev, intrinsic(ev, settle)), f"{ev.expiry} close", "expiry")
    if settle is None:
        ev.unpriced_exits["hold"] = {"reason": "completed expiry close unavailable", "trigger_et": None}

    target = (1 - 0.5) * ev.credit
    must_exit_day = None
    if ev.earnings_date is not None:
        must_exit_day = ev.earnings_date + timedelta(days=1)

    cycles = list(_decision_bars(stock, ev, (at for series in legs.values() for at, _ in series)))
    for rule in ("policy", "breach stop", "loss stop"):
        waiting_for = None                       # a decision taken, waiting for a priced bar
        decided_at = None
        result = None
        for when, local, spot in cycles:
            if decided_at is not None:
                fill = debit_at(ev, legs, when, after=decided_at)
                if (fill is not None and 0 <= fill <= ev.width
                        and (waiting_for != "profit target" or fill <= target)):
                    result = (pnl(ev, max(fill, 0.0)), local.strftime("%m-%d %H:%M"), waiting_for)
                    break
            close = _session_close(local.date())
            if local >= close or waiting_for not in (None, "profit target"):
                continue
            reason = None
            if rule == "breach stop" and spot is not None and (spot < ev.short_put or spot > ev.short_call):
                reason = "breach stop"
            elif must_exit_day is not None and local.date() >= must_exit_day:
                reason = "after the print"
            elif local.date() == ev.expiry and local >= min(
                    local.replace(hour=FORCED_CLOSE_MINUTE // 60, minute=0), close - timedelta(hours=1)):
                reason = "expiry deadline"
            debit = debit_at(ev, legs, when)
            if (reason is None and rule == "loss stop" and debit is not None
                    and (debit - ev.credit) * 100 >= 0.5 * ev.max_loss):
                reason = "loss stop"
            if reason is None and waiting_for is None and debit is not None and debit <= target:
                reason = "profit target"
            if reason is not None and reason != waiting_for:
                waiting_for, decided_at = reason, when
        if result is None:
            ev.unpriced_exits[rule] = {
                "reason": waiting_for or "insufficient price history",
                "trigger_et": _et(decided_at).isoformat() if decided_at else None}
        out[rule] = result
    return out


# ── Loading ──────────────────────────────────────────────────────────────────
def _legs_from_strikes(ticker: str, expiry: date, sp: float, lp: float, sc: float, lc: float) -> list:
    return [{"symbol": occ(ticker, expiry, "C", sc), "side": "sell", "strike": sc, "right": "C"},
            {"symbol": occ(ticker, expiry, "C", lc), "side": "buy", "strike": lc, "right": "C"},
            {"symbol": occ(ticker, expiry, "P", sp), "side": "sell", "strike": sp, "right": "P"},
            {"symbol": occ(ticker, expiry, "P", lp), "side": "buy", "strike": lp, "right": "P"}]


def load_events(con: sqlite3.Connection) -> list:
    con.row_factory = sqlite3.Row
    events = []
    for r in con.execute("SELECT * FROM iv_condors WHERE status='closed' ORDER BY id"):
        expiry = date.fromisoformat(r["expiry"])
        sp, lp, sc, lc = (float(r[k]) for k in ("short_put", "long_put", "short_call", "long_call"))
        legs = _legs_from_strikes(r["ticker"], expiry, sp, lp, sc, lc)
        stored = {l["symbol"]: l["side"] for l in json.loads(r["legs_json"])}
        if set(stored) != {l["symbol"] for l in legs}:
            continue                                  # unusual strikes: skip rather than guess
        events.append(Event(
            kind="trade", ident=f"condor #{r['id']}", ticker=r["ticker"], qty=int(r["qty"]),
            credit=float(r["credit"]), fees=0.0, legs=legs, short_put=sp, short_call=sc,
            width=max(lc - sc, sp - lp), entry=_utc(r["opened_at"]),
            earnings_date=date.fromisoformat(r["earnings_date"]) if r["earnings_date"] else None,
            expiry=expiry, recorded_pnl=float(r["pnl"]) / int(r["qty"]) if r["pnl"] is not None else None,
            cohort="bot trades"))
    for r in con.execute(
            """SELECT * FROM iv_variant_evals WHERE resolved=1 AND variant LIKE 'condor%'
               AND pricing_model='conservative_bid_ask_v2' AND long_put IS NOT NULL AND long_call IS NOT NULL
               ORDER BY expiry, id"""):
        expiry = date.fromisoformat(r["expiry"])
        sp, lp, sc, lc = (float(r[k]) for k in ("short_put", "long_put", "short_call", "long_call"))
        rec = r["recommendation"] or "unknown gates"
        events.append(Event(
            kind="research", ident=f"{r['variant']} #{r['id']}", ticker=r["ticker"], qty=1,
            credit=float(r["credit"]), fees=float(r["fees"] or 0), legs=_legs_from_strikes(
                r["ticker"], expiry, sp, lp, sc, lc), short_put=sp, short_call=sc,
            width=max(lc - sc, sp - lp), entry=_utc(r["created_at"]),
            earnings_date=date.fromisoformat(r["earnings_date"]) if r["earnings_date"] else None,
            expiry=expiry, recorded_pnl=float(r["realized_pnl"]) if r["realized_pnl"] is not None else None,
            cohort=f"research ({'passed gates' if rec == 'SELL_PREMIUM' else rec.lower()})"))
    return events


def load_market_events(con: sqlite3.Connection) -> list:
    try:
        return [dict(r) for r in con.execute("SELECT date, label FROM market_events ORDER BY date")]
    except sqlite3.OperationalError:
        return []                                     # a ledger from before the table existed


def spans_event(ev: Event, market_events: list) -> bool:
    start, end = _et(ev.entry).date().isoformat(), ev.expiry.isoformat()
    return any(start <= m["date"] <= end for m in market_events)


# ── Summary ──────────────────────────────────────────────────────────────────
def summarise(events: list, market_events: list) -> dict:
    out = {}
    cohorts = sorted({e.cohort for e in events})
    scopes = ("all", "excluding market-event days") if market_events else ("all",)
    for cohort in cohorts:
        for scope in scopes:
            group = [e for e in events if e.cohort == cohort
                     and (scope == "all" or not spans_event(e, market_events))]
            if not group:
                continue
            rows = {}
            for rule in RULES:
                vals = [e.results[rule][0] for e in group if e.results.get(rule)]
                rows[rule] = {
                    "n": len(vals), "no_data": len(group) - len(vals),
                    "total_per_spread": round(sum(vals), 2) if vals else None,
                    "avg_per_spread": round(sum(vals) / len(vals), 2) if vals else None,
                    "win_rate": round(sum(v > 0 for v in vals) / len(vals) * 100, 1) if vals else None,
                    "worst": round(min(vals), 2) if vals else None,
                }
            out[f"{cohort} — {scope}"] = {"events": len(group), "rules": rows}
    return out


def run(con: sqlite3.Connection, data, now: Optional[datetime] = None) -> dict:
    con.row_factory = sqlite3.Row
    market_events = load_market_events(con)
    events = load_events(con)
    now = now or datetime.now(timezone.utc)
    latest = now - RECENT_DATA_LAG
    for ev in events:
        start = ev.entry - BAR_INTERVAL
        close = _session_close(ev.expiry)
        if close is None or start >= latest:
            ev.results = {}
            continue
        end = min(close.astimezone(timezone.utc), latest)
        stock = data.stock(ev.ticker, start, end)
        legs = data.options([l["symbol"] for l in ev.legs], start, end)
        if stock is None:
            ev.results = {}
        else:
            ev.results = replay(ev, [(at, px) for at, px in stock if at <= end],
                                {symbol: [(at, px) for at, px in series if at <= end]
                                 for symbol, series in (legs or {}).items()}, now=now)
    return {"generated": now.isoformat(), "market_events": market_events,
            "rules": RULES,
            "events": [{"ident": e.ident, "ticker": e.ticker, "cohort": e.cohort, "qty": e.qty,
                        "credit": e.credit, "max_loss_per_spread": round(e.max_loss, 2),
                        "entry_et": _et(e.entry).strftime("%Y-%m-%d %H:%M"), "expiry": e.expiry.isoformat(),
                        "earnings_date": e.earnings_date.isoformat() if e.earnings_date else None,
                        "spans_market_event": spans_event(e, market_events),
                        "recorded_pnl_per_spread": e.recorded_pnl,
                        "no_data_reasons": e.unpriced_exits,
                        "results": {rule: (None if not e.results.get(rule) else
                                           {"pnl_per_spread": e.results[rule][0], "exit": e.results[rule][1],
                                            "reason": e.results[rule][2]}) for rule in RULES}}
                       for e in events],
            "summary": summarise(events, market_events)}


def render(report: dict) -> str:
    lines = [f"Stop what-if study — {report['generated'][:16]} UTC",
             "Diagnostic trade-bar marks; hypothetical later-bar fills, not executable quotes.",
             "Breaches use completed-bar closes. Missing exits stay unknown; compare matched events.",
             "Tagged market-event days: " + (", ".join(f"{m['date']} ({m['label']})" for m in report["market_events"])
                                              or "none"), ""]
    for e in report["events"]:
        lines.append(f"{e['cohort']:32s} {e['ticker']:5s} {e['ident']:20s} entry {e['entry_et']}  exp {e['expiry']}"
                     + ("  [spans a tagged day]" if e["spans_market_event"] else ""))
        for rule in RULES:
            r = e["results"][rule]
            missing = (e.get("no_data_reasons") or {}).get(rule) or {}
            why = f" ({missing['reason']}, signal {missing.get('trigger_et') or 'unknown'})" if missing else ""
            lines.append(f"      {rule:12s} " + ("no data" + why if r is None else
                                                f"{r['pnl_per_spread']:+9.2f} per spread  ({r['reason']}, {r['exit']})"))
        if e["recorded_pnl_per_spread"] is not None:
            lines.append(f"      {'recorded':12s} {e['recorded_pnl_per_spread']:+9.2f} per spread")
    lines.append("")
    for name, block in report["summary"].items():
        lines.append(f"{name}: {block['events']} event(s)")
        for rule, s in block["rules"].items():
            lines.append(f"      {rule:12s} n={s['n']:<3d} total {s['total_per_spread'] if s['total_per_spread'] is not None else '—':>9} "
                         f"avg {s['avg_per_spread'] if s['avg_per_spread'] is not None else '—':>8} "
                         f"win {s['win_rate'] if s['win_rate'] is not None else '—':>5}% "
                         f"worst {s['worst'] if s['worst'] is not None else '—':>8}"
                         + (f"  ({s['no_data']} without data)" if s["no_data"] else ""))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None, help="ledger path (default: the configured one)")
    parser.add_argument("--json", default=None, help="also write the full result here")
    args = parser.parse_args(argv)
    from config import get_settings
    from db import resolve_db_path
    from trading.alpaca_trader import AlpacaTrader
    s = get_settings()
    path = Path(args.db) if args.db else resolve_db_path(s.db_path)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)     # read-only: this never writes
    try:
        trader = AlpacaTrader(s.alpaca_api_key, s.alpaca_secret_key, paper=s.alpaca_paper,
                              options_feed=s.alpaca_options_feed)
        report = run(con, AlpacaBars(trader))
    finally:
        con.close()
    print(render(report))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

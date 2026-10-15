"""The stop what-if study replays condors under each exit rule. Fake price
history and temporary ledgers; nothing is fetched or traded."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from backtest import stop_study as study
from db import Database
from market_time import ET

EXPIRY = "2026-11-27"            # a Friday
EARNINGS = "2026-11-24"          # Tuesday: the policy must exit on Wednesday from 09:45
SP, LP, SC, LC = 90.0, 85.0, 110.0, 115.0


def et(day, hh, mm):
    return datetime(2026, 11, day, hh, mm, tzinfo=ET).astimezone(timezone.utc)


def legs():
    return {l["symbol"]: l for l in study._legs_from_strikes("TEST", datetime(2026, 11, 27).date(), SP, LP, SC, LC)}


def session_bars(path):
    """One bar every 30 min, 09:30–15:30 ET, Mon 23rd to Fri 27th: {(day, hh, mm): value}."""
    out = []
    for day in (23, 24, 25, 26, 27):
        for hh, mm in ((9, 30), (9, 45), (10, 0), (11, 0), (12, 0), (13, 0), (14, 0), (15, 0), (15, 30), (15, 55)):
            out.append(((day, hh, mm), path(day, hh, mm)))
    return out


class Fake:
    def __init__(self, stock_path, option_price, missing=()):
        self.stock_series = [(et(*k), v) for k, v in session_bars(stock_path)]
        self.option_price, self.missing = option_price, set(missing)

    def stock(self, symbol, start, end):
        return self.stock_series

    def options(self, symbols, start, end):
        out = {}
        for symbol in symbols:
            leg = legs()[symbol]
            out[symbol] = [] if symbol in self.missing else [
                (when, self.option_price(leg, spot, when)) for when, spot in self.stock_series]
        return out


def toy_price(leg, spot, when, decay=1.0):
    """Intrinsic value, plus time value on the short legs that fades to nothing
    at expiry: the condor is worth about its $1.00 credit at entry. `decay` > 1
    makes the time value fade faster, so the profit target comes early."""
    days_left = max((datetime(2026, 11, 27, 16, tzinfo=ET) - when.astimezone(ET)).total_seconds() / 86400, 0)
    k = leg["strike"]
    value = max(spot - k, 0) if leg["right"] == "C" else max(k - spot, 0)
    time_value = 0.5 * (days_left / 4.25) ** decay if leg["side"] == "sell" else 0.0
    return round(value + time_value, 4)


def fast_decay(leg, spot, when):
    return toy_price(leg, spot, when, decay=2.0)


async def ledger(tmp_path, *, research=True, trade=True):
    db = Database(tmp_path / "study.db")
    await db.connect()
    if trade:
        legs_json = json.dumps([{"symbol": s, "side": l["side"], "ratio_qty": 1} for s, l in legs().items()])
        cid = await db.record_condor("TEST", EARNINGS, EXPIRY, legs_json,
                                     {"short_put": SP, "long_put": LP, "short_call": SC, "long_call": LC},
                                     4, 1.0, 400, "entry", "filled")
        await db.activate_condor(cid, 4, -1.0)
        await db.close_condor(cid, 0.40, 240.0)
        await db._exec("UPDATE iv_condors SET opened_at=? WHERE id=?", ("2026-11-23T15:00:00", cid), strict=True)
    if research:
        await db._exec(
            """INSERT INTO iv_variant_evals (ticker, earnings_date, signal_date, expiry, variant, credit, fees,
                   short_put, long_put, short_call, long_call, pricing_model, max_loss, recommendation, source,
                   resolve_after, resolved, realized_pnl, win, created_at)
               VALUES ('TEST',?, '2026-11-23', ?, 'condor_1.0sd', 1.0, 2.6, ?,?,?,?, 'conservative_bid_ask_v2',
                       402.6, 'SELL_PREMIUM', 'watchlist', ?, 1, 97.4, 1, '2026-11-23T15:00:00')""",
            (EARNINGS, EXPIRY, SP, LP, SC, LC, EXPIRY), strict=True)
    await db.close()
    con = sqlite3.connect(f"file:{tmp_path / 'study.db'}?mode=ro", uri=True)
    return con


def run(con, fake):
    return study.run(con, fake)


def by_ident(report, cohort_prefix):
    return next(e for e in report["events"] if e["cohort"].startswith(cohort_prefix))


# ── Rules ───────────────────────────────────────────────────────────────────
async def test_a_quiet_stock_takes_profit_and_no_stop_ever_fires(tmp_path):
    con = await ledger(tmp_path, trade=False)
    report = run(con, Fake(lambda d, h, m: 100.0, fast_decay))
    r = by_ident(report, "research")["results"]
    assert r["policy"]["reason"] == "profit target" and r["policy"]["exit"].startswith("11-24")
    assert r["breach stop"] == r["policy"] and r["loss stop"] == r["policy"]
    assert r["hold"]["pnl_per_spread"] == pytest.approx(100 - 2.6)          # full credit, less fees


async def test_a_dip_through_the_short_put_that_recovers_is_sold_at_the_bottom_by_the_breach_stop(tmp_path):
    """MS and WFC on 2026-10-14: through the short put, then back above it."""
    def dip(day, hh, mm):
        return 86.0 if (day == 24 and hh in (10, 11)) else 100.0
    con = await ledger(tmp_path, trade=False)
    r = by_ident(run(con, Fake(dip, toy_price)), "research")["results"]
    assert r["breach stop"]["reason"] == "breach stop" and r["breach stop"]["exit"] == "11-24 10:00"
    assert r["breach stop"]["pnl_per_spread"] < r["policy"]["pnl_per_spread"] < r["hold"]["pnl_per_spread"] + 1e-9


async def test_a_collapse_that_keeps_going_is_cut_short_by_the_stops(tmp_path):
    def collapse(day, hh, mm):
        return 100.0 if day == 23 else (89.0 if (day == 24 and hh < 12) else 80.0)
    con = await ledger(tmp_path, trade=False)
    r = by_ident(run(con, Fake(collapse, toy_price)), "research")["results"]
    assert r["hold"]["pnl_per_spread"] == pytest.approx((1.0 - 5.0) * 100 - 2.6)   # the full max loss
    assert r["breach stop"]["exit"] == "11-24 09:45"
    assert r["breach stop"]["pnl_per_spread"] > r["hold"]["pnl_per_spread"]
    assert r["loss stop"]["reason"] in ("loss stop", "breach stop", "after the print")


async def test_the_loss_stop_fires_at_half_the_maximum_loss(tmp_path):
    def slide(day, hh, mm):
        return 100.0 if day == 23 else 86.0
    con = await ledger(tmp_path, trade=False)
    r = by_ident(run(con, Fake(slide, toy_price)), "research")["results"]
    assert (r["loss stop"]["reason"], r["loss stop"]["exit"]) == ("loss stop", "11-24 09:45")
    assert r["loss stop"]["pnl_per_spread"] <= -(400 * 0.5)                  # at least half the max loss
    assert r["policy"]["reason"] == "after the print"                         # without the stop: Wednesday


async def test_nothing_acts_before_09_45(tmp_path):
    def opening_dip(day, hh, mm):
        return 85.0 if (day == 24 and (hh, mm) == (9, 30)) else 100.0
    con = await ledger(tmp_path, trade=False)
    r = by_ident(run(con, Fake(opening_dip, toy_price)), "research")["results"]
    assert r["breach stop"]["reason"] != "breach stop"                       # the 09:30 bar is never acted on


async def test_after_the_print_the_policy_exits_the_next_morning(tmp_path):
    def drift(day, hh, mm):
        return 100.0 if day < 25 else 104.0
    con = await ledger(tmp_path, trade=False)
    expensive = lambda leg, spot, when: toy_price(leg, spot, when) + (0.6 if leg["side"] == "sell" else 0)
    r = by_ident(run(con, Fake(drift, expensive)), "research")["results"]
    assert (r["policy"]["reason"], r["policy"]["exit"]) == ("after the print", "11-25 09:45")


# ── Data gaps ───────────────────────────────────────────────────────────────
async def test_a_leg_without_trades_is_reported_not_guessed(tmp_path):
    con = await ledger(tmp_path, trade=False)
    missing = [s for s, l in legs().items() if l["side"] == "buy" and l["right"] == "P"]
    r = by_ident(run(con, Fake(lambda d, h, m: 100.0, toy_price, missing=missing)), "research")["results"]
    assert r["hold"] is not None                                              # settles off the stock alone
    assert r["policy"]["reason"] == "held to expiry (no priced exit)"


async def test_no_price_history_at_all_is_no_data(tmp_path):
    con = await ledger(tmp_path, trade=False)

    class Nothing:
        def stock(self, *a):
            return None

        def options(self, *a):
            return None
    report = run(con, Nothing())
    assert all(v is None for v in by_ident(report, "research")["results"].values())
    block = next(iter(report["summary"].values()))
    assert block["rules"]["hold"]["n"] == 0 and block["rules"]["hold"]["no_data"] == 1


def test_a_stale_trade_is_not_a_price():
    t0 = et(24, 10, 0)
    series = [(t0, 1.0)]
    assert study.price_at(series, t0 + timedelta(minutes=59)) == 1.0
    assert study.price_at(series, t0 + timedelta(minutes=61)) is None
    assert study.price_at(series, t0 - timedelta(minutes=1)) is None


# ── Ledger, events, summary ─────────────────────────────────────────────────
async def test_the_bots_own_trades_and_research_are_separate_cohorts(tmp_path):
    con = await ledger(tmp_path)
    report = run(con, Fake(lambda d, h, m: 100.0, toy_price))
    cohorts = {e["cohort"] for e in report["events"]}
    assert cohorts == {"bot trades", "research (passed gates)"}
    trade = by_ident(report, "bot")
    assert trade["recorded_pnl_per_spread"] == 60.0 and trade["qty"] == 4      # $240 over 4 spreads


async def test_events_open_on_a_tagged_day_are_flagged_and_summarised_both_ways(tmp_path):
    db = Database(tmp_path / "study.db")
    await db.connect()
    await db.add_market_event("2026-11-25", "CPI")
    await db.close()
    con = await ledger(tmp_path)
    report = run(con, Fake(lambda d, h, m: 100.0, toy_price))
    assert all(e["spans_market_event"] for e in report["events"])
    assert any(k.endswith("— all") for k in report["summary"])
    assert not any(k.endswith("excluding market-event days") for k in report["summary"])   # nothing left


async def test_a_ledger_from_before_market_events_still_runs(tmp_path):
    con = await ledger(tmp_path)
    con.close()
    rw = sqlite3.connect(tmp_path / "study.db")
    rw.execute("DROP TABLE market_events")
    rw.commit()
    rw.close()
    con = sqlite3.connect(f"file:{tmp_path / 'study.db'}?mode=ro", uri=True)
    assert run(con, Fake(lambda d, h, m: 100.0, toy_price))["market_events"] == []


async def test_the_study_cannot_write_to_the_ledger(tmp_path):
    con = await ledger(tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM iv_condors")


def test_the_report_text_names_every_rule_and_the_sample_size(tmp_path):
    report = {"generated": "2026-11-28T00:00:00", "market_events": [], "events": [], "summary": {
        "research (passed gates) — all": {"events": 2, "rules": {rule: {
            "n": 2, "no_data": 0, "total_per_spread": 10.0, "avg_per_spread": 5.0, "win_rate": 50.0, "worst": -5.0}
            for rule in study.RULES}}}}
    text = study.render(report)
    assert all(rule in text for rule in study.RULES) and "2 event(s)" in text


# ── Price history over the broker's data API ────────────────────────────────
def test_bars_are_paged_and_parsed():
    calls = []
    pages = [
        (200, {"bars": {"TEST": [{"t": "2026-11-24T15:00:00Z", "c": 100.0}]}, "next_page_token": "p2"}),
        (200, {"bars": {"TEST": [{"t": "2026-11-24T14:55:00.123456789Z", "c": 99.5}]}, "next_page_token": None})]

    class Trader:
        _data_base = "https://data.example"

        def _rest(self, method, url):
            calls.append((method, url))
            return pages[len(calls) - 1]
    series = study.AlpacaBars(Trader()).stock("TEST", et(24, 9, 30), et(24, 16, 0))
    assert [px for _, px in series] == [99.5, 100.0]                          # sorted by time
    assert all(m == "GET" for m, _ in calls) and "page_token=p2" in calls[1][1]


def test_a_failed_page_is_no_data_not_an_empty_history():
    class Trader:
        def _rest(self, method, url):
            return 500, {"message": "down"}
    assert study.AlpacaBars(Trader()).options(["A"], et(24, 9, 30), et(24, 16, 0)) is None


async def test_history_is_never_requested_closer_than_the_data_plan_allows(tmp_path):
    """The plan answers 403 for history ending in the last 15 minutes, which
    is what an expiry still in the future asks for."""
    con = await ledger(tmp_path, trade=False)
    asked = []

    class Recording(Fake):
        def stock(self, symbol, start, end):
            asked.append(end)
            return super().stock(symbol, start, end)

        def options(self, symbols, start, end):
            asked.append(end)
            return super().options(symbols, start, end)
    now = et(25, 12, 0)                                                     # before the Friday expiry
    study.run(con, Recording(lambda d, h, m: 100.0, toy_price), now=now)
    assert asked and all(end <= now - timedelta(minutes=15) for end in asked)


async def test_without_tagged_days_there_is_no_separate_excluding_summary(tmp_path):
    con = await ledger(tmp_path)
    report = run(con, Fake(lambda d, h, m: 100.0, toy_price))
    assert report["summary"] and not any("excluding" in k for k in report["summary"])

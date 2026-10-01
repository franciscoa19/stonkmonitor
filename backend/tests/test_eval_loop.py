from datetime import date as _dtdate, datetime as _dtdatetime
import json
from types import SimpleNamespace
"""
Mock-data tests for the paper-trading eval loop.

No Alpaca / UW / network — a temp SQLite DB is seeded with synthetic fills and
a FakeTrader stands in for the broker, so the P&L math, strategy attribution,
and daily-report metrics are all verified deterministically.

Run:  cd backend && ./venv/bin/python -m pytest -q
"""
import os
import tempfile
import pathlib
from datetime import datetime, timezone
import pytest
import pytest_asyncio

from db import Database, _is_occ, _et_hour, _minutes_between
from daily_report import build_report_data, build_watchlist_review, export_history, render_html
from market_time import et_today

URI = "URI260918C01050000"      # OCC option symbols (×100 multiplier)
DXCM = "DXCM260918P00090000"
WIN = "ABC260918C00100000"


# ── Fixtures / helpers ──────────────────────────────────────────────────
@pytest_asyncio.fixture
async def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    d = Database(path=pathlib.Path(path))
    await d.connect()
    try:
        yield d
    finally:
        await d.close()
        os.unlink(path)


class FakeTrader:
    """Stand-in broker: no network, returns canned account/positions."""
    def __init__(self, equity=50000.0, cash=50000.0, positions=None):
        self._e, self._c, self._p = equity, cash, positions or []

    def get_account(self):
        return {"equity": self._e, "cash": self._c, "buying_power": self._e * 4}

    def get_positions(self):
        return self._p


async def seed_entry(db, order_id, symbol, ticker, price, qty, strategy="",
                     trade_type="option", submitted_at="2026-09-03T14:30:00Z"):
    """Seed a filled BUY entry, attributed via a matching pending_trade."""
    if strategy:
        await db.save_pending_trade(
            expires_at="2026-09-03T14:35:00Z", ticker=ticker, trade_type=trade_type,
            symbol=symbol, side="bullish", qty=qty, limit_price=price,
            risk_amount=price * qty * (100 if _is_occ(symbol) else 1),
            score=10.0, strategy=strategy,
        )
        # link the freshly-inserted pending row to this order id

        rows = await db._query("SELECT id FROM pending_trades WHERE symbol=? ORDER BY id DESC LIMIT 1", (symbol,))
        await db.update_pending_trade(rows[0]["id"], alpaca_order_id=order_id)
    await db.upsert_trade_performance(
        alpaca_order_id=order_id, symbol=symbol, ticker=ticker, side="buy",
        qty=qty, filled_qty=qty, filled_avg_price=price, order_type="limit",
        order_status="filled", submitted_at=submitted_at, filled_at=submitted_at,
        trade_type=trade_type, signal_score=10.0,
    )


async def seed_exit(db, order_id, symbol, ticker, price, qty,
                    filled_at="2026-09-03T15:30:00Z", trade_type="option"):
    await db.upsert_trade_performance(
        alpaca_order_id=order_id, symbol=symbol, ticker=ticker, side="sell",
        qty=qty, filled_qty=qty, filled_avg_price=price, order_type="limit",
        order_status="filled", submitted_at=filled_at, filled_at=filled_at,
        trade_type=trade_type,
    )


# ── Pure helpers ────────────────────────────────────────────────────────
def test_is_occ():
    assert _is_occ("URI260918C01050000") is True
    assert _is_occ("DXCM260918P00090000") is True
    assert _is_occ("AAPL") is False
    assert _is_occ("SPY") is False
    assert _is_occ("") is False
    assert _is_occ(None) is False


def test_et_hour():
    # 14:30 UTC = 10:30 ET (EDT, summer)
    assert _et_hour("2026-09-03T14:30:00Z") == 10
    assert _et_hour("2026-09-03T20:00:00+00:00") == 16
    assert _et_hour("") is None
    assert _et_hour("garbage") is None


def test_minutes_between():
    assert _minutes_between("2026-09-03T14:30:00Z", "2026-09-03T15:30:00Z") == 60.0
    assert _minutes_between("2026-09-03T14:30:00Z", "2026-09-03T14:45:30Z") == 15.5
    assert _minutes_between("bad", "worse") is None


# ── Realized-P&L reconciliation ─────────────────────────────────────────
async def test_reconcile_option_loss(db):
    # URI call: buy 1 @ 8.50, sell 1 @ 5.50 → (5.5-8.5)*1*100 = -300
    await seed_entry(db, "b1", URI, "URI", 8.50, 1, strategy="triple_confluence")
    await seed_exit(db, "s1", URI, "URI", 5.50, 1)
    n = await db.reconcile_trades()
    assert n == 1
    row = (await db._query("SELECT * FROM trade_performance WHERE symbol=? AND side='buy'", (URI,)))[0]
    assert row["realized_pnl"] == -300.0
    assert row["realized_pnl_pct"] == pytest.approx(-35.29, abs=0.01)
    assert row["exit_reason"] == "closed_loss"
    assert row["strategy"] == "triple_confluence"   # attribution survived
    assert row["hold_minutes"] == 60.0


async def test_reconcile_option_win(db):
    # buy 2 @ 1.00, sell 2 @ 2.00 → (2-1)*2*100 = +200
    await seed_entry(db, "b2", WIN, "ABC", 1.00, 2, strategy="golden_sweep")
    await seed_exit(db, "s2", WIN, "ABC", 2.00, 2)
    await db.reconcile_trades()
    row = (await db._query("SELECT * FROM trade_performance WHERE symbol=? AND side='buy'", (WIN,)))[0]
    assert row["realized_pnl"] == 200.0
    assert row["exit_reason"] == "closed_win"


async def test_reconcile_partial_exit(db):
    # buy 3 @ 2.55, sell only 1 @ 1.60 → realized on the 1 sold: (1.6-2.55)*1*100 = -95
    await seed_entry(db, "b3", DXCM, "DXCM", 2.55, 3, strategy="triple_confluence")
    await seed_exit(db, "s3", DXCM, "DXCM", 1.60, 1)
    await db.reconcile_trades()
    row = (await db._query("SELECT * FROM trade_performance WHERE symbol=? AND side='buy'", (DXCM,)))[0]
    assert row["realized_pnl"] == -95.0


async def test_reconcile_equity_multiplier(db):
    # equity (non-OCC) uses ×1: buy 10 @ 100, sell 10 @ 110 → +100
    await seed_entry(db, "b4", "AAPL", "AAPL", 100.0, 10, strategy="insider_buy", trade_type="equity")
    await seed_exit(db, "s4", "AAPL", "AAPL", 110.0, 10, trade_type="equity")
    await db.reconcile_trades()
    row = (await db._query("SELECT * FROM trade_performance WHERE symbol='AAPL' AND side='buy'"))[0]
    assert row["realized_pnl"] == 100.0


async def test_reconcile_idempotent(db):
    await seed_entry(db, "b5", URI, "URI", 8.50, 1, strategy="triple_confluence")
    await seed_exit(db, "s5", URI, "URI", 5.50, 1)
    await db.reconcile_trades()
    await db.reconcile_trades()   # second run must not double-count
    row = (await db._query("SELECT * FROM trade_performance WHERE symbol=? AND side='buy'", (URI,)))[0]
    assert row["realized_pnl"] == -300.0


async def test_open_trade_not_booked(db):
    # entry with no exit → no realized P&L
    await seed_entry(db, "b6", WIN, "ABC", 1.00, 1, strategy="sweep")
    n = await db.reconcile_trades()
    assert n == 0
    row = (await db._query("SELECT * FROM trade_performance WHERE symbol=? AND side='buy'", (WIN,)))[0]
    assert row["realized_pnl"] is None


# ── Attribution join ────────────────────────────────────────────────────
async def test_attribution_and_entry_hour(db):
    await seed_entry(db, "b7", URI, "URI", 8.50, 1, strategy="triple_confluence",
                     submitted_at="2026-09-03T14:30:00Z")
    row = (await db._query("SELECT * FROM trade_performance WHERE symbol=? AND side='buy'", (URI,)))[0]
    assert row["strategy"] == "triple_confluence"
    assert row["entry_hour_et"] == 10   # 14:30 UTC → 10:30 ET


# ── Equity baseline (immutable open) ────────────────────────────────────
async def test_daily_equity_open_is_immutable(db):
    await db.record_daily_equity("2026-09-03", 50000.0)        # first snapshot = open
    await db.record_daily_equity("2026-09-03", 49414.85)       # intraday update
    rows = await db.get_daily_equity(5)
    assert rows[0]["open_equity"] == 50000.0   # open preserved
    assert rows[0]["equity"] == 49414.85       # latest moved


# ── End-to-end daily report ─────────────────────────────────────────────
async def test_build_report_metrics(db):
    # one win (+200) and one loss (-300); start 50k, now 49.9k
    await seed_entry(db, "b8", WIN, "ABC", 1.00, 2, strategy="golden_sweep")
    await seed_exit(db, "s8", WIN, "ABC", 2.00, 2)
    await seed_entry(db, "b9", URI, "URI", 8.50, 1, strategy="triple_confluence")
    await seed_exit(db, "s9", URI, "URI", 5.50, 1)
    await db.reconcile_trades()
    await db.record_daily_equity("2026-09-03", 50000.0)
    await db.record_daily_equity("2026-09-03", 49900.0)

    trader = FakeTrader(equity=49900.0, cash=49900.0)
    d = await build_report_data(db, trader)

    m = d["metrics"]
    assert m["closed_trades"] == 2
    assert m["wins"] == 1 and m["losses"] == 1
    assert m["win_rate"] == 50.0
    assert m["realized_total"] == -100.0            # +200 - 300
    assert m["profit_factor"] == pytest.approx(200 / 300, abs=0.001)
    assert d["account"]["start_equity"] == 50000.0  # from open_equity, not clobbered
    assert d["account"]["total_pnl"] == pytest.approx(-100.0, abs=0.01)
    # per-strategy attribution present for both setups
    strats = {s["strategy"]: s for s in d["by_strategy"]}
    assert "golden_sweep" in strats and "triple_confluence" in strats
    assert strats["golden_sweep"]["pnl"] == 200.0
    assert strats["triple_confluence"]["pnl"] == -300.0
    assert d["iv_condors"]["closed"] == 0


async def test_daily_report_surfaces_empty_variant_sample_rail(db):
    """The evidence requirement remains visible before any event resolves."""
    report = render_html(await build_report_data(db, FakeTrader()))
    assert "Forward-test sample rail." in report
    assert "Gated: 0 resolved / 100 more needed" in report
    assert "ungated: 0 resolved / 100 more needed" in report


# ── Weekly watchlist review ─────────────────────────────────────────────
async def test_watchlist_review_add_and_remove(db):
    now = datetime.now(timezone.utc).isoformat()
    # TSM (not watchlisted) generates lots of signals → should be proposed as ADD
    for i in range(10):
        await db._exec(
            "INSERT INTO signals (type,ticker,score,side,title,description,raw,created_at) "
            "VALUES ('sweep','TSM',9.0,'bullish','t','d','{}',?)", (now,))
    # SPY watchlisted + has signals (active) → NOT removed; GLD watchlisted + silent → REMOVE?
    await db._exec(
        "INSERT INTO signals (type,ticker,score,side,title,description,raw,created_at) "
        "VALUES ('iv_high','SPY',8.0,'neutral','t','d','{}',?)", (now,))

    props = await build_watchlist_review(db, ["SPY", "GLD"], add_threshold=8)
    text = " | ".join(props)
    assert "ADD: TSM" in text
    assert "REMOVE?: GLD" in text
    assert "SPY" not in text          # active → not flagged for removal


async def test_watchlist_review_skips_recently_added(db):
    # A silent name added TODAY (within the window) must NOT be flagged for removal.
    await db.add_watchlist("GLD")     # added_at = now
    props = await build_watchlist_review(db, ["GLD"], lookback_days=14)
    assert not any("REMOVE?: GLD" in p for p in props)


# ── IV/RV edge validation (hypothetical short-straddle tracking) ────────
async def test_iv_rv_eval_lifecycle(db):
    # dedup: one open eval per ticker
    r1 = await db.record_iv_eval("NVDA", "SELL_PREMIUM", 1.30, 8.0, 100.0, "2026-09-16")
    r2 = await db.record_iv_eval("NVDA", "CONSIDER", 1.10, 6.0, 101.0, "2026-09-16")
    assert r1 == 1 and r2 is None
    await db.record_iv_eval("AAPL", "CONSIDER", 1.05, 5.0, 200.0, "2026-09-16")

    due = await db.get_due_iv_evals("2026-09-20")
    assert len(due) == 2
    nv = next(x for x in due if x["ticker"] == "NVDA")
    ap = next(x for x in due if x["ticker"] == "AAPL")
    # NVDA moved 3% vs 8% implied → seller wins; AAPL moved 9% vs 5% → seller loses
    await db.resolve_iv_eval(nv["id"], 103.0, 3.0, 8.0)
    await db.resolve_iv_eval(ap["id"], 218.0, 9.0, 5.0)

    s = await db.get_iv_eval_summary()
    assert s["resolved"] == 2 and s["wins"] == 1 and s["win_rate"] == 50.0
    assert s["avg_edge_pct"] == round(((8 - 3) + (5 - 9)) / 2, 2)   # (+5, -4) → 0.5
    assert s["open"] == 0
    # already-resolved ticker can open a new eval
    assert await db.record_iv_eval("NVDA", "SELL_PREMIUM", 1.4, 7.0, 105.0, "2026-10-01") == 1


async def test_iv_rv_eval_stores_earnings_date(db):
    # earnings_date (yfinance calendar) is persisted and surfaced when due
    assert await db.record_iv_eval(
        "MSFT", "SELL_PREMIUM", 1.35, 5.0, 400.0,
        resolve_after="2026-09-16", earnings_date="2026-09-14") == 1
    due = await db.get_due_iv_evals("2026-09-20")
    row = next(x for x in due if x["ticker"] == "MSFT")
    assert row["earnings_date"] == "2026-09-14"
    # earnings_date is optional — omitting it stores NULL, still records fine
    assert await db.record_iv_eval(
        "AMZN", "CONSIDER", 1.10, 6.0, 180.0, resolve_after="2026-09-16") == 1
    due2 = await db.get_due_iv_evals("2026-09-20")
    amzn = next(x for x in due2 if x["ticker"] == "AMZN")
    assert amzn["earnings_date"] is None


# ── Earnings sell-premium eligibility filter (scoring cleanup) ──────────
def test_is_sell_eligible():
    from signals.earnings_scanner import is_sell_eligible
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    import types as _t
    now = _dt(2026, 9, 15, 15, tzinfo=_tz.utc)

    def mk(rec="SELL_PREMIUM", ivrv=1.30, days=3):
        ed = (now.date() + _td(days=days)).isoformat() if days is not None else None
        return _t.SimpleNamespace(recommendation=rec, iv30_rv30=ivrv, next_earnings_date=ed)

    assert is_sell_eligible(mk(), 7, now=now) is True                 # near, rich IV, scores
    assert is_sell_eligible(mk(rec="AVOID"), 7, now=now) is False     # doesn't score
    assert is_sell_eligible(mk(ivrv=0.0), 7, now=now) is False        # broken/zero IV/RV
    assert is_sell_eligible(mk(ivrv=float("nan")), 7, now=now) is False
    assert is_sell_eligible(mk(ivrv=float("inf")), 7, now=now) is False
    assert is_sell_eligible(mk(days=None), 7, now=now) is False       # no known earnings (ETF)
    assert is_sell_eligible(mk(days=30), 7, now=now) is False         # earnings too far out
    assert is_sell_eligible(mk(days=-2), 7, now=now) is False         # print already passed
    assert is_sell_eligible(None, 7, now=now) is False                # no setup


# ── Short-premium metric set (VALIDATION_SPEC §4) ───────────────────────
def test_metrics_on_hand_built_fixture():
    from backtest.metrics import compute_metrics
    # 3 wins of +100, 2 losses of -50 and -200.
    m = compute_metrics([100.0, -50.0, 100.0, 100.0, -200.0])
    assert m["n_events"] == 5 and m["n_wins"] == 3
    assert m["win_rate"] == 60.0
    assert m["total_pnl"] == 50.0
    assert m["expectancy"] == 10.0                      # 50 / 5
    assert m["profit_factor"] == 1.2                    # 300 gross win / 250 gross loss
    assert m["avg_win"] == 100.0 and m["avg_loss"] == -125.0
    assert m["largest_single_loss"] == -200.0
    # curve: 100, 50, 150, 250, 50 → peak 250, trough 50 → drawdown 200
    assert m["max_drawdown"] == 200.0
    assert m["tail_events"] == 1 and m["tail_pct_effective"] == 20.0
    assert m["sufficient_sample"] is False              # n=5 is not evidence
    assert m["events_needed"] == 95


def test_tail_ratio_exposes_hidden_negative_expectancy():
    """The whole point of the metric set: a strong win rate hiding a fat tail."""
    from backtest.metrics import compute_metrics
    # 19 wins of +100, one loss of -3000 → 95% win rate, negative expectancy.
    pnls = [100.0] * 19 + [-3000.0]
    m = compute_metrics(pnls)
    assert m["win_rate"] == 95.0                        # looks great
    assert m["expectancy"] < 0                          # ...but loses money
    assert m["profit_factor"] < 1
    # worst 5% (1 event) vs the rest: -3000 / 100 = -30 typical wins erased
    assert m["tail_ratio"] == -30.0
    assert m["tail_events"] == 1 and m["tail_pct_effective"] == 5.0


def test_metrics_empty_and_all_wins():
    from backtest.metrics import compute_metrics
    e = compute_metrics([])
    assert e["n_events"] == 0 and e["profit_factor"] is None and e["max_drawdown"] == 0.0
    assert e["tail_events"] == 0 and e["tail_pct_effective"] is None
    w = compute_metrics([10.0, 20.0])
    assert w["profit_factor"] is None                   # no losses to divide by
    assert w["max_drawdown"] == 0.0 and w["largest_single_loss"] is None


def test_sortino_uses_breakeven_as_the_downside_target():
    from backtest.metrics import compute_metrics
    # One loss must contribute to downside deviation even though it is the only
    # observation below the sample mean. The old sample-stdev implementation
    # returned None here.
    m = compute_metrics([10.0, -10.0, 10.0])
    assert m["sortino"] == 0.58
    # A consistently losing series has a defined negative Sortino, not None.
    assert compute_metrics([-10.0, -10.0])["sortino"] == -1.0


def test_pct_events_exceeding_implied():
    from backtest.metrics import compute_metrics
    m = compute_metrics([10.0, -10.0, 10.0, 10.0], exceeded_implied=[False, True, False, False])
    assert m["pct_events_exceeding_implied"] == 25.0


def test_is_near_earnings_is_weaker_than_sell_eligible():
    """Baseline 2 needs the ungated population: near-earnings regardless of gates."""
    from signals.earnings_scanner import is_near_earnings, is_sell_eligible
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    import types as _t
    now = _dt(2026, 9, 15, 15, tzinfo=_tz.utc)
    avoid = _t.SimpleNamespace(recommendation="AVOID", iv30_rv30=0.6,
                               next_earnings_date=(now.date() + _td(days=2)).isoformat())
    assert is_sell_eligible(avoid, 7, now=now) is False      # gates rejected it
    assert is_near_earnings(avoid, 7, now=now) is True        # ...but it still gets measured
    far = _t.SimpleNamespace(recommendation="AVOID", iv30_rv30=0.6,
                             next_earnings_date=(now.date() + _td(days=40)).isoformat())
    assert is_near_earnings(far, 7, now=now) is False
    etf = _t.SimpleNamespace(recommendation="SELL_PREMIUM", iv30_rv30=1.4,
                             next_earnings_date=None)
    assert is_near_earnings(etf, 7, now=now) is False
    # The ungated baseline must not price an event after a same-day BMO/unknown
    # release. A confirmed after-close release is still valid before 16:00 ET.
    bmo = _t.SimpleNamespace(recommendation="SELL_PREMIUM", iv30_rv30=1.4,
                             next_earnings_date=now.date().isoformat(),
                             earnings_report_time="pre")
    post = _t.SimpleNamespace(recommendation="SELL_PREMIUM", iv30_rv30=1.4,
                              next_earnings_date=now.date().isoformat(),
                              earnings_report_time="post")
    assert is_near_earnings(bmo, 7, now=now) is False
    assert is_sell_eligible(bmo, 7, now=now) is False
    assert is_near_earnings(post, 7, now=_dt(2026, 9, 15, 19, 59, tzinfo=_tz.utc)) is True
    assert is_near_earnings(post, 7, now=_dt(2026, 9, 15, 20, 0, tzinfo=_tz.utc)) is False

    # 03:01 UTC is still 23:01 on 15 Sep in ET. These checks used to disagree
    # whenever the process happened to run with a UTC local timezone.
    utc_boundary = _dt(2026, 9, 16, 3, 1, tzinfo=_tz.utc)
    assert is_near_earnings(bmo, 7, now=utc_boundary) is False
    assert is_sell_eligible(bmo, 7, now=utc_boundary) is False
    assert is_near_earnings(post, 7, now=utc_boundary) is False


def test_market_time_normalizes_a_utc_date_boundary_to_et():
    from market_time import et_today
    from datetime import datetime as _dt, date as _d, timezone as _tz
    assert et_today(_dt(2026, 9, 16, 3, 1, tzinfo=_tz.utc)) == _d(2026, 9, 15)


def test_auto_trade_flow_is_opt_in_by_default():
    from config import Settings
    assert Settings.model_fields["auto_trade_flow_enabled"].default is False


def test_market_cap_parsing():
    from feeds.earnings_calendar import _parse_market_cap
    assert _parse_market_cap("$399,729,623,000") == 399_729_623_000.0
    assert _parse_market_cap("$1,400,530,690") == 1_400_530_690.0
    assert _parse_market_cap("N/A") == 0.0        # unknown cap fails a size filter
    assert _parse_market_cap("") == 0.0
    assert _parse_market_cap(None) == 0.0


def test_measurement_universe_selection():
    """Measurement candidates: near the print first, liquid, watchlist excluded."""
    import feeds.earnings_calendar as cal
    from datetime import timedelta as _td
    import time as _time
    today = et_today()

    def day(n):
        return (today + _td(days=n)).isoformat()

    original, original_ts = cal._cache["map"], cal._cache["ts"]
    cal._cache["map"] = {
        "SOON_BIG":   {"date": day(1), "time": "time-after-hours", "market_cap": 50e9},
        "SOON_SMALL": {"date": day(1), "time": "time-pre-market",   "market_cap": 1e8},
        "LATER_BIG":  {"date": day(3), "time": "time-after-hours", "market_cap": 90e9},
        "FAR":        {"date": day(30), "time": "",                 "market_cap": 90e9},
        "ONWATCH":    {"date": day(1), "time": "",                  "market_cap": 80e9},
        "NOCAP":      {"date": day(2), "time": "",                  "market_cap": 0.0},
    }
    cal._cache["ts"] = _time.time()     # keep the cache "fresh"
    try:
        got = cal.get_upcoming_reporters(7, min_market_cap=2e9, exclude={"ONWATCH"})
        names = [r["ticker"] for r in got]
        # Soonest first, then largest cap; sub-threshold, unknown-cap, far-dated
        # and watchlist names are all excluded.
        assert names == ["SOON_BIG", "LATER_BIG"]
        assert got[0]["days"] == 1 and got[0]["report_time"] == "time-after-hours"
        capped = cal.get_upcoming_reporters(7, min_market_cap=2e9, limit=1,
                                            exclude={"ONWATCH"})
        assert [r["ticker"] for r in capped] == ["SOON_BIG"]
        # Without the exclude, a larger same-day name outranks it on cap.
        assert cal.get_upcoming_reporters(7, min_market_cap=2e9, limit=1)[0]["ticker"] == "ONWATCH"
        # Without a cap floor the small and unknown-cap names come back too.
        assert len(cal.get_upcoming_reporters(7, exclude={"ONWATCH"})) == 4
    finally:
        cal._cache["map"] = original
        cal._cache["ts"] = original_ts


def test_pin_risk_flag():
    from signals.iv_variants import pin_risk
    row = {"short_put": 90.0, "short_call": 110.0}
    assert pin_risk(row, 110.5, 2.5) is True     # settled inside one strike of the short call
    assert pin_risk(row, 89.0, 2.5) is True      # ...or the short put
    assert pin_risk(row, 100.0, 2.5) is False    # comfortably between
    assert pin_risk(row, 110.5, None) is False   # unknown increment → no claim


# ── IV/RV strategy-variant logger (measurement only) ────────────────────
def test_variant_payoff():
    from signals.iv_variants import expiry_settlement_date, variant_payoff
    condor = {"short_put": 90.0, "long_put": 87.0, "short_call": 110.0,
              "long_call": 113.0, "credit": 1.20}
    assert variant_payoff(condor, 100) == 120.0        # inside → full credit
    assert variant_payoff(condor, 85) == -180.0        # below long put → capped max loss
    assert variant_payoff(condor, 111) == 20.0         # partway into call spread
    assert variant_payoff(condor, 120) == -180.0       # blown through → capped
    fly = {"short_put": 100.0, "long_put": 95.0, "short_call": 100.0,
           "long_call": 105.0, "credit": 4.0}
    assert variant_payoff(fly, 100) == 400.0           # pin ATM → full credit
    assert variant_payoff(fly, 110) == -100.0          # capped (width 5 − credit 4)
    straddle = {"short_put": 100.0, "long_put": None, "short_call": 100.0,
                "long_call": None, "credit": 8.0}
    assert variant_payoff(straddle, 100) == 800.0
    assert variant_payoff(straddle, 90) == -200.0      # uncapped (moved 10 vs 8 credit)
    # Never evaluate during the option's expiry session; use the completed close.
    assert expiry_settlement_date("2026-09-18") == "2026-09-18"
    assert expiry_settlement_date("not-a-date") is None


async def test_variant_eval_lifecycle(db):
    sp = {"short_put": 90.0, "long_put": 87.0, "short_call": 110.0, "long_call": 113.0}
    await db.record_variant_eval("NVDA", "2026-09-16", "2026-09-18", "condor_1.0sd",
                                 100.0, 8.0, sp, credit=1.20, max_loss=180.0,
                                 resolve_after="2026-09-18", credit_mid=1.30,
                                 fees=5.20, strike_step=1.0)
    await db.record_variant_eval("NVDA", "2026-09-16", "2026-09-18", "straddle",
                                 100.0, 8.0, {"short_put": 100.0, "short_call": 100.0},
                                 credit=8.0, max_loss=None, resolve_after="2026-09-18",
                                 credit_mid=8.10, fees=2.60, strike_step=1.0)
    assert await db.has_variant_evals("NVDA", "2026-09-16") is True
    assert await db.has_variant_evals("AAPL", "2026-09-16") is False

    due = await db.get_due_variant_evals("2026-09-20")
    assert len(due) == 2
    from signals.iv_variants import variant_payoff
    for ev in due:
        await db.resolve_variant_eval(ev["id"], 103.0, variant_payoff(ev, 103.0))

    summ = {r["variant"]: r for r in await db.get_variant_summary()}
    # NVDA moved to 103: condor stays inside (full +120), straddle loses 3 → (8-3)*100=+500
    assert summ["condor_1.0sd"]["avg_pnl"] == 120.0 and summ["condor_1.0sd"]["win_rate"] == 100.0
    assert summ["condor_1.0sd"]["avg_ror_pct"] == round(120.0 / 180.0 * 100, 1)
    assert summ["straddle"]["avg_pnl"] == 500.0
    assert summ["straddle"]["avg_ror_pct"] is None      # undefined risk → no RoR


async def test_open_variant_evals_are_rescheduled_to_after_expiry(db):
    sp = {"short_put": 90.0, "long_put": 87.0, "short_call": 110.0, "long_call": 113.0}
    await db.record_variant_eval("NVDA", "2026-09-15", "2026-09-18", "condor_1.0sd",
                                 100.0, 8.0, sp, credit=1.20, max_loss=180.0,
                                 resolve_after="2026-09-17", credit_mid=1.30,
                                 fees=5.20, strike_step=1.0)
    await db._repair_open_variant_resolution_dates()
    # Rescheduled onto the expiry itself: due that day, not the day after. The
    # resolver still holds it until that session closes (settlement_ready).
    assert await db.get_due_variant_evals("2026-09-17") == []
    assert len(await db.get_due_variant_evals("2026-09-18")) == 1
    assert len(await db.get_due_variant_evals("2026-09-19")) == 1


def test_daily_close_uses_the_requested_completed_session():
    """The variant resolver must not substitute a later latest quote."""
    from datetime import datetime as _dt
    from types import SimpleNamespace
    from feeds.alpaca_feed import AlpacaFeed

    class FakeBarSet:
        """Mirrors the real BarSet: dict access via .data, and NO .get()."""
        def __init__(self, data):
            self.data = data

    class FakeStockClient:
        def get_stock_bars(self, _request):
            return FakeBarSet({"NVDA": [
                SimpleNamespace(timestamp=_dt(2026, 9, 17), close=175.0),
                SimpleNamespace(timestamp=_dt(2026, 9, 18), close=180.0),
            ]})

    feed = AlpacaFeed.__new__(AlpacaFeed)
    feed.stock_client = FakeStockClient()
    assert feed.get_daily_close("NVDA", "2026-09-18") == 180.0


def test_alpaca_feed_uses_the_et_clock_for_market_date_bounds(monkeypatch):
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from zoneinfo import ZoneInfo
    import feeds.alpaca_feed as feed_module

    fixed = _dt(2026, 9, 15, 23, 30, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(feed_module, "et_now", lambda: fixed)

    class FakeStockClient:
        request = None
        def get_stock_bars(self, request):
            self.request = request
            return {"NVDA": []}

    class FakeOptionClient:
        request = None
        def get_option_chain(self, request):
            self.request = request
            return {}

    feed = feed_module.AlpacaFeed.__new__(feed_module.AlpacaFeed)
    feed.stock_client, feed.option_client = FakeStockClient(), FakeOptionClient()
    assert feed.get_bars("NVDA", days=2) == []
    # alpaca-py serializes timezone-aware request bounds to naive UTC.
    assert feed.stock_client.request.end == fixed.astimezone(_tz.utc).replace(tzinfo=None)
    assert feed.stock_client.request.start == (fixed - _td(days=2)).astimezone(_tz.utc).replace(tzinfo=None)
    assert feed.get_option_chain("NVDA", expiry_days=45) == []
    assert str(feed.option_client.request.expiration_date_lte) == "2026-10-30"


# ── Account-fetch failure detection (guards the $0/-100% bogus report) ──
async def test_report_flags_account_fetch_failure(db):
    class FailingTrader:
        def get_account(self):
            raise RuntimeError("unauthorized")
        def get_positions(self):
            return []
    await db.record_daily_equity("2026-09-03", 50000.0)
    d = await build_report_data(db, FailingTrader())
    # These are exactly what generate_daily_report checks to skip a bogus report.
    assert d["account"]["equity"] == 0
    assert d["account"]["error"]


# ── Durable history export (git/backup) ─────────────────────────────────
async def test_export_history(db, tmp_path):
    await db.record_daily_equity("2026-09-02", 50000.0)
    await db.record_daily_equity("2026-09-03", 49414.85)
    await seed_entry(db, "e1", URI, "URI", 8.50, 1, strategy="triple_confluence")
    await seed_exit(db, "x1", URI, "URI", 5.50, 1)
    await db.reconcile_trades()

    res = await export_history(db, tmp_path)
    assert res["days"] == 2 and res["trades_n"] == 1
    # history.jsonl: one JSON row per day, open_equity preserved
    lines = (tmp_path / "history.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    import json
    assert json.loads(lines[0])["open_equity"] == 50000.0
    # trades.csv: header + one closed trade, attributed with the correct P&L
    csv_rows = (tmp_path / "trades.csv").read_text().strip().splitlines()
    assert len(csv_rows) == 2
    assert "triple_confluence" in csv_rows[1] and "-300" in csv_rows[1]


async def test_export_history_records_today_iv_rv_snapshot(db, tmp_path):
    today = et_today().isoformat()
    await db.record_daily_equity(today, 50000.0)
    await db.record_iv_eval("NVDA", "SELL_PREMIUM", 1.4, 8.0, 100.0,
                            resolve_after="2099-01-01", earnings_date=today)

    await export_history(db, tmp_path)
    import json
    row = json.loads((tmp_path / "history.jsonl").read_text().strip())
    assert row["date"] == today
    assert row["iv_rv"] == {
        "resolved": 0, "wins": 0, "win_rate": 0.0,
        "avg_edge_pct": 0.0, "open": 1,
    }


# ── IV/RV Phase 2 execution: iron-condor builder + DB lifecycle ──────────
import json as _json
import types as _types
from datetime import timedelta as _timedelta


def _occ(t, exp, cp, strike):
    return f"{t}{exp:%y%m%d}{cp}{int(strike * 1000):08d}"


class FakeOptionTrader(FakeTrader):
    """FakeTrader + a canned single-expiry option chain for build_iron_condor."""
    def __init__(self, expiry, strikes, mids, **kw):
        super().__init__(**kw)
        self.expiry = expiry                 # datetime.date
        self.strikes = strikes               # list[float]
        self.mids = mids                     # {("C"|"P", strike): mid}

    def get_option_contracts(self, underlying, exp_gte, exp_lte, opt_type=None, limit=500):
        if not (exp_gte <= self.expiry <= exp_lte):
            return []
        cp = "C" if opt_type == "call" else "P"
        return [{"symbol": _occ(underlying, self.expiry, cp, k), "strike": k,
                 "expiry": self.expiry, "type": opt_type, "open_interest": 500}
                for k in self.strikes]

    def get_option_quotes(self, symbols):
        out = {}
        for s in symbols:
            cp = "C" if "C" in s[-9:] else "P"
            strike = int(s[-8:]) / 1000.0
            m = self.mids.get((cp, strike), 0)
            out[s] = {"bid": round(m * 0.98, 2), "ask": round(m * 1.02, 2), "mid": m}
        return out


def test_build_iron_condor_happy_path():
    from signals.iv_executor import build_iron_condor
    from config import get_settings
    exp = et_today() + _timedelta(days=1)             # front expiry, day after the print
    strikes = [float(k) for k in range(80, 121)]      # $1-wide chain 80..120
    mids = {}
    for k in strikes:
        # cheap OTM wings, richer near-the-money shorts — enough credit to pass gates
        mids[("C", k)] = max(0.10, 3.0 - 0.12 * (k - 100)) if k >= 100 else 3.0
        mids[("P", k)] = max(0.10, 3.0 - 0.12 * (100 - k)) if k <= 100 else 3.0
    trader = FakeOptionTrader(exp, strikes, mids, equity=50000.0)
    setup = _types.SimpleNamespace(
        ticker="TEST", price=100.0, expected_move="10.0%",
        recommendation="SELL_PREMIUM",
        next_earnings_date=et_today().isoformat())         # prints today → expiry tomorrow
    plan = build_iron_condor(trader, setup, 50000.0, get_settings())
    assert plan["ok"], plan.get("reason")
    st = plan["strikes"]
    # shorts at ~the implied move (±10 from 100), wings 3% ($3) beyond
    assert st["short_call"] == 110.0 and st["short_put"] == 90.0
    assert st["long_call"] == 113.0 and st["long_put"] == 87.0
    assert plan["qty"] >= 1
    assert plan["credit"] > 0 and plan["limit_price"] < 0     # net credit = negative limit
    assert plan["expiry"] == exp.isoformat()
    assert len(plan["legs"]) == 4


def test_variant_pricing_applies_slippage_and_fees():
    """VALIDATION_SPEC §3: fills must be worse than mid and commission explicit."""
    from signals.iv_variants import build_variants
    from config import get_settings
    exp = et_today() + _timedelta(days=1)
    strikes = [float(k) for k in range(80, 121)]
    mids = {}
    for k in strikes:
        mids[("C", k)] = max(0.10, 3.0 - 0.12 * (k - 100)) if k >= 100 else 3.0
        mids[("P", k)] = max(0.10, 3.0 - 0.12 * (100 - k)) if k <= 100 else 3.0
    trader = FakeOptionTrader(exp, strikes, mids, equity=50000.0)
    setup = _types.SimpleNamespace(
        ticker="TEST", price=100.0, expected_move="10.0%", recommendation="SELL_PREMIUM",
        next_earnings_date=et_today().isoformat())
    plans = {v["variant"]: v for v in build_variants(trader, setup, get_settings())}

    condor = plans["condor_1.0sd"]
    # Selling the bid and buying the ask must collect LESS than mid pricing.
    assert condor["credit"] < condor["credit_mid"]
    # 4 legs × $0.65 × 2 (open+close) = $5.20; straddle is 2 legs = $2.60.
    assert condor["fees"] == 5.20 and condor["n_legs"] == 4
    assert plans["straddle"]["fees"] == 2.60 and plans["straddle"]["n_legs"] == 2
    # Max loss carries the commission drag too.
    width = condor["strikes"]["long_call"] - condor["strikes"]["short_call"]
    assert condor["max_loss"] == round((width - condor["credit"]) * 100 + 5.20, 2)
    assert condor["strike_step"] == 1.0          # $1-wide fixture chain


def test_variant_pricing_rejects_a_missing_executable_side_quote():
    """A zero bid must not be silently replaced with the option's mid price."""
    from signals.iv_variants import build_variants
    from config import get_settings
    exp = et_today() + _timedelta(days=1)
    strikes = [float(k) for k in range(80, 121)]
    mids = {}
    for k in strikes:
        mids[("C", k)] = max(0.10, 3.0 - 0.12 * (k - 100)) if k >= 100 else 3.0
        mids[("P", k)] = max(0.10, 3.0 - 0.12 * (100 - k)) if k <= 100 else 3.0

    class NoShortCallBidTrader(FakeOptionTrader):
        def get_option_quotes(self, symbols):
            quotes = super().get_option_quotes(symbols)
            # The 1.0σ condor shorts the 110 call for this fixture.
            quotes[_occ("TEST", exp, "C", 110.0)]["bid"] = 0.0
            return quotes

    trader = NoShortCallBidTrader(exp, strikes, mids, equity=50000.0)
    setup = _types.SimpleNamespace(
        ticker="TEST", price=100.0, expected_move="10.0%", recommendation="SELL_PREMIUM",
        next_earnings_date=et_today().isoformat())
    diagnostics = {}
    plans = {v["variant"]: v for v in build_variants(
        trader, setup, get_settings(), diagnostics)}
    assert "condor_1.0sd" not in plans
    assert diagnostics["attempted"] == 5
    assert diagnostics["priced"] == len(plans)
    assert diagnostics["dropped"]["condor_1.0sd"] == "missing_executable_quote"


def test_collapsed_variants_are_tagged_on_a_coarse_strike_grid():
    """CTAS priced 1.0σ and 1.3σ identically — $5-wide strikes, small implied
    move, both targets rounding to the same four legs. The later shape must be
    tagged so it is not counted as independent evidence about shape."""
    from signals.iv_variants import build_variants
    from config import get_settings
    exp = et_today() + _timedelta(days=1)
    strikes = [float(k) for k in range(100, 301, 5)]     # $5 grid, like CTAS
    mids = {}
    for k in strikes:
        mids[("C", k)] = max(0.10, 12.0 - 0.10 * (k - 200)) if k >= 200 else 12.0
        mids[("P", k)] = max(0.10, 12.0 - 0.10 * (200 - k)) if k <= 200 else 12.0
    trader = FakeOptionTrader(exp, strikes, mids, equity=50000.0)
    setup = _types.SimpleNamespace(
        ticker="TEST", price=200.0, expected_move="3.0%",   # move = $6 on a $5 grid
        recommendation="SELL_PREMIUM", next_earnings_date=et_today().isoformat())
    plans = {v["variant"]: v for v in build_variants(trader, setup, get_settings())}

    # 1.0σ ($206) and 1.3σ ($207.80) both round up to the 210 call / 190 put,
    # while 0.7σ ($204.20) still lands on its own 205/195 — the CTAS pattern.
    assert plans["condor_1.0sd"]["strikes"] == plans["condor_1.3sd"]["strikes"]
    assert plans["condor_0.7sd"]["strikes"] != plans["condor_1.0sd"]["strikes"]
    assert plans["condor_1.0sd"]["collapsed_with"] is None      # first wins, stays canonical
    assert plans["condor_1.3sd"]["collapsed_with"] == "condor_1.0sd"
    # Genuinely distinct shapes are untouched.
    assert plans["condor_0.7sd"]["collapsed_with"] is None
    assert plans["straddle"]["collapsed_with"] is None


async def test_collapsed_backfill_tags_preexisting_rows(db):
    """Rows written before the column existed must still be tagged — the
    collapse was always true of them, only the column is new."""
    same = {"short_put": 195.0, "long_put": 185.0, "short_call": 205.0, "long_call": 215.0}
    other = {"short_put": 197.5, "long_put": 190.0, "short_call": 202.5, "long_call": 210.0}
    for variant, sk in (("condor_0.7sd", other), ("condor_1.0sd", same),
                        ("condor_1.3sd", same)):
        await db.record_variant_eval(
            "CTAS", "2026-09-23", "2026-09-25", variant, 200.0, 2.0, sk,
            credit=1.14, max_loss=391.0, resolve_after="2026-09-26",
            credit_mid=2.88, fees=5.20, strike_step=2.5)   # no collapsed_with
    await db._backfill_collapsed_variants()

    got = {r["variant"]: r["collapsed_with"] for r in await db._query(
        "SELECT variant, collapsed_with FROM iv_variant_evals WHERE ticker='CTAS'")}
    assert got["condor_1.3sd"] == "condor_1.0sd"   # later duplicate tagged
    assert got["condor_1.0sd"] is None             # canonical untouched
    assert got["condor_0.7sd"] is None             # genuinely distinct


async def test_collapsed_rows_excluded_from_shape_comparison(db):
    sp = {"short_put": 90.0, "long_put": 87.0, "short_call": 110.0, "long_call": 113.0}

    async def seed(variant, pnl, collapsed_with=None):
        await db.record_variant_eval(
            "AAA", "2026-09-16", "2026-09-18", variant, 100.0, 8.0, sp,
            credit=1.20, max_loss=180.0, resolve_after="2026-09-19",
            credit_mid=1.30, fees=5.20, strike_step=1.0,
            collapsed_with=collapsed_with)
        row = (await db._query("SELECT id FROM iv_variant_evals ORDER BY id DESC LIMIT 1"))[0]
        await db.resolve_variant_eval(row["id"], 103.0, pnl)

    await seed("condor_1.0sd", 120.0)
    await seed("condor_1.3sd", 120.0, collapsed_with="condor_1.0sd")

    full = {v["variant"]: v for v in await db.get_variant_summary()}
    assert full["condor_1.3sd"]["n_events"] == 1
    assert full["condor_1.3sd"]["collapsed_n"] == 1     # visible either way
    assert full["condor_1.0sd"]["collapsed_n"] == 0

    distinct = {v["variant"] for v in await db.get_variant_summary(distinct_only=True)}
    assert "condor_1.0sd" in distinct and "condor_1.3sd" not in distinct


async def test_gate_comparison_scores_gated_vs_ungated(db):
    """The experiment: do the three gates beat selling everything?"""
    sp = {"short_put": 90.0, "long_put": 87.0, "short_call": 110.0, "long_call": 113.0}

    async def seed(ticker, gate_passed, pnl):
        await db.record_variant_eval(ticker, "2026-09-16", "2026-09-18", "condor_1.0sd",
                                     100.0, 8.0, sp, credit=1.20, max_loss=180.0,
                                     resolve_after="2026-09-19", credit_mid=1.30,
                                     fees=5.20, strike_step=1.0, gate_passed=gate_passed)
        row = (await db._query(
            "SELECT id FROM iv_variant_evals ORDER BY id DESC LIMIT 1"))[0]
        await db.resolve_variant_eval(row["id"], 103.0, pnl)

    await seed("AAA", True, 120.0)
    await seed("BBB", True, 100.0)
    await seed("CCC", False, -300.0)
    await seed("DDD", False, 100.0)

    cmp = await db.get_gate_comparison()
    assert cmp["gated"]["n_events"] == 2 and cmp["gated"]["expectancy"] == 110.0
    assert cmp["ungated"]["n_events"] == 2 and cmp["ungated"]["expectancy"] == -100.0
    # Summary respects the gate filter too.
    gated_only = await db.get_variant_summary(gate_passed=True)
    assert gated_only[0]["n_events"] == 2
    everything = await db.get_variant_summary(gate_passed=None)
    assert everything[0]["n_events"] == 4


async def test_variant_provenance_filters_and_quote_coverage(db):
    """Gate effects and quote coverage remain attributable to each population."""
    sp = {"short_put": 90.0, "long_put": 87.0, "short_call": 110.0, "long_call": 113.0}

    async def seed(ticker, source, gate_passed, pnl):
        await db.record_variant_eval(
            ticker, "2026-09-16", "2026-09-18", "condor_1.0sd", 100.0, 8.0, sp,
            credit=1.20, max_loss=180.0, resolve_after="2026-09-19",
            credit_mid=1.30, fees=5.20, strike_step=1.0,
            gate_passed=gate_passed, source=source)
        row = (await db._query(
            "SELECT id FROM iv_variant_evals ORDER BY id DESC LIMIT 1"))[0]
        await db.resolve_variant_eval(row["id"], 103.0, pnl)

    await seed("WATCH", "watchlist", True, 100.0)
    await seed("MEASURE", "measurement", False, -100.0)
    assert (await db.get_variant_summary(source="watchlist"))[0]["n_events"] == 1
    assert (await db.get_variant_summary(source="measurement", gate_passed=False))[0]["expectancy"] == -100.0
    assert (await db.get_gate_comparison(source="measurement"))["ungated"]["n_events"] == 1

    await db.record_variant_attempt(
        "MEASURE", "2026-09-16", 5, 3,
        {"condor_1.0sd": "missing_executable_quote", "fly": "nonpositive_credit"},
        source="measurement")
    # Retries replace an event's observation instead of inflating the denominator.
    await db.record_variant_attempt(
        "MEASURE", "2026-09-16", 5, 4,
        {"fly": "nonpositive_credit"}, source="measurement")
    coverage = await db.get_variant_quote_coverage(source="measurement")
    assert coverage == {
        "events": 1, "structures_attempted": 5, "structures_priced": 4,
        "priced_pct": 80.0, "dropped_variants": {"fly": 1},
    }
    report = render_html(await build_report_data(db, FakeTrader()))
    assert "4/5 structures priceable (80.0%)" in report
    assert "Dropped in at least one event: fly (1)." in report


async def test_variant_source_migrates_an_existing_database(tmp_path):
    """The source index must wait until legacy rows gain the source column."""
    import sqlite3
    from db import Database

    path = tmp_path / "legacy.db"
    initial = Database(path=path)
    await initial.connect()
    await initial.close()

    # Simulate the pre-provenance database shape that a deployed instance has.
    raw = sqlite3.connect(path)
    raw.execute("DROP INDEX idx_variant_source")
    raw.execute("ALTER TABLE iv_variant_evals DROP COLUMN source")
    raw.commit()
    raw.close()

    migrated = Database(path=path)
    await migrated.connect()
    try:
        columns = {r["name"] for r in await migrated._query("PRAGMA table_info(iv_variant_evals)")}
        indexes = await migrated._query(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_variant_source'")
        assert "source" in columns and indexes
    finally:
        await migrated.close()


async def test_variant_metrics_exclude_legacy_rows_and_order_by_expiry(db):
    """Validated metrics use one pricing model and an actual event sequence."""
    from db import LEGACY_VARIANT_PRICING_MODEL
    sp = {"short_put": 90.0, "long_put": 87.0, "short_call": 110.0, "long_call": 113.0}

    async def seed(ticker, expiry, pnl, pricing_model=None):
        await db.record_variant_eval(
            ticker, "2026-09-16", expiry, "condor_1.0sd", 100.0, 8.0, sp,
            credit=1.20, max_loss=180.0, resolve_after="2026-09-25",
            credit_mid=1.30, fees=5.20, strike_step=1.0, pricing_model=pricing_model)
        row = (await db._query("SELECT id FROM iv_variant_evals ORDER BY id DESC LIMIT 1"))[0]
        await db.resolve_variant_eval(row["id"], 103.0, pnl)

    # Insertion order would be +100, +100, -100, -100 (drawdown 200).
    # Expiry order is +100, -100, +100, -100 (drawdown 100).
    await seed("AAA", "2026-09-18", 100.0)
    await seed("BBB", "2026-09-20", 100.0)
    await seed("CCC", "2026-09-19", -100.0)
    await seed("DDD", "2026-09-21", -100.0)
    await seed("OLD", "2026-09-17", 999.0, LEGACY_VARIANT_PRICING_MODEL)

    summary = (await db.get_variant_summary())[0]
    assert summary["n_events"] == 4
    assert summary["max_drawdown"] == 100.0
    comparison = await db.get_gate_comparison()
    assert comparison["gated"]["n_events"] == 4
    assert comparison["gated"]["max_drawdown"] == 100.0


def test_build_iron_condor_rejects_far_earnings():
    from signals.iv_executor import build_iron_condor
    from config import get_settings
    exp = et_today() + _timedelta(days=1)
    trader = FakeOptionTrader(exp, [float(k) for k in range(80, 121)], {}, equity=50000.0)
    setup = _types.SimpleNamespace(
        ticker="TEST", price=100.0, expected_move="10.0%", recommendation="SELL_PREMIUM",
        next_earnings_date=(et_today() + _timedelta(days=40)).isoformat())
    plan = build_iron_condor(trader, setup, 50000.0, get_settings())
    assert not plan["ok"] and "DTE band" in plan["reason"]


def test_pre_earnings_entry_window():
    from datetime import datetime as _dt
    from signals.iv_executor import is_pre_earnings_entry_window

    # Future events can be entered regardless of whether a report-time source is
    # available. Same-day events require a confirmed post-close report and must
    # be entered before 16:00 ET; BMO and unknown reports fail closed.
    assert is_pre_earnings_entry_window("2026-09-16", None, 2,
                                        now=_dt(2026, 9, 15, 10, 0)) is True
    assert is_pre_earnings_entry_window("2026-09-15", "post", 2,
                                        now=_dt(2026, 9, 15, 15, 59)) is True
    assert is_pre_earnings_entry_window("2026-09-15", "pre", 2,
                                        now=_dt(2026, 9, 15, 10, 0)) is False
    assert is_pre_earnings_entry_window("2026-09-15", None, 2,
                                        now=_dt(2026, 9, 15, 10, 0)) is False
    assert is_pre_earnings_entry_window("2026-09-15", "post", 2,
                                        now=_dt(2026, 9, 15, 16, 0)) is False


async def test_condor_db_lifecycle(db):
    strikes = {"short_put": 90.0, "long_put": 87.0, "short_call": 110.0, "long_call": 113.0}
    legs = _json.dumps([
        {"symbol": "TEST260911C00110000", "side": "sell", "position_intent": "sell_to_open", "ratio_qty": 1},
        {"symbol": "TEST260911C00113000", "side": "buy",  "position_intent": "buy_to_open",  "ratio_qty": 1},
        {"symbol": "TEST260911P00090000", "side": "sell", "position_intent": "sell_to_open", "ratio_qty": 1},
        {"symbol": "TEST260911P00087000", "side": "buy",  "position_intent": "buy_to_open",  "ratio_qty": 1},
    ])
    from datetime import datetime as _dt
    today = _dt.utcnow().strftime("%Y-%m-%d")
    cid = await db.record_condor("NVDA", "2026-09-10", "2026-09-11", legs, strikes,
                                 qty=2, credit=1.20, max_loss=180.0,
                                 entry_order_id="o1", entry_status="new")
    assert cid > 0
    assert await db.has_open_condor("NVDA") is True
    assert await db.has_open_condor("AAPL") is False
    assert await db.count_open_condors() == 1
    assert await db.count_condors_opened_today(today) == 1
    assert len(await db.get_active_condor_leg_symbols()) == 4
    assert len(await db.get_open_condors()) == 0  # submitted is not filled

    # A real parent fill overwrites planning economics.  -1.05 is the MLeg
    # net-credit convention, so the actual per-spread max loss is $195.
    actual = await db.activate_condor(cid, filled_qty=1, filled_avg_price=-1.05)
    assert actual == {"qty": 1, "credit": 1.05, "max_loss": 195.0}
    assert len(await db.get_open_condors()) == 1

    # 'closing' still counts as active/open-for-dedup, but not in get_open_condors
    await db.mark_condor_closing(cid, "c1")
    assert await db.has_open_condor("NVDA") is True
    assert len(await db.get_open_condors()) == 0
    assert len(await db.get_active_condors()) == 1

    # Close using actual fill economics: (1.05 - 0.50) * 100 * 1 = 55.
    await db.close_condor(cid, exit_debit=0.50, pnl=(1.05 - 0.50) * 100)
    assert await db.has_open_condor("NVDA") is False
    s = await db.get_condor_summary()
    assert s["closed"] == 1 and s["wins"] == 1 and s["total_pnl"] == 55.0 and s["open"] == 0


# ── Liveness heartbeat (launchd restarts a dead process; this catches a wedged one) ──
async def test_heartbeat_detects_a_stalled_loop(db):
    from datetime import datetime as _dtm, timedelta as _tdm
    # Nothing written yet → stale, and say so rather than implying health.
    cold = await db.get_heartbeat(180)
    assert cold["stale"] is True and cold["last_seen"] is None

    await db.record_daily_equity("2026-09-23", 50000.0)
    fresh = await db.get_heartbeat(180)
    assert fresh["stale"] is False and fresh["age_minutes"] < 5

    # The hourly upsert must move updated_at, not just created_at — otherwise a
    # wedged loop looks alive forever because the day's first write never ages.
    stale_ts = (_dtm.utcnow() - _tdm(hours=9)).isoformat()
    await db._exec("UPDATE daily_equity SET updated_at=? WHERE date=?",
                   (stale_ts, "2026-09-23"))
    stalled = await db.get_heartbeat(180)
    assert stalled["stale"] is True
    assert 530 < stalled["age_minutes"] < 550        # ~9h
    assert "expected hourly" in stalled["note"]

    # A later pass clears it.
    await db.record_daily_equity("2026-09-23", 50123.0)
    assert (await db.get_heartbeat(180))["stale"] is False


async def test_stale_heartbeat_renders_a_warning_banner(db):
    """A silent outage must not render as a normal-looking report.

    Built from a real build_report_data payload rather than a hand-rolled dict,
    so this cannot rot into a KeyError chase every time the report grows a field.
    """
    await db.record_daily_equity("2026-09-23", 50000.0)
    base = await build_report_data(db, FakeTrader())

    stale = render_html({**base, "heartbeat": {"stale": True, "age_minutes": 540.0,
                                               "threshold_minutes": 180, "last_seen": "x"}})
    assert "BACKEND MAY BE DOWN" in stale and "9.0 hours ago" in stale

    ok = render_html({**base, "heartbeat": {"stale": False, "age_minutes": 12.0,
                                            "threshold_minutes": 180, "last_seen": "x"}})
    assert "BACKEND MAY BE DOWN" not in ok and "heartbeat OK" in ok


# ── Implied vs realized: the structural edge, measured per EVENT ──────────
async def test_implied_vs_realized_is_per_event_not_per_variant(db):
    """All five structures on one print share the same underlying move. Counting
    variant rows would inflate n fivefold and shrink the error bars fraudulently."""
    sp = {"short_put": 90.0, "long_put": 87.0, "short_call": 110.0, "long_call": 113.0}

    async def seed_event(ticker, edate, spot, exit_spot, implied, lead=1):
        for variant in ("condor_0.7sd", "condor_1.0sd", "condor_1.3sd", "fly", "straddle"):
            await db.record_variant_eval(
                ticker, edate, "2026-09-18", variant, spot, implied, sp,
                credit=1.20, max_loss=180.0, resolve_after="2026-09-19",
                credit_mid=1.30, fees=5.20, strike_step=1.0, lead_days=lead)
            row = (await db._query(
                "SELECT id FROM iv_variant_evals ORDER BY id DESC LIMIT 1"))[0]
            await db.resolve_variant_eval(row["id"], exit_spot, 0.0)

    # AAA moved 5% against a 7% implied → seller wins. BBB moved 12% vs 6% → exceeded.
    await seed_event("AAA", "2026-09-16", 100.0, 95.0, 7.0)
    await seed_event("BBB", "2026-09-17", 100.0, 112.0, 6.0)

    r = await db.get_implied_vs_realized()
    assert r["n_events"] == 2                 # 2 events, NOT 10 variant rows
    assert r["n_exceeding"] == 1
    assert r["pct_exceeding_implied"] == 50.0
    assert r["avg_implied_pct"] == 6.5
    assert r["avg_realized_pct"] == 8.5       # (5.0 + 12.0) / 2
    assert r["avg_edge_pct"] == -2.0          # implied minus realized, negative = seller lost
    # worst edge first, so the events that beat the market are top of the list
    assert r["events"][0]["ticker"] == "BBB" and r["events"][0]["exceeded"] is True
    assert r["events"][1]["exceeded"] is False
    assert r["stale_capture"]["n_events"] == 0

    # An implied move read a week out is the quiet front-month IV, not the
    # earnings premium, so it understates implied and scores "exceeded" almost
    # for free. It must not be allowed to set the headline.
    await seed_event("CCC", "2026-09-18", 100.0, 103.0, 3.0, lead=7)
    r = await db.get_implied_vs_realized()
    assert r["n_events"] == 2                       # headline unchanged
    assert r["pct_exceeding_implied"] == 50.0
    assert r["stale_capture"]["n_events"] == 1
    assert r["stale_capture"]["events"][0]["ticker"] == "CCC"
    assert r["stale_capture"]["events"][0]["lead_days"] == 7
    # Pooling would have read 66.7% exceeding off the same data.
    assert r["lead_cutoff_days"] == 2


async def test_report_keeps_implied_realized_cohorts_separate(db):
    """Selection-rule cohorts must never become one headline edge statistic."""
    sp = {"short_put": 90.0, "long_put": 87.0, "short_call": 110.0, "long_call": 113.0}

    async def seed(ticker, source, exit_spot):
        await db.record_variant_eval(
            ticker, "2026-09-16", "2026-09-18", "condor_1.0sd", 100.0, 8.0, sp,
            credit=1.20, max_loss=180.0, resolve_after="2026-09-19",
            credit_mid=1.30, fees=5.20, strike_step=1.0, source=source, lead_days=1)
        row = (await db._query(
            "SELECT id FROM iv_variant_evals ORDER BY id DESC LIMIT 1"))[0]
        await db.resolve_variant_eval(row["id"], exit_spot, 0.0)

    await seed("WATCH", "watchlist", 105.0)
    await seed("MEASURE", "measurement", 112.0)
    report_data = await build_report_data(db, FakeTrader())
    cohorts = report_data["implied_vs_realized"]
    assert set(cohorts) == {"watchlist", "measurement"}
    assert cohorts["watchlist"]["n_events"] == 1
    assert cohorts["watchlist"]["avg_realized_pct"] == 5.0
    assert cohorts["measurement"]["n_events"] == 1
    assert cohorts["measurement"]["avg_realized_pct"] == 12.0
    report = render_html(report_data)
    assert "Implied vs realized — Watchlist" in report
    assert "Implied vs realized — Measurement" in report


async def test_implied_vs_realized_empty_is_honest(db):
    r = await db.get_implied_vs_realized()
    assert r["n_events"] == 0 and r["pct_exceeding_implied"] is None


# ── Risk throttle: anti-martingale sizing + latching circuit breaker ─────
async def _close_condor(db, ticker, pnl, closed_at):
    sp = {"short_put": 90.0, "long_put": 87.0, "short_call": 110.0, "long_call": 113.0}
    await db.record_condor(ticker, "2026-09-16", "2026-09-18", "[]", sp,
                           qty=1, credit=1.20, max_loss=180.0,
                           entry_order_id=f"o{ticker}{closed_at}", entry_status="filled")
    row = (await db._query("SELECT id FROM iv_condors ORDER BY id DESC LIMIT 1"))[0]
    await db.close_condor(row["id"], 0.5, pnl)
    await db._exec("UPDATE iv_condors SET closed_at=? WHERE id=?", (closed_at, row["id"]))


async def test_risk_throttle_ladder_down_and_back_up(db):
    st = lambda: db.get_risk_state(0.5, 2.0, 0.25, 3)
    assert (await st())["multiplier"] == 1.0          # nothing closed yet

    await _close_condor(db, "AAA", -100.0, "2026-09-20T10:00:00")
    s1 = await st(); assert s1["multiplier"] == 0.5 and s1["loss_streak"] == 1

    await _close_condor(db, "BBB", -100.0, "2026-09-21T10:00:00")
    s2 = await st(); assert s2["multiplier"] == 0.25 and s2["loss_streak"] == 2

    # Floor holds — a third loss cannot size below 25%.
    await _close_condor(db, "CCC", -100.0, "2026-09-22T10:00:00")
    s3 = await st(); assert s3["multiplier"] == 0.25 and s3["loss_streak"] == 3

    # Wins ratchet back up and reset the streak.
    await _close_condor(db, "DDD", 200.0, "2026-09-23T10:00:00")
    s4 = await st(); assert s4["multiplier"] == 0.5 and s4["loss_streak"] == 0
    await _close_condor(db, "EEE", 200.0, "2026-09-24T10:00:00")
    assert (await st())["multiplier"] == 1.0
    await _close_condor(db, "FFF", 200.0, "2026-09-25T10:00:00")
    assert (await st())["multiplier"] == 1.0          # capped, never above full


async def test_risk_throttle_breakeven_preserves_loss_state(db):
    st = lambda: db.get_risk_state(0.5, 2.0, 0.25, 3)
    await _close_condor(db, "AAA", -100.0, "2026-09-20T10:00:00")
    await _close_condor(db, "BBB", -100.0, "2026-09-21T10:00:00")
    before = await st()
    await _close_condor(db, "CCC", 0.0, "2026-09-22T10:00:00")
    after = await st()
    assert before["multiplier"] == after["multiplier"] == 0.25
    assert before["loss_streak"] == after["loss_streak"] == 2


async def test_circuit_breaker_latches_until_cleared(db):
    st = lambda: db.get_risk_state(0.5, 2.0, 0.25, 3)
    for i, tk in enumerate(("AAA", "BBB", "CCC")):
        await _close_condor(db, tk, -100.0, f"2026-09-2{i}T10:00:00")
    assert (await st())["loss_streak"] == 3           # breaker condition met

    await db.set_halt("3 consecutive losing condors — halted pending review")
    h = await st()
    assert h["halted"] is True and "consecutive" in h["halted_reason"]

    # A win does NOT silently un-halt it — a breaker that resets itself is not
    # a breaker, it just re-enters the regime that tripped it.
    await _close_condor(db, "DDD", 500.0, "2026-09-24T10:00:00")
    assert (await st())["halted"] is True

    # Clearing re-arms AND resets the streak, so it is not one loss from
    # re-halting the moment a human says it may trade again.
    await db.clear_halt()
    cleared = await st()
    assert cleared["halted"] is False
    assert cleared["loss_streak"] == 0 and cleared["multiplier"] == 1.0
    assert cleared["closes_counted"] == 0             # prior history no longer counts


def test_risk_multiplier_shrinks_the_wing_not_just_quantity():
    """Shrinking only qty would leave sizing flat between integer thresholds."""
    from signals.iv_executor import build_iron_condor
    from config import get_settings
    exp = et_today() + _timedelta(days=1)
    strikes = [float(k) for k in range(80, 121)]
    mids = {}
    for k in strikes:
        mids[("C", k)] = max(0.10, 3.0 - 0.12 * (k - 100)) if k >= 100 else 3.0
        mids[("P", k)] = max(0.10, 3.0 - 0.12 * (100 - k)) if k <= 100 else 3.0
    trader = FakeOptionTrader(exp, strikes, mids, equity=50000.0)
    setup = _types.SimpleNamespace(
        ticker="TEST", price=100.0, expected_move="10.0%", recommendation="SELL_PREMIUM",
        next_earnings_date=et_today().isoformat())
    s = get_settings()
    full = build_iron_condor(trader, setup, 50000.0, s, risk_multiplier=1.0)
    half = build_iron_condor(trader, setup, 50000.0, s, risk_multiplier=0.5)
    assert full["ok"] and half["ok"]
    full_risk = full["max_loss"] * full["qty"]
    half_risk = half["max_loss"] * half["qty"]
    assert half_risk < full_risk        # actually de-risked, not merely re-quantized


# ── Re-pricing near the print (implied move inflates as earnings approach) ──
async def test_open_variant_lead_and_reprice_delete(db):
    sp = {"short_put": 90.0, "long_put": 87.0, "short_call": 110.0, "long_call": 113.0}

    async def seed(variant, lead, resolved=False):
        await db.record_variant_eval(
            "COST", "2026-09-24", "2026-09-25", variant, 900.0, 1.22, sp,
            credit=1.20, max_loss=180.0, resolve_after="2026-09-26",
            credit_mid=1.30, fees=5.20, strike_step=2.5, lead_days=lead)
        row = (await db._query("SELECT id FROM iv_variant_evals ORDER BY id DESC LIMIT 1"))[0]
        if resolved:
            await db.resolve_variant_eval(row["id"], 930.0, 10.0)

    assert await db.get_open_variant_lead("COST", "2026-09-24") is None   # nothing yet
    await seed("condor_1.0sd", 7)
    await seed("straddle", 7)
    assert await db.get_open_variant_lead("COST", "2026-09-24") == 7

    # A settled measurement must never be discarded by a re-price.
    await seed("fly", 7, resolved=True)
    dropped = await db.delete_open_variant_evals("COST", "2026-09-24")
    assert dropped == 2                                    # the two unresolved only
    remaining = await db._query("SELECT variant, resolved FROM iv_variant_evals")
    assert [(r["variant"], r["resolved"]) for r in remaining] == [("fly", 1)]
    assert await db.get_open_variant_lead("COST", "2026-09-24") is None

    # Re-priced rows carry the tighter lead, so the bias is auditable.
    await seed("condor_1.0sd", 1)
    assert await db.get_open_variant_lead("COST", "2026-09-24") == 1


async def test_variant_pricing_action_is_shared_by_both_loops(db, monkeypatch):
    """The watchlist scanner and the measurement universe must agree on when an
    event gets re-priced. The measurement loop used to skip on "rows exist",
    which left measurement-source names (CCL/JBL/ACN) pinned to the implied move
    read 7 days before the print.
    """
    import main
    monkeypatch.setattr(main, "db", db)
    sp = {"short_put": 20.0, "long_put": 18.0, "short_call": 26.0, "long_call": 28.0}

    async def seed(variant, lead):
        await db.record_variant_eval(
            "CCL", "2026-09-29", "2026-09-22", variant, 22.86, 3.39, sp,
            credit=0.18, max_loss=182.0, resolve_after="2026-10-04",
            credit_mid=0.20, fees=2.60, strike_step=1.0, lead_days=lead)

    # Never priced → price it, whatever the distance.
    assert await main.variant_pricing_action("CCL", "2026-09-29", 7) == ("fresh", None)

    await seed("condor_1.0sd", 7)
    within = main.settings.iv_variants_reprice_within_days

    # A capture in the other provenance cohort must not suppress this source.
    assert await main.variant_pricing_action(
        "CCL", "2026-09-29", within, source="measurement") == ("fresh", None)

    # Closer than what we hold AND inside the window → re-price.
    assert await main.variant_pricing_action("CCL", "2026-09-29", within) == ("reprice", 7)
    assert await main.variant_pricing_action("CCL", "2026-09-29", 0) == ("reprice", 7)
    # Closer, but still far enough out that the earnings premium isn't in the
    # quotes yet — re-pricing there would just burn scans for the same bad number.
    assert await main.variant_pricing_action("CCL", "2026-09-29", within + 1) == ("skip", 7)
    # No closer than the held rows, or no usable date → leave them alone.
    assert await main.variant_pricing_action("CCL", "2026-09-29", 7) == ("skip", 7)
    assert await main.variant_pricing_action("CCL", "2026-09-29", 9) == ("skip", 7)
    assert await main.variant_pricing_action("CCL", "2026-09-29", None) == ("skip", 7)

    # Legacy rows predate lead_days. They are treated as maximally stale (999)
    # rather than as "unknown, leave it" — otherwise the events already on the
    # books when this shipped could never be corrected.
    await db._exec("UPDATE iv_variant_evals SET lead_days=NULL")
    assert await main.variant_pricing_action("CCL", "2026-09-29", 2) == ("reprice", 999)


def test_days_to_print_handles_junk():
    import main
    assert main.days_to_print(None) is None
    assert main.days_to_print("not-a-date") is None
    assert main.days_to_print("2026-09-28") == (
        __import__("market_time").et_today() - __import__("datetime").date(2026, 9, 28)).days * -1


async def test_signal_date_is_the_et_trading_day_not_the_utc_day(db, monkeypatch):
    """signal_date answers "which session did we price this in". After 20:00 ET
    the UTC calendar has already rolled, so a utcnow()-derived date stamped
    evening entries with tomorrow's session — which is exactly when the
    near-print re-pricing pass runs.
    """
    import datetime as _d
    import db as dbmod

    class FrozenUTC(_d.datetime):
        @classmethod
        def utcnow(cls):
            return cls(2026, 9, 29, 1, 34)          # 21:34 ET on the 28th
    monkeypatch.setattr(dbmod, "datetime", FrozenUTC)
    monkeypatch.setattr(dbmod, "et_today", lambda: _d.date(2026, 9, 28))

    sp = {"short_put": 20.0, "long_put": 18.0, "short_call": 26.0, "long_call": 28.0}
    await db.record_variant_eval(
        "CCL", "2026-09-29", "2026-10-02", "condor_1.0sd", 22.86, 7.09, sp,
        credit=0.55, max_loss=145.0, resolve_after="2026-10-04",
        credit_mid=0.58, fees=2.60, strike_step=1.0, lead_days=1)
    await db.record_iv_eval("CCL", "2026-09-29", "CONSIDER", 1.31, 7.09, 22.86,
                            "2026-10-01")

    v = (await db._query("SELECT signal_date, created_at FROM iv_variant_evals"))[0]
    i = (await db._query("SELECT signal_date, created_at FROM iv_rv_evals"))[0]
    assert v["signal_date"] == "2026-09-28", v["signal_date"]
    assert i["signal_date"] == "2026-09-28", i["signal_date"]
    # The audit trail still keeps the true UTC instant of the write.
    assert v["created_at"].startswith("2026-09-29T01:34")
    assert i["created_at"].startswith("2026-09-29T01:34")


async def test_lead_days_backfill_recovers_pre_column_captures(db):
    """A NULL lead_days is not evidence of a stale capture — the column simply
    did not exist yet. Treating every legacy row as maximally stale would discard
    LEN, which was priced on the print date, along with the genuinely early ones.
    """
    sp = {"short_put": 75.0, "long_put": 72.0, "short_call": 86.0, "long_call": 89.0}
    for tk, sd, ed in (("LEN", "2026-09-16", "2026-09-16"),     # priced at the print
                       ("COST", "2026-09-17", "2026-09-24"),    # a week early
                       ("ORPH", "2026-09-17", None)):           # no print date known
        await db.record_variant_eval(
            tk, ed, "2026-09-18", "condor_1.0sd", 80.0, 6.35, sp,
            credit=0.35, max_loss=265.0, resolve_after="2026-09-18",
            credit_mid=0.40, fees=2.60, strike_step=1.0)
        await db._exec("UPDATE iv_variant_evals SET signal_date=?, lead_days=NULL "
                       "WHERE ticker=?", (sd, tk))

    await db._backfill_variant_lead_days()
    got = {r["ticker"]: r["lead_days"] for r in
           await db._query("SELECT ticker, lead_days FROM iv_variant_evals")}
    assert got["LEN"] == 0
    assert got["COST"] == 7
    assert got["ORPH"] is None          # nothing to derive from; stays maximally stale

    # Which is what puts LEN back in the headline cohort and keeps COST out.
    await db._exec("UPDATE iv_variant_evals SET resolved=1, exit_spot=84.2 "
                   "WHERE ticker IN ('LEN','COST')")
    r = await db.get_implied_vs_realized(max_lead_days=2)
    assert [e["ticker"] for e in r["events"]] == ["LEN"]
    assert [e["ticker"] for e in r["stale_capture"]["events"]] == ["COST"]


def test_settlement_waits_for_the_close_not_a_calendar_day():
    """Settlement is the expiry session's close, so it can be read that evening.

    The old rule added a calendar day, which pushed every Friday expiry into the
    weekend. What it was really guarding against is unaffected by a date offset:
    Alpaca publishes a Day bar for the session in progress, already stamped with
    that date, so the gate has to be the CLOCK, not the date.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from signals.iv_variants import expiry_settlement_date, settlement_ready
    ET = ZoneInfo("America/New_York")
    friday = "2026-10-02"

    assert expiry_settlement_date(friday) == friday      # same day, not Saturday
    assert expiry_settlement_date(None) is None
    assert expiry_settlement_date("nope") is None

    def at(y, m, d, hh, mm):
        return datetime(y, m, d, hh, mm, tzinfo=ET)

    # Expiry morning/midday: the Day bar exists but is still trading.
    assert settlement_ready(friday, at(2026, 10, 2, 9, 45)) is False
    assert settlement_ready(friday, at(2026, 10, 2, 15, 59)) is False
    # The close itself is not enough — late prints still settle the bar.
    assert settlement_ready(friday, at(2026, 10, 2, 16, 0)) is False
    assert settlement_ready(friday, at(2026, 10, 2, 16, 29)) is False
    # Past the buffer, that evening. This is the day we gained back.
    assert settlement_ready(friday, at(2026, 10, 2, 16, 30)) is True
    assert settlement_ready(friday, at(2026, 10, 2, 21, 0)) is True
    # Any later session is long over — weekends and holidays included.
    assert settlement_ready(friday, at(2026, 10, 3, 2, 0)) is True
    assert settlement_ready(friday, at(2026, 10, 5, 9, 0)) is True
    # Before expiry, never.
    assert settlement_ready(friday, at(2026, 10, 1, 23, 59)) is False
    assert settlement_ready(None, at(2026, 10, 5, 9, 0)) is False

    # A naive timestamp is read as ET, not as the host's zone — the whole point
    # of routing through market_time. 16:30 naive must mean 16:30 in New York.
    assert settlement_ready(friday, datetime(2026, 10, 2, 16, 30)) is True
    assert settlement_ready(friday, datetime(2026, 10, 2, 15, 30)) is False
    # And a UTC instant converts: 20:30Z is 16:30 ET on the same date.
    assert settlement_ready(friday, datetime(2026, 10, 2, 20, 30,
                                             tzinfo=ZoneInfo("UTC"))) is True
    assert settlement_ready(friday, datetime(2026, 10, 2, 19, 30,
                                             tzinfo=ZoneInfo("UTC"))) is False


def test_settlement_uses_the_calendar_early_close():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from signals.iv_variants import settlement_ready
    from feeds.uw_budget import market_close_minute
    ET = ZoneInfo("America/New_York")
    early = "2026-11-27"

    assert market_close_minute(datetime(2026, 11, 27, 12, 0, tzinfo=ET)) == 13 * 60
    assert settlement_ready(early, datetime(2026, 11, 27, 13, 29, tzinfo=ET)) is False
    assert settlement_ready(early, datetime(2026, 11, 27, 13, 30, tzinfo=ET)) is True


async def test_reprice_never_drops_a_capture_it_cannot_replace(db, monkeypatch):
    """The re-price used to delete first and rebuild second. When pricing then
    returned nothing — which is what happens outside RTH, where options have no
    two-sided market — the event was left with NO rows at all. That destroyed the
    JBL/MU/ACN/NKE captures on 2026-09-29. A held capture beats a lost one.
    """
    import main
    from datetime import timedelta as _td
    from market_time import et_today
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "is_rth_now", lambda: True)
    sp = {"short_put": 300.0, "long_put": 295.0, "short_call": 320.0, "long_call": 325.0}

    # Anchor the print one day out from the REAL today. lead_days is computed
    # against et_today(), so a hardcoded date makes the expected lead drift and
    # the test fails on a later calendar day for no reason.
    edate = (et_today() + _td(days=1)).isoformat()
    expiry = (et_today() + _td(days=3)).isoformat()

    for variant in ("condor_1.0sd", "straddle"):
        await db.record_variant_eval(
            "JBL", edate, expiry, variant, 307.70, 3.46, sp,
            credit=0.94, max_loss=406.0, resolve_after=expiry,
            credit_mid=1.02, fees=5.20, strike_step=5.0, lead_days=2,
            source="measurement")

    setup = SimpleNamespace(ticker="JBL", price=307.70, expected_move="8.34%",
                            next_earnings_date=edate, recommendation="CONSIDER")
    monkeypatch.setattr("signals.earnings_scanner.is_near_earnings", lambda *a, **k: True)

    def prices_nothing(_trader, _setup, _settings, diagnostics):
        diagnostics.update({"attempted": 5, "priced": 0,
                            "dropped": {"condor_1.0sd": "invalid_input"}})
        return []
    monkeypatch.setattr("signals.iv_variants.build_variants", prices_nothing)

    await main.log_variant_evals(setup, gate_passed=True, source="measurement")
    kept = await db._query("SELECT variant, lead_days FROM iv_variant_evals WHERE resolved=0")
    assert len(kept) == 2, "a failed re-price must not destroy the held capture"
    assert {r["lead_days"] for r in kept} == {2}

    # A partial quote pass still cannot replace the held capture: it would lose
    # the straddle row and bias the shape comparison toward liquid variants.
    def prices_fine(_trader, _setup, _settings, diagnostics):
        diagnostics.update({"attempted": 5, "priced": 1, "dropped": {}})
        return [{"variant": "condor_1.0sd", "expiry": expiry, "strikes": sp,
                 "credit": 2.10, "max_loss": 290.0, "credit_mid": 2.20,
                 "fees": 5.20, "strike_step": 5.0}]
    monkeypatch.setattr("signals.iv_variants.build_variants", prices_fine)
    await main.log_variant_evals(setup, gate_passed=True, source="measurement")
    now = await db._query("SELECT variant, lead_days, credit FROM iv_variant_evals WHERE resolved=0")
    assert len(now) == 2 and {r["credit"] for r in now} == {0.94}
    assert {r["lead_days"] for r in now} == {2}

    # Once every held shape has a valid replacement, swap the source cohort.
    def prices_complete(_trader, _setup, _settings, diagnostics):
        diagnostics.update({"attempted": 5, "priced": 2, "dropped": {}})
        return [
            {"variant": "condor_1.0sd", "expiry": expiry, "strikes": sp,
             "credit": 2.10, "max_loss": 290.0, "credit_mid": 2.20,
             "fees": 5.20, "strike_step": 5.0},
            {"variant": "straddle", "expiry": expiry, "strikes": sp,
             "credit": 4.10, "max_loss": None, "credit_mid": 4.20,
             "fees": 2.60, "strike_step": 5.0},
        ]
    monkeypatch.setattr("signals.iv_variants.build_variants", prices_complete)
    await main.log_variant_evals(setup, gate_passed=True, source="measurement")
    now = await db._query("SELECT variant, lead_days, credit, source FROM iv_variant_evals WHERE resolved=0")
    assert {r["variant"] for r in now} == {"condor_1.0sd", "straddle"}
    assert {r["lead_days"] for r in now} == {1}
    assert {r["source"] for r in now} == {"measurement"}


async def test_reprice_never_deletes_the_other_source_cohort(db):
    """Watchlist and measurement rows may share a ticker/event but are distinct
    experimental populations and must survive each other's refreshes."""
    sp = {"short_put": 90.0, "long_put": 87.0, "short_call": 110.0, "long_call": 113.0}
    for source in ("watchlist", "measurement"):
        await db.record_variant_eval(
            "MU", "2026-10-01", "2026-10-02", "condor_1.0sd", 100.0, 8.0, sp,
            credit=1.20, max_loss=180.0, resolve_after="2026-10-02",
            credit_mid=1.30, fees=5.20, strike_step=1.0, lead_days=2, source=source)

    assert await db.get_open_variant_lead("MU", "2026-10-01", "watchlist") == 2
    assert await db.get_open_variant_lead("MU", "2026-10-01", "measurement") == 2
    assert await db.delete_open_variant_evals("MU", "2026-10-01", "measurement") == 1
    remaining = await db._query("SELECT source FROM iv_variant_evals WHERE resolved=0")
    assert [r["source"] for r in remaining] == ["watchlist"]


async def test_reprice_is_not_attempted_outside_market_hours(db, monkeypatch):
    """The overnight passes had nothing to price. Defer instead of burning the
    capture on a doomed attempt."""
    import main
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "is_rth_now", lambda: False)
    sp = {"short_put": 300.0, "long_put": 295.0, "short_call": 320.0, "long_call": 325.0}
    await db.record_variant_eval(
        "JBL", "2026-09-30", "2026-10-02", "condor_1.0sd", 307.70, 3.46, sp,
        credit=0.94, max_loss=406.0, resolve_after="2026-10-02",
        credit_mid=1.02, fees=5.20, strike_step=5.0, lead_days=2,
        source="measurement")

    called = []
    monkeypatch.setattr("signals.iv_variants.build_variants",
                        lambda *a, **k: called.append(1) or [])
    monkeypatch.setattr("signals.earnings_scanner.is_near_earnings", lambda *a, **k: True)
    setup = SimpleNamespace(ticker="JBL", price=307.70, expected_move="8.34%",
                            next_earnings_date="2026-09-30", recommendation="CONSIDER")
    await main.log_variant_evals(setup, gate_passed=True, source="measurement")
    assert called == [], "should not even attempt to price outside RTH"
    assert len(await db._query("SELECT id FROM iv_variant_evals WHERE resolved=0")) == 1


def test_condor_rollup_sums_the_structures_own_legs():
    """Alpaca reports positions leg by leg. A condor's P&L is the sum of its four,
    and nothing was doing that addition — the report listed four unrelated option
    lines and could not answer "how is the MU condor doing".
    """
    from daily_report import _condor_rollups
    legs = json.dumps([{"symbol": "MU261002C01140000", "side": "sell"},
                       {"symbol": "MU261002C01170000", "side": "buy"},
                       {"symbol": "MU261002P00965000", "side": "sell"},
                       {"symbol": "MU261002P00935000", "side": "buy"}])
    condor = {"ticker": "MU", "qty": 2, "credit": 9.20, "max_loss": 2080.0,
              "expiry": "2026-10-02", "earnings_date": "2026-09-30",
              "long_put": 935.0, "short_put": 965.0,
              "short_call": 1140.0, "long_call": 1170.0, "legs_json": legs}
    positions = [{"symbol": "MU261002C01140000", "pnl": -200.0},
                 {"symbol": "MU261002C01170000", "pnl": 20.0},
                 {"symbol": "MU261002P00935000", "pnl": -490.0},
                 {"symbol": "MU261002P00965000", "pnl": 720.0},
                 {"symbol": "AAPL261002C00200000", "pnl": 9999.0}]   # not ours

    r = _condor_rollups([condor], positions)[0]
    assert r["unrealized_pnl"] == 50.0          # the unrelated position is excluded
    assert r["credit_collected"] == 1840.0      # $9.20 x 100 x 2
    assert r["max_risk"] == 4160.0              # per-spread max loss x qty
    assert r["pct_of_max_risk"] == 1.2
    assert r["legs_matched"] == 4 and r["legs_expected"] == 4
    assert r["partial"] is False

    # A leg assigned or closed separately makes the sum cover only part of the
    # structure. Report that rather than presenting a partial total as the P&L.
    r = _condor_rollups([condor], positions[:3])[0]
    assert r["partial"] is True
    assert r["unrealized_pnl"] is None
    assert r["pct_of_max_risk"] is None
    assert r["legs_matched"] == 3 and r["legs_expected"] == 4

    assert _condor_rollups([{**condor, "legs_json": "not json"}], positions)[0]["partial"]
    assert _condor_rollups([], positions) == []


async def test_report_reads_the_position_pnl_key_that_actually_exists(db):
    """AlpacaTrader.get_positions() returns "pnl". The report read "unrealized_pl",
    so every live position showed flat 0.0 P&L in both the card and the digest."""
    class PnlTrader(FakeTrader):
        def get_positions(self):
            return [{"symbol": "MU261002P00965000", "qty": -2.0, "pnl": 720.0,
                     "pnl_pct": 36.5, "market_val": -1250.0}]

    d = await build_report_data(db, PnlTrader())
    assert d["open_positions"][0]["pnl"] == 720.0, "must not silently report 0.0"
    assert d["open_positions"][0]["pnl_pct"] == 36.5


async def test_proposals_count_closed_condors_not_just_the_legacy_ledger(db):
    """Closed trades live in two tables. Counting only trade_performance made the
    digest report "no closed trades" while three winning condors sat in
    iv_condors — and then propose loosening a threshold to fix a drought that was
    not happening.
    """
    # Three closed, winning condors; nothing at all in the single-leg ledger.
    for i, (tk, pnl) in enumerate((("ORCL", 246.0), ("ADBE", 24.0), ("COST", 740.0)), 1):
        await db._exec(
            """INSERT INTO iv_condors (ticker, earnings_date, expiry, legs_json, qty,
                                       credit, max_loss, opened_at, status, pnl)
               VALUES (?,?,?,?,?,?,?,?,'closed',?)""",
            (tk, "2026-09-10", "2026-09-11", "[]", 1, 2.0, 300.0,
             "2026-09-08T10:00:00", pnl))

    d = await build_report_data(db, FakeTrader())
    text = " ".join(d["proposals"])
    assert d["metrics"]["closed_condors"] == 3
    assert d["metrics"]["closed_all"] == d["metrics"]["closed_trades"] + 3
    assert "No closed trades" not in text, f"still blind to condors: {text}"
    assert d["activity"]["tag"] != "QUIET"


async def test_no_trade_proposal_does_not_target_a_disabled_engine(db, monkeypatch):
    """With AUTO_TRADE_FLOW_ENABLED=false, lowering AUTO_TRADE_SCORE_THRESHOLD
    changes nothing — the flow engine it governs is switched off. Proposing it
    reads as actionable advice that cannot work."""
    import config

    real = config.get_settings

    def flow_off():
        s = real()
        object.__setattr__(s, "auto_trade_flow_enabled", False)
        return s
    monkeypatch.setattr(config, "get_settings", flow_off)

    # Nothing closed anywhere, and long enough running for the proposal to fire.
    await db._exec("INSERT INTO daily_equity (date, equity, open_equity) VALUES "
                   "('2026-09-01', 50000, 50000), ('2026-09-10', 50000, 50000)")
    d = await build_report_data(db, FakeTrader())
    text = " ".join(d["proposals"])
    if "No closed trades" in text:
        assert "AUTO_TRADE_FLOW_ENABLED=false" in text
        assert "would change nothing" in text
        assert "IV/earnings condor" in text


def test_close_debit_refuses_a_one_sided_or_missing_book():
    """The debit drives BOTH the profit-target decision and the submitted limit.
    Reading `mid` with a 0 default let an untradeable leg pull it toward zero —
    faking a TP and setting a limit too low to fill, from data shaped like price.
    """
    from main import _condor_close_debit, _condor_wing_width
    legs = [{"symbol": "SC"}, {"symbol": "LC"}, {"symbol": "SP"}, {"symbol": "LP"}]

    def q(**kw):
        return {s: {"bid": b, "ask": a, "mid": (b + a) / 2 if b and a else (b or a)}
                for s, (b, a) in kw.items()}

    full = q(SC=(6.0, 6.4), LC=(2.0, 2.2), SP=(3.0, 3.4), LP=(1.0, 1.2))
    # (6.2 + 3.2) - (2.1 + 1.1)
    assert _condor_close_debit(legs, full) == pytest.approx(6.2)

    # A missing leg used to contribute 0 and understate the debit.
    missing = dict(full); del missing["SP"]
    assert _condor_close_debit(legs, missing) is None
    # One-sided is just as bad: get_option_quotes falls back to `bid or ask`.
    one_sided = q(SC=(6.0, 6.4), LC=(2.0, 2.2), SP=(3.0, 0.0), LP=(1.0, 1.2))
    assert _condor_close_debit(legs, one_sided) is None
    assert _condor_close_debit(legs, {}) is None

    # Wing width is the guaranteed-fill ceiling for the forced expiry close.
    assert _condor_wing_width({"short_put": 965.0, "long_put": 935.0,
                               "long_call": 1170.0, "short_call": 1140.0}) == 30.0
    assert _condor_wing_width({}) == 0.0


async def test_condor_close_defers_outside_rth_but_never_past_expiry(db, monkeypatch):
    """Exit pricing must come off a live session, except on expiry day: an
    unclosed short leg through expiry is assignment, which beats a poor fill."""
    import main
    monkeypatch.setattr(main, "db", db)

    legs = [{"symbol": "SC", "side": "sell", "ratio_qty": 1},
            {"symbol": "LC", "side": "buy", "ratio_qty": 1},
            {"symbol": "SP", "side": "sell", "ratio_qty": 1},
            {"symbol": "LP", "side": "buy", "ratio_qty": 1}]
    submitted = []

    class ExitTrader:
        def get_order_raw(self, _oid):
            return {"status": "filled", "filled_qty": 2, "filled_avg_price": -9.20}

        def get_option_quotes(self, symbols):
            # One-sided book, as after hours: no trustworthy mid.
            return {s: {"bid": 1.0, "ask": 0.0, "mid": 1.0} for s in symbols}

        def close_multileg(self, legs_, qty, limit, tif="day"):
            submitted.append(limit)
            return {"id": "close-1"}

    monkeypatch.setattr(main, "trader", ExitTrader())
    row = {"id": 1, "ticker": "MU", "qty": 2, "credit": 9.20, "status": "open",
           "entry_status": "filled", "entry_order_id": "e1",
           "earnings_date": "2026-09-30", "expiry": "2026-10-02",
           "legs_json": json.dumps(legs), "short_put": 965.0, "long_put": 935.0,
           "short_call": 1140.0, "long_call": 1170.0}

    # Post-earnings, but outside RTH → defer rather than price off a dead book.
    monkeypatch.setattr(main, "is_rth_now", lambda: False)
    await main._manage_condor(dict(row))
    assert submitted == [], "must not submit a close priced off an overnight book"

    # In RTH but still one-sided → the debit is not computable, so still defer.
    monkeypatch.setattr(main, "is_rth_now", lambda: True)
    await main._manage_condor(dict(row))
    assert submitted == [], "a one-sided book is not a price"

    # Expiry day, past the force hour → close anyway, capped at the wing width.
    import market_time
    monkeypatch.setattr(main, "is_rth_now", lambda: False)
    monkeypatch.setattr(market_time, "et_today", lambda *a: _dtdate(2026, 10, 2))
    monkeypatch.setattr(market_time, "et_now",
                        lambda *a: _dtdatetime(2026, 10, 2, 15, 30, tzinfo=market_time.ET))
    await main._manage_condor(dict(row))
    assert submitted == [30.0], f"expected wing-width limit, got {submitted}"

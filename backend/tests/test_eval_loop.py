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
import pytest
import pytest_asyncio

from db import Database, _is_occ, _et_hour, _minutes_between
from daily_report import build_report_data, build_watchlist_review, export_history

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


# ── Weekly watchlist review ─────────────────────────────────────────────
async def test_watchlist_review_add_and_remove(db):
    now = "2026-09-03T14:30:00.000000"
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


# ── IV/RV Phase 2 execution: iron-condor builder + DB lifecycle ──────────
import json as _json
import types as _types
from datetime import date as _date, timedelta as _timedelta


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
    exp = _date.today() + _timedelta(days=1)          # front expiry, day after the print
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
        next_earnings_date=(_date.today()).isoformat())   # prints today → expiry tomorrow
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


def test_build_iron_condor_rejects_far_earnings():
    from signals.iv_executor import build_iron_condor
    from config import get_settings
    exp = _date.today() + _timedelta(days=1)
    trader = FakeOptionTrader(exp, [float(k) for k in range(80, 121)], {}, equity=50000.0)
    setup = _types.SimpleNamespace(
        ticker="TEST", price=100.0, expected_move="10.0%", recommendation="SELL_PREMIUM",
        next_earnings_date=(_date.today() + _timedelta(days=40)).isoformat())
    plan = build_iron_condor(trader, setup, 50000.0, get_settings())
    assert not plan["ok"] and "DTE band" in plan["reason"]


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

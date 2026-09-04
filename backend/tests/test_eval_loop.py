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

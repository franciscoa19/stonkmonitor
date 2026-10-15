"""Market-event days are tagged, never deleted: results are shown both ways.

Temporary ledgers only.
"""
import json
from types import SimpleNamespace as NS

import httpx
import pytest
from fastapi import FastAPI

from api.routes import router
from daily_report import build_report_data, render_html
from test_execution_safety import database, session  # noqa: F401  (fixtures)
from test_account_limits import settings
from test_sizing_fixes import balance

CPI = "2026-10-14"


async def condor(db, ticker, opened, closed, pnl, expiry="2026-10-16"):
    legs = json.dumps([{"symbol": f"{ticker}{i}", "side": side, "ratio_qty": 1}
                       for i, side in enumerate(("sell", "buy", "sell", "buy"))])
    cid = await db.record_condor(ticker, opened, expiry, legs,
                                 {"short_put": 90, "long_put": 85, "short_call": 110, "long_call": 115},
                                 1, 1, 400, "entry", "filled")
    await db.activate_condor(cid, 1, -1)
    await db.close_condor(cid, 1 - pnl / 100, pnl)
    await db._exec("UPDATE iv_condors SET opened_at=?, closed_at=? WHERE id=?",
                   (f"{opened}T14:00:00", f"{closed}T14:00:00", cid), strict=True)


async def iv_eval(db, ticker, signal, resolve_after, win):
    from signals.iv_research import measurement_session
    await db.record_iv_eval(ticker, "SELL_PREMIUM", 1.5, 4.0, 100.0, resolve_after, signal)
    row = (await db._query("SELECT id FROM iv_rv_evals WHERE ticker=?", (ticker,)))[0]
    await db._exec("UPDATE iv_rv_evals SET signal_date=? WHERE id=?", (signal, row["id"]), strict=True)
    session = measurement_session({"resolve_after": resolve_after, "earnings_date": signal, "signal_date": signal})
    await db.resolve_iv_eval(row["id"], 101.0 if win else 110.0, 1.0 if win else 10.0, 4.0,
                             measurement_session=session)


async def variant(db, ticker, signal, expiry, pnl, recommendation="SELL_PREMIUM"):
    await db._exec(
        """INSERT INTO iv_variant_evals (ticker, earnings_date, signal_date, expiry, variant, credit,
               pricing_model, max_loss, gate_passed, recommendation, source, resolve_after, resolved,
               realized_pnl, win, created_at)
           VALUES (?,?,?,?,'condor_1.0sd',1.0,'conservative_bid_ask_v2',400,?,?,'watchlist',?,1,?,?,?)""",
        (ticker, signal, signal, expiry, 1 if recommendation == "SELL_PREMIUM" else 0, recommendation,
         expiry, pnl, 1 if pnl > 0 else 0, f"{signal}T14:00:00"), strict=True)


async def history(db):
    """PEP and UNH were closed before the CPI day; WFC and MS were open across it."""
    await condor(db, "PEP", "2026-10-06", "2026-10-09", 180.0, expiry="2026-10-09")
    await condor(db, "UNH", "2026-10-12", "2026-10-13", 1160.0)
    await condor(db, "WFC", "2026-10-12", "2026-10-14", -880.0)
    await iv_eval(db, "PEP", "2026-10-05", "2026-10-10", win=True)
    await iv_eval(db, "MS", "2026-10-12", "2026-10-16", win=False)
    await variant(db, "STZ", "2026-10-06", "2026-10-09", -238.0)
    await variant(db, "PEP", "2026-10-07", "2026-10-09", 50.0)
    await variant(db, "MS", "2026-10-13", "2026-10-16", -300.0)


# ── The ledger ──────────────────────────────────────────────────────────────
async def test_events_are_kept_listed_relabelled_and_removed(database):
    await database.add_market_event(CPI, "CPI", "hot print")
    await database.add_market_event("2026-10-28", "FOMC")
    await database.add_market_event(CPI, "CPI", "hot print; broad selloff")       # the same day again: updated
    events = await database.get_market_events()
    assert [(e["date"], e["label"], e["note"]) for e in events] == [
        (CPI, "CPI", "hot print; broad selloff"), ("2026-10-28", "FOMC", None)]
    assert await database.remove_market_event("2026-10-28") is True
    assert await database.remove_market_event("2026-10-28") is False
    assert [e["date"] for e in await database.get_market_events()] == [CPI]


async def test_with_nothing_tagged_the_figures_are_unchanged(database):
    await history(database)
    assert await database.get_condor_summary(exclude_market_events=True) == await database.get_condor_summary()
    assert await database.get_iv_eval_summary(exclude_market_events=True) == await database.get_iv_eval_summary()
    assert (await database.get_gate_comparison(exclude_market_events=True)
            == await database.get_gate_comparison())


async def test_records_open_across_a_tagged_day_are_left_out_only_when_asked(database):
    await history(database)
    await database.add_market_event(CPI, "CPI")
    everything = await database.get_condor_summary()
    without = await database.get_condor_summary(exclude_market_events=True)
    assert (everything["closed"], everything["total_pnl"]) == (3, 460.0)          # nothing deleted
    assert (without["closed"], without["total_pnl"]) == (2, 1340.0)               # WFC was open on the 14th
    assert (await database.get_iv_eval_summary())["resolved"] == 2
    assert (await database.get_iv_eval_summary(exclude_market_events=True))["resolved"] == 1   # MS spans it
    gated = await database.get_gate_comparison()
    gated_without = await database.get_gate_comparison(exclude_market_events=True)
    assert gated["gated"]["n_events"] == 3 and gated_without["gated"]["n_events"] == 2
    variants = {v["variant"]: v for v in await database.get_variant_summary(exclude_market_events=True)}
    assert variants["condor_1.0sd"]["n"] == 2


@pytest.mark.parametrize("opened,closed,spans", [
    ("2026-10-14", "2026-10-14", True), ("2026-10-13", "2026-10-15", True),
    ("2026-10-12", "2026-10-13", False), ("2026-10-15", "2026-10-16", False)])
async def test_a_window_counts_when_the_tagged_day_is_inside_it_inclusively(database, opened, closed, spans):
    await condor(database, "X", opened, closed, 100.0)
    await database.add_market_event(CPI, "CPI")
    assert (await database.get_condor_summary(exclude_market_events=True))["closed"] == (0 if spans else 1)


# ── The report ──────────────────────────────────────────────────────────────
def broker():
    return NS(paper=True, get_account=lambda: balance(), get_positions=lambda: [],
              get_positions_raw=lambda: [], get_open_orders_raw=lambda: [])


async def test_the_report_shows_both_ways_and_says_nothing_was_removed(database):
    await history(database)
    data = await build_report_data(database, broker(), settings=settings())
    assert data["market_events"] == [] and data["excluding_market_events"] is None
    assert "Market-event days" not in render_html(data)

    await database.add_market_event(CPI, "CPI", "hot print")
    data = await build_report_data(database, broker(), settings=settings())
    assert data["iv_condors"]["total_pnl"] == 460.0                                # headline untouched
    assert data["excluding_market_events"]["iv_condors"]["total_pnl"] == 1340.0
    html = render_html(data)
    assert "Market-event days" in html and "2026-10-14 (CPI)" in html
    assert "$+460" in html and "$+1,340" in html and "Nothing is removed" in html


async def test_a_label_is_escaped_in_the_report(database):
    await database.add_market_event(CPI, "<script>x</script>")
    html = render_html(await build_report_data(database, broker(), settings=settings()))
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html


# ── The API ─────────────────────────────────────────────────────────────────
async def client_for(session, monkeypatch, database):
    monkeypatch.setattr(session, "db", database)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")


async def test_events_can_be_tagged_listed_and_removed_over_the_api(database, session, monkeypatch):
    async with await client_for(session, monkeypatch, database) as http:
        reply = await http.post("/api/market-events", json={"date": CPI, "label": " CPI ", "note": "hot print"})
        assert reply.status_code == 200 and reply.json()["events"][0]["label"] == "CPI"
        assert (await http.get("/api/market-events")).json()["events"][0]["date"] == CPI
        assert (await http.delete(f"/api/market-events/{CPI}")).json()["events"] == []
        assert (await http.delete(f"/api/market-events/{CPI}")).status_code == 404


@pytest.mark.parametrize("body", [
    {"date": "2026-13-01", "label": "CPI"}, {"date": "2026-02-30", "label": "CPI"},
    {"date": "10/14/2026", "label": "CPI"}, {"date": CPI, "label": ""}, {"date": CPI, "label": "x" * 41},
    {"date": CPI, "label": "CPI", "note": "x" * 201}, {"date": CPI}, {"label": "CPI"}])
async def test_a_bad_tag_is_refused(database, session, monkeypatch, body):
    async with await client_for(session, monkeypatch, database) as http:
        assert (await http.post("/api/market-events", json=body)).status_code == 422
    assert await database.get_market_events() == []

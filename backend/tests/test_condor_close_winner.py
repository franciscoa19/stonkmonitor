"""Closing a condor that worked.

A winning condor's wings are worthless and have no bid, so a four-leg close
cannot fill: NKE (2026-10-02) and PEP (10-08) both sat unfilled at limits above
the quoted cost. The close now leaves an unsellable wing out, and the wings it
leaves behind are tracked from the close fills. Fake brokers, temporary ledgers.
"""
import json
import logging
import sqlite3
from datetime import date
from types import SimpleNamespace as NS

import pytest

from db import Database, DatabaseError
from trading.account_risk import account_limits
from trading.ownership import option_ownership
from test_execution_safety import database, session  # noqa: F401  (fixtures)
from test_account_limits import settings
from test_sizing_fixes import balance

LATER = "2026-12-04"                      # not the session fixture's "today" (2026-11-27)
# Real OCC symbols: the ownership check ignores anything that is not an option.
SC, LC = "TEST261204C00110000", "TEST261204C00115000"
SP, LP = "TEST261204P00090000", "TEST261204P00085000"
SHORTS, WINGS = [SC, SP], [LC, LP]


async def condor(db, expiry=LATER, qty=2):
    """A filled 90/85 put – 110/115 call condor: $1.00 credit, $400 max loss."""
    legs = [{"symbol": symbol, "side": side, "ratio_qty": 1}
            for symbol, side in ((SC, "sell"), (LC, "buy"), (SP, "sell"), (LP, "buy"))]
    cid = await db.record_condor("TEST", "2026-11-25", expiry, json.dumps(legs),
        {"short_put": 90, "long_put": 85, "short_call": 110, "long_call": 115},
        qty, 1, 400, "entry", "filled")
    await db.activate_condor(cid, qty, -1)
    return (await db.get_active_condors())[0]


def book(lc_bid=0.0, lp_bid=0.0, sc=(0.07, 0.10), sp=(0.04, 0.07)):
    """A winner by default: cheap shorts, wings nobody bids for."""
    return {SC: {"bid": sc[0], "ask": sc[1]}, SP: {"bid": sp[0], "ask": sp[1]},
            LC: {"bid": lc_bid, "ask": max(lc_bid, 0) + 0.01},
            LP: {"bid": lp_bid, "ask": max(lp_bid, 0) + 0.01}}


class Broker:
    """Records every close it is asked to send; fills when told to."""

    def __init__(self, quotes=None, results=None):
        self.quotes = book() if quotes is None else quotes
        self.results = list(results or [])
        self.sent, self.cancelled, self.orders = [], [], {}

    def get_option_quotes(self, symbols):
        return {s: self.quotes[s] for s in symbols if s in self.quotes}

    def close_multileg(self, legs, qty, limit, client_order_id=None):
        self.sent.append({"legs": [l["symbol"] for l in legs], "qty": qty, "limit": limit,
                          "client": client_order_id})
        if self.results:
            return self.results.pop(0)
        oid = f"close-{len(self.sent)}"
        self.orders[oid] = {"status": "new", "filled_qty": "0", "limit_price": str(limit),
                            "created_at": "2026-11-27T17:30:00Z"}
        return {"id": oid, "status": "new"}

    def get_order_raw(self, oid):
        return self.orders[oid]

    def cancel_order_raw(self, oid):
        self.cancelled.append(oid)
        return True

    def get_order_by_client_id(self, _client):
        return {"not_found": True}

    def fill(self, oid, qty, price, status="filled"):
        self.orders[oid] = {**self.orders[oid], "status": status, "filled_qty": str(qty),
                            "filled_avg_price": str(price)}


async def manage(main, db):
    for row in await db.get_active_condors():
        await main._manage_condor(row)


async def the_condor(db):
    return (await db._query("SELECT * FROM iv_condors ORDER BY id LIMIT 1"))[0]


@pytest.fixture
def desk(database, session, monkeypatch):
    def make(broker):
        monkeypatch.setattr(session, "db", database)
        monkeypatch.setattr(session, "trader", broker)
        return session
    return make


# ── Which legs a close can trade ────────────────────────────────────────────
LEGS = [{"symbol": SC, "side": "sell"}, {"symbol": LC, "side": "buy"},
        {"symbol": SP, "side": "sell"}, {"symbol": LP, "side": "buy"}]


@pytest.mark.parametrize("quotes,expected", [
    (book(), [SC, SP]),                                  # both wings worthless
    (book(lc_bid=0.05), [SC, LC, SP]),                 # the call wing can still be sold
    (book(lp_bid=0.02), [SC, SP, LP]),
    (book(lc_bid=0.4, lp_bid=0.3), [SC, LC, SP, LP]),   # a loser: nothing changes
])
def test_a_wing_is_left_out_only_when_nobody_bids_for_it(session, quotes, expected):
    assert [l["symbol"] for l in session._condor_close_legs(LEGS, quotes)] == expected


@pytest.mark.parametrize("quotes", [
    {}, {SC: {"bid": 1, "ask": 1}},                         # wings missing from the book
    {**book(), LC: {"ask": 0.01}}, {**book(), LC: {"bid": None, "ask": 0.01}},
    {**book(), LC: {"bid": "bad"}}, {**book(), LC: {"bid": float("nan")}},
    {**book(), LP: {"bid": -0.01}}])
def test_an_unreadable_wing_changes_nothing(session, quotes):
    assert session._condor_close_legs(LEGS, quotes) == LEGS


def test_a_short_leg_is_never_left_out(session):
    worthless_everything = {s: {"bid": 0.0, "ask": 0.01} for s in (SC, LC, SP, LP)}
    assert [l["symbol"] for l in session._condor_close_legs(LEGS, worthless_everything)] == SHORTS
    malformed = [{"symbol": SC, "side": "sell"}, {"symbol": LC, "side": "buy"},
                 {"symbol": SP, "side": "buy"}, {"symbol": LP, "side": "buy"}]      # only one short
    assert session._condor_close_legs(malformed, book()) == malformed


# ── The close order ─────────────────────────────────────────────────────────
async def test_a_winner_is_closed_by_buying_back_the_short_legs_only(database, desk):
    await condor(database, expiry=LATER)
    broker = Broker()
    main = desk(broker)
    await manage(main, database)
    assert broker.sent[0]["legs"] == SHORTS and broker.sent[0]["qty"] == 2
    assert broker.sent[0]["limit"] == 0.18              # $0.17 at the touch + one tick, as before
    row = await the_condor(database)
    assert row["status"] == "closing" and row["close_order_id"] == "close-1"
    assert [l["symbol"] for l in json.loads(row["close_legs_json"])] == SHORTS
    assert row["close_client_order_id"] == broker.sent[0]["client"]


async def test_its_fill_closes_the_condor_and_leaves_the_wings_on_record(database, desk):
    await condor(database, expiry=LATER)
    broker = Broker()
    main = desk(broker)
    await manage(main, database)
    broker.fill("close-1", 2, 0.17)
    await manage(main, database)
    row = await the_condor(database)
    assert row["status"] == "closed" and row["closed_qty"] == 2
    assert row["pnl"] == pytest.approx((1.00 - 0.17) * 100 * 2) and row["exit_debit"] == 0.17
    assert await database.get_active_condors() == []
    assert await database.get_condor_residual_legs() == {LC: 2, LP: 2}
    assert len(broker.sent) == 1                        # nothing further is sent for the wings


async def test_a_loser_still_closes_all_four_legs(database, desk):
    await condor(database, expiry=LATER)
    broker = Broker(book(lc_bid=0.40, lp_bid=0.30, sc=(3.9, 4.0), sp=(0.5, 0.6)))
    main = desk(broker)
    await manage(main, database)
    assert broker.sent[0]["legs"] == [SC, LC, SP, LP]
    assert (await the_condor(database))["close_legs_json"] is None
    broker.fill("close-1", 2, 3.9)
    await manage(main, database)
    assert (await the_condor(database))["status"] == "closed"
    assert await database.get_condor_residual_legs() == {}


async def test_one_sellable_wing_is_sold_and_only_the_other_is_left(database, desk):
    await condor(database, expiry=LATER)
    broker = Broker(book(lc_bid=0.05))
    main = desk(broker)
    await manage(main, database)
    assert broker.sent[0]["legs"] == [SC, LC, SP]
    broker.fill("close-1", 2, 0.12)
    await manage(main, database)
    assert await database.get_condor_residual_legs() == {LP: 2}


async def test_without_a_book_the_forced_close_is_unchanged(database, desk, monkeypatch):
    await condor(database, expiry="2026-11-27")           # expires on the fixture's "today"
    broker = Broker(quotes={})
    main = desk(broker)
    monkeypatch.setattr(main, "_expiry_force_close_due", lambda *a, **kw: True)
    await manage(main, database)
    assert broker.sent[0]["legs"] == [SC, LC, SP, LP] and broker.sent[0]["limit"] == 5.0
    assert (await the_condor(database))["close_legs_json"] is None


async def test_a_forced_close_of_a_winner_also_leaves_the_dead_wings_out(database, desk, monkeypatch):
    await condor(database, expiry="2026-11-27")
    broker = Broker()
    main = desk(broker)
    monkeypatch.setattr(main, "_expiry_force_close_due", lambda *a, **kw: True)
    monkeypatch.setattr(main, "_condor_expiry_hold", lambda *a, **kw: False)
    await manage(main, database)
    assert broker.sent[0]["legs"] == SHORTS and broker.sent[0]["limit"] == 5.0     # wing-width ceiling


# ── Refusals ────────────────────────────────────────────────────────────────
async def test_a_refused_reduced_order_falls_back_to_all_four_legs(database, desk):
    """If the broker will not take the two-leg order, behave exactly as before."""
    await condor(database, expiry=LATER)
    broker = Broker(results=[{"error": "invalid legs", "ambiguous": False}])
    main = desk(broker)
    await manage(main, database)
    assert [s["legs"] for s in broker.sent] == [SHORTS, [SC, LC, SP, LP]]
    assert broker.sent[0]["client"] != broker.sent[1]["client"]
    row = await the_condor(database)
    assert row["status"] == "closing" and row["close_legs_json"] is None
    assert row["close_client_order_id"] == broker.sent[1]["client"]


async def test_an_uncertain_reduced_order_is_tracked_not_resent(database, desk):
    await condor(database, expiry=LATER)
    broker = Broker(results=[{"error": "timeout", "ambiguous": True}])
    main = desk(broker)
    await manage(main, database)
    assert len(broker.sent) == 1
    row = await the_condor(database)
    assert row["status"] == "closing" and row["close_order_id"] is None
    assert row["close_client_order_id"] == broker.sent[0]["client"]
    assert [l["symbol"] for l in json.loads(row["close_legs_json"])] == SHORTS


async def test_both_orders_refused_leaves_the_condor_open_for_the_next_cycle(database, desk):
    await condor(database, expiry=LATER)
    broker = Broker(results=[{"error": "no", "ambiguous": False}, {"error": "no", "ambiguous": False}])
    main = desk(broker)
    await manage(main, database)
    assert len(broker.sent) == 2 and (await the_condor(database))["status"] == "open"


# ── Partial fills across differently shaped orders ──────────────────────────
async def test_partial_fills_of_different_shapes_add_up(database, desk):
    await condor(database, expiry=LATER, qty=4)
    broker = Broker()
    main = desk(broker)
    await manage(main, database)                          # short legs only, 4
    broker.fill("close-1", 1, 0.17, status="canceled")    # one fills, then it is cancelled
    broker.quotes = book(lc_bid=0.05, lp_bid=0.05)        # the wings find a bid
    await manage(main, database)                          # books the 1, resubmits the other 3
    assert broker.sent[1] == {**broker.sent[1], "legs": [SC, LC, SP, LP], "qty": 3}
    row = await the_condor(database)
    assert row["closed_qty"] == 1 and row["close_legs_json"] is None
    assert await database.get_condor_residual_legs() == {LC: 1, LP: 1}
    broker.fill("close-2", 3, 0.07)
    await manage(main, database)
    row = await the_condor(database)
    assert row["status"] == "closed" and row["closed_qty"] == 4
    assert row["pnl"] == pytest.approx(((1 - 0.17) * 1 + (1 - 0.07) * 3) * 100)
    assert await database.get_condor_residual_legs() == {LC: 1, LP: 1}    # only the first order's


async def test_a_repeated_fill_report_keeps_the_legs_it_was_first_recorded_with(database):
    row = await condor(database, expiry=LATER, qty=4)
    shorts = json.dumps([l for l in json.loads(row["legs_json"]) if l["side"] == "sell"])
    await database.record_condor_close_fill(row["id"], "o1", 1, 0.17, legs_json=shorts)
    await database.record_condor_close_fill(row["id"], "o1", 3, 0.17)          # cumulative update
    assert await database.get_condor_residual_legs() == {LC: 3, LP: 3}
    await database.record_condor_close_fill(row["id"], "o2", 1, 0.20)          # a four-leg close
    assert await database.get_condor_residual_legs(row["id"]) == {LC: 3, LP: 3}


# ── The rest of the bot recognises the leftover wings ───────────────────────
async def closed_winner(database, desk, qty=2):
    await condor(database, expiry=LATER, qty=qty)
    broker = Broker()
    main = desk(broker)
    await manage(main, database)
    broker.fill("close-1", qty, 0.17)
    await manage(main, database)
    return main, broker


def holding(symbol, qty, value=0.0):
    return {"symbol": symbol, "qty": str(qty), "market_value": str(value)}


async def test_leftover_wings_are_ours_not_unknown_holdings(database, desk):
    await closed_winner(database, desk)
    b = NS(get_positions_raw=lambda: [holding(LC, 2), holding(LP, 2)], get_open_orders_raw=lambda: [])
    got = await option_ownership(database, b)
    assert got["entry_block_reason"] == ""               # new entries are not blocked
    assert {LC, LP} <= got["protected_symbols"]      # and they never get a single-leg exit


@pytest.mark.parametrize("held", [
    [holding(LC, 3), holding(LP, 2)],                # more than the close left behind
    [holding(LC, -2), holding(LP, 2)],               # the wrong way round
    [holding(LC, 2), holding(LP, 2), holding("ZZZ261204C00100000", 1)]])   # something else entirely
async def test_anything_beyond_the_leftover_wings_still_blocks(database, desk, held):
    await closed_winner(database, desk)
    b = NS(get_positions_raw=lambda: held, get_open_orders_raw=lambda: [])
    assert (await option_ownership(database, b))["entry_block_reason"]


async def test_a_partly_closed_condor_owns_its_attached_and_leftover_wings(database, desk):
    await condor(database, expiry=LATER, qty=4)
    broker = Broker()
    main = desk(broker)
    await manage(main, database)
    broker.fill("close-1", 1, 0.17, status="partially_filled")
    await manage(main, database)                          # books 1; three condors remain whole
    held = [holding(SC, -3), holding(SP, -3), holding(LC, 4), holding(LP, 4)]
    b = NS(get_positions_raw=lambda: held, get_open_orders_raw=lambda: [])
    b.get_open_orders_raw = lambda: [{"id": "close-1", "order_class": "mleg", "client_order_id": broker.sent[0]["client"],
                                      "legs": [{"symbol": SC}, {"symbol": SP}]}]
    assert (await option_ownership(database, b))["entry_block_reason"] == ""


async def test_a_closed_winner_frees_its_risk_and_its_slot(database, desk):
    await closed_winner(database, desk)
    assert await database.count_open_condors() == 0 and not await database.has_open_condor("TEST")
    held = [holding(LC, 2, 4.0), holding(LP, 2, 2.0)]
    got = await account_limits(database, balance(), held, [], settings())
    assert got["condor_risk"] == 0 and got["position_risk"] == 6.0      # the wings at what they are worth


async def test_leftover_wings_stop_being_recognised_once_their_expiry_is_long_past(database, desk, monkeypatch):
    import market_time
    await closed_winner(database, desk)
    assert await database.get_condor_residual_legs(min_expiry="2026-12-04") == {LC: 2, LP: 2}
    assert await database.get_condor_residual_legs(min_expiry="2026-12-05") == {}
    monkeypatch.setattr(market_time, "et_today", lambda *a: date(2026, 12, 20))
    b = NS(get_positions_raw=lambda: [holding(LC, 2)], get_open_orders_raw=lambda: [])
    assert (await option_ownership(database, b))["entry_block_reason"]


async def test_a_fill_that_claims_to_have_left_a_short_open_fails_closed(database):
    row = await condor(database, expiry=LATER)
    wings_only = json.dumps([l for l in json.loads(row["legs_json"]) if l["side"] == "buy"])
    await database.record_condor_close_fill(row["id"], "bad", 1, 0.1, legs_json=wings_only)
    with pytest.raises(DatabaseError):
        await database.get_condor_residual_legs()
    with pytest.raises(DatabaseError):
        await option_ownership(database, NS(get_positions_raw=lambda: [], get_open_orders_raw=lambda: []))


@pytest.mark.parametrize("bad", ["not json", "[{}]", '[{"symbol": 5}]'])
async def test_unreadable_close_legs_fail_closed(database, bad):
    row = await condor(database, expiry=LATER)
    await database.record_condor_close_fill(row["id"], "bad", 1, 0.1, legs_json=bad)
    with pytest.raises(DatabaseError):
        await database.get_condor_residual_legs()


# ── Expiry of what is left ──────────────────────────────────────────────────
async def test_expiry_settles_a_partly_closed_condor_with_its_leftover_wings(database, desk, monkeypatch):
    """Three whole condors and one leftover pair of wings expire together: the
    broker reports 3 of each short and 4 of each wing. Expecting 3 of every leg
    would never match, and the condor would wait for settlement for ever."""
    import market_time
    from datetime import datetime
    row = await condor(database, expiry=LATER, qty=4)
    shorts = json.dumps([l for l in json.loads(row["legs_json"]) if l["side"] == "sell"])
    await database.record_condor_close_fill(row["id"], "earlier-close", 1, 0.17, legs_json=shorts)
    assert await database.get_condor_residual_legs(row["id"]) == {LC: 1, LP: 1}

    broker = Broker()
    main = desk(broker)
    after = datetime(2026, 12, 5, 10, 0, tzinfo=market_time.ET)
    monkeypatch.setattr(market_time, "et_today", lambda *a: after.date())
    monkeypatch.setattr(market_time, "et_now", lambda *a: after)
    broker.get_positions_raw = lambda: []

    def activities(expired):
        return lambda _after: [
            {"id": f"x-{s}", "activity_type": "OPEXP", "status": "executed", "date": LATER,
             "symbol": s, "qty": str(-q)} for s, q in expired.items()]

    broker.get_option_activities = activities({SC: 3, SP: 3, LC: 3, LP: 3})   # wings one short
    await manage(main, database)
    assert (await the_condor(database))["status"] == "awaiting_settlement"

    broker.get_option_activities = activities({SC: 3, SP: 3, LC: 4, LP: 4})
    await manage(main, database)
    row = await the_condor(database)
    assert row["status"] == "closed" and broker.sent == []
    assert row["pnl"] == pytest.approx((1 - 0.17) * 100 * 1 + 1.00 * 100 * 3)


# ── A leftover wing that became worth something ─────────────────────────────
@pytest.mark.parametrize("bid,warned", [(0.0, False), (0.04, False), (0.05, True), (1.20, True)])
async def test_a_leftover_wing_with_a_real_bid_is_reported_and_never_traded(
        database, desk, caplog, bid, warned):
    main, broker = await closed_winner(database, desk)
    broker.get_positions_raw = lambda: [holding(LC, 2), holding(LP, 2)]
    broker.quotes = book(lc_bid=bid)
    with caplog.at_level(logging.WARNING, logger="main"):
        await main._note_valuable_leftover_wings()
    mentioned = [r.message for r in caplog.records if "Leftover wing" in r.message]
    assert bool(mentioned) is warned and all(LC in m for m in mentioned)
    assert len(broker.sent) == 1 and broker.cancelled == []


async def test_no_leftover_check_reads_the_broker_when_there_is_nothing_left_over(database, desk):
    await condor(database, expiry=LATER)
    broker = Broker()
    main = desk(broker)
    broker.get_positions_raw = lambda: pytest.fail("no leftover wings: no broker read")
    await main._note_valuable_leftover_wings()


# ── Existing ledgers ────────────────────────────────────────────────────────
async def test_an_older_ledger_gains_the_columns_and_keeps_its_fills(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path)
    await db.connect()
    row = await condor(db, expiry=LATER)
    await db.record_condor_close_fill(row["id"], "old-order", 1, 0.5)
    await db.close()
    with sqlite3.connect(path) as conn:                  # put the tables back as they were
        conn.execute("ALTER TABLE iv_condor_close_fills DROP COLUMN legs_json")
        conn.execute("ALTER TABLE iv_condors DROP COLUMN close_legs_json")
    for _ in range(2):                                   # migrating twice changes nothing
        await db.connect()
        try:
            fills = await db._query("SELECT * FROM iv_condor_close_fills")
            assert fills == [{"order_id": "old-order", "condor_id": row["id"], "filled_qty": 1,
                              "debit": 0.5, "legs_json": None}]
            assert (await db._query("SELECT close_legs_json FROM iv_condors"))[0]["close_legs_json"] is None
            assert await db.get_condor_residual_legs() == {}    # an old four-leg fill left nothing behind
        finally:
            await db.close()

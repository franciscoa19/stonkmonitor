"""An entry the broker never received is voided once its session has closed.

AXP #17 (2026-10-21): the entry POST timed out at 11:20 ET and Alpaca never
saw it. The bot rightly never resends an ambiguous submission, so it looked the
order up by its client ID every cycle — 404 each time — and the row held one of
the three slots until it was voided by hand. An entry is a day order: after its
session closes, a 404 cannot be an order still on its way. Fake brokers,
temporary ledgers.
"""
import json
from datetime import datetime

import pytest

import market_time
from test_execution_safety import database, session  # noqa: F401  (fixtures)
from test_account_limits import settings
from test_condor_close_winner import SC, LC, SP, LP

LEGS = [SC, LC, SP, LP]
NOT_FOUND = {"error": "order not found", "not_found": True}


async def unsent(db, opened_at="2026-11-25T16:20:00"):           # 11:20 ET on Wednesday 11-25
    legs = [{"symbol": s, "side": side, "ratio_qty": 1}
            for s, side in ((SC, "sell"), (LC, "buy"), (SP, "sell"), (LP, "buy"))]
    cid = await db.record_condor("TEST", "2026-12-02", "2026-12-04", json.dumps(legs),
                                 {"short_put": 90, "long_put": 85, "short_call": 110, "long_call": 115},
                                 3, 1.0, 400, None, "submitting")
    await db._exec("UPDATE iv_condors SET opened_at=? WHERE id=?", (opened_at, cid), strict=True)
    return cid


class Broker:
    def __init__(self, lookup=NOT_FOUND, positions=()):
        self.lookup, self.positions, self.asked = lookup, positions, []

    def get_order_by_client_id(self, client_id):
        self.asked.append(client_id)
        return self.lookup

    def get_positions_raw(self):
        return None if self.positions is None else [{"symbol": s, "qty": "3"} for s in self.positions]

    def get_order_raw(self, oid):
        return {"status": "expired", "filled_qty": "0", "created_at": "2026-11-25T16:20:00Z"}


@pytest.fixture
def desk(database, session, monkeypatch):
    def make(broker, at):
        monkeypatch.setattr(session, "db", database)
        monkeypatch.setattr(session, "trader", broker)
        monkeypatch.setattr(session, "settings", settings())
        now = datetime.fromisoformat(at).replace(tzinfo=market_time.ET)
        monkeypatch.setattr(market_time, "et_now", lambda *a: now)
        monkeypatch.setattr(market_time, "et_today", lambda *a: now.date())
        return session
    return make


async def cycle(main, db):
    for row in await db.get_active_condors():
        await main._manage_condor(row)


async def row(db):
    return (await db._query("SELECT status, entry_status, entry_order_id FROM iv_condors"))[0]


@pytest.mark.parametrize("at,voided", [
    ("2026-11-25T11:30:00", False),        # the same session: it may still be on its way
    ("2026-11-25T16:14:00", False),        # the close, inside the grace period
    ("2026-11-25T16:15:00", True),         # day order: can no longer exist
    ("2026-11-27T09:00:00", True),         # found on a later day (e.g. after a restart)
])
async def test_a_404_is_resolved_only_after_the_session_it_was_sent_in(database, desk, at, voided):
    await unsent(database)
    main = desk(Broker(), at)
    await cycle(main, database)
    r = await row(database)
    assert (r["status"], r["entry_status"]) == (("void", "not_submitted") if voided else ("pending_entry", "submitting"))
    assert await database.count_open_condors() == (0 if voided else 1)


async def test_a_half_day_closes_at_1300(database, desk):
    await unsent(database, opened_at="2026-11-27T15:20:00")        # 10:20 ET on the half day
    main = desk(Broker(), "2026-11-27T13:14:00")
    await cycle(main, database)
    assert (await row(database))["status"] == "pending_entry"
    main = desk(Broker(), "2026-11-27T13:15:00")
    await cycle(main, database)
    assert (await row(database))["status"] == "void"


@pytest.mark.parametrize("lookup,positions,why", [
    ({"error": "HTTP 0", "not_found": False}, (), "the lookup itself failed: not proof of anything"),
    (NOT_FOUND, None, "positions unreadable"),
    (NOT_FOUND, (SC,), "a leg is held: something did happen"),
])
async def test_it_waits_when_the_evidence_is_incomplete(database, desk, lookup, positions, why):
    await unsent(database)
    main = desk(Broker(lookup, positions), "2026-11-26T12:00:00")
    await cycle(main, database)
    assert (await row(database))["status"] == "pending_entry", why


async def test_an_order_that_did_arrive_is_adopted_not_voided(database, desk):
    await unsent(database)
    main = desk(Broker({"id": "real-order", "status": "expired"}), "2026-11-26T12:00:00")
    await cycle(main, database)
    r = await row(database)
    assert r["entry_order_id"] == "real-order"
    assert (r["status"], r["entry_status"]) == ("void", "expired")      # the normal path, not "not_submitted"


@pytest.mark.parametrize("stamp", [None, "garbage", ""])
def test_an_unreadable_stamp_never_counts_as_over(session, stamp):
    assert session._entry_session_over(stamp) is False


def test_the_session_clock_is_eastern_whatever_the_stamp_says():
    import main
    at = lambda s: datetime.fromisoformat(s).replace(tzinfo=market_time.ET)
    # 19:59 UTC on a normal day is 15:59 ET: the session is still open at 16:10 ET.
    assert main._entry_session_over("2026-11-25T19:59:00", now=at("2026-11-25T16:10:00")) is False
    assert main._entry_session_over("2026-11-25T19:59:00", now=at("2026-11-25T16:15:00")) is True
    # 03:00 UTC on the 26th is 22:00 ET on the 25th: that session is over, not the 26th's.
    assert main._entry_session_over("2026-11-26T03:00:00", now=at("2026-11-25T23:00:00")) is True

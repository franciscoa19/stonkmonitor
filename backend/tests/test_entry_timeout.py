"""An entry that has not filled is cancelled after a while, freeing its slot.

Half the condor entries to 2026-10-19 never filled (BLK twice, SCHW, TXN,
LRCX), and each held one of the three slots until its day order expired at the
close; NFLX, GE and TSLA were turned away 56 times on 10-19 while TXN and LRCX
waited. Fills, when they came, took 10-149 minutes. Fake brokers, temporary
ledgers; the session clock is 2026-11-27 12:30 ET.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

import market_time
from test_execution_safety import database, session  # noqa: F401  (fixtures)
from test_account_limits import settings
from test_condor_close_winner import Broker, book, SC, LC, SP, LP

NOW = datetime(2026, 11, 27, 12, 30, tzinfo=market_time.ET)
NOT_AT_TARGET = book(sc=(0.60, 0.70), sp=(0.50, 0.60))      # $1.28 to close: no profit target


def created(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def pending(db, ticker="TEST", qty=2):
    legs = [{"symbol": s, "side": side, "ratio_qty": 1}
            for s, side in ((SC, "sell"), (LC, "buy"), (SP, "sell"), (LP, "buy"))]
    return await db.record_condor(ticker, "2026-12-02", "2026-12-04", json.dumps(legs),
                                  {"short_put": 90, "long_put": 85, "short_call": 110, "long_call": 115},
                                  qty, 1.0, 400, f"entry-{ticker}", "new")


def broker_with(minutes_ago, status="new"):
    broker = Broker(NOT_AT_TARGET)
    broker.orders["entry-TEST"] = {"status": status, "filled_qty": "0", "created_at": created(minutes_ago)}
    return broker


@pytest.fixture
def desk(database, session, monkeypatch):
    def make(broker, **overrides):
        monkeypatch.setattr(session, "db", database)
        monkeypatch.setattr(session, "trader", broker)
        monkeypatch.setattr(session, "settings", settings(**overrides))
        return session
    return make


async def cycle(main, db):
    for row in await db.get_active_condors():
        await main._manage_condor(row)


async def status(db):
    return (await db._query("SELECT status, qty, credit FROM iv_condors"))[0]


# ── The timeout ─────────────────────────────────────────────────────────────
async def test_an_entry_younger_than_the_timeout_is_left_alone(database, desk):
    await pending(database)
    broker = broker_with(59)
    await cycle(desk(broker), database)
    assert broker.cancelled == [] and (await status(database))["status"] == "pending_entry"


async def test_an_unfilled_entry_is_cancelled_and_its_slot_freed(database, desk):
    await pending(database)
    broker = broker_with(61)
    main = desk(broker)
    await cycle(main, database)
    assert broker.cancelled == ["entry-TEST"]
    assert (await status(database))["status"] == "pending_entry"     # until the broker says so
    assert await database.count_open_condors() == 1

    broker.orders["entry-TEST"]["status"] = "pending_cancel"
    await cycle(main, database)
    assert broker.cancelled == ["entry-TEST"]                          # not sent twice

    broker.orders["entry-TEST"]["status"] = "canceled"
    await cycle(main, database)
    assert (await status(database))["status"] == "void"
    assert await database.count_open_condors() == 0                    # the slot is free


async def test_a_partial_fill_at_the_timeout_is_kept(database, desk):
    await pending(database, qty=2)
    broker = broker_with(61, status="partially_filled")
    main = desk(broker)
    await cycle(main, database)
    assert broker.cancelled == ["entry-TEST"]
    broker.fill("entry-TEST", 1, -1.0, status="canceled")
    await cycle(main, database)
    row = await status(database)
    assert (row["status"], row["qty"], row["credit"]) == ("open", 1, 1.0)


async def test_a_fill_that_beats_the_cancel_is_booked_in_full(database, desk):
    await pending(database, qty=2)
    broker = broker_with(61)
    main = desk(broker)
    await cycle(main, database)
    broker.fill("entry-TEST", 2, -1.0)                                 # filled before the cancel landed
    await cycle(main, database)
    row = await status(database)
    assert (row["status"], row["qty"]) == ("open", 2)


@pytest.mark.parametrize("why", ["timeout off", "after hours", "unreadable stamp", "naive stamp"])
async def test_no_cancel_when_it_should_wait(database, desk, monkeypatch, why):
    await pending(database)
    broker = broker_with(300)
    overrides = {}
    if why == "timeout off":
        overrides["iv_exec_entry_timeout_minutes"] = 0
    if why == "unreadable stamp":
        broker.orders["entry-TEST"]["created_at"] = None
    if why == "naive stamp":
        broker.orders["entry-TEST"]["created_at"] = "2026-11-27T10:00:00"
    main = desk(broker, **overrides)
    if why == "after hours":
        monkeypatch.setattr(main, "is_rth_now", lambda: False)
    await cycle(main, database)
    assert broker.cancelled == []


async def test_the_expiry_deadline_still_cancels_an_entry_regardless_of_age(database, desk, monkeypatch):
    await pending(database)
    broker = broker_with(5)
    main = desk(broker)
    monkeypatch.setattr(main, "_expiry_force_close_due", lambda *a, **kw: True)
    await cycle(main, database)
    assert broker.cancelled == ["entry-TEST"]


# ── The daily allowance and retries ─────────────────────────────────────────
async def test_an_entry_that_never_filled_does_not_use_up_the_day(database):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")         # opened_at is a UTC stamp
    first = await pending(database, "AAA")
    await pending(database, "BBB")
    assert await database.count_condors_opened_today(today) == 2
    await database.void_condor(first, "canceled")
    assert await database.count_condors_opened_today(today) == 1
    assert await database.condor_tried_today("AAA", today)              # but AAA is not sent again today
    assert not await database.condor_tried_today("CCC", today)
    assert not await database.condor_tried_today("AAA", "2099-01-01")


async def test_a_ticker_tried_today_is_not_sent_again(database, desk, monkeypatch):
    import signals.iv_executor as executor
    main = desk(Broker(NOT_AT_TARGET), iv_exec_enabled=True)
    monkeypatch.setattr(executor, "is_pre_earnings_entry_window", lambda *a, **kw: True)
    monkeypatch.setattr(main, "opening_quotes_settling", lambda: False)
    tried = await pending(database, "TRIED")                            # timed out this morning
    await database.void_condor(tried, "canceled")
    await database._exec("UPDATE iv_condors SET opened_at='2026-11-27T15:00:00' WHERE id=?", (tried,), strict=True)
    opened = []

    async def open_condor(setup, s, ticker):
        opened.append(ticker)
    monkeypatch.setattr(main, "_open_condor", open_condor)
    for ticker in ("TRIED", "FRESH"):
        await main.maybe_execute_condor(NS(ticker=ticker, recommendation="SELL_PREMIUM",
                                           next_earnings_date="2026-11-30", earnings_report_time="amc"))
    assert opened == ["FRESH"]


async def test_a_borderline_setup_is_not_traded_when_consider_is_off(database, desk, monkeypatch):
    import signals.iv_executor as executor
    main = desk(Broker(NOT_AT_TARGET), iv_exec_enabled=True, iv_exec_allow_consider=False)
    monkeypatch.setattr(executor, "is_pre_earnings_entry_window", lambda *a, **kw: True)
    monkeypatch.setattr(main, "opening_quotes_settling", lambda: False)
    opened = []

    async def open_condor(setup, s, ticker):
        opened.append(ticker)
    monkeypatch.setattr(main, "_open_condor", open_condor)
    for ticker, rec in (("LRCX", "CONSIDER"), ("NFLX", "SELL_PREMIUM")):
        await main.maybe_execute_condor(NS(ticker=ticker, recommendation=rec,
                                           next_earnings_date="2026-11-30", earnings_report_time="amc"))
    assert opened == ["NFLX"]

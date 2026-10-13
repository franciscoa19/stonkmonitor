"""The close limit rises on every unfilled retry, up to a ceiling.

WFC's take-profit close went unfilled eleven times on 2026-10-13 at one cent
over the quote; PEP's and NKE's had done the same, while UNH (six cents over)
and MU filled at once. Fake brokers, temporary ledgers; the WFC and UNH numbers
are the real ones.
"""
import sqlite3

import pytest

from db import Database
from test_execution_safety import database, session  # noqa: F401  (fixtures)
from test_account_limits import settings
from test_condor_close_winner import (Broker, book, condor, manage, the_condor, desk,  # noqa: F401
                                      LATER, SC, LC, SP, LP, SHORTS)

LEGS = [{"symbol": SC, "side": "sell"}, {"symbol": LC, "side": "buy"},
        {"symbol": SP, "side": "sell"}, {"symbol": LP, "side": "buy"}]


def quotes(sc_ask, lc_bid, sp_ask, lp_bid):
    return {SC: {"bid": max(sc_ask - 0.01, 0), "ask": sc_ask}, LC: {"bid": lc_bid, "ask": lc_bid + 0.01},
            SP: {"bid": max(sp_ask - 0.01, 0), "ask": sp_ask}, LP: {"bid": lp_bid, "ask": lp_bid + 0.01}}


def limits(main, c, q, tp_hit, attempts=range(6)):
    debit = main._condor_close_debit(LEGS, q)
    return [main._condor_close_limit(c, LEGS, q, debit, tp_hit, n) for n in attempts]


@pytest.fixture
def main(session, monkeypatch):
    monkeypatch.setattr(session, "settings", settings())          # 50% profit target
    return session


WFC = {"credit": 0.51, "short_put": 79.0, "long_put": 77.0, "short_call": 87.5, "long_call": 90.0}
WFC_BOOK = quotes(0.06, 0.03, 0.27, 0.06)          # 14:56 ET on 2026-10-13: $0.24 at the touch
UNH = {"credit": 3.66, "short_put": 350.0, "long_put": 337.5, "short_call": 402.5, "long_call": 415.0}
UNH_BOOK = quotes(1.10, 0.20, 0.47, 0.09)          # $1.28 at the touch; it filled at $1.34


# ── The limit ───────────────────────────────────────────────────────────────
def test_a_close_that_has_to_happen_bids_higher_each_time_up_to_the_short_legs_cost(main):
    """WFC tomorrow morning: $0.25, then up to what the two short legs cost on
    their own ($0.33) plus a cushion — where it must fill even if the wings
    fetch nothing."""
    assert limits(main, WFC, WFC_BOOK, tp_hit=False) == [0.25, 0.28, 0.32, 0.35, 0.35, 0.35]


def test_a_profit_target_close_never_bids_past_the_target(main):
    """WFC today: the target is $0.255, so $0.25 is all a take-profit may pay.
    If that does not fill, the target has not really been reached."""
    assert limits(main, WFC, WFC_BOOK, tp_hit=True) == [0.25] * 6
    assert limits(main, UNH, UNH_BOOK, tp_hit=True) == [1.34, 1.51, 1.67, 1.83, 1.83, 1.83]


def test_the_first_attempt_is_what_it_always_was(main):
    assert limits(main, UNH, UNH_BOOK, tp_hit=True)[0] == 1.34          # 5% over $1.28
    assert limits(main, WFC, WFC_BOOK, tp_hit=False)[0] == 0.25         # one tick over $0.24
    penny = {"credit": 1.0, "short_put": 90, "long_put": 85, "short_call": 110, "long_call": 115}
    assert limits(main, penny, quotes(0.04, 0.0, 0.03, 0.0), tp_hit=True)[0] == 0.08   # NKE: 5% of $0.07 is nothing


def test_a_loser_is_bid_up_towards_what_its_short_legs_cost(main):
    c = {"credit": 1.0, "short_put": 90, "long_put": 85, "short_call": 110, "long_call": 115}
    assert limits(main, c, quotes(4.10, 0.90, 0.05, 0.0), tp_hit=False) == [3.41, 3.73, 4.04, 4.36, 4.36, 4.36]


def test_never_more_than_the_wing_width(main):
    c = {"credit": 1.0, "short_put": 90, "long_put": 85, "short_call": 110, "long_call": 115}
    assert limits(main, c, quotes(5.20, 0.30, 0.02, 0.0), tp_hit=False) == [5.0] * 6


@pytest.mark.parametrize("credit,touch_book,expected", [
    (0.50, quotes(0.15, 0.0, 0.10, 0.0), 0.25),     # quote already at the target: no cushion past it
    (0.51, quotes(0.15, 0.0, 0.10, 0.0), 0.25),     # $0.255 target is $0.25 in whole cents, never $0.26
    (0.53, quotes(0.15, 0.0, 0.10, 0.0), 0.26)])
def test_the_target_is_floored_to_a_whole_cent(main, credit, touch_book, expected):
    c = {"credit": credit, "short_put": 90, "long_put": 85, "short_call": 110, "long_call": 115}
    assert limits(main, c, touch_book, tp_hit=True) == [expected] * 6


@pytest.mark.parametrize("attempt", [-5, 0])
def test_a_nonsense_attempt_number_is_the_first_attempt(main, attempt):
    debit = main._condor_close_debit(LEGS, WFC_BOOK)
    assert main._condor_close_limit(WFC, LEGS, WFC_BOOK, debit, False, attempt) == 0.25


def test_the_limit_never_falls_as_attempts_rise(main):
    for c, q in ((WFC, WFC_BOOK), (UNH, UNH_BOOK)):
        for tp in (True, False):
            seq = limits(main, c, q, tp, attempts=range(12))
            assert seq == sorted(seq) and seq[3] == seq[-1]             # at the ceiling by the fourth


# ── Through the close path ──────────────────────────────────────────────────
class Unfilled(Broker):
    """Every close rests unfilled; a cancel is acknowledged on the next look."""

    def close_multileg(self, legs, qty, limit, client_order_id=None):
        result = super().close_multileg(legs, qty, limit, client_order_id)
        if result.get("id"):
            self.orders[result["id"]]["created_at"] = "2020-01-01T00:00:00Z"   # long past the 5-minute wait
        return result

    def cancel_order_raw(self, oid):
        self.orders[oid]["status"] = "canceled"
        return super().cancel_order_raw(oid)


MUST_EXIT = book(sc=(0.30, 0.40), sp=(0.25, 0.35), lc_bid=0.05, lp_bid=0.04)    # $0.66: above the $0.50 target


async def retry(main, db, times):
    for _ in range(times):
        await manage(main, db)       # submit
        await manage(main, db)       # five minutes on: cancel it


async def test_each_unfilled_retry_is_sent_a_step_higher(database, desk, monkeypatch):
    await condor(database, expiry=LATER)
    broker = Unfilled(MUST_EXIT)
    main = desk(broker)
    monkeypatch.setattr(main, "settings", settings())
    await retry(main, database, 5)
    assert [s["limit"] for s in broker.sent] == [0.69, 0.72, 0.76, 0.79, 0.79]
    assert all(len(s["legs"]) == 4 for s in broker.sent)
    row = await the_condor(database)
    assert (row["close_attempts"], row["close_attempt_day"]) == (5, "2026-11-27")


async def test_a_fill_at_a_higher_limit_books_the_price_actually_paid(database, desk, monkeypatch):
    await condor(database, expiry=LATER)
    broker = Unfilled(MUST_EXIT)
    main = desk(broker)
    monkeypatch.setattr(main, "settings", settings())
    await retry(main, database, 2)
    await manage(main, database)                              # third attempt, limit $0.76
    assert broker.sent[-1]["limit"] == 0.76
    broker.fill("close-3", 2, 0.71)                           # the market's price, under the limit
    await manage(main, database)
    row = await the_condor(database)
    assert row["status"] == "closed" and row["exit_debit"] == 0.71
    assert row["pnl"] == pytest.approx((1.00 - 0.71) * 100 * 2)


async def test_the_count_survives_a_restart_and_starts_again_the_next_day(database, desk, monkeypatch):
    import market_time
    from datetime import datetime
    await condor(database, expiry=LATER)
    broker = Unfilled(MUST_EXIT)
    main = desk(broker)
    monkeypatch.setattr(main, "settings", settings())
    await retry(main, database, 2)

    # A restart keeps nothing in memory. The count is on the row, and the row
    # is all the next cycle reads.
    row = await the_condor(database)
    assert (row["close_attempts"], row["close_attempt_day"], row["status"]) == (2, "2026-11-27", "closing")
    await main._manage_condor(row)                            # books the cancel, sends the next
    assert broker.sent[-1]["limit"] == 0.76                   # carries on from the third step
    await manage(main, database)                              # five minutes on: cancel it

    next_day = datetime(2026, 11, 30, 12, 30, tzinfo=market_time.ET)
    monkeypatch.setattr(market_time, "et_today", lambda *a: next_day.date())
    monkeypatch.setattr(market_time, "et_now", lambda *a: next_day)
    await retry(main, database, 1)
    assert broker.sent[-1]["limit"] == 0.69                   # a new day's book: start from the quote
    row = await the_condor(database)
    assert (row["close_attempts"], row["close_attempt_day"]) == (1, "2026-11-30")


async def test_a_take_profit_that_will_not_fill_keeps_bidding_the_target_and_no_more(database, desk, monkeypatch):
    await condor(database, expiry=LATER)
    broker = Unfilled(book(sc=(0.20, 0.27), sp=(0.15, 0.22), lc_bid=0.0, lp_bid=0.0))   # $0.49 against a $0.50 target
    main = desk(broker)
    monkeypatch.setattr(main, "settings", settings())
    await retry(main, database, 4)
    assert [s["limit"] for s in broker.sent] == [0.5] * 4 and all(s["legs"] == SHORTS for s in broker.sent)


async def test_a_refused_reduced_order_and_its_fallback_count_as_one_attempt(database, desk, monkeypatch):
    await condor(database, expiry=LATER)
    broker = Unfilled(results=[{"error": "invalid legs", "ambiguous": False}])
    main = desk(broker)
    monkeypatch.setattr(main, "settings", settings())
    await manage(main, database)
    assert len(broker.sent) == 2 and broker.sent[0]["limit"] == broker.sent[1]["limit"]
    assert (await the_condor(database))["close_attempts"] == 1


async def test_the_forced_expiry_close_still_uses_the_wing_width_from_the_first_attempt(database, desk, monkeypatch):
    await condor(database, expiry="2026-11-27")
    broker = Unfilled(MUST_EXIT)
    main = desk(broker)
    monkeypatch.setattr(main, "settings", settings())
    monkeypatch.setattr(main, "_condor_expiry_hold", lambda *a, **kw: False)
    monkeypatch.setattr(main, "_expiry_force_close_due", lambda *a, **kw: True)
    await manage(main, database)
    assert broker.sent[0]["limit"] == 5.0 and (await the_condor(database))["close_attempts"] == 1


# ── Existing ledgers ────────────────────────────────────────────────────────
async def test_an_older_ledger_gains_the_counter_starting_at_zero(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path)
    await db.connect()
    await condor(db, expiry=LATER)
    await db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE iv_condors DROP COLUMN close_attempts")
        conn.execute("ALTER TABLE iv_condors DROP COLUMN close_attempt_day")
    for _ in range(2):
        await db.connect()
        try:
            row = (await db._query("SELECT * FROM iv_condors"))[0]
            assert (row["close_attempts"], row["close_attempt_day"], row["status"]) == (0, None, "open")
        finally:
            await db.close()


async def test_claiming_a_close_without_a_count_leaves_the_count_alone(database):
    row = await condor(database, expiry=LATER)
    await database.begin_condor_close(row["id"], "c1", None, attempts=3, attempt_day="2026-11-27")
    await database.begin_condor_close(row["id"], "c2")
    after = await the_condor(database)
    assert (after["close_attempts"], after["close_attempt_day"], after["close_client_order_id"]) == (3, "2026-11-27", "c2")

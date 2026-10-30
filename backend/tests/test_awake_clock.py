"""No new condor unless the bot has run unbroken for a while.

With the lid shut the Mac woke for a minute every ~15 and the bot ran in those
wakes: it opened MSFT and GOOGL on 2026-10-26 in one and could not look at them
again until the next. Explicit clock values; no sleeping, no network.
"""
from types import SimpleNamespace as NS

import pytest

import awake_clock
from awake_clock import AwakeClock, GAP_SECONDS
from test_execution_safety import database, session  # noqa: F401  (fixtures)
from test_account_limits import settings

T0 = 1_800_000_000.0


def beats(clock, minutes, every=60, start=T0):
    """Beat every `every` seconds for `minutes`; returns the last time."""
    t = start
    for _ in range(int(minutes * 60 / every) + 1):
        clock.beat(t)
        t += every
    return t - every


# ── The clock ───────────────────────────────────────────────────────────────
def test_an_unbroken_hour_counts_as_an_hour():
    c = AwakeClock()
    last = beats(c, 61)
    assert c.awake_minutes(last) == pytest.approx(61, abs=0.1)


def test_a_sleep_starts_the_count_again():
    c = AwakeClock()
    last = beats(c, 50)
    woke = last + 16 * 60                                       # asleep 16 minutes
    c.beat(woke)
    assert c.awake_minutes(woke) == 0
    assert c.awake_minutes(woke + 30) == pytest.approx(0.5)


def test_brief_wakes_every_fifteen_minutes_never_add_up():
    """The pattern in the power log: about a minute awake every quarter hour."""
    c = AwakeClock()
    t = T0
    for _ in range(40):                                         # ten hours of it
        c.beat(t)
        c.beat(t + 60)
        t += 15 * 60
    assert c.awake_minutes(T0 + 39 * 15 * 60 + 60) <= 1


def test_no_recent_beat_is_no_time_awake():
    c = AwakeClock()
    assert c.awake_minutes(T0) == 0                             # never beat
    last = beats(c, 90)
    assert c.awake_minutes(last + GAP_SECONDS + 1) == 0         # the beats stopped


def test_a_clock_that_goes_backwards_starts_again():
    c = AwakeClock()
    last = beats(c, 90)
    c.beat(last - 600)
    assert c.awake_minutes(last - 600) == 0


def test_start_beats_at_once_and_only_once():
    c = AwakeClock()
    c.start()
    first_thread = c._thread
    assert c.last is not None and first_thread.is_alive() and first_thread.daemon
    c.start()
    assert c._thread is first_thread


# ── The entry gate ──────────────────────────────────────────────────────────
@pytest.fixture
def entry(database, session, monkeypatch):
    import signals.iv_executor as executor
    monkeypatch.setattr(executor, "is_pre_earnings_entry_window", lambda *a, **kw: True)
    monkeypatch.setattr(session, "opening_quotes_settling", lambda: False)
    monkeypatch.setattr(session, "db", database)
    opened = []

    async def open_condor(setup, s, ticker):
        opened.append(ticker)
    monkeypatch.setattr(session, "_open_condor", open_condor)

    async def attempt(clock, **overrides):
        monkeypatch.setattr(session, "awake_clock", clock)
        monkeypatch.setattr(session, "settings", settings(iv_exec_enabled=True, **overrides))
        await session.maybe_execute_condor(NS(ticker="TEST", recommendation="SELL_PREMIUM",
                                              next_earnings_date="2026-11-30", earnings_report_time="amc"))
        return list(opened)
    return attempt


def awake_for(minutes):
    c = AwakeClock()
    c.awake_minutes = lambda now=None: float(minutes)
    return c


async def test_no_entry_after_a_short_run(entry):
    assert await entry(awake_for(20)) == []


async def test_an_entry_after_an_hour_unbroken(entry):
    assert await entry(awake_for(61)) == ["TEST"]


async def test_the_guard_can_be_turned_off(entry):
    assert await entry(AwakeClock(), iv_exec_require_awake_minutes=0) == ["TEST"]   # never beat


async def test_a_fresh_start_waits_the_full_period(entry):
    assert await entry(AwakeClock()) == []                      # never beat: zero minutes awake


async def test_open_condors_are_still_managed_while_entries_wait(database, session, monkeypatch):
    """The guard is on entries only: the monitor still closes what is open."""
    from test_condor_close_winner import Broker, condor
    await condor(database)
    broker = Broker()                                           # a winner at its profit target
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "trader", broker)
    monkeypatch.setattr(session, "settings", settings())
    monkeypatch.setattr(session, "awake_clock", AwakeClock())   # zero minutes awake
    await session._manage_condor((await database.get_active_condors())[0])
    assert len(broker.sent) == 1


async def test_health_reports_how_long_the_bot_has_been_awake(session, monkeypatch):
    monkeypatch.setattr(session, "awake_clock", awake_for(42.04))
    assert (await session.health())["awake_minutes"] == 42.0

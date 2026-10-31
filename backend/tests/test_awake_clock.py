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


class HostTimes:
    """Independent clocks: suspension advances only the inclusive clock."""
    def __init__(self):
        self.continuous = T0
        self.uptime = T0

    def __call__(self):
        return self.continuous, self.uptime

    def advance(self, seconds, *, asleep=False):
        self.continuous += seconds
        if not asleep:
            self.uptime += seconds


def beats(clock, times, minutes, every=60):
    clock.beat()
    for _ in range(int(minutes * 60 / every)):
        times.advance(every)
        clock.beat()


# ── The clock ───────────────────────────────────────────────────────────────
def test_an_unbroken_hour_counts_as_an_hour():
    times = HostTimes()
    c = AwakeClock(times)
    beats(c, times, 61)
    assert c.awake_minutes() == pytest.approx(61, abs=0.1)
    assert c.entry_token(60) is not None


@pytest.mark.parametrize("sleep_seconds", [2, 30, 180, 16 * 60])
def test_a_sleep_starts_the_count_again(sleep_seconds):
    times = HostTimes()
    c = AwakeClock(times)
    beats(c, times, 61)
    times.advance(sleep_seconds, asleep=True)
    c.beat()
    assert c.awake_minutes() == 0
    assert c.entry_token(60) is None
    times.advance(30)
    assert c.awake_minutes() == pytest.approx(0.5)


def test_a_read_detects_sleep_before_the_heartbeat_thread_runs():
    times = HostTimes()
    c = AwakeClock(times)
    beats(c, times, 61)
    times.advance(3, asleep=True)
    assert c.entry_token(60) is None
    assert c.awake_minutes() == 0
    c.beat()
    beats(c, times, 61)
    assert c.entry_token(60) is not None


@pytest.mark.parametrize("sleep_seconds", [3, 180, 14 * 60])
def test_brief_wakes_never_add_up(sleep_seconds):
    times = HostTimes()
    c = AwakeClock(times)
    for _ in range(70):
        c.beat()
        times.advance(60)
        c.beat()
        assert c.awake_minutes() <= 1
        assert c.entry_token(60) is None
        times.advance(sleep_seconds, asleep=True)


def test_sub_tolerance_sleeps_are_detected_cumulatively():
    times = HostTimes()
    c = AwakeClock(times)
    beats(c, times, 61)
    for _ in range(3):
        times.advance(0.4, asleep=True)
        c.beat()
    assert c.awake_minutes() == 0


def test_scheduling_delay_without_suspend_is_not_sleep():
    times = HostTimes()
    c = AwakeClock(times)
    beats(c, times, 61)
    token = c.entry_token(60)
    times.advance(240)
    c.beat()
    assert c.awake_minutes() == 65
    assert c.entry_token(60) == token


def test_reset_invalidates_old_plans_even_after_another_full_hour():
    times = HostTimes()
    c = AwakeClock(times)
    beats(c, times, 61)
    token = c.entry_token(60)
    times.advance(3, asleep=True)
    c.beat()
    beats(c, times, 61)
    assert c.entry_token(60) is not None
    assert c.entry_token(60) != token


def test_no_recent_beat_is_no_time_awake():
    times = HostTimes()
    c = AwakeClock(times)
    assert c.awake_minutes() == 0
    beats(c, times, 90)
    times.advance(GAP_SECONDS + 1)
    assert c.awake_minutes() == 0
    assert c.entry_token(60) is None


@pytest.mark.parametrize("fields", [["continuous"], ["uptime"], ["continuous", "uptime"]])
def test_a_clock_that_goes_backwards_starts_again(fields):
    times = HostTimes()
    c = AwakeClock(times)
    beats(c, times, 90)
    for field in fields:                     # both together leave their difference unchanged
        setattr(times, field, getattr(times, field) - 600)
    c.beat()
    assert c.awake_minutes() == 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1])
@pytest.mark.parametrize("field", ["continuous", "uptime"])
def test_invalid_clock_readings_block_entries(bad, field):
    times = HostTimes()
    c = AwakeClock(times)
    beats(c, times, 61)
    setattr(times, field, bad)
    assert c.entry_token(60) is None
    assert c.awake_minutes() == 0


def test_clock_failure_requires_a_new_warmup(monkeypatch):
    times = HostTimes()
    c = AwakeClock(times)
    beats(c, times, 61)
    def failed():
        raise OSError("clock unavailable")
    monkeypatch.setattr(c, "_read_times", failed)
    c.beat()  # a source error must not kill the heartbeat thread
    assert c.entry_token(60) is None
    monkeypatch.setattr(c, "_read_times", times)
    c.beat()
    assert c.awake_minutes() == 0
    assert c.entry_token(60) is None
    beats(c, times, 61)
    assert c.entry_token(60) is not None


@pytest.mark.parametrize("platform,continuous_id,uptime_id", [
    ("darwin", 11, 12), ("linux", 13, 14),
])
def test_platform_clock_pair(platform, continuous_id, uptime_id, monkeypatch):
    calls = []
    def gettime(clock_id):
        calls.append(clock_id)
        return 100 if clock_id == continuous_id else 80
    monkeypatch.setattr(awake_clock, "sys", NS(platform=platform))
    monkeypatch.setattr(awake_clock, "time", NS(clock_gettime=gettime,
        CLOCK_MONOTONIC_RAW=11, CLOCK_UPTIME_RAW=12, CLOCK_BOOTTIME=13, CLOCK_MONOTONIC=14))
    assert awake_clock._clock_times() == (100, 80)
    assert calls == [continuous_id, uptime_id, continuous_id]


def test_interrupted_clock_sampling_blocks_readiness(monkeypatch):
    readings = iter([100, 80, 103])
    monkeypatch.setattr(awake_clock, "sys", NS(platform="darwin"))
    monkeypatch.setattr(awake_clock, "time", NS(clock_gettime=lambda _: next(readings),
        CLOCK_MONOTONIC_RAW=11, CLOCK_UPTIME_RAW=12))
    c = AwakeClock()
    c.beat()
    assert c.since is None


def test_unsupported_platform_blocks_readiness(monkeypatch):
    monkeypatch.setattr(awake_clock, "sys", NS(platform="unsupported"))
    c = AwakeClock()
    c.beat()
    assert c.awake_minutes() == 0
    assert c.entry_token(60) is None


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
    c.awake_minutes = lambda: float(minutes)
    c.entry_token = lambda required: 0 if minutes >= required else None
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

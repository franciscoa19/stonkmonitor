"""Sleep during entry preparation must never submit a stale plan. Fake broker only."""
import asyncio

import pytest

from awake_clock import AwakeClock
from test_account_limits import SETUP, entry as prepare_entry
from test_awake_clock import HostTimes, beats
from test_execution_safety import database, session  # noqa: F401


@pytest.mark.parametrize("recovered_minutes", [0, 61])
async def test_sleep_during_planning_does_not_submit(database, session, monkeypatch, recovered_minutes):
    import signals.iv_executor as executor

    seen = prepare_entry(session, database, monkeypatch)
    monkeypatch.setattr(session, "opening_quotes_settling", lambda: False)
    times = HostTimes()
    clock = AwakeClock(times)
    beats(clock, times, 61)
    monkeypatch.setattr(session, "awake_clock", clock)
    original = executor.build_iron_condor

    def plan_after_sleep(*args, **kwargs):
        times.advance(16 * 60, asleep=True)
        clock.beat()
        beats(clock, times, recovered_minutes)
        return original(*args, **kwargs)

    monkeypatch.setattr(executor, "build_iron_condor", plan_after_sleep)
    await session.maybe_execute_condor(SETUP)
    assert seen.orders == []
    assert await database.get_active_condors() == []


@pytest.fixture
def ready_entry(database, session, monkeypatch):
    seen = prepare_entry(session, database, monkeypatch)
    monkeypatch.setattr(session, "opening_quotes_settling", lambda: False)
    times = HostTimes()
    clock = AwakeClock(times)
    beats(clock, times, 61)
    monkeypatch.setattr(session, "awake_clock", clock)
    return session, seen, times, clock


async def test_sleep_while_waiting_for_entry_lock_stops_preparation(ready_entry, monkeypatch):
    from trading import account_risk

    main, seen, times, _ = ready_entry
    waiting, release = asyncio.Event(), asyncio.Event()

    class WaitingLock:
        async def __aenter__(self):
            waiting.set()
            await release.wait()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(account_risk, "entry_lock", WaitingLock())
    task = asyncio.create_task(main.maybe_execute_condor(SETUP))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=2)
        times.advance(3, asleep=True)
    finally:
        release.set()
        await task
    assert seen.reads == []
    assert seen.orders == []


async def test_sleep_during_namespace_read_does_not_persist(ready_entry, database, monkeypatch):
    main, seen, times, _ = ready_entry
    original = database.order_namespace

    async def after_sleep():
        namespace = await original()
        times.advance(3, asleep=True)
        return namespace

    monkeypatch.setattr(database, "order_namespace", after_sleep)
    await main.maybe_execute_condor(SETUP)
    assert seen.orders == []
    assert await database._query("SELECT id FROM iv_condors") == []


@pytest.mark.parametrize("stage", ["persistence", "executor_queue"])
async def test_sleep_at_submission_voids_only_the_unsent_reservation(
        ready_entry, database, monkeypatch, stage):
    main, seen, times, _ = ready_entry
    if stage == "persistence":
        original = database.record_condor

        async def persist_after_sleep(*args, **kwargs):
            cid = await original(*args, **kwargs)
            times.advance(3, asleep=True)
            return cid

        monkeypatch.setattr(database, "record_condor", persist_after_sleep)
    else:
        loop = asyncio.get_running_loop()
        original = loop.run_in_executor

        def queue(executor, func, *args):
            if getattr(func, "__name__", None) != "submit":
                return original(executor, func, *args)

            def resume_in_worker():
                times.advance(3, asleep=True)
                return func(*args)

            return original(executor, resume_in_worker)

        monkeypatch.setattr(loop, "run_in_executor", queue)
    await main.maybe_execute_condor(SETUP)
    assert seen.orders == []
    assert await database.get_active_condors() == []
    row, = await database._query("SELECT * FROM iv_condors")
    assert row["status"] == "void"
    assert row["entry_status"] == "not_submitted_asleep"
    assert row["entry_order_id"] is None
    assert not await database.condor_tried_today(SETUP.ticker, row["opened_at"][:10])
    assert await database.count_condors_opened_today(row["opened_at"][:10]) == 0


async def test_unsent_reservation_can_retry_after_a_new_full_warmup(ready_entry, database, monkeypatch):
    main, seen, times, clock = ready_entry
    original = database.record_condor

    async def persist_after_sleep(*args, **kwargs):
        cid = await original(*args, **kwargs)
        times.advance(3, asleep=True)
        return cid

    monkeypatch.setattr(database, "record_condor", persist_after_sleep)
    await main.maybe_execute_condor(SETUP)
    monkeypatch.setattr(database, "record_condor", original)
    # Align the ledger date with the fixture's session so the retry gate really
    # reads the void row, rather than passing because it was from another day.
    await database._exec("UPDATE iv_condors SET opened_at='2026-11-27T17:00:00'", strict=True)
    clock.beat()
    beats(clock, times, 61)
    await main.maybe_execute_condor(SETUP)
    assert seen.orders == [5]
    assert len(await database.get_active_condors()) == 1


async def test_failed_void_keeps_the_reservation_tracked(ready_entry, database, monkeypatch):
    from db import DatabaseError

    main, seen, times, _ = ready_entry
    original = database.record_condor

    async def persist_after_sleep(*args, **kwargs):
        cid = await original(*args, **kwargs)
        times.advance(3, asleep=True)
        return cid

    async def failed_void(*args):
        raise DatabaseError("cannot commit void")

    monkeypatch.setattr(database, "record_condor", persist_after_sleep)
    monkeypatch.setattr(database, "void_condor", failed_void)
    with pytest.raises(DatabaseError):
        await main.maybe_execute_condor(SETUP)
    assert seen.orders == []
    row, = await database.get_active_condors()
    assert row["entry_status"] == "submitting"
    assert await database.condor_tried_today(SETUP.ticker, row["opened_at"][:10])


@pytest.mark.parametrize("response", [
    {"id": "accepted", "status": "accepted"},
    {"error": "timeout", "ambiguous": True},
])
async def test_sleep_after_post_does_not_void_a_possible_order(ready_entry, database, response):
    main, seen, times, _ = ready_entry

    def submit(*args, **kwargs):
        seen.orders.append(5)
        times.advance(180, asleep=True)
        return response

    main.trader.multileg_order = submit
    await main.maybe_execute_condor(SETUP)
    assert seen.orders == [5]
    row, = await database.get_active_condors()
    assert row["status"] == "pending_entry"
    assert row["entry_status"] == response.get("status", "submitting")
    assert row["entry_order_id"] == response.get("id")


async def test_unexpected_broker_response_is_not_an_unsent_sentinel(ready_entry, database):
    main, seen, _, _ = ready_entry

    def submit(*args, **kwargs):
        seen.orders.append(5)
        return None

    main.trader.multileg_order = submit
    with pytest.raises(AttributeError):
        await main.maybe_execute_condor(SETUP)
    assert seen.orders == [5]
    row, = await database.get_active_condors()
    assert row["entry_status"] == "submitting"


async def test_disabled_guard_allows_submission_with_an_unready_clock(database, session, monkeypatch):
    seen = prepare_entry(session, database, monkeypatch, iv_exec_require_awake_minutes=0)
    monkeypatch.setattr(session, "opening_quotes_settling", lambda: False)
    monkeypatch.setattr(session, "awake_clock", AwakeClock())
    await session.maybe_execute_condor(SETUP)
    assert seen.orders == [5]


@pytest.mark.parametrize("entry_status", [None, "rejected", "canceled", "expired"])
async def test_real_submission_attempts_still_block_same_day_retry(database, entry_status):
    from test_account_limits import PLAN

    cid = await database.record_condor(SETUP.ticker, SETUP.next_earnings_date, PLAN["expiry"],
        PLAN["legs_json"], PLAN["strikes"], PLAN["qty"], PLAN["credit"], PLAN["max_loss"],
        None, "submitting")
    await database.void_condor(cid, entry_status)
    row, = await database._query("SELECT * FROM iv_condors")
    assert await database.condor_tried_today(SETUP.ticker, row["opened_at"][:10])


def test_short_repeated_sleeps_do_not_qualify_as_an_hour():
    times = HostTimes()
    clock = AwakeClock(times)
    for _ in range(15):
        clock.beat()
        times.advance(60)
        clock.beat()
        times.advance(180, asleep=True)
    assert clock.awake_minutes() < 60

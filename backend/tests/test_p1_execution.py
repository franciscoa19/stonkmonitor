"""P1 regressions: temp databases and local brokers only; never real orders."""
import asyncio
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from pydantic import ValidationError

from config import Settings
from db import Database, DatabaseError, SCHEMA
from signals.auto_trade import AutoTradeEngine
from signals.position_exits import manage_position_exit
from trading.alpaca_trader import AlpacaTrader
from test_execution_safety import (
    database, session, condor, engine_with_trade, EXIT_SETTINGS, POSITION,
)


class Broker:
    paper = True

    def __init__(self):
        self.calls, self.positions, self.orders = [], [], []
        self.clients, self.by_id = {}, {}
        self.response = None

    def get_order_by_client_id(self, client_id):
        return self.clients.get(client_id, {"error": "404", "not_found": True})

    def get_order_raw(self, order_id):
        return self.by_id.get(order_id, {"error": "404", "not_found": True})

    def get_positions_raw(self):
        return self.positions

    def get_open_orders_raw(self):
        return self.orders

    def bracket_order(self, **kwargs):
        self.calls.append(kwargs)
        result = self.response or {"id": f"order-{len(self.calls)}", "status": "accepted"}
        if result.get("id"):
            self.clients[kwargs["client_order_id"]] = result
            self.by_id[result["id"]] = {**result, "filled_qty": "0"}
        return result


async def another_trade(engine, ticker="MSFT"):
    tid = await engine._db.save_pending_trade(datetime.now(timezone.utc) + timedelta(minutes=5),
        ticker=ticker, symbol=ticker, qty=1, limit_price=100)
    source = next(iter(engine._pending.values()))
    suggestion = deepcopy(source)
    suggestion.ticker = suggestion.symbol = ticker
    engine._pending[tid] = suggestion
    return tid


async def readonly(db, enabled=True):
    await db._conn.execute(f"PRAGMA query_only={'ON' if enabled else 'OFF'}")


async def test_concurrent_condor_inserts_return_their_own_identity(database):
    async def insert(i):
        return await database.record_condor(f"TK{i}", "2026-11-25", "2026-11-27", "[]",
            {"short_put": 90, "long_put": 85, "short_call": 110, "long_call": 115},
            1, 1, 400, None, "submitting")
    tasks = [insert(i) for i in range(20)] + [database.add_watchlist(f"WL{i}") for i in range(40)]
    ids = (await asyncio.gather(*tasks))[:20]
    rows = await database._query("SELECT id,ticker FROM iv_condors")
    assert len(set(ids)) == 20
    assert dict(zip(ids, [f"TK{i}" for i in range(20)])) == {r["id"]: r["ticker"] for r in rows}


async def test_existing_database_migrates_reservations_without_losing_legacy_rows(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute("""INSERT INTO pending_trades
            (ticker,trade_type,symbol,side,qty,limit_price,risk_amount,status,created_at,expires_at)
            VALUES ('AAPL','equity','AAPL','bullish',1,100,100,'confirmed',?,?)""",
            ("2026-10-01T12:00:00", "2026-10-01T12:05:00"))
    db = Database(path)
    await db.connect()
    try:
        rows = await db.get_entry_reservations()
        assert len(rows) == 1 and rows[0]["symbol"] == "AAPL" and rows[0]["entry_order_status"] is None
    finally:
        await db.close()
    await db.connect()
    try:
        assert len(await db.get_entry_reservations()) == 1  # migration is idempotent
    finally:
        await db.close()


async def test_failed_stop_save_leaves_no_memory_state_or_broker_order(database):
    state, calls = {}, []
    broker = NS(market_order=lambda *a, **kw: calls.append(kw) or {"id": "stop"})
    await readonly(database)
    with pytest.raises(DatabaseError):
        await manage_position_exit(database, broker, POSITION, EXIT_SETTINGS, state)
    assert state == {} and calls == []
    assert await database.get_position_monitor_states() == {}
    await readonly(database, False)
    await manage_position_exit(database, broker, POSITION, EXIT_SETTINGS, state)
    assert len(calls) == 1 and state["pending"]["order_id"] == "stop"


async def test_failed_save_after_stop_post_retains_recoverable_client_id(database, monkeypatch):
    save = database.save_position_monitor_state
    writes, calls, state = [], [], {}

    async def save_once(symbol, updated):
        writes.append(deepcopy(updated))
        if len(writes) == 2:
            raise DatabaseError("disk unavailable after POST")
        await save(symbol, updated)

    broker = NS(market_order=lambda *a, **kw: calls.append(kw) or {"id": "stop"},
                get_order_by_client_id=lambda _: {"id": "stop", "status": "accepted"})
    monkeypatch.setattr(database, "save_position_monitor_state", save_once)
    with pytest.raises(DatabaseError):
        await manage_position_exit(database, broker, POSITION, EXIT_SETTINGS, state)
    assert state["pending"]["client_id"] == calls[0]["client_order_id"]
    assert "order_id" not in state["pending"]
    restored = (await database.get_position_monitor_states())["AAPL"]
    await manage_position_exit(database, broker, POSITION, EXIT_SETTINGS, restored)
    assert len(calls) == 1


async def test_failed_condor_entry_save_does_not_submit(database, session, monkeypatch):
    from signals import iv_executor
    settings = Settings(_env_file=None, alpaca_api_key="unused", alpaca_secret_key="unused",
                        iv_exec_enabled=True, auto_trade_auto_execute=False)
    plan = {"ok": True, "expiry": "2026-11-27", "legs_json": "[]", "legs": [],
            "strikes": {"short_put": 90, "long_put": 85, "short_call": 110, "long_call": 115},
            "qty": 1, "credit": 1, "max_loss": 400, "limit_price": -1, "risk_usd": 400}
    calls = []
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "settings", settings)
    monkeypatch.setattr(session, "trader", NS(get_account=lambda: {"equity": 100000, "cash": 100000, "options_buying_power": 100000},
        get_positions_raw=lambda: [], get_open_orders_raw=lambda: [],
        multileg_order=lambda *a, **kw: calls.append(kw) or {"id": "entry"}))
    monkeypatch.setattr(iv_executor, "is_pre_earnings_entry_window", lambda *a, **kw: True)
    monkeypatch.setattr(iv_executor, "build_iron_condor", lambda *a, **kw: plan)
    await database.order_namespace()
    await readonly(database)
    with pytest.raises(DatabaseError):
        await session.maybe_execute_condor(NS(ticker="TEST", recommendation="SELL_PREMIUM",
            next_earnings_date="2026-11-25", earnings_report_time="AMC"))
    assert calls == [] and await database.get_active_condors() == []


async def test_failed_condor_close_save_does_not_submit(database, session, monkeypatch):
    row = await condor(database)
    calls = []
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "trader", NS(get_option_quotes=lambda _: {},
        close_multileg=lambda *a, **kw: calls.append(kw) or {"id": "close"}))
    await readonly(database)
    with pytest.raises(DatabaseError):
        await session._manage_condor(row)
    assert calls == []
    assert (await database.get_active_condors())[0]["status"] == "open"


@pytest.mark.parametrize("failure", ["readonly", "commit", "missing_row"])
async def test_confirmation_requires_committed_matching_row(database, monkeypatch, failure):
    broker = Broker()
    engine, tid = await engine_with_trade(database, broker)
    await database.order_namespace()
    commit = database._conn.commit
    if failure == "readonly":
        await readonly(database)
    elif failure == "commit":
        async def failed_commit():
            raise OSError("disk full")
        monkeypatch.setattr(database._conn, "commit", failed_commit)
    else:
        await database._exec("DELETE FROM pending_trades WHERE id=?", (tid,), strict=True)
    result = await engine.confirm_trade(tid, 0)
    assert result["error"] and result["ambiguous"] is False
    assert broker.calls == [] and tid not in engine._uncertain_submissions
    assert tid in engine._pending
    if failure != "missing_row":
        assert (await database.get_pending_trades())[0]["status"] == "pending"
        await readonly(database, False)
        monkeypatch.setattr(database._conn, "commit", commit)
        assert (await engine.confirm_trade(tid, 0))["id"]
        assert len(broker.calls) == 1


async def test_post_confirmation_save_failure_recovers_without_second_post(database, monkeypatch):
    broker = Broker()
    engine, tid = await engine_with_trade(database, broker)
    update = database.update_pending_trade

    async def fail_confirmation(trade_id, **fields):
        if fields.get("status") == "confirmed":
            raise DatabaseError("disk full after acceptance")
        await update(trade_id, **fields)

    monkeypatch.setattr(database, "update_pending_trade", fail_confirmation)
    result = await engine.confirm_trade(tid, 0)
    assert result["ambiguous"] is True and len(broker.calls) == 1
    assert (await database.get_entry_reservations())[0]["status"] == "submitting"
    monkeypatch.setattr(database, "update_pending_trade", update)
    engine.record_loss("AAPL", -10000)  # recovery is bookkeeping, even while halted
    assert (await engine.confirm_trade(tid, 0))["id"] == "order-1"
    await engine.skip_trade(tid, 0)
    assert len(broker.calls) == 1
    assert (await database.get_pending_trades("confirmed"))[0]["id"] == tid


async def test_risk_reads_and_halt_writes_fail_closed(database):
    await readonly(database)
    with pytest.raises(DatabaseError):
        await database.set_halt("operator halt")
    await readonly(database, False)
    await database._exec("DELETE FROM risk_control", strict=True)
    with pytest.raises(DatabaseError):
        await database.get_risk_state()
    await database._exec("DROP TABLE risk_control", strict=True)
    with pytest.raises(DatabaseError):
        await database.get_risk_state()


async def test_safety_read_waits_for_failed_commit_rollback(database, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    commit = database._conn.commit

    async def failed_commit():
        entered.set()
        await release.wait()
        raise OSError("disk full")

    monkeypatch.setattr(database._conn, "commit", failed_commit)
    writing = asyncio.create_task(database.set_halt("not committed"))
    await entered.wait()
    reading = asyncio.create_task(database.get_risk_state())
    await asyncio.sleep(0)
    assert not reading.done()
    release.set()
    with pytest.raises(DatabaseError):
        await writing
    assert not (await reading)["halted"]
    monkeypatch.setattr(database._conn, "commit", commit)


@pytest.mark.parametrize("replacement_basis", [100, 200])
async def test_full_exit_retires_state_before_same_symbol_reentry(database, replacement_basis):
    calls, state = [], {}
    order = {"status": "accepted", "filled_qty": "0"}
    broker = NS(market_order=lambda *a, **kw: calls.append(kw) or {"id": f"exit-{len(calls)}"},
                get_order_raw=lambda _: order)
    await manage_position_exit(database, broker, POSITION, EXIT_SETTINGS, state)
    order = {"status": "filled", "filled_qty": "2", "filled_avg_price": "55"}
    replacement = {**POSITION, "avg_price": replacement_basis}
    await manage_position_exit(database, broker, replacement, EXIT_SETTINGS, state)
    assert state == {}
    state = (await database.get_position_monitor_states())["AAPL"]  # process restart
    await manage_position_exit(database, broker, replacement, EXIT_SETTINGS, state)
    assert len(calls) == 2 and state["pending"]["entry_price"] == replacement_basis
    assert calls[0]["client_order_id"] != calls[1]["client_order_id"]


async def test_partial_tp_keeps_trailing_progress_across_restart(database):
    state, calls = {}, []
    order = {"status": "filled", "filled_qty": "2", "filled_avg_price": "180"}
    broker = NS(market_order=lambda *a, **kw: calls.append((a, kw)) or {"id": "tp"},
                get_order_raw=lambda _: order)
    pos = {**POSITION, "qty": 4, "pnl_pct": 80}
    await manage_position_exit(database, broker, pos, EXIT_SETTINGS, state)
    await manage_position_exit(database, broker, {**pos, "qty": 2}, EXIT_SETTINGS, state)
    assert state["tp_fired"] and state["trailing"]
    restored = (await database.get_position_monitor_states())["AAPL"]
    await manage_position_exit(database, broker, {**pos, "qty": 2, "pnl_pct": 110}, EXIT_SETTINGS, restored)
    assert len(calls) == 1 and restored["high_watermark"] == 110
    await manage_position_exit(database, broker, {**pos, "qty": 2, "pnl_pct": 85}, EXIT_SETTINGS, restored)
    assert len(calls) == 2 and restored["pending"]["action"] == "trailing_stop"


async def test_tp_rounding_to_full_close_retires_state(database):
    state = {}
    broker = NS(market_order=lambda *a, **kw: {"id": "tp"}, get_order_raw=lambda _: {
        "status": "filled", "filled_qty": "1", "filled_avg_price": "180"})
    pos = {**POSITION, "qty": 1, "pnl_pct": 80}
    await manage_position_exit(database, broker, pos, EXIT_SETTINGS, state)
    await manage_position_exit(database, broker, pos, EXIT_SETTINGS, state)
    assert state == {}


@pytest.mark.parametrize("replacement", [{"avg_price": 200}, {"qty": 4}])
async def test_changed_basis_or_increased_size_rearms_one_shot_flags(database, replacement):
    state = {"position": {"qty": 2, "avg_price": 100}, "trimmed": True}
    calls = []
    broker = NS(market_order=lambda *a, **kw: calls.append(a) or {"id": "trim"})
    await manage_position_exit(database, broker, {**POSITION, "pnl_pct": -36, **replacement}, EXIT_SETTINGS, state)
    assert len(calls) == 1 and state["pending"]["action"] == "trim"


async def test_canceled_partial_stop_retries_only_remaining_position(database):
    calls, state, fills = [], {}, []
    broker = NS(market_order=lambda *a, **kw: calls.append(a) or {"id": "stop"},
        get_order_raw=lambda _: {"status": "canceled", "filled_qty": "1", "filled_avg_price": "55"})
    await manage_position_exit(database, broker, POSITION, EXIT_SETTINGS, state)
    remaining = {**POSITION, "qty": 1}
    await manage_position_exit(database, broker, remaining, EXIT_SETTINGS, state, lambda *a: fills.append(a))
    assert not state["sl_fired"] and fills == [("AAPL", -45)]
    await manage_position_exit(database, broker, remaining, EXIT_SETTINGS, state)
    assert [args[1] for args in calls] == [2, 1]


@pytest.mark.parametrize("block", ["breaker", "cooldown", "daily", "held", "open_buy", "unknown"])
async def test_confirmation_rechecks_current_limits(database, block):
    broker = Broker()
    engine, tid = await engine_with_trade(database, broker)
    if block == "breaker":
        engine.record_loss("OTHER", -10000)
    elif block == "cooldown":
        engine.record_loss("AAPL", -1)
    elif block == "daily":
        engine.settings.auto_trade_max_trades_per_day = 1
        prior = await another_trade(engine)
        await database.update_pending_trade(prior, status="confirmed", entry_order_status="filled",
                                           executed_at=datetime.now(timezone.utc).isoformat())
    elif block in ("held", "open_buy"):
        engine.settings.auto_trade_max_open_positions = 1
        if block == "held":
            broker.get_positions_raw = lambda: [{"symbol": "OTHER", "qty": "1"}]
        else:
            broker.get_open_orders_raw = lambda: [{"symbol": "OTHER", "side": "buy"}]
    else:
        engine.settings.auto_trade_max_open_positions = 1
        prior = await another_trade(engine)
        await database.update_pending_trade(prior, status="submission_unknown")
    result = await engine.confirm_trade(tid, 0)
    assert result["error"] and broker.calls == [] and tid in engine._pending


@pytest.mark.parametrize("snapshot", ["positions", "orders", "daily_db", "reservations_db", "invalid_qty"])
async def test_confirmation_blocks_when_capacity_cannot_be_verified(database, monkeypatch, snapshot):
    broker = Broker()
    engine, tid = await engine_with_trade(database, broker)
    async def unavailable(*a, **kw):
        raise DatabaseError("read unavailable")
    if snapshot == "positions":
        broker.get_positions_raw = lambda: None
    elif snapshot == "orders":
        broker.get_open_orders_raw = lambda: None
    elif snapshot == "daily_db":
        monkeypatch.setattr(database, "count_confirmed_today", unavailable)
    elif snapshot == "reservations_db":
        monkeypatch.setattr(database, "get_entry_reservations", unavailable)
    else:
        broker.get_positions_raw = lambda: [{"symbol": "OTHER", "qty": "NaN"}]
    assert (await engine.confirm_trade(tid, 0))["error"]
    assert broker.calls == []


@pytest.mark.parametrize("cap", ["daily", "positions"])
async def test_concurrent_cards_cannot_overrun_capacity(database, cap):
    broker = Broker()
    engine, first = await engine_with_trade(database, broker)
    second = await another_trade(engine)
    if cap == "daily":
        engine.settings.auto_trade_max_trades_per_day = 1
    else:
        engine.settings.auto_trade_max_open_positions = 1
    results = await asyncio.gather(engine.confirm_trade(first, 0), engine.confirm_trade(second, 0))
    assert len(broker.calls) == 1
    assert sum(bool(r.get("id")) for r in results) == 1
    assert len(await database.get_entry_reservations()) == 1


async def test_entry_reservations_survive_restart_and_release_after_cancel(database):
    broker = Broker()
    original, first = await engine_with_trade(database, broker)
    await original.confirm_trade(first, 0)
    restored, second = await engine_with_trade(database, broker)
    restored._pending[second].symbol = restored._pending[second].ticker = "MSFT"
    restored.settings.auto_trade_max_open_positions = 1
    assert (await restored.confirm_trade(second, 0))["error"]
    broker.by_id["order-1"] = {"status": "canceled", "filled_qty": "0"}
    assert (await restored.confirm_trade(second, 0))["id"] == "order-2"
    assert len(broker.calls) == 2


async def test_unresolved_daily_reservation_survives_midnight(database):
    engine, tid = await engine_with_trade(database, Broker())
    await database.update_pending_trade(tid, status="submitting")
    assert await database.count_confirmed_today("2030-01-01", include_unresolved=True) == 1


async def test_capacity_deduplicates_fills_and_includes_nested_entry_legs(database):
    broker = Broker()
    engine, tid = await engine_with_trade(database, broker)
    await engine.confirm_trade(tid, 0)
    broker.get_positions_raw = lambda: [{"symbol": "AAPL", "qty": "1"}]
    broker.get_open_orders_raw = lambda: [{"symbol": "AAPL", "side": "buy"},
        {"side": "sell", "legs": [{"symbol": "MSFT", "side": "buy", "position_intent": "buy_to_open"},
                                  {"symbol": "EXIT", "side": "buy", "position_intent": "buy_to_close"}]}]
    engine.settings.auto_trade_max_open_positions = 3
    assert (await engine._max_positions_check())[0] is True
    engine.settings.auto_trade_max_open_positions = 2
    assert (await engine._max_positions_check())[0] is False


async def test_terminal_partial_entry_still_reserves_capacity(database):
    broker = Broker()
    engine, tid = await engine_with_trade(database, broker)
    await engine.confirm_trade(tid, 0)
    engine.settings.auto_trade_max_open_positions = 1
    broker.by_id["order-1"] = {"status": "canceled", "filled_qty": "1"}
    assert (await engine._max_positions_check())[0] is False
    # Subsequent checks use the holding snapshot, after the entry is terminal.
    broker.get_positions_raw = lambda: [{"symbol": "AAPL", "qty": "1"}]
    assert (await engine._max_positions_check())[0] is False


async def test_snapshot_failure_does_not_release_terminal_entry_reservation(database):
    broker = Broker()
    engine, tid = await engine_with_trade(database, broker)
    await engine.confirm_trade(tid, 0)
    broker.by_id["order-1"] = {"status": "filled", "filled_qty": "1"}
    broker.get_positions_raw = lambda: None
    assert (await engine._max_positions_check())[0] is False
    assert (await database.get_entry_reservations())[0]["id"] == tid
    broker.get_positions_raw = lambda: [{"symbol": "AAPL", "qty": "1"}]
    await engine._max_positions_check()
    assert await database.get_entry_reservations() == []


async def test_loss_during_snapshot_fetch_blocks_confirmation(database):
    broker = Broker()
    engine, tid = await engine_with_trade(database, broker)
    def positions():
        engine.record_loss("OTHER", -10000)
        return []
    broker.get_positions_raw = positions
    assert (await engine.confirm_trade(tid, 0))["error"].startswith("Circuit breaker")
    assert not broker.calls


@pytest.mark.parametrize("day,start,end", [
    ("2026-10-04", "2026-10-04T04:00:00+00:00", "2026-10-05T04:00:00+00:00"),
    ("2026-11-01", "2026-11-01T04:00:00+00:00", "2026-11-02T05:00:00+00:00"),
])
async def test_daily_cap_uses_execution_day_in_et(database, day, start, end):
    start_dt, end_dt = datetime.fromisoformat(start), datetime.fromisoformat(end)
    for execution in (start_dt - timedelta(seconds=1), start_dt, end_dt - timedelta(seconds=1), end_dt):
        tid = await database.save_pending_trade(end_dt, ticker="TEST")
        await database._exec("UPDATE pending_trades SET created_at=? WHERE id=?",
                            ("2020-01-01T00:00:00", tid), strict=True)
        await database.update_pending_trade(tid, status="confirmed", executed_at=execution.isoformat())
    assert await database.count_confirmed_today(day) == 2


@pytest.mark.parametrize("code,body,expected", [(200, [], []), (500, {}, None), (200, {}, None), (200, [{}] * 500, None)])
def test_open_order_snapshot_distinguishes_failure_and_incomplete_page(code, body, expected):
    trader = object.__new__(AlpacaTrader)
    trader._trade_base = "https://example.test"
    trader._rest = lambda *a: (code, body)
    assert trader.get_open_orders_raw() == expected


def test_live_autonomous_configuration_is_rejected():
    with pytest.raises(ValidationError, match="requires ALPACA_PAPER=true"):
        Settings(_env_file=None, alpaca_api_key="unused", alpaca_secret_key="unused",
                 alpaca_paper=False, auto_trade_auto_execute=True)
    assert not Settings(_env_file=None, alpaca_api_key="unused", alpaca_secret_key="unused",
                        alpaca_paper=False, auto_trade_auto_execute=False).alpaca_paper


QUEUE_ARGS = dict(ticker="AAPL", symbol="AAPL", trade_type="equity", side="bullish",
                  qty=1, limit_price=100, risk_amount=100, stop_pct=5, target_pct=15)


@pytest.mark.parametrize("settings_paper,broker_paper", [(True, False), (False, True)])
async def test_runtime_guard_blocks_mismatched_autonomous_modes(database, settings_paper, broker_paper):
    broker = Broker()
    engine, tid = await engine_with_trade(database, broker)
    engine.settings.auto_trade_auto_execute = True
    engine.settings.alpaca_paper = settings_paper  # bypass startup validation deliberately
    broker.paper = broker_paper
    result = await engine.confirm_trade(tid, 0, autonomous=True)
    assert result["error"] and not broker.calls
    engine._pending.clear()
    before = len(await database.get_pending_trades())
    await engine._queue(**QUEUE_ARGS)
    assert len(await database.get_pending_trades()) == before and not broker.calls


async def test_valid_paper_queue_executes_while_manual_live_confirm_remains_allowed(database):
    broker = Broker()
    engine, tid = await engine_with_trade(database, broker)
    engine._pending.clear()
    await database.update_pending_trade(tid, status="skipped")
    engine.settings.auto_trade_auto_execute = True
    await engine._queue(**QUEUE_ARGS)
    assert len(broker.calls) == 1 and not engine._pending
    live, tid = await engine_with_trade(database, broker)
    live.settings.alpaca_paper = broker.paper = False
    assert (await live.confirm_trade(tid, 0))["id"] == "order-2"


async def test_autonomous_guard_runs_again_after_queue_notifications(database, monkeypatch):
    broker = Broker()
    engine, tid = await engine_with_trade(database, broker)
    engine._pending.clear()
    await database.update_pending_trade(tid, status="skipped")
    engine.settings.auto_trade_auto_execute = True

    async def change_broker_mode(_):
        broker.paper = False

    async def expire(_):
        return

    engine._telegram = NS(enabled=True, send_trade_alert=change_broker_mode)
    monkeypatch.setattr(engine, "_expire", expire)
    await engine._queue(**QUEUE_ARGS)
    assert broker.calls == [] and len(engine._pending) == 1


async def test_monitor_retries_failed_state_restore(database, session, monkeypatch):
    from feeds import uw_budget
    reads, sleeps = [], []

    async def restore():
        reads.append(True)
        if len(reads) == 1:
            raise DatabaseError("temporarily locked")
        return {"AAPL": {"tp_fired": True}}

    async def tick(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "_alpaca_pos_state", {})
    monkeypatch.setattr(database, "get_position_monitor_states", restore)
    monkeypatch.setattr(uw_budget, "current_session", lambda: "overnight")
    monkeypatch.setattr(session.asyncio, "sleep", tick)
    with pytest.raises(asyncio.CancelledError):
        await session.alpaca_position_monitor()
    assert len(reads) == 2 and session._alpaca_pos_state["AAPL"]["tp_fired"]


async def test_failed_skip_preserves_pending_card(database):
    engine, tid = await engine_with_trade(database, Broker())
    await readonly(database)
    with pytest.raises(DatabaseError):
        await engine.skip_trade(tid, 0)
    assert tid in engine._pending


async def test_expiry_waits_for_inflight_confirmation(database, monkeypatch):
    broker = Broker()
    engine, tid = await engine_with_trade(database, broker)
    entered, release = asyncio.Event(), asyncio.Event()
    update = database.update_pending_trade

    async def hold_submission(trade_id, **fields):
        if fields.get("status") == "submitting":
            entered.set()
            await release.wait()
        await update(trade_id, **fields)

    async def no_wait(_):
        return

    monkeypatch.setattr(database, "update_pending_trade", hold_submission)
    confirming = asyncio.create_task(engine.confirm_trade(tid, 0))
    await entered.wait()
    monkeypatch.setattr("signals.auto_trade.asyncio.sleep", no_wait)
    expiring = asyncio.create_task(engine._expire(tid))
    release.set()
    await asyncio.gather(confirming, expiring)
    assert len(broker.calls) == 1
    assert (await database.get_pending_trades("confirmed"))[0]["id"] == tid

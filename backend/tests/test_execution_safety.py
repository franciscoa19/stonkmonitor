"""Broker failure-path regressions. All broker responses are local fakes."""
import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio

from db import Database
from config import Settings
from signals.position_exits import manage_position_exit
from signals.auto_trade import AutoTradeEngine
from trading.alpaca_trader import AlpacaTrader


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


async def condor(db, expiry="2026-11-27", qty=2):
    legs = [{"symbol": symbol, "side": side, "ratio_qty": 1}
            for symbol, side in (("SC", "sell"), ("LC", "buy"), ("SP", "sell"), ("LP", "buy"))]
    cid = await db.record_condor("TEST", "2026-11-25", expiry, json.dumps(legs),
        {"short_put": 90, "long_put": 85, "short_call": 110, "long_call": 115},
        qty, 1, 400, "entry", "filled")
    await db.activate_condor(cid, qty, -1)
    return (await db.get_active_condors())[0]


@pytest.fixture
def session(monkeypatch):
    import main
    import market_time
    now = datetime(2026, 11, 27, 12, 30, tzinfo=market_time.ET)
    monkeypatch.setattr(market_time, "et_today", lambda *a: now.date())
    monkeypatch.setattr(market_time, "et_now", lambda *a: now)
    monkeypatch.setattr(main, "is_rth_now", lambda: True)
    monkeypatch.setattr(main, "feed", NS(get_latest_quote=lambda _: {"bid": 91, "ask": 91}))
    # These tests use fake brokers; the account guard has its own tests.
    monkeypatch.setattr(main, "_account_bound", True)
    monkeypatch.setattr(main, "_account_bind_lock", asyncio.Lock())
    return main


async def test_expiry_replaces_old_close_after_cancel_and_books_partial_fills(database, session, monkeypatch):
    main = session
    monkeypatch.setattr(main, "db", database)
    row = await condor(database)
    await database.mark_condor_closing(row["id"], "old")
    broker_order = {"status": "accepted", "limit_price": "0.5", "filled_qty": "0"}
    submissions, cancels = [], []

    def submit(legs, qty, limit, **kw):
        submissions.append((qty, limit))
        return {"id": "new"}

    broker = NS(get_order_raw=lambda _: broker_order,
                cancel_order_raw=lambda oid: cancels.append(oid) or True,
                get_option_quotes=lambda _: {}, close_multileg=submit)
    monkeypatch.setattr(main, "trader", broker)
    async def manage():
        await main._manage_condor((await database.get_active_condors())[0])
    await manage()
    assert cancels == ["old"] and not submissions
    # A pending cancellation is not permission to submit an overlapping close.
    broker_order = {"status": "pending_cancel", "filled_qty": "1", "filled_avg_price": "0.4"}
    await manage()
    await manage()  # replay the same cumulative fill
    assert not submissions
    broker_order = {"status": "canceled", "filled_qty": "1", "filled_avg_price": "0.4"}
    await manage()
    assert submissions == [(1, 5.0)]  # remaining spread; early-close ceiling
    broker_order = {"status": "filled", "filled_qty": "1", "filled_avg_price": "0.6"}
    await manage()
    result = (await database._query("SELECT * FROM iv_condors"))[0]
    assert result["status"] == "closed"
    assert result["closed_qty"] == 2
    assert result["pnl"] == pytest.approx(100)
    assert result["exit_debit"] == pytest.approx(0.5)


async def test_close_timeout_is_reconciled_without_resubmitting(database, session, monkeypatch):
    monkeypatch.setattr(session, "db", database)
    await condor(database)
    submissions = []
    broker = NS(get_option_quotes=lambda _: {},
                close_multileg=lambda *a, **kw: submissions.append(kw) or {"error": "timeout", "ambiguous": True},
                get_order_by_client_id=lambda _: {"not_found": True, "error": "404"})
    monkeypatch.setattr(session, "trader", broker)
    await session._manage_condor((await database.get_active_condors())[0])
    await session._manage_condor((await database.get_active_condors())[0])
    assert len(submissions) == 1
    assert (await database.get_active_condors())[0]["close_client_order_id"]


@pytest.mark.parametrize("event,expected", [(None, "awaiting_settlement"), ("OPEXP", "closed"), ("OPASN", "awaiting_settlement")])
async def test_expiry_requires_broker_evidence(database, session, monkeypatch, event, expected):
    monkeypatch.setattr(session, "db", database)
    row = await condor(database, expiry="2026-11-25", qty=1)
    activities = [] if event is None else [
        {"id": symbol, "activity_type": event, "symbol": symbol, "qty": "1",
         "status": "executed", "date": row["expiry"]} for symbol in ("SC", "LC", "SP", "LP")]
    monkeypatch.setattr(session, "trader", NS(get_option_activities=lambda _: activities,
                                             get_positions_raw=lambda: []))
    await session._manage_condor(row)
    result = (await database._query("SELECT * FROM iv_condors"))[0]
    assert result["status"] == expected
    assert result["pnl"] == (100 if event == "OPEXP" else None)
    if event == "OPASN":
        assert (await database.get_risk_state())["halted"]
        assert await database.has_open_condor("TEST")


EXIT_SETTINGS = NS(pos_sl_pct=-40, pos_trim_pct=-35, pos_trim_sell_pct=.5,
                   pos_tp_pct=80, pos_tp_sell_pct=.5, pos_tp2_pct=175,
                   pos_tp2_sell_pct=1, pos_trail_pct=20, pos_trail_after_tp=True)
POSITION = {"symbol": "AAPL", "qty": 2, "pnl_pct": -45, "avg_price": 100}


async def test_rejected_stop_can_retry_and_waits_for_fill(database):
    state, calls = {}, []
    response = {"error": "rejected", "ambiguous": False}
    order = {"status": "accepted", "filled_qty": "0"}
    trader = NS(market_order=lambda *a, **kw: calls.append(kw) or response,
                get_order_raw=lambda _: order)
    await manage_position_exit(database, trader, POSITION, EXIT_SETTINGS, state)
    assert not state.get("sl_fired") and not state.get("pending")
    response = {"id": "stop"}
    await manage_position_exit(database, trader, POSITION, EXIT_SETTINGS, state)
    restored = (await database.get_position_monitor_states())["AAPL"]
    assert restored["pending"]["order_id"] == "stop"
    await manage_position_exit(database, trader, POSITION, EXIT_SETTINGS, restored)
    assert len(calls) == 2 and not restored.get("sl_fired")
    order = {"status": "filled", "filled_qty": "2", "filled_avg_price": "55"}
    fills = []
    await manage_position_exit(database, trader, POSITION, EXIT_SETTINGS, restored,
                               lambda *a: fills.append(a))
    assert restored == {}  # a full exit retires flags before same-symbol re-entry
    assert fills == [("AAPL", -90)]


async def test_ambiguous_stop_survives_restart_without_second_order(database):
    calls, state = [], {}
    trader = NS(market_order=lambda *a, **kw: calls.append(kw) or {"error": "timeout", "ambiguous": True},
                get_order_by_client_id=lambda _: {"error": "temporarily unavailable"})
    await manage_position_exit(database, trader, POSITION, EXIT_SETTINGS, state)
    restored = (await database.get_position_monitor_states())["AAPL"]
    await manage_position_exit(database, trader, POSITION, EXIT_SETTINGS, restored)
    assert len(calls) == 1 and restored["pending"]


async def engine_with_trade(db, trader):
    engine = AutoTradeEngine(Settings(_env_file=None, alpaca_api_key="unused",
        alpaca_secret_key="unused", auto_trade_auto_execute=False))
    trader.get_positions_raw = lambda: []
    trader.get_open_orders_raw = lambda: []
    trader.get_account = lambda: {"equity": 100000, "cash": 100000,
        "options_buying_power": 100000, "non_marginable_buying_power": 100000}
    for name, value in (("get_order_history", []), ("get_orders", []),
                        ("get_fill_activities", []), ("get_mleg_leg_order_ids", set())):
        if not hasattr(trader, name):
            setattr(trader, name, lambda *a, _value=value, **kw: _value)
    async def verified_account():
        return
    engine.set_dependencies(None, db, trader, account_check=verified_account)
    tid = await db.save_pending_trade(datetime.now(timezone.utc) + timedelta(minutes=5),
                                      ticker="AAPL", symbol="AAPL", trade_type="equity",
                                      qty=1, limit_price=100, risk_amount=100)
    engine._pending[tid] = NS(symbol="AAPL", qty=1, limit_price=100, target_pct=15,
        stop_pct=5, expires_at=datetime.utcnow() + timedelta(minutes=5),
        ticker="AAPL", trade_type="equity", option_type=None)
    return engine, tid


@pytest.mark.parametrize("unsupported", [False, True])
async def test_bracket_fallback_requires_definitive_unsupported_response(database, unsupported):
    calls = []
    broker = NS(get_order_by_client_id=lambda _: {"error": "404", "not_found": True},
        bracket_order=lambda **kw: calls.append("bracket") or {
            "error": "unsupported" if unsupported else "timeout",
            "ambiguous": not unsupported, "unsupported_bracket": unsupported},
        limit_order=lambda **kw: calls.append("limit") or {"id": "plain"})
    engine, tid = await engine_with_trade(database, broker)
    await engine.confirm_trade(tid, 0)
    assert calls == (["bracket", "limit"] if unsupported else ["bracket"])
    if not unsupported:
        await engine.confirm_trade(tid, 0)
        assert calls == ["bracket"]
        assert (await database.get_pending_trades("submission_unknown"))[0]["id"] == tid


async def test_double_confirmation_submits_once(database):
    calls = []
    broker = NS(get_order_by_client_id=lambda _: {"error": "404", "not_found": True},
                bracket_order=lambda **kw: calls.append(kw) or {"id": "one"})
    engine, tid = await engine_with_trade(database, broker)
    await asyncio.gather(engine.confirm_trade(tid, 0), engine.confirm_trade(tid, 0))
    assert len(calls) == 1


def test_sdk_market_data_models():
    from alpaca.data.models import BarSet, OptionsSnapshot
    from feeds.alpaca_feed import AlpacaFeed
    feed = object.__new__(AlpacaFeed)
    feed.stock_client = NS(get_stock_bars=lambda _: BarSet({"AAPL": [{"t": "2026-10-01T04:00:00Z",
        "o": 100, "h": 102, "l": 99, "c": 101, "v": 1000, "n": 10, "vw": 100.5}]}))
    assert feed.get_bars("AAPL")[0]["c"] == 101
    feed.option_client = NS(get_option_chain=lambda _: {
        "AAPL261016C00100000": OptionsSnapshot("AAPL261016C00100000", {"impliedVolatility": .3})})
    option = feed.get_option_chain("AAPL")[0]
    assert (option["strike"], option["type"], option["expiry"]) == (100, "call", "2026-10-16")


def test_submission_timeout_recovers_accepted_client_id():
    trader = object.__new__(AlpacaTrader)
    trader.client = NS(submit_order=lambda _: (_ for _ in ()).throw(TimeoutError()))
    trader.get_order_by_client_id = lambda _: {"id": "accepted", "status": "new", "order_class": "bracket"}
    result = trader.bracket_order("AAPL", 1, "buy", 100, 115, 95, client_order_id="stable")
    assert result["id"] == "accepted" and not result.get("unsupported_bracket")


def test_condor_risk_uses_minimum_accepted_credit():
    from signals.iv_executor import build_iron_condor
    from market_time import et_today
    today = et_today()
    expiry = today + timedelta(days=3)
    broker = NS(get_option_contracts=lambda tk, start, end, kind: [
        {"symbol": f"{kind}-{strike}", "strike": strike, "expiry": expiry}
        for strike in ([110, 115] if kind == "call" else [85, 90])],
        get_option_quotes=lambda syms: {s: {"bid": 1.5 if s in ("call-110", "put-90") else 1,
                                          "ask": 1.5 if s in ("call-110", "put-90") else 1} for s in syms})
    settings = NS(iv_exec_min_dte=1, iv_exec_max_dte=7, iv_exec_short_move_mult=1,
        iv_exec_wing_width_pct=.05, iv_exec_risk_pct=.016,
        iv_exec_max_risk_usd=800, iv_exec_min_credit=.25)
    plan = build_iron_condor(broker, NS(ticker="TEST", price=100, expected_move="10%",
        next_earnings_date=(today + timedelta(days=1)).isoformat()), 50000, settings)
    assert plan["ok"] and plan["qty"] == 1
    assert plan["limit_price"] == -.9 and plan["risk_usd"] == 410


async def test_missing_close_quantity_does_not_invent_a_fill(database, session, monkeypatch):
    monkeypatch.setattr(session, "db", database)
    row = await condor(database)
    await database.mark_condor_closing(row["id"], "close")
    monkeypatch.setattr(session, "trader", NS(get_order_raw=lambda _: {
        "status": "filled", "filled_avg_price": "0.4"}))
    await session._manage_condor((await database.get_active_condors())[0])
    result = (await database.get_active_condors())[0]
    assert result["closed_qty"] == 0 and result["pnl"] is None


async def test_telegram_does_not_dispatch_unauthorized_callback():
    from notifications.telegram import TelegramNotifier
    bot = TelegramNotifier("unused", 123)
    calls = []
    async def callback(*a): calls.append(a)
    bot._on_confirm = callback
    bot.answer_callback = callback
    await bot._handle_update({"callback_query": {"id": "tap", "data": "confirm_1",
        "from": {"id": 999}, "message": {"message_id": 1, "chat": {"id": 123}}}})
    assert calls == [("tap", "Unauthorized")]


@pytest.mark.parametrize("host,client,origin,allowed", [
    ("localhost", "127.0.0.1", "http://localhost:3000", True),
    ("localhost", "192.168.1.2", None, False),
    ("attacker.test", "127.0.0.1", None, False),
    ("localhost", "127.0.0.1", "https://attacker.test", False)])
def test_local_access_boundary(host, client, origin, allowed):
    from api.local_access import local_access_allowed
    request = NS(client=NS(host=client), url=NS(hostname=host),
                 headers={} if origin is None else {"origin": origin})
    assert local_access_allowed(request, {"http://localhost:3000"}) is allowed


def test_report_push_uses_remote_base_and_preserves_developer_index(tmp_path, monkeypatch):
    import main
    local, remote = tmp_path / "local", tmp_path / "remote.git"
    local.mkdir()
    def git(args, cwd=local):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout
    git(["init", "-b", "main"])
    git(["config", "user.name", "Test"])
    git(["config", "user.email", "test@example.test"])
    files = ["history.jsonl", "trades.csv", "latest.json"]
    reports = local / "backend" / "reports"
    reports.mkdir(parents=True)
    for file in files: (reports / file).write_text("old")
    git(["add", "."])
    git(["commit", "-m", "base"])
    git(["init", "--bare", str(remote)])
    git(["remote", "add", "origin", str(remote)])
    git(["push", "origin", "main"])
    # An unpushed code commit and an unrelated staged edit must stay local.
    (local / "app.py").write_text("unreviewed")
    git(["add", "app.py"])
    git(["commit", "-m", "unreviewed code"])
    (local / "staged.txt").write_text("private draft")
    git(["add", "staged.txt"])
    before, head = git(["diff", "--cached"]), git(["rev-parse", "HEAD"])
    for file in files: (reports / file).write_text("new")
    monkeypatch.setattr(main, "__file__", str(local / "backend" / "main.py"))
    assert main._git_push_eval_data("2026-10-02") is True
    assert git(["diff", "--cached"]) == before and git(["rev-parse", "HEAD"]) == head
    changed = git(["--git-dir", str(remote), "show", "--format=", "--name-only", "main"]).decode().splitlines()
    assert set(changed) == {f"backend/reports/{f}" for f in files}
    assert b"app.py" not in git(["--git-dir", str(remote), "ls-tree", "--name-only", "main"])


async def test_order_namespace_is_stable_per_db_and_new_after_reset(tmp_path):
    first = Database(tmp_path / "a.db")
    await first.connect()
    ns = await first.order_namespace()
    await first.close()
    reopened = Database(tmp_path / "a.db")
    await reopened.connect()
    assert await reopened.order_namespace() == ns and ns
    await reopened.close()
    recreated = Database(tmp_path / "b.db")      # a reset: fresh file, ids restart at 1
    await recreated.connect()
    assert await recreated.order_namespace() not in ("", ns)
    await recreated.close()


async def test_recreated_db_never_adopts_an_old_condor_entry(tmp_path, monkeypatch):
    """2026-10-02 reset: row ids restarted at 1. A broker order placed by the old
    DB's condor #1 must not be recovered as the new DB's condor #1 entry."""
    import main
    monkeypatch.setattr(main, "_account_bound", True)  # fake brokers, identity tested separately
    legs = json.dumps([{"symbol": "SC", "side": "sell", "ratio_qty": 1}])
    strikes = {"short_put": 90, "long_put": 85, "short_call": 110, "long_call": 115}
    broker_orders = {}                            # client_order_id -> broker order
    old, new = Database(tmp_path / "old.db"), Database(tmp_path / "new.db")
    await old.connect()
    await new.connect()
    try:
        old_cid = await old.record_condor("OLD", "2026-09-28", "2026-10-02", legs, strikes,
                                          2, 1, 400, "OLD-ORDER", "filled")
        broker_orders[main._condor_entry_client_id(await old.order_namespace(), old_cid)] = {
            "id": "OLD-ORDER", "status": "filled", "filled_qty": "2", "filled_avg_price": "-1"}
        new_cid = await new.record_condor("NEW", "2026-11-25", "2026-11-27", legs, strikes,
                                          2, 1, 400, None, "submitting")
        assert new_cid == old_cid                 # the collision precondition
        monkeypatch.setattr(main, "db", new)
        monkeypatch.setattr(main, "trader", NS(
            get_order_by_client_id=lambda cid: broker_orders.get(cid, {"error": "404", "not_found": True}),
            get_order_raw=lambda oid: next(o for o in broker_orders.values() if o["id"] == oid),
            get_option_quotes=lambda symbols: {}))
        await main._manage_condor((await new.get_active_condors())[0])
        row = (await new.get_active_condors())[0]
        assert row["entry_order_id"] is None, "recovered another database's order"
    finally:
        await old.close()
        await new.close()



async def test_condor_entry_waits_for_regular_hours(database, session, monkeypatch):
    """The scanner runs pre-market, after the close and overnight. An entry
    planned there is sized and limited off stale option marks, so nothing is
    recorded or submitted until regular hours."""
    from signals import iv_executor
    plan = {"ok": True, "expiry": "2026-11-27", "legs_json": "[]", "legs": [],
            "strikes": {"short_put": 90, "long_put": 85, "short_call": 110, "long_call": 115},
            "qty": 1, "credit": 1, "max_loss": 400, "limit_price": -1, "risk_usd": 400}
    calls, planned = [], []
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "settings", Settings(
        _env_file=None, alpaca_api_key="unused", alpaca_secret_key="unused",
        iv_exec_enabled=True, auto_trade_auto_execute=False))
    monkeypatch.setattr(session, "trader", NS(get_account=lambda: {"equity": 100000},
        get_positions_raw=lambda: [], get_open_orders_raw=lambda: [],
        multileg_order=lambda *a, **kw: calls.append(kw) or {"id": "entry", "status": "accepted"}))
    monkeypatch.setattr(iv_executor, "is_pre_earnings_entry_window", lambda *a, **kw: True)
    monkeypatch.setattr(iv_executor, "build_iron_condor", lambda *a: planned.append(a) or plan)
    setup = NS(ticker="TEST", recommendation="SELL_PREMIUM",
               next_earnings_date="2026-11-25", earnings_report_time="AMC")

    monkeypatch.setattr(session, "is_rth_now", lambda: False)
    await session.maybe_execute_condor(setup)
    assert calls == [] and planned == [] and await database.get_active_condors() == []

    monkeypatch.setattr(session, "is_rth_now", lambda: True)
    await session.maybe_execute_condor(setup)
    assert len(calls) == 1 and len(await database.get_active_condors()) == 1

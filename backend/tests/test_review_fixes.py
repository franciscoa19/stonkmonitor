"""October review regressions: temporary databases and fake brokers only."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from db import DatabaseError
from signals.auto_trade import AutoTradeEngine
from trading.manual_orders import manual_order_request
from trading.ownership import option_ownership
from test_execution_safety import database, session, condor, engine_with_trade
from trading.alpaca_trader import AlpacaTrader

SYMBOLS = ["TEST261009C00110000", "TEST261009C00115000", "TEST261009P00090000", "TEST261009P00085000"]


def positions():
    return [{"symbol": s, "qty": str(-2 if i % 2 == 0 else 2),
             "avg_entry_price": "1", "unrealized_plpc": "-0.6"} for i, s in enumerate(SYMBOLS)]


@pytest.mark.parametrize("status", ["pending_entry", "open", "closing", "awaiting_settlement"])
async def test_disarmed_condor_monitor_still_manages_existing_risk(database, session, monkeypatch, status):
    row = await condor(database)
    await database._exec("UPDATE iv_condors SET status=?", (status,), strict=True)
    called = []
    async def manage(c):
        called.append((c["id"], c["status"]))
    async def sleep(seconds):
        if seconds == 300:
            raise asyncio.CancelledError
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "settings", session.settings.model_copy(update={"iv_exec_enabled": False}))
    monkeypatch.setattr(session, "_manage_condor", manage)
    monkeypatch.setattr(session.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await session.iv_condor_monitor_loop()
    assert called == [(row["id"], status)]


async def test_disarmed_monitor_reconciles_real_expiry_evidence(database, session, monkeypatch):
    row = await condor(database, expiry="2026-11-25", qty=1)
    events = [{"id": s, "activity_type": "OPEXP", "symbol": s, "qty": "1",
               "status": "executed", "date": row["expiry"]} for s in ("SC", "LC", "SP", "LP")]
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "settings", session.settings.model_copy(update={"iv_exec_enabled": False}))
    monkeypatch.setattr(session, "trader", NS(get_option_activities=lambda _: events, get_positions_raw=lambda: []))
    async def sleep(seconds):
        if seconds == 300:
            raise asyncio.CancelledError
    monkeypatch.setattr(session.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await session.iv_condor_monitor_loop()
    result = (await database._query("SELECT * FROM iv_condors"))[0]
    assert result["status"] == "closed" and result["pnl"] == 100


def test_measurement_snapshot_preserves_quotes_and_excludes_credentials():
    from signals.measurement_capture import measurement_snapshot
    quote = {"bid": 1, "ask": 2, "timestamp": "2026-10-06T14:00:00+00:00", "feed": "opra"}
    snapshot = measurement_snapshot(NS(ticker="TEST", price=100),
        NS(alpaca_options_feed="opra", alpaca_api_key="never-archive-credentials"), [],
        {"market": {"quotes": {SYMBOLS[0]: quote}}}, action="fresh", lead_days=1, gate_passed=True)
    assert snapshot["diagnostics"]["market"]["quotes"][SYMBOLS[0]] == quote
    assert "never-archive-credentials" not in json.dumps(snapshot)
    assert snapshot["config"]["alpaca_options_feed"] == "opra"


@pytest.mark.parametrize("working_spread", [False, True])
async def test_unknown_spread_wings_are_quarantined(database, session, monkeypatch, working_spread):
    from feeds import uw_budget
    calls = []
    orders = [{"id": "manual-spread", "order_class": "mleg", "legs": [{"symbol": s} for s in SYMBOLS]}] if working_spread else []
    broker = NS(get_positions_raw=positions, get_open_orders_raw=lambda: orders,
                market_order=lambda *a, **kw: calls.append(a) or {"id": "bad-exit"})
    ownership = await option_ownership(database, broker)
    assert set(SYMBOLS) <= ownership["protected_symbols"]
    assert ownership["entry_block_reason"]
    async def sleep(seconds):
        if seconds != 45:
            raise asyncio.CancelledError
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "trader", broker)
    monkeypatch.setattr(session, "_alpaca_pos_state", {})
    monkeypatch.setattr(uw_budget, "current_session", lambda: "rth")
    monkeypatch.setattr(session.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await session.alpaca_position_monitor()
    assert calls == []


@pytest.mark.parametrize("legs", ["broken", "[]", '[{"symbol":"ONE"}]'])
async def test_malformed_active_ownership_fails_closed(database, legs):
    await condor(database)
    await database._exec("UPDATE iv_condors SET legs_json=?", (legs,), strict=True)
    with pytest.raises(DatabaseError):
        await option_ownership(database, NS(get_positions_raw=positions, get_open_orders_raw=lambda: []))


async def test_unavailable_or_partial_broker_ownership_fails_closed(database):
    for ps, os in ((None, []), ([], None), ([{"qty": "NaN"}], [])):
        with pytest.raises(DatabaseError):
            await option_ownership(database, NS(get_positions_raw=lambda: ps, get_open_orders_raw=lambda: os))


@pytest.mark.parametrize("account", [{}, {"equity": 0}, {"equity": -1}, {"equity": "bad"},
                                       {"equity": float("nan")}, {"equity": float("inf")},
                                       {"equity": 50000, "error": "unavailable"}])
async def test_iv_entry_requires_verified_equity(database, session, monkeypatch, account):
    from signals import iv_executor
    planned = []
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "settings", session.settings.model_copy(update={"iv_exec_enabled": True}))
    monkeypatch.setattr(session, "trader", NS(get_account=lambda: account))
    monkeypatch.setattr(session.auto_trade, "_cached_equity", 100000)
    monkeypatch.setattr(iv_executor, "is_pre_earnings_entry_window", lambda *a, **kw: True)
    monkeypatch.setattr(iv_executor, "build_iron_condor", lambda *a: planned.append(a))
    await session.maybe_execute_condor(NS(ticker="TEST", recommendation="SELL_PREMIUM",
        next_earnings_date="2026-11-25", earnings_report_time="post"))
    assert planned == [] and await database.get_active_condors() == []


async def test_unknown_holdings_block_new_iv_entries(database, session, monkeypatch):
    from signals import iv_executor
    planned = []
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "settings", session.settings.model_copy(update={"iv_exec_enabled": True}))
    monkeypatch.setattr(session, "trader", NS(get_account=lambda: {"equity": 50000},
        get_positions_raw=positions, get_open_orders_raw=lambda: []))
    monkeypatch.setattr(iv_executor, "is_pre_earnings_entry_window", lambda *a, **kw: True)
    monkeypatch.setattr(iv_executor, "build_iron_condor", lambda *a: planned.append(a))
    await session.maybe_execute_condor(NS(ticker="NEW", recommendation="SELL_PREMIUM",
        next_earnings_date="2026-11-25", earnings_report_time="post"))
    assert planned == []


async def test_local_spread_ownership_uses_remaining_quantity_and_side(database):
    row = await condor(database)
    legs = [{"symbol": s, "side": "sell" if i % 2 == 0 else "buy"} for i, s in enumerate(SYMBOLS)]
    await database._exec("UPDATE iv_condors SET legs_json=?", (json.dumps(legs),), strict=True)
    broker = NS(get_positions_raw=positions, get_open_orders_raw=lambda: [])
    assert not (await option_ownership(database, broker))["entry_block_reason"]
    await database._exec("UPDATE iv_condors SET closed_qty=1 WHERE id=?", (row["id"],), strict=True)
    assert (await option_ownership(database, broker))["entry_block_reason"]
    broker.get_positions_raw = lambda: [{"symbol": SYMBOLS[0], "qty": "1"}]
    assert (await option_ownership(database, broker))["entry_block_reason"]


class ManualBroker:
    def __init__(self):
        self.calls = []
        self.order = None
        self.response = {"error": "timeout", "ambiguous": True}
    def get_order_by_client_id(self, client_id):
        return self.order or {"not_found": True}
    def market_order(self, ticker, qty, side, tif, **kw):
        self.calls.append(kw["client_order_id"])
        return self.response
    def accepted(self):
        self.order = {"id": "accepted", "symbol": "AAPL", "qty": "1", "side": "buy",
                      "type": "market", "time_in_force": "day", "status": "new"}


PAYLOAD = {"ticker": "AAPL", "qty": 1.0, "side": "buy", "order_type": "market", "limit_price": None, "tif": "day"}


async def test_manual_timeout_retry_and_restart_never_post_twice(database):
    broker, rid = ManualBroker(), str(uuid4())
    assert (await manual_order_request(database, broker, rid, PAYLOAD))["status"] == "pending"
    assert (await manual_order_request(database, broker, rid, PAYLOAD))["status"] == "pending"
    assert (await manual_order_request(database, broker, rid))["status"] == "pending"
    broker.accepted()
    assert (await manual_order_request(database, broker, rid))["id"] == "accepted"
    assert (await manual_order_request(database, broker, rid, PAYLOAD))["id"] == "accepted"
    assert len(broker.calls) == 1 and len(broker.calls[0]) <= 48


async def test_manual_id_survives_lost_local_record(database):
    broker, rid = ManualBroker(), str(uuid4())
    await manual_order_request(database, broker, rid, PAYLOAD)
    broker.accepted()
    await database._exec("DELETE FROM manual_order_requests", strict=True)
    assert (await manual_order_request(database, broker, rid, PAYLOAD))["id"] == "accepted"
    assert len(broker.calls) == 1


async def test_manual_double_click_and_changed_payload(database):
    broker, rid = ManualBroker(), str(uuid4())
    await asyncio.gather(*(manual_order_request(database, broker, rid, PAYLOAD) for _ in range(2)))
    assert len(broker.calls) == 1
    with pytest.raises(ValueError):
        await manual_order_request(database, broker, rid, {**PAYLOAD, "qty": 2})


async def test_manual_persistence_failure_prevents_post(database):
    broker = ManualBroker()
    await database._conn.execute("PRAGMA query_only=ON")
    with pytest.raises(DatabaseError):
        await manual_order_request(database, broker, str(uuid4()), PAYLOAD)
    assert broker.calls == []


async def test_manual_save_failure_after_acceptance_recovers_by_client_id(database, monkeypatch):
    broker, rid = ManualBroker(), str(uuid4())
    update = database.update_manual_order_request
    async def fail_after_post(*args, **kw):
        if args[1] == "confirmed":
            raise DatabaseError("disk full after acceptance")
        return await update(*args, **kw)
    def accept(*args, **kw):
        broker.calls.append(kw["client_order_id"])
        broker.accepted()
        return {"id": "accepted"}
    broker.market_order = accept
    monkeypatch.setattr(database, "update_manual_order_request", fail_after_post)
    with pytest.raises(DatabaseError):
        await manual_order_request(database, broker, rid, PAYLOAD)
    monkeypatch.setattr(database, "update_manual_order_request", update)
    assert (await manual_order_request(database, broker, rid))["id"] == "accepted"
    assert len(broker.calls) == 1


async def test_manual_api_preserves_pending_outcome_and_request_identity(database, monkeypatch):
    import main
    from fastapi import FastAPI
    import httpx
    from api.routes import router
    app = FastAPI()
    app.include_router(router, prefix="/api")
    broker, rid = ManualBroker(), str(uuid4())
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main, "trader", broker)
    monkeypatch.setattr(main, "_account_bound", True)  # account guard tested separately
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as http:
        payload = {**PAYLOAD, "request_id": rid, "ticker": "aapl"}
        reply = await http.post("/api/order", json=payload)
        assert reply.status_code == 202 and reply.json()["request_id"] == rid
        broker.accepted()
        reply = await http.get(f"/api/order-requests/{rid}")
        assert reply.status_code == 200 and reply.json()["status"] == "confirmed"
        assert (await http.post("/api/order", json={**payload, "qty": 2})).status_code == 409
        assert (await http.get(f"/api/order-requests/{uuid4()}")).status_code == 404
        assert (await http.post("/api/order", json=PAYLOAD)).status_code == 422
        for bad in ({"qty": 0}, {"qty": -1}, {"order_type": "limit", "limit_price": None}, {"tif": "wrong"}):
            assert (await http.post("/api/order", json={**payload, **bad})).status_code == 422
    assert len(broker.calls) == 1


async def seed_fills(db, exit_times):
    buy_at = (datetime.fromisoformat(exit_times[0]) - timedelta(hours=1)).isoformat()
    fills = []
    for oid, side, qty, price, at in [("buy", "buy", len(exit_times), 100, buy_at)] + [
            (f"sell-{i}", "sell", 1, 50, at) for i, at in enumerate(exit_times)]:
        await db.upsert_trade_performance(strict=True, alpaca_order_id=oid, symbol="AAPL", ticker="AAPL",
            side=side, qty=qty, filled_qty=qty, filled_avg_price=price, order_status="filled",
            submitted_at=at, filled_at=at, trade_type="equity")
        fills.append({"id": oid, "order_id": oid, "symbol": "AAPL", "side": side,
                      "qty": qty, "price": price, "transaction_time": at})
    await db.record_trade_fills(fills)
    await db.reconcile_trades(require_fills=True)
    return fills


async def test_realized_losses_are_on_exit_day_and_replay_is_idempotent(database):
    at = ["2026-10-05T23:00:00+00:00", "2026-10-06T14:00:00+00:00"]
    fills = await seed_fills(database, at)
    now = datetime(2026, 10, 6, 15, tzinfo=timezone.utc)
    state = await database.get_flow_risk_state(now)
    assert state["daily_pnl"] == -50 and state["date"] == "2026-10-06"
    await database.record_trade_fills(fills)
    await database.reconcile_trades(require_fills=True)
    assert await database.get_flow_risk_state(now) == state
    assert len(await database._query("SELECT * FROM realized_trade_exits")) == 2


async def test_bracket_loss_restored_after_restart_and_day_rollover(database):
    from config import Settings
    now = datetime.now(timezone.utc)
    await seed_fills(database, [(now - timedelta(minutes=2)).isoformat()])
    engine = AutoTradeEngine(Settings(_env_file=None, alpaca_api_key="unused", alpaca_secret_key="unused"))
    engine.set_dependencies(None, database, None)
    await engine.refresh_risk_controls()
    assert engine._daily_pnl == -50 and engine._ticker_in_cooldown("AAPL")
    tomorrow = await database.get_flow_risk_state(now + timedelta(days=1))
    assert tomorrow["daily_pnl"] == 0 and tomorrow["loss_times"]["AAPL"]


async def test_incomplete_fill_history_blocks_risk_hydration(database):
    await database.upsert_trade_performance(strict=True, alpaca_order_id="missing", symbol="AAPL", ticker="AAPL",
        side="sell", qty=1, filled_qty=1, filled_avg_price=50, order_status="filled")
    with pytest.raises(DatabaseError):
        await database.get_flow_risk_state()


async def test_fill_corrections_replace_losses_instead_of_accumulating(database):
    now = datetime.now(timezone.utc)
    fills = await seed_fills(database, [(now - timedelta(minutes=2)).isoformat()])
    fills[-1]["price"] = 200
    await database.record_trade_fills(fills)
    await database.reconcile_trades(require_fills=True)
    state = await database.get_flow_risk_state(now)
    assert state["daily_pnl"] == 100 and state["loss_times"] == {}


async def test_cooldown_uses_net_closing_execution_across_multiple_entry_lots(database):
    now = datetime.now(timezone.utc)
    fills = []
    for oid, side, qty, price, at in [
            ("cheap", "buy", 1, 100, now - timedelta(hours=2)),
            ("expensive", "buy", 1, 300, now - timedelta(hours=1)),
            ("exit", "sell", 2, 250, now - timedelta(minutes=2))]:
        await database.upsert_trade_performance(strict=True, alpaca_order_id=oid, symbol="AAPL", ticker="AAPL",
            side=side, qty=qty, filled_qty=qty, filled_avg_price=price, order_status="filled",
            submitted_at=at.isoformat(), filled_at=at.isoformat(), trade_type="equity")
        fills.append({"id": oid, "order_id": oid, "symbol": "AAPL", "side": side,
                      "qty": qty, "price": price, "transaction_time": at.isoformat()})
    await database.record_trade_fills(fills)
    await database.reconcile_trades(require_fills=True)
    state = await database.get_flow_risk_state(now)
    assert state["daily_pnl"] == 100 and state["loss_times"] == {}


async def test_restarted_flow_confirmation_checks_bracket_loss_before_post(database):
    now = datetime.now(timezone.utc)
    await seed_fills(database, [(now - timedelta(minutes=2)).isoformat()])
    calls = []
    broker = NS(get_order_by_client_id=lambda _: {"not_found": True},
                bracket_order=lambda **kw: calls.append(kw) or {"id": "unsafe"})
    engine, tid = await engine_with_trade(database, broker)
    assert engine._daily_pnl == 0 and not engine._ticker_in_cooldown("AAPL")
    await engine.confirm_trade(tid, 0)
    assert calls == [] and engine._daily_pnl == -50 and engine._ticker_in_cooldown("AAPL")


async def test_interrupted_performance_sync_blocks_flow_risk_until_complete(database):
    from trading.performance import sync_trade_performance
    now = datetime.now(timezone.utc)
    await seed_fills(database, [(now - timedelta(minutes=2)).isoformat()])
    broker = NS(get_order_history=lambda **kw: [], get_orders=lambda **kw: [],
                get_fill_activities=lambda **kw: None, get_mleg_leg_order_ids=lambda: set())
    with pytest.raises(RuntimeError):
        await sync_trade_performance(database, broker)
    with pytest.raises(DatabaseError):
        await database.get_flow_risk_state(now)
    broker.get_fill_activities = lambda **kw: []
    await sync_trade_performance(database, broker)
    assert (await database.get_flow_risk_state(now))["daily_pnl"] == -50


SESSION_NOW = datetime(2026, 10, 6, 15, 40, tzinfo=timezone.utc)   # Tue 11:40 ET, mid-session


@pytest.mark.parametrize("bad", [{"bp": float("nan")}, {"ap": float("inf")}, {"bp": 3},
                                {"t": "bad"}, {"t": None}, {"t": "2020-01-01T12:00:00Z"}, {"ap": 0}])
def test_option_adapter_rejects_invalid_crossed_or_stale_quotes(bad):
    broker = object.__new__(AlpacaTrader)
    broker._data_base = "https://data.invalid"
    broker._now = lambda: SESSION_NOW     # each case fails for its own reason, not the hour
    quote = {"bp": 1, "ap": 2, "t": SESSION_NOW.isoformat(), **bad}
    broker._rest = lambda *a: (200, {"quotes": {"OPTION": quote}})
    assert broker.get_option_quotes(["OPTION"]) == {}


def test_zero_bid_never_becomes_a_one_sided_mid_and_provenance_is_retained():
    broker = object.__new__(AlpacaTrader)
    broker._data_base, broker.options_feed = "https://data.invalid", "opra"
    urls = []
    def rest(method, url):
        urls.append(url)
        return 200, {"quotes": {"OPTION": {"bp": 0, "ap": 1, "bs": 0, "as": 2,
                                            "t": SESSION_NOW.isoformat()}}}
    broker._rest = rest
    broker._now = lambda: SESSION_NOW
    quote = broker.get_option_quotes(["OPTION"])["OPTION"]
    assert quote["mid"] == 0 and quote["ask"] == 1
    assert quote["timestamp"] and quote["feed"] == "opra" and quote["ask_size"] == 2
    assert "feed=opra" in urls[0]


def test_explicit_zero_book_remains_available_for_worthless_wing_closes():
    broker = object.__new__(AlpacaTrader)
    broker._data_base = "https://data.invalid"
    broker._now = lambda: SESSION_NOW
    broker._rest = lambda *a: (200, {"quotes": {"WING": {"bp": 0, "ap": 0,
        "t": SESSION_NOW.isoformat()}}})
    quote = broker.get_option_quotes(["WING"])["WING"]
    assert quote["bid"] == quote["ask"] == quote["mid"] == 0


@pytest.mark.parametrize("quote", [{"mid": 2, "ask": 2}, {"mid": 2, "bid": 2},
                                  {"bid": 0, "ask": 2}, {"bid": 3, "ask": 2},
                                  {"bid": float("nan"), "ask": 2}])
def test_condor_planner_refuses_unusable_books_even_when_mid_is_present(quote):
    from signals.iv_executor import build_iron_condor
    from market_time import et_today
    expiry = et_today() + timedelta(days=3)
    broker = NS(get_option_contracts=lambda tk, start, end, kind: [
        {"symbol": f"{kind}-{strike}", "strike": strike, "expiry": expiry}
        for strike in ([110, 115] if kind == "call" else [85, 90])],
        get_option_quotes=lambda syms: {s: quote for s in syms})
    settings = NS(iv_exec_min_dte=1, iv_exec_max_dte=7, iv_exec_short_move_mult=1,
        iv_exec_wing_width_pct=.05, iv_exec_risk_pct=.016,
        iv_exec_max_risk_usd=800, iv_exec_min_credit=.25)
    assert not build_iron_condor(broker, NS(ticker="TEST", price=100, expected_move="10%",
        next_earnings_date=(expiry - timedelta(days=1)).isoformat()), 50000, settings)["ok"]


def test_duplicate_broker_client_id_never_becomes_permission_for_a_new_request():
    from alpaca.common.exceptions import APIError
    from requests import HTTPError, Response
    response = Response()
    response.status_code = 422
    duplicate = APIError('{"code":42210000,"message":"client_order_id must be unique"}',
                         HTTPError(response=response))
    broker = object.__new__(AlpacaTrader)
    broker.get_order_by_client_id = lambda _: {"not_found": True}
    assert broker._submission_error(duplicate, "stable")["ambiguous"]
    rejected = APIError('{"code":42210000,"message":"insufficient buying power"}',
                        HTTPError(response=response))
    assert not broker._submission_error(rejected, "stable")["ambiguous"]
    broker._trade_base = "https://broker.invalid"
    broker._rest = lambda *a: (422, {"error": "client_order_id must be unique"})
    assert broker.multileg_order([], 1, -.5, client_order_id="stable")["ambiguous"]


# ── Quote currency and entry books, as the live paper feed behaves ──────────
def quote_broker(quotes, now=SESSION_NOW):
    broker = object.__new__(AlpacaTrader)
    broker._data_base = "https://data.invalid"
    broker._now = lambda: now
    broker._rest = lambda *a: (200, {"quotes": quotes})
    return broker


@pytest.mark.parametrize("stamp,kept", [
    ("2026-10-06T15:39:55Z", True),     # 5 seconds ago
    ("2026-10-06T15:10:00Z", True),     # unchanged for 30 minutes: a quiet, cheap wing
    ("2026-10-06T13:30:00Z", True),     # unchanged since the 09:30 ET open
    ("2026-10-06T15:40:04Z", True),     # clock skew inside the tolerance
    ("2026-10-06T13:29:59Z", False),    # stamped before today's open
    ("2026-10-05T19:59:00Z", False),    # yesterday's closing mark
    ("2026-10-06T15:41:00Z", False),    # from the future
])
def test_a_quote_stays_current_for_the_session_not_for_two_minutes(stamp, kept):
    """The timestamp is when the book last CHANGED. On 2026-10-06, 62% of options
    asking <= $0.05 had not moved in over two minutes (median 30). A 120-second
    cutoff discarded them, and with them a winning condor's wings."""
    got = quote_broker({"WING": {"bp": 0, "ap": 0.05, "t": stamp}}).get_option_quotes(["WING"])
    assert ("WING" in got) is kept
    if kept:
        assert got["WING"]["age_seconds"] == (SESSION_NOW - datetime.fromisoformat(
            stamp.replace("Z", "+00:00"))).total_seconds()


@pytest.mark.parametrize("now,stamp", [
    (datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc), "2026-10-06T12:59:00Z"),    # 09:00 ET, pre-open
    (datetime(2026, 10, 7, 5, 0, tzinfo=timezone.utc), "2026-10-06T19:59:00Z"),     # 01:00 ET, overnight
    (datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc), "2026-10-09T19:59:00Z"),   # Saturday
])
def test_nothing_is_current_before_the_open_overnight_or_on_a_weekend(now, stamp):
    got = quote_broker({"OPT": {"bp": 1, "ap": 1.1, "t": stamp}}, now).get_option_quotes(["OPT"])
    assert got == {}


def test_a_quiet_worthless_wing_does_not_block_closing_a_winning_condor():
    """After the print the wings go to 0.00/0.05 and stop updating, while the
    shorts still trade. The close must price off that book, not wait for it."""
    import main
    legs = [{"symbol": "SC", "side": "sell"}, {"symbol": "LC", "side": "buy"},
            {"symbol": "SP", "side": "sell"}, {"symbol": "LP", "side": "buy"}]
    quiet = "2026-10-06T14:55:00Z"       # 45 minutes without a change
    quotes = quote_broker({
        "SC": {"bp": 0.08, "ap": 0.10, "t": "2026-10-06T15:39:58Z"},
        "SP": {"bp": 0.05, "ap": 0.07, "t": "2026-10-06T15:39:50Z"},
        "LC": {"bp": 0, "ap": 0.05, "t": quiet},
        "LP": {"bp": 0, "ap": 0.01, "t": quiet},
    }).get_option_quotes([leg["symbol"] for leg in legs])
    assert set(quotes) == {"SC", "LC", "SP", "LP"}
    assert main._condor_close_debit(legs, quotes) == pytest.approx(0.17)   # shorts at ask, wings at bid


def planner(quotes):
    from signals.iv_executor import build_iron_condor
    from market_time import et_today
    expiry = et_today() + timedelta(days=3)
    broker = NS(get_option_contracts=lambda tk, start, end, kind: [
        {"symbol": f"{kind}-{strike}", "strike": strike, "expiry": expiry}
        for strike in ([110, 115] if kind == "call" else [85, 90])],
        get_option_quotes=lambda syms: {s: quotes[s] for s in syms if s in quotes})
    settings = NS(iv_exec_min_dte=1, iv_exec_max_dte=7, iv_exec_short_move_mult=1,
        iv_exec_wing_width_pct=.05, iv_exec_risk_pct=.016,
        iv_exec_max_risk_usd=800, iv_exec_min_credit=.25)
    return build_iron_condor(broker, NS(ticker="TEST", price=100, expected_move="10%",
        next_earnings_date=(expiry - timedelta(days=1)).isoformat()), 50000, settings)


def test_condor_entry_accepts_a_no_bid_wing_and_budgets_it_at_its_ask():
    """A wing the bot BUYS needs an ask, not a bid. Requiring both refused about
    a quarter of listed contracts. The no-bid wing costs its full ask."""
    plan = planner({"call-110": {"bid": 1.4, "ask": 1.6}, "put-90": {"bid": 1.4, "ask": 1.6},
                    "call-115": {"bid": 0, "ask": 0.10},        # no bid: priced at 0.10, not 0.05
                    "put-85": {"bid": 0.20, "ask": 0.30}})      # two-sided: priced at the mid
    assert plan["ok"], plan
    assert plan["credit_mid"] == pytest.approx((1.5 + 1.5) - (0.10 + 0.25))


@pytest.mark.parametrize("short_leg", ["call-110", "put-90"])
def test_condor_entry_still_refuses_a_leg_it_sells_with_no_bid(short_leg):
    quotes = {"call-110": {"bid": 1.4, "ask": 1.6}, "put-90": {"bid": 1.4, "ask": 1.6},
              "call-115": {"bid": 0.05, "ask": 0.10}, "put-85": {"bid": 0.20, "ask": 0.30}}
    quotes[short_leg] = {"bid": 0, "ask": 1.6}
    assert not planner(quotes)["ok"]


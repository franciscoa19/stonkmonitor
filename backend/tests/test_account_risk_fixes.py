"""Order-lifecycle account-risk regressions; fake brokers and temporary ledgers."""
import asyncio
import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
import httpx
from fastapi import FastAPI

from api.routes import router
from db import Database, DatabaseError
from signals.auto_trade import AutoTradeEngine
from trading import account_risk
from trading.account_risk import account_limits_snapshot, reconcile_entry_statuses
from trading.manual_orders import manual_order_request
from test_execution_safety import database, session, condor  # noqa: F401
from test_account_limits import entry, SETUP, PLAN, settings, position
from test_sizing_fixes import balance


@pytest.fixture(autouse=True)
def fresh_entry_lock(monkeypatch):
    monkeypatch.setattr(account_risk, "entry_lock", asyncio.Lock())


def stock_order(**fields):
    return {"id": "stock-entry", "symbol": "MSFT", "asset_class": "us_equity",
            "side": "buy", "qty": "300", "filled_qty": "0", "limit_price": "100",
            "status": "accepted", "order_class": "simple", "type": "limit", **fields}


def broker(*, orders=(), held=(), account=None, history=None):
    return NS(get_open_orders_raw=lambda: list(orders), get_positions_raw=lambda: list(held),
              get_account=lambda: account or balance(), get_order_raw=lambda oid: (history or {})[oid])


async def test_working_manual_stock_buy_stops_condor_at_the_account_cap(database, session, monkeypatch):
    seen = entry(session, database, monkeypatch, account=balance(options=70000, stocks=70000))
    session.trader.get_open_orders_raw = lambda: [stock_order()]
    await session.maybe_execute_condor(SETUP)
    assert seen.orders == seen.sized == []


async def test_pending_condor_reserves_full_width_before_the_next_entry(database, session, monkeypatch):
    await condor(database, qty=23)
    await database._exec("UPDATE iv_condors SET status='pending_entry',entry_order_id=NULL", strict=True)
    seen = entry(session, database, monkeypatch, account_cash_reserve_pct=.8,
                 plan={**PLAN, "qty": 20, "risk_usd": 8000, "collateral_usd": 10000})
    await session.maybe_execute_condor(SETUP)
    assert seen.orders == [] and seen.sized[0]["cash_available"] == 8500


async def flow_row(db, *, kind="equity", qty=200, price=100):
    tid = await db.save_pending_trade(datetime.utcnow(), ticker="MSFT", symbol="MSFT",
        trade_type=kind, qty=qty, limit_price=price, risk_amount=qty * price)
    await db.update_pending_trade(tid, status="confirmed", alpaca_order_id="stock-entry",
                                 entry_order_status="unknown")
    return tid


async def test_filled_flow_buy_counts_only_its_holding_and_allows_a_condor(database, session, monkeypatch):
    await flow_row(database)
    seen = entry(session, database, monkeypatch, account=balance(cash=80000, options=80000),
                 positions=[position("MSFT", 200, market_value=20000)])
    session.trader.get_order_raw = lambda oid: stock_order(id=oid, qty="200", status="filled", filled_qty="200")
    await session.maybe_execute_condor(SETUP)
    assert seen.orders == [5] and seen.sized[0]["risk_headroom"] == 10000


async def test_unmatched_stock_order_is_counted_with_existing_condors_and_no_client_id(database):
    await condor(database)
    got = await account_limits_snapshot(database, broker(orders=[stock_order()]), settings())
    assert got["broker_order_risk"] == 30000 and got["open_risk"] == 30800


@pytest.mark.parametrize("listed", [False, True])
async def test_partial_flow_fill_splits_holdings_and_remaining_commitment(database, listed):
    await flow_row(database)
    order = stock_order(qty="200", status="partially_filled", filled_qty="120")
    got = await account_limits_snapshot(database, broker(orders=[order] if listed else [],
        held=[position("MSFT", 120, market_value=12000)], history={order["id"]: order}), settings())
    assert got["position_risk"] == 12000 and got["pending_risk"] == 8000
    assert got["open_risk"] == 20000 and got["broker_order_risk"] == 0
    assert got["unlisted_reserved"] == (0 if listed else 8000)


@pytest.mark.parametrize("status,filled,held_value", [
    ("filled", 200, 20000), ("canceled", 120, 12000), ("expired", 0, 0), ("rejected", 0, 0),
    ("replaced", 0, 0), ("filled", 200, 0)])
async def test_periodic_reconciliation_retires_terminal_flow_reservations(database, status, filled, held_value):
    tid = await flow_row(database)
    order = stock_order(qty="200", status=status, filled_qty=str(filled))
    b = broker(held=[position("MSFT", filled, market_value=held_value)] if held_value else [],
               history={order["id"]: order})
    engine = AutoTradeEngine(settings())
    async def verified(): return
    engine.set_dependencies(None, database, b, account_check=verified)
    await engine.reconcile_submissions()
    row = (await database.get_pending_trades("confirmed"))[0]
    assert row["id"] == tid and row["entry_order_status"] == status
    assert await database.get_entry_reservations() == []
    b.get_order_raw = lambda _: pytest.fail("Resolved entries must not be fetched forever")
    got = await account_limits_snapshot(database, b, settings())
    assert got["open_risk"] == held_value and got["unlisted_reserved"] == 0


@pytest.mark.parametrize("bad_order", [
    {"error": "offline"}, stock_order(qty="200", status="filled", filled_qty=None),
    stock_order(qty="200", status="filled", filled_qty="NaN"),
    stock_order(qty="200", status="filled", filled_qty="100"),
    stock_order(qty="200", filled_qty="201"), stock_order(qty="200", symbol="OTHER")])
async def test_bad_order_evidence_never_releases_a_durable_reservation(database, bad_order):
    await flow_row(database)
    b = broker(history={"stock-entry": bad_order})
    with pytest.raises(DatabaseError):
        await reconcile_entry_statuses(database, b, settings())
    assert (await database.get_entry_reservations())[0]["entry_order_status"] == "unknown"


async def test_risk_api_uses_filled_order_evidence_without_updating_the_ledger(database, session, monkeypatch):
    await flow_row(database)
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "settings", settings())
    monkeypatch.setattr(session, "trader", broker(held=[position("MSFT", 200, market_value=20000)],
        history={"stock-entry": stock_order(qty="200", status="filled", filled_qty="200")}))
    app = FastAPI()
    app.include_router(router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        reply = await client.get("/api/risk/account")
        assert reply.status_code == 200 and reply.json()["open_risk"] == 20000
        session.trader.get_order_raw = lambda _: {"error": "offline"}
        assert (await client.get("/api/risk/account")).status_code == 503
    assert (await database.get_entry_reservations())[0]["entry_order_status"] == "unknown"


async def test_legacy_manual_journal_migration_preserves_requests_and_is_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE manual_order_requests (
            request_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL,
            client_order_id TEXT UNIQUE NOT NULL, status TEXT NOT NULL DEFAULT 'new',
            alpaca_order_id TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO manual_order_requests VALUES (?,?,?,?,?,?,?,?)",
                     ("legacy", "{}", "legacy-client", "confirmed", "legacy-order", None, "2026-10-06", "2026-10-06"))
    db = Database(path)
    for _ in range(2):
        await db.connect()
        try:
            row = await db.get_manual_order_request("legacy")
            assert row["client_order_id"] == "legacy-client" and row["alpaca_order_id"] == "legacy-order"
            assert row["broker_order_status"] is None
            assert len(await db.get_manual_entry_reservations()) == 1
        finally:
            await db.close()


@pytest.mark.parametrize("corruption", ["long_put=NULL", "long_call=short_call", "long_put=short_put", "short_put=0"])
async def test_unusable_pending_condor_collateral_blocks_new_entries(database, corruption):
    await condor(database)
    await database._exec(f"UPDATE iv_condors SET status='pending_entry',{corruption}", strict=True)
    with pytest.raises(DatabaseError):
        await account_limits_snapshot(database, broker(), settings())


async def test_position_capacity_reconciliation_waits_for_other_entries(database):
    engine = AutoTradeEngine(settings())
    reads = []
    b = broker()
    b.get_open_orders_raw = lambda: reads.append(True) or []
    engine.set_dependencies(None, database, b)
    async with account_risk.entry_lock:
        task = asyncio.create_task(engine._max_positions_check())
        await asyncio.sleep(.05)
        assert not task.done() and reads == []
    assert (await task)[0] and reads == [True]


@pytest.mark.parametrize("failure", ["positions", "account", "persistence"])
async def test_incomplete_reconciliation_preserves_or_fails_the_claim(database, failure, monkeypatch):
    await flow_row(database)
    b = broker(history={"stock-entry": stock_order(qty="200", status="filled", filled_qty="200")})
    if failure == "positions":
        b.get_positions_raw = lambda: None
    elif failure == "account":
        b.get_account = lambda: balance(error="offline")
    else:
        async def fail(*a, **kw): raise DatabaseError("disk full")
        monkeypatch.setattr(database, "update_pending_trade", fail)
    with pytest.raises(DatabaseError):
        await reconcile_entry_statuses(database, b, settings())
    assert (await database.get_entry_reservations())[0]["entry_order_status"] == "unknown"


async def manual_row(db, status="confirmed"):
    rid, client = str(uuid4()), f"manual-{uuid4().hex}"
    payload = {"ticker": "MSFT", "side": "buy", "qty": 300, "order_type": "limit", "limit_price": 100, "tif": "day"}
    await db.ensure_manual_order_request(rid, json.dumps(payload), client)
    await db.update_manual_order_request(rid, status, "stock-entry" if status == "confirmed" else None)
    return rid, client


@pytest.mark.parametrize("listed", [False, True])
async def test_journaled_manual_order_is_counted_once_even_before_the_broker_lists_it(database, listed):
    rid, client = await manual_row(database)
    order = stock_order(client_order_id=client)
    got = await account_limits_snapshot(database, broker(orders=[order] if listed else [],
        history={order["id"]: order}), settings())
    assert got["manual_risk"] == got["open_risk"] == 30000 and got["broker_order_risk"] == 0
    assert got["unlisted_reserved"] == (0 if listed else 30000)
    assert (await database.get_manual_order_request(rid))["broker_order_status"] is None  # read-only view


async def test_terminal_manual_entry_is_retired_without_changing_its_request_identity(database):
    rid, client = await manual_row(database)
    order = stock_order(status="filled", filled_qty="300", client_order_id=client)
    b = broker(held=[position("MSFT", 300, market_value=30000)], history={order["id"]: order})
    await reconcile_entry_statuses(database, b, settings())
    row = await database.get_manual_order_request(rid)
    assert row["status"] == "confirmed" and row["client_order_id"] == client and row["broker_order_status"] == "filled"
    assert await database.get_manual_entry_reservations() == []
    b.get_order_raw = lambda _: pytest.fail("Resolved manual journal should not cause repeated lookups")
    assert (await account_limits_snapshot(database, b, settings()))["open_risk"] == 30000


@pytest.mark.parametrize("status", ["submitting", "pending"])
async def test_unresolved_manual_outcome_blocks_condor_entries(database, session, monkeypatch, status):
    await manual_row(database, status)
    seen = entry(session, database, monkeypatch)
    await session.maybe_execute_condor(SETUP)
    assert seen.orders == seen.sized == []


async def test_manual_submission_waits_for_the_shared_entry_lock(database):
    posted = []
    b = NS(get_order_by_client_id=lambda _: {"not_found": True}, market_order=lambda *a, **kw:
           posted.append(kw) or {"id": "stock-entry"})
    payload = {"ticker": "MSFT", "qty": 1, "side": "buy", "order_type": "market", "tif": "day"}
    async with account_risk.entry_lock:
        task = asyncio.create_task(manual_order_request(database, b, uuid4(), payload))
        await asyncio.sleep(.05)
        assert not task.done() and posted == []
    assert (await task)["id"] == "stock-entry" and len(posted) == 1


@pytest.mark.parametrize("kind", ["bracket", "oco", "sell_exit", "cover_exit", "notional"])
async def test_external_order_risk_distinguishes_openings_from_exits(database, kind):
    child = stock_order(id="exit-child", side="sell", type="stop", limit_price=None)
    held = []
    if kind == "bracket":
        order = stock_order(order_class="bracket", legs=[child])
        orders, expected = [order, child, dict(order)], 30000  # nested + top-level and repeated parent
    elif kind in ("oco", "sell_exit"):
        held = [position("MSFT", 300, market_value=30000)]
        order = stock_order(side="sell", order_class="oco" if kind == "oco" else "simple", legs=[child] if kind == "oco" else None)
        orders, expected = [order], 0
    elif kind == "cover_exit":
        held = [position("MSFT", -300, market_value=-30000)]
        orders, expected = [stock_order(type="market", limit_price=None)], 0
    else:
        orders, expected = [stock_order(type="market", qty=None, notional="10000", filled_qty="2", filled_avg_price="100")], 9800
        held = [position("MSFT", 2, market_value=200)]
    got = await account_limits_snapshot(database, broker(orders=orders, held=held), settings())
    assert got["broker_order_risk"] == expected
    assert got["open_risk"] == expected + sum(abs(float(p["market_value"])) for p in held)


@pytest.mark.parametrize("change", [
    {"type": "market", "limit_price": None}, {"type": "stop", "limit_price": None},
    {"side": "sell"}, {"qty": "NaN"}, {"limit_price": "bad"}, {"limit_price": 0},
    {"filled_qty": "301"}, {"order_class": "mleg"}, {"symbol": ""}])
async def test_unpriceable_or_malformed_external_entry_blocks_new_risk(database, change):
    with pytest.raises(DatabaseError):
        await account_limits_snapshot(database, broker(orders=[stock_order(**change)]), settings())


async def test_two_cover_buys_cannot_each_spend_the_same_short_inventory(database):
    held = [position("MSFT", -300, market_value=-30000)]
    got = await account_limits_snapshot(database, broker(orders=[stock_order(), stock_order(id="another")], held=held), settings())
    assert got["broker_order_risk"] == 30000  # first covers; the second would open a long holding


async def test_explicit_close_intent_and_a_second_order_do_not_share_cover_inventory(database):
    got = await account_limits_snapshot(database, broker(orders=[stock_order(position_intent="buy_to_close"),
        stock_order(id="another")], held=[position("MSFT", -300, market_value=-30000)]), settings())
    assert got["broker_order_risk"] == 30000


@pytest.mark.parametrize("order", [
    stock_order(order_class="oco"),
    stock_order(order_class="bracket", legs=[stock_order(id="child", side="buy", position_intent="buy_to_open")])])
async def test_untracked_oco_openings_and_inconsistent_bracket_children_block(database, order):
    with pytest.raises(DatabaseError):
        await account_limits_snapshot(database, broker(orders=[order]), settings())


async def test_read_only_risk_view_fetches_entry_status_before_positions_and_balance(database):
    await flow_row(database)
    reads = []
    b = broker()
    b.get_open_orders_raw = lambda: reads.append("orders") or []
    b.get_order_raw = lambda _: reads.append("entry") or stock_order(qty="200", status="filled", filled_qty="200")
    b.get_positions_raw = lambda: reads.append("positions") or [position("MSFT", 200, market_value=20000)]
    b.get_account = lambda: reads.append("balance") or balance()
    got = await account_limits_snapshot(database, b, settings())
    assert reads == ["orders", "entry", "positions", "balance"] and got["open_risk"] == 20000
    assert (await database.get_entry_reservations())[0]["entry_order_status"] == "unknown"

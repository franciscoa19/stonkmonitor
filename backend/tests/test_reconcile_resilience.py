"""The periodic reconciliation must heal what it can and never take the fill
sync down with it. Fake brokers and temporary ledgers only."""
import asyncio
from types import SimpleNamespace as NS
from uuid import UUID

import pytest

from daily_report import build_report_data, render_html
from db import DatabaseError
from signals.auto_trade import AutoTradeEngine
from trading import account_risk
from trading.account_risk import account_limits_snapshot
from test_execution_safety import database  # noqa: F401  (fixture)
from test_account_limits import settings
from test_account_risk_fixes import broker, flow_row, manual_row, stock_order
from test_sizing_fixes import balance


@pytest.fixture(autouse=True)
def fresh_entry_lock(monkeypatch):
    monkeypatch.setattr(account_risk, "entry_lock", asyncio.Lock())


def engine_for(database, b):
    engine = AutoTradeEngine(settings())

    async def verified():
        return
    engine.set_dependencies(None, database, b, account_check=verified)
    return engine


def placed(client):
    """The broker's copy of the order manual_row() journals."""
    return stock_order(client_order_id=client, time_in_force="day")


@pytest.mark.parametrize("status", ["submitting", "pending"])
async def test_a_stuck_manual_order_is_resolved_without_a_browser(database, status):
    """An unresolved dashboard order defers every automated entry. Only the
    dashboard's own polling used to resolve it, so a closed tab left the bot
    unable to open anything."""
    rid, _ = await manual_row(database, status)
    # manual_row() journals an arbitrary client ID; the real one is derived
    # from the request ID, and that is what the reconciler looks up.
    client = f"sm-manual-{UUID(rid).hex}"
    await database._exec("UPDATE manual_order_requests SET client_order_id=? WHERE request_id=?",
                         (client, rid), strict=True, expected_rows=1)
    order = placed(client)
    b = broker(history={order["id"]: order})
    b.get_order_by_client_id = lambda cid: order if cid == client else {"not_found": True}
    with pytest.raises(DatabaseError):
        await account_limits_snapshot(database, b, settings())           # blocked beforehand

    await engine_for(database, b).reconcile_submissions()

    row = await database.get_manual_order_request(rid)
    assert (row["status"], row["alpaca_order_id"], row["client_order_id"]) == ("confirmed", order["id"], client)
    got = await account_limits_snapshot(database, b, settings())          # readable again
    assert got["manual_risk"] == 30000


async def test_an_order_the_broker_never_saw_stays_unresolved_and_keeps_blocking(database):
    """A missing order is not proof it was never accepted, so nothing is assumed."""
    rid, _ = await manual_row(database, "pending")
    b = broker()
    b.get_order_by_client_id = lambda cid: {"not_found": True}
    await engine_for(database, b).reconcile_submissions()
    assert (await database.get_manual_order_request(rid))["status"] == "pending"
    with pytest.raises(DatabaseError):
        await account_limits_snapshot(database, b, settings())


@pytest.mark.parametrize("lookup", [
    lambda cid: {"error": "offline"},                                     # broker unreachable
    lambda cid: (_ for _ in ()).throw(RuntimeError("timeout")),           # lookup itself fails
    lambda cid: stock_order(client_order_id=cid, qty="999", time_in_force="day")])   # not the saved request
async def test_a_manual_lookup_that_fails_leaves_the_request_alone_and_does_not_raise(database, lookup):
    rid, _ = await manual_row(database, "pending")
    b = broker()
    b.get_order_by_client_id = lookup
    await engine_for(database, b).reconcile_submissions()
    row = await database.get_manual_order_request(rid)
    assert row["status"] == "pending" and row["alpaca_order_id"] is None


@pytest.mark.parametrize("fault", ["order evidence", "positions", "balance", "unpriceable manual order"])
async def test_a_failed_risk_reconciliation_does_not_abort_the_periodic_pass(database, fault):
    """reconcile_submissions() runs at the top of the 15-minute performance
    sync. Raising here skipped the fill and P&L sync for the whole cycle, for
    as long as the fault lasted."""
    await flow_row(database)
    filled = stock_order(qty="200", status="filled", filled_qty="200")
    b = broker(history={"stock-entry": filled})
    if fault == "order evidence":
        b.get_order_raw = lambda oid: {"error": "offline"}
    elif fault == "positions":
        b.get_positions_raw = lambda: None
    elif fault == "balance":
        b.get_account = lambda: balance(options=None)
    else:
        b.get_open_orders_raw = lambda: [stock_order(id="queued", type="market", limit_price=None)]

    await engine_for(database, b).reconcile_submissions()                 # must not raise

    # ...and nothing was released on incomplete evidence.
    assert (await database.get_entry_reservations())[0]["entry_order_status"] == "unknown"


async def test_the_healthy_pass_still_retires_finished_reservations(database):
    await flow_row(database)
    b = broker(held=[{"symbol": "MSFT", "qty": "200", "market_value": "20000"}],
               history={"stock-entry": stock_order(qty="200", status="filled", filled_qty="200")})
    await engine_for(database, b).reconcile_submissions()
    assert await database.get_entry_reservations() == []


# ── The report says so when the limits cannot be read ───────────────────────
def report_broker(**kw):
    return NS(paper=True, get_account=lambda: kw.get("account") or balance(),
              get_positions=lambda: [], get_positions_raw=lambda: [],
              get_open_orders_raw=lambda: list(kw.get("orders", [])))


async def test_the_report_names_why_the_limits_are_unreadable(database):
    await manual_row(database, "pending")
    data = await build_report_data(database, report_broker(), settings=settings())
    assert data["account_risk"] is None
    assert "manual order outcome is unresolved" in data["account_risk_error"]
    html = render_html(data)
    assert "Risk limits" in html and "unreadable" in html and "manual order outcome is unresolved" in html
    assert "Risk in use" not in html


async def test_the_reason_is_escaped_and_bounded(database):
    data = await build_report_data(database, report_broker(), settings=settings())
    data.update(account_risk=None, account_risk_error="<script>alert(1)</script>")
    html = render_html(data)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html

    class Loud(NS):
        def get_open_orders_raw(self):
            raise RuntimeError("x" * 5000)
    data = await build_report_data(database, Loud(paper=True, get_account=lambda: balance(),
                                                  get_positions=lambda: []), settings=settings())
    assert len(data["account_risk_error"]) == 200


@pytest.mark.parametrize("s", [None, "readable"])
async def test_no_warning_tile_when_limits_are_readable_or_were_not_requested(database, s):
    data = await build_report_data(database, report_broker(), settings=settings() if s else None)
    assert data["account_risk_error"] is None and "unreadable" not in render_html(data)

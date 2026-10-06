"""Account identity, strict budgets, and cash-flow regressions. No real brokers."""
import asyncio
from datetime import datetime
from types import SimpleNamespace as NS
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from api.routes import router
from bind_account import bind_legacy_account
from config import Settings
from daily_report import build_report_data, render_html
from db import AccountBindingRequired, AccountMismatch, Database, DatabaseError
from signals.auto_trade import AutoTradeEngine
from trading.alpaca_trader import AlpacaTrader
from test_execution_safety import database, session, condor, engine_with_trade  # noqa: F401
from test_report_stages import report  # noqa: F401


def settings(**kw):
    return Settings(_env_file=None, alpaca_api_key="unused", alpaca_secret_key="unused", **kw)


def balance(equity=100000, cash=100000, options=100000, stocks=100000, **kw):
    return {"equity": equity, "cash": cash, "options_buying_power": options,
            "non_marginable_buying_power": stocks, "buying_power": 400000, **kw}


async def card(db, *, kind="equity", qty=20, price=100):
    calls = []
    broker = NS(paper=True,
        get_order_by_client_id=lambda _: {"not_found": True},
        bracket_order=lambda **kw: calls.append(kw) or {"id": "entry", "status": "accepted"})
    engine, tid = await engine_with_trade(db, broker)
    suggestion = engine._pending[tid]
    suggestion.qty, suggestion.limit_price, suggestion.trade_type = qty, price, kind
    suggestion.risk_amount = qty * price * (100 if kind == "option" else 1)
    await db._exec("UPDATE pending_trades SET trade_type=?,qty=?,limit_price=?,risk_amount=? WHERE id=?",
                   (kind, qty, price, suggestion.risk_amount, tid), strict=True)
    return engine, tid, broker, calls


@pytest.mark.parametrize("identity", [None, "live:different"])
async def test_unverified_account_cannot_close_or_read_condor_orders(database, session, monkeypatch, identity):
    await database.bind_account("paper:original")
    row = await condor(database, expiry="2026-12-04")
    calls = []
    broker = NS(account_fingerprint=lambda: identity,
        get_option_quotes=lambda *a: calls.append("quotes") or {},
        get_order_raw=lambda *a: calls.append("order") or {},
        close_multileg=lambda *a, **kw: calls.append("close") or {"id": "wrong"},
        cancel_order_raw=lambda *a: calls.append("cancel"))
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "trader", broker)
    monkeypatch.setattr(session, "_account_bound", False)
    with pytest.raises(DatabaseError if identity is None else AccountMismatch):
        await session._manage_condor(row)
    assert calls == [] and not session._account_bound
    assert (await database.get_active_condors())[0]["status"] == "open"


async def test_identity_recovery_resumes_existing_condor_management(database, session, monkeypatch):
    await database.bind_account("paper:original")
    row = await condor(database, expiry="2026-12-04")
    identity, calls = [None], []
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "_account_bound", False)
    monkeypatch.setattr(session, "trader", NS(account_fingerprint=lambda: identity[0],
        get_option_quotes=lambda syms: {s: {"bid": .1, "ask": .1} for s in syms},
        close_multileg=lambda *a, **kw: calls.append(a) or {"id": "correct-close"}))
    with pytest.raises(DatabaseError):
        await session._manage_condor(row)
    identity[0] = "paper:original"
    await session._manage_condor(row)
    assert session._account_bound and len(calls) == 1


async def test_startup_waits_for_identity_then_retries(database, session, monkeypatch):
    identity, reads = [None], []
    retry, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "_account_bound", False)
    monkeypatch.setattr(session, "trader", NS(account_fingerprint=lambda: reads.append(True) or identity[0]))
    async def sleep(_):
        retry.set()
        await release.wait()
    monkeypatch.setattr(session.asyncio, "sleep", sleep)
    waiting = asyncio.create_task(session.wait_for_broker_account())
    try:
        await retry.wait()
        assert not waiting.done() and not session._account_bound
        identity[0] = "paper:original"
        release.set()
        await waiting
        assert session._account_bound and len(reads) == 2
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_lifespan_mismatch_aborts_before_starting_tasks(tmp_path, session, monkeypatch):
    db = Database(tmp_path / "startup.db")
    await db.connect()
    await db.bind_account("paper:original")
    await db.close()
    monkeypatch.setattr(session, "db", db)
    monkeypatch.setattr(session, "trader", NS(account_fingerprint=lambda: "live:different"))
    async def forbidden():
        raise AssertionError("scanner must not start")
    monkeypatch.setattr(session, "iv_scanner_loop", forbidden)
    with pytest.raises(AccountMismatch):
        async with session.lifespan(session.app):
            pytest.fail("unverified application must not serve requests")
    assert not session._account_bound
    with pytest.raises(DatabaseError):
        await db._scalar("SELECT 1", strict=True)  # failed startup released its connection


@pytest.mark.parametrize("operation", ["submit", "reconcile", "cancel", "close"])
async def test_manual_api_blocks_every_broker_action_until_identity(database, session, monkeypatch, operation):
    calls = []
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "_account_bound", False)
    monkeypatch.setattr(session, "trader", NS(account_fingerprint=lambda: None,
        market_order=lambda *a, **kw: calls.append("submit"),
        get_order_by_client_id=lambda *a: calls.append("reconcile"),
        cancel_order=lambda *a: calls.append("cancel"), close_position=lambda *a: calls.append("close")))
    app = FastAPI()
    app.include_router(router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        if operation == "submit":
            reply = await client.post("/api/order", json={"request_id": str(uuid4()),
                "ticker": "AAPL", "side": "buy", "qty": 1})
        elif operation == "reconcile":
            reply = await client.get(f"/api/order-requests/{uuid4()}")
        else:
            reply = await client.delete("/api/order/old" if operation == "cancel" else "/api/positions/AAPL")
    assert reply.status_code == 503 and calls == []
    assert await database._query("SELECT * FROM manual_order_requests") == []


async def test_populated_legacy_ledger_requires_explicit_verified_migration(database):
    await condor(database)
    with pytest.raises(AccountBindingRequired):
        await database.bind_account("live:different")
    assert await database._scalar("SELECT value FROM db_meta WHERE key='broker_account'") == {}
    broker = NS(account_fingerprint=lambda: "paper:original")
    with pytest.raises(AccountMismatch):
        await bind_legacy_account(database, broker, "live:different")
    assert await database._scalar("SELECT value FROM db_meta WHERE key='broker_account'") == {}
    await bind_legacy_account(database, broker, "paper:original")
    await database.bind_account("paper:original")
    assert len(await database.get_active_condors()) == 1
    with pytest.raises(AccountMismatch):
        await database.bind_account("live:different", legacy_fingerprint="live:different")


async def test_equity_history_also_prevents_automatic_legacy_binding(database):
    await database.record_daily_equity("2026-10-06", 100000)
    with pytest.raises(AccountBindingRequired):
        await database.bind_account("paper:other")


async def test_unavailable_identity_never_migrates_a_legacy_ledger(database):
    await condor(database)
    with pytest.raises(DatabaseError):
        await bind_legacy_account(database, NS(account_fingerprint=lambda: None), "paper:original")
    assert await database._scalar("SELECT value FROM db_meta WHERE key='broker_account'") == {}


async def test_fingerprint_probe_never_opens_or_changes_a_database(monkeypatch, capsys):
    import bind_account
    monkeypatch.setattr(bind_account, "get_settings", lambda: settings())
    monkeypatch.setattr(bind_account, "AlpacaTrader", lambda *a, **kw: NS(account_fingerprint=lambda: "paper:original"))
    def forbidden(*a):
        raise AssertionError("Read-only fingerprint probe must not open a ledger")
    monkeypatch.setattr(bind_account, "Database", forbidden)
    await bind_account.main(show_fingerprint=True)
    assert capsys.readouterr().out.strip() == "paper:original"


async def test_concurrent_account_bindings_have_one_winner(database):
    results = await asyncio.gather(database.bind_account("paper:a"), database.bind_account("paper:b"),
                                   return_exceptions=True)
    assert sum(r is None for r in results) == 1
    assert sum(isinstance(r, AccountMismatch) for r in results) == 1


@pytest.mark.parametrize("kind,price,qty", [("option",25,0), ("option",20,1), ("option",.1,200),
                                           ("equity",2500,0), ("equity",2000,1), ("equity",100,20)])
def test_percentage_budget_never_rounds_up_one_unit(kind, price, qty):
    engine = AutoTradeEngine(settings())
    actual, risk = (engine._size_options(100000,price) if kind == "option"
                    else engine._size_equity(100000,price))
    assert actual == qty and risk <= 2000


@pytest.mark.parametrize("invalid", [0,-1,float("nan"),float("inf")])
def test_invalid_equity_and_prices_never_produce_a_quantity(invalid):
    engine = AutoTradeEngine(settings())
    assert engine._size_options(invalid,1) == engine._size_equity(invalid,100) == (0,0)
    assert engine._size_options(100000,invalid) == engine._size_equity(100000,invalid) == (0,0)


@pytest.mark.parametrize("value", [{},{"equity": "bad"},{"equity":float("nan")},
                                  {"equity":100000,"error":"unavailable"}])
@pytest.mark.parametrize("longterm", [False,True])
async def test_equity_builders_never_size_from_default_or_cached_balance(value, longterm, monkeypatch):
    engine = AutoTradeEngine(settings())
    engine._cached_equity = 100000
    calls = []
    async def price(_): return 100
    async def queue(**kw): calls.append(kw)
    monkeypatch.setattr(engine,"_get_equity_price",price)
    monkeypatch.setattr(engine,"_queue",queue)
    if longterm:
        await engine._build_longterm_equity_trade("AAPL",10,value)
    else:
        await engine._build_equity_trade("AAPL","bullish",10,value)
    assert calls == []


def test_flow_ceiling_is_optional_and_only_explicit_settings_bind():
    assert settings().auto_trade_max_risk_usd is None
    assert settings(auto_trade_max_risk_usd="").auto_trade_max_risk_usd is None
    engine = AutoTradeEngine(settings())
    assert engine._size_equity(5000000,100) == (1000,100000)
    engine.settings = settings(auto_trade_max_risk_usd=50000)
    assert engine._size_equity(5000000,100) == (500,50000)
    for value in (0,-1,float("inf")):
        with pytest.raises(ValidationError): settings(auto_trade_max_risk_usd=value)


@pytest.mark.parametrize("kind,price,original,new_equity,expected", [
    ("equity",100,20,50000,10), ("equity",100,20,250000,20),
    ("option",1,20,50000,10), ("equity_long",100,50,50000,25)])
async def test_confirmation_reduces_and_persists_fresh_percentage_size(database, kind, price, original, new_equity, expected):
    engine,tid,broker,calls = await card(database,kind=kind,price=price,qty=original)
    broker.get_account = lambda: balance(equity=new_equity)
    result = await engine.confirm_trade(tid,0)
    assert result["id"] == "entry" and calls[0]["qty"] == expected
    row = (await database.get_pending_trades("confirmed"))[0]
    assert row["qty"] == expected and row["risk_amount"] == expected * price * (100 if kind == "option" else 1)


@pytest.mark.parametrize("kind,cash,options,stocks,expected", [
    ("equity",300,100000,100000,3), ("equity",100000,100000,500,5),
    ("option",100000,175,100000,1), ("option",50,100000,100000,0)])
async def test_cash_and_asset_buying_power_limit_confirmed_size(database,kind,cash,options,stocks,expected):
    engine,tid,broker,calls = await card(database,kind=kind,price=1 if kind == "option" else 100)
    broker.get_account = lambda: balance(cash=cash,options=options,stocks=stocks)
    result = await engine.confirm_trade(tid,0)
    assert [call["qty"] for call in calls] == ([expected] if expected else [])
    if not expected:
        assert result["error"] and not result["ambiguous"]
        assert (await database.get_pending_trades())[0]["status"] == "pending"


@pytest.mark.parametrize("account", [{"equity":50000}, balance(cash=-1), balance(options=None),
                                    balance(stocks=float("nan")), balance(trading_blocked=True),
                                    balance(account_blocked=True)])
async def test_unverified_or_blocked_funds_cannot_submit(database,account):
    kind = "option" if account.get("options_buying_power","missing") is None else "equity"
    engine,tid,broker,calls = await card(database,kind=kind)
    broker.get_account = lambda: account
    result = await engine.confirm_trade(tid,0)
    assert result["error"] and not result["ambiguous"] and calls == []
    assert (await database.get_pending_trades())[0]["status"] == "pending"


@pytest.mark.parametrize("listed", [False,True])
async def test_local_unlisted_entries_reserve_funds_without_counting_broker_orders_twice(database,listed):
    engine,tid,broker,calls = await card(database)
    old = await database.save_pending_trade(datetime.utcnow(),ticker="MSFT",symbol="MSFT",
        trade_type="equity",qty=9,limit_price=100,risk_amount=900)
    await database.update_pending_trade(old,status="confirmed",alpaca_order_id="prior",entry_order_status="accepted")
    broker.get_order_raw = lambda _: {"id":"prior","status":"accepted","filled_qty":"0"}
    broker.get_account = lambda: balance(cash=1000,stocks=1000)
    broker.get_open_orders_raw = lambda: ([{"id":"prior","symbol":"MSFT","side":"buy"}] if listed else [])
    assert (await engine.confirm_trade(tid,0))["id"] == "entry"
    assert calls[0]["qty"] == (10 if listed else 1)


async def test_unlisted_condor_reserves_cash_before_flow_entry(database):
    engine,tid,broker,calls = await card(database)
    await condor(database,qty=2)
    await database._exec("UPDATE iv_condors SET status='pending_entry',entry_order_id=NULL",strict=True)
    broker.get_account = lambda: balance(cash=1000,stocks=1000)
    assert (await engine.confirm_trade(tid,0))["id"] == "entry"
    assert calls[0]["qty"] == 2  # $1,000 less 2 x $400 reserved condor max loss


@pytest.mark.parametrize("invalid", [0, "bad", "NaN"])
async def test_invalid_local_reservations_block_submission(database, invalid):
    engine, tid, broker, calls = await card(database)
    old = await database.save_pending_trade(datetime.utcnow(), ticker="MSFT", symbol="MSFT",
        trade_type="equity", qty=9, limit_price=100, risk_amount=900)
    await database.update_pending_trade(old, status="submission_unknown")
    await database._exec("UPDATE pending_trades SET limit_price=? WHERE id=?", (invalid, old), strict=True)
    assert (await engine.confirm_trade(tid, 0))["error"] and calls == []


async def test_unresolved_manual_order_blocks_flow_without_losing_the_card(database):
    engine, tid, broker, calls = await card(database)
    request_id = str(uuid4())
    await database.ensure_manual_order_request(request_id, "{}", "manual-unknown")
    await database.update_manual_order_request(request_id, "pending")
    assert (await engine.confirm_trade(tid, 0))["error"] and calls == []
    assert tid in engine._pending and (await database.get_pending_trades())[0]["status"] == "pending"


async def test_ambiguous_order_is_recovered_without_resizing_or_another_post(database):
    engine,tid,broker,calls = await card(database)
    broker.get_account = lambda: balance(equity=50000)
    broker.bracket_order = lambda **kw: calls.append(kw) or {"error":"timeout","ambiguous":True}
    assert (await engine.confirm_trade(tid,0))["ambiguous"]
    assert calls[0]["qty"] == 10
    broker.get_account = lambda: (_ for _ in ()).throw(AssertionError("accepted order must not be resized"))
    broker.get_order_by_client_id = lambda _: {"id":"accepted","status":"accepted"}
    assert (await engine.confirm_trade(tid,0))["id"] == "accepted"
    assert len(calls) == 1 and (await database.get_pending_trades("confirmed"))[0]["qty"] == 10


async def test_failed_resize_claim_never_submits_or_changes_card(database, monkeypatch):
    engine,tid,broker,calls = await card(database)
    broker.get_account = lambda: balance(equity=50000)
    update = database.update_pending_trade
    async def fail_claim(trade_id, **fields):
        if fields.get("status") == "submitting":
            assert fields["qty"] == 10
            raise DatabaseError("Failed to commit resized entry claim")
        await update(trade_id, **fields)
    monkeypatch.setattr(database, "update_pending_trade", fail_claim)
    result = await engine.confirm_trade(tid,0)
    assert result["error"] and calls == [] and engine._pending[tid].qty == 20
    assert (await database.get_pending_trades())[0]["qty"] == 20


async def test_flow_identity_check_precedes_broker_order_recovery(database):
    engine,tid,broker,calls = await card(database)
    reads = []
    broker.get_order_by_client_id = lambda _: reads.append(True) or {"id":"wrong-account-order"}
    async def unverified(): raise AccountMismatch("different account")
    engine._account_check = unverified
    assert (await engine.confirm_trade(tid,0))["error"]
    with pytest.raises(AccountMismatch): await engine.reconcile_submissions()
    assert calls == reads == []


async def test_final_balance_refresh_also_rechecks_percentage_loss_limit(database, monkeypatch):
    engine, tid, broker, calls = await card(database)
    snapshots = iter([balance(equity=100000), balance(equity=50000)])
    broker.get_account = lambda: next(snapshots)
    async def hydrate(**kw):
        engine._refresh_daily_pnl_date()
        engine._daily_pnl = -3000
    monkeypatch.setattr(engine, "refresh_risk_controls", hydrate)
    result = await engine.confirm_trade(tid, 0)
    assert "Circuit breaker" in result["error"] and calls == []
    assert engine._cached_equity == 50000


@pytest.mark.parametrize("withdrawal,equity,pnl", [(100000,10000,10000), (105000,5000,10000)])
async def test_withdrawn_principal_preserves_dollar_profit_and_renders_missing_percentage(database,withdrawal,equity,pnl):
    await database.record_daily_equity("2026-09-09",100000)
    await database.replace_cash_transfers([
        {"id":"deposit","activity_type":"CSD","amount":100000,"date":"2026-09-09"},
        {"id":"withdrawal","activity_type":"CSW","amount":-withdrawal,"date":"2026-10-06"}])
    report = await build_report_data(database,NS(get_account=lambda:balance(equity=equity),get_positions=lambda:[]))
    assert report["account"]["baseline"] == "net_deposits"
    assert report["account"]["total_pnl"] == pnl and report["account"]["total_pnl_pct"] is None
    html = render_html(report)
    assert "+$10,000" in html and "Percentage unavailable" in html
    assert not any("Drawdown:" in p for p in report["proposals"])


async def test_report_export_and_notification_support_an_unavailable_return_percentage(report, monkeypatch):
    import daily_report
    main, calls, reports = report
    build = daily_report.build_report_data
    messages = []
    async def withdrawn_principal(*args, **kwargs):
        data = await build(*args, **kwargs)
        data["account"].update(total_pnl_pct=None, total_pnl=10000)
        return data
    async def notify(title, message):
        messages.append(message)
        return True
    monkeypatch.setattr(daily_report, "build_report_data", withdrawn_principal)
    monkeypatch.setattr(main.pushover, "send_alert", notify)
    data = await main.generate_daily_report(scheduled=True)
    assert data["account"]["total_pnl_pct"] is None and calls.export == 1
    assert main._report_complete(reports, "2026-10-06")
    assert len(messages) == 1 and "$+10,000" in messages[0]
    assert "return percentage unavailable" in messages[0]


def test_account_adapter_exposes_unleveraged_and_options_funds():
    broker = object.__new__(AlpacaTrader)
    account = NS(equity="100000",cash="60000",buying_power="400000",options_buying_power="12000",
                 non_marginable_buying_power="45000",daytrade_count=0,pattern_day_trader=False,
                 trading_blocked=False,account_blocked=False,status=NS(value="ACTIVE"))
    broker.client = NS(get_account=lambda:account)
    got = broker.get_account()
    assert got["options_buying_power"] == 12000 and got["non_marginable_buying_power"] == 45000
    account.options_buying_power = None
    assert broker.get_account()["options_buying_power"] is None


@pytest.mark.parametrize("paper,label", [(True,"Paper"), (False,"Live")])
async def test_report_identifies_actual_account_mode_without_hardcoded_balance(database,paper,label):
    broker = NS(paper=paper,get_account=lambda:balance(),get_positions=lambda:[])
    html = render_html(await build_report_data(database,broker))
    assert f"<title>{label} Trading" in html and "paper $50k" not in html

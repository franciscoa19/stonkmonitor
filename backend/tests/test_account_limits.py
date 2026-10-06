"""Account-wide risk cap and cash reserve. No real brokers or network."""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from api.routes import router
from config import Settings
from daily_report import build_report_data, render_html
from db import DatabaseError
from signals.iv_executor import build_iron_condor
from trading import account_risk
from trading.account_risk import account_limits, account_limits_snapshot
from test_execution_safety import database, session, condor  # noqa: F401  (fixtures)
from test_sizing_fixes import balance, card


def settings(**overrides):
    return Settings(_env_file=None, alpaca_api_key="unused", alpaca_secret_key="unused", **overrides)


def position(symbol, qty=1, market_value=None, cost_basis=None):
    row = {"symbol": symbol, "qty": str(qty)}
    if market_value is not None:
        row["market_value"] = str(market_value)
    if cost_basis is not None:
        row["cost_basis"] = str(cost_basis)
    return row


async def limits(db, account=None, positions=(), orders=(), s=None, **kw):
    return await account_limits(db, account or balance(), list(positions), list(orders), s or settings(), **kw)


# ── Settings ────────────────────────────────────────────────────────────────
def test_defaults_match_what_three_ten_percent_condors_already_implied():
    s = settings()
    assert s.account_max_risk_pct == pytest.approx(s.iv_exec_risk_pct * s.iv_exec_max_positions) == 0.30
    assert s.account_cash_reserve_pct == 0.20


@pytest.mark.parametrize("field,value", [
    ("account_max_risk_pct", 0), ("account_max_risk_pct", -0.1), ("account_max_risk_pct", 1.01),
    ("account_max_risk_pct", float("nan")), ("account_max_risk_pct", float("inf")),
    ("account_cash_reserve_pct", -0.01), ("account_cash_reserve_pct", 1),
    ("account_cash_reserve_pct", float("nan"))])
def test_limits_outside_a_sane_fraction_are_refused_at_startup(field, value):
    with pytest.raises(ValidationError):
        settings(**{field: value})


def test_the_reserve_can_be_switched_off_and_the_cap_set_to_everything():
    s = settings(account_cash_reserve_pct=0, account_max_risk_pct=1)
    assert (s.account_cash_reserve_pct, s.account_max_risk_pct) == (0, 1)


# ── What counts as risk in use ──────────────────────────────────────────────
async def test_an_idle_account_has_its_whole_cap_and_everything_above_the_reserve(database):
    got = await limits(database, balance(equity=100_000, cash=100_000, options=90_000))
    assert got["open_risk"] == 0 and got["risk_cap"] == got["risk_headroom"] == pytest.approx(30_000)
    assert got["free_cash"] == 90_000                     # the lower of cash and options buying power
    assert got["cash_reserve"] == pytest.approx(20_000) and got["cash_available"] == pytest.approx(70_000)


async def test_open_and_partly_closed_condors_count_their_remaining_max_loss(database):
    await condor(database, qty=4)                                   # $400 max loss per spread
    assert (await limits(database))["condor_risk"] == 1600
    await database._exec("UPDATE iv_condors SET closed_qty=3", strict=True)
    got = await limits(database)
    assert got["condor_risk"] == got["open_risk"] == 400
    assert got["risk_headroom"] == pytest.approx(30_000 - 400)


async def test_a_pending_condor_counts_before_it_fills(database):
    await condor(database, qty=2)
    await database._exec("UPDATE iv_condors SET status='pending_entry'", strict=True)
    assert (await limits(database))["condor_risk"] == 800


async def test_condor_legs_are_counted_once_and_other_holdings_at_what_they_can_lose(database):
    await condor(database, qty=2)                                   # legs SC, LC, SP, LP
    held = [position("SC", -2, market_value=-900), position("LC", 2, market_value=150),
            position("SP", -2, market_value=-700), position("LP", 2, market_value=120),
            position("AAPL", 10, market_value=2500),                # stock: its current value
            position("TSLA", -5, market_value=-1500),               # a short still counts
            position("MSFT", 3, cost_basis=1200),                   # no mark: cost is the fallback
            position("GONE", 0, market_value=999)]                  # flat: nothing at risk
    got = await limits(database, positions=held)
    assert got["condor_risk"] == 800 and got["position_risk"] == 2500 + 1500 + 1200
    assert got["open_risk"] == 800 + 5200


async def test_queued_flow_entries_count_at_full_cost_until_they_resolve(database):
    for kind, qty, price, status in (("option", 2, 1.5, "submitting"), ("equity", 10, 50, "submission_unknown"),
                                     ("equity_long", 4, 25, "confirmed")):
        tid = await database.save_pending_trade(datetime.utcnow(), ticker="X", symbol="X", trade_type=kind,
                                                qty=qty, limit_price=price, risk_amount=1)
        await database._exec("UPDATE pending_trades SET status=? WHERE id=?", (status, tid), strict=True)
    got = await limits(database)
    assert got["pending_risk"] == 2 * 1.5 * 100 + 10 * 50 + 4 * 25
    await database._exec("UPDATE pending_trades SET entry_order_status='filled' WHERE status='confirmed'",
                         strict=True)
    assert (await limits(database))["pending_risk"] == 300 + 500   # a filled entry is a position now


async def test_the_cap_follows_the_balance_down(database):
    """Three condors opened at 10% each are 30% of the account that day and
    more than that after a loss — a position count cannot see it."""
    await condor(database, qty=75)                                  # $30,000 of max loss
    assert (await limits(database, balance(equity=100_000)))["risk_headroom"] == 0
    assert (await limits(database, balance(equity=80_000)))["open_risk_pct"] == pytest.approx(0.375)
    assert (await limits(database, balance(equity=150_000)))["risk_headroom"] == pytest.approx(15_000)


# ── Free cash and the reserve ───────────────────────────────────────────────
@pytest.mark.parametrize("asset,expected", [("option", 40_000), ("stock", 25_000)])
async def test_free_cash_is_the_funding_that_asset_actually_uses(database, asset, expected):
    account = balance(equity=100_000, cash=60_000, options=40_000, stocks=25_000)   # stock BP 400k ignored
    got = await limits(database, account, asset=asset)
    assert got["free_cash"] == expected and got["cash_available"] == pytest.approx(expected - 20_000)


async def test_cash_never_goes_negative_when_the_reserve_is_already_breached(database):
    got = await limits(database, balance(equity=100_000, cash=15_000, options=15_000))
    assert got["free_cash"] == 15_000 and got["cash_available"] == 0


@pytest.mark.parametrize("listed", [False, True])
async def test_a_local_entry_the_broker_does_not_list_is_held_back_from_free_cash(database, listed):
    await condor(database, qty=2)
    await database._exec("UPDATE iv_condors SET status='pending_entry',entry_order_id='working'", strict=True)
    orders = [{"id": "working", "order_class": "mleg"}] if listed else []
    got = await limits(database, balance(cash=50_000, options=50_000), orders=orders)
    assert got["unlisted_reserved"] == (0 if listed else 1000)
    assert got["cash_available"] == pytest.approx(50_000 - 20_000 - (0 if listed else 1000))


# ── Unreadable inputs block; they are never read as zero ────────────────────
@pytest.mark.parametrize("account", [
    None, {}, {"equity": 100_000}, balance(equity=0), balance(equity=-5), balance(equity=float("nan")),
    balance(cash=None), balance(cash=-1), balance(cash="bad"), balance(options=None),
    balance(options=float("inf")), balance(error="unavailable"),
    balance(trading_blocked=True), balance(account_blocked=True)])
async def test_an_unreadable_or_blocked_balance_blocks(database, account):
    with pytest.raises(DatabaseError):
        await account_limits(database, account, [], [], settings())


@pytest.mark.parametrize("held", [
    [position("AAPL", 10)],                                # nothing to value it with
    [position("AAPL", "bad", market_value=10)], [position("AAPL", 10, market_value="NaN")],
    [{"qty": "1", "market_value": "10"}], ["AAPL"]])
async def test_a_holding_that_cannot_be_valued_blocks(database, held):
    with pytest.raises(DatabaseError):
        await limits(database, positions=held)


@pytest.mark.parametrize("corruption", ["closed_qty=99", "max_loss=0", "max_loss=-1", "qty=0",
                                        "legs_json='not json'"])
async def test_a_corrupt_condor_row_blocks(database, corruption):
    await condor(database, qty=2)
    await database._exec(f"UPDATE iv_condors SET {corruption}", strict=True)
    with pytest.raises(DatabaseError):
        await limits(database)


@pytest.mark.parametrize("positions,orders", [(None, []), ([], None), ([], [{"no": "id"}])])
async def test_a_missing_or_malformed_broker_snapshot_blocks(database, positions, orders):
    with pytest.raises(DatabaseError):
        await account_limits(database, balance(), positions, orders, settings())


async def test_an_unknown_asset_type_is_refused(database):
    with pytest.raises(DatabaseError):
        await limits(database, asset="crypto")


# ── Condor sizing honours both limits ───────────────────────────────────────
def narrow_condor(equity=50_000, **limits_kw):
    """$1-wide wings: $75 max loss and $100 collateral per spread; 20 spreads
    at $50k from the per-condor rules alone."""
    from market_time import et_today
    expiry = et_today() + timedelta(days=3)
    quotes = {"call-110": {"bid": 0.30, "ask": 0.34}, "put-90": {"bid": 0.30, "ask": 0.34},
              "call-111": {"bid": 0.16, "ask": 0.20}, "put-89": {"bid": 0.16, "ask": 0.20}}
    broker = NS(get_option_contracts=lambda tk, start, end, kind: [
        {"symbol": f"{kind}-{strike}", "strike": strike, "expiry": expiry}
        for strike in ([110, 111] if kind == "call" else [89, 90])],
        get_option_quotes=lambda syms: {sym: quotes[sym] for sym in syms})
    setup = NS(ticker="TEST", price=100, expected_move="10%",
               next_earnings_date=(expiry - timedelta(days=1)).isoformat())
    return build_iron_condor(broker, setup, equity,
                             settings(iv_exec_wing_width_pct=0.01, iv_exec_min_credit=0.05), **limits_kw)


def test_without_limits_or_with_room_to_spare_the_size_is_unchanged():
    plain, roomy = narrow_condor(), narrow_condor(risk_headroom=1e9, cash_available=1e9)
    assert plain["qty"] == roomy["qty"] == 20 and plain["limited_by"] is roomy["limited_by"] is None
    assert plain["max_loss"] == 75 and plain["collateral_usd"] == 2000
    assert plain["strikes"] == roomy["strikes"]


@pytest.mark.parametrize("headroom,cash,qty,limited_by", [
    (750, None, 10, "account_risk_cap"),          # 750 / $75 max loss
    (None, 750, 7, "cash_reserve"),               # 750 / $100 collateral
    (749.99, None, 9, "account_risk_cap"),        # floors, never rounds up
    (1e9, 300, 3, "cash_reserve"), (300, 1e9, 4, "account_risk_cap"),
    (600, 600, 6, "cash_reserve"),                # the tighter of the two wins
    (75, 100, 1, "account_risk_cap")])            # exactly one spread fits both
def test_each_limit_reduces_the_quantity_and_never_the_structure(headroom, cash, qty, limited_by):
    plan = narrow_condor(risk_headroom=headroom, cash_available=cash)
    assert plan["ok"] and plan["qty"] == qty and plan["limited_by"] == limited_by
    assert plan["risk_usd"] == qty * 75 and plan["collateral_usd"] == qty * 100
    assert plan["strikes"] == narrow_condor()["strikes"]
    assert headroom is None or plan["risk_usd"] <= headroom
    assert cash is None or plan["collateral_usd"] <= cash


@pytest.mark.parametrize("kw,reason", [
    ({"risk_headroom": 74.99}, "account risk cap"), ({"risk_headroom": 0}, "account risk cap"),
    ({"risk_headroom": -500}, "account risk cap"), ({"risk_headroom": float("nan")}, "account risk cap"),
    ({"cash_available": 99.99}, "cash reserve"), ({"cash_available": 0}, "cash reserve")])
def test_a_condor_that_does_not_fit_one_spread_is_not_opened(kw, reason):
    plan = narrow_condor(**kw)
    assert not plan["ok"] and reason in plan["reason"]


# ── The entry path ──────────────────────────────────────────────────────────
PLAN = {"ok": True, "expiry": "2026-11-27", "legs_json": "[]", "legs": [],
        "strikes": {"short_put": 90, "long_put": 85, "short_call": 110, "long_call": 115},
        "qty": 5, "credit": 1, "max_loss": 400, "limit_price": -1, "risk_usd": 2000,
        "collateral_usd": 2500}
SETUP = NS(ticker="NEW", recommendation="SELL_PREMIUM", next_earnings_date="2026-11-25",
           earnings_report_time="AMC")


def entry(session, database, monkeypatch, *, account=None, plan=None, positions=(), **overrides):
    """A fake broker and sizer around the real entry path. Returns what it saw."""
    from signals import iv_executor
    seen = NS(orders=[], sized=[], reads=[])
    account = account or balance()

    def sizer(*a, **kw):
        seen.sized.append(kw)
        return dict(plan or PLAN)

    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "settings", settings(iv_exec_enabled=True, iv_exec_max_positions=10, **overrides))
    monkeypatch.setattr(session, "trader", NS(
        get_account=lambda: seen.reads.append("account") or account,
        get_positions_raw=lambda: seen.reads.append("positions") or list(positions),
        get_open_orders_raw=lambda: seen.reads.append("orders") or [],
        multileg_order=lambda legs, qty, limit, **kw: seen.orders.append(qty) or {"id": "entry", "status": "accepted"}))
    monkeypatch.setattr(iv_executor, "is_pre_earnings_entry_window", lambda *a, **kw: True)
    monkeypatch.setattr(iv_executor, "build_iron_condor", sizer)
    return seen


async def test_the_sizer_is_given_what_is_left_under_both_limits(database, session, monkeypatch):
    await condor(database, qty=10)                                  # $4,000 already at risk
    seen = entry(session, database, monkeypatch, account=balance(equity=100_000, cash=70_000, options=64_000))
    await session.maybe_execute_condor(SETUP)
    assert seen.sized == [{"risk_headroom": pytest.approx(30_000 - 4_000),
                           "cash_available": pytest.approx(64_000 - 20_000)}]
    assert seen.orders == [5] and len(await database.get_active_condors()) == 2


async def test_the_balance_is_read_after_the_order_snapshot(database, session, monkeypatch):
    seen = entry(session, database, monkeypatch)
    await session.maybe_execute_condor(SETUP)
    assert seen.reads.index("orders") < len(seen.reads) - 1 - seen.reads[::-1].index("account")
    assert seen.reads[-1] == "account"                              # the newest snapshot sizes the trade


@pytest.mark.parametrize("why,kw", [
    ("risk cap reached", {"account": balance(equity=13_000, cash=13_000, options=13_000)}),   # $4,000 open > 30%
    ("cash reserve reached", {"account": balance(equity=100_000, cash=19_000, options=19_000)}),
    ("another holding uses the cap", {"positions": [position("SPY", 60, market_value=30_000)]}),
    ("buying power unreadable", {"account": balance(options=None)}),
    ("cash unreadable", {"account": {"equity": 100_000}}),
    ("account blocked", {"account": balance(trading_blocked=True)}),
    ("holding cannot be valued", {"positions": [position("SPY", 60)]})])
async def test_no_condor_is_sized_persisted_or_sent(database, session, monkeypatch, why, kw):
    await condor(database, qty=10)
    seen = entry(session, database, monkeypatch, **kw)
    await session.maybe_execute_condor(SETUP)
    assert seen.sized == [] and seen.orders == [], why
    assert len(await database.get_active_condors()) == 1


@pytest.mark.parametrize("plan_changes", [
    {"qty": 80},                                    # $32,000 of max loss against $30,000 of cap
    {"collateral_usd": 90_000},                     # more collateral than cash above the reserve
    {"qty": 0}, {"max_loss": float("nan")}])
async def test_a_plan_over_either_limit_is_refused_even_if_the_sizer_produced_it(
        database, session, monkeypatch, plan_changes):
    seen = entry(session, database, monkeypatch, plan={**PLAN, **plan_changes})
    await session.maybe_execute_condor(SETUP)
    assert len(seen.sized) == 1 and seen.orders == []
    assert await database.get_active_condors() == []


async def test_switching_both_limits_off_restores_the_old_behaviour(database, session, monkeypatch):
    await condor(database, qty=10)
    seen = entry(session, database, monkeypatch, account=balance(equity=13_000, cash=3_000, options=3_000),
                 account_max_risk_pct=1, account_cash_reserve_pct=0)
    await session.maybe_execute_condor(SETUP)
    assert seen.orders == [5]


async def test_an_entry_waits_for_the_shared_entry_lock(database, session, monkeypatch):
    """A condor and a flow trade must not both spend the same headroom."""
    monkeypatch.setattr(account_risk, "entry_lock", asyncio.Lock())
    seen = entry(session, database, monkeypatch)
    async with account_risk.entry_lock:
        task = asyncio.create_task(session.maybe_execute_condor(SETUP))
        await asyncio.sleep(0.2)          # far longer than the whole entry takes unblocked
        # Not one broker read: the balance must be taken inside the lock.
        assert not task.done() and seen.reads == [] and seen.orders == []
    await task
    assert seen.orders == [5]


# ── The flow path uses the same limits ──────────────────────────────────────
async def flow_card(db, s, **kw):
    engine, tid, broker, calls = await card(db, **kw)
    engine.settings = s
    return engine, tid, broker, calls


async def test_a_flow_entry_is_reduced_to_what_the_account_cap_leaves(database, monkeypatch):
    monkeypatch.setattr(account_risk, "entry_lock", asyncio.Lock())
    await condor(database, qty=72)                                  # $28,800 of the $30,000 cap in use
    engine, tid, broker, calls = await flow_card(database, settings())      # 20 x $100 card, 2% = $2,000
    result = await engine.confirm_trade(tid, 0)
    assert result["id"] == "entry" and calls[0]["qty"] == 12        # $1,200 of headroom
    assert (await database.get_pending_trades("confirmed"))[0]["qty"] == 12


async def test_a_flow_entry_is_reduced_to_the_cash_above_the_reserve(database, monkeypatch):
    monkeypatch.setattr(account_risk, "entry_lock", asyncio.Lock())
    engine, tid, broker, calls = await flow_card(database, settings())
    broker.get_account = lambda: balance(equity=100_000, cash=20_700, stocks=20_700)
    assert (await engine.confirm_trade(tid, 0))["id"] == "entry" and calls[0]["qty"] == 7


@pytest.mark.parametrize("account,condors", [
    (balance(equity=100_000, cash=20_050, stocks=20_050), 0),       # under one share above the reserve
    (balance(), 75)])                                               # cap fully used
async def test_a_flow_entry_that_no_longer_fits_stays_a_card(database, monkeypatch, account, condors):
    monkeypatch.setattr(account_risk, "entry_lock", asyncio.Lock())
    if condors:
        await condor(database, qty=condors)
    engine, tid, broker, calls = await flow_card(database, settings())
    broker.get_account = lambda: account
    result = await engine.confirm_trade(tid, 0)
    assert result["error"] and not result["ambiguous"] and calls == []
    assert (await database.get_pending_trades())[0]["status"] == "pending"


async def test_the_card_being_confirmed_does_not_count_against_itself(database, monkeypatch):
    monkeypatch.setattr(account_risk, "entry_lock", asyncio.Lock())
    engine, tid, broker, calls = await flow_card(database, settings(account_max_risk_pct=0.02))
    assert (await engine.confirm_trade(tid, 0))["id"] == "entry" and calls[0]["qty"] == 20


# ── Report and API ──────────────────────────────────────────────────────────
def report_broker(account=None, positions=()):
    return NS(paper=True, get_account=lambda: account or balance(equity=100_000, cash=80_000, options=74_000),
              get_positions=lambda: [], get_positions_raw=lambda: list(positions),
              get_open_orders_raw=lambda: [])


async def test_the_report_shows_risk_in_use_and_free_cash_against_their_limits(database):
    await condor(database, qty=10)
    data = await build_report_data(database, report_broker(), settings=settings())
    got = data["account_risk"]
    assert got["open_risk"] == 4000 and got["open_risk_pct"] == 0.04 and got["max_risk_pct"] == 0.30
    assert got["free_cash"] == 74_000 and got["cash_reserve_pct"] == 0.20
    json.dumps(data, default=str)
    html = render_html(data)
    assert "Risk in use" in html and "4.0%" in html and "cap 30%" in html
    assert "Free cash" in html and "74%" in html and "reserve 20%" in html


@pytest.mark.parametrize("broker,s", [
    (report_broker(account=balance(options=None)), settings()),      # cannot be read
    (NS(paper=True, get_account=lambda: balance(), get_positions=lambda: []), settings()),  # no raw snapshots
    (report_broker(), None)])                                         # caller did not ask
async def test_the_report_omits_limits_it_cannot_read_rather_than_inventing_them(database, broker, s):
    data = await build_report_data(database, broker, settings=s)
    assert data["account_risk"] is None and "Risk in use" not in render_html(data)


async def test_the_api_reports_the_limits_and_answers_503_when_they_are_unreadable(database, session, monkeypatch):
    await condor(database, qty=10)
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "settings", settings())
    monkeypatch.setattr(session, "trader", report_broker())
    app = FastAPI()
    app.include_router(router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as http:
        reply = await http.get("/api/risk/account")
        assert reply.status_code == 200
        body = reply.json()
        assert body["open_risk"] == 4000 and body["risk_headroom"] == 26_000
        assert body["cash_available"] == 74_000 - 20_000
        monkeypatch.setattr(session, "trader", report_broker(account=balance(cash=None)))
        assert (await http.get("/api/risk/account")).status_code == 503


async def test_the_snapshot_reads_orders_before_the_balance(database):
    reads = []
    broker = NS(get_open_orders_raw=lambda: reads.append("orders") or [],
                get_positions_raw=lambda: reads.append("positions") or [],
                get_account=lambda: reads.append("account") or balance())
    await account_limits_snapshot(database, broker, settings())
    assert reads == ["orders", "positions", "account"]

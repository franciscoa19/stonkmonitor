"""Sizing follows the account's true balance, and a ledger stays with its account.

No broker or network: fake REST responses and temporary databases only.
"""
from datetime import timedelta
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import ValidationError

from config import Settings
from db import AccountMismatch, Database, DB_PATH, resolve_db_path
from signals.auto_trade import AutoTradeEngine
from signals.iv_executor import build_iron_condor, condor_qty_cap, condor_risk_budget
from test_execution_safety import database, session  # noqa: F401  (fixtures)
from trading.alpaca_trader import AlpacaTrader


def settings(**overrides):
    return Settings(_env_file=None, alpaca_api_key="unused", alpaca_secret_key="unused", **overrides)


# ── Condor sizing ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("equity", [25_000, 51_870.47, 60_000, 100_000, 250_000])
def test_condor_risk_is_the_configured_percentage_at_any_account_size(equity):
    """A fixed $6,000 ceiling replaced the 10% above $60k of equity, so a $100k
    account would have risked 6% per condor. Unset, the percentage governs."""
    budget, ceiling_binding = condor_risk_budget(equity, settings())
    assert budget == pytest.approx(equity * 0.10) and not ceiling_binding


def test_an_explicit_dollar_ceiling_still_caps_and_reports_that_it_did():
    s = settings(iv_exec_max_risk_usd=6000)
    assert condor_risk_budget(51_870, s) == (pytest.approx(5187.0), False)
    assert condor_risk_budget(100_000, s) == (6000, True)
    assert condor_risk_budget(100_000, s, risk_multiplier=0.5) == (3000, True)   # throttle still applies


@pytest.mark.parametrize("value,expected", [("", None), ("   ", None), ("6000", 6000.0)])
def test_blank_ceiling_setting_means_no_ceiling(value, expected):
    assert settings(iv_exec_max_risk_usd=value).iv_exec_max_risk_usd == expected
    assert settings().iv_exec_max_risk_usd is None


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_a_zero_or_negative_ceiling_is_rejected_rather_than_silently_stopping_trades(bad):
    with pytest.raises(ValidationError):
        settings(iv_exec_max_risk_usd=bad)


@pytest.mark.parametrize("equity,cap", [(51_870.47, 20), (100_000, 40), (250_000, 100),
                                        (25_000, 10), (10_000, 4), (2_000, 1), (0, 1)])
def test_contract_cap_scales_with_equity(equity, cap):
    """It was a fixed 20, right for the ~$50k account it was written for."""
    assert condor_qty_cap(equity, settings()) == cap


def cheap_condor(equity, s):
    """A narrow, cheap spread: max loss $75 per spread, so the budget alone buys many."""
    from market_time import et_today
    expiry = et_today() + timedelta(days=3)
    quotes = {"call-110": {"bid": 0.30, "ask": 0.34}, "put-90": {"bid": 0.30, "ask": 0.34},
              "call-111": {"bid": 0.16, "ask": 0.20}, "put-89": {"bid": 0.16, "ask": 0.20}}
    broker = NS(get_option_contracts=lambda tk, start, end, kind: [
        {"symbol": f"{kind}-{strike}", "strike": strike, "expiry": expiry}
        for strike in ([110, 111] if kind == "call" else [89, 90])],
        get_option_quotes=lambda syms: {sym: quotes[sym] for sym in syms})
    return build_iron_condor(broker, NS(ticker="TEST", price=100, expected_move="10%",
        next_earnings_date=(expiry - timedelta(days=1)).isoformat()), equity, s)


def test_a_cheap_spread_is_capped_by_the_scaled_contract_limit_not_a_fixed_twenty():
    s = settings(iv_exec_wing_width_pct=0.01, iv_exec_min_credit=0.05)
    small, large = cheap_condor(50_000, s), cheap_condor(100_000, s)
    assert small["ok"] and large["ok"], (small, large)
    assert (small["qty"], small["qty_cap"]) == (20, 20)
    assert (large["qty"], large["qty_cap"]) == (40, 40)            # was 20 at any size
    assert large["risk_usd"] <= large["risk_budget"] == 10_000 and not large["ceiling_binding"]


# ── Flow daily-loss limit ───────────────────────────────────────────────────
@pytest.mark.parametrize("equity,loss,halted", [
    (100_000, -2_500, False),     # -2.5%: the old -$2,000 fallback halted here
    (100_000, -5_000, True),      # -5%
    (40_000, -1_900, False),      # -4.75%
    (40_000, -2_000, True),       # -5% of a smaller account
    (0, -1_999, False),           # equity unknown: the dollar limit is the only guard
    (0, -2_000, True),
])
def test_daily_loss_limit_is_a_percentage_whenever_equity_is_known(equity, loss, halted):
    engine = AutoTradeEngine(settings())
    engine._cached_equity = equity
    engine._refresh_daily_pnl_date()
    engine._daily_pnl = loss
    assert engine._circuit_breaker_active() is halted


# ── One database, one broker account ────────────────────────────────────────
def trader_with(rest, paper=True):
    trader = AlpacaTrader.__new__(AlpacaTrader)
    trader._trade_base, trader.paper, trader._rest = "https://broker.invalid", paper, rest
    return trader


def test_account_fingerprint_separates_accounts_and_never_exposes_the_id():
    account_id = "0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"
    paper = trader_with(lambda *a: (200, {"id": account_id})).account_fingerprint()
    live = trader_with(lambda *a: (200, {"id": account_id}), paper=False).account_fingerprint()
    other = trader_with(lambda *a: (200, {"id": "ffffffff-0000-1111-2222-333333333333"})).account_fingerprint()
    assert paper.startswith("paper:") and live.startswith("live:")
    assert len({paper, live, other}) == 3
    assert account_id not in paper and account_id[:8] not in paper
    assert trader_with(lambda *a: (200, {"id": account_id})).account_fingerprint() == paper   # stable


@pytest.mark.parametrize("reply", [(500, {"message": "down"}), (200, {}), (200, {"id": ""}), (200, [])])
def test_account_fingerprint_is_none_when_the_account_cannot_be_read(reply):
    assert trader_with(lambda *a: reply).account_fingerprint() is None


async def test_database_binds_to_one_account_and_refuses_another(tmp_path):
    db = Database(tmp_path / "ledger.db")
    await db.connect()
    try:
        await db.bind_account("paper:aaaa")
        await db.bind_account("paper:aaaa")                        # the same account, any number of times
        with pytest.raises(AccountMismatch, match="DB_PATH"):
            await db.bind_account("live:bbbb")                     # paper ledger, live keys
    finally:
        await db.close()
    reopened = Database(tmp_path / "ledger.db")                    # the binding survives a restart
    await reopened.connect()
    try:
        with pytest.raises(AccountMismatch):
            await reopened.bind_account("paper:cccc")
        await reopened.bind_account("paper:aaaa")
    finally:
        await reopened.close()


async def test_unverified_account_blocks_new_condors_until_the_broker_answers(database, session, monkeypatch):
    from signals import iv_executor
    plan = {"ok": True, "expiry": "2026-11-27", "legs_json": "[]", "legs": [],
            "strikes": {"short_put": 90, "long_put": 85, "short_call": 110, "long_call": 115},
            "qty": 1, "credit": 1, "max_loss": 400, "limit_price": -1, "risk_usd": 400}
    calls, fingerprint = [], [None]
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "_account_bound", False)
    monkeypatch.setattr(session, "settings", settings(iv_exec_enabled=True))
    monkeypatch.setattr(session, "trader", NS(
        get_account=lambda: {"equity": 100000, "cash": 100000, "options_buying_power": 100000}, account_fingerprint=lambda: fingerprint[0],
        get_positions_raw=lambda: [], get_open_orders_raw=lambda: [],
        multileg_order=lambda *a, **kw: calls.append(kw) or {"id": "entry", "status": "accepted"}))
    monkeypatch.setattr(iv_executor, "is_pre_earnings_entry_window", lambda *a, **kw: True)
    monkeypatch.setattr(iv_executor, "build_iron_condor", lambda *a, **kw: plan)
    setup = NS(ticker="TEST", recommendation="SELL_PREMIUM",
               next_earnings_date="2026-11-25", earnings_report_time="AMC")

    await session.maybe_execute_condor(setup)                      # broker cannot identify the account
    assert calls == [] and await database.get_active_condors() == []

    fingerprint[0] = "paper:aaaa"
    await session.maybe_execute_condor(setup)
    assert len(calls) == 1 and session._account_bound


async def test_a_ledger_from_another_account_is_never_traded(database, session, monkeypatch):
    await database.bind_account("paper:original")
    monkeypatch.setattr(session, "db", database)
    monkeypatch.setattr(session, "_account_bound", False)
    monkeypatch.setattr(session, "trader", NS(account_fingerprint=lambda: "live:different"))
    with pytest.raises(AccountMismatch):
        await session.bind_broker_account()
    assert session._account_bound is False


def test_database_path_setting(tmp_path):
    assert resolve_db_path("") == resolve_db_path("   ") == DB_PATH
    assert resolve_db_path("live.db") == DB_PATH.parent / "live.db"          # from backend/, not the cwd
    assert resolve_db_path(str(tmp_path / "x.db")) == tmp_path / "x.db"
    assert settings().db_path == "" and settings(db_path="live.db").db_path == "live.db"


# ── P&L measured against contributed cash ───────────────────────────────────
def transfer(i, kind, amount, date="2026-09-09", status="executed"):
    return {"id": f"{date.replace('-', '')}000000000::{i}", "activity_type": kind,
            "net_amount": str(amount), "date": date, "status": status}


def test_cash_transfers_are_read_in_full_and_canceled_ones_left_out():
    pages = [[transfer(i, "CSD", 100) for i in range(100)],
             [transfer(100, "JNLC", 50_000), transfer(101, "CSW", -20_000),
              transfer(102, "CSD", 999, status="canceled")]]
    queries = []

    def rest(method, url):
        queries.append(parse_qs(urlparse(url).query))
        return 200, pages.pop(0)

    got = trader_with(rest).get_cash_transfers()
    assert len(got) == 102 and sum(t["amount"] for t in got) == 100 * 100 + 50_000 - 20_000
    assert queries[0]["activity_types"] == ["CSD,CSW,JNLC,ACATC,TRANS"]
    assert queries[1]["page_token"] == [transfer(99, "CSD", 100)["id"]] and pages == []


@pytest.mark.parametrize("reply", [
    (500, {"message": "down"}), (200, {"not": "a list"}),
    (200, [{"id": "x", "activity_type": "FILL", "net_amount": "1"}]),          # not a transfer
    (200, [{"id": "x", "activity_type": "CSD", "net_amount": "nan"}]),
    (200, [{"id": "x", "activity_type": "CSD"}]),
])
def test_an_unreadable_transfer_list_is_none_not_an_empty_history(reply):
    assert trader_with(lambda *a: reply).get_cash_transfers() is None
    assert trader_with(lambda *a: (200, [])).get_cash_transfers() == []


async def test_a_deposit_is_capital_not_profit(database):
    from daily_report import build_report_data
    await database.record_daily_equity("2026-09-09", 50_000.0)
    await database.record_daily_equity("2026-10-06", 151_500.0)
    broker = NS(get_account=lambda: {"equity": 151_500.0, "cash": 151_500.0, "buying_power": 0},
                get_positions=lambda: [])

    before = (await build_report_data(database, broker))["account"]
    assert before["baseline"] == "first_snapshot"                   # no transfers known yet:
    assert before["total_pnl"] == pytest.approx(101_500)            # the $100k deposit reads as profit

    await database.replace_cash_transfers([
        {"id": "a", "activity_type": "JNLC", "amount": 50_000, "date": "2026-09-09"},
        {"id": "b", "activity_type": "CSD", "amount": 100_000, "date": "2026-10-05"}])
    after = (await build_report_data(database, broker))["account"]
    assert (after["baseline"], after["net_deposits"], after["start_equity"]) == ("net_deposits", 150_000, 150_000)
    assert after["total_pnl"] == pytest.approx(1_500) and after["total_pnl_pct"] == pytest.approx(1.0)

    await database.replace_cash_transfers([                          # then $30k is withdrawn
        {"id": "a", "activity_type": "JNLC", "amount": 50_000, "date": "2026-09-09"},
        {"id": "b", "activity_type": "CSD", "amount": 100_000, "date": "2026-10-05"},
        {"id": "c", "activity_type": "CSW", "amount": -30_000, "date": "2026-10-06"}])
    broker.get_account = lambda: {"equity": 121_500.0, "cash": 121_500.0, "buying_power": 0}
    withdrawn = (await build_report_data(database, broker))["account"]
    assert withdrawn["total_pnl"] == pytest.approx(1_500)            # a withdrawal is not a loss


async def test_transfer_mirror_is_atomic_and_rejects_bad_rows(database):
    good = [{"id": "a", "activity_type": "JNLC", "amount": 50_000, "date": "2026-09-09"}]
    await database.replace_cash_transfers(good)
    with pytest.raises((ValueError, TypeError)):
        await database.replace_cash_transfers(good + [{"id": "", "activity_type": "CSD", "amount": 1}])
    with pytest.raises(Exception):                                   # duplicate id: nothing is replaced
        await database.replace_cash_transfers(good + good)
    assert await database.get_contributed_capital() == 50_000
    await database.replace_cash_transfers([])
    assert await database.get_contributed_capital() is None

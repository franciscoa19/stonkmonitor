"""Study causality, missing data, session clocks, and measurement provenance."""
import sqlite3
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from backtest import stop_study as s
from db import Database
from market_time import ET
from test_execution_safety import database  # noqa: F401


def at(day, hh=0, mm=0):
    return datetime.fromisoformat(day).replace(hour=hh, minute=mm, tzinfo=ET).astimezone(timezone.utc)


def event(expiry="2026-10-16", earnings="2026-10-14"):
    exp = date.fromisoformat(expiry)
    return s.Event(kind="research", ident="case", ticker="TEST", qty=1, credit=1, fees=0,
        legs=s._legs_from_strikes("TEST", exp, 90, 85, 110, 115), short_put=90, short_call=110,
        width=5, entry=at("2026-10-12", 10),
        earnings_date=date.fromisoformat(earnings) if earnings else None,
        expiry=exp, recorded_pnl=None, cohort="research")


def raw(day, hh, mm, close):
    return {"t": at(day, hh, mm).isoformat(), "o": 100, "h": max(100, close),
            "l": min(100, close), "c": close}


def option_bars(ev, values):
    """values = [(bar start, net debit)]; positive prices for every leg."""
    return {leg["symbol"]: s.AlpacaBars._series([
        {"t": start.isoformat(), "c": debit + .01 if leg["right"] == "P" and leg["side"] == "sell" else .01}
        for start, debit in values]) for leg in ev.legs}


def test_real_adapter_waits_for_bar_completion_and_then_a_later_fill_bar():
    ev = event()
    stock = s.AlpacaBars._series([raw("2026-10-13", 9, 55, 89), raw("2026-10-13", 10, 0, 89)])
    legs = option_bars(ev, [(at("2026-10-13", 9, 55), 2), (at("2026-10-13", 10), 3)])
    assert stock[0][0] == at("2026-10-13", 10)
    assert s.price_at(stock, at("2026-10-13", 9, 59)) is None
    early = s.replay(ev, stock, legs, now=at("2026-10-13", 10, 4))
    assert early["breach stop"] is None
    assert ev.unpriced_exits["breach stop"]["trigger_et"].startswith("2026-10-13T10:00")
    result = s.replay(ev, stock, legs, now=at("2026-10-13", 10, 5))
    assert result["breach stop"] == (-200, "10-13 10:05", "breach stop")


def test_fill_needs_a_post_decision_observation_in_every_leg():
    ev = event()
    stock = s.AlpacaBars._series([raw("2026-10-13", 9, 55, 89), raw("2026-10-13", 10, 0, 89)])
    legs = option_bars(ev, [(at("2026-10-13", 9, 55), 2), (at("2026-10-13", 10), 3)])
    legs[ev.legs[1]["symbol"]] = legs[ev.legs[1]["symbol"]][:1]
    result = s.replay(ev, stock, legs, now=at("2026-10-13", 10, 5))
    assert result["breach stop"] is None  # Its old long-wing print is not an exit fill.


def test_partial_bar_after_a_decision_is_not_a_post_decision_fill():
    decision = at("2026-10-13", 10)
    spanning = s.AlpacaBars._series([raw("2026-10-13", 9, 58, 2)])
    assert s.price_at(spanning, decision + timedelta(minutes=3), after=decision) is None


def test_unfinished_last_bar_is_not_exposed_by_the_api_adapter():
    class Broker:
        def _rest(self, method, url):
            assert method == "GET"
            return 200, {"bars": {"TEST": [raw("2026-10-13", 10, 0, 89)]}}
    assert s.AlpacaBars(Broker()).stock("TEST", at("2026-10-13", 9), at("2026-10-13", 10, 4)) == []


def test_unpriceable_stop_is_missing_data_even_when_expiry_is_profitable():
    ev = event()
    stock = s.AlpacaBars._series([raw("2026-10-13", 9, 55, 89), raw("2026-10-16", 15, 55, 100)])
    ev.results = s.replay(ev, stock, {}, now=at("2026-10-17"))
    assert ev.results["hold"][0] == 100
    assert ev.results["breach stop"] is None
    assert ev.unpriced_exits["breach stop"]["reason"] == "breach stop"
    block = s.summarise([ev], [])["research — all"]["rules"]["breach stop"]
    assert block["n"] == 0 and block["no_data"] == 1 and block["win_rate"] is None


@pytest.mark.parametrize("when,known", [
    ("2026-10-16T10:00:00-04:00", False),
    ("2026-10-16T16:29:59-04:00", False),
    ("2026-10-16T20:30:00+00:00", True),
    ("2026-10-17T02:00:00+00:00", True),
])
def test_expiry_close_requires_a_completed_session_even_if_future_bars_are_supplied(when, known):
    ev = event()
    stock = s.AlpacaBars._series([raw("2026-10-16", 9, 55, 100), raw("2026-10-16", 15, 55, 100)])
    result = s.replay(ev, stock, {}, now=datetime.fromisoformat(when))
    assert (result["hold"] is not None) is known


def test_a_morning_or_stale_expiry_bar_is_never_a_session_close():
    ev = event()
    for hh, mm in ((9, 55), (15, 45)):
        stock = s.AlpacaBars._series([raw("2026-10-16", hh, mm, 100)])
        assert s.replay(ev, stock, {}, now=at("2026-10-17"))["hold"] is None


@pytest.mark.parametrize("when,known", [
    ("2026-11-27T13:29:59-05:00", False), ("2026-11-27T18:30:00+00:00", True)])
def test_half_day_uses_the_1300_close_and_ignores_after_hours(when, known):
    ev = event("2026-11-27", "2026-11-24")
    stock = s.AlpacaBars._series([raw("2026-11-27", 12, 55, 100), raw("2026-11-27", 14, 0, 80)])
    result = s.replay(ev, stock, {}, now=datetime.fromisoformat(when))
    assert (result["hold"] is not None) is known
    if known:
        assert result["hold"][0] == 100


def test_half_day_forced_close_signals_at_noon_and_prices_a_later_bar():
    ev = event("2026-11-27", earnings=None)
    stock = s.AlpacaBars._series([raw("2026-11-27", 11, 55, 100), raw("2026-11-27", 12, 0, 100)])
    legs = option_bars(ev, [(at("2026-11-27", 11, 55), 1), (at("2026-11-27", 12), 1.2)])
    result = s.replay(ev, stock, legs, now=at("2026-11-28"))
    assert result["policy"][1:] == ("11-27 12:05", "expiry deadline")
    assert result["policy"][0] == pytest.approx(-20)


def test_holidays_and_after_hours_cannot_trigger_or_price_an_exit():
    ev = event("2026-11-27", "2026-11-24")
    stock = s.AlpacaBars._series([raw("2026-11-26", 9, 55, 89), raw("2026-11-27", 14, 0, 89)])
    legs = option_bars(ev, [(at("2026-11-26", 9, 55), 2), (at("2026-11-27", 14), 3)])
    result = s.replay(ev, stock, legs, now=at("2026-11-28"))
    assert all(value is None for value in result.values())


def test_a_waiting_profit_target_cannot_fill_above_its_limit():
    ev = event()
    stock = s.AlpacaBars._series([raw("2026-10-13", 9, 55, 100), raw("2026-10-13", 10, 0, 100)])
    legs = option_bars(ev, [(at("2026-10-13", 9, 55), .4), (at("2026-10-13", 10), 1.2)])
    assert s.replay(ev, stock, legs, now=at("2026-10-13", 10, 5))["policy"] is None


async def test_real_resolver_records_the_session_and_filters_that_day_not_the_retry_day(database, monkeypatch):
    import main
    await database.record_iv_eval("TEST", "SELL_PREMIUM", 1.5, 4, 100, "2026-10-18", "2026-10-16")
    await database._exec("UPDATE iv_rv_evals SET signal_date='2026-10-15'", strict=True)
    calls = []
    def historical(ticker, day):
        calls.append((ticker, day))
        return 105
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main, "feed", NS(get_daily_close=historical))
    assert await main.resolve_due_iv_evals("2026-10-20", now=at("2026-10-20", 12)) == 1
    row = (await database._query("SELECT * FROM iv_rv_evals"))[0]
    assert calls == [("TEST", "2026-10-19")] and row["measurement_session"] == "2026-10-19"
    await database.add_market_event("2026-10-19", "MEASUREMENT")
    assert (await database.get_iv_eval_summary())["resolved"] == 1
    assert (await database.get_iv_eval_summary(exclude_market_events=True))["resolved"] == 0
    await database.remove_market_event("2026-10-19")
    await database.add_market_event("2026-10-20", "LATE RETRY")
    assert (await database.get_iv_eval_summary(exclude_market_events=True))["resolved"] == 1


async def test_legacy_window_is_conservative_uses_et_dates_and_never_invents_provenance(database):
    await database.record_iv_eval("TEST", "SELL_PREMIUM", 1.5, 4, 100, "2026-10-18", "2026-10-16")
    await database._exec("UPDATE iv_rv_evals SET signal_date='2026-10-15'", strict=True)
    row = (await database._query("SELECT * FROM iv_rv_evals"))[0]
    await database.resolve_iv_eval(row["id"], 105, 5, 4)
    await database._exec("UPDATE iv_rv_evals SET resolved_at='2026-10-20T02:00:00'", strict=True)
    original = (await database._query("SELECT * FROM iv_rv_evals"))[0]
    await database.add_market_event("2026-10-19", "WITHIN LEGACY WINDOW")
    assert (await database.get_iv_eval_summary(exclude_market_events=True))["resolved"] == 0
    await database.remove_market_event("2026-10-19")
    await database.add_market_event("2026-10-20", "AFTER LEGACY WINDOW")
    assert (await database.get_iv_eval_summary(exclude_market_events=True))["resolved"] == 1
    assert (await database._query("SELECT * FROM iv_rv_evals"))[0] == original
    assert original["measurement_session"] is None


async def test_session_provenance_migration_preserves_old_results(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path)
    await db.connect()
    await db.record_iv_eval("TEST", "SELL_PREMIUM", 1.5, 4, 100, "2026-10-18", "2026-10-16")
    await db.resolve_iv_eval(1, 105, 5, 4)
    await db.close()
    with sqlite3.connect(path) as con:
        con.execute("ALTER TABLE iv_rv_evals DROP COLUMN measurement_session")
    await db.connect()
    try:
        row = (await db._query("SELECT * FROM iv_rv_evals"))[0]
        assert row["resolved"] == 1 and row["exit_price"] == 105 and row["measurement_session"] is None
    finally:
        await db.close()

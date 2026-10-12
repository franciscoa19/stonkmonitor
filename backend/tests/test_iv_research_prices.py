"""Research price evidence: real feed adapter, fake SDK, temporary ledgers."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from feeds.alpaca_feed import AlpacaFeed, spot_from
from market_time import ET
from test_execution_safety import database  # noqa: F401


def bar(day, close):
    return NS(timestamp=datetime.fromisoformat(day).replace(tzinfo=ET), close=close)


def research_feed(bars=(), *, error=None, stale_trade=None):
    calls = []
    feed = object.__new__(AlpacaFeed)

    def historical(request):
        calls.append(("bars", request.symbol_or_symbols, request.start.date().isoformat()))
        if error:
            raise error
        return NS(data={"PEP": list(bars)})

    def latest_trade(request):
        calls.append(("trade",))
        return {"PEP": stale_trade} if stale_trade else {}

    def latest_quote(request):
        calls.append(("quote",))
        return {"PEP": NS(bid_price=100, ask_price=100.2, bid_size=1, ask_size=1,
                          timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc))}

    feed.stock_client = NS(get_stock_bars=historical,
                           get_stock_latest_trade=latest_trade,
                           get_stock_latest_quote=latest_quote)
    return feed, calls


async def seed(db, *, horizon="2026-10-10", earnings="2026-10-08", signal="2026-10-06"):
    await db.record_iv_eval("PEP", "SELL_PREMIUM", 1.5, 4.0, 100.0, horizon, earnings)
    await db._exec("UPDATE iv_rv_evals SET signal_date=?", (signal,), strict=True)


async def saved(db):
    return (await db._query("SELECT * FROM iv_rv_evals"))[0]


async def resolve(db, monkeypatch, feed, *, now="2026-10-11T22:00:00-04:00"):
    import main
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "feed", feed)
    n = datetime.fromisoformat(now)
    return await main.resolve_due_iv_evals(n.astimezone(ET).date().isoformat(), now=n)


@pytest.mark.parametrize("old_bars", [(), (bar("2026-09-01", 100.1),),
                                      (bar("2026-10-08", 100.1),),
                                      (bar("2026-10-12", 100.1),)])
async def test_no_target_bar_cannot_resolve_from_an_old_quote_or_another_session(
        database, monkeypatch, old_bars):
    await seed(database)
    feed, calls = research_feed(old_bars)
    assert await resolve(database, monkeypatch, feed) == 0
    row = await saved(database)
    assert row["resolved"] == 0 and row["exit_price"] is None
    assert calls == [("bars", "PEP", "2026-10-09")]


async def test_a_pre_earnings_trade_is_never_used_to_resolve(database, monkeypatch):
    await seed(database)
    old = NS(price=100, timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc))
    feed, calls = research_feed(stale_trade=old)
    assert await resolve(database, monkeypatch, feed) == 0
    assert (await saved(database))["resolved"] == 0
    assert calls == [("bars", "PEP", "2026-10-09")]


async def test_weekend_resolution_uses_fridays_historical_close_and_is_idempotent(database, monkeypatch):
    await seed(database)
    feed, calls = research_feed([bar("2026-10-09", 106)])
    assert await resolve(database, monkeypatch, feed) == 1
    row = await saved(database)
    assert row["resolved"] == 1 and row["exit_price"] == 106
    assert row["realized_move_pct"] == 6 and row["hypo_win"] == 0
    assert await resolve(database, monkeypatch, feed) == 0
    assert await saved(database) == row
    assert calls == [("bars", "PEP", "2026-10-09")]


async def test_delayed_retry_uses_the_same_horizon_not_mondays_latest_price(database, monkeypatch):
    await seed(database)
    latest = NS(price=100.1, timestamp=datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc))
    feed, calls = research_feed([bar("2026-10-09", 106), bar("2026-10-12", 100.1)], stale_trade=latest)
    assert await resolve(database, monkeypatch, feed, now="2026-10-12T11:00:00-04:00") == 1
    assert (await saved(database))["exit_price"] == 106
    assert (await saved(database))["hypo_win"] == 0
    assert calls == [("bars", "PEP", "2026-10-09")]


async def test_missing_history_retries_without_substituting_a_quote(database, monkeypatch):
    await seed(database)
    feed, calls = research_feed(error=RuntimeError("history unavailable"))
    assert await resolve(database, monkeypatch, feed) == 0
    assert (await saved(database))["resolved"] == 0
    feed, calls = research_feed([bar("2026-10-09", 102)])
    assert await resolve(database, monkeypatch, feed) == 1
    assert (await saved(database))["exit_price"] == 102


@pytest.mark.parametrize("close", [None, "bad", 0, -1, float("nan"), float("inf")])
async def test_invalid_historical_price_leaves_the_observation_pending(database, monkeypatch, close):
    await seed(database)
    feed, _ = research_feed([bar("2026-10-09", close)])
    assert await resolve(database, monkeypatch, feed) == 0
    assert (await saved(database))["resolved"] == 0


@pytest.mark.parametrize("when,expected", [
    ("2026-10-09T09:45:00-04:00", 0),
    ("2026-10-09T16:29:59-04:00", 0),
    ("2026-10-09T20:30:00+00:00", 1),
    ("2026-11-27T13:29:59-05:00", 0),
    ("2026-11-27T18:30:00+00:00", 1),
])
async def test_live_daily_bar_is_not_a_completed_close(database, monkeypatch, when, expected):
    n = datetime.fromisoformat(when).astimezone(ET)
    target = n.date().isoformat()
    earnings = (n.date() - timedelta(days=2)).isoformat()
    await seed(database, horizon=target, earnings=earnings)
    feed, calls = research_feed([bar(target, 102)])
    assert await resolve(database, monkeypatch, feed, now=when) == expected
    assert len(calls) == expected  # No request at all until the session has settled.


@pytest.mark.parametrize("horizon,earnings,signal,expected", [
    ("2026-10-10", "2026-10-08", "2026-10-06", "2026-10-09"),
    ("2026-11-26", "2026-11-24", "2026-11-23", "2026-11-25"),
    ("2026-10-11", "2026-10-09", "2026-10-08", "2026-10-12"),
    ("2026-10-10", None, "2026-10-06", "2026-10-09"),
    ("2026-10-10", None, "2026-10-11", "2026-10-12"),
    ("bad", "2026-10-08", "2026-10-06", None),
    ("2026-10-10", "bad", "2026-10-06", None),
])
def test_measurement_session_respects_the_horizon_and_event(horizon, earnings, signal, expected):
    import main
    assert main._iv_eval_measurement_session({"resolve_after": horizon,
        "earnings_date": earnings, "signal_date": signal}) == expected


async def test_weekend_horizon_after_friday_earnings_waits_for_a_post_report_session(database, monkeypatch):
    await seed(database, horizon="2026-10-11", earnings="2026-10-09")
    feed, calls = research_feed([bar("2026-10-09", 100.1), bar("2026-10-12", 106)])
    assert await resolve(database, monkeypatch, feed) == 0
    assert calls == []
    assert await resolve(database, monkeypatch, feed, now="2026-10-12T16:30:00-04:00") == 1
    assert (await saved(database))["exit_price"] == 106
    assert calls == [("bars", "PEP", "2026-10-12")]


@pytest.mark.parametrize("stamp", [None, "bad", "2026-10-09T13:30:00", "2026-10-09T13:30:01Z"])
def test_unlimited_trade_age_still_requires_a_past_timestamp(stamp):
    now = datetime(2026, 10, 9, 13, 30, tzinfo=timezone.utc)
    assert spot_from({"price": 100, "timestamp": stamp}, None,
                     now=now, max_age_seconds=None) is None


def test_unlimited_trade_age_never_disables_the_quote_age_limit():
    now = datetime(2026, 10, 9, 13, 30, tzinfo=timezone.utc)
    old = {"bid": 100, "ask": 100.2, "timestamp": "2026-09-01T13:30:00Z"}
    assert spot_from(None, old, now=now, max_age_seconds=None) is None
    fresh = {**old, "timestamp": now.isoformat()}
    assert spot_from(None, fresh, now=now, max_age_seconds=None) == pytest.approx(100.1)
    assert spot_from({"price": 100, "timestamp": old["timestamp"]}, None,
                     now=now, max_age_seconds=None) == 100

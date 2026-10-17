"""The daily close is asked for only up to 16 minutes ago.

The data plan refuses SIP history ending within the last 15 minutes. Asking
for the session's bar through UTC midnight (20:00 ET) was refused every hour
until 20:15 ET: on 2026-10-16, 125 research structures expiring that day waited
until 21:11 to settle (124 refusals in the log). Fake data client, no network.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from feeds.alpaca_feed import AlpacaFeed
from market_time import ET
from test_execution_safety import database  # noqa: F401  (fixture)
from test_iv_research_prices import seed


class DataPlan:
    """Answers like the real data API on this plan: an end within the last 15
    minutes is refused; otherwise the session's daily bar (stamped midnight ET)."""

    def __init__(self, now, close=180.57):
        self.now, self.close, self.requests = now, close, []

    def get_stock_bars(self, request):
        fields = request.to_request_fields()              # what the SDK sends
        self.requests.append(fields)
        end = datetime.fromisoformat(fields["end"])
        if end > self.now - timedelta(minutes=15):
            raise RuntimeError('{"message":"subscription does not permit querying recent SIP data"}')
        day = datetime.fromisoformat(fields["start"]).date()
        stamp = datetime(day.year, day.month, day.day, tzinfo=ET)
        return NS(data={request.symbol_or_symbols: [NS(timestamp=stamp, close=self.close)]})


def feed_at(now):
    feed = AlpacaFeed.__new__(AlpacaFeed)
    feed.stock_client = DataPlan(now)
    return feed


def et(day, hh, mm):
    return datetime.fromisoformat(day).replace(hour=hh, minute=mm, tzinfo=ET)


def test_half_an_hour_after_the_close_the_bar_is_read_not_refused():
    now = et("2026-10-16", 16, 31)
    feed = feed_at(now)
    assert feed.get_daily_close("MS", "2026-10-16", now=now) == 180.57
    assert feed.stock_client.requests[0]["end"] == "2026-10-16T20:15:00+00:00"     # 16:15 ET


def test_before_the_close_is_covered_nothing_is_asked():
    now = et("2026-10-16", 16, 25)                    # could only ask up to 16:09
    feed = feed_at(now)
    assert feed.get_daily_close("MS", "2026-10-16", now=now) is None
    assert feed.stock_client.requests == []


def test_a_session_long_past_is_asked_exactly_as_before():
    now = et("2026-10-19", 12, 0)
    feed = feed_at(now)
    assert feed.get_daily_close("MS", "2026-10-16", now=now) == 180.57
    sent = feed.stock_client.requests[0]
    assert (sent["start"], sent["end"]) == ("2026-10-16T00:00:00+00:00", "2026-10-17T00:00:00+00:00")


@pytest.mark.parametrize("hh,mm,readable", [(13, 25, False), (13, 27, True)])
def test_a_half_day_uses_its_1300_close(hh, mm, readable):
    now = et("2026-11-27", hh, mm)
    feed = feed_at(now)
    assert (feed.get_daily_close("MS", "2026-11-27", now=now) is not None) is readable


@pytest.mark.parametrize("day", ["2026-10-17", "2026-11-26"])      # a Saturday, Thanksgiving
def test_a_day_without_a_session_has_no_close(day):
    now = et("2026-11-30", 12, 0)
    feed = feed_at(now)
    assert feed.get_daily_close("MS", day, now=now) is None
    assert feed.stock_client.requests == []


def test_a_naive_clock_is_eastern_time_like_the_settlement_check():
    now = et("2026-10-16", 16, 31)
    feed = feed_at(now)
    assert feed.get_daily_close("MS", "2026-10-16", now=now.replace(tzinfo=None)) == 180.57
    assert feed.stock_client.requests[0]["end"] == "2026-10-16T20:15:00+00:00"


async def test_the_research_resolver_reads_the_close_on_its_own_clock(database, monkeypatch):
    """resolve_due_iv_evals(now=…) and the fetch agree on the time: half an hour
    after a half day's 13:00 close, the observation settles on that session."""
    import main
    await seed(database, horizon="2026-11-27", earnings="2026-11-25", signal="2026-11-24")
    now = et("2026-11-27", 13, 30)
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main, "feed", feed_at(now))
    assert await main.resolve_due_iv_evals("2026-11-27", now=now) == 1
    row = (await database._query("SELECT measurement_session, exit_price FROM iv_rv_evals"))[0]
    assert (row["measurement_session"], row["exit_price"]) == ("2026-11-27", 180.57)


def test_the_old_request_was_refused_at_the_same_moment():
    """The regression itself: ending at UTC midnight is still 'recent' at 16:31."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    plan = DataPlan(et("2026-10-16", 16, 31))
    start = datetime(2026, 10, 16, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError, match="recent SIP data"):
        plan.get_stock_bars(StockBarsRequest(symbol_or_symbols="MS", timeframe=TimeFrame.Day,
                                             start=start, end=start + timedelta(days=1)))

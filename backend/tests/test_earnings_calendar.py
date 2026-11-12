"""The Nasdaq earnings calendar: partial answers, refusals, restarts, stale entries.

From 2026-11-10 Nasdaq refused every request (HTTP 403). In the week before,
partial answers had replaced the whole map, shrinking it from 1,908 names to
734, and the map lived only in memory. When Nasdaq has no timing for a print due
today, the time Yahoo shows for a confirmed report fills in. Fake Nasdaq, fake
Yahoo, temporary cache file.
"""
import json
import stat
import urllib.error
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS

import pytest

from feeds import earnings_calendar as cal
from market_time import ET
from signals import earnings_scanner as scanner

TODAY = date(2026, 11, 9)                     # a Monday
T0 = 1_800_000_000.0
HOUR, DAY = 3600, 86400


def day(n):
    return (TODAY + timedelta(days=n)).isoformat()


@pytest.fixture
def nasdaq(monkeypatch):
    """Answers from `days` ({iso date: [(symbol, time)]}); a date in `refused`, or
    everything when "all" is in it, gets no answer."""
    state = NS(days={}, refused=set(), calls=[], now=T0)

    def fetch(d):
        state.calls.append(d.isoformat())
        if "all" in state.refused or d.isoformat() in state.refused:
            return None
        return [{"symbol": s, "time": t, "market_cap": 1e10}
                for s, t in state.days.get(d.isoformat(), [])]

    monkeypatch.setattr(cal, "_fetch_nasdaq_day", fetch)
    monkeypatch.setattr(cal, "et_today", lambda: TODAY)
    monkeypatch.setattr(cal, "time", NS(time=lambda: state.now, sleep=lambda _s: None))
    monkeypatch.setitem(cal._cache, "map", {})
    monkeypatch.setitem(cal._cache, "ts", 0.0)
    return state


def dates():
    return {sym: rec["date"] for sym, rec in cal._cache["map"].items()}


# ── Partial answers and refusals ────────────────────────────────────────────
def test_a_partial_answer_keeps_what_the_unanswered_days_had(nasdaq):
    nasdaq.days = {day(1): [("AAA", "time-after-hours")], day(2): [("BBB", "time-pre-market")],
                   day(3): [("CCC", ""), ("EEE", "")]}
    cal._refresh()
    nasdaq.now += 13 * HOUR
    nasdaq.refused = {day(2)}                                  # BBB's day goes unanswered
    nasdaq.days = {day(1): [("AAA", "time-after-hours")], day(4): [("DDD", "")],
                   day(8): [("CCC", "")]}                      # CCC moved; EEE dropped
    cal._refresh()
    assert dates() == {"AAA": day(1), "BBB": day(2), "CCC": day(8), "DDD": day(4)}
    assert cal._cache["map"]["BBB"]["seen"] == T0               # last listed in the first refresh
    assert cal._cache["map"]["AAA"]["seen"] == T0 + 13 * HOUR


def test_a_refusal_from_the_start_keeps_everything_and_stops_asking(nasdaq, caplog):
    nasdaq.days = {day(1): [("AAA", "time-after-hours")]}
    cal._refresh()
    before, nasdaq.calls = dict(cal._cache["map"]), []
    nasdaq.now += 13 * HOUR
    nasdaq.refused = {"all"}
    cal._ensure_fresh()
    assert len(nasdaq.calls) == cal._GIVE_UP_AFTER == 3         # not the ~42 days of the horizon
    assert cal._cache["map"] == before
    assert "got nothing from Nasdaq" in caplog.text
    nasdaq.now += HOUR - 1
    cal._ensure_fresh()
    assert len(nasdaq.calls) == 3                               # no new attempt within the hour
    nasdaq.now += 2
    cal._ensure_fresh()
    assert len(nasdaq.calls) == 6


def test_with_nothing_known_a_refusal_is_not_retried_on_every_lookup(nasdaq):
    nasdaq.refused = {"all"}
    for _ in range(50):
        assert cal.get_next_earnings("AAA") is None
    assert len(nasdaq.calls) == 3


# ── Kept across a restart ───────────────────────────────────────────────────
def restart():
    cal._cache.update(map={}, ts=0.0, loaded=False)


def test_the_map_survives_a_restart(nasdaq):
    nasdaq.days = {day(1): [("AAA", "time-after-hours")]}
    cal._refresh()
    assert stat.S_IMODE(cal._CACHE_FILE.stat().st_mode) == 0o600
    restart()
    nasdaq.refused, nasdaq.calls = {"all"}, []
    nasdaq.now += HOUR
    assert cal.get_next_earnings("AAA") == day(1)
    assert cal.get_report_time("AAA") == "post"
    assert nasdaq.calls == []                                   # loaded, still within its refresh time


@pytest.mark.parametrize("content", ["not json", '{"map": [], "ts": 1}', '{"map": {"A": 1}, "ts": 1}',
                                     '{"ts": 1}'])
def test_an_unreadable_saved_file_is_ignored(nasdaq, caplog, content):
    cal._CACHE_FILE.write_text(content)
    restart()
    nasdaq.refused = {"all"}
    assert cal.get_next_earnings("A") is None
    assert "cache ignored" in caplog.text


# ── What is still trusted ───────────────────────────────────────────────────
def test_an_entry_nasdaq_has_not_listed_for_three_days_is_not_used(nasdaq):
    nasdaq.days = {day(4): [("AAA", "time-after-hours")]}
    cal._refresh()
    nasdaq.refused = {"all"}
    nasdaq.now = T0 + 3 * DAY - 1
    assert cal.get_next_earnings("AAA") == day(4) and cal.get_report_time("AAA") == "post"
    assert [r["ticker"] for r in cal.get_upcoming_reporters(7)] == ["AAA"]
    nasdaq.now = T0 + 3 * DAY + 1
    assert cal.get_next_earnings("AAA") is None
    assert cal.get_report_time("AAA") is None
    assert cal.get_upcoming_reporters(7) == []


def test_a_date_that_has_passed_is_not_used(nasdaq, monkeypatch):
    nasdaq.days = {day(1): [("AAA", "time-after-hours")]}
    cal._refresh()
    monkeypatch.setattr(cal, "et_today", lambda: TODAY + timedelta(days=2))
    assert cal.get_next_earnings("AAA") is None
    assert cal.get_report_time("AAA") is None


def test_a_refusal_is_told_apart_from_a_day_with_no_reports(monkeypatch):
    answers = []

    class Reply:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def urlopen(req, timeout=None, context=None):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Reply(answer)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    answers[:] = [urllib.error.HTTPError("u", 403, "Forbidden", {}, None),
                  b'{"data": {"rows": null}}', b'{"data": null}',
                  b'{"data": {"rows": [{"symbol": "csco", "time": "time-after-hours"}]}}']
    assert cal._fetch_nasdaq_day(TODAY) is None
    assert cal._fetch_nasdaq_day(TODAY) == []
    assert cal._fetch_nasdaq_day(TODAY) is None
    assert cal._fetch_nasdaq_day(TODAY) == [{"symbol": "CSCO", "time": "time-after-hours",
                                             "market_cap": 0.0}]


# ── Report timing from Yahoo for a print due today ──────────────────────────
PRINT_DAY = date(2026, 11, 12)


def stamp(hour, minute=0, on=PRINT_DAY):
    return datetime(on.year, on.month, on.day, hour, minute, tzinfo=ET).timestamp()


def stock(start, end=None, estimate=False, ticker="CSCO"):
    info = {"earningsTimestampStart": start, "earningsTimestampEnd": start if end is None else end}
    if estimate is not None:
        info["isEarningsDateEstimate"] = estimate
    return NS(ticker=ticker, info=info)


@pytest.fixture
def yahoo(monkeypatch):
    monkeypatch.setattr(scanner, "et_today", lambda: PRINT_DAY)
    monkeypatch.setattr(scanner, "_RT_CACHE", {})
    monkeypatch.setattr(scanner, "_ED_CACHE", {})


@pytest.mark.parametrize("ticker_info,expected", [
    (stock(stamp(8, 30)), "pre"),                     # UBER, PEP, UNH
    (stock(stamp(16)), "post"),                       # PLTR, AMZN, ABNB, MSFT, CSCO
    (stock(stamp(4)), "pre"), (stock(stamp(9, 29)), "pre"),
    (stock(stamp(19, 59)), "post"),
    (stock(stamp(0)), None),                          # midnight: no time given
    (stock(stamp(3, 59)), None), (stock(stamp(9, 30)), None), (stock(stamp(12)), None),
    (stock(stamp(15, 59)), None), (stock(stamp(20)), None),
    (stock(stamp(16), estimate=True), None),          # an estimated date
    (stock(stamp(16), estimate=None), None),          # not said either way
    (stock(stamp(16), end=stamp(17)), None),          # a range, not a time
    (stock(stamp(16, on=date(2026, 11, 13))), None),  # another day
    (stock("16:00"), None), (stock(None), None),
])
def test_only_a_confirmed_time_outside_the_session_counts(yahoo, ticker_info, expected):
    assert scanner._announced_time(ticker_info, "2026-11-12") == expected


class Refusing:
    ticker = "CSCO"

    @property
    def info(self):
        raise RuntimeError("Yahoo timed out")


def test_a_failed_yahoo_read_means_unknown(yahoo):
    assert scanner._announced_time(Refusing(), "2026-11-12") is None


def test_yahoo_is_asked_only_for_a_print_due_today_and_then_remembered(yahoo):
    asked = []

    class Counting:
        ticker = "CSCO"

        @property
        def info(self):
            asked.append(1)
            return stock(stamp(16)).info

    assert scanner._announced_time(Counting(), "2026-11-13") is None
    assert asked == []
    assert scanner._announced_time(Counting(), "2026-11-12") == "post"
    assert scanner._announced_time(Counting(), "2026-11-12") == "post"
    assert asked == [1]


def test_nasdaq_timing_wins_and_yahoo_fills_only_a_gap(yahoo, monkeypatch):
    monkeypatch.setattr(cal, "get_next_earnings", lambda _t: "2026-11-12")
    monkeypatch.setattr(cal, "get_report_time", lambda _t: "pre")
    assert scanner._next_earnings_info(stock(stamp(16))) == ("2026-11-12", "pre")
    monkeypatch.setattr(cal, "get_report_time", lambda _t: None)
    assert scanner._next_earnings_info(stock(stamp(16))) == ("2026-11-12", "post")


def test_the_yahoo_date_fallback_gets_the_timing_too(yahoo, monkeypatch):
    monkeypatch.setattr(cal, "get_next_earnings", lambda _t: None)
    monkeypatch.setattr(scanner, "_fetch_next_earnings_date", lambda _s: "2026-11-12")
    assert scanner._next_earnings_info(stock(stamp(16))) == ("2026-11-12", "post")
    assert scanner._next_earnings_info(stock(stamp(16))) == ("2026-11-12", "post")  # date cached
    monkeypatch.setattr(scanner, "_RT_CACHE", {})
    assert scanner._next_earnings_info(stock(stamp(16), estimate=True)) == ("2026-11-12", None)

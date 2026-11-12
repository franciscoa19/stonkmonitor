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
import pandas as pd

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
    assert cal._cache["map"] == {sym: {**row, "verified": False} for sym, row in before.items()}
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


def test_a_saved_file_from_the_future_is_ignored(nasdaq, caplog):
    cal._CACHE_FILE.write_text(json.dumps({"ts": T0 + DAY, "map": {
        "AAA": {"date": day(1), "time": "time-after-hours", "seen": T0}}}))
    restart()
    nasdaq.refused = {"all"}
    assert cal.get_next_earnings("AAA") is None
    assert "cache ignored" in caplog.text


def test_a_row_listed_in_the_future_is_not_used(nasdaq):
    cal._CACHE_FILE.write_text(json.dumps({"ts": T0 - HOUR, "map": {
        "AAA": {"date": day(1), "time": "time-after-hours", "seen": T0 + DAY},
        "BBB": {"date": day(1), "time": "time-after-hours", "seen": T0 - HOUR}}}))
    restart()
    assert cal.get_next_earnings("AAA") is None
    assert cal.get_next_earnings("BBB") == day(1)


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


def test_yahoo_is_asked_again_after_an_hour_or_on_a_new_day(yahoo):
    import time

    morning = stock(stamp(8))                       # what Yahoo shows now
    scanner._RT_CACHE["CSCO"] = ("2026-11-12", ("2026-11-12", "post"),
                                 time.time() - scanner._RT_TTL - 1)
    assert scanner._announced_time(morning, "2026-11-12") == "pre"
    scanner._RT_CACHE["CSCO"] = ("2026-11-11", ("2026-11-12", "post"), time.time())
    assert scanner._announced_time(morning, "2026-11-12") == "pre"


@pytest.mark.parametrize("nasdaq_timing,yahoo_time,expected", [
    ("pre", 16, None), ("post", 8, None), (None, 16, "post"),
    ("post", 16, "post"), ("post", 12, None), ("post", None, "post"),
])
def test_fresh_nasdaq_timing_cannot_override_a_conflict(yahoo, monkeypatch,
                                                         nasdaq_timing, yahoo_time, expected):
    monkeypatch.setattr(cal, "get_earnings_record", lambda _: {
        "date": "2026-11-12", "timing": nasdaq_timing, "verified": True})
    ticker = stock(stamp(yahoo_time)) if yahoo_time is not None else Refusing()
    assert scanner._next_earnings_info(ticker) == ("2026-11-12", expected)


def test_the_yahoo_date_fallback_gets_the_timing_too(yahoo, monkeypatch):
    monkeypatch.setattr(cal, "get_earnings_record", lambda _t: None)
    monkeypatch.setattr(scanner, "_fetch_next_earnings_date", lambda _s: "2026-11-12")
    assert scanner._next_earnings_info(stock(stamp(16))) == ("2026-11-12", "post")
    assert scanner._next_earnings_info(stock(stamp(16))) == ("2026-11-12", "post")  # date cached
    monkeypatch.setattr(scanner, "_RT_CACHE", {})
    assert scanner._next_earnings_info(stock(stamp(16), estimate=True)) == ("2026-11-12", None)


@pytest.mark.parametrize("hour,allowed", [(8, False), (None, False), (16, True)])
def test_retained_post_must_be_corroborated_after_restart(nasdaq, yahoo, monkeypatch, hour, allowed):
    from signals.iv_executor import is_pre_earnings_entry_window

    session_day = PRINT_DAY.isoformat()
    nasdaq.now = stamp(10, 30)
    nasdaq.refused = {"all"}
    monkeypatch.setattr(cal, "et_today", lambda: PRINT_DAY)
    cal._CACHE_FILE.write_text(json.dumps({"ts": nasdaq.now - 2 * DAY, "map": {
        "CSCO": {"date": session_day, "time": "time-after-hours", "market_cap": 1e10,
                 "seen": nasdaq.now - 2 * DAY, "verified": True}}}))
    restart()
    ticker = stock(stamp(hour)) if hour is not None else Refusing()
    d, timing = scanner._next_earnings_info(ticker)
    assert d == session_day
    assert not cal.get_earnings_record("CSCO")["verified"]
    assert len(nasdaq.calls) == 3
    now = datetime(2026, 11, 12, 10, 30, tzinfo=ET)
    assert is_pre_earnings_entry_window(d, timing, 2, now=now) is allowed


# ── Which rows count as verified ────────────────────────────────────────────
# The `nasdaq` fixture's clock (T0) is in 2027 while its "today" is 2026-11-09,
# so no row is ever verified there. These run the clock on the print day itself.
@pytest.fixture
def print_day(nasdaq, monkeypatch):
    clock = NS(today=PRINT_DAY)
    monkeypatch.setattr(cal, "et_today", lambda: clock.today)
    nasdaq.days = {PRINT_DAY.isoformat(): [("CSCO", "time-after-hours")],
                   (PRINT_DAY + timedelta(days=1)).isoformat(): [("AAA", "time-pre-market")]}
    return clock


def verified(sym="CSCO"):
    return cal.get_earnings_record(sym)["verified"]


def test_a_refresh_this_morning_verifies_its_rows_for_the_day(nasdaq, print_day):
    nasdaq.now = stamp(7, 48)
    cal._ensure_fresh()
    nasdaq.now = stamp(15, 30)
    assert cal.get_earnings_record("CSCO") == {"date": "2026-11-12", "timing": "post",
                                               "verified": True}
    assert verified("AAA")


def test_verification_lapses_after_the_refresh_interval(nasdaq, print_day):
    nasdaq.now = stamp(7, 48)
    cal._ensure_fresh()
    nasdaq.now = stamp(7, 48) + cal._TTL - 60
    assert verified()
    cal._cache["ts"] = nasdaq.now           # no refresh due: only the row's own age counts
    nasdaq.now = stamp(7, 48) + cal._TTL + 60
    assert not verified()


def test_a_refresh_late_last_night_does_not_verify_today(nasdaq, print_day, yahoo):
    print_day.today = PRINT_DAY - timedelta(days=1)
    nasdaq.now = stamp(23, 0, on=PRINT_DAY - timedelta(days=1))
    cal._ensure_fresh()
    print_day.today = PRINT_DAY
    nasdaq.now = stamp(10, 30)              # 11.5 h later: no refresh due yet
    calls = len(nasdaq.calls)
    assert not verified()
    assert len(nasdaq.calls) == calls
    assert scanner._next_earnings_info(Refusing()) == ("2026-11-12", None)
    scanner._RT_CACHE.clear()
    assert scanner._next_earnings_info(stock(stamp(16))) == ("2026-11-12", "post")


def test_a_row_restored_from_disk_is_not_verified_even_from_this_morning(nasdaq, print_day, yahoo):
    nasdaq.now = stamp(7, 48)
    cal._ensure_fresh()
    assert verified()
    restart()
    nasdaq.now = stamp(9, 0)
    calls = len(nasdaq.calls)
    assert not verified()
    assert len(nasdaq.calls) == calls       # loaded from disk, not asked again
    assert scanner._next_earnings_info(Refusing()) == ("2026-11-12", None)
    scanner._RT_CACHE.clear()
    assert scanner._next_earnings_info(stock(stamp(16))) == ("2026-11-12", "post")


def test_rows_kept_from_an_unanswered_day_are_not_verified(nasdaq, print_day):
    nasdaq.now = stamp(7, 48)
    cal._ensure_fresh()
    nasdaq.now = stamp(8, 48)
    nasdaq.refused = {PRINT_DAY.isoformat()}
    cal._refresh()                          # CSCO's day unanswered this time, AAA's answered
    assert cal._cache["map"]["CSCO"]["seen"] == stamp(7, 48)
    assert not verified("CSCO")
    assert verified("AAA")


def test_a_verified_row_today_yields_to_a_confirmed_yahoo_date_elsewhere(yahoo, monkeypatch):
    monkeypatch.setattr(cal, "get_earnings_record", lambda _: {
        "date": "2026-11-12", "timing": "post", "verified": True})
    tomorrow = date(2026, 11, 13)
    assert scanner._next_earnings_info(stock(stamp(16, on=tomorrow))) == ("2026-11-12", None)


def test_a_retained_future_date_needs_a_confirmed_yahoo_date(yahoo, monkeypatch):
    monkeypatch.setattr(cal, "get_earnings_record", lambda _: {
        "date": "2026-11-13", "timing": "post", "verified": False})
    tomorrow = date(2026, 11, 13)
    assert scanner._next_earnings_info(stock(stamp(16, on=tomorrow), estimate=True)) == (None, None)
    scanner._RT_CACHE.clear()
    assert scanner._next_earnings_info(Refusing()) == (None, None)
    scanner._RT_CACHE.clear()
    assert scanner._next_earnings_info(stock(stamp(16, on=tomorrow))) == ("2026-11-13", "post")


def test_a_retained_future_date_cannot_hide_a_report_today(yahoo, monkeypatch):
    monkeypatch.setattr(cal, "get_earnings_record", lambda _: {
        "date": "2026-11-13", "timing": "post", "verified": False})
    assert scanner._next_earnings_info(stock(stamp(8))) == (None, None)


def test_past_yahoo_date_cache_is_refreshed(yahoo, monkeypatch):
    import time

    scanner._ED_CACHE["CSCO"] = ("2026-11-11", time.time())
    monkeypatch.setattr(cal, "get_earnings_record", lambda _: None)
    monkeypatch.setattr(scanner, "_fetch_next_earnings_date", lambda _: "2026-11-12")
    assert scanner._next_earnings_info(stock(stamp(16))) == ("2026-11-12", "post")


def test_measurement_fallback_only_fills_unanswered_days(nasdaq, monkeypatch):
    import yfinance

    nasdaq.days = {day(1): [("NASDAQ", "time-after-hours")]}
    nasdaq.refused = {day(2)}
    requests = []

    def fetch(**kwargs):
        requests.append(kwargs)
        return pd.DataFrame({
            "Event Start Date": [f"{day(3)}T00:30:00+00:00", f"{day(2)}T21:00:00+00:00",
                                 f"{day(2)}T21:00:00+00:00", f"{day(1)}T21:00:00+00:00",
                                 f"{day(2)}T21:00:00+00:00", "bad"],
            "Marketcap": [5e10, 4e10, 9e10, 9e10, float("nan"), 9e10],
            "Timing": ["AMC"] * 6,
        }, index=["YAHOO", "YAHOO", "ONWATCH", "ANSWERED", "BADCAP", "BADDATE"])

    monkeypatch.setattr(yfinance, "Calendars", lambda: NS(get_earnings_calendar=fetch))
    rows = cal.get_measurement_reporters(3, min_market_cap=1e10, limit=2, exclude={"ONWATCH"})
    assert [(r["ticker"], r["date"], r["report_time"]) for r in rows] == [
        ("NASDAQ", day(1), "time-after-hours"), ("YAHOO", day(2), "")]
    assert len(requests) == 1 and requests[0]["market_cap"] == 1e10
    assert cal.get_earnings_record("YAHOO") is None


def test_measurement_fallback_drops_companies_below_the_floor(nasdaq, monkeypatch):
    import yfinance

    nasdaq.refused = {"all"}
    frame = pd.DataFrame({"Event Start Date": [f"{day(1)}T21:00:00+00:00"] * 2,
                          "Marketcap": [5e9, 2e10]}, index=["SMALL", "LARGE"])
    monkeypatch.setattr(yfinance, "Calendars", lambda: NS(get_earnings_calendar=lambda **_: frame))
    rows = cal.get_measurement_reporters(3, min_market_cap=1e10)
    assert [r["ticker"] for r in rows] == ["LARGE"]


def test_a_date_nasdaq_answered_beats_yahoo_on_a_day_it_did_not(nasdaq, monkeypatch):
    import yfinance

    nasdaq.days = {day(1): [("DUAL", "time-after-hours")]}
    nasdaq.refused = {day(2)}
    frame = pd.DataFrame({"Event Start Date": [f"{day(2)}T21:00:00+00:00"],
                          "Marketcap": [2e10]}, index=["DUAL"])
    monkeypatch.setattr(yfinance, "Calendars", lambda: NS(get_earnings_calendar=lambda **_: frame))
    rows = cal.get_measurement_reporters(3, min_market_cap=1e9)
    assert [(r["ticker"], r["date"]) for r in rows] == [("DUAL", day(1))]


def test_measurement_paging_is_bounded_during_a_full_outage(nasdaq, monkeypatch):
    import yfinance

    nasdaq.refused = {"all"}
    offsets = []

    def fetch(**kwargs):
        offset = kwargs["offset"]
        offsets.append(offset)
        return pd.DataFrame({"Event Start Date": [f"{day(1)}T21:00:00+00:00"] * 100,
                             "Marketcap": [1e10] * 100},
                            index=[f"TEST{i}" for i in range(offset, offset + 100)])

    monkeypatch.setattr(yfinance, "Calendars", lambda: NS(get_earnings_calendar=fetch))
    rows = cal.get_measurement_reporters(3, min_market_cap=1e10, limit=150)
    assert len(rows) == 150
    assert offsets == [0, 100, 200]
    assert len(nasdaq.calls) == 3

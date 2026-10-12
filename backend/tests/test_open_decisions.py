"""Decisions at the opening bell.

On 2026-10-09 the bot bought PEP's condor back 25 seconds after the open for
$945. The stock sat inside the hold band all day and the condor would have
expired worthless for the full $1,125. Two inputs were wrong: the stock price
(the midpoint of a one-sided quote) and the option prices (opening asks).
Fake brokers and feeds, temporary ledgers; the PEP numbers are the real ones.
"""
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from pydantic import ValidationError

from feeds.alpaca_feed import AlpacaFeed, spot_from
from test_execution_safety import database, session  # noqa: F401  (fixtures)
from test_account_limits import entry, SETUP, settings
from test_condor_close_winner import Broker, book, condor, manage, the_condor, desk, LATER, SHORTS  # noqa: F401

UTC = timezone.utc
BELL = datetime(2026, 10, 9, 13, 30, tzinfo=UTC)          # 09:30:00 ET


def at(seconds):
    return (BELL + timedelta(seconds=seconds)).isoformat()


def trade(price, seconds):
    return {"price": price, "timestamp": at(seconds)}


def quote(bid, ask, seconds):
    return {"bid": bid, "ask": ask, "timestamp": at(seconds)}


def spot(trade_, quote_, seconds, **kw):
    return spot_from(trade_, quote_, now=BELL + timedelta(seconds=seconds), **kw)


# ── The stock's price ───────────────────────────────────────────────────────
def test_friday_the_quote_said_123_and_the_stock_traded_at_126(session):
    """The real prints: last trade $126.245 at 09:30:17.7, quote $120.23 x
    $126.31 at 09:30:25.0. The old reading was the quote's midpoint."""
    pep = {"short_put": 121.0, "short_call": 131.0}
    old_reading = (120.23 + 126.31) / 2
    assert old_reading == pytest.approx(123.27) and not session._condor_expiry_hold(pep, old_reading, 0.03)
    price = spot(trade(126.245, 17.7), quote(120.23, 126.31, 25.0), 25.5)
    assert price == 126.245 and session._condor_expiry_hold(pep, price, 0.03)


@pytest.mark.parametrize("bid,ask", [
    (120.23, 133.83),        # placeholder on both sides (PEP at the bell and after the close)
    (120.23, 126.31),        # placeholder bid
    (126.17, 133.83),        # placeholder ask
    (0, 126.31), (126.2, 0), (126.4, 126.2),               # one-sided, crossed
    (126.0, 126.7),          # 0.55% wide: not tight enough to call a price
    (float("nan"), 126.3), ("bad", 126.3), (None, None)])
def test_a_quote_that_is_not_a_price_is_never_averaged_into_one(bid, ask):
    stale = trade(126.2, -3600)
    assert spot(stale, quote(bid, ask, 25), 25.5) is None
    assert spot(None, quote(bid, ask, 25), 25.5) is None


def test_a_recent_trade_wins_over_any_quote():
    assert spot(trade(126.245, 17.7), quote(126.17, 126.31, 25), 25.5) == 126.245
    assert spot(trade(126.245, 17.7), None, 25.5) == 126.245
    assert spot(trade(126.245, 17.7), {}, 25.5) == 126.245


def test_without_a_recent_trade_a_tight_fresh_quote_is_a_price():
    stale = trade(125.0, -3600)
    assert spot(stale, quote(126.17, 126.31, 25), 25.5) == pytest.approx(126.24)
    assert spot(stale, quote(126.17, 126.31, -3600), 25.5) is None       # the quote is old too


@pytest.mark.parametrize("age,max_age,expected", [
    (899, 900, 126.2), (900, 900, 126.2), (901, 900, None),
    (86_400 * 3, None, 126.2),       # any age on request: the last price the market traded at
    (-5, 900, None)])                # stamped in the future: not evidence
def test_how_old_a_trade_may_be(age, max_age, expected):
    assert spot(trade(126.2, -age), None, 0, max_age_seconds=max_age) == expected


@pytest.mark.parametrize("bad", [
    None, {}, {"price": None, "timestamp": at(0)}, {"price": "bad", "timestamp": at(0)},
    {"price": 0, "timestamp": at(0)}, {"price": -1, "timestamp": at(0)},
    {"price": float("nan"), "timestamp": at(0)}, {"price": 126.2},
    {"price": 126.2, "timestamp": "not a time"},
    {"price": 126.2, "timestamp": "2026-10-09T13:30:00"}])               # no timezone: age unknowable
def test_an_unusable_trade_is_not_a_price(bad):
    assert spot(bad, None, 1) is None


def feed_with(trade_=None, quote_=None, trade_error=None):
    feed = object.__new__(AlpacaFeed)

    def latest_trade(_req):
        if trade_error:
            raise trade_error
        return {"PEP": trade_} if trade_ else {}
    feed.stock_client = NS(get_stock_latest_trade=latest_trade,
                           get_stock_latest_quote=lambda _req: {"PEP": quote_} if quote_ else {})
    return feed


def test_the_feed_reads_the_trade_print_and_falls_back_to_a_tight_quote():
    now = datetime.now(UTC)
    print_ = NS(price=126.245, timestamp=now - timedelta(seconds=8))
    placeholder = NS(bid_price=120.23, ask_price=126.31, bid_size=2, ask_size=1, timestamp=now)
    tight = NS(bid_price=126.17, ask_price=126.31, bid_size=1, ask_size=1, timestamp=now)
    assert feed_with(print_, placeholder).get_spot("pep") == 126.245
    assert feed_with(None, tight).get_spot("PEP") == pytest.approx(126.24)
    assert feed_with(None, placeholder).get_spot("PEP") is None
    assert feed_with(None, tight, trade_error=RuntimeError("offline")).get_spot("PEP") == pytest.approx(126.24)
    assert feed_with(None, None, trade_error=RuntimeError("offline")).get_spot("PEP") is None
    old = NS(price=125.0, timestamp=now - timedelta(days=2))
    assert feed_with(old, placeholder).get_spot("PEP") is None
    assert feed_with(old, placeholder).get_spot("PEP", max_age_seconds=None) == 125.0


# ── Hold or close, on expiry day ────────────────────────────────────────────
TODAY = "2026-11-27"                  # the session fixture's date; the test condor is 90 / 110


def priced(main, monkeypatch, value):
    seen = []

    def get_spot(ticker, *a, **kw):
        seen.append(ticker)
        return value
    monkeypatch.setattr(main, "feed", NS(get_spot=get_spot,
        get_latest_quote=lambda _t: pytest.fail("the hold rule must not read a quote midpoint")))
    return seen


@pytest.mark.parametrize("price,closes", [
    (100.0, False), (93.0, False), (106.5, False),          # 3% or more inside both strikes: hold
    (92.0, True), (107.5, True), (111.0, True)])            # too close to a strike, or through it
async def test_the_hold_decision_follows_the_traded_price(database, desk, monkeypatch, caplog, price, closes):
    await condor(database, expiry=TODAY)
    broker = Broker()
    main = desk(broker)
    assert priced(main, monkeypatch, price) == []
    with caplog.at_level(logging.INFO, logger="main"):
        await manage(main, database)
    assert bool(broker.sent) is closes
    assert any("holding to expiry" in r.message for r in caplog.records) is not closes


async def test_with_no_reliable_price_the_decision_waits(database, desk, monkeypatch, caplog):
    await condor(database, expiry=TODAY)
    broker = Broker()
    main = desk(broker)
    priced(main, monkeypatch, None)
    # The fixture's date is a 13:00 early close, so 12:30 is already past its
    # forced-close deadline; this case is about the hours before it.
    monkeypatch.setattr(main, "_expiry_force_close_due", lambda *a, **kw: False)
    with caplog.at_level(logging.INFO, logger="main"):
        await manage(main, database)
        await manage(main, database)
    assert broker.sent == [] and (await the_condor(database))["status"] == "open"
    assert sum("no reliable stock price yet" in r.message for r in caplog.records) == 2


async def test_at_the_expiry_deadline_it_closes_even_without_a_price(database, desk, monkeypatch):
    await condor(database, expiry=TODAY)
    broker = Broker()
    main = desk(broker)
    priced(main, monkeypatch, None)
    monkeypatch.setattr(main, "_expiry_force_close_due", lambda *a, **kw: True)
    await manage(main, database)
    assert len(broker.sent) == 1 and broker.sent[0]["legs"] == SHORTS


# ── The first minutes after the bell ────────────────────────────────────────
def clock(monkeypatch, hour, minute, second=0, day=27):
    import market_time
    now = datetime(2026, 11, day, hour, minute, second, tzinfo=market_time.ET)
    monkeypatch.setattr(market_time, "et_today", lambda *a: now.date())
    monkeypatch.setattr(market_time, "et_now", lambda *a: now)


@pytest.mark.parametrize("hour,minute,second,settling", [
    (9, 29, 59, False), (9, 30, 0, True), (9, 30, 25, True), (9, 44, 59, True),
    (9, 45, 0, False), (12, 30, 0, False), (15, 59, 0, False), (4, 0, 0, False)])
def test_the_settling_window_is_the_first_fifteen_minutes(session, monkeypatch, hour, minute, second, settling):
    monkeypatch.setattr(session, "settings", settings())
    clock(monkeypatch, hour, minute, second)
    assert session.opening_quotes_settling() is settling


@pytest.mark.parametrize("minutes,at_0931,at_0950", [(0, False, False), (15, True, False), (30, True, True)])
def test_the_window_is_a_setting(session, monkeypatch, minutes, at_0931, at_0950):
    monkeypatch.setattr(session, "settings", settings(iv_exec_open_delay_minutes=minutes))
    clock(monkeypatch, 9, 31)
    assert session.opening_quotes_settling() is at_0931
    clock(monkeypatch, 9, 50)
    assert session.opening_quotes_settling() is at_0950


@pytest.mark.parametrize("bad", [-1, 181, 2.5, "soon"])
def test_a_nonsense_window_is_refused_at_startup(bad):
    with pytest.raises(ValidationError):
        settings(iv_exec_open_delay_minutes=bad)


async def test_a_close_by_choice_waits_for_the_book_to_settle(database, desk, monkeypatch):
    await condor(database, expiry=LATER)                    # after the print: wants to close
    broker = Broker()
    main = desk(broker)
    monkeypatch.setattr(main, "settings", settings())
    clock(monkeypatch, 9, 30, 25)
    await manage(main, database)
    assert broker.sent == [] and (await the_condor(database))["status"] == "open"
    clock(monkeypatch, 9, 44, 59)
    await manage(main, database)
    assert broker.sent == []
    clock(monkeypatch, 9, 45)
    await manage(main, database)
    assert len(broker.sent) == 1


async def test_a_forced_close_is_never_delayed(database, desk, monkeypatch):
    await condor(database, expiry=TODAY)
    broker = Broker()
    main = desk(broker)
    monkeypatch.setattr(main, "settings", settings())
    monkeypatch.setattr(main, "_condor_expiry_hold", lambda *a, **kw: False)
    monkeypatch.setattr(main, "_expiry_force_close_due", lambda *a, **kw: True)
    clock(monkeypatch, 9, 31)
    await manage(main, database)
    assert len(broker.sent) == 1


async def test_a_new_condor_is_not_opened_on_opening_quotes(database, session, monkeypatch):
    seen = entry(session, database, monkeypatch)
    clock(monkeypatch, 9, 35)
    await session.maybe_execute_condor(SETUP)
    assert seen.sized == seen.orders == [] and seen.reads == []        # not even a broker read
    clock(monkeypatch, 9, 45)
    await session.maybe_execute_condor(SETUP)
    assert seen.orders == [5]


async def test_with_the_window_off_entries_and_closes_behave_as_before(database, desk, session, monkeypatch):
    await condor(database, expiry=LATER)
    broker = Broker()
    main = desk(broker)
    monkeypatch.setattr(main, "settings", settings(iv_exec_open_delay_minutes=0))
    clock(monkeypatch, 9, 30, 25)
    await manage(main, database)
    assert len(broker.sent) == 1


# ── Friday, replayed ────────────────────────────────────────────────────────
async def test_friday_replayed_the_condor_is_held_not_bought_back(database, desk, monkeypatch, caplog):
    """Expiry day. At 09:30:25 the short legs are asked $0.40 and $0.23 and the
    stock quote is one-sided. The bot sent a $945 close. Now: nothing at the
    bell, and once the book has settled the traded price says hold."""
    await condor(database, expiry=TODAY)
    broker = Broker(book(sc=(0.01, 0.40), sp=(0.01, 0.23)))
    main = desk(broker)
    monkeypatch.setattr(main, "settings", settings())
    reads = priced(main, monkeypatch, None)                 # 09:30:25: no price worth the name
    clock(monkeypatch, 9, 30, 25)
    with caplog.at_level(logging.INFO, logger="main"):
        await manage(main, database)
        assert broker.sent == [] and reads == []            # not even consulted yet
        clock(monkeypatch, 9, 45)
        priced(main, monkeypatch, 100.0)                    # trading mid-range, as PEP was all day
        broker.quotes = book()                              # the legs back to pennies
        await manage(main, database)
    assert broker.sent == [] and (await the_condor(database))["status"] == "open"
    assert any("holding to expiry" in r.message for r in caplog.records)


# ── Research resolves from its historical measurement session ──────────────
async def test_a_realized_move_is_measured_from_the_session_close_not_a_placeholder_quote(database, session, monkeypatch):
    monkeypatch.setattr(session, "db", database)
    await database.record_iv_eval("PEP", "SELL_PREMIUM", 1.5, 3.4, 123.73, "2026-10-10", "2026-10-08")
    asked = []

    def get_daily_close(ticker, session_date):
        asked.append((ticker, session_date))
        return 125.97                                       # Friday's close; the quote midpoint said 127.03
    monkeypatch.setattr(session, "feed", NS(get_daily_close=get_daily_close,
        get_latest_quote=lambda _t: pytest.fail("a quote midpoint is not a measurement")))
    assert await session.resolve_due_iv_evals("2026-10-11") == 1
    assert asked == [("PEP", "2026-10-09")]               # Saturday's horizon uses Friday's close
    row = (await database._query("SELECT * FROM iv_rv_evals"))[0]
    assert row["resolved"] == 1 and row["exit_price"] == 125.97
    assert row["realized_move_pct"] == pytest.approx(abs(125.97 / 123.73 - 1) * 100, abs=0.01)


async def test_with_no_historical_close_the_eval_stays_due(database, session, monkeypatch):
    monkeypatch.setattr(session, "db", database)
    await database.record_iv_eval("PEP", "SELL_PREMIUM", 1.5, 3.4, 123.73, "2026-10-10", "2026-10-08")
    monkeypatch.setattr(session, "feed", NS(get_daily_close=lambda *a, **kw: None))
    assert await session.resolve_due_iv_evals("2026-10-11") == 0
    assert len(await database.get_due_iv_evals("2026-10-11")) == 1

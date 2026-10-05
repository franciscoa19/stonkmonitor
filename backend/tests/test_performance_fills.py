"""Actual execution chronology and short-cover regressions; no broker/network."""
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlparse

import pytest

from db import Database
from test_eval_loop import db, seed_entry, seed_exit
from trading.alpaca_trader import AlpacaTrader
from trading.performance import sync_trade_performance


def fill(activity_id, order_id, side, qty, price, at, symbol="AAPL"):
    return {"id": activity_id, "order_id": order_id, "activity_type": "FILL",
            "symbol": symbol, "side": side, "qty": str(qty), "price": str(price),
            "transaction_time": at}


async def row(db, order_id):
    return (await db._query("SELECT * FROM trade_performance WHERE alpaca_order_id=?", (order_id,)))[0]


async def test_canceled_partial_exit_uses_execution_time_not_placement(db):
    await seed_entry(db, "entry", "AAPL", "AAPL", 100, 10, trade_type="equity",
                     submitted_at="2026-09-03T13:30:00Z")
    await db.upsert_trade_performance(alpaca_order_id="entry", filled_at="2026-09-03T13:40:00Z")
    await db.upsert_trade_performance(
        alpaca_order_id="exit", symbol="AAPL", ticker="AAPL", side="sell", qty=5,
        filled_qty=3, filled_avg_price=110, order_status="canceled",
        submitted_at="2026-09-03T13:35:00Z", trade_type="equity")
    events = [fill("b1", "entry", "buy", 5, 100, "2026-09-03T13:31:00Z"),
              fill("b2", "entry", "buy", 5, 100, "2026-09-03T13:40:00Z"),
              fill("s1", "exit", "sell", 3, 110, "2026-09-03T13:45:00Z")]
    await db.record_trade_fills(list(reversed(events)))
    assert await db.reconcile_trades(require_fills=True) == 1
    entry = await row(db, "entry")
    assert (entry["realized_pnl"], entry["realized_pnl_pct"], entry["hold_minutes"]) == (30, 10, 14)
    await db.record_trade_fills(events + events)
    assert await db.reconcile_trades(require_fills=True) == 0
    assert len(await db._query("SELECT * FROM trade_fills")) == 3


async def test_late_buy_fill_does_not_reprice_shares_already_sold(db):
    await seed_entry(db, "entry", "AAPL", "AAPL", 100, 5, trade_type="equity",
                     submitted_at="2026-09-03T13:30:00Z")
    await seed_exit(db, "exit", "AAPL", "AAPL", 110, 3, trade_type="equity",
                    filled_at="2026-09-03T13:35:00Z")
    events = [fill("b1", "entry", "buy", 5, 100, "2026-09-03T13:30:00Z"),
              fill("s1", "exit", "sell", 3, 110, "2026-09-03T13:35:00Z")]
    await db.record_trade_fills(events)
    await db.reconcile_trades(require_fills=True)
    assert (await row(db, "entry"))["realized_pnl"] == 30
    await db.upsert_trade_performance(alpaca_order_id="entry", filled_qty=10, filled_avg_price=110,
                                      filled_at="2026-09-03T13:40:00Z")
    await db.record_trade_fills([fill("b2", "entry", "buy", 5, 120, "2026-09-03T13:40:00Z")])
    assert await db.reconcile_trades(require_fills=True) == 0
    assert (await row(db, "entry"))["realized_pnl"] == 30


@pytest.mark.parametrize("use_activities", [False, True])
async def test_short_cover_cannot_take_profit_from_a_later_long(db, use_activities):
    times = [f"2026-09-03T{hour}:00:00Z" for hour in ("13", "14", "15", "16")]
    await seed_exit(db, "short", "AAPL", "AAPL", 100, 10, filled_at=times[0], trade_type="equity")
    await seed_entry(db, "cover", "AAPL", "AAPL", 90, 10, submitted_at=times[1], trade_type="equity")
    await seed_entry(db, "long", "AAPL", "AAPL", 120, 10, submitted_at=times[2], trade_type="equity")
    await seed_exit(db, "exit", "AAPL", "AAPL", 130, 10, filled_at=times[3], trade_type="equity")
    await db._exec("UPDATE trade_performance SET realized_pnl=400, exit_reason='closed_win' WHERE alpaca_order_id='cover'")
    if use_activities:
        await db.record_trade_fills([fill(o, o, side, 10, price, t) for o, side, price, t in zip(
            ("short", "cover", "long", "exit"), ("sell", "buy", "buy", "sell"), (100, 90, 120, 130), times)])
    assert await db.reconcile_trades(require_fills=use_activities) == 2
    assert (await row(db, "cover"))["realized_pnl"] is None
    assert (await row(db, "long"))["realized_pnl"] == 100
    assert (await db.get_performance_summary())["total_pnl"] == 100
    assert await db.reconcile_trades(require_fills=use_activities) == 0


async def test_buy_crossing_flat_creates_only_the_excess_long_qty(db):
    await seed_exit(db, "short", "AAPL", "AAPL", 100, 10, filled_at="2026-09-03T13:00:00Z", trade_type="equity")
    await seed_entry(db, "cover_and_long", "AAPL", "AAPL", 90, 15, submitted_at="2026-09-03T14:00:00Z", trade_type="equity")
    await seed_exit(db, "exit", "AAPL", "AAPL", 110, 5, filled_at="2026-09-03T15:00:00Z", trade_type="equity")
    await db.reconcile_trades()
    assert (await row(db, "cover_and_long"))["realized_pnl"] == 100


async def test_sell_crossing_flat_reserves_its_short_remainder(db):
    for order_id, side, qty, price, hour in [("entry", "buy", 5, 100, "13"),
            ("exit_and_short", "sell", 8, 110, "14"), ("cover", "buy", 3, 90, "15"),
            ("long", "buy", 2, 120, "16"), ("exit", "sell", 2, 130, "17")]:
        if side == "buy":
            await seed_entry(db, order_id, "AAPL", "AAPL", price, qty, submitted_at=f"2026-09-03T{hour}:00:00Z", trade_type="equity")
        else:
            await seed_exit(db, order_id, "AAPL", "AAPL", price, qty, filled_at=f"2026-09-03T{hour}:00:00Z", trade_type="equity")
    await db.reconcile_trades()
    assert [(await row(db, o))["realized_pnl"] for o in ("entry", "cover", "long")] == [50, None, 20]


async def test_fill_times_with_different_offsets_sort_in_utc_and_options_use_100(db):
    symbol = "AAPL260918C00100000"
    await seed_entry(db, "entry", symbol, "AAPL", 2, 2)
    await seed_exit(db, "exit", symbol, "AAPL", 3, 1)
    await db.record_trade_fills([
        fill("s1", "exit", "sell", 1, 3, "2026-09-03T14:35:00Z", symbol),
        fill("b1", "entry", "buy", 2, 2, "2026-09-03T10:30:00-04:00", symbol)])
    await db.reconcile_trades(require_fills=True)
    assert (await row(db, "entry"))["realized_pnl"] == 100


async def test_incomplete_activities_preserve_last_pnl_then_recover(db):
    await seed_entry(db, "entry", "AAPL", "AAPL", 100, 10, trade_type="equity")
    await seed_exit(db, "exit", "AAPL", "AAPL", 110, 10, trade_type="equity")
    await db.reconcile_trades()
    previous = await row(db, "entry")
    await db.record_trade_fills([fill("b1", "entry", "buy", 5, 100, "2026-09-03T14:30:00Z")])
    assert await db.reconcile_trades(require_fills=True) == 0
    assert await row(db, "entry") == previous
    await db.record_trade_fills([fill("b2", "entry", "buy", 5, 100, "2026-09-03T14:30:00Z"),
                                fill("s1", "exit", "sell", 10, 110, "2026-09-03T15:30:00Z")])
    assert await db.reconcile_trades(require_fills=True) == 0


async def test_partial_without_activities_never_uses_submission_time(db):
    await seed_entry(db, "entry", "AAPL", "AAPL", 100, 10, trade_type="equity")
    await db.upsert_trade_performance(alpaca_order_id="exit", symbol="AAPL", ticker="AAPL", side="sell",
        qty=10, filled_qty=3, filled_avg_price=110, order_status="canceled", submitted_at="2026-09-03T15:30:00Z")
    assert await db.reconcile_trades() == 0
    assert (await row(db, "entry"))["realized_pnl"] is None


@pytest.mark.parametrize("field,value", [("qty", "nan"), ("price", "inf"), ("qty", "0"),
                                       ("transaction_time", "bad"), ("side", "invalid"), ("id", "")])
async def test_invalid_fill_batch_is_not_partially_persisted(db, field, value):
    good = fill("good", "entry", "buy", 1, 100, "2026-09-03T14:30:00Z")
    bad = {**good, "id": "bad", field: value}
    with pytest.raises(ValueError):
        await db.record_trade_fills([good, bad])
    assert await db._query("SELECT * FROM trade_fills") == []


async def test_fill_write_failure_rolls_back_earlier_executions(db):
    import sqlite3
    await db._exec("""CREATE TRIGGER fail_fill BEFORE INSERT ON trade_fills
                     WHEN NEW.activity_id='bad' BEGIN SELECT RAISE(ABORT, 'disk failure'); END""", strict=True)
    good = fill("good", "entry", "buy", 1, 100, "2026-09-03T14:30:00Z")
    with pytest.raises(sqlite3.IntegrityError):
        await db.record_trade_fills([good, {**good, "id": "bad"}])
    assert await db._query("SELECT * FROM trade_fills") == []


def fake_broker(*, closed=None, active=None, activities=None, raw=None, leg_ids=()):
    calls = NS(raw=[], after=[])

    def get_raw(order_id):
        calls.raw.append(order_id)
        return (raw or {}).get(order_id, {"error": "unavailable"})

    def get_fills(after=None):
        calls.after.append(after)
        return activities

    return NS(get_order_history=lambda **kw: closed or [], get_orders=lambda **kw: active or [],
              get_fill_activities=get_fills, get_order_raw=get_raw,
              get_mleg_leg_order_ids=lambda: None if leg_ids is None else set(leg_ids)), calls


def raw_order(order_id, side, executed_qty, price, at, **extra):
    return {"id": order_id, "symbol": "AAPL", "side": side, "qty": str(executed_qty),
            "filled_qty": str(executed_qty), "filled_avg_price": str(price), "type": "limit",
            "status": "filled", "created_at": at, "filled_at": at, **extra}


async def test_sync_hydrates_an_active_partial_and_an_exit_missing_from_history(db):
    buy = raw_order("entry", "buy", 5, 100, "2026-09-03T14:30:00Z", qty="10", status="partially_filled", filled_at=None)
    sell = raw_order("exit", "sell", 3, 110, "2026-09-03T15:30:00Z", qty="5", status="canceled", filled_at=None)
    events = [fill("b1", "entry", "buy", 5, 100, "2026-09-03T14:30:00Z"),
              fill("s1", "exit", "sell", 3, 110, "2026-09-03T15:30:00Z")]
    broker, calls = fake_broker(active=[AlpacaTrader._map_rest_order(buy)], activities=events, raw={"exit": sell})
    assert await sync_trade_performance(db, broker) == 1
    assert calls.raw == ["exit"] and calls.after == [None]
    assert (await row(db, "entry"))["realized_pnl"] == 30
    assert (await row(db, "exit"))["filled_at"] in (None, "")
    # Completed backfill changes to a recent overlapping window, without
    # dropping executions already persisted outside that window.
    broker.get_fill_activities = lambda **kw: []
    assert await sync_trade_performance(db, broker) == 0
    assert await db.get_trade_fill_sync_start() is not None


async def test_sync_backfills_a_short_older_than_the_order_history_window(db):
    short = raw_order("short", "sell", 10, 100, "2025-01-03T14:30:00Z")
    cover = raw_order("cover", "buy", 10, 90, "2026-09-03T14:30:00Z")
    long = raw_order("long", "buy", 10, 120, "2026-09-03T15:30:00Z")
    exit = raw_order("exit", "sell", 10, 130, "2026-09-03T16:30:00Z")
    events = [fill(o["id"], o["id"], o["side"], 10, o["filled_avg_price"], o["filled_at"])
              for o in (short, cover, long, exit)]
    broker, calls = fake_broker(closed=[AlpacaTrader._map_rest_order(o) for o in (cover, long, exit)],
                               activities=events, raw={"short": short})
    assert await sync_trade_performance(db, broker) == 1
    assert calls.after == [None] and calls.raw == ["short"]
    assert (await row(db, "cover"))["realized_pnl"] is None
    assert (await row(db, "long"))["realized_pnl"] == 100
    # Later incremental pages omit the old short; persisted inventory must
    # still prevent its cover from becoming a long lot after a restart/sync.
    broker.get_fill_activities = lambda **kw: events[1:]
    assert await sync_trade_performance(db, broker) == 0
    assert (await row(db, "cover"))["realized_pnl"] is None
    assert (await row(db, "long"))["realized_pnl"] == 100


async def test_failed_activity_fetch_does_not_change_pnl_or_advance_watermark(db):
    await seed_entry(db, "entry", "AAPL", "AAPL", 100, 10, trade_type="equity")
    await seed_exit(db, "exit", "AAPL", "AAPL", 110, 10, trade_type="equity")
    await db.reconcile_trades()
    before = await row(db, "entry")
    broker, _ = fake_broker(activities=None)
    with pytest.raises(RuntimeError, match="fill history unavailable"):
        await sync_trade_performance(db, broker)
    assert await row(db, "entry") == before
    assert await db.get_trade_fill_sync_start() is None


async def test_sync_skips_multi_leg_parent_activities(db):
    event = fill("m1", "parent", "sell", 1, 1, "2026-09-03T14:30:00Z")
    broker, calls = fake_broker(activities=[event, {**event, "id": "m2"}],
                               raw={"parent": {"id": "parent", "order_class": "mleg"}})
    assert await sync_trade_performance(db, broker) == 0
    assert calls.raw == ["parent"] and await db.get_performance_orders() == []
    assert await db._query("SELECT * FROM trade_fills") == []


async def test_missing_activity_order_aborts_before_persist_or_watermark(db):
    broker, _ = fake_broker(activities=[fill("f1", "missing", "buy", 1, 100, "2026-09-03T14:30:00Z")])
    with pytest.raises(RuntimeError, match="could not be retrieved"):
        await sync_trade_performance(db, broker)
    assert await db._query("SELECT * FROM trade_fills") == []
    assert await db.get_trade_fill_sync_start() is None


async def test_late_activity_gap_moves_sync_start_back_to_old_order(db):
    await seed_entry(db, "entry", "AAPL", "AAPL", 100, 10, submitted_at="2025-01-03T14:30:00Z", trade_type="equity")
    await db.mark_trade_fill_sync("2026-10-05T12:00:00+00:00")
    assert await db.get_trade_fill_sync_start() == "2025-01-02T00:00:00+00:00"


async def test_fill_ledger_migrates_and_survives_restart(tmp_path):
    path = tmp_path / "legacy.db"
    original = Database(path)
    await original.connect()
    await seed_entry(original, "entry", "AAPL", "AAPL", 100, 10, trade_type="equity")
    await original._exec("DROP TABLE trade_fills")
    await original.close()
    migrated = Database(path)
    await migrated.connect()
    event = fill("b1", "entry", "buy", 10, 100, "2026-09-03T14:30:00Z")
    await migrated.record_trade_fills([event])
    await migrated.mark_trade_fill_sync("2026-10-05T12:00:00+00:00")
    await migrated.close()
    reopened = Database(path)
    await reopened.connect()
    try:
        await reopened.record_trade_fills([event])
        assert len(await reopened._query("SELECT * FROM trade_fills")) == 1
        assert await reopened.get_trade_fill_sync_start() == "2026-10-03T00:00:00+00:00"
    finally:
        await reopened.close()


def test_fill_activity_pagination_uses_the_last_activity_id():
    trader = AlpacaTrader.__new__(AlpacaTrader)
    trader._trade_base = "https://paper-api.alpaca.markets"
    pages = [[{"id": str(i), "activity_type": "FILL"} for i in range(100)],
             [{"id": "100", "activity_type": "FILL"}]]
    queries = []

    def rest(method, url):
        queries.append(parse_qs(urlparse(url).query))
        return 200, pages.pop(0)

    trader._rest = rest
    assert len(trader.get_fill_activities("2026-09-01T00:00:00+00:00")) == 101
    assert queries[1]["page_token"] == ["99"]
    assert queries[0]["activity_types"] == ["FILL"] and queries[0]["direction"] == ["asc"]
    assert queries[1]["after"] == ["2026-09-01T00:00:00+00:00"]


@pytest.mark.parametrize("code,body", [(503, []), (200, {}), (200, [None]), (200, [{"activity_type": "DIV"}])])
def test_fill_activity_failure_is_distinct_from_empty_history(code, body):
    trader = AlpacaTrader.__new__(AlpacaTrader)
    trader._trade_base = "https://paper-api.alpaca.markets"
    trader._rest = lambda *args: (code, body)
    assert trader.get_fill_activities() is None


def test_repeated_activity_page_is_not_accepted_as_complete():
    trader = AlpacaTrader.__new__(AlpacaTrader)
    trader._trade_base = "https://paper-api.alpaca.markets"
    page = [{"id": str(i), "activity_type": "FILL"} for i in range(100)]
    trader._rest = lambda *args: (200, page)
    assert trader.get_fill_activities() is None


def test_activity_page_limit_does_not_return_a_truncated_ledger():
    trader = AlpacaTrader.__new__(AlpacaTrader)
    trader._trade_base = "https://paper-api.alpaca.markets"
    calls = []

    def rest(*args):
        calls.append(True)
        return 200, [{"id": f"{len(calls)}-{i}", "activity_type": "FILL"} for i in range(100)]

    trader._rest = rest
    assert trader.get_fill_activities() is None and len(calls) == 100


# ── Multi-leg legs and short-sale sides, as the live broker reports them ──────
async def test_condor_leg_fills_are_skipped_without_fetching_the_leg_order(db):
    """Paper account, 2026-10-05: 44 of 46 fills were condor legs, and
    GET /v2/orders/{leg id} answers 404. Looking each leg up aborted the whole
    sync, so nothing would ever have been reconciled."""
    opt = "NKE261002C00039500"
    legs = [fill(f"l{i}", f"leg-{i}", side, 20, 1.0, "2026-09-29T14:13:10Z", symbol=opt)
            for i, side in enumerate(("sell_short", "buy", "buy", "sell_short"))]
    cover = {**raw_order("single", "buy", 20, 0.01, "2026-10-02T16:04:08Z"), "symbol": opt}
    events = legs + [fill("c1", "single", "buy", 20, 0.01, "2026-10-02T16:04:08Z", symbol=opt)]
    # `raw` is left empty: a leg lookup returns an error, exactly as the broker does.
    broker, calls = fake_broker(closed=[AlpacaTrader._map_rest_order(cover)], activities=events,
                                leg_ids={f"leg-{i}" for i in range(4)})
    assert await sync_trade_performance(db, broker) == 0
    assert calls.raw == []                                   # no leg was looked up
    stored = await db._query("SELECT alpaca_order_id FROM trade_fills")
    assert [r["alpaca_order_id"] for r in stored] == ["single"]
    assert [o["alpaca_order_id"] for o in await db.get_performance_orders()] == ["single"]
    assert await db.get_trade_fill_sync_start() is not None  # the sync completed


async def test_unreadable_multi_leg_listing_defers_the_sync(db):
    event = fill("f1", "entry", "buy", 1, 100, "2026-09-03T14:30:00Z")
    order = AlpacaTrader._map_rest_order(raw_order("entry", "buy", 1, 100, "2026-09-03T14:30:00Z"))
    broker, _ = fake_broker(closed=[order], activities=[event], leg_ids=None)
    with pytest.raises(RuntimeError, match="Multi-leg order listing"):
        await sync_trade_performance(db, broker)
    assert await db._query("SELECT * FROM trade_fills") == []
    assert await db.get_trade_fill_sync_start() is None


async def test_single_leg_short_sale_reported_as_sell_short_is_ledgered_as_a_sell(db):
    """The order's side is "sell" but its executions say "sell_short". Rejected
    as an invalid side, one manual short would have blocked every later sync."""
    short = raw_order("short", "sell", 10, 100, "2026-09-03T14:30:00Z")
    cover = raw_order("cover", "buy", 10, 90, "2026-09-03T15:30:00Z")
    long = raw_order("long", "buy", 10, 120, "2026-09-04T14:30:00Z")
    exit_ = raw_order("exit", "sell", 10, 130, "2026-09-04T15:30:00Z")
    sides = {"short": "sell_short", "cover": "buy", "long": "buy", "exit": "sell"}
    events = [fill(o["id"], o["id"], sides[o["id"]], 10, o["filled_avg_price"], o["filled_at"])
              for o in (short, cover, long, exit_)]
    broker, _ = fake_broker(closed=[AlpacaTrader._map_rest_order(o) for o in (short, cover, long, exit_)],
                            activities=events)
    assert await sync_trade_performance(db, broker) == 1
    stored = await db._query("SELECT alpaca_order_id, side FROM trade_fills ORDER BY executed_at")
    assert [(r["alpaca_order_id"], r["side"]) for r in stored] == [
        ("short", "sell"), ("cover", "buy"), ("long", "buy"), ("exit", "sell")]
    assert (await row(db, "cover"))["realized_pnl"] is None   # it closed the short, not a long
    assert (await row(db, "long"))["realized_pnl"] == 100


def leg_listing_trader(pages):
    trader = AlpacaTrader.__new__(AlpacaTrader)
    trader._trade_base = "https://paper-api.alpaca.markets"
    queries = []

    def rest(method, url):
        queries.append(parse_qs(urlparse(url).query))
        return pages.pop(0)

    trader._rest = rest
    return trader, queries


def test_mleg_leg_ids_come_from_nested_parents_and_follow_pages():
    parent = lambda n, at: {"id": f"p{n}", "order_class": "mleg", "submitted_at": at,
                            "legs": [{"id": f"p{n}-leg{i}"} for i in range(4)]}
    simple = lambda n, at: {"id": f"s{n}", "order_class": "", "submitted_at": at, "legs": None}
    first = [simple(i, f"2026-09-01T10:00:{i % 60:02d}Z") for i in range(499)] + [parent(1, "2026-09-02T10:00:00Z")]
    second = [parent(2, "2026-09-03T10:00:00Z"), simple(999, "2026-09-03T11:00:00Z")]
    trader, queries = leg_listing_trader([(200, first), (200, second)])
    assert trader.get_mleg_leg_order_ids() == {f"p{n}-leg{i}" for n in (1, 2) for i in range(4)}
    assert queries[0]["nested"] == ["true"] and queries[0]["status"] == ["all"] and "after" not in queries[0]
    assert queries[1]["after"] == ["2026-09-02T10:00:00Z"]


@pytest.mark.parametrize("page", [
    (500, {"message": "error"}),                                   # HTTP failure
    (200, {"not": "a list"}),                                      # malformed body
    (200, [{"id": "p", "order_class": "mleg", "legs": None}]),     # parent with unreadable legs
    (200, [{"id": "p", "order_class": "mleg", "legs": [{"symbol": "X"}]}]),
])
def test_mleg_leg_listing_failure_is_distinct_from_no_multi_leg_orders(page):
    trader, _ = leg_listing_trader([page])
    assert trader.get_mleg_leg_order_ids() is None
    empty, _ = leg_listing_trader([(200, [])])
    assert empty.get_mleg_leg_order_ids() == set()


def test_mleg_leg_listing_that_cannot_advance_is_not_accepted_as_complete():
    full = [{"id": str(i), "order_class": "", "submitted_at": "2026-09-01T10:00:00Z"} for i in range(500)]
    trader, _ = leg_listing_trader([(200, list(full)), (200, list(full))])
    assert trader.get_mleg_leg_order_ids() is None

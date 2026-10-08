"""Reporting and cohort regressions. Temporary ledgers and fake brokers only."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from daily_report import build_report_data, render_html
from db import Database
from market_time import et_today
from signals.earnings_scanner import EarningsSetup, is_sell_eligible, passes_all_sell_gates
from trading.alpaca_trader import AlpacaTrader
from trading.performance import sync_trade_performance
from test_eval_loop import db, FakeTrader, seed_entry, seed_exit  # noqa: F401
from test_performance_fills import fake_broker, fill, raw_order, row


@pytest.mark.parametrize("intent", ["buy_to_open", "buy_to_close", "sell_to_open", "sell_to_close", None])
def test_order_adapter_preserves_broker_intent(intent):
    order = raw_order("order", "buy", 1, 1, "2026-10-01T14:00:00Z", position_intent=intent)
    assert AlpacaTrader._map_rest_order(order)["position_intent"] == intent


async def test_legacy_orphan_condor_cover_is_reclassified_without_losing_its_fill(db):
    symbol = "NKE261002C00039500"
    at = datetime.now(timezone.utc).isoformat()
    # An existing ledger row lacks the short entry because it was a multi-leg fill.
    await seed_entry(db, "cover", symbol, "NKE", .01, 20, submitted_at=at)
    await db.reconcile_trades()
    assert (await db.get_performance_summary())["open_trades"] == 1
    cover = raw_order("cover", "buy", 20, .01, at, symbol=symbol, position_intent="buy_to_close")
    events = [fill("cover-fill", "cover", "buy", 20, .01, at, symbol=symbol)]
    broker, _ = fake_broker(closed=[AlpacaTrader._map_rest_order(cover)], activities=events)
    await sync_trade_performance(db, broker)
    saved = await row(db, "cover")
    assert (saved["position_intent"], saved["long_entry_qty"], saved["open_qty"]) == ("buy_to_close", 0, 0)
    assert saved["realized_pnl"] is None
    summary = await db.get_performance_summary()
    assert summary["total_trades"] == summary["open_trades"] == summary["closed_trades"] == 0
    data = await build_report_data(db, FakeTrader())
    assert data["metrics"]["trades_7d"] == data["metrics"]["closed_trades"] == 0
    assert len(await db._query("SELECT * FROM trade_fills")) == 1
    assert await sync_trade_performance(db, broker) == 0
    replayed = await row(db, "cover")
    assert {k:v for k,v in replayed.items() if k != "updated_at"} == {
        k:v for k,v in saved.items() if k != "updated_at"}


async def test_cover_does_not_acquire_profit_from_a_later_option_round_trip(db):
    symbol = "NKE261002C00039500"
    specs = [("cover", "buy", 20, .01, "buy_to_close", "13"),
             ("entry", "buy", 2, 1, "buy_to_open", "14"),
             ("exit", "sell", 2, 1.5, "sell_to_close", "15")]
    orders = [raw_order(o, side, qty, price, f"2026-10-01T{hour}:00:00Z",
                        symbol=symbol, position_intent=intent)
              for o, side, qty, price, intent, hour in specs]
    events = [fill(o["id"], o["id"], o["side"], o["qty"], o["filled_avg_price"],
                   o["filled_at"], symbol=symbol) for o in orders]
    broker, _ = fake_broker(closed=[AlpacaTrader._map_rest_order(o) for o in orders], activities=events)
    await sync_trade_performance(db, broker)
    assert (await row(db, "cover"))["realized_pnl"] is None
    assert (await row(db, "entry"))["realized_pnl"] == 100
    summary = await db.get_performance_summary()
    assert (summary["total_trades"], summary["open_trades"], summary["total_pnl"]) == (1, 0, 100)


async def test_partial_exit_stays_open_and_unfilled_orders_are_not_entries(db):
    at = datetime.now(timezone.utc).isoformat()
    await seed_entry(db, "entry", "AAPL", "AAPL", 100, 10, trade_type="equity", submitted_at=at)
    await seed_exit(db, "exit", "AAPL", "AAPL", 110, 3, trade_type="equity", filled_at=at)
    await db.upsert_trade_performance(alpaca_order_id="unfilled", symbol="MSFT", ticker="MSFT",
        side="buy", qty=10, filled_qty=0, order_status="canceled", submitted_at=at)
    await db.reconcile_trades()
    saved = await row(db, "entry")
    assert (saved["long_entry_qty"], saved["open_qty"], saved["realized_pnl"]) == (10, 7, 30)
    summary = await db.get_performance_summary()
    assert summary["total_trades"] == summary["open_trades"] == 1
    assert (await build_report_data(db, FakeTrader()))["metrics"]["trades_7d"] == 1


async def test_inferred_stock_cover_counts_only_its_excess_as_a_long_entry(db):
    await seed_exit(db, "short", "AAPL", "AAPL", 100, 10, filled_at="2026-10-01T13:00:00Z")
    await seed_entry(db, "mixed", "AAPL", "AAPL", 90, 15, submitted_at="2026-10-01T14:00:00Z")
    await db.reconcile_trades()
    assert ((await row(db, "mixed"))["long_entry_qty"], (await row(db, "mixed"))["open_qty"]) == (5, 5)
    assert (await db.get_performance_summary())["open_trades"] == 1


async def test_orphan_sell_to_close_does_not_create_short_inventory(db):
    await seed_exit(db, "old-close", "AAPL", "AAPL", 100, 10, filled_at="2026-10-01T13:00:00Z")
    await db.upsert_trade_performance(alpaca_order_id="old-close", position_intent="sell_to_close")
    await seed_entry(db, "new-entry", "AAPL", "AAPL", 90, 5, submitted_at="2026-10-01T14:00:00Z")
    await db.reconcile_trades()
    assert (await row(db, "new-entry"))["open_qty"] == 5
    assert (await db.get_performance_summary())["open_trades"] == 1


async def test_manual_exit_cannot_attribute_pnl_to_a_closing_purchase(db):
    await seed_entry(db, "entry", "AAPL", "AAPL", 100, 1)
    await seed_entry(db, "cover", "AAPL", "AAPL", 90, 1)
    await db.upsert_trade_performance(alpaca_order_id="cover", position_intent="buy_to_close")
    await db.record_exit("AAPL", 110, "manual", 10, 10)
    assert (await row(db, "entry"))["realized_pnl"] == 10
    assert (await row(db, "cover"))["realized_pnl"] is None


@pytest.mark.parametrize("pnls,expected", [
    ([100]*9+[-100], 9), ([100]+[-100]*9, 1/9), ([9999, -1], 9999),
    ([100, 200], None), ([], None), ([0], None), ([-100], 0)])
async def test_api_and_report_profit_factor_use_gross_pnl(db, pnls, expected):
    for i, pnl in enumerate(pnls):
        await seed_entry(db, str(i), f"TEST{i}", f"TEST{i}", 100, 1, trade_type="equity")
        await db._exec("UPDATE trade_performance SET realized_pnl=? WHERE alpaca_order_id=?", (pnl, str(i)))
    summary = await db.get_performance_summary()
    data = await build_report_data(db, FakeTrader())
    if expected is None:
        assert summary["profit_factor"] is data["metrics"]["profit_factor"] is None
    else:
        assert summary["profit_factor"] == pytest.approx(expected, abs=.005)
        assert data["metrics"]["profit_factor"] == pytest.approx(expected)
    assert summary["win_rate"] == pytest.approx(data["metrics"]["win_rate"], abs=.05)
    html = render_html(data)
    if expected == 9999:
        assert "9999.00" in html
    elif pnls and min(pnls) > 0:
        assert "∞" in html


def setup_for(rec):
    return EarningsSetup(ticker="TEST", price=100, avg_volume=2_000_000,
        iv30=.3, rv30=.2, iv30_rv30=1.5, ts_slope=-.005, expected_move="3%",
        vol_ok=rec == "SELL_PREMIUM", iv_expensive=rec != "AVOID", ts_inverted=True,
        next_earnings_date=(et_today()+timedelta(days=1)).isoformat())


@pytest.mark.parametrize("rec,expected", [("SELL_PREMIUM", True), ("CONSIDER", False), ("AVOID", False)])
def test_strict_gate_helper_keeps_execution_policy_separate(rec, expected):
    setup = setup_for(rec)
    assert setup.recommendation == rec
    assert passes_all_sell_gates(setup, 7) is expected
    if rec == "CONSIDER":
        assert is_sell_eligible(setup, 7) is True  # Marginal alerts remain allowed.


@pytest.mark.parametrize("source", ["watchlist", "measurement"])
@pytest.mark.parametrize("rec", ["SELL_PREMIUM", "CONSIDER", "AVOID"])
async def test_variant_logger_persists_the_actual_cohort_for_each_source(db, monkeypatch, source, rec):
    import main
    from signals import iv_variants
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "is_rth_now", lambda: True)
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={"iv_variants_log_enabled": True}))
    expiry = (et_today()+timedelta(days=(4-et_today().weekday()) % 7+7)).isoformat()

    def variants(*args):
        args[-1].update(attempted=1, priced=1, dropped={})
        return [{"variant": "condor_1.0sd", "expiry": expiry,
                 "strikes": {"long_put": 90, "short_put": 95, "short_call": 105, "long_call": 110},
                 "credit": 1, "credit_mid": 1.2, "fees": 5.2, "max_loss": 405.2}]
    monkeypatch.setattr(iv_variants, "build_variants", variants)
    # A stale/generic caller flag cannot mislabel CONSIDER as three-gate.
    await main.log_variant_evals(setup_for(rec), gate_passed=True, source=source)
    saved = (await db._query("SELECT * FROM iv_variant_evals"))[0]
    assert saved["recommendation"] == rec and saved["gate_passed"] == (rec == "SELL_PREMIUM")
    capture = json.loads((await db._query("SELECT snapshot_json FROM iv_variant_captures"))[0]["snapshot_json"])
    assert capture["gate_passed"] is (rec == "SELL_PREMIUM")
    await db.resolve_variant_eval(saved["id"], 100, 95)
    comparison = await db.get_gate_comparison(source)
    assert comparison["gated"]["n_events"] == (rec == "SELL_PREMIUM")
    assert comparison["consider"]["n_events"] == (rec == "CONSIDER")
    assert comparison["failed_gates"]["n_events"] == (rec != "SELL_PREMIUM")
    assert comparison["ungated"]["n_events"] == 1
    data = await build_report_data(db, FakeTrader())
    html = render_html(data)
    assert "CONSIDER only" in html and "unknown legacy gates" in html


async def test_old_ledger_migration_preserves_rows_but_does_not_certify_ambiguous_gates(tmp_path):
    path = tmp_path/"old.db"
    original = Database(path)
    await original.connect()
    await seed_entry(original, "old-order", "AAPL", "AAPL", 100, 1)
    await original.record_variant_eval("TEST", "2026-10-08", "2026-10-09", "condor_1.0sd",
        100, 3, {}, 1, 400, "2026-10-09", credit_mid=1.2, fees=5.2)
    await original.resolve_variant_eval(1, 100, 95)
    for column in ("position_intent", "long_entry_qty", "open_qty"):
        await original._exec(f"ALTER TABLE trade_performance DROP COLUMN {column}", strict=True)
    await original._exec("ALTER TABLE iv_variant_evals DROP COLUMN recommendation", strict=True)
    await original.close()
    migrated = Database(path)
    await migrated.connect()
    try:
        assert (await row(migrated, "old-order"))["position_intent"] is None
        comparison = await migrated.get_gate_comparison()
        assert comparison["unknown_gates"]["n_events"] == comparison["ungated"]["n_events"] == 1
        assert comparison["gated"]["n_events"] == comparison["failed_gates"]["n_events"] == 0
        assert await migrated.get_variant_summary() == []
        assert len(await migrated.get_variant_summary(gate_passed=None)) == 1
        assert (await migrated._query("SELECT gate_passed,realized_pnl FROM iv_variant_evals"))[0] == {
            "gate_passed": 1, "realized_pnl": 95}
        await migrated.reconcile_trades()
        assert (await row(migrated, "old-order"))["open_qty"] == 1
    finally:
        await migrated.close()

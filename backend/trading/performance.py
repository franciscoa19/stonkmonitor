"""Read-only broker sync for the single-instrument performance ledger."""
import asyncio
from datetime import datetime, timezone

from db import _is_occ
from trading.alpaca_trader import AlpacaTrader


_FILL_SIDES = {"sell_short": "sell"}


async def _save_order(db, order: dict) -> None:
    symbol = order["symbol"]
    option = _is_occ(symbol)
    ticker = symbol[:-15] if option else symbol
    await db.upsert_trade_performance(
        strict=True, alpaca_order_id=order["id"], symbol=symbol, ticker=ticker,
        side=order["side"], qty=order["qty"], filled_qty=order["filled_qty"],
        filled_avg_price=order["filled_avg"] or 0, order_type=order["type"],
        order_status=order["status"], submitted_at=order["created_at"],
        filled_at=order.get("filled_at"), trade_type="option" if option else "equity")


async def sync_trade_performance(db, trader) -> int:
    """Backfill actual fills before reconciling; never substitute placement time.

    Include working orders, whose partial fills are absent from closed history.
    Hydrate unknown activity order IDs too, so the 500-order history limit does
    not omit an entry or exit. Multi-leg orders stay in the condor ledger: their
    leg fills are recognised from the parents' nested legs, because a leg cannot
    be fetched by its own ID (the broker answers 404).
    A failed/incomplete activity or order fetch leaves existing P&L untouched
    for retry.
    """
    orders = await asyncio.to_thread(trader.get_order_history, days=90, limit=500)
    orders += await asyncio.to_thread(trader.get_orders, status="open")
    refreshed = set()
    for order in orders:
        await _save_order(db, order)
        refreshed.add(order["id"])

    started_at = datetime.now(timezone.utc).isoformat()
    start = await db.get_trade_fill_sync_start()
    activities = await asyncio.to_thread(trader.get_fill_activities, after=start)
    if activities is None:
        raise RuntimeError("Performance fill history unavailable or incomplete; reconciliation deferred")
    leg_ids = await asyncio.to_thread(trader.get_mleg_leg_order_ids)
    if leg_ids is None:
        raise RuntimeError("Multi-leg order listing unavailable or incomplete; reconciliation deferred")

    fills = []
    excluded = set()
    for activity in activities:
        order_id = activity["order_id"]
        if order_id in excluded or order_id in leg_ids:
            continue
        if order_id not in refreshed:
            raw = await asyncio.to_thread(trader.get_order_raw, order_id)
            if not isinstance(raw, dict) or raw.get("id") != order_id or raw.get("error"):
                raise RuntimeError("Performance activity order could not be retrieved")
            if raw.get("order_class") == "mleg":
                excluded.add(order_id)
                continue
            order = AlpacaTrader._map_rest_order(raw)
            await _save_order(db, order)
            refreshed.add(order_id)
        # The broker reports a short sale's executions as "sell_short" while
        # the order itself is a "sell"; the ledger models both as sells and
        # tracks the short inventory itself.
        fills.append({**activity, "side": _FILL_SIDES.get(activity.get("side"), activity.get("side"))})

    await db.record_trade_fills(fills)
    await db.mark_trade_fill_sync(started_at)
    return await db.reconcile_trades(require_fills=True)

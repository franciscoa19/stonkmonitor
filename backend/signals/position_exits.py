"""Durable single-position exits: submission is distinct from a broker fill."""
import asyncio
import logging
from copy import deepcopy
from math import isclose, isfinite
from uuid import uuid4

logger = logging.getLogger(__name__)
TERMINAL = {"filled", "canceled", "expired", "rejected"}


async def _persist(db, symbol, state, updated):
    """Only expose state in memory after the required write has committed."""
    await db.save_position_monitor_state(symbol, updated)
    state.clear()
    state.update(updated)


async def manage_position_exit(db, trader, pos, settings, state, on_fill=None):
    """Manage one long position. Return an operator message when state changes.

    Save a client order ID before POST, retain ambiguous outcomes for lookup,
    and advance one-shot flags only after actual fills. No estimated realized
    P&L is booked here: the performance sync reconciles broker executions.
    """
    symbol, qty, pnl = pos["symbol"], float(pos["qty"]), float(pos["pnl_pct"])
    updated = deepcopy(state)
    pending = state.get("pending")
    if pending:
        if pending.get("order_id"):
            order = await asyncio.to_thread(trader.get_order_raw, pending["order_id"])
        else:
            order = await asyncio.to_thread(trader.get_order_by_client_id, pending["client_id"])
        if order.get("error") or order.get("status") not in TERMINAL:
            return None
        filled = float(order.get("filled_qty") or 0)
        if not isfinite(filled) or filled < 0 or filled > pending["qty"]:
            return None
        if order["status"] == "filled" and filled != pending["qty"]:
            return None  # malformed broker response; never invent a quantity
        if filled > 0:
            try:
                price = float(order["filled_avg_price"])
                if not isfinite(price) or price <= 0:
                    return None
            except (KeyError, ValueError, TypeError):
                return None
            action = pending["action"]
            flag = {"sl": "sl_fired", "trim": "trimmed", "tp1": "tp_fired",
                    "tp2": "tp2_fired", "trailing_stop": "tp2_fired"}[action]
            # An incomplete liquidating exit remains eligible to close the rest.
            updated[flag] = filled >= pending["qty"] if pending.get("liquidating") else True
            if action == "tp1" and settings.pos_trail_after_tp:
                updated["trailing"] = True
                updated["high_watermark"] = max(pnl, pending["pnl_pct"])
            # Retire the entire lifecycle even if this poll's snapshot already
            # contains a same-symbol replacement (or the old pre-fill position).
            original_qty = pending.get("position_qty", pending["qty"] if pending.get("liquidating") else float("inf"))
            if filled >= original_qty:
                updated.clear()
            message = f"{action.upper()} filled: {symbol} x{filled:g} at ${price:.2f}"
        else:
            message = f"{pending['action'].upper()} {order['status']}: {symbol}; eligible to retry"
        updated.pop("pending", None)
        await _persist(db, symbol, state, updated)
        if filled > 0 and on_fill:
            from db import _is_occ
            realized = (price - pending["entry_price"]) * filled * (100 if _is_occ(symbol) else 1)
            on_fill(symbol, realized)
        return message

    if qty <= 0:
        return None
    basis = float(pos["avg_price"])
    if not all(isfinite(v) for v in (qty, pnl, basis)) or basis <= 0:
        return None
    previous = updated.get("position")
    # A changed basis or increased size denotes a new/scaled position. Partial
    # exits keep their TP/trailing progress, including after a process restart.
    if (previous is None and (updated.get("sl_fired") or updated.get("tp2_fired"))) or (
            previous is not None and (not isclose(basis, previous["avg_price"], rel_tol=1e-6)
                                      or qty > previous["qty"])):
        updated.clear()
    updated["position"] = {"qty": qty, "avg_price": basis}

    action, fraction = None, 1.0
    if pnl <= settings.pos_sl_pct and not updated.get("sl_fired"):
        action = "sl"
    elif pnl <= settings.pos_trim_pct and not updated.get("trimmed") and not updated.get("sl_fired"):
        action, fraction = "trim", settings.pos_trim_sell_pct
    elif updated.get("tp_fired") and not updated.get("tp2_fired"):
        if settings.pos_trail_after_tp and updated.get("trailing"):
            updated["high_watermark"] = max(pnl, updated.get("high_watermark", pnl))
            floor = max(updated["high_watermark"] - settings.pos_trail_pct, settings.pos_tp_pct * 0.75)
            if pnl <= floor:
                action = "trailing_stop"
        elif not settings.pos_trail_after_tp and pnl >= settings.pos_tp2_pct:
            action, fraction = "tp2", settings.pos_tp2_sell_pct
    elif pnl >= settings.pos_tp_pct and not updated.get("tp_fired"):
        action, fraction = "tp1", settings.pos_tp_sell_pct
    if not action:
        await _persist(db, symbol, state, updated)
        return None
    sell_qty = qty if fraction >= 1 else min(qty, max(1, int(qty * fraction)))
    client_id = f"sm-exit-{uuid4().hex}"
    updated["pending"] = {"action": action, "qty": sell_qty, "client_id": client_id,
                        "position_qty": qty, "pnl_pct": pnl, "entry_price": basis,
                        "liquidating": fraction >= 1}
    await _persist(db, symbol, state, updated)
    try:
        result = await asyncio.to_thread(trader.market_order, symbol, sell_qty, "sell",
                                        client_order_id=client_id)
    except Exception as e:
        result = {"error": str(e), "ambiguous": True}
    updated = deepcopy(state)
    if result.get("id"):
        updated["pending"]["order_id"] = result["id"]
        message = f"{action.upper()} submitted: {symbol} x{sell_qty:g}; awaiting fill"
    elif result.get("ambiguous", True):
        message = f"{action.upper()} outcome unknown: {symbol}; awaiting broker reconciliation"
    else:
        updated.pop("pending", None)
        message = f"{action.upper()} rejected: {symbol}; eligible to retry"
    await _persist(db, symbol, state, updated)
    return message

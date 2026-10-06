"""Fail-closed ownership checks; unknown options never get single-leg exits."""
import asyncio
import math
import json

from db import DatabaseError, _is_occ


async def option_ownership(db, trader, positions=None) -> dict:
    condors = await db.get_active_condors()
    spread_legs = await db.get_active_condor_leg_symbols()
    trades = await db._query(
        "SELECT * FROM pending_trades WHERE status IN ('confirmed','submitting','submission_unknown')",
        strict=True)
    long_options = {t["symbol"] for t in trades if t["status"] == "confirmed" and _is_occ(t["symbol"])}
    if positions is None:
        positions = await asyncio.to_thread(trader.get_positions_raw)
    orders = await asyncio.to_thread(trader.get_open_orders_raw)
    if positions is None or orders is None:
        raise DatabaseError("Broker ownership snapshot unavailable; automated trading deferred")
    known_ids = {c.get(k) for c in condors for k in ("entry_order_id", "close_order_id")}
    known_ids.update(t.get("alpaca_order_id") for t in trades)
    ns = await db.order_namespace()
    known_clients = {f"sm-{ns}-condor-{c['id']}-entry" for c in condors}
    known_clients.update(c.get("close_client_order_id") for c in condors)
    known_clients.update(f"sm-{ns}-trade-{t['id']}-{kind}" for t in trades for kind in ("bracket", "limit"))
    known_clients.discard(None)
    unknown_orders = []
    for order in orders:
        if not isinstance(order, dict) or not order.get("id"):
            raise DatabaseError("Malformed broker order ownership")
        if order.get("order_class") == "mleg":
            legs = order.get("legs")
            if not isinstance(legs, list) or not legs or any(
                    not isinstance(l, dict) or not isinstance(l.get("symbol"), str) or not l["symbol"].strip()
                    for l in legs):
                raise DatabaseError("Unreadable broker spread ownership")
            spread_legs.update(l["symbol"].strip().upper() for l in legs)
            if (order["id"] not in known_ids and order.get("client_order_id") not in known_clients):
                unknown_orders.append(order["id"])
        elif _is_occ(order.get("symbol")):
            if order["id"] not in known_ids and order.get("client_order_id") not in known_clients:
                unknown_orders.append(order["id"])
    held, short_tickers = {}, set()
    for position in positions:
        try:
            symbol, qty = position["symbol"], float(position["qty"])
            if not isinstance(symbol, str) or not symbol or not math.isfinite(qty):
                raise ValueError("invalid position")
        except (KeyError, TypeError, ValueError) as e:
            raise DatabaseError("Malformed broker position ownership") from e
        if _is_occ(symbol) and qty:
            held[symbol] = qty
            if qty < 0:
                short_tickers.add(symbol[:-15])
    unknown = {s for s, qty in held.items()
               if s not in spread_legs and (s not in long_options or qty < 0 or s[:-15] in short_tickers)}
    local_qty = {}
    for c in condors:
        qty = float(c["qty"])
        closed = float(c.get("closed_qty") or 0)
        if not math.isfinite(qty) or qty <= 0 or not math.isfinite(closed) or not 0 <= closed <= qty:
            raise DatabaseError("Invalid local spread quantity")
        for leg in json.loads(c["legs_json"]):
            symbol = leg["symbol"].strip().upper()
            signed_qty = (qty - closed) * (1 if leg["side"] == "buy" else -1)
            local_qty[symbol] = local_qty.get(symbol, 0) + signed_qty
    unknown.update(s for s in held if s in local_qty and (
        held[s] * local_qty[s] <= 0 or abs(held[s]) > abs(local_qty[s]) + 1e-8))
    # Short options can also belong to a spread assembled one leg at a time.
    protected = spread_legs | unknown | {s for s in held if s[:-15] in short_tickers}
    reasons = []
    if unknown:
        reasons.append("untracked option holdings: " + ", ".join(sorted(unknown)))
    if unknown_orders:
        reasons.append("untracked working option orders: " + ", ".join(unknown_orders))
    return {"positions": positions, "orders": orders, "protected_symbols": protected,
            "entry_block_reason": "; ".join(reasons)}

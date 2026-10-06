"""Account-wide limits on new risk.

Two percent-of-equity rules that every automated entry passes through:

* a cap on the most the account can lose across everything open or pending
  (ACCOUNT_MAX_RISK_PCT);
* a reserve of free cash that a new entry may not spend
  (ACCOUNT_CASH_RESERVE_PCT).

Per-trade sizing already follows equity, but "10% per condor, three at a time"
is only 30% of the account on the day each one is opened. After losses or a
withdrawal the same open positions are a larger share of what is left, and a
position count says nothing about that. These rules are measured against the
current balance every time.

They limit NEW entries only. Nothing here closes a position: when open risk is
above the cap, entries stop until it is back under. Anything unreadable — a
balance, a position value, a ledger row — blocks the entry rather than being
assumed to be zero.
"""
import asyncio
import json
import math

from db import DatabaseError, _is_occ

# Held from reading the limits until the new entry is persisted, so two entries
# (a condor and a flow trade, say) cannot both spend the same headroom.
entry_lock = asyncio.Lock()

FLOW_TRADE_TYPES = ("option", "equity", "equity_long")
TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced"}


def _amount(value, what: str, *, signed: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise DatabaseError(f"Unusable {what}") from e
    if not math.isfinite(number) or (number < 0 and not signed):
        raise DatabaseError(f"Unusable {what}")
    return number


def _entry_cost(row: dict) -> float:
    """What a queued flow entry commits: its full cost at the limit price."""
    if row.get("trade_type") not in FLOW_TRADE_TYPES:
        raise DatabaseError("Unknown pending entry reservation type")
    multiplier = 100 if row["trade_type"] == "option" else 1
    amount = (_amount(row.get("qty"), "pending entry reservation")
              * _amount(row.get("limit_price"), "pending entry reservation") * multiplier)
    if amount <= 0:
        raise DatabaseError("Unusable pending entry reservation")
    return amount


def _condor_remaining(row: dict) -> tuple[float, float]:
    """(spreads still open or pending, max loss per spread in dollars)."""
    qty = _amount(row.get("qty"), "condor quantity")
    closed = _amount(row.get("closed_qty") or 0, "condor quantity")
    max_loss = _amount(row.get("max_loss"), "condor max loss")
    if qty <= 0 or closed > qty or max_loss <= 0:
        raise DatabaseError("Unusable condor risk")
    return qty - closed, max_loss


def _position_value(position: dict) -> float:
    """What a holding outside a condor can still lose: its current value. Cost
    is the fallback when the broker sends no mark."""
    for key in ("market_value", "cost_basis"):
        if position.get(key) is not None:
            return abs(_amount(position[key], "broker position value", signed=True))
    raise DatabaseError("Unusable broker position value")


def _order_maps(orders):
    if not isinstance(orders, list):
        raise DatabaseError("Broker order snapshot unavailable")
    by_id, by_client = {}, {}
    for order in orders:
        if (not isinstance(order, dict) or not isinstance(order.get("id"), str)
                or not order["id"].strip()):
            raise DatabaseError("Malformed open-order sizing snapshot")
        if order["id"] in by_id and by_id[order["id"]] != order:
            raise DatabaseError("Conflicting broker order snapshot")
        by_id[order["id"]] = order
        if order.get("client_order_id"):
            client = order["client_order_id"]
            if not isinstance(client, str):
                raise DatabaseError("Malformed broker client ID")
            if client in by_client and by_client[client]["id"] != order["id"]:
                raise DatabaseError("Conflicting broker client IDs")
            by_client[client] = order
    return by_id, by_client


def _flow_order(row, ns, by_id, by_client):
    order = by_id.get(row.get("alpaca_order_id"))
    if order is not None:
        return order
    for kind in ("bracket", "limit"):
        client = f"sm-{ns}-trade-{row['id']}-{kind}"
        if client in by_client:
            return by_client[client]
    return None


def _manual_order(row, by_id, by_client):
    return by_id.get(row.get("alpaca_order_id")) or by_client.get(row["client_order_id"])


def _remaining_qty(qty, order):
    if order is None:
        return qty
    if order.get("qty") is not None and _amount(order["qty"], "broker order quantity") != qty:
        raise DatabaseError("Broker order quantity differs from its reservation")
    status = order.get("status")
    if status in TERMINAL_STATUSES and order.get("filled_qty") is None:
        raise DatabaseError("Terminal order fill quantity unavailable")
    filled = _amount(order.get("filled_qty", 0), "broker entry fill quantity")
    if filled > qty or (status == "filled" and filled != qty):
        raise DatabaseError("Unusable broker entry fill quantity")
    return 0.0 if status in TERMINAL_STATUSES else qty - filled


def _flow_cost(row, order):
    cost = _entry_cost(row)  # Validate the durable economics even for a filled order.
    if order is not None and (order.get("error") or
            (order.get("symbol") is not None and order["symbol"] != row["symbol"]) or
            (order.get("side") is not None and order["side"] != "buy")):
        raise DatabaseError("Broker entry does not match its reservation")
    if order is not None and order.get("limit_price") is not None:
        price = _amount(order["limit_price"], "broker entry limit price")
        if price <= 0:
            raise DatabaseError("Unusable broker entry limit price")
        cost *= max(float(row["limit_price"]), price) / float(row["limit_price"])
    return cost * _remaining_qty(float(row["qty"]), order) / float(row["qty"])


def _condor_collateral(row):
    strikes = [_amount(row.get(key), "condor collateral strikes")
               for key in ("long_put", "short_put", "short_call", "long_call")]
    lp, sp, sc, lc = strikes
    if not 0 < lp < sp <= sc < lc:
        raise DatabaseError("Unusable condor collateral strikes")
    collateral = round(max(sp - lp, lc - sc) * 100, 2)
    if not math.isfinite(collateral) or collateral <= 0:
        raise DatabaseError("Unusable condor collateral")
    return collateral


def _opening_order_cost(order, held):
    """Unfilled opening commitment. Contingent bracket/OCO children are exits.
    Unbounded opening sells and unpriceable market buys defer automated entries."""
    if order.get("status") in TERMINAL_STATUSES:
        if order.get("qty") is not None:
            _remaining_qty(_amount(order["qty"], "broker order quantity"), order)
        elif order.get("filled_qty") is None:
            raise DatabaseError("Terminal order fill quantity unavailable")
        return 0.0
    symbol, side = order.get("symbol"), order.get("side")
    if not isinstance(symbol, str) or not symbol.strip() or side not in ("buy", "sell"):
        raise DatabaseError("Unusable broker entry order")
    if order.get("order_class") == "mleg":
        raise DatabaseError("Untracked multi-leg entry cannot be valued")
    intent = order.get("position_intent")
    if intent in ("buy_to_close", "sell_to_close"):
        if side != ("buy" if intent == "buy_to_close" else "sell"):
            raise DatabaseError("Inconsistent closing order intent")
        qty = _amount(order.get("qty"), "broker closing order quantity")
        remaining = _remaining_qty(qty, order)
        symbol = symbol.strip().upper()
        cover = max(0.0, -held.get(symbol, 0)) if side == "buy" else max(0.0, held.get(symbol, 0))
        covered = min(remaining, cover)
        held[symbol] = held.get(symbol, 0) + (covered if side == "buy" else -covered)
        return 0.0
    if intent not in (None, "", "buy_to_open", "sell_to_open"):
        raise DatabaseError("Unknown broker position intent")
    symbol = symbol.strip().upper()
    if order.get("notional") is not None:
        if side != "buy" or held.get(symbol, 0) < 0:
            raise DatabaseError("Unusable notional opening order")
        notional = _amount(order["notional"], "broker order notional")
        filled = _amount(order.get("filled_qty", 0), "broker entry fill quantity")
        spent = filled * _amount(order.get("filled_avg_price"), "broker fill price") if filled else 0
        if notional <= 0 or spent > notional:
            raise DatabaseError("Unusable notional opening order")
        return notional - spent
    qty = _amount(order.get("qty"), "broker order quantity")
    remaining = _remaining_qty(qty, order)
    if qty <= 0:
        raise DatabaseError("Unusable broker order quantity")
    if not intent:
        cover = max(0.0, -held.get(symbol, 0)) if side == "buy" else max(0.0, held.get(symbol, 0))
        covered = min(remaining, cover)
        held[symbol] = held.get(symbol, 0) + (covered if side == "buy" else -covered)
        remaining -= covered
    if not remaining:
        return 0.0
    if order.get("order_class") == "oco":
        raise DatabaseError("OCO opening order cannot be bounded by one exit leg")
    if side == "sell":
        raise DatabaseError("Opening short order cannot be bounded by its sale value")
    kind = order.get("type") or order.get("order_type")
    if kind not in ("limit", "stop_limit"):
        raise DatabaseError("Opening order has no enforceable price ceiling")
    price = _amount(order.get("limit_price"), "broker entry limit price")
    if price <= 0:
        raise DatabaseError("Unusable broker entry limit price")
    return remaining * price * (100 if _is_occ(symbol) or order.get("asset_class") == "us_option" else 1)


async def read_risk_snapshot(db, trader):
    """Read entry statuses BEFORE holdings and balance. No broker writes or ledger
    releases; callers hold entry_lock when sizing or persisting reconciled state."""
    flows = await db.get_entry_reservations()
    manuals = await db.get_manual_entry_reservations()
    if any(row["status"] != "confirmed" for row in manuals):
        raise DatabaseError("A manual order outcome is unresolved; new entries are deferred")
    orders = await asyncio.to_thread(trader.get_open_orders_raw)
    by_id, by_client = _order_maps(orders)
    ns = await db.order_namespace()
    historical = []
    for row in [*flows, *manuals]:
        is_manual = "request_id" in row
        found = (_manual_order(row, by_id, by_client) if is_manual
                 else _flow_order(row, ns, by_id, by_client))
        if found is not None or row["status"] != "confirmed":
            continue
        oid = row.get("alpaca_order_id")
        if not oid:
            raise DatabaseError("Confirmed entry has no broker order ID")
        order = await asyncio.to_thread(trader.get_order_raw, oid)
        if (not isinstance(order, dict) or order.get("error") or not order.get("status")
                or order.get("id", oid) != oid):
            raise DatabaseError("Entry-order reconciliation unavailable")
        order = {**order, "id": oid}
        historical.append(order)
    positions = await asyncio.to_thread(trader.get_positions_raw)
    account = await asyncio.to_thread(trader.get_account)
    if not isinstance(positions, list) or not isinstance(account, dict):
        raise DatabaseError("Broker position/account snapshot unavailable")
    return {"account": account, "positions": positions, "orders": orders,
            "entry_orders": [*orders, *historical]}


async def account_limits(db, account: dict, positions: list, orders: list, settings, *,
                         asset: str = "option", exclude_trade_id=None, entry_orders=None) -> dict:
    """Risk in use and cash free, against the account's two percentage limits.

    `account`, `positions` and `orders` are fresh broker snapshots (orders read
    before the account). `asset` picks the buying power that actually funds the
    entry: options buying power for options and spreads, non-marginable buying
    power for stock — never the leveraged stock figure.

    Open risk is: the remaining max loss of every active condor (pending ones
    included), the unfilled commitment of flow/manual/broker opening orders,
    and the current value of every other holding. Filled quantities count in
    holdings once; a holding the bot did not open counts too.
    """
    if not isinstance(account, dict) or account.get("error"):
        raise DatabaseError("Verified account balance unavailable")
    if account.get("trading_blocked") or account.get("account_blocked"):
        raise DatabaseError("Broker reports the account blocked from trading")
    equity = _amount(account.get("equity"), "account equity")
    if equity <= 0:
        raise DatabaseError("Unusable account equity")
    if asset not in ("option", "stock"):
        raise DatabaseError("Unknown asset type for account limits")
    if positions is None or orders is None:
        raise DatabaseError("Broker position/order snapshot unavailable")

    listed, _ = _order_maps(orders)
    by_id, by_client = _order_maps(orders if entry_orders is None else entry_orders)
    ns = await db.order_namespace()
    known_ids, known_clients = set(), set()
    unlisted = 0.0

    condor_risk, condor_legs = 0.0, set()
    for row in await db.get_active_condors():
        remaining, max_loss = _condor_remaining(row)
        condor_risk += remaining * max_loss
        try:
            condor_legs.update(leg["symbol"].strip().upper() for leg in json.loads(row["legs_json"]))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise DatabaseError("Unreadable condor legs") from e
        known_ids.update(row.get(key) for key in ("entry_order_id", "close_order_id"))
        client = f"sm-{ns}-condor-{row['id']}-entry"
        known_clients.update((client, row.get("close_client_order_id")))
        if row["status"] == "pending_entry" and row.get("entry_order_id") not in listed and not any(
                order.get("client_order_id") == client for order in orders):
            unlisted += remaining * _condor_collateral(row)

    known_ids.discard(None)
    known_clients.discard(None)

    position_risk, held = 0.0, {}
    for position in positions:
        if (not isinstance(position, dict) or not isinstance(position.get("symbol"), str)
                or not position["symbol"].strip()):
            raise DatabaseError("Malformed broker position")
        qty = _amount(position.get("qty"), "broker position quantity", signed=True)
        if not qty:
            continue
        symbol = position["symbol"].strip().upper()
        if symbol in held:
            raise DatabaseError("Duplicate broker position")
        held[symbol] = qty
        if symbol in condor_legs:
            continue  # counted once, as its condor's max loss
        position_risk += _position_value(position)

    pending_risk = 0.0
    for row in await db.get_entry_reservations():
        order = _flow_order(row, ns, by_id, by_client)
        known_ids.add(row.get("alpaca_order_id"))
        known_clients.update(f"sm-{ns}-trade-{row['id']}-{kind}" for kind in ("bracket", "limit"))
        if row["id"] == exclude_trade_id:
            continue
        cost = _flow_cost(row, order)
        pending_risk += cost
        if order is None or order["id"] not in listed:
            unlisted += cost

    manual_risk = 0.0
    for row in await db.get_manual_entry_reservations():
        if row["status"] != "confirmed":
            raise DatabaseError("A manual order outcome is unresolved; new entries are deferred")
        order = _manual_order(row, by_id, by_client)
        if order is None:
            raise DatabaseError("Manual entry-order reconciliation unavailable")
        if order["id"] in known_ids or order.get("client_order_id") in known_clients:
            raise DatabaseError("Manual entry duplicates another reservation")
        cost = _opening_order_cost(order, held)
        manual_risk += cost
        known_ids.add(order["id"])
        known_clients.add(row["client_order_id"])
        if order["id"] not in listed:
            unlisted += cost

    broker_risk = 0.0
    exit_children = set()
    for parent in orders:
        if parent.get("order_class") in ("bracket", "oco"):
            legs = parent.get("legs") or []
            if not isinstance(legs, list) or any(not isinstance(leg, dict) or not leg.get("id") for leg in legs):
                raise DatabaseError("Malformed contingent exit orders")
            expected_side = parent.get("side")
            if parent["order_class"] == "bracket":
                expected_side = "sell" if expected_side == "buy" else "buy"
            if any(leg.get("position_intent") in ("buy_to_open", "sell_to_open") or
                   (leg.get("side") is not None and leg["side"] != expected_side) for leg in legs):
                raise DatabaseError("Inconsistent contingent exit orders")
            exit_children.update(leg["id"] for leg in legs)
    seen = set()
    for order in orders:
        if order["id"] in seen or order["id"] in exit_children:
            continue
        seen.add(order["id"])
        if order["id"] in known_ids or order.get("client_order_id") in known_clients:
            continue
        broker_risk += _opening_order_cost(order, held)
        # Bracket and OCO legs are contingent exits, never extra opening risk.
        # Unrecognised nested orders cannot safely be assumed to be exits.
        if order.get("legs") and order.get("order_class") not in ("bracket", "oco"):
            raise DatabaseError("Untracked nested order cannot be valued")

    funding = "options_buying_power" if asset == "option" else "non_marginable_buying_power"
    free_cash = min(_amount(account.get("cash"), "cash balance"),
                    _amount(account.get(funding), funding.replace("_", " ")))

    max_risk_pct = float(settings.account_max_risk_pct)
    reserve_pct = float(settings.account_cash_reserve_pct)
    open_risk = condor_risk + position_risk + pending_risk + manual_risk + broker_risk
    if not all(math.isfinite(value) for value in (open_risk, unlisted)):
        raise DatabaseError("Nonfinite account commitments")
    risk_cap = equity * max_risk_pct
    cash_reserve = equity * reserve_pct
    return {
        "equity": equity,
        "max_risk_pct": max_risk_pct, "risk_cap": risk_cap,
        "open_risk": open_risk, "open_risk_pct": open_risk / equity,
        "condor_risk": condor_risk, "position_risk": position_risk, "pending_risk": pending_risk,
        "manual_risk": manual_risk, "broker_order_risk": broker_risk,
        "risk_headroom": max(0.0, risk_cap - open_risk),
        "cash_reserve_pct": reserve_pct, "cash_reserve": cash_reserve,
        "free_cash": free_cash, "free_cash_pct": free_cash / equity,
        "unlisted_reserved": unlisted,
        "cash_available": max(0.0, free_cash - unlisted - cash_reserve),
    }


async def account_limits_snapshot(db, trader, settings, *, asset: str = "option") -> dict:
    """Read-only view for the report and the API: fetches its own snapshots."""
    async with entry_lock:
        snapshot = await read_risk_snapshot(db, trader)
        return await account_limits(db, **snapshot, settings=settings, asset=asset)


async def reconcile_entry_statuses(db, trader, settings):
    """Retire confirmed reservations only after complete verified snapshots.
    Reports stay read-only; the periodic execution reconciler persists this."""
    async with entry_lock:
        flows = await db.get_entry_reservations()
        manuals = await db.get_manual_entry_reservations()
        if not any(row["status"] == "confirmed" for row in [*flows, *manuals]):
            return
        snapshot = await read_risk_snapshot(db, trader)
        await account_limits(db, **snapshot, settings=settings)
        by_id, by_client = _order_maps(snapshot["entry_orders"])
        ns = await db.order_namespace()
        for row in flows:
            order = _flow_order(row, ns, by_id, by_client)
            if row["status"] == "confirmed" and order and order.get("status") in TERMINAL_STATUSES:
                await db.update_pending_trade(row["id"], entry_order_status=order["status"])
        for row in manuals:
            order = _manual_order(row, by_id, by_client)
            if order and order.get("status") in TERMINAL_STATUSES:
                await db.resolve_manual_order_status(row["request_id"], order["status"])


def describe(limits: dict) -> str:
    return (f"risk in use ${limits['open_risk']:,.0f} ({limits['open_risk_pct']:.1%} of equity, "
            f"cap {limits['max_risk_pct']:.0%}) · free cash ${limits['free_cash']:,.0f} "
            f"({limits['free_cash_pct']:.1%}, reserve {limits['cash_reserve_pct']:.0%})")

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

from db import DatabaseError

# Held from reading the limits until the new entry is persisted, so two entries
# (a condor and a flow trade, say) cannot both spend the same headroom.
entry_lock = asyncio.Lock()

FLOW_TRADE_TYPES = ("option", "equity", "equity_long")


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


async def unlisted_entry_reservations(db, orders: list, *, exclude_trade_id=None) -> float:
    """Dollars committed to local entries the broker does not list yet.

    A working order the broker lists is already taken out of the buying power it
    reports. An entry that was persisted but is not (or not yet) in that list —
    a crash mid-submission, an ambiguous timeout — is not, so it is held back
    here. `orders` must be read BEFORE the account balance it is netted against:
    the other way round, an order listed in between would be counted by neither.
    """
    known_orders, known_clients = set(), set()
    for order in orders:
        if not isinstance(order, dict) or not order.get("id"):
            raise DatabaseError("Malformed open-order sizing snapshot")
        known_orders.add(order["id"])
        if order.get("client_order_id"):
            known_clients.add(order["client_order_id"])
    ns = await db.order_namespace()
    reserved = 0.0
    for row in await db.get_entry_reservations():
        if row["id"] == exclude_trade_id:
            continue
        if (row.get("alpaca_order_id") in known_orders or any(
                f"sm-{ns}-trade-{row['id']}-{kind}" in known_clients for kind in ("bracket", "limit"))):
            continue  # already reflected in the broker's available funds
        reserved += _entry_cost(row)
    for row in await db.get_active_condors():
        if row["status"] != "pending_entry":
            continue
        if (row.get("entry_order_id") in known_orders
                or f"sm-{ns}-condor-{row['id']}-entry" in known_clients):
            continue
        remaining, max_loss = _condor_remaining(row)
        reserved += remaining * max_loss
    return reserved


async def account_limits(db, account: dict, positions: list, orders: list, settings, *,
                         asset: str = "option", exclude_trade_id=None) -> dict:
    """Risk in use and cash free, against the account's two percentage limits.

    `account`, `positions` and `orders` are fresh broker snapshots (orders read
    before the account). `asset` picks the buying power that actually funds the
    entry: options buying power for options and spreads, non-marginable buying
    power for stock — never the leveraged stock figure.

    Open risk is: the remaining max loss of every active condor (pending ones
    included), the full cost of every queued flow entry, and the current value
    of every other holding. A holding the bot did not open counts too — the cap
    is on the account, not on one strategy.
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

    condor_risk, condor_legs = 0.0, set()
    for row in await db.get_active_condors():
        remaining, max_loss = _condor_remaining(row)
        condor_risk += remaining * max_loss
        try:
            condor_legs.update(leg["symbol"].strip().upper() for leg in json.loads(row["legs_json"]))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise DatabaseError("Unreadable condor legs") from e

    position_risk = 0.0
    for position in positions:
        if not isinstance(position, dict) or not isinstance(position.get("symbol"), str):
            raise DatabaseError("Malformed broker position")
        if not _amount(position.get("qty"), "broker position quantity", signed=True):
            continue
        if position["symbol"].strip().upper() in condor_legs:
            continue  # counted once, as its condor's max loss
        position_risk += _position_value(position)

    pending_risk = sum([_entry_cost(row) for row in await db.get_entry_reservations()
                        if row["id"] != exclude_trade_id])

    funding = "options_buying_power" if asset == "option" else "non_marginable_buying_power"
    free_cash = min(_amount(account.get("cash"), "cash balance"),
                    _amount(account.get(funding), funding.replace("_", " ")))
    unlisted = await unlisted_entry_reservations(db, orders, exclude_trade_id=exclude_trade_id)

    max_risk_pct = float(settings.account_max_risk_pct)
    reserve_pct = float(settings.account_cash_reserve_pct)
    open_risk = condor_risk + position_risk + pending_risk
    risk_cap = equity * max_risk_pct
    cash_reserve = equity * reserve_pct
    return {
        "equity": equity,
        "max_risk_pct": max_risk_pct, "risk_cap": risk_cap,
        "open_risk": open_risk, "open_risk_pct": open_risk / equity,
        "condor_risk": condor_risk, "position_risk": position_risk, "pending_risk": pending_risk,
        "risk_headroom": max(0.0, risk_cap - open_risk),
        "cash_reserve_pct": reserve_pct, "cash_reserve": cash_reserve,
        "free_cash": free_cash, "free_cash_pct": free_cash / equity,
        "unlisted_reserved": unlisted,
        "cash_available": max(0.0, free_cash - unlisted - cash_reserve),
    }


async def account_limits_snapshot(db, trader, settings, *, asset: str = "option") -> dict:
    """Read-only view for the report and the API: fetches its own snapshots."""
    orders = await asyncio.to_thread(trader.get_open_orders_raw)
    positions = await asyncio.to_thread(trader.get_positions_raw)
    account = await asyncio.to_thread(trader.get_account)
    return await account_limits(db, account, positions, orders, settings, asset=asset)


def describe(limits: dict) -> str:
    return (f"risk in use ${limits['open_risk']:,.0f} ({limits['open_risk_pct']:.1%} of equity, "
            f"cap {limits['max_risk_pct']:.0%}) · free cash ${limits['free_cash']:,.0f} "
            f"({limits['free_cash_pct']:.1%}, reserve {limits['cash_reserve_pct']:.0%})")

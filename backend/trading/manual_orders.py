"""Durable manual requests: one stable broker ID across retries and DB resets."""
import asyncio
import json
import math
from uuid import UUID

_lock = asyncio.Lock()


def _response(row, status=None, error=None):
    return {"request_id": row["request_id"], "client_order_id": row["client_order_id"],
            "status": status or row["status"], "id": row.get("alpaca_order_id"),
            "error": error or row.get("error")}


def _matches(order, payload):
    try:
        qty = float(order["qty"])
        if (not math.isfinite(qty) or qty != payload["qty"]
                or order["symbol"] != payload["ticker"] or order["side"] != payload["side"]
                or (order.get("type") or order.get("order_type")) != payload["order_type"]
                or order["time_in_force"] != payload["tif"]):
            return False
        return payload["order_type"] != "limit" or float(order["limit_price"]) == payload["limit_price"]
    except (KeyError, TypeError, ValueError):
        return False


async def manual_order_request(db, trader, request_id, payload=None):
    """Payload provided means POST; absent means reconcile-only GET."""
    request_id = str(UUID(str(request_id)))
    client_id = f"sm-manual-{UUID(request_id).hex}"  # 42 chars, unique beyond a local DB lifecycle
    from trading.account_risk import entry_lock
    async with _lock, entry_lock:
        if payload is not None:
            row = await db.ensure_manual_order_request(
                request_id, json.dumps(payload, sort_keys=True, separators=(",", ":")), client_id)
        else:
            row = await db.get_manual_order_request(request_id)
            if row is None:
                return None
        if row["status"] in ("confirmed", "rejected"):
            return _response(row)
        found = await asyncio.to_thread(trader.get_order_by_client_id, client_id)
        if found.get("id"):
            original = json.loads(row["payload_json"])
            if not _matches(found, original):
                raise ValueError("Broker order does not match the saved request; manual reconciliation required")
            status = "rejected" if found.get("status") == "rejected" else "confirmed"
            await db.update_manual_order_request(request_id, status, found["id"],
                                                 "Broker rejected order" if status == "rejected" else None)
            return _response(await db.get_manual_order_request(request_id))
        if not found.get("not_found"):
            return _response(row, "pending", "Broker reconciliation unavailable; checking the same request")
        if row["status"] != "new":
            # A 404 after an ambiguous POST does not prove non-acceptance.
            return _response(row, "pending", "Order outcome unknown; awaiting broker confirmation")
        if payload is None:
            return _response(row, "ready")
        await db.update_manual_order_request(request_id, "submitting", claim=True)
        try:
            if payload["order_type"] == "market":
                result = await asyncio.to_thread(trader.market_order, payload["ticker"], payload["qty"],
                    payload["side"], payload["tif"], client_order_id=client_id)
            else:
                result = await asyncio.to_thread(trader.limit_order, payload["ticker"], payload["qty"],
                    payload["side"], payload["limit_price"], payload["tif"], client_order_id=client_id)
        except Exception as e:
            result = {"error": str(e), "ambiguous": True}
        if result.get("id"):
            await db.update_manual_order_request(request_id, "confirmed", result["id"])
        elif result.get("ambiguous", True):
            await db.update_manual_order_request(request_id, "pending", error=result.get("error"))
        else:
            await db.update_manual_order_request(request_id, "rejected", error=result.get("error"))
        return _response(await db.get_manual_order_request(request_id))

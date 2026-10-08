"""
Alpaca order execution — paper and live trading.
Supports market, limit, stop, and options orders.
"""
import logging
from typing import Optional, Literal
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopLimitOrderRequest,
    TrailingStopOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, OrderClass

logger = logging.getLogger(__name__)


def _client_id_conflict(message) -> bool:
    message = str(message).lower()
    return (("client_order_id" in message or "client order id" in message)
            and any(word in message for word in ("unique", "duplicate", "already")))


class AlpacaTrader:
    def __init__(self, api_key: str, secret_key: str, paper: bool = True, options_feed: str = "auto"):
        self.paper = paper
        self.options_feed = options_feed
        self.client = TradingClient(api_key, secret_key, paper=paper)
        # Raw-REST essentials for multi-leg (mleg) orders — alpaca-py 0.29 has no
        # OptionLegRequest/MLEG, but the REST API supports it (verified on paper).
        self._key = api_key
        self._secret = secret_key
        self._trade_base = ("https://paper-api.alpaca.markets" if paper
                            else "https://api.alpaca.markets")
        self._data_base = "https://data.alpaca.markets"
        mode = "PAPER" if paper else "LIVE"
        logger.info(f"AlpacaTrader initialized in {mode} mode")

    @property
    def _rest_headers(self) -> dict:
        return {"APCA-API-KEY-ID": self._key,
                "APCA-API-SECRET-KEY": self._secret,
                "Content-Type": "application/json"}

    @staticmethod
    def _retry_call(fn, attempts: int = 2, delay: float = 0.5):
        """Call fn(), retrying once on any exception. Alpaca intermittently drops
        the connection mid-request (RemoteDisconnected / read timeout); one quick
        retry absorbs the blip instead of surfacing a transient error."""
        import time as _t
        last = None
        for i in range(attempts):
            try:
                return fn()
            except Exception as e:
                last = e
                if i < attempts - 1:
                    _t.sleep(delay)
        raise last

    # ------------------------------------------------------------------ #
    #  Account Info                                                        #
    # ------------------------------------------------------------------ #
    def get_account(self) -> dict:
        try:
            acct = self._retry_call(self.client.get_account)
            return {
                "equity":         float(acct.equity or 0),
                "cash":           float(acct.cash or 0),
                "buying_power":   float(acct.buying_power or 0),
                # These are deliberately distinct from leveraged stock buying
                # power. Missing fields remain unknown, never invented cash.
                "options_buying_power": (float(acct.options_buying_power)
                                         if acct.options_buying_power is not None else None),
                "non_marginable_buying_power": (float(acct.non_marginable_buying_power)
                                                if acct.non_marginable_buying_power is not None else None),
                "trading_blocked": bool(acct.trading_blocked),
                "account_blocked": bool(acct.account_blocked),
                "day_trade_count":int(acct.daytrade_count or 0),
                "pdt_flag":       bool(acct.pattern_day_trader),
                "status":         acct.status.value if acct.status else "unknown",
            }
        except Exception as e:
            logger.error(f"get_account error: {e}")
            return {}

    def get_positions(self) -> list[dict]:
        try:
            positions = self._retry_call(self.client.get_all_positions)
            return [
                {
                    "symbol":    p.symbol,
                    "qty":       float(p.qty),
                    "side":      p.side.value,
                    "avg_price": float(p.avg_entry_price),
                    "current":   float(p.current_price),
                    "pnl":       float(p.unrealized_pl),
                    "pnl_pct":   float(p.unrealized_plpc) * 100,
                    "market_val":float(p.market_value),
                }
                for p in positions
            ]
        except Exception as e:
            logger.error(f"get_positions error: {e}")
            return []

    @staticmethod
    def _map_rest_order(o: dict) -> dict:
        """Map a raw REST order dict to our flat shape (SDK-free)."""
        def _f(v):
            return float(v) if v not in (None, "") else None
        return {
            "id":         str(o.get("id")),
            "symbol":     o.get("symbol"),
            "qty":        float(o.get("qty") or 0),
            "side":       o.get("side") or "",
            "position_intent": o.get("position_intent"),
            "type":       o.get("order_type") or o.get("type") or "",
            "status":     o.get("status") or "",
            "limit":      _f(o.get("limit_price")),
            "stop":       _f(o.get("stop_price")),
            "filled_qty": float(o.get("filled_qty") or 0),
            "filled_avg": _f(o.get("filled_avg_price")),
            "created_at": o.get("created_at"),
            "filled_at":  o.get("filled_at"),
            "updated_at": o.get("updated_at"),
        }

    def _list_orders(self, status: str = "open", after: str = None,
                     limit: int = 500) -> list[dict]:
        """List orders via raw REST. Skips multi-leg (mleg) parent orders — the
        SDK can't parse them and they aren't single instruments; condor orders
        are tracked separately via get_order_raw / the iv_condors ledger."""
        import urllib.parse
        params = {"status": status, "limit": str(limit)}
        if after:
            params["after"] = after
        url = f"{self._trade_base}/v2/orders?{urllib.parse.urlencode(params)}"
        code, body = self._rest("GET", url)
        if code != 200 or not isinstance(body, list):
            err = body.get("error") if isinstance(body, dict) else f"HTTP {code}"
            logger.error(f"list_orders error: {err}")
            return []
        return [self._map_rest_order(o) for o in body
                if o.get("order_class") != "mleg"]

    def get_orders(self, status: str = "open") -> list[dict]:
        return self._list_orders(status=status)

    def get_order_history(self, days: int = 30, limit: int = 500) -> list[dict]:
        """Fetch closed/filled orders for performance tracking (mleg-safe)."""
        from datetime import datetime, timedelta
        after = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
        return self._list_orders(status="closed", after=after, limit=limit)

    # ------------------------------------------------------------------ #
    #  Stock Orders                                                        #
    # ------------------------------------------------------------------ #
    def market_order(
        self,
        ticker: str,
        qty: float,
        side: Literal["buy", "sell"],
        tif: str = "day",
        client_order_id: Optional[str] = None,
    ) -> dict:
        try:
            req = MarketOrderRequest(
                symbol=ticker.upper(),
                qty=qty,
                side=OrderSide(side),
                time_in_force=TimeInForce(tif),
                client_order_id=client_order_id,
            )
            order = self.client.submit_order(req)
            logger.info(f"Market order submitted: {side} {qty} {ticker} | id={order.id}")
            return {"id": str(order.id), "status": order.status.value}
        except Exception as e:
            logger.error(f"market_order error: {e}")
            return self._submission_error(e, client_order_id)

    def limit_order(
        self,
        ticker: str,
        qty: float,
        side: Literal["buy", "sell"],
        limit_price: float,
        tif: str = "day",
        client_order_id: Optional[str] = None,
    ) -> dict:
        try:
            req = LimitOrderRequest(
                symbol=ticker.upper(),
                qty=qty,
                side=OrderSide(side),
                time_in_force=TimeInForce(tif),
                limit_price=limit_price,
                client_order_id=client_order_id,
            )
            order = self.client.submit_order(req)
            logger.info(f"Limit order submitted: {side} {qty} {ticker} @ {limit_price} | id={order.id}")
            return {"id": str(order.id), "status": order.status.value}
        except Exception as e:
            logger.error(f"limit_order error: {e}")
            return self._submission_error(e, client_order_id)

    def bracket_order(
        self,
        ticker: str,
        qty: float,
        side: Literal["buy", "sell"],
        limit_price: float,
        take_profit_price: float,
        stop_loss_price: float,
        tif: str = "day",
        client_order_id: Optional[str] = None,
    ) -> dict:
        """Bracket order: entry limit + server-side TP limit + SL stop."""
        try:
            req = LimitOrderRequest(
                symbol=ticker.upper(),
                qty=qty,
                side=OrderSide(side),
                time_in_force=TimeInForce(tif),
                limit_price=limit_price,
                order_class=OrderClass.BRACKET,
                client_order_id=client_order_id,
                take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)),
                stop_loss=StopLossRequest(stop_price=round(stop_loss_price, 2)),
            )
            order = self.client.submit_order(req)
            logger.info(
                f"Bracket order submitted: {side} {qty} {ticker} @ {limit_price} "
                f"TP={take_profit_price:.2f} SL={stop_loss_price:.2f} | id={order.id}"
            )
            return {"id": str(order.id), "status": order.status.value, "protection": "bracket"}
        except Exception as e:
            logger.error(f"bracket_order error: {e}")
            result = self._submission_error(e, client_order_id)
            # Only a definitive broker rejection of bracket support permits a
            # new, plain-limit submission. Timeouts and 5xx are ambiguous.
            message = str(e).lower()
            result["unsupported_bracket"] = (
                not result.get("ambiguous", True) and "error" in result
                and ("bracket" in message or "complex" in message)
                and ("not supported" in message or "unsupported" in message))
            return result

    def _submission_error(self, error: Exception, client_order_id: Optional[str]) -> dict:
        from alpaca.common.exceptions import APIError
        code = error.status_code if isinstance(error, APIError) else None
        ambiguous = not isinstance(error, ValueError) and not (code and 400 <= code < 500)
        if client_order_id and _client_id_conflict(error):
            # This proves a reused identity, not that the original order failed.
            ambiguous = True
        if client_order_id:
            found = self.get_order_by_client_id(client_order_id)
            if found.get("id"):
                return {"id": found["id"], "status": found.get("status"), "protection": found.get("order_class")}
        return {"error": str(error), "ambiguous": ambiguous}

    def get_order_by_client_id(self, client_order_id: str) -> dict:
        import urllib.parse
        query = urllib.parse.urlencode({"client_order_id": client_order_id})
        code, body = self._rest("GET", f"{self._trade_base}/v2/orders:by_client_order_id?{query}")
        return body if code == 200 else {"error": body.get("error", f"HTTP {code}"),
                                        "not_found": code == 404}

    def cancel_order_raw(self, order_id: str) -> bool:
        code, _ = self._rest("DELETE", f"{self._trade_base}/v2/orders/{order_id}")
        return code in (200, 204)

    def get_option_activities(self, after: str) -> Optional[list[dict]]:
        """Complete option-event history, or None on failure/incomplete paging.

        OPEXP proves worthless expiry. OPASN/OPEXC/OPXRC need human review;
        they are never turned into intrinsic-based realized P&L.
        """
        import urllib.parse
        from datetime import datetime, timezone
        start = datetime.fromisoformat(after.replace("Z", "+00:00"))
        start = start.replace(tzinfo=start.tzinfo or timezone.utc,
                              hour=0, minute=0, second=0, microsecond=0)
        out, token = [], None
        for _ in range(100):
            params = {"after": start.isoformat(), "direction": "asc", "page_size": 100}
            if token:
                params["page_token"] = token
            code, body = self._rest("GET", f"{self._trade_base}/v2/account/activities?{urllib.parse.urlencode(params)}")
            if code != 200 or not isinstance(body, list):
                return None
            out.extend(x for x in body if x.get("activity_type") in ("OPEXP", "OPASN", "OPEXC", "OPXRC"))
            if len(body) < 100:
                return out
            next_token = body[-1].get("id")
            if not next_token or next_token == token:
                return None
            token = next_token
        return None

    def get_fill_activities(self, after: Optional[str] = None) -> Optional[list[dict]]:
        """All executions (optionally since `after`), or None on paging failure.

        Order filled_at is absent for canceled partials and describes only the
        final execution for completed orders. Account FILL activities provide
        the individual quantities, prices and transaction timestamps instead.
        """
        import urllib.parse
        out, token, seen = [], None, set()
        for _ in range(100):
            params = {"activity_types": "FILL", "direction": "asc", "page_size": 100}
            if after:
                params["after"] = after
            if token:
                params["page_token"] = token
            code, body = self._rest(
                "GET", f"{self._trade_base}/v2/account/activities?{urllib.parse.urlencode(params)}")
            if (code != 200 or not isinstance(body, list)
                    or any(not isinstance(a, dict) or a.get("activity_type") != "FILL" for a in body)):
                return None
            out.extend(body)
            if len(body) < 100:
                return out
            next_token = body[-1].get("id")
            if not isinstance(next_token, str) or not next_token or next_token in seen:
                return None
            seen.add(next_token)
            token = next_token
        return None

    def account_fingerprint(self) -> Optional[str]:
        """Stable identifier of the broker account behind these keys, or None
        when it cannot be read. A hash: the account ID itself is never stored
        or logged. Paper and live accounts can never share a fingerprint."""
        import hashlib
        code, body = self._rest("GET", f"{self._trade_base}/v2/account")
        account_id = body.get("id") if code == 200 and isinstance(body, dict) else None
        if not isinstance(account_id, str) or not account_id:
            return None
        return ("paper:" if self.paper else "live:") + hashlib.sha256(account_id.encode()).hexdigest()[:16]

    # Cash moving in or out of the account: deposits, withdrawals, journals and
    # ACAT cash. (The paper account's opening balance is a JNLC.)
    CASH_TRANSFER_TYPES = ("CSD", "CSW", "JNLC", "ACATC", "TRANS")

    def get_cash_transfers(self) -> Optional[list[dict]]:
        """Every cash transfer on the account, or None on failure/incomplete
        paging. Canceled entries are left out. Used to measure P&L against the
        cash actually contributed, so a deposit is not mistaken for profit."""
        import math
        import urllib.parse
        out, token, seen = [], None, set()
        for _ in range(100):
            params = {"activity_types": ",".join(self.CASH_TRANSFER_TYPES),
                      "direction": "asc", "page_size": 100}
            if token:
                params["page_token"] = token
            code, body = self._rest(
                "GET", f"{self._trade_base}/v2/account/activities?{urllib.parse.urlencode(params)}")
            if code != 200 or not isinstance(body, list):
                return None
            for row in body:
                try:
                    kind, amount = row["activity_type"], float(row["net_amount"])
                    if (kind not in self.CASH_TRANSFER_TYPES or not math.isfinite(amount)
                            or not isinstance(row["id"], str) or not row["id"]):
                        return None
                    if str(row.get("status") or "").lower() != "canceled":
                        out.append({"id": row["id"], "activity_type": kind, "amount": amount,
                                    "date": str(row.get("date") or "")[:10]})
                except (KeyError, TypeError, ValueError):
                    return None
            if len(body) < 100:
                return out
            next_token = body[-1].get("id")
            if not isinstance(next_token, str) or not next_token or next_token in seen:
                return None
            seen.add(next_token)
            token = next_token
        return None

    def get_mleg_leg_order_ids(self) -> Optional[set[str]]:
        """Order IDs of every multi-leg order's legs, or None if the listing is
        unavailable or incomplete.

        A FILL activity for a condor leg carries the leg's own order ID, but
        GET /v2/orders/{leg id} answers 404: legs exist only nested under their
        parent order. The nested listing is the one place those IDs can be read
        (verified on paper 2026-10-05: 16 parents, 64 legs, matching all 44 leg
        fills). Pages ascend by submission time.
        """
        import urllib.parse
        ids, after, seen = set(), None, set()
        for _ in range(100):
            params = {"status": "all", "nested": "true", "limit": 500, "direction": "asc"}
            if after:
                params["after"] = after
            code, body = self._rest(
                "GET", f"{self._trade_base}/v2/orders?{urllib.parse.urlencode(params)}")
            if (code != 200 or not isinstance(body, list)
                    or any(not isinstance(o, dict) for o in body)):
                return None
            for order in body:
                if order.get("order_class") != "mleg":
                    continue
                legs = order.get("legs")
                if not isinstance(legs, list) or any(
                        not isinstance(leg, dict) or not leg.get("id") for leg in legs):
                    return None          # a parent whose legs cannot be read
                ids.update(str(leg["id"]) for leg in legs)
            if len(body) < 500:
                return ids
            cursor = body[-1].get("submitted_at")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                return None
            seen.add(cursor)
            after = cursor
        return None

    def get_positions_raw(self) -> Optional[list[dict]]:
        code, body = self._rest("GET", f"{self._trade_base}/v2/positions")
        return body if code == 200 and isinstance(body, list) else None

    def get_open_orders_raw(self) -> Optional[list[dict]]:
        """Risk-check snapshot, retaining MLegs and distinguishing errors from empty.

        At the API's page limit we cannot prove the snapshot complete, so fail
        closed instead of overlooking outstanding entry orders.
        """
        code, body = self._rest("GET", f"{self._trade_base}/v2/orders?status=open&limit=500&nested=true")
        return body if code == 200 and isinstance(body, list) and len(body) < 500 else None

    def trailing_stop(
        self,
        ticker: str,
        qty: float,
        side: Literal["buy", "sell"],
        trail_percent: float,
    ) -> dict:
        try:
            req = TrailingStopOrderRequest(
                symbol=ticker.upper(),
                qty=qty,
                side=OrderSide(side),
                time_in_force=TimeInForce.day,
                trail_percent=trail_percent,
            )
            order = self.client.submit_order(req)
            return {"id": str(order.id), "status": order.status.value}
        except Exception as e:
            logger.error(f"trailing_stop error: {e}")
            return {"error": str(e)}

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel_order_by_id(order_id)
            return True
        except Exception as e:
            logger.error(f"cancel_order error: {e}")
            return False

    def cancel_all_orders(self) -> bool:
        try:
            self.client.cancel_orders()
            return True
        except Exception as e:
            logger.error(f"cancel_all_orders error: {e}")
            return False

    def close_position(self, ticker: str) -> dict:
        try:
            resp = self.client.close_position(ticker.upper())
            return {"id": str(resp.id), "status": resp.status.value}
        except Exception as e:
            logger.error(f"close_position error: {e}")
            return {"error": str(e)}

    # ------------------------------------------------------------------ #
    #  Options — chain data + multi-leg (spreads / iron condors)          #
    # ------------------------------------------------------------------ #
    def _rest(self, method: str, url: str, body: Optional[dict] = None) -> tuple:
        """Minimal blocking REST call (stdlib). Returns (status_code, parsed).
        Retries once on a transient connection drop — but ONLY for GET: a POST
        that dropped after the server received it must never be resent (a retried
        order submission would double-fill)."""
        import json, time, urllib.request, urllib.error
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._rest_headers, method=method)
        attempts = 2 if method.upper() == "GET" else 1
        last_err = None
        for i in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    txt = r.read().decode()
                    return r.status, (json.loads(txt) if txt else {})
            except urllib.error.HTTPError as e:
                return e.code, {"error": e.read().decode()[:600]}   # real API error — don't retry
            except Exception as e:
                last_err = e
                if i < attempts - 1:
                    time.sleep(0.5)
        return 0, {"error": str(last_err)}

    def get_order_raw(self, order_id: str) -> dict:
        """Raw order dict via REST (works for mleg orders the SDK can't parse)."""
        code, body = self._rest("GET", f"{self._trade_base}/v2/orders/{order_id}")
        return body if code == 200 else {"error": body.get("error", f"HTTP {code}")}

    def get_option_contracts(self, underlying: str, exp_gte, exp_lte,
                             opt_type: Optional[str] = None, limit: int = 500) -> list[dict]:
        """List tradable option contracts for an underlying within a date window."""
        try:
            from alpaca.trading.requests import GetOptionContractsRequest
            kw = dict(underlying_symbols=[underlying.upper()],
                      expiration_date_gte=exp_gte, expiration_date_lte=exp_lte, limit=limit)
            if opt_type:
                kw["type"] = opt_type
            res = self.client.get_option_contracts(GetOptionContractsRequest(**kw))
            return [
                {"symbol": c.symbol, "strike": float(c.strike_price),
                 "expiry": c.expiration_date, "type": c.type.value if c.type else None,
                 "open_interest": int(c.open_interest or 0) if getattr(c, "open_interest", None) else 0}
                for c in (res.option_contracts or [])
            ]
        except Exception as e:
            logger.error(f"get_option_contracts error: {e}")
            return []

    @staticmethod
    def _now():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)

    @staticmethod
    def _quote_is_current(at, now) -> bool:
        """Whether a quote last changed at `at` is still the standing market.

        A latest-quote timestamp says when the NBBO last CHANGED, not when it
        was last valid. Cheap options sit unchanged for long stretches: on the
        paper feed at 11:40 ET on 2026-10-06, 62% of contracts asking <= $0.05
        (and 65% of zero-bid books) had not moved in over two minutes, median
        30 minutes. Those are a winning condor's wings after the print, so an
        age cutoff would have blocked its profit target and post-earnings close.

        What must be refused is a mark carried over from an earlier session —
        the overnight book that mispriced exits on 2026-10-01 — or one stamped
        in the future. Before today's 09:30 ET open nothing is current.
        """
        from market_time import ET
        if (now - at).total_seconds() < -5:
            return False
        session_open = now.astimezone(ET).replace(hour=9, minute=30, second=0, microsecond=0)
        return at.astimezone(ET) >= session_open

    def get_option_quotes(self, symbols: list[str]) -> dict:
        """Finite, uncrossed, current-session quotes with timestamp/feed provenance.

        A real zero bid is retained for close/measurement consumers, but never
        turned into an invented one-sided mid. Invalid books, and marks from an
        earlier session, are absent. `age_seconds` records how long ago the
        book last changed.
        """
        if not symbols:
            return {}
        import urllib.parse
        import math
        from datetime import datetime, timezone
        now = self._now()
        quote_feed = getattr(self, "options_feed", "auto")
        out: dict = {}
        # batch to keep URLs sane
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            params = {"symbols": ",".join(chunk)}
            if quote_feed != "auto":
                params["feed"] = quote_feed
            q = urllib.parse.urlencode(params)
            url = f"{self._data_base}/v1beta1/options/quotes/latest?{q}"
            code, body = self._rest("GET", url)
            if code == 200:
                for sym, qt in (body.get("quotes") or {}).items():
                    try:
                        bid, ask = float(qt["bp"]), float(qt["ap"])
                        at = datetime.fromisoformat(qt["t"].replace("Z", "+00:00"))
                        if (at.tzinfo is None or not all(math.isfinite(v) for v in (bid, ask))
                                or bid < 0 or ask < 0 or bid > ask
                                or not self._quote_is_current(at, now)):
                            continue
                    except (KeyError, AttributeError, TypeError, ValueError):
                        continue
                    out[sym] = {"bid": bid, "ask": ask, "mid": bid / 2 + ask / 2 if bid > 0 else 0,
                                "timestamp": at.astimezone(timezone.utc).isoformat(), "feed": quote_feed,
                                "age_seconds": round((now - at).total_seconds(), 1),
                                "bid_size": qt.get("bs"), "ask_size": qt.get("as")}
            else:
                logger.debug(f"get_option_quotes {code}: {body.get('error')}")
        return out

    def multileg_order(self, legs: list[dict], qty: int, limit_price: float,
                       order_type: str = "limit", tif: str = "day",
                       client_order_id: Optional[str] = None) -> dict:
        """Submit a multi-leg (mleg) options order via REST.

        legs: [{symbol, side('buy'|'sell'), position_intent, ratio_qty(int=1)}]
        limit_price: net price of the spread. NEGATIVE = net credit (we receive),
                     POSITIVE = net debit (we pay) — Alpaca's mleg convention.
        Defined-risk spreads only at options level 3 (no naked shorts).
        """
        payload = {
            "order_class": "mleg",
            "qty": str(int(qty)),
            "type": order_type,
            "time_in_force": tif,
            "legs": [
                {"symbol": l["symbol"], "ratio_qty": str(int(l.get("ratio_qty", 1))),
                 "side": l["side"], "position_intent": l["position_intent"]}
                for l in legs
            ],
        }
        if order_type == "limit":
            payload["limit_price"] = str(round(float(limit_price), 2))
        if client_order_id:
            payload["client_order_id"] = client_order_id
        code, body = self._rest("POST", f"{self._trade_base}/v2/orders", payload)
        if code in (200, 201) and body.get("id"):
            logger.info(f"MLEG order submitted: {len(legs)} legs qty={qty} "
                        f"net={limit_price:+.2f} | id={body['id']} status={body.get('status')}")
            return {"id": body["id"], "status": body.get("status"),
                    "legs": body.get("legs", [])}
        logger.error(f"multileg_order failed ({code}): {body.get('error')}")
        if client_order_id:
            found = self.get_order_by_client_id(client_order_id)
            if found.get("id"):
                return {"id": found["id"], "status": found.get("status"), "protection": found.get("order_class")}
        return {"error": body.get("error", f"HTTP {code}"),
                "ambiguous": not (400 <= code < 500) or bool(
                    client_order_id and _client_id_conflict(body.get("error")))}

    def close_multileg(self, legs: list[dict], qty: int, limit_price: float,
                       tif: str = "day", client_order_id: Optional[str] = None) -> dict:
        """Close an existing spread by submitting the inverse legs (…_to_close)."""
        inv = []
        for l in legs:
            side = "buy" if l["side"] == "sell" else "sell"
            intent = "buy_to_close" if l["side"] == "sell" else "sell_to_close"
            inv.append({"symbol": l["symbol"], "side": side,
                        "position_intent": intent, "ratio_qty": l.get("ratio_qty", 1)})
        return self.multileg_order(inv, qty, limit_price, "limit", tif, client_order_id)

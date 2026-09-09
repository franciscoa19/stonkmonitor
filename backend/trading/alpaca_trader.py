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


class AlpacaTrader:
    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        self.paper = paper
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

    # ------------------------------------------------------------------ #
    #  Account Info                                                        #
    # ------------------------------------------------------------------ #
    def get_account(self) -> dict:
        try:
            acct = self.client.get_account()
            return {
                "equity":         float(acct.equity or 0),
                "cash":           float(acct.cash or 0),
                "buying_power":   float(acct.buying_power or 0),
                "day_trade_count":int(acct.daytrade_count or 0),
                "pdt_flag":       bool(acct.pattern_day_trader),
                "status":         acct.status.value if acct.status else "unknown",
            }
        except Exception as e:
            logger.error(f"get_account error: {e}")
            return {}

    def get_positions(self) -> list[dict]:
        try:
            positions = self.client.get_all_positions()
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

    def get_orders(self, status: str = "open") -> list[dict]:
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            req = GetOrdersRequest(status=QueryOrderStatus(status))
            orders = self.client.get_orders(filter=req)
            return [
                {
                    "id":         str(o.id),
                    "symbol":     o.symbol,
                    "qty":        float(o.qty or 0),
                    "side":       o.side.value,
                    "type":       o.order_type.value,
                    "status":     o.status.value,
                    "limit":      float(o.limit_price) if o.limit_price else None,
                    "stop":       float(o.stop_price) if o.stop_price else None,
                    "filled_qty": float(o.filled_qty or 0),
                    "filled_avg": float(o.filled_avg_price) if o.filled_avg_price else None,
                    "created_at": o.created_at.isoformat() if o.created_at else None,
                }
                for o in orders
            ]
        except Exception as e:
            logger.error(f"get_orders error: {e}")
            return []

    def get_order_history(self, days: int = 30, limit: int = 500) -> list[dict]:
        """Fetch closed/filled orders for performance tracking."""
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            from datetime import datetime, timedelta
            after = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                after=after,
                limit=limit,
            )
            orders = self.client.get_orders(filter=req)
            return [
                {
                    "id":         str(o.id),
                    "symbol":     o.symbol,
                    "qty":        float(o.qty or 0),
                    "side":       o.side.value,
                    "type":       o.order_type.value,
                    "status":     o.status.value,
                    "limit":      float(o.limit_price) if o.limit_price else None,
                    "stop":       float(o.stop_price) if o.stop_price else None,
                    "filled_qty": float(o.filled_qty or 0),
                    "filled_avg": float(o.filled_avg_price) if o.filled_avg_price else None,
                    "created_at": o.created_at.isoformat() if o.created_at else None,
                    "filled_at":  o.filled_at.isoformat() if o.filled_at else None,
                    "updated_at": o.updated_at.isoformat() if o.updated_at else None,
                }
                for o in orders
            ]
        except Exception as e:
            logger.error(f"get_order_history error: {e}")
            return []

    # ------------------------------------------------------------------ #
    #  Stock Orders                                                        #
    # ------------------------------------------------------------------ #
    def market_order(
        self,
        ticker: str,
        qty: float,
        side: Literal["buy", "sell"],
        tif: str = "day",
    ) -> dict:
        try:
            req = MarketOrderRequest(
                symbol=ticker.upper(),
                qty=qty,
                side=OrderSide(side),
                time_in_force=TimeInForce(tif),
            )
            order = self.client.submit_order(req)
            logger.info(f"Market order submitted: {side} {qty} {ticker} | id={order.id}")
            return {"id": str(order.id), "status": order.status.value}
        except Exception as e:
            logger.error(f"market_order error: {e}")
            return {"error": str(e)}

    def limit_order(
        self,
        ticker: str,
        qty: float,
        side: Literal["buy", "sell"],
        limit_price: float,
        tif: str = "day",
    ) -> dict:
        try:
            req = LimitOrderRequest(
                symbol=ticker.upper(),
                qty=qty,
                side=OrderSide(side),
                time_in_force=TimeInForce(tif),
                limit_price=limit_price,
            )
            order = self.client.submit_order(req)
            logger.info(f"Limit order submitted: {side} {qty} {ticker} @ {limit_price} | id={order.id}")
            return {"id": str(order.id), "status": order.status.value}
        except Exception as e:
            logger.error(f"limit_order error: {e}")
            return {"error": str(e)}

    def bracket_order(
        self,
        ticker: str,
        qty: float,
        side: Literal["buy", "sell"],
        limit_price: float,
        take_profit_price: float,
        stop_loss_price: float,
        tif: str = "day",
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
                take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)),
                stop_loss=StopLossRequest(stop_price=round(stop_loss_price, 2)),
            )
            order = self.client.submit_order(req)
            logger.info(
                f"Bracket order submitted: {side} {qty} {ticker} @ {limit_price} "
                f"TP={take_profit_price:.2f} SL={stop_loss_price:.2f} | id={order.id}"
            )
            return {"id": str(order.id), "status": order.status.value}
        except Exception as e:
            logger.error(f"bracket_order error: {e}")
            return {"error": str(e)}

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
        """Minimal blocking REST call (stdlib). Returns (status_code, parsed)."""
        import json, urllib.request, urllib.error
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._rest_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                txt = r.read().decode()
                return r.status, (json.loads(txt) if txt else {})
        except urllib.error.HTTPError as e:
            return e.code, {"error": e.read().decode()[:600]}
        except Exception as e:
            return 0, {"error": str(e)}

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

    def get_option_quotes(self, symbols: list[str]) -> dict:
        """Latest bid/ask per OCC symbol. Returns {symbol: {bid, ask, mid}}."""
        if not symbols:
            return {}
        import urllib.parse
        out: dict = {}
        # batch to keep URLs sane
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            q = urllib.parse.urlencode({"symbols": ",".join(chunk)})
            url = f"{self._data_base}/v1beta1/options/quotes/latest?{q}"
            code, body = self._rest("GET", url)
            if code == 200:
                for sym, qt in (body.get("quotes") or {}).items():
                    bid, ask = float(qt.get("bp") or 0), float(qt.get("ap") or 0)
                    mid = (bid + ask) / 2 if (bid and ask) else (bid or ask)
                    out[sym] = {"bid": bid, "ask": ask, "mid": mid}
            else:
                logger.debug(f"get_option_quotes {code}: {body.get('error')}")
        return out

    def multileg_order(self, legs: list[dict], qty: int, limit_price: float,
                       order_type: str = "limit", tif: str = "day") -> dict:
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
        code, body = self._rest("POST", f"{self._trade_base}/v2/orders", payload)
        if code in (200, 201) and body.get("id"):
            logger.info(f"MLEG order submitted: {len(legs)} legs qty={qty} "
                        f"net={limit_price:+.2f} | id={body['id']} status={body.get('status')}")
            return {"id": body["id"], "status": body.get("status"),
                    "legs": body.get("legs", [])}
        logger.error(f"multileg_order failed ({code}): {body.get('error')}")
        return {"error": body.get("error", f"HTTP {code}")}

    def close_multileg(self, legs: list[dict], qty: int, limit_price: float,
                       tif: str = "day") -> dict:
        """Close an existing spread by submitting the inverse legs (…_to_close)."""
        inv = []
        for l in legs:
            side = "buy" if l["side"] == "sell" else "sell"
            intent = "buy_to_close" if l["side"] == "sell" else "sell_to_close"
            inv.append({"symbol": l["symbol"], "side": side,
                        "position_intent": intent, "ratio_qty": l.get("ratio_qty", 1)})
        return self.multileg_order(inv, qty, limit_price, "limit", tif)

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
        mode = "PAPER" if paper else "LIVE"
        logger.info(f"AlpacaTrader initialized in {mode} mode")

    # ------------------------------------------------------------------ #
    #  Account Info                                                        #
    # ------------------------------------------------------------------ #
    def get_account(self) -> dict:
        try:
            acct = self.client.get_account()
            return {
                "equity":         float(acct.equity),
                "cash":           float(acct.cash),
                "buying_power":   float(acct.buying_power),
                "day_trade_count":int(acct.daytrade_count),
                "pdt_flag":       acct.pattern_day_trader,
                "status":         acct.status.value,
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

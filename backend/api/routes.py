"""
REST API routes for the frontend.
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, Literal
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


# ------------------------------------------------------------------ #
#  Request Models                                                      #
# ------------------------------------------------------------------ #
class OrderRequest(BaseModel):
    ticker: str
    qty: float
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"] = "market"
    limit_price: Optional[float] = None
    tif: str = "day"


class WatchlistRequest(BaseModel):
    ticker: str


# ------------------------------------------------------------------ #
#  Account & Positions                                                 #
# ------------------------------------------------------------------ #
@router.get("/account")
async def get_account(trader=Depends(lambda: None)):
    """Get Alpaca account info."""
    from main import trader as t
    return t.get_account()


@router.get("/positions")
async def get_positions():
    from main import trader as t
    return t.get_positions()


@router.get("/orders")
async def get_orders(status: str = "open"):
    from main import trader as t
    return t.get_orders(status=status)


# ------------------------------------------------------------------ #
#  Order Execution                                                     #
# ------------------------------------------------------------------ #
@router.post("/order")
async def place_order(req: OrderRequest):
    from main import trader as t
    if req.order_type == "market":
        result = t.market_order(req.ticker, req.qty, req.side, req.tif)
    elif req.order_type == "limit":
        if not req.limit_price:
            raise HTTPException(400, "limit_price required for limit orders")
        result = t.limit_order(req.ticker, req.qty, req.side, req.limit_price, req.tif)
    else:
        raise HTTPException(400, "Unsupported order type")

    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.delete("/order/{order_id}")
async def cancel_order(order_id: str):
    from main import trader as t
    ok = t.cancel_order(order_id)
    if not ok:
        raise HTTPException(400, "Failed to cancel order")
    return {"status": "cancelled"}


@router.delete("/positions/{ticker}")
async def close_position(ticker: str):
    from main import trader as t
    result = t.close_position(ticker)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


# ------------------------------------------------------------------ #
#  Market Data                                                         #
# ------------------------------------------------------------------ #
@router.get("/quote/{ticker}")
async def get_quote(ticker: str):
    from main import feed as f
    return f.get_latest_quote(ticker)


@router.get("/options/{ticker}")
async def get_option_chain(ticker: str, expiry_days: int = 45):
    from main import feed as f
    return f.get_option_chain(ticker, expiry_days)


@router.get("/bars/{ticker}")
async def get_bars(ticker: str, days: int = 30, timeframe: str = "1Day"):
    from main import feed as f
    return f.get_bars(ticker, days, timeframe)


# ------------------------------------------------------------------ #
#  Live Feed (REST snapshot, WS for streaming)                         #
# ------------------------------------------------------------------ #
@router.get("/flow")
async def get_flow_snapshot(ticker: Optional[str] = None, limit: int = 50):
    """REST snapshot of recent options flow."""
    from main import uw_client
    return await uw_client.get_options_flow(ticker=ticker, limit=limit)


@router.get("/darkpool")
async def get_darkpool_snapshot(ticker: Optional[str] = None, limit: int = 50):
    from main import uw_client
    return await uw_client.get_darkpool_flow(ticker=ticker, limit=limit)


@router.get("/insider")
async def get_insider_snapshot(ticker: Optional[str] = None, limit: int = 50):
    from main import uw_client
    return await uw_client.get_insider_trades(ticker=ticker, limit=limit)


@router.get("/congress")
async def get_congress_snapshot(limit: int = 50):
    from main import uw_client
    return await uw_client.get_congress_trades(limit=limit)


@router.get("/iv/{ticker}")
async def get_iv_rank(ticker: str):
    from main import uw_client
    return await uw_client.get_iv_rank(ticker)


@router.get("/options-chain/{ticker}")
async def get_option_contracts(ticker: str):
    from main import uw_client
    return await uw_client.get_option_contracts(ticker)


@router.get("/uw/budget")
async def get_uw_budget():
    """Snapshot of UW daily call budget + current market session."""
    from feeds.uw_budget import budget
    return budget.status()


# ------------------------------------------------------------------ #
#  Database — per-feed tables                                          #
# ------------------------------------------------------------------ #
@router.get("/db/stats")
async def db_stats():
    from main import db
    return await db.get_db_stats()

@router.get("/db/signals")
async def db_get_signals(
    ticker: Optional[str] = None,
    type: Optional[str] = None,
    min_score: float = 0.0,
    limit: int = 100,
    offset: int = 0,
):
    from main import db
    return await db.get_signals(ticker=ticker, signal_type=type,
                                min_score=min_score, limit=limit, offset=offset)

@router.get("/db/options-flow")
async def db_options_flow(
    ticker: Optional[str] = None,
    min_premium: float = 0,
    alert_rule: Optional[str] = None,
    has_sweep: Optional[bool] = None,
    limit: int = 100, offset: int = 0,
):
    from main import db
    return await db.get_options_flow(ticker=ticker, min_premium=min_premium,
                                     alert_rule=alert_rule, has_sweep=has_sweep,
                                     limit=limit, offset=offset)

@router.get("/db/dark-pool")
async def db_dark_pool(
    ticker: Optional[str] = None,
    min_premium: float = 0,
    limit: int = 100, offset: int = 0,
):
    from main import db
    return await db.get_dark_pool(ticker=ticker, min_premium=min_premium,
                                  limit=limit, offset=offset)

@router.get("/db/insider")
async def db_insider(
    ticker: Optional[str] = None,
    code: Optional[str] = None,
    min_value: float = 0,
    limit: int = 100, offset: int = 0,
):
    from main import db
    return await db.get_insider_trades(ticker=ticker, code=code,
                                       min_value=min_value, limit=limit, offset=offset)

@router.get("/db/congress")
async def db_congress(
    ticker: Optional[str] = None,
    txn_type: Optional[str] = None,
    limit: int = 100, offset: int = 0,
):
    from main import db
    return await db.get_congress_trades(ticker=ticker, txn_type=txn_type,
                                        limit=limit, offset=offset)

@router.get("/db/patterns")
async def db_patterns(
    ticker: Optional[str] = None,
    pattern: Optional[str] = None,
    limit: int = 50,
):
    from main import db
    return await db.get_pattern_hits(ticker=ticker, pattern=pattern, limit=limit)

@router.get("/db/top-tickers")
async def db_top_tickers(days: int = 7, limit: int = 20):
    from main import db
    return await db.get_top_tickers(days=days, limit=limit)

@router.get("/db/ticker/{ticker}")
async def db_ticker_profile(ticker: str):
    from main import db
    return await db.get_ticker_profile(ticker)


# ------------------------------------------------------------------ #
#  Kalshi Prediction Markets                                           #
# ------------------------------------------------------------------ #
class KalshiOrderRequest(BaseModel):
    ticker: str
    side: Literal["yes", "no"]
    count: int
    price: int    # cents (1-99)


@router.get("/kalshi/scan")
async def kalshi_scan():
    """Run the edge scanner across all open Kalshi markets."""
    from main import kalshi_client, kalshi_scanner, settings
    if not kalshi_client:
        raise HTTPException(503, "Kalshi not configured")
    balance_data = await kalshi_client.get_balance()
    balance_usd  = balance_data.get("balance", 0) / 100  # cents → dollars
    markets = await kalshi_client.get_markets()
    opps    = kalshi_scanner.scan(markets, balance_usd)
    return {
        "balance_usd": balance_usd,
        "markets_scanned": len(markets),
        "opportunities": [o.to_dict() for o in opps[:50]],
    }


@router.get("/kalshi/positions")
async def kalshi_positions():
    from main import kalshi_client
    if not kalshi_client:
        raise HTTPException(503, "Kalshi not configured")
    return await kalshi_client.get_positions()


@router.get("/kalshi/balance")
async def kalshi_balance():
    from main import kalshi_client
    if not kalshi_client:
        raise HTTPException(503, "Kalshi not configured")
    data = await kalshi_client.get_balance()
    return {"balance_usd": data.get("balance", 0) / 100}


@router.get("/kalshi/market/{ticker}")
async def kalshi_market(ticker: str):
    from main import kalshi_client
    if not kalshi_client:
        raise HTTPException(503, "Kalshi not configured")
    return await kalshi_client.get_market(ticker)


@router.post("/kalshi/order")
async def kalshi_order(req: KalshiOrderRequest):
    from main import kalshi_client
    if not kalshi_client:
        raise HTTPException(503, "Kalshi not configured")
    result = await kalshi_client.place_order(
        ticker=req.ticker,
        side=req.side,
        action="buy",
        count=req.count,
        order_type="limit",
        price=req.price,
    )
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


# ------------------------------------------------------------------ #
#  Auto-Trade Queue                                                    #
# ------------------------------------------------------------------ #
@router.get("/trade/queue")
async def get_trade_queue():
    """Get all currently pending trade suggestions."""
    from main import db
    return await db.get_pending_trades(status="pending")


@router.get("/trade/history")
async def get_trade_history(limit: int = 50):
    """Get all trades (any status) for the trade log."""
    from main import db
    return await db.get_trade_history(limit=limit)


@router.post("/trade/confirm/{trade_id}")
async def confirm_trade(trade_id: int):
    """Execute a queued trade via Alpaca."""
    from main import auto_trade
    result = await auto_trade.confirm_trade(trade_id, msg_id=0)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.post("/trade/skip/{trade_id}")
async def skip_trade(trade_id: int):
    """Skip / dismiss a queued trade."""
    from main import auto_trade
    await auto_trade.skip_trade(trade_id, msg_id=0)
    return {"status": "skipped"}


# ------------------------------------------------------------------ #
#  Performance Tracking                                                #
# ------------------------------------------------------------------ #
@router.get("/performance")
async def get_performance(ticker: Optional[str] = None, limit: int = 100):
    """Get trade performance history."""
    from main import db
    return await db.get_trade_performance(limit=limit, ticker=ticker)


@router.get("/performance/summary")
async def get_performance_summary():
    """Aggregate performance stats: win rate, P&L, profit factor."""
    from main import db
    return await db.get_performance_summary()


@router.get("/performance/positions")
async def get_current_positions_with_pnl():
    """Get current Alpaca positions with real-time P&L."""
    from main import trader as t
    return t.get_positions()


@router.get("/trade/filters")
async def get_filter_status():
    """Current state of all 8 auto-trade quality filters."""
    import time as _time
    from main import auto_trade as at, settings as s
    at._refresh_daily_pnl_date()
    spy_day, spy_trend = at._regime_cache[0], at._regime_cache[1]
    equity = at._cached_equity
    equity_loss_pct = round((at._daily_pnl / equity * 100) if equity > 0 else 0, 2)
    cooldown_tickers = [
        {"ticker": t, "hours_remaining": round(
            (s.auto_trade_ticker_cooldown_hours * 3600 - (_time.time() - ts)) / 3600, 1
        )}
        for t, ts in at._ticker_loss_ts.items()
        if at._ticker_in_cooldown(t)
    ]
    return {
        "circuit_breaker": {
            "active": at._circuit_breaker_active(),
            "daily_pnl": round(at._daily_pnl, 2),
            "daily_pnl_pct": equity_loss_pct,
            "pct_limit": s.auto_trade_daily_loss_pct,
            "dollar_limit": s.auto_trade_daily_loss_limit,
            "cached_equity": round(equity, 2),
            "date": at._daily_pnl_date,
        },
        "volume_controls": {
            "max_trades_per_day": s.auto_trade_max_trades_per_day,
            "max_open_positions": s.auto_trade_max_open_positions,
            "max_pending_alerts": s.auto_trade_max_pending,
            "pending_now": len(at._pending),
            "burst_limit": s.auto_trade_burst_limit,
            "burst_window_sec": s.auto_trade_burst_window,
            "alerts_in_window": len([t for t in at._alert_timestamps
                                     if __import__("time").time() - t < s.auto_trade_burst_window]),
        },
        "volatility_gate": {
            "spy_day_chg_pct": round(at._regime_cache[0], 2),
            "threshold_pct": s.intraday_vol_threshold,
            "bump": s.intraday_vol_bump,
            "active": abs(at._regime_cache[0]) >= s.intraday_vol_threshold,
            "effective_min_score": (s.auto_trade_score_threshold + s.intraday_vol_bump
                                    if abs(at._regime_cache[0]) >= s.intraday_vol_threshold
                                    else s.auto_trade_score_threshold),
        },
        "regime": {
            "spy_day_chg_pct": round(spy_day, 2),
            "spy_trend_chg_pct": round(spy_trend, 2),
            "bear_skip_threshold": s.auto_trade_regime_bear_skip_pct,
            "bull_skip_threshold": s.auto_trade_regime_bull_skip_pct,
        },
        "ticker_cooldowns": cooldown_tickers,
        "settings": {
            "score_threshold": s.auto_trade_score_threshold,
            "pattern_threshold": s.auto_trade_pattern_threshold,
            "put_min_score": s.auto_trade_put_min_score,
            "min_dte": s.auto_trade_min_dte,
            "max_dte": s.auto_trade_max_dte,
            "max_option_price": s.auto_trade_max_option_price,
            "max_otm_pct": s.auto_trade_max_otm_pct,
            "ticker_cooldown_hours": s.auto_trade_ticker_cooldown_hours,
            "equity_long_risk_pct": s.equity_long_risk_pct,
        }
    }


# ------------------------------------------------------------------ #
#  Watchlist                                                           #
# ------------------------------------------------------------------ #
_watchlist: list[str] = []


@router.get("/watchlist")
async def get_watchlist():
    return {"tickers": _watchlist}


@router.post("/watchlist")
async def add_to_watchlist(req: WatchlistRequest):
    ticker = req.ticker.upper()
    if ticker not in _watchlist:
        _watchlist.append(ticker)
    return {"tickers": _watchlist}


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(ticker: str):
    t = ticker.upper()
    if t in _watchlist:
        _watchlist.remove(t)
    return {"tickers": _watchlist}

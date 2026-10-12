"""
Alpaca market data feed — real-time quotes, bars, options chains.
Uses alpaca-py SDK.
"""
import logging
from typing import Optional
from alpaca.data import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import (
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
    StockBarsRequest,
    OptionChainRequest,
    OptionLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.data.live import StockDataStream
from datetime import date, datetime, time, timedelta

from market_time import et_now

logger = logging.getLogger(__name__)

# A quote midpoint is only a price when both sides are real. This feed is one
# exchange, and around the open and close (and whenever it is thin) one side of
# its book is a placeholder: PEP showed $120.23 x $126.31 at 09:30:25 on
# 2026-10-09, a "midpoint" of $123.27 while the stock traded at $126.25.
MAX_SPOT_SPREAD_PCT = 0.005


def spot_from(trade: Optional[dict], quote: Optional[dict], *, now: Optional[datetime] = None,
              max_age_seconds: Optional[float] = 900) -> Optional[float]:
    """The stock's price, from evidence that is actually a price.

    The last trade wins when it is recent enough: it is a price somebody paid.
    Otherwise a quote counts only if it is two-sided and tight. A one-sided or
    wide quote is not averaged into a number — the answer is None, and the
    caller waits. `max_age_seconds=None` accepts a trade of any age, for
    callers that want the latest price whenever the market last traded.
    """
    from math import isfinite
    now = now or datetime.now().astimezone()

    def fresh(stamp) -> bool:
        if max_age_seconds is None:
            return True
        try:
            at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if at.tzinfo is None:
                return False
            return 0 <= (now - at).total_seconds() <= max_age_seconds
        except (TypeError, ValueError):
            return False

    try:
        price = float((trade or {}).get("price"))
        if isfinite(price) and price > 0 and fresh((trade or {}).get("timestamp")):
            return price
    except (TypeError, ValueError):
        pass
    try:
        bid, ask = float((quote or {}).get("bid")), float((quote or {}).get("ask"))
    except (TypeError, ValueError):
        return None
    if not (isfinite(bid) and isfinite(ask)) or bid <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    if (ask - bid) / mid > MAX_SPOT_SPREAD_PCT or not fresh((quote or {}).get("timestamp")):
        return None
    return mid


class AlpacaFeed:
    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self.stock_client = StockHistoricalDataClient(api_key, secret_key)
        self.option_client = OptionHistoricalDataClient(api_key, secret_key)

    def get_latest_quote(self, ticker: str) -> dict:
        """Get latest bid/ask/last for a stock."""
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=ticker.upper())
            result = self.stock_client.get_stock_latest_quote(req)
            quote = result.get(ticker.upper())
            if not quote:
                return {}
            return {
                "ticker": ticker.upper(),
                "bid": float(quote.bid_price),
                "ask": float(quote.ask_price),
                "bid_size": int(quote.bid_size),
                "ask_size": int(quote.ask_size),
                "timestamp": quote.timestamp.isoformat(),
            }
        except Exception as e:
            logger.error(f"Alpaca quote error for {ticker}: {e}")
            return {}

    def get_latest_trade(self, ticker: str) -> dict:
        """The most recent trade print: {price, timestamp}, or {} if unavailable."""
        try:
            req = StockLatestTradeRequest(symbol_or_symbols=ticker.upper())
            trade = self.stock_client.get_stock_latest_trade(req).get(ticker.upper())
            if not trade:
                return {}
            return {"ticker": ticker.upper(), "price": float(trade.price),
                    "timestamp": trade.timestamp.isoformat()}
        except Exception as e:
            logger.error(f"Alpaca trade error for {ticker}: {e}")
            return {}

    def get_spot(self, ticker: str, max_age_seconds: Optional[float] = 900) -> Optional[float]:
        """The stock's current price for a decision, or None when there is no
        trustworthy one (see spot_from). Never the midpoint of a one-sided book."""
        return spot_from(self.get_latest_trade(ticker), self.get_latest_quote(ticker),
                         max_age_seconds=max_age_seconds)

    def get_bars(self, ticker: str, days: int = 30, timeframe: str = "1Day") -> list[dict]:
        """Get OHLCV bars for a ticker."""
        try:
            tf_map = {
                "1Min": TimeFrame.Minute,
                "5Min": TimeFrame(5, "Min"),
                "1Hour": TimeFrame.Hour,
                "1Day": TimeFrame.Day,
            }
            tf = tf_map.get(timeframe, TimeFrame.Day)
            end = et_now()
            start = end - timedelta(days=days)

            req = StockBarsRequest(
                symbol_or_symbols=ticker.upper(),
                timeframe=tf,
                start=start,
                end=end,
            )
            bars = self.stock_client.get_stock_bars(req)
            bar_list = bars.data.get(ticker.upper(), [])
            return [
                {
                    "t": b.timestamp.isoformat(),
                    "o": float(b.open),
                    "h": float(b.high),
                    "l": float(b.low),
                    "c": float(b.close),
                    "v": int(b.volume),
                }
                for b in bar_list
            ]
        except Exception as e:
            logger.error(f"Alpaca bars error for {ticker}: {e}")
            return []

    def get_daily_close(self, ticker: str, session_date: str) -> Optional[float]:
        """Closing stock price for one completed market session.

        This is deliberately historical rather than a latest quote: expiry-based
        option evaluations must use the underlying's price *at expiration*, not
        a later live price. Returns None until Alpaca has a bar for that session.
        """
        try:
            target = date.fromisoformat(session_date)
            start = datetime.combine(target, time.min)
            req = StockBarsRequest(
                symbol_or_symbols=ticker.upper(),
                timeframe=TimeFrame.Day,
                start=start,
                end=start + timedelta(days=1),
            )
            # get_stock_bars returns a BarSet — dict-like through .data, with
            # no .get() of its own. Reaching for .get() on the BarSet silently
            # raised and left every expiry-based evaluation unresolved.
            resp = self.stock_client.get_stock_bars(req)
            data = getattr(resp, "data", resp) or {}
            bars = data.get(ticker.upper(), []) if hasattr(data, "get") else []
            for bar in reversed(bars):
                if bar.timestamp.date() == target:
                    return float(bar.close)
            logger.debug(f"No daily close yet for {ticker} on {session_date}")
        except Exception as e:
            logger.warning(f"Alpaca daily-close error for {ticker} {session_date}: {e}")
        return None

    def get_option_chain(self, ticker: str, expiry_days: int = 45) -> list[dict]:
        """Get option chain with IV for a ticker."""
        try:
            exp_date = (et_now() + timedelta(days=expiry_days)).strftime("%Y-%m-%d")
            req = OptionChainRequest(
                underlying_symbol=ticker.upper(),
                expiration_date_lte=exp_date,
            )
            chain = self.option_client.get_option_chain(req)
            results = []
            for symbol, snapshot in chain.items():
                try:
                    # Snapshots contain quotes/greeks, not contract metadata.
                    # OCC's trailing 15 characters encode date, type and strike.
                    suffix = symbol[-15:]
                    expiry = datetime.strptime(suffix[:6], "%y%m%d").date()
                    option_type = {"C": "call", "P": "put"}[suffix[6]]
                    results.append({
                        "symbol": symbol,
                        "underlying": ticker.upper(),
                        "strike": int(suffix[7:]) / 1000,
                        "expiry": expiry.isoformat(),
                        "type": option_type,
                        "iv": float(snapshot.implied_volatility or 0),
                        "delta": float(snapshot.greeks.delta if snapshot.greeks else 0),
                        "gamma": float(snapshot.greeks.gamma if snapshot.greeks else 0),
                        "theta": float(snapshot.greeks.theta if snapshot.greeks else 0),
                        "vega": float(snapshot.greeks.vega if snapshot.greeks else 0),
                        "open_interest": None,
                        "volume": None,
                        "bid": float(snapshot.latest_quote.bid_price if snapshot.latest_quote else 0),
                        "ask": float(snapshot.latest_quote.ask_price if snapshot.latest_quote else 0),
                    })
                except Exception:
                    continue
            return results
        except Exception as e:
            logger.error(f"Alpaca option chain error for {ticker}: {e}")
            return []

    def start_stream(self, tickers: list[str], on_quote=None, on_bar=None):
        """Stream real-time stock quotes/bars."""
        stream = StockDataStream(self.api_key, self.secret_key)
        if on_quote:
            stream.subscribe_quotes(on_quote, *tickers)
        if on_bar:
            stream.subscribe_bars(on_bar, *tickers)
        return stream

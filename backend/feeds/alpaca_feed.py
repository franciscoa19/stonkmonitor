"""
Alpaca market data feed — real-time quotes, bars, options chains.
Uses alpaca-py SDK.
"""
import logging
from time import sleep
from typing import Optional

import requests
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
from datetime import date, datetime, time, timedelta, timezone

from market_time import ET, et_now

logger = logging.getLogger(__name__)

# The data plan refuses SIP history that ends within the last 15 minutes
# ("subscription does not permit querying recent SIP data"), and an end in the
# future counts as that. A daily-close request ending at UTC midnight was
# therefore refused until 20:15 ET, holding every expiry evaluation for hours.
RECENT_SIP_LAG = timedelta(minutes=16)
# A daily bar is read only once the request covers this long past the
# scheduled close, so the closing auction is in it.
CLOSE_REPORTED_AFTER = timedelta(minutes=10)

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
    caller waits. `max_age_seconds=None` accepts a timestamped past trade of
    any age; it never removes timestamp checks or the quote's 15-minute limit.
    """
    from math import isfinite
    now = now or datetime.now().astimezone()

    def fresh(stamp, age_limit) -> bool:
        try:
            at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if at.tzinfo is None:
                return False
            age = (now - at).total_seconds()
            return age >= 0 and (age_limit is None or age <= age_limit)
        except (TypeError, ValueError):
            return False

    try:
        price = float((trade or {}).get("price"))
        if isfinite(price) and price > 0 and fresh((trade or {}).get("timestamp"), max_age_seconds):
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
    quote_age_limit = 900 if max_age_seconds is None else max_age_seconds
    if (ask - bid) / mid > MAX_SPOT_SPREAD_PCT or not fresh((quote or {}).get("timestamp"), quote_age_limit):
        return None
    return mid


def _retry_dropped_connection(fn, delay: float = 0.5):
    """fn(), called once more if the connection drops.

    The SDK retries only HTTP 429/504. A pooled connection the server dropped
    while the Mac slept answers its next request with a reset: on 2026-10-30
    the first stock-price read after a brief wake failed nine times, and twice
    AMZN's expiry-day hold-or-close check had to wait for the next wake. The
    retry goes out on a fresh connection. An error Alpaca actually returned is
    not retried. Reads only.
    """
    try:
        return fn()
    except requests.exceptions.ConnectionError:
        sleep(delay)
        return fn()


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
            result = _retry_dropped_connection(lambda: self.stock_client.get_stock_latest_quote(req))
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
            result = _retry_dropped_connection(lambda: self.stock_client.get_stock_latest_trade(req))
            trade = result.get(ticker.upper())
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
            bars = _retry_dropped_connection(lambda: self.stock_client.get_stock_bars(req))
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

    def get_daily_close(self, ticker: str, session_date: str, *,
                        now: Optional[datetime] = None) -> Optional[float]:
        """Closing stock price for one completed market session.

        This is deliberately historical rather than a latest quote: expiry-based
        option evaluations must use the underlying's price *at expiration*, not
        a later live price. Returns None until Alpaca has a bar for that session.
        The daily bar's close is the regular session's (2026-10-16: STT's daily
        close $165.72, its last after-hours trade $168.39).
        """
        from feeds.uw_budget import market_close_minute
        try:
            target = date.fromisoformat(session_date)
            start = datetime.combine(target, time.min, tzinfo=timezone.utc)
            day = datetime.combine(target, time.min, tzinfo=ET)
            close_minute = market_close_minute(day)
            if close_minute is None:
                logger.debug(f"No session on {session_date} for {ticker}")
                return None
            now = et_now(now)                          # naive = ET, as in settlement_ready
            end = min(start + timedelta(days=1), now - RECENT_SIP_LAG)
            if end < day + timedelta(minutes=close_minute) + CLOSE_REPORTED_AFTER:
                logger.debug(f"Daily close for {ticker} on {session_date} not readable yet")
                return None
            req = StockBarsRequest(
                symbol_or_symbols=ticker.upper(),
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
            )
            # get_stock_bars returns a BarSet — dict-like through .data, with
            # no .get() of its own. Reaching for .get() on the BarSet silently
            # raised and left every expiry-based evaluation unresolved.
            resp = _retry_dropped_connection(lambda: self.stock_client.get_stock_bars(req))
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
            chain = _retry_dropped_connection(lambda: self.option_client.get_option_chain(req))
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

"""
Alpaca market data feed — real-time quotes, bars, options chains.
Uses alpaca-py SDK.
"""
import logging
from typing import Optional
from alpaca.data import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import (
    StockLatestQuoteRequest,
    StockBarsRequest,
    OptionChainRequest,
    OptionLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.data.live import StockDataStream
from datetime import date, datetime, time, timedelta

from market_time import et_now

logger = logging.getLogger(__name__)


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
            bar_list = bars.get(ticker.upper(), [])
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
            bars = self.stock_client.get_stock_bars(req).get(ticker.upper(), [])
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
                    results.append({
                        "symbol": symbol,
                        "underlying": ticker.upper(),
                        "strike": float(snapshot.details.strike_price),
                        "expiry": snapshot.details.expiration_date,
                        "type": snapshot.details.option_type,
                        "iv": float(snapshot.implied_volatility or 0),
                        "delta": float(snapshot.greeks.delta if snapshot.greeks else 0),
                        "gamma": float(snapshot.greeks.gamma if snapshot.greeks else 0),
                        "theta": float(snapshot.greeks.theta if snapshot.greeks else 0),
                        "vega": float(snapshot.greeks.vega if snapshot.greeks else 0),
                        "open_interest": int(snapshot.open_interest or 0),
                        "volume": int(snapshot.day.volume if snapshot.day else 0),
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

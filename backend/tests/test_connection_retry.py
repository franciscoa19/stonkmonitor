"""A dropped broker connection is retried once; an answer from the broker is not.

On 2026-10-15 the Mac slept with the lid shut. Each brief maintenance wake let
the research scan reuse a pooled connection the server had already dropped,
and the request came back "connection reset" — seven option-contract lookups
failed, each on the first request after a wake. On 2026-10-30 the same thing
hit the first stock-price read after each wake, nine times, while AMZN's
condor waited on its expiry-day hold-or-close check. These tests drive the
real SDK against a local server that resets a reused connection the same way.
Nothing leaves 127.0.0.1.
"""
import socket
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import pytest
import requests
from alpaca.data import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.trading.client import TradingClient

from feeds.alpaca_feed import AlpacaFeed
from trading.alpaca_trader import AlpacaTrader

CONTRACTS = (b'{"option_contracts": [{"id": "0d2c3f61-6c1b-4b0e-9d4e-4f7b7c9f0a11",'
             b' "symbol": "TEST261120C00100000", "name": "TEST Nov 20 2026 100 Call",'
             b' "status": "active", "tradable": true, "expiration_date": "2026-11-20",'
             b' "root_symbol": "TEST", "underlying_symbol": "TEST",'
             b' "underlying_asset_id": "5a7e0c5e-1b1f-4f55-9a43-2b0f8f0e9c22",'
             b' "type": "call", "style": "american", "strike_price": "100", "size": "100",'
             b' "open_interest": "12"}], "next_page_token": null}')


class FlakyBroker:
    """HTTP/1.1 on 127.0.0.1. `plan` decides each request in turn: "ok" answers
    and keeps the connection open, "reset" reads the request and drops the
    connection with a TCP reset, a number answers with that HTTP status.
    `body` is the "ok" answer, or a function of the request path that gives it."""

    def __init__(self, plan, body=CONTRACTS):
        self.plan = list(plan)
        self.body = body
        self.connections = self.requests = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        buf = b""
        with conn:
            while True:
                while b"\r\n\r\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                head, _, buf = buf.partition(b"\r\n\r\n")       # a GET has no body
                path = head.split(b" ", 2)[1].decode()
                self.requests += 1
                action = self.plan.pop(0) if self.plan else "ok"
                if action == "reset":
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    return                                       # close with linger 0 = RST
                if action == "ok":
                    status, body = 200, self.body(path) if callable(self.body) else self.body
                else:
                    status, body = action, b'{"message": "no"}'
                conn.sendall(b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\n"
                             b"Content-Length: %d\r\nConnection: keep-alive\r\n\r\n%s"
                             % (status, len(body), body))

    def close(self):
        self.sock.close()


@pytest.fixture
def broker():
    servers = []

    def start(*plan):
        server = FlakyBroker(plan)
        servers.append(server)
        trader = AlpacaTrader("test-key", "test-secret", paper=True)
        trader.client = TradingClient("test-key", "test-secret", paper=True, url_override=server.url)
        return server, trader

    yield start
    for server in servers:
        server.close()


def lookup(trader):
    """The SDK sets no timeout; a hang fails the test instead of stalling it."""
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(trader.get_option_contracts, "TEST",
                           date(2026, 11, 1), date(2026, 11, 30), "call").result(timeout=15)


def test_a_reused_connection_that_was_dropped_is_retried_once_on_a_fresh_one(broker):
    server, trader = broker("ok", "reset")
    assert [c["symbol"] for c in lookup(trader)] == ["TEST261120C00100000"]
    assert [c["symbol"] for c in lookup(trader)] == ["TEST261120C00100000"]   # was [] before
    assert (server.connections, server.requests) == (2, 3)


def test_an_error_the_broker_returned_is_not_retried(broker):
    server, trader = broker(403)
    assert lookup(trader) == []
    assert server.requests == 1


def test_two_drops_in_a_row_give_up_and_the_next_lookup_works(broker, caplog):
    server, trader = broker("ok", "reset", "reset")
    lookup(trader)
    assert lookup(trader) == []                                  # reported, not retried forever
    assert "get_option_contracts error" in caplog.text
    assert server.requests == 3
    assert len(lookup(trader)) == 1


def test_the_retry_only_catches_what_it_is_told_to():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("not a connection problem")
        return "ok"

    with pytest.raises(ValueError):
        AlpacaTrader._retry_call(flaky, delay=0, retry_on=(requests.exceptions.ConnectionError,))
    assert len(calls) == 1
    calls.clear()
    assert AlpacaTrader._retry_call(flaky, delay=0) == "ok"     # the default is unchanged
    assert len(calls) == 2


# --- the market-data feed: stock prices, daily closes, option chains ---------

def market_data(path):
    """What Alpaca's data API answers for one symbol; the trade is stamped now.
    The quote is one-sided, as IEX's often is, so only the trade gives a price."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if path.endswith("/trades/latest"):
        return ('{"symbol": "AMZN", "trade": {"t": "%s", "x": "V", "p": 238.84, "s": 100,'
                ' "c": ["@"], "i": 1, "z": "C"}}' % now).encode()
    if path.endswith("/quotes/latest"):
        return ('{"symbol": "AMZN", "quote": {"t": "%s", "ax": "V", "ap": 238.9, "as": 1,'
                ' "bx": "V", "bp": 232.11, "bs": 2, "c": ["R"], "z": "C"}}' % now).encode()
    if "/bars?" in path:
        return (b'{"symbol": "AMZN", "next_page_token": null, "bars": [{"t": "2026-10-29T04:00:00Z",'
                b' "o": 230.1, "h": 240.2, "l": 229.3, "c": 236.5, "v": 1000, "n": 10, "vw": 235.0}]}')
    if "/options/snapshots/" in path:
        return (b'{"next_page_token": null, "snapshots": {"AMZN261030C00267500": {"latestQuote":'
                b' {"t": "2026-10-30T15:00:00Z", "ax": "C", "ap": 0.05, "as": 1, "bx": "C",'
                b' "bp": 0.01, "bs": 1, "c": "A"}, "impliedVolatility": 0.3,'
                b' "greeks": {"delta": 0.1, "gamma": 0.01, "theta": -0.1, "vega": 0.05, "rho": 0.0}}}}')
    raise AssertionError(f"unexpected request {path}")


@pytest.fixture
def data_api():
    servers = []

    def start(*plan):
        server = FlakyBroker(plan, body=market_data)
        servers.append(server)
        feed = AlpacaFeed("test-key", "test-secret")
        feed.stock_client = StockHistoricalDataClient("test-key", "test-secret", url_override=server.url)
        feed.option_client = OptionHistoricalDataClient("test-key", "test-secret", url_override=server.url)
        return server, feed

    yield start
    for server in servers:
        server.close()


def read(fn, *args, **kwargs):
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(fn, *args, **kwargs).result(timeout=15)


FEED_READS = {
    "latest trade": (lambda f: read(f.get_latest_trade, "AMZN").get("price"), 238.84),
    "latest quote": (lambda f: read(f.get_latest_quote, "AMZN").get("bid"), 232.11),
    "bars": (lambda f: [b["c"] for b in read(f.get_bars, "AMZN")], [236.5]),
    "daily close": (lambda f: read(f.get_daily_close, "AMZN", "2026-10-29",
                                   now=datetime(2026, 10, 30, 12)), 236.5),
    "option chain": (lambda f: [c["symbol"] for c in read(f.get_option_chain, "AMZN")],
                     ["AMZN261030C00267500"]),
}


@pytest.mark.parametrize("name", FEED_READS)
def test_every_feed_read_retries_a_dropped_connection_once(data_api, name):
    call, expected = FEED_READS[name]
    server, feed = data_api("ok", "reset")
    assert call(feed) == expected
    assert call(feed) == expected                                # was empty before
    assert (server.connections, server.requests) == (2, 3)


def test_the_first_price_after_a_wake_is_still_read(data_api, caplog):
    server, feed = data_api("ok", "ok", "reset")
    assert read(feed.get_spot, "AMZN") == 238.84
    assert read(feed.get_spot, "AMZN") == 238.84                 # was None: decision deferred
    assert "Alpaca trade error" not in caplog.text
    assert server.requests == 5                                  # trade, quote; trade twice, quote


def test_an_error_the_data_api_returned_is_not_retried(data_api):
    server, feed = data_api(403)
    assert read(feed.get_latest_trade, "AMZN") == {}
    assert server.requests == 1


def test_two_dropped_reads_in_a_row_give_up_and_the_next_read_works(data_api, caplog):
    server, feed = data_api("ok", "reset", "reset")
    read(feed.get_latest_trade, "AMZN")
    assert read(feed.get_latest_trade, "AMZN") == {}             # reported, not retried forever
    assert "Alpaca trade error" in caplog.text
    assert server.requests == 3
    assert read(feed.get_latest_trade, "AMZN")["price"] == 238.84

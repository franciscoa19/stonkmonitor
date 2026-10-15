"""A dropped broker connection is retried once; an answer from the broker is not.

On 2026-10-15 the Mac slept with the lid shut. Each brief maintenance wake let
the research scan reuse a pooled connection the server had already dropped,
and the request came back "connection reset" — seven option-contract lookups
failed, each on the first request after a wake. These tests drive the real
SDK against a local server that resets a reused connection the same way.
Nothing leaves 127.0.0.1.
"""
import socket
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
import requests
from alpaca.trading.client import TradingClient

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
    connection with a TCP reset, a number answers with that HTTP status."""

    def __init__(self, plan):
        self.plan = list(plan)
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
                _, _, buf = buf.partition(b"\r\n\r\n")          # a GET has no body
                self.requests += 1
                action = self.plan.pop(0) if self.plan else "ok"
                if action == "reset":
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    return                                       # close with linger 0 = RST
                status, body = (200, CONTRACTS) if action == "ok" else (action, b'{"message": "no"}')
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

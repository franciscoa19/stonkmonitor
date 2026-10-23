"""Every outbound HTTPS path trusts certifi's bundle, which no longer carries
the root whose intermediates were compromised (PYSEC-2026-4320). No network."""
import os
import ssl

import certifi
import pytest

import tls_trust

REVOKED_ROOT = "emSign ECC Root CA - G3"


def roots(ctx: ssl.SSLContext) -> set:
    return {dict(x[0] for x in c["subject"]).get("commonName", "") for c in ctx.get_ca_certs()}


def test_the_bundle_no_longer_trusts_the_compromised_root():
    ctx = tls_trust.context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert len(roots(ctx)) > 100                       # a real bundle, not an empty store
    assert REVOKED_ROOT not in roots(ctx), "certifi older than 2026.10.19"


def test_orders_go_out_on_that_context(monkeypatch):
    from trading import alpaca_trader
    seen = {}

    class Response:
        status = 200
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def urlopen(req, timeout=None, context=None):
        seen["context"] = context
        return Response()
    monkeypatch.setattr("urllib.request.urlopen", urlopen)       # _rest imports urllib per call
    trader = alpaca_trader.AlpacaTrader("k", "s", paper=True)
    assert trader._rest("GET", "https://paper-api.alpaca.markets/v2/clock") == (200, {})
    assert seen["context"] is alpaca_trader._TLS
    assert REVOKED_ROOT not in roots(alpaca_trader._TLS)


def test_the_earnings_calendar_uses_it_too(monkeypatch):
    from feeds import earnings_calendar
    sentinel, seen = object(), {}
    monkeypatch.setattr(tls_trust, "context", lambda: sentinel)

    def urlopen(req, timeout=None, context=None):
        seen["context"] = context
        raise OSError("offline")
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    earnings_calendar._fetch_nasdaq_day(__import__("datetime").date(2026, 10, 22))
    assert seen["context"] is sentinel


def test_the_app_makes_certifi_the_default_for_everything_else():
    import main  # noqa: F401  (imports set the default)
    assert os.environ.get("SSL_CERT_FILE") == certifi.where()


def test_a_default_context_then_drops_the_root(monkeypatch):
    """aiohttp and websockets build contexts without a cafile."""
    monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
    assert REVOKED_ROOT not in roots(ssl.create_default_context())


def test_an_operator_setting_is_left_alone(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/custom-bundle.pem")
    tls_trust.use_certifi_by_default()
    assert os.environ["SSL_CERT_FILE"] == "/etc/custom-bundle.pem"

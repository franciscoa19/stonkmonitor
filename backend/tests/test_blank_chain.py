"""A Yahoo option chain without quotes is no data, not a reason to avoid a name.

At 09:40 ET on 2026-11-12 Yahoo showed CSCO's whole chain with bid = ask = 0 and
placeholder IVs (0.00001 for calls, 0.002–0.004 for puts) and yesterday's last
trades. Averaging those in gave IV/RV = 0.001, so the setup that had been
SELL_PREMIUM the night before scanned as AVOID, and the next look was 30 minutes
later (10:12, when it entered). Fake Yahoo throughout.
"""
import asyncio
import time
from datetime import date
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest

from signals import earnings_scanner as scanner
from test_execution_safety import session  # noqa: F401  (fixture)

TODAY = date(2026, 11, 12)
EXPIRIES = ["2026-11-13", "2026-11-20", "2026-11-27", "2026-12-04", "2026-12-18", "2027-01-15"]
FRONT_IV = 1.29                       # what Yahoo showed for CSCO's 11-13 ATM once quoted


def quote(bid, ask, iv):
    return {"bid": bid, "ask": ask, "impliedVolatility": iv}


def side(atm):
    """Strikes 95–105 around a $100 stock; only the ATM row matters to the scan."""
    rows = [{"strike": k, **(atm if k == 100 else quote(1.0, 1.2, 0.5))} for k in (95, 100, 105)]
    return pd.DataFrame(rows)


def good(i):
    iv = FRONT_IV if i == 0 else 0.30 + 0.01 * i
    return quote(3.85, 4.60, iv) if i == 0 else quote(5.0, 5.4, iv)


class FakeTicker:
    def __init__(self, atm_by_expiry):
        self.atm = atm_by_expiry
        self.options = tuple(EXPIRIES)

    def history(self, period):
        days = 1 if period == "1d" else 60
        close = 100 + np.sin(np.arange(days)) * 0.8           # ~a quiet 15–20% vol
        return pd.DataFrame({"Open": close - 0.2, "High": close + 0.9, "Low": close - 0.9,
                             "Close": close, "Volume": [17_000_000] * days})

    def option_chain(self, expiry):
        atm = self.atm[EXPIRIES.index(expiry)]
        return NS(calls=side(atm), puts=side(atm))


@pytest.fixture
def yahoo(monkeypatch):
    import yfinance
    chains = {"atm": [good(i) for i in range(len(EXPIRIES))]}
    monkeypatch.setattr(yfinance, "Ticker", lambda _t: FakeTicker(chains["atm"]))
    monkeypatch.setattr(scanner, "et_today", lambda *a: TODAY)
    monkeypatch.setattr(scanner, "_next_earnings_info", lambda _s: ("2026-11-12", "post"))
    return chains


async def test_a_quoted_chain_scans_as_before(yahoo):
    setup, why = await scanner.scan_ticker_with_reason("CSCO")
    assert why is None
    assert setup.recommendation == "SELL_PREMIUM"
    assert setup.expected_move == "8.45%"                   # (4.225 + 4.225) / 100
    assert setup.iv30_rv30 > scanner.MIN_IV_RV_RATIO


BLANK = {
    "Yahoo overnight (bid = ask = 0, placeholder IV)": quote(0.0, 0.0, 1e-05),
    "a quote but still the placeholder IV": quote(3.85, 4.60, 0.0039),
    "one-sided": quote(0.0, 4.60, 1.29),
    "crossed": quote(4.70, 4.60, 1.29),
    "missing bid": quote(float("nan"), 4.60, 1.29),
    "missing IV": quote(3.85, 4.60, None),
}


@pytest.mark.parametrize("front", BLANK.values(), ids=BLANK.keys())
async def test_an_unquoted_front_expiry_is_no_data_not_avoid(yahoo, front):
    yahoo["atm"][0] = front
    assert await scanner.scan_ticker_with_reason("CSCO") == (None, "quotes_not_ready")
    assert await scanner.scan_ticker("CSCO") is None


async def test_an_unquoted_later_expiry_is_left_out_of_the_term_structure(yahoo):
    full, _ = await scanner.scan_ticker_with_reason("CSCO")
    yahoo["atm"][4] = BLANK["Yahoo overnight (bid = ask = 0, placeholder IV)"]   # 12-18, 36 DTE
    partial, why = await scanner.scan_ticker_with_reason("CSCO")
    assert why is None and partial.expected_move == full.expected_move
    # 30-day IV is read between 12-04 and 12-18; without 12-18 it comes from
    # 12-04 and 01-15 instead of being dragged towards 0.
    assert partial.iv30 == pytest.approx(full.iv30, rel=0.02)
    assert partial.recommendation == full.recommendation == "SELL_PREMIUM"


async def test_other_failures_are_not_reported_as_quotes(yahoo, monkeypatch):
    import yfinance

    class Broken(FakeTicker):
        def history(self, period):
            raise RuntimeError("Yahoo timed out")

    monkeypatch.setattr(yfinance, "Ticker", lambda _t: Broken(yahoo["atm"]))
    assert await scanner.scan_ticker_with_reason("CSCO") == (None, "error")


# ── The scan loop asks again sooner while the open is still being quoted ─────
async def run_scan_loop(session, monkeypatch, rth, until):
    from api import routes
    from feeds import uw_budget
    clock = {"t": 1_000_000.0}
    scans = []

    async def scan(ticker):
        scans.append((ticker, clock["t"] - 1_000_000.0))
        return (None, "quotes_not_ready") if ticker == "CSCO" else (None, None)

    async def sleep(seconds):
        clock["t"] += seconds
        if clock["t"] - 1_000_000.0 > until:
            raise asyncio.CancelledError

    monkeypatch.setattr(time, "time", lambda: clock["t"])
    monkeypatch.setattr(routes, "_watchlist", ["CSCO", "NVDA"])
    monkeypatch.setattr(uw_budget, "current_session", lambda: "rth" if rth else "extended")
    monkeypatch.setattr(session, "is_rth_now", lambda: rth)
    monkeypatch.setattr(session, "settings", session.settings.model_copy(update={"uw_enabled": False}))
    monkeypatch.setattr(scanner, "scan_ticker_with_reason", scan)
    monkeypatch.setattr(session.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await session.iv_scanner_loop()
    return scans


async def test_in_market_hours_an_unquoted_ticker_is_tried_again_in_five_minutes(session, monkeypatch, caplog):
    caplog.set_level("INFO")
    scans = await run_scan_loop(session, monkeypatch, rth=True, until=1000)
    assert [t for t, _ in scans] == ["CSCO", "NVDA", "CSCO", "CSCO", "CSCO"]
    gaps = np.diff([at for t, at in scans if t == "CSCO"])
    assert all(300 <= g < 320 for g in gaps)
    assert "no Yahoo quote on the front expiry yet for 1 ticker(s) (CSCO)" in caplog.text


async def test_outside_market_hours_it_waits_the_usual_interval(session, monkeypatch, caplog):
    caplog.set_level("INFO")
    scans = await run_scan_loop(session, monkeypatch, rth=False, until=1800)
    assert [t for t, _ in scans] == ["CSCO", "NVDA"]
    assert "no Yahoo quote" not in caplog.text

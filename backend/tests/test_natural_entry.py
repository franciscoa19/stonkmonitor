"""Condor entries are priced at the natural; markets too wide to trade are skipped.

To 2026-11-16, an entry limit 6% or more above the natural credit (short legs
at their bids, wings at their asks) filled 2 times in 19 on the paper account,
and on the research log, priced at the natural and held to expiry, watchlist
names whose natural was under 70% of the mid lost 30% of max loss.
build_iron_condor() now skips a condor whose natural is under
IV_EXEC_MIN_NATURAL_TO_MID of its mid credit, and prices the rest at the natural.
"""
import logging
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from pydantic import ValidationError

from config import Settings
from signals.iv_executor import build_iron_condor
from signals.measurement_capture import CONFIG_FIELDS
from test_account_limits import PLAN, SETUP, entry
from test_execution_safety import database, session  # noqa: F401  (fixtures)


def settings(**kw):
    base = dict(iv_exec_wing_width_pct=0.01, iv_exec_min_credit=0.05)
    base.update(kw)
    return Settings(_env_file=None, alpaca_api_key="unused", alpaca_secret_key="unused", **base)


def book(short_bid, short_ask, wing_bid, wing_ask):
    """Both short legs and both $1 wings quoted alike."""
    return {"call-110": {"bid": short_bid, "ask": short_ask}, "put-90": {"bid": short_bid, "ask": short_ask},
            "call-111": {"bid": wing_bid, "ask": wing_ask}, "put-89": {"bid": wing_bid, "ask": wing_ask}}


def plan(quotes, s=None, equity=50_000):
    """Shorts at 110/90 (the 10% implied move on $100), $1 wings at 111/89."""
    from market_time import et_today
    expiry = et_today() + timedelta(days=3)
    broker = NS(get_option_contracts=lambda tk, start, end, kind: [
        {"symbol": f"{kind}-{strike}", "strike": strike, "expiry": expiry}
        for strike in ([110, 111] if kind == "call" else [89, 90])],
        get_option_quotes=lambda syms: {sym: dict(quotes[sym]) for sym in syms})
    setup = NS(ticker="TEST", price=100, expected_move="10%",
               next_earnings_date=(expiry - timedelta(days=1)).isoformat())
    return build_iron_condor(broker, setup, equity, s or settings())


# Natural $0.20 against a $0.28 mid: 71%, the kind of book that never filled.
WIDE = book(0.30, 0.34, 0.16, 0.20)
# Natural $0.26 against a $0.30 mid: 87%.
TIGHT = book(0.33, 0.35, 0.18, 0.20)


def test_a_market_too_wide_to_trade_is_skipped():
    p = plan(WIDE)
    assert not p["ok"]
    assert p["reason"] == "market too wide: natural $0.20 is 71% of mid $0.28 (min 85%)"


def test_a_tradable_market_is_priced_and_sized_at_the_natural():
    p = plan(TIGHT)
    assert p["ok"], p
    assert p["limit_price"] == -0.26 and p["credit"] == 0.26          # not 90% of the mid ($0.27)
    assert p["credit_mid"] == 0.30 and p["natural_to_mid"] == pytest.approx(0.867, abs=1e-3)
    assert p["max_loss"] == 74.0 and p["risk_usd"] == p["qty"] * 74.0


def test_a_market_tighter_than_ninety_percent_collects_the_natural_not_less():
    # Natural $0.30 against a $0.32 mid (94%): the old limit would have asked
    # $0.29, giving away credit the market was offering.
    p = plan(book(0.34, 0.35, 0.18, 0.19))
    assert p["ok"] and p["limit_price"] == -0.30 and p["credit_mid"] == 0.32


@pytest.mark.parametrize("minimum,ok", [(0.0, True), (0.86, True), (0.8666, True),
                                        (0.867, False), (0.95, False), (1.0, False)])
def test_the_minimum_share_is_inclusive_and_configurable(minimum, ok):
    p = plan(TIGHT, settings(iv_exec_min_natural_to_mid=minimum))
    assert p["ok"] is ok, p
    assert ok or p["reason"].startswith("market too wide")


def test_exactly_at_the_minimum_trades():
    # Natural $0.18 is exactly 90% of the $0.20 mid, but summed in floating
    # point it comes out at 0.8999999999999999. That must not be a skip.
    p = plan(book(0.21, 0.22, 0.11, 0.12), settings(iv_exec_min_natural_to_mid=0.90))
    assert p["ok"], p
    assert p["limit_price"] == -0.18 and p["credit_mid"] == 0.20


def test_no_minimum_still_prices_at_the_natural():
    p = plan(WIDE, settings(iv_exec_min_natural_to_mid=0))
    assert p["ok"] and p["limit_price"] == -0.20 and p["max_loss"] == 80.0


def test_the_limit_is_floored_to_a_cent_never_above_the_natural():
    # Natural $0.288: rounding would ask $0.29, more than the market offers.
    p = plan(book(0.339, 0.345, 0.18, 0.195))
    assert p["ok"], p
    assert p["limit_price"] == -0.28 and p["credit"] == 0.28


def test_a_wing_with_no_bid_is_bought_at_its_ask_in_both_prices():
    p = plan(book(0.33, 0.35, 0.0, 0.20))
    assert p["ok"], p
    assert p["limit_price"] == -0.26 and p["credit_mid"] == 0.28


def test_no_credit_at_the_natural_is_skipped():
    # Mid credit $0.02, but selling at the bids and buying at the asks pays $0.10.
    p = plan(book(0.20, 0.22, 0.15, 0.25))
    assert not p["ok"] and p["reason"].startswith("no credit at the natural")


def test_the_minimum_credit_applies_to_the_natural():
    p = plan(TIGHT, settings(iv_exec_min_credit=0.27))              # mid $0.30 clears it
    assert not p["ok"] and p["reason"] == "credit $0.26 at the natural < min"


def test_the_credit_to_width_floor_applies_to_the_natural():
    # Natural $0.09 on a $1 wing (mid $0.10 would have passed the 0.10 floor).
    p = plan(book(0.20, 0.205, 0.15, 0.155))
    assert not p["ok"] and p["reason"] == "credit/width 0.09 too thin"


@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_the_setting_is_a_share_between_zero_and_one(value):
    with pytest.raises(ValidationError):
        settings(iv_exec_min_natural_to_mid=value)


def test_the_default_is_85_percent_and_research_captures_record_it():
    assert settings().iv_exec_min_natural_to_mid == 0.85
    assert "iv_exec_min_natural_to_mid" in CONFIG_FIELDS


async def test_the_entry_log_shows_the_natural_against_the_mid(database, session, monkeypatch, caplog):
    entry(session, database, monkeypatch,
          plan=dict(PLAN, credit=1.0, credit_mid=1.11, natural_to_mid=0.901))
    with caplog.at_level(logging.INFO, logger="main"):
        await session.maybe_execute_condor(SETUP)
    line = next(r.getMessage() for r in caplog.records if "IV-exec ✅" in r.getMessage())
    assert "credit $1.00 (natural, 90% of mid $1.11) maxloss" in line

"""
IV/RV earnings execution (Phase 2) — build a defined-risk iron condor to sell
premium into an earnings print and collect the IV crush.

Strategy: when the scanner flags a sell-premium setup and the print is imminent,
sell a strangle at ~the implied (straddle) move and buy protective wings a fixed
width beyond each short. That caps risk (options level 3, no naked shorts) and
the whole thing is one atomic multi-leg (mleg) order.

`build_iron_condor()` is pure planning — it reads the live chain + quotes through
the trader and returns an executable plan (legs, credit, max-loss, size) or a
reason it declined. main.py's iv_scanner_loop submits the plan when armed.
"""
import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from market_time import et_now, et_today

logger = logging.getLogger(__name__)

# Skip junk risk/reward: a condor whose net credit is under this fraction of the
# wing width isn't worth the tail risk (e.g. $0.20 credit on a $10 wing).
MIN_CREDIT_WIDTH_RATIO = 0.10
CONTRACT_MULTIPLIER = 100          # 1 option contract = 100 shares
QTY_HARD_CAP = 20                  # never size beyond this many spreads


def is_pre_earnings_entry_window(earnings_date: Optional[str],
                                 report_time: Optional[str],
                                 entry_days_before: int,
                                 now: Optional[datetime] = None) -> bool:
    """Whether a condor can still be opened before its known earnings print.

    Calendar dates alone are insufficient on the event day: a before-market
    report has already happened at the opening bell. Same-day entries therefore
    fail closed unless Nasdaq identifies an after-close print and it is still
    before the regular-session close. `now` makes the safety rule testable.
    """
    try:
        edate = date.fromisoformat(str(earnings_date))
    except (TypeError, ValueError):
        return False
    now = et_now(now)
    days_to = (edate - now.date()).days
    if days_to < 0 or days_to > entry_days_before:
        return False
    if days_to > 0:
        return True
    return report_time == "post" and now.hour < 16


def _implied_move_frac(setup) -> float:
    """setup.expected_move is a string like '11.4%' → 0.114. 0.0 if absent."""
    try:
        return float(str(setup.expected_move or "0").rstrip("%")) / 100.0
    except Exception:
        return 0.0


def _nearest(strikes: list[float], target: float) -> Optional[float]:
    return min(strikes, key=lambda k: abs(k - target)) if strikes else None


def build_iron_condor(trader, setup, equity: float, settings,
                      risk_multiplier: float = 1.0) -> dict:
    """Return an executable iron-condor plan for `setup`, or {ok:False, reason}."""
    ticker = setup.ticker
    spot = float(setup.price or 0)
    em = _implied_move_frac(setup)
    if spot <= 0 or em <= 0:
        return {"ok": False, "reason": "no spot/implied move"}

    today = et_today()
    edate = None
    if setup.next_earnings_date:
        try:
            edate = date.fromisoformat(setup.next_earnings_date)
        except Exception:
            edate = None

    # Front expiry: the first listed expiry AFTER the print, inside our DTE band.
    start = max(today + timedelta(days=settings.iv_exec_min_dte),
                (edate + timedelta(days=1)) if edate else today + timedelta(days=settings.iv_exec_min_dte))
    end = today + timedelta(days=settings.iv_exec_max_dte)
    if start > end:
        return {"ok": False, "reason": f"earnings {edate} outside DTE band"}

    calls = trader.get_option_contracts(ticker, start, end, "call")
    puts = trader.get_option_contracts(ticker, start, end, "put")
    if not calls or not puts:
        return {"ok": False, "reason": "no option contracts in window"}

    # pick the nearest expiry that has both calls and puts
    call_exps = {c["expiry"] for c in calls}
    put_exps = {p["expiry"] for p in puts}
    both = sorted(call_exps & put_exps)
    if not both:
        return {"ok": False, "reason": "no expiry with both calls+puts"}
    expiry = both[0]
    exp_str = expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry)

    call_by_strike = {c["strike"]: c for c in calls if c["expiry"] == expiry}
    put_by_strike = {p["strike"]: p for p in puts if p["expiry"] == expiry}
    call_strikes = sorted(call_by_strike)
    put_strikes = sorted(put_by_strike)
    if len(call_strikes) < 2 or len(put_strikes) < 2:
        return {"ok": False, "reason": "thin chain"}

    # Short strikes at the implied move; long wings beyond them.
    move = spot * em * settings.iv_exec_short_move_mult
    # Wing width IS the risk knob: max loss = (width - credit) * 100. Taking a
    # flat % of spot makes that scale with share price, so a $943 stock demanded
    # a $28-wide spread (~$2,830 at risk) while a $160 one needed $4.80 — which
    # silently rejected every expensive name regardless of how good the setup
    # was. Cap the wing at what the risk budget can actually carry so the
    # structure is sized to the account, not to the share price.
    # risk_multiplier is the throttle: < 1.0 after losses, back to 1.0 on wins.
    # Applied to the budget so it shrinks the WING too, not just the quantity —
    # otherwise integer qty steps would leave sizing flat between thresholds.
    risk_budget = min(equity * settings.iv_exec_risk_pct,
                      settings.iv_exec_max_risk_usd) * max(0.0, risk_multiplier)
    affordable_wing = risk_budget / CONTRACT_MULTIPLIER
    wing = min(spot * settings.iv_exec_wing_width_pct, affordable_wing)
    short_call = _nearest([k for k in call_strikes if k >= spot + move], spot + move) \
        or _nearest([k for k in call_strikes if k > spot], spot + move)
    short_put = _nearest([k for k in put_strikes if k <= spot - move], spot - move) \
        or _nearest([k for k in put_strikes if k < spot], spot - move)
    if short_call is None or short_put is None:
        return {"ok": False, "reason": "no OTM short strikes"}
    long_call = _nearest([k for k in call_strikes if k > short_call], short_call + wing)
    long_put = _nearest([k for k in put_strikes if k < short_put], short_put - wing)
    if long_call is None or long_put is None:
        return {"ok": False, "reason": "no wing strikes"}

    legs = [
        {"symbol": call_by_strike[short_call]["symbol"], "side": "sell", "position_intent": "sell_to_open", "ratio_qty": 1},
        {"symbol": call_by_strike[long_call]["symbol"],  "side": "buy",  "position_intent": "buy_to_open",  "ratio_qty": 1},
        {"symbol": put_by_strike[short_put]["symbol"],   "side": "sell", "position_intent": "sell_to_open", "ratio_qty": 1},
        {"symbol": put_by_strike[long_put]["symbol"],    "side": "buy",  "position_intent": "buy_to_open",  "ratio_qty": 1},
    ]

    quotes = trader.get_option_quotes([l["symbol"] for l in legs])
    mids = {l["symbol"]: quotes.get(l["symbol"], {}).get("mid", 0) for l in legs}
    if not all(mids.values()):
        return {"ok": False, "reason": "missing option quotes"}

    credit = (mids[legs[0]["symbol"]] + mids[legs[2]["symbol"]]) \
        - (mids[legs[1]["symbol"]] + mids[legs[3]["symbol"]])
    call_width = long_call - short_call
    put_width = short_put - long_put
    width = max(call_width, put_width)
    if credit < settings.iv_exec_min_credit:
        return {"ok": False, "reason": f"credit ${credit:.2f} < min"}
    if width <= 0 or credit / width < MIN_CREDIT_WIDTH_RATIO:
        return {"ok": False, "reason": f"credit/width {credit/width:.2f} too thin"}

    max_loss_per = (width - credit) * CONTRACT_MULTIPLIER
    if max_loss_per <= 0:
        return {"ok": False, "reason": "non-positive max loss"}
    qty = int(risk_budget // max_loss_per)
    if qty < 1:
        return {"ok": False, "reason": f"one spread (${max_loss_per:.0f}) exceeds risk budget ${risk_budget:.0f}"}
    qty = min(qty, QTY_HARD_CAP)

    # Entry limit: give up a little credit for fill probability into the print.
    limit_price = -round(max(credit * 0.90, settings.iv_exec_min_credit), 2)

    return {
        "ok": True, "ticker": ticker, "expiry": exp_str, "earnings_date": setup.next_earnings_date,
        "legs": legs, "legs_json": json.dumps(legs),
        "strikes": {"short_put": short_put, "long_put": long_put,
                    "short_call": short_call, "long_call": long_call},
        "credit": round(credit, 2), "max_loss": round(max_loss_per, 2),
        "qty": qty, "limit_price": limit_price,
        "risk_usd": round(max_loss_per * qty, 2),
    }

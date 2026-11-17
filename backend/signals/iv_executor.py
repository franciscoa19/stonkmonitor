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
from math import floor, isfinite
from datetime import date, datetime, timedelta
from typing import Optional

from market_time import et_now, et_today

logger = logging.getLogger(__name__)

# Skip junk risk/reward: a condor whose net credit is under this fraction of the
# wing width isn't worth the tail risk (e.g. $0.20 credit on a $10 wing).
MIN_CREDIT_WIDTH_RATIO = 0.10
CONTRACT_MULTIPLIER = 100          # 1 option contract = 100 shares


def condor_risk_budget(equity: float, settings, risk_multiplier: float = 1.0) -> tuple[float, bool]:
    """Dollars one condor may lose, and whether a dollar ceiling replaced the
    percentage. The percentage of CURRENT equity governs; iv_exec_max_risk_usd
    is an optional ceiling that is unset by default."""
    budget = equity * settings.iv_exec_risk_pct
    ceiling = getattr(settings, "iv_exec_max_risk_usd", None)
    binding = ceiling is not None and ceiling < budget
    return (ceiling if binding else budget) * max(0.0, risk_multiplier), binding


def condor_qty_cap(equity: float, settings) -> int:
    """Most spreads one condor may hold, scaled with equity (never below 1)."""
    per_10k = float(getattr(settings, "iv_exec_max_qty_per_10k_equity", 4.0))
    return max(1, int(equity / 10_000 * per_10k))


def is_pre_earnings_entry_window(earnings_date: Optional[str],
                                 report_time: Optional[str],
                                 entry_days_before: int,
                                 now: Optional[datetime] = None) -> bool:
    """Whether a condor can still be opened before its known earnings print.

    Calendar dates alone are insufficient on the event day: a before-market
    report has already happened at the opening bell. Same-day entries therefore
    fail closed unless the scanner identifies a verified after-close print and it is still
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


def condor_entry_requires_opra(trader, settings) -> bool:
    """Either the configured or connected live mode requires OPRA quotes."""
    return (getattr(settings, "alpaca_paper", True) is not True
            or getattr(trader, "paper", True) is not True)


def build_iron_condor(trader, setup, equity: float, settings,
                      risk_multiplier: float = 1.0, *,
                      risk_headroom: Optional[float] = None,
                      cash_available: Optional[float] = None) -> dict:
    """Return an executable iron-condor plan for `setup`, or {ok:False, reason}.

    `risk_headroom` and `cash_available` are the account-wide limits in dollars
    (trading/account_risk.py): what the account may still put at risk, and the
    free cash left above the reserve. They only ever reduce the quantity — the
    strikes and wing stay those the per-condor budget chose. None = not applied.
    """
    ticker = setup.ticker
    spot = float(setup.price or 0)
    em = _implied_move_frac(setup)
    if not isfinite(spot) or not isfinite(em) or spot <= 0 or em <= 0:
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
    risk_budget, ceiling_binding = condor_risk_budget(equity, settings, risk_multiplier)
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

    symbols = [leg["symbol"] for leg in legs]
    require_opra = condor_entry_requires_opra(trader, settings)
    # Auto may be Alpaca's modified indicative feed, even under live keys.
    # Require an explicit OPRA response before using the natural to price a
    # live entry. Leave the shared quote defaults intact for paper and exits.
    quotes = (trader.get_option_quotes(symbols, feed="opra") if require_opra
              else trader.get_option_quotes(symbols))
    quote_error = ("complete, verified OPRA quotes unavailable for live entry" if require_opra
                   else "missing or invalid option quotes")
    # A leg we SELL needs a bid; a wing we BUY needs only an ask. A zero bid on
    # a cheap wing is a real market (about a quarter of listed contracts on
    # 2026-10-06), and iv_variants already prices it that way. Such a wing is
    # budgeted at its full ask, the price actually paid, never an invented mid.
    mids, natural_px = {}, {}
    quote_feeds = set()
    for leg in legs:
        try:
            quote = quotes[leg["symbol"]]
            leg_feed = quote.get("feed")
            if require_opra and leg_feed != "opra":
                return {"ok": False, "reason": quote_error}
            quote_feeds.add(leg_feed if leg_feed in ("opra", "indicative") else "unverified")
            bid, ask = float(quote["bid"]), float(quote["ask"])
            if not (isfinite(bid) and isfinite(ask)) or bid < 0 or ask <= 0 or bid > ask:
                raise ValueError("invalid book")
            if leg["side"] == "sell" and bid <= 0:
                raise ValueError("no bid for a leg we sell")
            mids[leg["symbol"]] = bid / 2 + ask / 2 if bid > 0 else ask
            # The executable side: a leg we sell goes at its bid, a wing we buy
            # at its ask.
            natural_px[leg["symbol"]] = bid if leg["side"] == "sell" else ask
        except (AttributeError, KeyError, TypeError, ValueError):
            return {"ok": False, "reason": quote_error}

    def net(px):
        return (px[legs[0]["symbol"]] + px[legs[2]["symbol"]]) \
            - (px[legs[1]["symbol"]] + px[legs[3]["symbol"]])

    credit = net(mids)
    natural = net(natural_px)
    call_width = long_call - short_call
    put_width = short_put - long_put
    width = max(call_width, put_width)
    if not isfinite(credit) or not isfinite(natural) or not isfinite(width) or width <= 0:
        return {"ok": False, "reason": "nonfinite credit/width"}

    # A market too wide to trade is skipped. The natural credit (every short leg
    # at its bid, every wing at its ask) must be at least this share of the mid.
    # On the research log, priced at the natural and held to expiry, names
    # under 70% lost 30% of max loss; on this paper account a limit placed 6% or
    # more above the natural filled 2 times in 19 (to 2026-11-16).
    if natural <= 0 or credit <= 0:
        return {"ok": False, "reason": f"no credit at the natural (${natural:.2f}, mid ${credit:.2f})"}
    natural_to_mid = natural / credit
    min_ratio = float(getattr(settings, "iv_exec_min_natural_to_mid", 0.85))
    if natural_to_mid < min_ratio - 1e-9:
        return {"ok": False, "reason": f"market too wide: natural ${natural:.2f} is "
                                       f"{natural_to_mid:.0%} of mid ${credit:.2f} (min {min_ratio:.0%})"}

    # The limit is the natural, floored to a cent so it is never above it: the
    # price the market offers now, and the price the research log measures.
    # Sized against it, since it is the credit actually collected.
    accepted_credit = floor(round(natural * 100, 6)) / 100
    if accepted_credit < settings.iv_exec_min_credit:
        return {"ok": False, "reason": f"credit ${accepted_credit:.2f} at the natural < min"}
    if accepted_credit / width < MIN_CREDIT_WIDTH_RATIO:
        return {"ok": False, "reason": f"credit/width {accepted_credit / width:.2f} too thin"}
    max_loss_per = (width - accepted_credit) * CONTRACT_MULTIPLIER
    if max_loss_per <= 0:
        return {"ok": False, "reason": "non-positive max loss"}
    qty = int(risk_budget // max_loss_per)
    if qty < 1:
        return {"ok": False, "reason": f"one spread (${max_loss_per:.0f}) exceeds risk budget ${risk_budget:.0f}"}
    qty_cap = condor_qty_cap(equity, settings)
    qty = min(qty, qty_cap)

    # Account-wide limits. Collateral is the wider side's full width: the broker
    # holds that against the spread, and the credit is not counted as offsetting
    # it. A condor that does not fit even one spread is not opened.
    collateral_per = width * CONTRACT_MULTIPLIER
    limited_by = None
    for name, limit, unit in (("account_risk_cap", risk_headroom, max_loss_per),
                              ("cash_reserve", cash_available, collateral_per)):
        if limit is None:
            continue
        fits = int(limit // unit) if isfinite(limit) and limit > 0 else 0
        if fits < 1:
            what = ("account risk cap" if name == "account_risk_cap" else "cash reserve")
            return {"ok": False, "reason": f"{what}: one spread needs ${unit:,.0f}, "
                                           f"${max(limit, 0):,.0f} available"}
        if fits < qty:
            qty, limited_by = fits, name

    # Entry limit: the natural credit (negative = a credit received).
    limit_price = -accepted_credit

    return {
        "ok": True, "ticker": ticker, "expiry": exp_str, "earnings_date": setup.next_earnings_date,
        "legs": legs, "legs_json": json.dumps(legs),
        "strikes": {"short_put": short_put, "long_put": long_put,
                    "short_call": short_call, "long_call": long_call},
        "credit": accepted_credit, "credit_mid": round(credit, 2),
        "natural_to_mid": round(natural_to_mid, 3),
        "quote_feed": quote_feeds.pop() if len(quote_feeds) == 1 else "unverified",
        "max_loss": round(max_loss_per, 2),
        "qty": qty, "limit_price": limit_price,
        "risk_usd": round(max_loss_per * qty, 2),
        "risk_budget": round(risk_budget, 2), "qty_cap": qty_cap,
        "ceiling_binding": ceiling_binding,
        "collateral_usd": round(collateral_per * qty, 2), "limited_by": limited_by,
    }

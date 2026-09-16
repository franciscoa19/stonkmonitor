"""
IV/RV strategy-variant logger — MEASUREMENT ONLY, no execution.

For each earnings event we price several hypothetical structures off the same
live chain and log them, then resolve each against the realized move to compare
expectancy before promoting any variant to real execution. This is the
"validate before you execute" discipline applied to strategy *shape*: instead of
guessing whether shorts belong at 0.7×, 1.0×, or 1.3× the implied move — or
whether an iron fly or a naked straddle would have paid better — we let a season
of real earnings prints answer it, risking nothing.

Variants (all priced on the front expiry after the print):
  straddle     — short ATM call+put (undefined risk; the pure-premium benchmark)
  condor_0.7sd — iron condor, shorts at 0.7× the implied move (more credit, tighter)
  condor_1.0sd — iron condor at 1.0× (what we actually execute today)
  condor_1.3sd — iron condor at 1.3× (safer, thinner credit)
  fly          — iron fly, ATM shorts with wings 1× the move out (fat credit, needs a bigger move to lose)

P&L is the expiry-intrinsic payoff at the realized underlying price, per 1 spread
(×100). Our real condors are 1 DTE after the print, so intrinsic-at-resolution is
a faithful proxy for the actual buy-to-close.
"""
import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

VARIANTS = ["straddle", "condor_0.7sd", "condor_1.0sd", "condor_1.3sd", "fly"]
_MULT = 100


def expiry_settlement_date(expiry: str) -> Optional[str]:
    """First calendar day on which an expiry close is safe to evaluate.

    The evaluator retrieves the historical close for `expiry`, so waiting one
    calendar day prevents a background pass earlier on expiration day from
    treating an intraday price as the settlement price. Weekend/holiday passes
    remain safe because they still query the completed expiration session.
    """
    try:
        return (date.fromisoformat(str(expiry)) + timedelta(days=1)).isoformat()
    except (TypeError, ValueError):
        return None


def _im_frac(setup) -> float:
    try:
        return float(str(setup.expected_move or "0").rstrip("%")) / 100.0
    except Exception:
        return 0.0


def _nearest(strikes, target):
    return min(strikes, key=lambda k: abs(k - target)) if strikes else None


def variant_payoff(row: dict, final_price: float) -> float:
    """Expiry-intrinsic P&L ($ per 1 spread) at `final_price`. Works for condors,
    flies (short_put==short_call), and straddles/strangles (long_* NULL)."""
    P = float(final_price)
    C = float(row["credit"])
    sp, lp = row.get("short_put"), row.get("long_put")
    sc, lc = row.get("short_call"), row.get("long_call")
    put_loss = max(0.0, (sp - P)) if sp is not None else 0.0
    if lp is not None:
        put_loss = min(put_loss, sp - lp)
    call_loss = max(0.0, (P - sc)) if sc is not None else 0.0
    if lc is not None:
        call_loss = min(call_loss, lc - sc)
    return round((C - put_loss - call_loss) * _MULT, 2)


def _front_expiry_chain(trader, setup, settings):
    """(expiry_str, {strike:call}, {strike:put}) for the front expiry after the
    print within the DTE band, or None."""
    today = date.today()
    edate = None
    if getattr(setup, "next_earnings_date", None):
        try:
            edate = date.fromisoformat(setup.next_earnings_date)
        except Exception:
            edate = None
    start = max(today + timedelta(days=settings.iv_exec_min_dte),
                (edate + timedelta(days=1)) if edate else today + timedelta(days=settings.iv_exec_min_dte))
    end = today + timedelta(days=settings.iv_exec_max_dte)
    if start > end:
        return None
    calls = trader.get_option_contracts(setup.ticker, start, end, "call")
    puts = trader.get_option_contracts(setup.ticker, start, end, "put")
    if not calls or not puts:
        return None
    both = sorted({c["expiry"] for c in calls} & {p["expiry"] for p in puts})
    if not both:
        return None
    exp = both[0]
    cbs = {c["strike"]: c for c in calls if c["expiry"] == exp}
    pbs = {p["strike"]: p for p in puts if p["expiry"] == exp}
    if len(cbs) < 2 or len(pbs) < 2:
        return None
    exp_str = exp.isoformat() if hasattr(exp, "isoformat") else str(exp)
    return exp_str, cbs, pbs


def build_variants(trader, setup, settings) -> list[dict]:
    """Price every variant off one chain fetch. Returns a list of
    {variant, expiry, strikes{}, credit, max_loss}. Empty on any data gap."""
    spot = float(getattr(setup, "price", 0) or 0)
    em = _im_frac(setup)
    if spot <= 0 or em <= 0:
        return []
    chain = _front_expiry_chain(trader, setup, settings)
    if not chain:
        return []
    exp_str, cbs, pbs = chain
    calls, puts = sorted(cbs), sorted(pbs)
    move = spot * em
    wing = spot * settings.iv_exec_wing_width_pct
    atm_c = _nearest(calls, spot)
    atm_p = _nearest(puts, spot)

    # Assemble each variant's strike picks (None where a side is absent).
    specs = {}
    for mult, name in [(0.7, "condor_0.7sd"), (1.0, "condor_1.0sd"), (1.3, "condor_1.3sd")]:
        sc = _nearest([k for k in calls if k >= spot + mult * move], spot + mult * move)
        sp = _nearest([k for k in puts if k <= spot - mult * move], spot - mult * move)
        if sc is None or sp is None:
            continue
        lc = _nearest([k for k in calls if k > sc], sc + wing)
        lp = _nearest([k for k in puts if k < sp], sp - wing)
        if lc is None or lp is None:
            continue
        specs[name] = dict(short_call=sc, long_call=lc, short_put=sp, long_put=lp)
    # iron fly: ATM shorts, wings 1× move out
    fly_lc = _nearest([k for k in calls if k > atm_c], atm_c + move) if atm_c else None
    fly_lp = _nearest([k for k in puts if k < atm_p], atm_p - move) if atm_p else None
    if atm_c and atm_p and fly_lc and fly_lp:
        specs["fly"] = dict(short_call=atm_c, long_call=fly_lc, short_put=atm_p, long_put=fly_lp)
    # straddle: ATM shorts, no wings
    if atm_c and atm_p:
        specs["straddle"] = dict(short_call=atm_c, long_call=None, short_put=atm_p, long_put=None)

    # One quotes call for every symbol we touched.
    syms = set()
    for s in specs.values():
        if s["short_call"] in cbs: syms.add(cbs[s["short_call"]]["symbol"])
        if s["short_put"] in pbs:  syms.add(pbs[s["short_put"]]["symbol"])
        if s.get("long_call") in cbs: syms.add(cbs[s["long_call"]]["symbol"])
        if s.get("long_put") in pbs:  syms.add(pbs[s["long_put"]]["symbol"])
    quotes = trader.get_option_quotes(list(syms))

    def mid(bystrike, strike):
        sym = bystrike.get(strike, {}).get("symbol")
        return quotes.get(sym, {}).get("mid", 0) if sym else 0

    out = []
    for name, s in specs.items():
        c_short = mid(cbs, s["short_call"]) + mid(pbs, s["short_put"])
        c_long = (mid(cbs, s["long_call"]) if s.get("long_call") else 0) + \
                 (mid(pbs, s["long_put"]) if s.get("long_put") else 0)
        credit = c_short - c_long
        if credit <= 0:
            continue
        if s.get("long_call") is not None:
            width = max(s["long_call"] - s["short_call"], s["short_put"] - s["long_put"])
            max_loss = round(max(0.0, width - credit) * _MULT, 2)
        else:
            max_loss = None            # undefined risk (straddle)
        out.append({"variant": name, "expiry": exp_str, "strikes": s,
                    "credit": round(credit, 2), "max_loss": max_loss})
    return out

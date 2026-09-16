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

from market_time import et_today

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
    today = et_today()
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

    # Fill assumption (VALIDATION_SPEC §3): never price the book at mid. We sell
    # into the bid and buy at the ask, so the logged credit is one a real order
    # could actually have collected. `credit_mid` is kept only for reference.
    conservative = getattr(settings, "iv_conservative_fills", True)
    fee = float(getattr(settings, "iv_fee_per_contract", 0.65) or 0)

    def q(bystrike, strike, side):
        """Return the executable-side fill and mid, or ``None`` without a fill.

        A missing bid/ask is not permission to replace that side with mid.  That
        would turn an untradeable option into an optimistic simulated fill, which
        is exactly the liquidity bias the conservative model is meant to avoid.
        """
        sym = bystrike.get(strike, {}).get("symbol")
        rec = quotes.get(sym, {}) if sym else {}
        try:
            mid_px = float(rec.get("mid") or 0)
        except (TypeError, ValueError):
            mid_px = 0.0
        if not conservative:
            return (mid_px, mid_px) if mid_px > 0 else (None, None)
        try:
            px = float(rec.get("bid") if side == "sell" else rec.get("ask"))
        except (TypeError, ValueError):
            px = 0.0
        return (px, mid_px) if px > 0 else (None, mid_px)

    # Median gap between listed strikes — "one strike increment" for pin risk.
    def _step(ks):
        gaps = sorted(round(b - a, 4) for a, b in zip(ks, ks[1:]) if b > a)
        return gaps[len(gaps) // 2] if gaps else None
    strike_step = _step(calls) or _step(puts)

    out = []
    for name, s in specs.items():
        sc_fill, sc_mid = q(cbs, s["short_call"], "sell")
        sp_fill, sp_mid = q(pbs, s["short_put"], "sell")
        lc_fill, lc_mid = (q(cbs, s["long_call"], "buy") if s.get("long_call") else (0, 0))
        lp_fill, lp_mid = (q(pbs, s["long_put"], "buy") if s.get("long_put") else (0, 0))
        if any(px is None for px in (sc_fill, sp_fill, lc_fill, lp_fill)):
            logger.debug("Variant %s skipped: missing executable-side option quote", name)
            continue

        credit = (sc_fill + sp_fill) - (lc_fill + lp_fill)
        credit_mid = (sc_mid + sp_mid) - (lc_mid + lp_mid)
        if credit <= 0:
            continue                      # no edge left once you pay the spread
        n_legs = 4 if s.get("long_call") is not None else 2
        fees = round(n_legs * fee * 2, 2)  # open + close, per contract-leg

        if s.get("long_call") is not None:
            width = max(s["long_call"] - s["short_call"], s["short_put"] - s["long_put"])
            max_loss = round(max(0.0, width - credit) * _MULT + fees, 2)
        else:
            max_loss = None               # undefined risk (straddle)
        out.append({"variant": name, "expiry": exp_str, "strikes": s,
                    "credit": round(credit, 2), "credit_mid": round(credit_mid, 2),
                    "fees": fees, "n_legs": n_legs, "strike_step": strike_step,
                    "max_loss": max_loss})
    return out


def pin_risk(row: dict, final_price: float, strike_step: Optional[float]) -> bool:
    """True when the underlying settled within one strike increment of a short
    strike — assignment/pin territory, which must be flagged rather than
    silently assumed to be a clean cash settlement (VALIDATION_SPEC §3)."""
    if not strike_step or strike_step <= 0:
        return False
    for k in (row.get("short_put"), row.get("short_call")):
        if k is not None and abs(float(final_price) - float(k)) <= strike_step:
            return True
    return False

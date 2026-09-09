"""
Curated large-cap / high-liquidity universe for the earnings IV/RV scanner
(backend/signals/earnings_scanner.py).

Selection criteria — every name should satisfy all of these so the scanner's
three gates are actually testable, not just "true by accident":

  1. Share volume   — 30-day avg comfortably > 1.5M (the vol_ok gate). All names
                      here run 3M–100M+/day; none are borderline.
  2. Weekly expiries— required. ts_slope compares the *nearest* expiry to 45 DTE;
                      without weeklies the front point is a monthly and the
                      earnings IV spike is smeared across 3–4 weeks.
  3. Options depth  — ATM bid/ask tight enough that (bid+ask)/2 straddle pricing
                      gives a real expected_move (penny/nickel-wide at the money).
  4. Real earnings IV— stock has a measurable pre-earnings front-month IV bump so
                      the inversion (slope <= -0.00406) is detectable.
  5. Price band     — roughly $20–$1,000. Below ~$20 the straddle-% math gets
                      noisy on nickel spreads; above ~$1k a single straddle ties
                      up too much buying power for a short-premium book.

Tiers (for sizing / how much you trust a single signal):
  1 = mega-cap, deepest chains, historically move LESS than implied more often
      than not → best short-straddle candidates.
  2 = large-cap, deep weeklies, good IV crush; standard sizing.
  3 = liquid but "gap-prone": historically exceeds the implied move often enough
      that a naked straddle is dangerous. Trade as iron condor / defined risk,
      or wider strangle, or size down. Still worth scanning — the IV/RV ratio
      is usually the richest here.

Usage:
    from signals.earnings_universe import EARNINGS_UNIVERSE, tickers
    for t in tickers(max_tier=2): ...
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class UniverseEntry:
    ticker: str
    sector: str
    tier: int
    note: str = ""


EARNINGS_UNIVERSE: list[UniverseEntry] = [
    # ── Tier 1: mega-cap core ─────────────────────────────────────────────
    UniverseEntry("AAPL", "Tech",        1, "Most liquid chain on the board; tends to under-move implied"),
    UniverseEntry("MSFT", "Tech",        1, "Very consistent IV crush, modest realized moves"),
    UniverseEntry("GOOGL","Tech",        1, "Use GOOGL over GOOG for options depth"),
    UniverseEntry("AMZN", "Tech",        1, "Clean weeklies; moves usually inside implied"),
    UniverseEntry("NVDA", "Semis",       1, "Enormous IV bump pre-earnings; occasional outsized move"),
    UniverseEntry("AVGO", "Semis",       1, "Post-split liquidity excellent"),
    UniverseEntry("JPM",  "Financials",  1, "Kicks off bank season; low realized surprise"),
    UniverseEntry("V",    "Financials",  1, "Textbook low-RV, overpriced-IV name"),
    UniverseEntry("MA",   "Financials",  1, "Same profile as V"),
    UniverseEntry("COST", "Consumer",    1, "Monthly sales pre-releases dampen earnings surprise"),
    UniverseEntry("WMT",  "Consumer",    1, "Deep chain, small moves"),
    UniverseEntry("HD",   "Consumer",    1, "Reliable IV crush"),
    UniverseEntry("LLY",  "Healthcare",  1, "High $ price but liquid; big IV premium"),
    UniverseEntry("UNH",  "Healthcare",  1, "First healthcare report of season"),
    UniverseEntry("JNJ",  "Healthcare",  1, "Very low RV; ratio clears 1.25 easily"),
    UniverseEntry("XOM",  "Energy",      1, "Guides/pre-announces; earnings rarely shock"),

    # ── Tier 2: large-cap, deep weeklies ──────────────────────────────────
    UniverseEntry("AMD",  "Semis",       2, "Rich IV; moves can be large — watch RV"),
    UniverseEntry("MU",   "Semis",       2, "Off-cycle reporter (Jun/Sep/Dec/Mar) — fills quiet weeks"),
    UniverseEntry("QCOM", "Semis",       2),
    UniverseEntry("TXN",  "Semis",       2, "Boring in a good way"),
    UniverseEntry("AMAT", "Semis",       2),
    UniverseEntry("LRCX", "Semis",       2, "Post-split, now cheap enough for straddles"),
    UniverseEntry("MRVL", "Semis",       2),
    UniverseEntry("INTC", "Semis",       2, "Cheap stock — check price band, huge volume"),
    UniverseEntry("ORCL", "Software",    2, "Off-cycle reporter; IV bump has grown a lot"),
    UniverseEntry("CRM",  "Software",    2),
    UniverseEntry("ADBE", "Software",    2, "Off-cycle reporter"),
    UniverseEntry("NOW",  "Software",    2, "Post-split liquidity good"),
    UniverseEntry("IBM",  "Software",    2, "Low RV, dependable crush"),
    UniverseEntry("CSCO", "Software",    2),
    UniverseEntry("PANW", "Software",    2),
    UniverseEntry("CRWD", "Software",    2),
    UniverseEntry("DELL", "Hardware",    2),
    UniverseEntry("UBER", "Internet",    2),
    UniverseEntry("ABNB", "Internet",    2),
    UniverseEntry("BAC",  "Financials",  2, "Very high share volume; tight strikes"),
    UniverseEntry("WFC",  "Financials",  2),
    UniverseEntry("C",    "Financials",  2),
    UniverseEntry("GS",   "Financials",  2),
    UniverseEntry("MS",   "Financials",  2),
    UniverseEntry("AXP",  "Financials",  2),
    UniverseEntry("PYPL", "Financials",  2),
    UniverseEntry("SCHW", "Financials",  2),
    UniverseEntry("BLK",  "Financials",  2, "High $ price; size accordingly"),
    UniverseEntry("MCD",  "Consumer",    2),
    UniverseEntry("SBUX", "Consumer",    2),
    UniverseEntry("NKE",  "Consumer",    2, "Off-cycle reporter"),
    UniverseEntry("TGT",  "Consumer",    2),
    UniverseEntry("LOW",  "Consumer",    2),
    UniverseEntry("DIS",  "Media",       2),
    UniverseEntry("CMCSA","Media",       2),
    UniverseEntry("T",    "Telecom",     2, "Low price — confirm >$20 band"),
    UniverseEntry("VZ",   "Telecom",     2),
    UniverseEntry("PG",   "Staples",     2, "Ultra-low RV"),
    UniverseEntry("KO",   "Staples",     2),
    UniverseEntry("PEP",  "Staples",     2),
    UniverseEntry("PFE",  "Healthcare",  2, "Low price — confirm >$20 band"),
    UniverseEntry("MRK",  "Healthcare",  2),
    UniverseEntry("ABBV", "Healthcare",  2),
    UniverseEntry("CVS",  "Healthcare",  2),
    UniverseEntry("BA",   "Industrials", 2, "Headline risk outside earnings — check RV window"),
    UniverseEntry("CAT",  "Industrials", 2),
    UniverseEntry("GE",   "Industrials", 2),
    UniverseEntry("DE",   "Industrials", 2),
    UniverseEntry("FDX",  "Industrials", 2, "Off-cycle reporter"),
    UniverseEntry("UPS",  "Industrials", 2),
    UniverseEntry("CVX",  "Energy",      2),

    # ── Tier 3: liquid but gap-prone — defined-risk only ──────────────────
    UniverseEntry("TSLA", "Auto",        3, "Frequently exceeds implied; enormous chain"),
    UniverseEntry("NFLX", "Media",       3, "Historically the poster child for blowing through the straddle"),
    UniverseEntry("META", "Internet",    3, "Guidance-driven 10%+ moves happen"),
    UniverseEntry("PLTR", "Software",    3, "Retail-driven; realized moves huge"),
    UniverseEntry("COIN", "Financials",  3, "Crypto beta overwhelms earnings"),
    UniverseEntry("SHOP", "Software",    3),
    UniverseEntry("SNOW", "Software",    3, "Off-cycle reporter"),
    UniverseEntry("ARM",  "Semis",       3),
    UniverseEntry("SMCI", "Hardware",    3, "Accounting/headline history — extra caution"),
    UniverseEntry("CMG",  "Consumer",    3, "Post-split; big reactions to comps"),
    UniverseEntry("LULU", "Consumer",    3),
]


def tickers(max_tier: int = 3, sectors: list[str] | None = None) -> list[str]:
    """Return ticker symbols filtered by tier and optional sector list."""
    return [
        e.ticker for e in EARNINGS_UNIVERSE
        if e.tier <= max_tier and (sectors is None or e.sector in sectors)
    ]


def by_tier(tier: int) -> list[UniverseEntry]:
    return [e for e in EARNINGS_UNIVERSE if e.tier == tier]


# Names deliberately EXCLUDED and why — so nobody re-adds them:
#   BKNG, NVR, AZO, MTD  — >$1k–$5k share price; one straddle = too much BP
#   BRK.B                — no meaningful earnings IV bump (reports Saturdays)
#   GOOG                 — thinner chain than GOOGL
#   F, PLUG, SOFI, NIO   — sub-$20; straddle-% distorted by nickel spreads
#   SNAP, ROKU, AFRM, UPST — mid-cap, routinely 20%+ moves; not "high cap"
#   ADRs (BABA, TSM, ASML) — TSM/ASML are liquid but report pre-market from
#                            Asia/Europe; timing of the IV crush is messier.
#                            Add back if you want them.

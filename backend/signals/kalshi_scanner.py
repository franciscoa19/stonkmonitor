"""
Kalshi Market Scanner + Edge Scorer

Strategy: find markets where the TRUE probability is meaningfully higher
(or lower) than what the market is pricing, then Kelly-size a bet.

Probability estimation methods (stacked in order of confidence):
  1. Near-expiry + extreme price  → "locked" market (99%+ confidence)
  2. Category rules               → known high-confidence categories
  3. Market sentiment + volume    → liquidity-weighted crowd wisdom

Edge definition:
  edge = true_prob - market_yes_price   (for YES bets)
  edge = (1 - true_prob) - market_no_price  (for NO bets)

We only act when edge >= MIN_EDGE (default 0.05 = 5 percentage points).

Kelly sizing:
  f = (edge * (1/price) - (1 - true_prob)) / (1/price - 1)
  capped at MAX_KELLY_FRACTION (0.25) and MAX_BET_USD

Categories we look for:
  - "Will X happen before end of day/week?" where X is near-certain
  - Fed meeting outcomes (already known in advance)
  - Economic data releases where one outcome is overwhelmingly likely
  - Sports/weather events close to resolution with clear outcome

Noise filters:
  - Min volume: 500 contracts (enough for liquid fill)
  - Max DTE: 7 days (shorter = more predictable, less noise)
  - Min liquidity: spread <= 10¢ (tight market)
  - Skip markets with result already decided but not settled
"""
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Tunable constants ─────────────────────────────────────────────────────────
MIN_EDGE              = 0.05   # 5% edge minimum to consider
MIN_VOLUME            = 500    # contracts traded total
MAX_DTE               = 7      # max days to expiry
MAX_SPREAD_CENTS      = 12     # reject if bid/ask spread > 12¢
MAX_KELLY_FRACTION    = 0.25   # never bet more than 25% of Kelly
MAX_BET_USD           = 500    # hard cap per market
MAX_EXPOSURE_PCT      = 0.03   # max 3% of balance per bet

# Markets we know tend to be near-certain close to expiry
HIGH_CONF_KEYWORDS = [
    "fed funds rate",
    "interest rate decision",
    "fomc",
    "will the fed",
    "cpi",
    "unemployment rate",
    "nonfarm payroll",
    "gdp",
    "recession",
]

# Categories Kalshi uses — these tend to have predictable outcomes
GOOD_CATEGORIES = {
    "economics",
    "financials",
    "politics",
    "fed",
    "rates",
}


@dataclass
class KalshiOpportunity:
    ticker: str
    title: str
    side: str             # "yes" | "no"
    market_price: float   # current price 0-1
    true_prob: float      # our estimated probability 0-1
    edge: float           # true_prob - market_price (for YES) or inverse for NO
    dte: float            # days to expiry
    volume: int
    spread: float         # bid/ask spread in cents
    kelly_fraction: float
    bet_contracts: int
    bet_cost_usd: float
    confidence: str       # "locked" | "high" | "medium"
    rationale: str

    def score(self) -> float:
        """0-10 score for sorting/display."""
        s = 0.0
        # Edge quality
        s += min(self.edge * 40, 4.0)       # up to 4 pts for edge
        # Confidence
        conf_bonus = {"locked": 3.0, "high": 2.0, "medium": 1.0}
        s += conf_bonus.get(self.confidence, 0)
        # Time remaining (closer = more certain)
        if self.dte <= 1:   s += 2.0
        elif self.dte <= 3: s += 1.5
        elif self.dte <= 7: s += 1.0
        # Liquidity
        if self.volume > 5000:   s += 0.5
        elif self.volume > 1000: s += 0.2
        return min(round(s, 2), 10.0)

    def to_dict(self) -> dict:
        return {
            "ticker":        self.ticker,
            "title":         self.title,
            "side":          self.side,
            "market_price":  round(self.market_price, 4),
            "true_prob":     round(self.true_prob, 4),
            "edge":          round(self.edge, 4),
            "dte":           round(self.dte, 2),
            "volume":        self.volume,
            "spread":        self.spread,
            "kelly_fraction":round(self.kelly_fraction, 4),
            "bet_contracts": self.bet_contracts,
            "bet_cost_usd":  round(self.bet_cost_usd, 2),
            "confidence":    self.confidence,
            "rationale":     self.rationale,
            "score":         self.score(),
        }


class KalshiScanner:
    def __init__(self, settings=None):
        self.settings = settings

    def _dte(self, close_time: str) -> float:
        """Days to expiry from ISO string."""
        try:
            exp = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return max(0.0, (exp - now).total_seconds() / 86400)
        except Exception:
            return 999.0

    def _estimate_true_prob(self, market: dict) -> tuple[float, str]:
        """
        Estimate the true probability of YES resolving.
        Returns (probability, confidence_level).

        Confidence levels:
          locked  — near-expiry + extreme price → almost certain
          high    — known high-conf category, close to expiry
          medium  — our best model guess
        """
        yes_ask = market.get("yes_ask", 50) / 100
        yes_bid = market.get("yes_bid", 50) / 100
        mid     = (yes_ask + yes_bid) / 2
        dte     = self._dte(market.get("close_time", ""))
        title   = (market.get("title") or "").lower()
        subtitle= (market.get("subtitle") or "").lower()
        text    = title + " " + subtitle

        # ── LOCKED: market is already essentially decided ──────────────────
        # Very close to expiry + extreme pricing = market has made up its mind
        if dte <= 0.5 and mid >= 0.95:
            return 0.99, "locked"
        if dte <= 0.5 and mid <= 0.05:
            return 0.01, "locked"
        if dte <= 1.0 and mid >= 0.97:
            return 0.985, "locked"
        if dte <= 1.0 and mid <= 0.03:
            return 0.015, "locked"

        # ── HIGH CONFIDENCE: known categories near resolution ─────────────
        is_high_conf_cat = any(kw in text for kw in HIGH_CONF_KEYWORDS)

        if is_high_conf_cat and dte <= 2:
            # Economic data markets close to release — crowd is well-informed
            if mid >= 0.90:
                return min(mid + 0.04, 0.99), "high"
            if mid <= 0.10:
                return max(mid - 0.04, 0.01), "high"

        # ── MEDIUM: general market wisdom with slight edge model ───────────
        # High-volume markets with extreme prices are still informative
        volume = market.get("volume", 0)
        if volume > 10_000 and mid >= 0.88 and dte <= 3:
            return min(mid + 0.03, 0.99), "medium"
        if volume > 10_000 and mid <= 0.12 and dte <= 3:
            return max(mid - 0.03, 0.01), "medium"

        # Default: no exploitable edge found
        return mid, "medium"

    def _kelly(self, prob: float, price: float) -> float:
        """
        Kelly fraction for binary bet.
        prob  = true probability of winning
        price = cost per contract (0-1)
        payout = 1.0 (win $1 per contract, net of cost = 1-price)
        loss   = price (lose cost if wrong)

        Kelly: f = (prob * (1-price) - (1-prob) * price) / ((1-price))
             = (prob - price) / (1 - price)
        """
        if price <= 0 or price >= 1:
            return 0.0
        net_win  = 1 - price
        net_loss = price
        f = (prob * net_win - (1 - prob) * net_loss) / net_win
        return max(0.0, min(f, MAX_KELLY_FRACTION))

    def scan(self, markets: list[dict], balance_usd: float) -> list[KalshiOpportunity]:
        """
        Scan all markets, return list of opportunities sorted by score.
        balance_usd = current Kalshi balance in dollars.
        """
        opps: list[KalshiOpportunity] = []

        for m in markets:
            try:
                if m.get("status") != "open":
                    continue

                dte     = self._dte(m.get("close_time", ""))
                volume  = m.get("volume", 0) or 0
                yes_ask = m.get("yes_ask") or 0   # cents
                yes_bid = m.get("yes_bid") or 0
                no_ask  = m.get("no_ask")  or 0
                no_bid  = m.get("no_bid")  or 0

                # ── Basic filters ──────────────────────────────────────────
                if dte > MAX_DTE:
                    continue
                if dte <= 0:
                    continue
                if volume < MIN_VOLUME:
                    continue
                if yes_ask <= 0 or yes_bid <= 0:
                    continue

                spread = yes_ask - yes_bid
                if spread > MAX_SPREAD_CENTS:
                    continue

                true_prob, confidence = self._estimate_true_prob(m)

                # ── Check YES bet ──────────────────────────────────────────
                yes_price  = yes_ask / 100   # cost to buy YES
                yes_edge   = true_prob - yes_price
                if yes_edge >= MIN_EDGE:
                    kf   = self._kelly(true_prob, yes_price)
                    max_bet = min(
                        balance_usd * MAX_EXPOSURE_PCT,
                        MAX_BET_USD,
                        balance_usd * MAX_KELLY_FRACTION * kf,
                    )
                    # contracts: each YES contract costs yes_ask cents = $yes_ask/100
                    contracts = max(1, int(max_bet / (yes_ask / 100))) if yes_ask > 0 else 0
                    cost = contracts * (yes_ask / 100)
                    opps.append(KalshiOpportunity(
                        ticker=m["ticker"],
                        title=m.get("title", ""),
                        side="yes",
                        market_price=yes_price,
                        true_prob=true_prob,
                        edge=yes_edge,
                        dte=dte,
                        volume=volume,
                        spread=spread,
                        kelly_fraction=kf,
                        bet_contracts=contracts,
                        bet_cost_usd=cost,
                        confidence=confidence,
                        rationale=(
                            f"YES @ {yes_ask}¢ | true prob ~{true_prob*100:.1f}% | "
                            f"edge {yes_edge*100:.1f}% | {dte:.1f}d to close"
                        ),
                    ))

                # ── Check NO bet ───────────────────────────────────────────
                no_price = no_ask / 100
                no_prob  = 1 - true_prob
                no_edge  = no_prob - no_price
                if no_edge >= MIN_EDGE:
                    kf   = self._kelly(no_prob, no_price)
                    max_bet = min(
                        balance_usd * MAX_EXPOSURE_PCT,
                        MAX_BET_USD,
                        balance_usd * MAX_KELLY_FRACTION * kf,
                    )
                    contracts = max(1, int(max_bet / (no_ask / 100))) if no_ask > 0 else 0
                    cost = contracts * (no_ask / 100)
                    opps.append(KalshiOpportunity(
                        ticker=m["ticker"],
                        title=m.get("title", ""),
                        side="no",
                        market_price=no_price,
                        true_prob=no_prob,
                        edge=no_edge,
                        dte=dte,
                        volume=volume,
                        spread=spread,
                        kelly_fraction=kf,
                        bet_contracts=contracts,
                        bet_cost_usd=cost,
                        confidence=confidence,
                        rationale=(
                            f"NO @ {no_ask}¢ | true prob ~{no_prob*100:.1f}% | "
                            f"edge {no_edge*100:.1f}% | {dte:.1f}d to close"
                        ),
                    ))

            except Exception as e:
                logger.warning(f"Scanner error on {m.get('ticker','?')}: {e}")

        opps.sort(key=lambda x: x.score(), reverse=True)
        return opps

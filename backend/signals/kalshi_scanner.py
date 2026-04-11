"""
Kalshi Market Scanner

Strategy: surface the most interesting prediction market opportunities
for human evaluation. We don't claim to beat 3M-contract efficient markets
with a model. Instead we find:

  1. NEAR-CERTAIN   — <30d to close, price >=95¢ or <=5¢ → near-guaranteed
     Play: buy the cheaper side for lotto upside, or buy the near-certain
     side for a low-risk yield

  2. HIGH-VOLUME EXTREME  — massive volume + extreme price → crowd has decided
     Play: fade or follow depending on your view

  3. RECENT MOVERS — last_price moved significantly vs previous_price
     Play: momentum or mean-reversion depending on catalyst

  4. BALANCED MARKETS — price near 50¢ with high volume = active debate
     Play: research the outcome and take a side

Score = extremeness × volume × time-urgency
User reviews in dashboard and decides; one-tap execution sends to Kalshi.

Real API fields (all prices in dollars 0.0–1.0):
  yes_ask_dollars, yes_bid_dollars, no_ask_dollars, no_bid_dollars
  volume_fp, last_price_dollars, previous_yes_ask_dollars, close_time
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MIN_VOLUME   = 1_000    # min contracts for any market to surface
MAX_SPREAD   = 0.15     # skip markets with wide bid/ask


@dataclass
class KalshiOpportunity:
    ticker: str
    title: str
    event_title: str
    category: str
    side: str           # "yes" | "no" | "watch"
    market_price: float # ask price for the recommended side (0–1)
    yes_ask: float
    yes_bid: float
    no_ask: float
    dte: float
    volume: float
    spread: float
    price_move: float   # last_price - previous_price (momentum signal)
    opportunity_type: str  # "near_certain" | "high_vol_extreme" | "mover" | "active"
    bet_contracts: int
    bet_cost_usd: float
    rationale: str

    def score(self) -> float:
        s = 0.0
        # Extremeness of price (how far from 50¢)
        extremeness = abs(self.yes_ask - 0.5) * 2   # 0-1
        s += extremeness * 3.0

        # Time urgency
        if self.dte <= 1:    s += 3.0
        elif self.dte <= 7:  s += 2.0
        elif self.dte <= 30: s += 1.5
        elif self.dte <= 90: s += 1.0

        # Volume
        if self.volume > 1_000_000: s += 2.0
        elif self.volume > 100_000: s += 1.5
        elif self.volume > 10_000:  s += 1.0

        # Momentum signal (big recent move = opportunity)
        if abs(self.price_move) >= 0.10: s += 1.5
        elif abs(self.price_move) >= 0.05: s += 0.5

        # Type bonus
        if self.opportunity_type == "near_certain": s += 1.5

        return min(round(s, 2), 10.0)

    def to_dict(self) -> dict:
        return {
            "ticker":           self.ticker,
            "title":            self.title,
            "event_title":      self.event_title,
            "category":         self.category,
            "side":             self.side,
            "market_price":     round(self.market_price, 4),
            "market_price_cents": round(self.market_price * 100, 1),
            "yes_ask":          round(self.yes_ask, 4),
            "yes_bid":          round(self.yes_bid, 4),
            "no_ask":           round(self.no_ask, 4),
            "dte":              round(self.dte, 1),
            "volume":           int(self.volume),
            "spread":           round(self.spread, 4),
            "price_move":       round(self.price_move, 4),
            "price_move_pct":   round(self.price_move * 100, 1),
            "opportunity_type": self.opportunity_type,
            "bet_contracts":    self.bet_contracts,
            "bet_cost_usd":     round(self.bet_cost_usd, 2),
            "rationale":        self.rationale,
            "score":            self.score(),
        }


class KalshiScanner:
    def __init__(self, settings=None):
        self.settings = settings

    def _dte(self, close_time: str) -> float:
        try:
            exp = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            return (exp - datetime.now(timezone.utc)).total_seconds() / 86400
        except Exception:
            return 9999.0

    def _size(self, balance_usd: float, price: float, max_pct: float = 0.03, cap: float = 500.0) -> tuple[int, float]:
        max_spend = min(balance_usd * max_pct, cap)
        contracts = max(1, int(max_spend / price)) if price > 0 else 0
        return contracts, round(contracts * price, 2)

    def scan(self, markets: list[dict], balance_usd: float) -> list[KalshiOpportunity]:
        opps: list[KalshiOpportunity] = []

        for m in markets:
            try:
                ya   = float(m.get("yes_ask_dollars") or 0)
                yb   = float(m.get("yes_bid_dollars") or 0)
                na   = float(m.get("no_ask_dollars")  or 0)
                lp   = float(m.get("last_price_dollars") or 0)
                prev = float(m.get("previous_yes_ask_dollars") or lp)
                vol  = float(m.get("volume_fp") or 0)
                dte  = self._dte(m.get("close_time", ""))

                if dte <= 0:          continue
                if ya <= 0:           continue
                if vol < MIN_VOLUME:  continue

                spread = (ya - yb) if yb > 0 else ya
                if spread > MAX_SPREAD: continue

                price_move = lp - prev

                ticker      = m.get("ticker", "")
                disp_title  = m.get("title") or m.get("event_title") or ""
                event_title = m.get("event_title") or disp_title
                category    = (m.get("event_category") or "").lower()

                # ── 1. NEAR-CERTAIN: extreme price + short DTE ────────────
                if dte <= 30 and (ya <= 0.05 or ya >= 0.95):
                    if ya <= 0.05:
                        # YES is cheap — lotto upside if it resolves YES
                        contracts, cost = self._size(balance_usd, ya, 0.01, 50.0)
                        opps.append(KalshiOpportunity(
                            ticker=ticker, title=disp_title, event_title=event_title,
                            category=category, side="yes",
                            market_price=ya, yes_ask=ya, yes_bid=yb, no_ask=na,
                            dte=dte, volume=vol, spread=spread, price_move=price_move,
                            opportunity_type="near_certain",
                            bet_contracts=contracts, bet_cost_usd=cost,
                            rationale=(
                                f"YES @ {ya*100:.0f}¢ ({dte:.0f}d) | "
                                f"Near-certain NO — buy YES for lotto upside | "
                                f"vol={vol/1000:.0f}k"
                            ),
                        ))
                    else:
                        # YES is near-certain — buy for near-guaranteed return
                        contracts, cost = self._size(balance_usd, ya, 0.02, 200.0)
                        opps.append(KalshiOpportunity(
                            ticker=ticker, title=disp_title, event_title=event_title,
                            category=category, side="yes",
                            market_price=ya, yes_ask=ya, yes_bid=yb, no_ask=na,
                            dte=dte, volume=vol, spread=spread, price_move=price_move,
                            opportunity_type="near_certain",
                            bet_contracts=contracts, bet_cost_usd=cost,
                            rationale=(
                                f"YES @ {ya*100:.0f}¢ ({dte:.0f}d) | "
                                f"Near-certain YES — {round((1-ya)*100,1)}¢ upside | "
                                f"vol={vol/1000:.0f}k"
                            ),
                        ))

                # ── 2. HIGH-VOL EXTREME: any DTE ──────────────────────────
                elif vol > 100_000 and (ya <= 0.08 or ya >= 0.92):
                    side = "yes" if ya >= 0.92 else "no"
                    price = ya if side == "yes" else na
                    if price > 0:
                        contracts, cost = self._size(balance_usd, price, 0.02, 200.0)
                        opps.append(KalshiOpportunity(
                            ticker=ticker, title=disp_title, event_title=event_title,
                            category=category, side=side,
                            market_price=price, yes_ask=ya, yes_bid=yb, no_ask=na,
                            dte=dte, volume=vol, spread=spread, price_move=price_move,
                            opportunity_type="high_vol_extreme",
                            bet_contracts=contracts, bet_cost_usd=cost,
                            rationale=(
                                f"{side.upper()} @ {price*100:.0f}¢ | "
                                f"High-volume crowd consensus | "
                                f"vol={vol/1000:.0f}k | {dte:.0f}d"
                            ),
                        ))

                # ── 3. RECENT MOVER: significant price change ──────────────
                elif abs(price_move) >= 0.08 and vol > 10_000:
                    side = "yes" if price_move > 0 else "no"
                    price = ya if side == "yes" else na
                    if price and price > 0:
                        contracts, cost = self._size(balance_usd, price, 0.02, 200.0)
                        direction = "UP" if price_move > 0 else "DOWN"
                        opps.append(KalshiOpportunity(
                            ticker=ticker, title=disp_title, event_title=event_title,
                            category=category, side=side,
                            market_price=price, yes_ask=ya, yes_bid=yb, no_ask=na,
                            dte=dte, volume=vol, spread=spread, price_move=price_move,
                            opportunity_type="mover",
                            bet_contracts=contracts, bet_cost_usd=cost,
                            rationale=(
                                f"Price moved {direction} {abs(price_move)*100:.0f}¢ | "
                                f"Current: {price*100:.0f}¢ | "
                                f"vol={vol/1000:.0f}k | {dte:.0f}d"
                            ),
                        ))

                # ── 4. ACTIVE DEBATE: near 50¢ with huge volume ────────────
                elif vol > 500_000 and 0.30 <= ya <= 0.70:
                    contracts, cost = self._size(balance_usd, ya, 0.02, 100.0)
                    opps.append(KalshiOpportunity(
                        ticker=ticker, title=disp_title, event_title=event_title,
                        category=category, side="watch",
                        market_price=ya, yes_ask=ya, yes_bid=yb, no_ask=na,
                        dte=dte, volume=vol, spread=spread, price_move=price_move,
                        opportunity_type="active",
                        bet_contracts=contracts, bet_cost_usd=cost,
                        rationale=(
                            f"Active 50/50 debate | YES={ya*100:.0f}¢ NO={na*100:.0f}¢ | "
                            f"vol={vol/1000:.0f}k | {dte:.0f}d — take a side if you have a view"
                        ),
                    ))

            except Exception as e:
                logger.warning(f"Scanner error on {m.get('ticker','?')}: {e}")

        opps.sort(key=lambda x: x.score(), reverse=True)
        return opps

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

  5. YIELD FARMING — 88-95¢ band + short DTE (<=3d) → annualized yield farming
     Play: buy the near-certain side for high annualized returns

  6. SMART MONEY — volume Z-score > 2.5 vs rolling history + directional price move
     Play: follow the spike, someone knows something

Scoring = extremeness × volume × time-urgency × (type bonus)
Maker pricing: execution placed at bid-side to earn spread instead of pay it.

Real API fields (all prices in dollars 0.0–1.0):
  yes_ask_dollars, yes_bid_dollars, no_ask_dollars, no_bid_dollars
  volume_fp, last_price_dollars, previous_yes_ask_dollars, close_time
"""
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MIN_VOLUME       = 1_000    # min contracts for any market to surface
MAX_SPREAD       = 0.15     # skip markets with wide bid/ask
VOL_HIST_LEN     = 20       # rolling window for smart-money Z-score
SMART_MONEY_Z    = 2.5      # Z-score threshold
SMART_MONEY_MOVE = 0.03     # min directional move to pair with spike


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
    no_bid: float = 0.0
    dte: float = 0.0
    volume: float = 0.0
    spread: float = 0.0
    price_move: float = 0.0   # last_price - previous_price (momentum signal)
    opportunity_type: str = ""  # "near_certain" | "high_vol_extreme" | "mover" | "active" | "yield_farm" | "smart_money"
    bet_contracts: int = 0
    bet_cost_usd: float = 0.0
    rationale: str = ""
    annualized_yield_pct: float = 0.0   # projected annualized return if wins
    volume_zscore: float = 0.0          # only populated for smart_money
    maker_price: float = 0.0            # price we'd target as limit order (bid side)

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

        # Type bonuses
        if self.opportunity_type == "near_certain": s += 1.5
        elif self.opportunity_type == "smart_money": s += 2.0
        elif self.opportunity_type == "yield_farm":
            # High yield = higher score
            if self.annualized_yield_pct >= 500: s += 2.5
            elif self.annualized_yield_pct >= 200: s += 2.0
            elif self.annualized_yield_pct >= 100: s += 1.5
            else: s += 1.0

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
            "maker_price":      round(self.maker_price, 4),
            "maker_price_cents": round(self.maker_price * 100, 1),
            "yes_ask":          round(self.yes_ask, 4),
            "yes_bid":          round(self.yes_bid, 4),
            "no_ask":           round(self.no_ask, 4),
            "no_bid":           round(self.no_bid, 4),
            "dte":              round(self.dte, 1),
            "volume":           int(self.volume),
            "spread":           round(self.spread, 4),
            "price_move":       round(self.price_move, 4),
            "price_move_pct":   round(self.price_move * 100, 1),
            "opportunity_type": self.opportunity_type,
            "bet_contracts":    self.bet_contracts,
            "bet_cost_usd":     round(self.bet_cost_usd, 2),
            "rationale":        self.rationale,
            "annualized_yield_pct": round(self.annualized_yield_pct, 1),
            "volume_zscore":    round(self.volume_zscore, 2),
            "score":            self.score(),
        }


class KalshiScanner:
    def __init__(self, settings=None):
        self.settings = settings
        # Rolling per-ticker volume history for smart-money Z-score detection
        self._volume_history: dict[str, deque] = {}
        # Last-seen volume to compute per-scan deltas
        self._last_volume: dict[str, float] = {}

    # ── Helpers ─────────────────────────────────────────────────────────
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

    def _annualized_yield(self, price: float, dte_days: float) -> float:
        """
        If we pay `price` for a $1 contract and it resolves our way, payoff is
        (1 - price). Compute annualized simple-interest yield.
        """
        if price <= 0 or price >= 1 or dte_days <= 0:
            return 0.0
        period_return = (1.0 - price) / price
        annualized = period_return * (365.0 / max(dte_days, 0.5))
        return annualized * 100.0

    def _maker_price(self, side: str, yb: float, ya: float, nb: float, na: float) -> float:
        """
        Pick a maker-side limit price. For YES we bid at yes_bid (passive),
        accepting we might not fill immediately; for NO we bid at no_bid.
        Fall back to ask if bid is zero.
        """
        if side == "yes":
            return yb if yb > 0 else ya
        if side == "no":
            return nb if nb > 0 else na
        return ya

    def _update_volume_history(self, ticker: str, vol: float) -> tuple[float, float]:
        """Returns (delta_since_last, zscore_of_delta)."""
        last = self._last_volume.get(ticker, vol)
        delta = max(0.0, vol - last)
        self._last_volume[ticker] = vol

        hist = self._volume_history.setdefault(ticker, deque(maxlen=VOL_HIST_LEN))
        hist.append(delta)

        if len(hist) < 5:
            return delta, 0.0
        mean = sum(hist) / len(hist)
        var = sum((x - mean) ** 2 for x in hist) / len(hist)
        std = math.sqrt(var) if var > 0 else 0.0
        if std == 0:
            return delta, 0.0
        z = (delta - mean) / std
        return delta, z

    # ── Main scan ───────────────────────────────────────────────────────
    def scan(self, markets: list[dict], balance_usd: float) -> list[KalshiOpportunity]:
        opps: list[KalshiOpportunity] = []

        for m in markets:
            try:
                ya   = float(m.get("yes_ask_dollars") or 0)
                yb   = float(m.get("yes_bid_dollars") or 0)
                na   = float(m.get("no_ask_dollars")  or 0)
                nb   = float(m.get("no_bid_dollars")  or 0)
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

                # Track volume delta + Z-score for smart-money detection
                vol_delta, vol_z = self._update_volume_history(ticker, vol)

                # ── 6. SMART MONEY: volume spike + directional move ───────
                # Highest priority — fires before other types
                if vol_z >= SMART_MONEY_Z and abs(price_move) >= SMART_MONEY_MOVE:
                    side = "yes" if price_move > 0 else "no"
                    price = ya if side == "yes" else na
                    if price > 0:
                        contracts, cost = self._size(balance_usd, price, 0.02, 200.0)
                        maker = self._maker_price(side, yb, ya, nb, na)
                        opps.append(KalshiOpportunity(
                            ticker=ticker, title=disp_title, event_title=event_title,
                            category=category, side=side,
                            market_price=price, yes_ask=ya, yes_bid=yb,
                            no_ask=na, no_bid=nb,
                            dte=dte, volume=vol, spread=spread, price_move=price_move,
                            opportunity_type="smart_money",
                            bet_contracts=contracts, bet_cost_usd=cost,
                            volume_zscore=vol_z,
                            maker_price=maker,
                            annualized_yield_pct=self._annualized_yield(price, dte),
                            rationale=(
                                f"🐋 SMART MONEY | {side.upper()} @ {price*100:.0f}¢ | "
                                f"Vol Z-score {vol_z:.1f}σ + {abs(price_move)*100:.0f}¢ move "
                                f"{'UP' if price_move > 0 else 'DOWN'} | "
                                f"Δvol={vol_delta/1000:.0f}k | {dte:.0f}d"
                            ),
                        ))
                        continue   # exclusive — don't double-emit this market

                # ── 1. NEAR-CERTAIN: extreme price + short DTE ────────────
                if dte <= 30 and (ya <= 0.05 or ya >= 0.95):
                    if ya <= 0.05:
                        # YES is cheap — lotto upside if it resolves YES
                        contracts, cost = self._size(balance_usd, ya, 0.01, 50.0)
                        maker = self._maker_price("yes", yb, ya, nb, na)
                        opps.append(KalshiOpportunity(
                            ticker=ticker, title=disp_title, event_title=event_title,
                            category=category, side="yes",
                            market_price=ya, yes_ask=ya, yes_bid=yb,
                            no_ask=na, no_bid=nb,
                            dte=dte, volume=vol, spread=spread, price_move=price_move,
                            opportunity_type="near_certain",
                            bet_contracts=contracts, bet_cost_usd=cost,
                            maker_price=maker,
                            annualized_yield_pct=self._annualized_yield(ya, dte),
                            rationale=(
                                f"YES @ {ya*100:.0f}¢ ({dte:.0f}d) | "
                                f"Near-certain NO — buy YES for lotto upside | "
                                f"vol={vol/1000:.0f}k"
                            ),
                        ))
                    else:
                        # YES is near-certain — buy for near-guaranteed return
                        contracts, cost = self._size(balance_usd, ya, 0.02, 200.0)
                        ay = self._annualized_yield(ya, dte)
                        maker = self._maker_price("yes", yb, ya, nb, na)
                        opps.append(KalshiOpportunity(
                            ticker=ticker, title=disp_title, event_title=event_title,
                            category=category, side="yes",
                            market_price=ya, yes_ask=ya, yes_bid=yb,
                            no_ask=na, no_bid=nb,
                            dte=dte, volume=vol, spread=spread, price_move=price_move,
                            opportunity_type="near_certain",
                            bet_contracts=contracts, bet_cost_usd=cost,
                            maker_price=maker,
                            annualized_yield_pct=ay,
                            rationale=(
                                f"YES @ {ya*100:.0f}¢ ({dte:.0f}d) | "
                                f"Near-certain YES — {round((1-ya)*100,1)}¢ upside | "
                                f"ann yld {ay:.0f}% | vol={vol/1000:.0f}k"
                            ),
                        ))

                # ── 5. YIELD FARM: 88-94.9¢ band, short DTE ───────────────
                elif dte <= 3.0 and 0.88 <= ya < 0.95:
                    # YES side yield farm
                    ay = self._annualized_yield(ya, dte)
                    if ay >= 100:   # only surface if annualized ≥100%
                        contracts, cost = self._size(balance_usd, ya, 0.02, 200.0)
                        maker = self._maker_price("yes", yb, ya, nb, na)
                        opps.append(KalshiOpportunity(
                            ticker=ticker, title=disp_title, event_title=event_title,
                            category=category, side="yes",
                            market_price=ya, yes_ask=ya, yes_bid=yb,
                            no_ask=na, no_bid=nb,
                            dte=dte, volume=vol, spread=spread, price_move=price_move,
                            opportunity_type="yield_farm",
                            bet_contracts=contracts, bet_cost_usd=cost,
                            maker_price=maker,
                            annualized_yield_pct=ay,
                            rationale=(
                                f"🌾 YIELD FARM | YES @ {ya*100:.0f}¢ ({dte*24:.0f}h) | "
                                f"annualized {ay:.0f}% | "
                                f"payoff {(1-ya)*100:.1f}¢ on {ya*100:.0f}¢ risk | "
                                f"vol={vol/1000:.0f}k"
                            ),
                        ))
                # Mirror: NO side yield farm (YES 5-12¢ → NO 88-94.9¢)
                elif dte <= 3.0 and na > 0 and 0.88 <= na < 0.95:
                    ay = self._annualized_yield(na, dte)
                    if ay >= 100:
                        contracts, cost = self._size(balance_usd, na, 0.02, 200.0)
                        maker = self._maker_price("no", yb, ya, nb, na)
                        opps.append(KalshiOpportunity(
                            ticker=ticker, title=disp_title, event_title=event_title,
                            category=category, side="no",
                            market_price=na, yes_ask=ya, yes_bid=yb,
                            no_ask=na, no_bid=nb,
                            dte=dte, volume=vol, spread=spread, price_move=price_move,
                            opportunity_type="yield_farm",
                            bet_contracts=contracts, bet_cost_usd=cost,
                            maker_price=maker,
                            annualized_yield_pct=ay,
                            rationale=(
                                f"🌾 YIELD FARM | NO @ {na*100:.0f}¢ ({dte*24:.0f}h) | "
                                f"annualized {ay:.0f}% | "
                                f"payoff {(1-na)*100:.1f}¢ on {na*100:.0f}¢ risk | "
                                f"vol={vol/1000:.0f}k"
                            ),
                        ))

                # ── 2. HIGH-VOL EXTREME: any DTE ──────────────────────────
                elif vol > 100_000 and (ya <= 0.08 or ya >= 0.92):
                    side = "yes" if ya >= 0.92 else "no"
                    price = ya if side == "yes" else na
                    if price > 0:
                        contracts, cost = self._size(balance_usd, price, 0.02, 200.0)
                        maker = self._maker_price(side, yb, ya, nb, na)
                        opps.append(KalshiOpportunity(
                            ticker=ticker, title=disp_title, event_title=event_title,
                            category=category, side=side,
                            market_price=price, yes_ask=ya, yes_bid=yb,
                            no_ask=na, no_bid=nb,
                            dte=dte, volume=vol, spread=spread, price_move=price_move,
                            opportunity_type="high_vol_extreme",
                            bet_contracts=contracts, bet_cost_usd=cost,
                            maker_price=maker,
                            annualized_yield_pct=self._annualized_yield(price, dte),
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
                        maker = self._maker_price(side, yb, ya, nb, na)
                        direction = "UP" if price_move > 0 else "DOWN"
                        opps.append(KalshiOpportunity(
                            ticker=ticker, title=disp_title, event_title=event_title,
                            category=category, side=side,
                            market_price=price, yes_ask=ya, yes_bid=yb,
                            no_ask=na, no_bid=nb,
                            dte=dte, volume=vol, spread=spread, price_move=price_move,
                            opportunity_type="mover",
                            bet_contracts=contracts, bet_cost_usd=cost,
                            maker_price=maker,
                            annualized_yield_pct=self._annualized_yield(price, dte),
                            rationale=(
                                f"Price moved {direction} {abs(price_move)*100:.0f}¢ | "
                                f"Current: {price*100:.0f}¢ | "
                                f"vol={vol/1000:.0f}k | {dte:.0f}d"
                            ),
                        ))

                # ── 4. ACTIVE DEBATE: near 50¢ with huge volume ────────────
                elif vol > 500_000 and 0.30 <= ya <= 0.70:
                    contracts, cost = self._size(balance_usd, ya, 0.02, 100.0)
                    maker = self._maker_price("yes", yb, ya, nb, na)
                    opps.append(KalshiOpportunity(
                        ticker=ticker, title=disp_title, event_title=event_title,
                        category=category, side="watch",
                        market_price=ya, yes_ask=ya, yes_bid=yb,
                        no_ask=na, no_bid=nb,
                        dte=dte, volume=vol, spread=spread, price_move=price_move,
                        opportunity_type="active",
                        bet_contracts=contracts, bet_cost_usd=cost,
                        maker_price=maker,
                        rationale=(
                            f"Active 50/50 debate | YES={ya*100:.0f}¢ NO={na*100:.0f}¢ | "
                            f"vol={vol/1000:.0f}k | {dte:.0f}d — take a side if you have a view"
                        ),
                    ))

            except Exception as e:
                logger.warning(f"Scanner error on {m.get('ticker','?')}: {e}")

        opps.sort(key=lambda x: x.score(), reverse=True)
        return opps

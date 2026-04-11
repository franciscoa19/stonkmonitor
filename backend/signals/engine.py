"""
Signal Engine — scores and filters incoming feed events.
Every event gets a score 1-10. Only high-score events trigger notifications.

Scoring factors:
  Options Flow:   premium size, sweep vs block, OTM aggressive bets, repeat hits
  Dark Pool:      print size relative to ADV, price vs market, clustering
  Insider:        buy vs sell, officer/director level, cluster buying, size
  IV:             rank extremes (very high = sell premium, very low = buy premium)
"""
import logging
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class SignalType(str, Enum):
    OPTIONS_FLOW   = "options_flow"
    DARK_POOL      = "dark_pool"
    INSIDER_BUY    = "insider_buy"
    INSIDER_SELL   = "insider_sell"
    CONGRESS_TRADE = "congress_trade"
    IV_HIGH        = "iv_high"
    IV_LOW         = "iv_low"
    SWEEP          = "sweep"
    GOLDEN_SWEEP   = "golden_sweep"
    EARNINGS_SETUP = "earnings_setup"


class SignalSide(str, Enum):
    BULLISH  = "bullish"
    BEARISH  = "bearish"
    NEUTRAL  = "neutral"


@dataclass
class Signal:
    type: SignalType
    ticker: str
    score: float           # 1-10
    side: SignalSide
    title: str
    description: str
    raw: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    premium: float = 0.0
    expiry: Optional[str] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "ticker": self.ticker,
            "score": round(self.score, 2),
            "side": self.side.value,
            "title": self.title,
            "description": self.description,
            "premium": self.premium,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "timestamp": self.timestamp.isoformat(),
        }


class SignalEngine:
    """
    Processes raw feed events into scored, actionable signals.
    """
    def __init__(self, settings):
        self.settings = settings

    # ------------------------------------------------------------------ #
    #  Options Flow Scoring                                                #
    # ------------------------------------------------------------------ #
    def score_options_flow(self, event: dict) -> Optional[Signal]:
        """
        Score an options flow alert from UW /api/option-trades/flow-alerts.
        Real fields: total_premium, type (call/put), alert_rule, has_sweep,
                     strike, expiry, volume, open_interest, iv_start,
                     total_ask_side_prem, total_bid_side_prem, ticker
        """
        try:
            ticker   = (event.get("ticker") or "").upper()
            premium  = float(event.get("total_premium", 0) or 0)
            opt_type = (event.get("type") or "").lower()          # "call" | "put"
            rule     = (event.get("alert_rule") or "").lower()    # "GoldenSweep", "Sweep", etc.
            has_sweep = bool(event.get("has_sweep", False))
            strike   = event.get("strike")
            expiry   = event.get("expiry")
            volume   = int(event.get("volume", 0) or 0)
            oi       = int(event.get("open_interest", 0) or 0)
            iv       = float(event.get("iv_start", 0) or 0)
            ask_prem = float(event.get("total_ask_side_prem", 0) or 0)  # aggressive buys
            bid_prem = float(event.get("total_bid_side_prem", 0) or 0)  # aggressive sells

            if not ticker or premium < self.settings.min_premium_alert:
                return None

            # --- Score calculation ---
            score = 3.0

            # Premium size bonus
            if premium >= 1_000_000:
                score += 3.0
            elif premium >= 500_000:
                score += 2.0
            elif premium >= 200_000:
                score += 1.5
            elif premium >= 100_000:
                score += 1.0

            # Alert rule / sweep type
            is_golden = "golden" in rule
            is_sweep  = has_sweep or "sweep" in rule
            if is_golden:
                score += 2.5
                sig_type = SignalType.GOLDEN_SWEEP
            elif is_sweep:
                score += 1.5
                sig_type = SignalType.SWEEP
            else:
                sig_type = SignalType.OPTIONS_FLOW

            # Vol/OI ratio — unusually large vs existing open interest
            vol_oi = float(event.get("volume_oi_ratio", 0) or 0)
            if vol_oi > 2.0:
                score += 0.5

            # IV conviction — high IV means they're paying a premium
            if iv > 0.5:
                score += 0.5

            score = min(score, 10.0)

            # Direction: ask-side premium = aggressive buys = bullish
            # For calls: ask_prem bullish / bid_prem bearish
            # For puts:  ask_prem bearish / bid_prem bullish
            if opt_type == "call":
                side = SignalSide.BULLISH if ask_prem >= bid_prem else SignalSide.BEARISH
            elif opt_type == "put":
                side = SignalSide.BEARISH if ask_prem >= bid_prem else SignalSide.BULLISH
            else:
                side = SignalSide.NEUTRAL

            emoji      = "🟢" if side == SignalSide.BULLISH else "🔴" if side == SignalSide.BEARISH else "⚪"
            type_label = "GOLDEN SWEEP" if is_golden else "SWEEP" if is_sweep else "FLOW"
            exp_label  = f"Exp {expiry}" if expiry else ""

            title = f"{emoji} {ticker} — {type_label} {opt_type.upper()}"
            desc  = (
                f"${premium:,.0f} premium | "
                f"${strike} strike | "
                f"{exp_label} | "
                f"Vol/OI {vol_oi:.1f} | Score: {score:.1f}/10"
            )

            return Signal(
                type=sig_type,
                ticker=ticker,
                score=score,
                side=side,
                title=title,
                description=desc,
                raw=event,
                premium=premium,
                expiry=expiry,
                strike=float(strike) if strike else None,
                option_type=opt_type,
            )

        except Exception as e:
            logger.warning(f"Error scoring options flow: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  Dark Pool Scoring                                                   #
    # ------------------------------------------------------------------ #
    def score_darkpool(self, event: dict) -> Optional[Signal]:
        """Score a dark pool print."""
        try:
            ticker = event.get("ticker", "").upper()
            size   = float(event.get("size", 0) or 0)
            price  = float(event.get("price", 0) or 0)
            total  = size * price

            if total < self.settings.min_darkpool_size:
                return None

            score = 3.0
            if total >= 5_000_000:
                score += 3.0
            elif total >= 2_000_000:
                score += 2.0
            elif total >= 1_000_000:
                score += 1.5
            elif total >= 500_000:
                score += 0.5

            score = min(score, 10.0)

            title = f"🌑 {ticker} — Dark Pool Print"
            desc = (
                f"{size:,.0f} shares @ ${price:.2f} | "
                f"Total: ${total:,.0f} | "
                f"Score: {score:.1f}/10"
            )

            return Signal(
                type=SignalType.DARK_POOL,
                ticker=ticker,
                score=score,
                side=SignalSide.NEUTRAL,
                title=title,
                description=desc,
                raw=event,
                premium=total,
            )

        except Exception as e:
            logger.warning(f"Error scoring dark pool: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  Insider Trade Scoring                                               #
    # ------------------------------------------------------------------ #
    def score_insider(self, event: dict) -> Optional[Signal]:
        """
        Score an insider trade from UW /api/insider/transactions.
        Real fields: ticker, amount (shares signed), price (per share),
                     owner_name, officer_title, is_officer, is_director,
                     is_ten_percent_owner, transaction_code, is_10b5_1
        transaction_code:
          P = open-market purchase  ← only real buy signal
          S = open-market sale      ← weak bearish
          D = disposition           ← weak bearish
          A = award (skip)          ← compensation, not a trade
          M = exercise (skip)       ← scheduled, not a trade
          F = tax withholding (skip) ← not a trade
        """
        try:
            ticker = (event.get("ticker") or "").upper()
            shares = float(event.get("amount", 0) or 0)           # number of shares (signed)
            price  = float(event.get("price", 0) or 0)            # price per share
            name   = event.get("owner_name") or "Unknown"
            title  = event.get("officer_title") or ""
            code   = (event.get("transaction_code") or "").upper()
            is_10b = bool(event.get("is_10b5_1", False))

            # Only care about actual open-market buys and sells
            # Skip awards (A), exercises (M), tax withholding (F), transfers (J/G)
            SKIP_CODES = {"A", "M", "F", "J", "G", "C", "X"}
            if code in SKIP_CODES:
                return None

            is_buy  = code == "P"
            is_sell = code in ("S", "D")

            if not (is_buy or is_sell):
                return None

            # Dollar value = shares * price per share
            # Fall back to raw amount if price is 0 (some filings omit price)
            value = abs(shares) * price if price > 0 else abs(shares)
            if value < 10_000:
                return None

            score = 3.0

            # Buys >> sells as signal; pre-planned 10b5-1 sales get lowest weight
            if is_buy:
                score += 2.0
            elif is_10b:
                score += 0.0   # pre-planned — barely interesting
            else:
                score += 0.5

            # Role bonus — C-suite buying own stock is a big signal
            is_officer  = bool(event.get("is_officer", False))
            is_director = bool(event.get("is_director", False))
            is_whale    = bool(event.get("is_ten_percent_owner", False))
            title_lower = title.lower()

            if any(t in title_lower for t in ["chief executive", "ceo", "president", "chairman"]):
                score += 2.0
            elif any(t in title_lower for t in ["chief financial", "cfo", "chief operating", "coo"]):
                score += 1.5
            elif is_officer or is_director:
                score += 1.0
            elif is_whale:
                score += 0.5

            # Size bonus
            if value >= 1_000_000:
                score += 2.0
            elif value >= 500_000:
                score += 1.5
            elif value >= 100_000:
                score += 0.5

            score = min(score, 10.0)

            side   = SignalSide.BULLISH if is_buy else SignalSide.BEARISH
            action = "BUY" if is_buy else "SELL"
            emoji  = "🟢" if is_buy else "🔴"
            role_label = title or ("Director" if is_director else "Insider")
            pre_planned = " (10b5-1)" if is_10b else ""

            signal_title = f"{emoji} {ticker} — Insider {action}"
            desc = (
                f"{name} | {role_label}{pre_planned} | "
                f"${value:,.0f} | Score: {score:.1f}/10"
            )

            return Signal(
                type=SignalType.INSIDER_BUY if is_buy else SignalType.INSIDER_SELL,
                ticker=ticker,
                score=score,
                side=side,
                title=signal_title,
                description=desc,
                raw=event,
                premium=value,
            )

        except Exception as e:
            logger.warning(f"Error scoring insider trade: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  Congress Trade Scoring                                              #
    # ------------------------------------------------------------------ #
    def score_congress(self, event: dict) -> Optional[Signal]:
        """
        Score a congressional trade from UW /api/congress/recent-trades.
        Real fields: name, ticker, txn_type ("Buy"/"Sell"), amounts,
                     member_type ("house"/"senate"), reporter, transaction_date
        Note: no party field in the API response.
        """
        try:
            ticker      = (event.get("ticker") or "").upper()
            member      = event.get("name") or event.get("reporter") or "Unknown"
            txn_type    = (event.get("txn_type") or "").lower()   # "Buy" | "Sell" | "Exchange"
            amounts     = event.get("amounts") or ""               # "$15,001 - $50,000"
            chamber     = (event.get("member_type") or "").title() # "House" | "Senate"
            txn_date    = event.get("transaction_date") or ""

            # Skip if no ticker (e.g. T-bills, mutual funds)
            if not ticker:
                return None

            is_buy  = "buy" in txn_type or "purchase" in txn_type
            is_sell = "sell" in txn_type or "sale" in txn_type

            # Always flag — congress trades are historically alpha
            score = 7.0

            # Senate trades get slight bonus (more likely inside info)
            if chamber.lower() == "senate":
                score += 0.5

            score = min(score, 10.0)

            side  = SignalSide.BULLISH if is_buy else SignalSide.BEARISH if is_sell else SignalSide.NEUTRAL
            emoji = "🟢" if is_buy else "🔴" if is_sell else "⚪"
            verb  = "Bought" if is_buy else "Sold" if is_sell else txn_type.title()

            signal_title = f"{emoji} {ticker} — {chamber} {verb}"
            desc = (
                f"{member} | "
                f"{verb} {amounts} | "
                f"{txn_date} | Score: {score:.1f}/10"
            )

            return Signal(
                type=SignalType.CONGRESS_TRADE,
                ticker=ticker,
                score=score,
                side=side,
                title=signal_title,
                description=desc,
                raw=event,
            )

        except Exception as e:
            logger.warning(f"Error scoring congress trade: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  IV Rank Scoring                                                     #
    # ------------------------------------------------------------------ #
    def score_iv_rank(self, ticker: str, iv_rank: float, iv_percentile: float) -> Optional[Signal]:
        """Alert on extreme IV rank conditions."""
        if iv_rank >= self.settings.iv_rank_threshold:
            score = 5.0 + (iv_rank - self.settings.iv_rank_threshold) / 10
            score = min(score, 9.0)
            return Signal(
                type=SignalType.IV_HIGH,
                ticker=ticker.upper(),
                score=score,
                side=SignalSide.NEUTRAL,
                title=f"📈 {ticker.upper()} — IV Rank EXTREME HIGH",
                description=(
                    f"IV Rank: {iv_rank:.0f} | IV %ile: {iv_percentile:.0f} | "
                    f"Consider selling premium | Score: {score:.1f}/10"
                ),
                raw={"iv_rank": iv_rank, "iv_percentile": iv_percentile},
            )

        if iv_rank <= self.settings.iv_rank_low_threshold:
            score = 5.0 + (self.settings.iv_rank_low_threshold - iv_rank) / 5
            score = min(score, 8.0)
            return Signal(
                type=SignalType.IV_LOW,
                ticker=ticker.upper(),
                score=score,
                side=SignalSide.NEUTRAL,
                title=f"📉 {ticker.upper()} — IV Rank EXTREME LOW",
                description=(
                    f"IV Rank: {iv_rank:.0f} | IV %ile: {iv_percentile:.0f} | "
                    f"Options cheap — consider buying premium | Score: {score:.1f}/10"
                ),
                raw={"iv_rank": iv_rank, "iv_percentile": iv_percentile},
            )

        return None

    def score_earnings_setup(self, setup) -> Optional[Signal]:
        """
        Convert an EarningsSetup from earnings_scanner into a Signal.
        Only surfaces SELL_PREMIUM and CONSIDER recommendations.
        """
        if setup is None or setup.recommendation == "AVOID":
            return None

        rec   = setup.recommendation  # "SELL_PREMIUM" | "CONSIDER"
        score = setup.score           # 8.0 or 6.0

        # Boost score further when IV/RV ratio is extreme
        if setup.iv30_rv30 >= 1.5:
            score = min(score + 1.0, 9.5)
        elif setup.iv30_rv30 >= 1.35:
            score = min(score + 0.5, 9.0)

        checks = (
            f"Vol {'✅' if setup.vol_ok else '❌'} "
            f"| IV/RV {'✅' if setup.iv_expensive else '❌'} {setup.iv30_rv30:.2f}x "
            f"| TS {'✅' if setup.ts_inverted else '❌'}"
        )
        em = f" | Expected move: {setup.expected_move}" if setup.expected_move else ""

        return Signal(
            type=SignalType.EARNINGS_SETUP,
            ticker=setup.ticker,
            score=round(score, 1),
            side=SignalSide.NEUTRAL,
            title=(
                f"{'🎯' if rec == 'SELL_PREMIUM' else '⚠️'} "
                f"{setup.ticker} — {'SELL PREMIUM' if rec == 'SELL_PREMIUM' else 'CONSIDER PREMIUM SALE'}"
            ),
            description=(
                f"IV30={setup.iv30*100:.0f}% vs RV30={setup.rv30*100:.0f}% "
                f"({setup.iv30_rv30:.2f}x){em} | {checks} | "
                f"Price ${setup.price:.2f} | Score {score:.1f}/10"
            ),
            raw=setup.to_dict(),
        )

    # ------------------------------------------------------------------ #
    #  Master dispatch                                                     #
    # ------------------------------------------------------------------ #
    def process_event(self, event_type: str, event: dict) -> Optional[Signal]:
        """Route raw events to the right scorer."""
        handlers = {
            "options-flow":    self.score_options_flow,
            "options_flow":    self.score_options_flow,
            "darkpool":        self.score_darkpool,
            "dark_pool":       self.score_darkpool,
            "insider-trades":  self.score_insider,
            "insider_trades":  self.score_insider,
            "congress":        self.score_congress,
            "congress-trades": self.score_congress,
        }
        handler = handlers.get(event_type)
        if handler:
            return handler(event)
        return None

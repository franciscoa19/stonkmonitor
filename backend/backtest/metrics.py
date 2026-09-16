"""
Short-premium metric set — pure functions over a sequence of per-event P&Ls.

Design rule (from VALIDATION_SPEC §4): **a run that emits only total P&L is a
bug.** The failure mode for selling premium is a long, tidy equity curve broken
by one catastrophic loss; total return and win rate both hide exactly that. An
iron condor wins ~65-70% of the time *by construction*, so win rate is close to
meaningless on its own — the question is always what the losers cost.

`tail_ratio` is the metric that catches it: the mean of the worst 5% of events
against the mean of the rest. A strategy can post a 70% win rate and still be
negative expectancy because five events erase fifty.

These are deliberately dumb and dependency-free: metrics in, no opinions out.
Nothing here ranks strategies or picks parameters — that separation is the point
(VALIDATION_SPEC "explicitly out of scope"). Used today on the live forward-test
data (iv_variant_evals / iv_condors); reusable unchanged by a historical harness
if we ever license options history.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

# VALIDATION_SPEC §1: the scanner fires ~4×/yr/ticker and the gates reject most
# of those. Under this many resolved events the result is a story, not evidence —
# report the shortfall instead of a curve drawn through a handful of trades.
MIN_EVENTS_FOR_CONFIDENCE = 100

_TAIL_PCT = 0.05


def _safe_div(a: float, b: float) -> Optional[float]:
    return (a / b) if b else None


def max_drawdown(pnls: Sequence[float]) -> float:
    """Largest peak-to-trough decline of the cumulative event-sequence curve ($).
    Returned as a positive magnitude; 0.0 when the curve never retraces."""
    peak = 0.0
    equity = 0.0
    worst = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return round(worst, 2)


def tail_ratio(pnls: Sequence[float]) -> Optional[float]:
    """Mean of the worst 5% of events ÷ mean of the remainder.

    Negative by construction when the tail loses and the body wins. Read it as
    "one tail event costs N typical events": -1 means a bad event erases one
    good one, -10 means it erases ten. None when there is no remainder to
    compare against (n < 2) or the body averages exactly zero.
    """
    n = len(pnls)
    if n < 2:
        return None
    k = max(1, math.ceil(n * _TAIL_PCT))
    if k >= n:
        return None
    ordered = sorted(pnls)
    tail, body = ordered[:k], ordered[k:]
    body_mean = sum(body) / len(body)
    if body_mean == 0:
        return None
    return round((sum(tail) / k) / body_mean, 2)


def _stdev(xs: Sequence[float], mean: float) -> float:
    if len(xs) < 2:
        return 0.0
    return math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))


def compute_metrics(pnls: Sequence[float],
                    exceeded_implied: Optional[Sequence[bool]] = None) -> dict:
    """Full metric set for a sequence of per-event P&L figures ($ per event).

    `exceeded_implied` (optional, parallel to pnls) flags events where the
    realized move was larger than the implied move priced at entry — the
    premium-seller's structural question, independent of which structure was used.

    Sharpe/Sortino here are **per-event**, not annualized: annualizing would need
    an assumption about event frequency that the caller, not this module, owns.
    """
    pnls = [float(p) for p in pnls]
    n = len(pnls)
    if n == 0:
        return {"n_events": 0, "n_wins": 0, "win_rate": None, "total_pnl": 0.0,
                "expectancy": None, "profit_factor": None, "avg_win": None,
                "avg_loss": None, "largest_single_loss": None, "max_drawdown": 0.0,
                "sharpe": None, "sortino": None, "tail_ratio": None,
                "pct_events_exceeding_implied": None,
                "sufficient_sample": False, "events_needed": MIN_EVENTS_FOR_CONFIDENCE}

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    mean = sum(pnls) / n
    sd = _stdev(pnls, mean)
    downside = [p for p in pnls if p < mean]
    dsd = _stdev(downside, mean) if len(downside) >= 2 else 0.0

    pct_exceed = None
    if exceeded_implied is not None and len(exceeded_implied) == n and n:
        pct_exceed = round(sum(1 for x in exceeded_implied if x) / n * 100, 1)

    return {
        "n_events":            n,
        "n_wins":              len(wins),
        "win_rate":            round(len(wins) / n * 100, 1),
        "total_pnl":           round(sum(pnls), 2),
        "expectancy":          round(mean, 2),          # avg $ per event — the number that matters
        "profit_factor":       (round(gross_win / gross_loss, 2) if gross_loss else None),
        "avg_win":             (round(gross_win / len(wins), 2) if wins else None),
        "avg_loss":            (round(sum(losses) / len(losses), 2) if losses else None),
        "largest_single_loss": (round(min(losses), 2) if losses else None),
        "max_drawdown":        max_drawdown(pnls),
        "sharpe":              (round(mean / sd, 2) if sd else None),
        "sortino":             (round(mean / dsd, 2) if dsd else None),
        "tail_ratio":          tail_ratio(pnls),
        "pct_events_exceeding_implied": pct_exceed,
        # Honesty rail: below this, decline to draw conclusions.
        "sufficient_sample":   n >= MIN_EVENTS_FOR_CONFIDENCE,
        "events_needed":       max(0, MIN_EVENTS_FOR_CONFIDENCE - n),
    }

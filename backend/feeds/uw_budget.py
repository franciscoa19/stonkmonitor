"""
Unusual Whales API budget + market-hours awareness.

Two jobs:

1. Rate-limit tracking — UW returns `x-uw-daily-req-count` and
   `x-uw-token-req-limit` on every response. We parse them, cache the
   latest snapshot, and expose a global `should_throttle()` / `should_pause()`
   used by the streaming loop to slow down or stop when the daily budget
   is almost exhausted.

2. Market-session awareness — options flow, dark pool, insider filings,
   congress disclosures all have wildly different refresh rates.
     - Options flow + dark pool: only meaningful during US market hours.
     - Insider (Form 4): filed business days, usually batched after close.
     - Congress (PTR): filed weekdays, usually lagged by days/weeks.
   We compute the current "session" (rth / extended / overnight / weekend)
   and look up the right poll interval per channel.

Call-budget target: stay comfortably under 15,000/day, with weekends
burning near zero. Prior setup burned ~11,250/day (75%) polling all four
channels every 15s around the clock.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")

# Session → (channel → poll interval seconds). INF (-1) disables the channel
# entirely for that session. Tuned so weekday daily spend ≈ 8k calls and
# weekend spend ≈ <200 calls.
#
# Sessions:
#   rth       Mon–Fri 09:30–16:00 ET       (regular trading hours)
#   extended  Mon–Fri 04:00–09:30 + 16:00–20:00 ET
#   overnight Mon–Fri 20:00–04:00 ET       (closed, low value)
#   weekend   Sat + Sun all day            (nothing updates)
SCHEDULE: dict[str, dict[str, int]] = {
    "rth": {
        "options-flow":   15,
        "darkpool":       15,
        "insider-trades": 60,
        "congress-trades":60,
    },
    "extended": {
        "options-flow":   60,
        "darkpool":       60,
        "insider-trades": 300,
        "congress-trades":300,
    },
    "overnight": {
        "options-flow":   -1,   # options market closed — nothing new
        "darkpool":       -1,   # ditto
        "insider-trades": 900,  # 15 min — late Form 4 filings do happen
        "congress-trades":1800, # 30 min
    },
    "weekend": {
        "options-flow":   -1,
        "darkpool":       -1,
        "insider-trades": 3600, # hourly — catches late Friday filings
        "congress-trades":3600,
    },
}


def current_session(now: Optional[datetime] = None) -> str:
    """Return the current market session tag based on US/Eastern wall clock."""
    if now is None:
        now = datetime.now(_NY)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_NY)
    else:
        now = now.astimezone(_NY)

    # Monday=0 … Sunday=6
    wd = now.weekday()
    if wd >= 5:
        return "weekend"

    hm = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= hm < 16 * 60:
        return "rth"
    if 4 * 60 <= hm < 20 * 60:
        return "extended"
    return "overnight"


# ── Sub-phases within RTH for noise-aware signal filtering ──────────────────
# Returned as a string tag + a score_bump (extra points a signal must clear
# on top of the base threshold before it triggers a notification/auto-trade).
#
#  open_first_5   09:30–09:35  +2.0  — pure chaos, barely usable
#  open           09:35–10:00  +1.5  — still noisy, require strong confirmation
#  close          15:45–16:00  +0.5  — MOC/imbalance noise
#  normal         all other RTH  0.0 — baseline
#  extended         outside RTH  0.0 — score already adjusted by session
SUBPHASE_BUMPS: dict[str, float] = {
    "open_first_5": 2.0,
    "open":         1.5,
    "close":        0.5,
    "normal":       0.0,
    "extended":     0.0,
    "overnight":    0.0,
    "weekend":      0.0,
}


def is_auto_trade_window(now: Optional[datetime] = None) -> bool:
    """
    Return True if it's safe to queue auto-trades right now.

    Window: weekdays 04:00 ET — 18:00 ET. Outside this, bid/ask spreads on
    options are wide and stale (post-close marks rather than real liquidity)
    and overnight fills are unpredictable. CHTR 2026-04-28 was queued at
    20:20 ET on a $0.12 ask that was almost certainly a stale book mark.
    """
    if now is None:
        now = datetime.now(_NY)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_NY)
    else:
        now = now.astimezone(_NY)
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return 4 * 60 <= hm < 18 * 60


def market_subphase(now: Optional[datetime] = None) -> str:
    """Return a fine-grained market sub-phase tag for noise filtering."""
    if now is None:
        now = datetime.now(_NY)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_NY)
    else:
        now = now.astimezone(_NY)

    wd = now.weekday()
    if wd >= 5:
        return "weekend"

    hm = now.hour * 60 + now.minute

    # Regular trading hours sub-phases
    if 9 * 60 + 30 <= hm < 16 * 60:
        if hm < 9 * 60 + 35:
            return "open_first_5"   # 09:30–09:35
        if hm < 10 * 60:
            return "open"           # 09:35–10:00
        if hm >= 15 * 60 + 45:
            return "close"          # 15:45–16:00
        return "normal"

    if 4 * 60 <= hm < 20 * 60:
        return "extended"
    return "overnight"


def score_bump_for_subphase(subphase: str, bumps: Optional[dict] = None) -> float:
    """Extra score points required above the base threshold for this sub-phase.

    Pass a `bumps` dict to override defaults, e.g.:
        {"open_first_5": 2.0, "open": 1.5, "close": 0.5}
    Falls back to SUBPHASE_BUMPS for any missing key.
    """
    if bumps:
        return bumps.get(subphase, SUBPHASE_BUMPS.get(subphase, 0.0))
    return SUBPHASE_BUMPS.get(subphase, 0.0)


@dataclass
class UWBudget:
    """Singleton-ish tracker of UW daily call budget."""
    daily_count:    int = 0
    daily_limit:    int = 15000
    last_update_ts: float = 0.0
    last_path:      str   = ""
    throttle_pct:   float = 0.80   # start slowing above 80%
    pause_pct:      float = 0.95   # hard-pause above 95%

    def update_from_headers(self, path: str, headers: dict) -> None:
        """Parse UW rate-limit headers off any response."""
        cnt = headers.get("x-uw-daily-req-count") or headers.get("X-UW-Daily-Req-Count")
        lim = headers.get("x-uw-token-req-limit") or headers.get("X-UW-Token-Req-Limit")
        try:
            if cnt is not None:
                self.daily_count = int(cnt)
            if lim is not None:
                self.daily_limit = int(lim)
        except (TypeError, ValueError):
            return
        self.last_update_ts = time.time()
        self.last_path = path

    @property
    def usage_pct(self) -> float:
        if self.daily_limit <= 0:
            return 0.0
        return self.daily_count / self.daily_limit

    def should_throttle(self) -> bool:
        return self.usage_pct >= self.throttle_pct

    def should_pause(self) -> bool:
        return self.usage_pct >= self.pause_pct

    def status(self) -> dict:
        return {
            "daily_count":   self.daily_count,
            "daily_limit":   self.daily_limit,
            "usage_pct":     round(self.usage_pct, 4),
            "session":       current_session(),
            "throttle":      self.should_throttle(),
            "pause":         self.should_pause(),
            "last_update":   self.last_update_ts,
        }


# Module-level singleton imported by the UW client and by main.py
budget = UWBudget()


def interval_for(channel: str, session: Optional[str] = None) -> int:
    """
    Return the poll interval (seconds) for a given channel in the current
    (or supplied) session. -1 means "do not poll this cycle".
    """
    sess = session or current_session()
    table = SCHEDULE.get(sess, SCHEDULE["rth"])
    return table.get(channel, 60)

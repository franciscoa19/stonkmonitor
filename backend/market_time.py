"""Canonical market-time helpers.

Earnings dates, option expiries, and regular-session cutoffs are defined in US
Eastern time. Never derive those dates from the host's local timezone: a UTC
deployment otherwise crosses its calendar day while New York is still trading.
"""
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def et_now(now: Optional[datetime] = None) -> datetime:
    """Return ``now`` normalized to Eastern time (naive inputs are ET)."""
    if now is None:
        return datetime.now(ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def et_today(now: Optional[datetime] = None) -> date:
    """Calendar date in Eastern time, suitable for market-event comparisons."""
    return et_now(now).date()

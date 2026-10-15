"""Session provenance for hypothetical IV/RV observations; no execution."""
from datetime import date, datetime, time, timedelta

from feeds.uw_budget import market_close_minute
from market_time import ET


def measurement_session(ev: dict) -> str | None:
    """The declared horizon's session, strictly after a known earnings date."""
    def trading_day(day):
        return market_close_minute(datetime.combine(day, time.min, tzinfo=ET)) is not None

    try:
        target = date.fromisoformat(ev["resolve_after"])
        while not trading_day(target):
            target -= timedelta(days=1)
        earnings = ev.get("earnings_date")
        floor = date.fromisoformat(earnings or ev["signal_date"])
        earliest = floor + timedelta(days=1) if earnings else floor
        if target < earliest:
            target = earliest
            while not trading_day(target):
                target += timedelta(days=1)
        return target.isoformat()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def legacy_window_end(ev: dict) -> str | None:
    """Conservative bound when an older result has no measured-session evidence.

    Older resolvers used latest prices, while later ones used historical bars.
    With no provenance, cover the declared horizon, derived session, and actual
    ET resolution date rather than claiming a tagged day could not affect it.
    This is read-only; it does not certify a price or backfill invented dates.
    """
    candidates = [measurement_session(ev)]
    for key in ("resolve_after", "signal_date"):
        try:
            candidates.append(date.fromisoformat(ev[key]).isoformat())
        except (KeyError, TypeError, ValueError):
            pass
    try:
        from datetime import timezone
        at = datetime.fromisoformat(ev["resolved_at"].replace("Z", "+00:00"))
        candidates.append(at.replace(tzinfo=at.tzinfo or timezone.utc).astimezone(ET).date().isoformat())
    except (KeyError, AttributeError, TypeError, ValueError):
        pass
    return max((day for day in candidates if day), default=None)

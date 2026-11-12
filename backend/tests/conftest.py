"""Shared test setup."""
import pytest


@pytest.fixture(autouse=True)
def awake_for_hours(monkeypatch):
    """Tests run as if the Mac had been awake for hours, so entry tests are not
    held by the sleep guard. test_awake_clock.py replaces this with real clocks."""
    import awake_clock
    clock = awake_clock.AwakeClock()
    monkeypatch.setattr(clock, "awake_minutes", lambda now=None: 10_000.0)
    monkeypatch.setattr(clock, "entry_token", lambda required_minutes: 0)
    monkeypatch.setattr(awake_clock, "clock", clock)
    try:
        import main
    except Exception:          # a test without the app's settings never reaches the gate
        return
    monkeypatch.setattr(main, "awake_clock", clock)


@pytest.fixture(autouse=True)
def calendar_cache_in_tmp(monkeypatch, tmp_path):
    """The saved earnings calendar is never read from or written to the real
    backend folder by a test."""
    from feeds import earnings_calendar
    monkeypatch.setattr(earnings_calendar, "_CACHE_FILE", tmp_path / "earnings_calendar_cache.json")
    monkeypatch.setitem(earnings_calendar._cache, "loaded", True)
    monkeypatch.setitem(earnings_calendar._cache, "answered_days", frozenset())

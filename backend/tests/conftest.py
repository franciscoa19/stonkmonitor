"""Shared test setup."""
import importlib

import pytest

# Application modules that copy market_time's clock into their own namespace at
# import time (a top-level `from market_time import et_today` / `et_now`).
# Tests freeze the clock with monkeypatch.setattr(market_time, "et_today", ...).
# A module imported for the FIRST time while such a patch is active copies the
# fake, and keeps it after monkeypatch restores market_time, so "today" stays
# frozen for every later test in the run. That made test_eval_loop fail when it
# ran after test_review_fixes (the shared `session` fixture's 2026-11-27 clock
# leaked into earnings_scanner and iv_executor). Importing them here, before any
# test runs, makes the result independent of test order.
# test_clock_isolation.py fails if a module with such an import is missing here.
CLOCK_COPYING_MODULES = (
    "daily_report",
    "db",
    "feeds.alpaca_feed",
    "feeds.earnings_calendar",
    "signals.earnings_scanner",
    "signals.iv_executor",
    "signals.iv_variants",
)
for _name in CLOCK_COPYING_MODULES:
    importlib.import_module(_name)


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

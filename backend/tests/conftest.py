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

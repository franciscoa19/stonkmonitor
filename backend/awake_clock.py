"""How long this process has been running without the Mac going to sleep.

With the lid shut the Mac still wakes for 10-60 s about every 15 minutes, and
the bot runs in those wakes: on 2026-10-26 it opened MSFT and GOOGL in one and
could not look at them again until the next. A thread notes the wall-clock time
every minute; while the Mac sleeps nothing runs, so the next note lands late and
the count starts again. Brief wakes never add up to a long run. New entries
require one (IV_EXEC_REQUIRE_AWAKE_MINUTES); exits never wait for it.

A thread rather than the event loop, so a busy loop is not mistaken for sleep.
"""
import threading
import time
from typing import Optional

BEAT_SECONDS = 60
GAP_SECONDS = 300          # a beat this late means the machine was not running


class AwakeClock:
    def __init__(self):
        self._lock = threading.Lock()
        self.since: Optional[float] = None     # wall time the current run began
        self.last: Optional[float] = None      # wall time of the latest beat
        self._thread: Optional[threading.Thread] = None

    def _broken(self, now: float) -> bool:
        return self.last is None or now < self.last or now - self.last > GAP_SECONDS

    def beat(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            if self._broken(now):              # first beat, a gap, or the clock went back
                self.since = now
            self.last = now

    def awake_minutes(self, now: Optional[float] = None) -> float:
        """Minutes of the current unbroken run; 0 when the last beat is stale."""
        now = time.time() if now is None else now
        with self._lock:
            return 0.0 if self._broken(now) else (now - self.since) / 60

    def start(self) -> None:
        """Beat now, then every minute in a daemon thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self.beat()

        def run():
            while True:
                time.sleep(BEAT_SECONDS)
                self.beat()
        self._thread = threading.Thread(target=run, name="awake-clock", daemon=True)
        self._thread.start()


clock = AwakeClock()

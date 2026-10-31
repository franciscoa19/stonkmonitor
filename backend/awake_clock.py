"""Entry readiness from host clocks that include and exclude suspended time.

macOS: CLOCK_MONOTONIC_RAW includes sleep; CLOCK_UPTIME_RAW excludes it.
Linux: CLOCK_BOOTTIME includes suspend; CLOCK_MONOTONIC excludes it.
Their difference detects short sleeps even between regular heartbeat samples.
Wall-clock adjustments do not affect the count. Unknown/failed clocks block
readiness; exits never use this guard.
"""
import logging
import math
import sys
import threading
import time
from typing import Callable, Optional

BEAT_SECONDS = 60
GAP_SECONDS = 300          # a stalled heartbeat also invalidates readiness
SUSPEND_TOLERANCE_SECONDS = 1.0   # clock-read jitter; cumulative since the reset
logger = logging.getLogger(__name__)


def _clock_times() -> tuple[float, float]:
    """Return (including suspend, excluding suspend), without adjustable clocks.

    Bracket the uptime read: a pause during sampling must not return an old
    inclusive timestamp alongside a newer uptime timestamp to the final gate.
    """
    if sys.platform == "darwin":
        continuous_id, uptime_id = time.CLOCK_MONOTONIC_RAW, time.CLOCK_UPTIME_RAW
    elif sys.platform.startswith("linux"):
        continuous_id, uptime_id = time.CLOCK_BOOTTIME, time.CLOCK_MONOTONIC
    else:
        raise NotImplementedError("No suspend-aware clock pair on this platform")
    before = time.clock_gettime(continuous_id)
    uptime = time.clock_gettime(uptime_id)
    after = time.clock_gettime(continuous_id)
    if not 0 <= after - before <= SUSPEND_TOLERANCE_SECONDS:
        raise ValueError("Clock sampling was interrupted")
    return after, uptime


class AwakeClock:
    def __init__(self, read_times: Optional[Callable[[], tuple[float, float]]] = None):
        self._lock = threading.Lock()
        self._read_times = read_times or _clock_times
        self.since: Optional[float] = None     # uptime at the start of this run
        self.last: Optional[float] = None      # inclusive time of the latest beat
        self._last_sample: Optional[tuple[float, float]] = None
        self._offset: Optional[float] = None
        self._generation = 0
        self._source_failed = False
        self._thread: Optional[threading.Thread] = None

    def _invalidate(self) -> None:
        if self.since is not None:
            self._generation += 1
        self.since = self.last = self._offset = None
        self._last_sample = None

    def _observe(self, *, beat: bool) -> float:
        """Sample under the lock; reads detect suspension before the next beat."""
        try:
            continuous, uptime = self._read_times()
            if not all(math.isfinite(t) and t >= 0 for t in (continuous, uptime)):
                raise ValueError("Invalid host clock")
        except Exception:
            self._invalidate()
            if not self._source_failed:
                logger.warning("Awake clock unavailable; new entries remain paused")
            self._source_failed = True
            return 0.0
        self._source_failed = False
        if self.since is not None:
            previous_continuous, previous_uptime = self._last_sample
            if (continuous < previous_continuous or uptime < previous_uptime
                    or continuous - self.last > GAP_SECONDS
                    or abs((continuous - uptime) - self._offset) > SUSPEND_TOLERANCE_SECONDS):
                self._invalidate()
        if beat:
            if self.since is None:
                self._generation += 1
                self.since = uptime
                self._offset = continuous - uptime
            self.last = continuous
        self._last_sample = continuous, uptime
        return 0.0 if self.since is None else (uptime - self.since) / 60

    def beat(self) -> None:
        with self._lock:
            self._observe(beat=True)

    def awake_minutes(self) -> float:
        """Zero until a beat, after suspension, or when the heartbeat is stale."""
        with self._lock:
            return self._observe(beat=False)

    def entry_token(self, required_minutes: float) -> Optional[int]:
        """An epoch for an eligible plan; a reset permanently invalidates it."""
        with self._lock:
            minutes = self._observe(beat=False)
            return self._generation if self.since is not None and minutes >= required_minutes else None

    def start(self) -> None:
        """Beat now, then every minute independently of the event loop."""
        def run():
            while True:
                time.sleep(BEAT_SECONDS)
                self.beat()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._observe(beat=True)
            self._thread = threading.Thread(target=run, name="awake-clock", daemon=True)
            self._thread.start()


clock = AwakeClock()

"""
Pushover push notifications — phone alerts for high-score signals.
"""
import logging
import aiohttp
from signals.engine import Signal, SignalSide

logger = logging.getLogger(__name__)

PUSHOVER_API = "https://api.pushover.net/1/messages.json"

# Priority levels: -1=quiet, 0=normal, 1=high, 2=emergency (requires ack)
PRIORITY_MAP = {
    (0, 5):   0,   # score 0-5: normal
    (5, 8):   1,   # score 5-8: high
    (8, 10):  1,   # score 8-10: high (use 2 for emergency if you want)
}

SOUND_MAP = {
    "golden_sweep":  "siren",
    "sweep":         "cashregister",
    "dark_pool":     "cosmic",
    "insider_buy":   "bugle",
    "congress_trade":"magic",
    "iv_high":       "tugboat",
    "iv_low":        "tugboat",
}


class PushoverNotifier:
    def __init__(self, api_token: str, user_key: str):
        self.api_token = api_token
        self.user_key = user_key
        # Treat unfilled .env placeholders ("your_...") as disabled, so we don't
        # spam invalid-token errors when Pushover was never configured.
        self.enabled = bool(api_token and user_key
                            and not api_token.startswith("your_")
                            and not user_key.startswith("your_"))

    def _get_priority(self, score: float) -> int:
        if score >= 8:
            return 1
        if score >= 5:
            return 0
        return -1

    async def send_signal(self, signal: Signal, score_threshold: float = 7.0):
        """Send a push notification for a signal."""
        if not self.enabled:
            return
        if signal.score < score_threshold:
            return

        priority = self._get_priority(signal.score)
        sound = SOUND_MAP.get(signal.type.value, "pushover")

        message = (
            f"{signal.description}\n"
            f"Score: {signal.score:.1f}/10 | {signal.side.value.upper()}"
        )

        payload = {
            "token":    self.api_token,
            "user":     self.user_key,
            "title":    signal.title,
            "message":  message,
            "priority": priority,
            "sound":    sound,
            "html":     0,
        }

        # Emergency priority needs retry + expire
        if priority == 2:
            payload["retry"] = 60
            payload["expire"] = 3600

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(PUSHOVER_API, data=payload) as resp:
                    if resp.status != 200:
                        logger.error(f"Pushover error {resp.status}: {await resp.text()}")
                    else:
                        logger.info(f"Pushover sent for {signal.ticker} score={signal.score:.1f}")
        except Exception as e:
            logger.error(f"Pushover send error: {e}")

    async def send_alert(self, title: str, message: str, priority: int = 0):
        """Send a manual/custom push notification."""
        if not self.enabled:
            return
        payload = {
            "token":    self.api_token,
            "user":     self.user_key,
            "title":    title,
            "message":  message,
            "priority": priority,
        }
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(PUSHOVER_API, data=payload)
        except Exception as e:
            logger.error(f"Pushover alert error: {e}")

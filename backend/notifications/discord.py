"""
Discord webhook notifications — rich embeds per signal type.
"""
import logging
import aiohttp
from datetime import datetime
from signals.engine import Signal, SignalType, SignalSide

logger = logging.getLogger(__name__)

# Color map per signal type
COLORS = {
    SignalType.GOLDEN_SWEEP:  0xFFD700,   # gold
    SignalType.SWEEP:         0x00FF88,   # green
    SignalType.OPTIONS_FLOW:  0x0099FF,   # blue
    SignalType.DARK_POOL:     0x9B59B6,   # purple
    SignalType.INSIDER_BUY:   0x2ECC71,   # green
    SignalType.INSIDER_SELL:  0xE74C3C,   # red
    SignalType.CONGRESS_TRADE:0xF39C12,   # orange
    SignalType.IV_HIGH:       0xFF6B6B,   # salmon
    SignalType.IV_LOW:        0x4ECDC4,   # teal
}

SIDE_COLORS = {
    SignalSide.BULLISH: 0x2ECC71,
    SignalSide.BEARISH: 0xE74C3C,
    SignalSide.NEUTRAL: 0x95A5A6,
}


class DiscordNotifier:
    def __init__(self, webhook_url: str, alerts_enabled: bool = True):
        self.webhook_url = webhook_url
        # `alerts_enabled` is a master mute: when False every send no-ops (they all
        # gate on self.enabled), but the client stays intact for later re-enable.
        self.enabled = bool(webhook_url) and alerts_enabled

    async def send_signal(self, signal: Signal, score_threshold: float = 0.0):
        """Post a signal as a rich Discord embed."""
        if not self.enabled:
            return
        if signal.score < score_threshold:
            return

        color = COLORS.get(signal.type, SIDE_COLORS.get(signal.side, 0x95A5A6))

        score_bar = self._score_bar(signal.score)

        fields = [
            {"name": "Signal Type", "value": signal.type.value.replace("_", " ").title(), "inline": True},
            {"name": "Score", "value": f"{score_bar} **{signal.score:.1f}/10**", "inline": True},
            {"name": "Direction", "value": signal.side.value.upper(), "inline": True},
        ]

        if signal.premium:
            fields.append({"name": "$ Value", "value": f"${signal.premium:,.0f}", "inline": True})
        if signal.strike:
            fields.append({"name": "Strike", "value": f"${signal.strike}", "inline": True})
        if signal.option_type:
            fields.append({"name": "Type", "value": signal.option_type.upper(), "inline": True})
        if signal.expiry:
            fields.append({"name": "Expiry", "value": signal.expiry, "inline": True})

        embed = {
            "title": signal.title,
            "description": signal.description,
            "color": color,
            "fields": fields,
            "footer": {"text": "StonkMonitor | Unusual Whales Feed"},
            "timestamp": signal.timestamp.isoformat(),
        }

        payload = {"embeds": [embed]}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.webhook_url, json=payload) as resp:
                    if resp.status not in (200, 204):
                        logger.error(f"Discord webhook error {resp.status}: {await resp.text()}")
        except Exception as e:
            logger.error(f"Discord send error: {e}")

    async def send_raw(self, content: str):
        """Send a plain text message."""
        if not self.enabled:
            return
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(self.webhook_url, json={"content": content})
        except Exception as e:
            logger.error(f"Discord raw send error: {e}")

    def _score_bar(self, score: float) -> str:
        filled = int(round(score))
        empty = 10 - filled
        return "🟩" * filled + "⬛" * empty

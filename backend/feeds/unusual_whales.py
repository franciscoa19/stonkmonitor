"""
Unusual Whales API client — REST polling + async streaming.
Correct endpoints sourced from https://unusualwhales.com/skill.md
Auth: Bearer token + UW-CLIENT-API-ID header required on every request.
"""
import asyncio
import json
import logging
import aiohttp
from typing import Callable, Optional

logger = logging.getLogger(__name__)

UW_BASE = "https://api.unusualwhales.com"

# Correct endpoint paths per official skill.md
ENDPOINTS = {
    "options_flow":    "/api/option-trades/flow-alerts",
    "darkpool_recent": "/api/darkpool/recent",
    "darkpool_ticker": "/api/darkpool/{ticker}",
    "insider":         "/api/insider/transactions",
    "congress":        "/api/congress/recent-trades",
    "iv":              "/api/stock/{ticker}/interpolated-iv",
    "flow_recent":     "/api/stock/{ticker}/flow-recent",
    "option_contracts":"/api/stock/{ticker}/option-contracts",
    "greeks":          "/api/stock/{ticker}/greeks",
}


class UnusualWhalesClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "UW-CLIENT-API-ID": "100001",   # required by UW API
            "Content-Type": "application/json",
        }
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: dict = None) -> dict | list:
        """Shared GET helper with error logging and 429 backoff."""
        session = await self._get_session()
        url = f"{UW_BASE}{path}"
        async with session.get(url, params=params or {}) as resp:
            if resp.status == 401:
                logger.error(f"UW 401 Unauthorized — check your API key")
                return {}
            if resp.status == 429:
                retry_after = int(resp.headers.get("Retry-After", 30))
                logger.warning(f"UW 429 rate limit on {path} — backing off {retry_after}s")
                await asyncio.sleep(retry_after)
                return {}
            if resp.status != 200:
                text = await resp.text()
                logger.error(f"UW {resp.status} on {path}: {text[:200]}")
                return {}
            return await resp.json()

    # ------------------------------------------------------------------ #
    #  REST: Options Flow                                                  #
    # ------------------------------------------------------------------ #
    async def get_options_flow(
        self,
        ticker: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Fetch recent unusual options flow alerts."""
        if ticker:
            # Per-ticker flow
            path = ENDPOINTS["flow_recent"].format(ticker=ticker.upper())
            data = await self._get(path, {"limit": limit})
        else:
            # Market-wide flow alerts
            data = await self._get(ENDPOINTS["options_flow"], {"limit": limit})
        return data.get("data", []) if isinstance(data, dict) else data

    # ------------------------------------------------------------------ #
    #  REST: Dark Pool Prints                                              #
    # ------------------------------------------------------------------ #
    async def get_darkpool_flow(
        self,
        ticker: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Fetch recent dark pool / off-exchange block prints."""
        if ticker:
            path = ENDPOINTS["darkpool_ticker"].format(ticker=ticker.upper())
        else:
            path = ENDPOINTS["darkpool_recent"]
        data = await self._get(path, {"limit": limit})
        return data.get("data", []) if isinstance(data, dict) else data

    # ------------------------------------------------------------------ #
    #  REST: Insider Trades                                                #
    # ------------------------------------------------------------------ #
    async def get_insider_trades(
        self,
        ticker: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Fetch recent SEC Form 4 insider transactions."""
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker.upper()
        data = await self._get(ENDPOINTS["insider"], params)
        return data.get("data", []) if isinstance(data, dict) else data

    # ------------------------------------------------------------------ #
    #  REST: Congress Trades                                               #
    # ------------------------------------------------------------------ #
    async def get_congress_trades(self, limit: int = 50) -> list[dict]:
        """Fetch recent congressional stock disclosures."""
        data = await self._get(ENDPOINTS["congress"], {"limit": limit})
        return data.get("data", []) if isinstance(data, dict) else data

    # ------------------------------------------------------------------ #
    #  REST: IV Data                                                       #
    # ------------------------------------------------------------------ #
    async def get_iv_rank(self, ticker: str) -> dict:
        """Get interpolated IV data for a ticker."""
        path = ENDPOINTS["iv"].format(ticker=ticker.upper())
        data = await self._get(path)
        return data.get("data", {}) if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ #
    #  REST: Option Contracts (chain with Greeks)                          #
    # ------------------------------------------------------------------ #
    async def get_option_contracts(self, ticker: str) -> list[dict]:
        """Get option contracts with Greeks for a ticker."""
        path = ENDPOINTS["option_contracts"].format(ticker=ticker.upper())
        data = await self._get(path)
        return data.get("data", []) if isinstance(data, dict) else []

    # ------------------------------------------------------------------ #
    #  Streaming: Poll-based real-time feed                                #
    # ------------------------------------------------------------------ #
    async def stream_flow(
        self,
        on_event: Callable,
        channels: list[str] = None,
        poll_interval: float = 15.0,
    ):
        """
        Poll UW REST endpoints on a tight loop to simulate streaming.
        Deduplicates by tracking seen IDs so each event fires only once.
        channels can include: "options-flow", "darkpool", "insider-trades", "congress-trades"
        """
        if channels is None:
            channels = ["options-flow", "darkpool", "insider-trades", "congress-trades"]

        seen_ids: set[str] = set()

        async def fetch_and_emit(feed_type: str, items: list):
            for item in items:
                # Build a dedup key from whatever ID fields UW returns
                uid = (
                    item.get("id") or
                    item.get("trade_id") or
                    item.get("filing_id") or
                    f"{item.get('ticker','')}-{item.get('date','')}-{item.get('premium','')}"
                )
                if uid in seen_ids:
                    continue
                seen_ids.add(uid)
                if len(seen_ids) > 5000:          # keep memory bounded
                    seen_ids.clear()

                event = {"channel": feed_type, "data": item}
                if asyncio.iscoroutinefunction(on_event):
                    await on_event(event)
                else:
                    on_event(event)

        logger.info("UW feed starting (REST polling mode, staggered)...")

        # Stagger channels so we never exceed 3 concurrent requests (UW limit)
        # Each channel polls sequentially with a small gap between them
        CHANNEL_FUNCS = {
            "options-flow":   lambda: self.get_options_flow(limit=50),
            "darkpool":       lambda: self.get_darkpool_flow(limit=50),
            "insider-trades": lambda: self.get_insider_trades(limit=50),
            "congress-trades":lambda: self.get_congress_trades(limit=50),
        }
        active_channels = [ch for ch in CHANNEL_FUNCS if ch in channels]

        # Per-channel cooldown tracker for 429 backoff
        backoff: dict[str, float] = {ch: 0.0 for ch in active_channels}

        while True:
            for feed_type in active_channels:
                # Respect per-channel backoff
                if backoff[feed_type] > 0:
                    backoff[feed_type] = max(0.0, backoff[feed_type] - poll_interval)
                    continue
                try:
                    result = await CHANNEL_FUNCS[feed_type]()
                    if result:
                        await fetch_and_emit(feed_type, result)
                except Exception as e:
                    logger.warning(f"Poll error on {feed_type}: {e}")

                # Stagger: 2s gap between each channel call
                await asyncio.sleep(2)

            await asyncio.sleep(poll_interval)

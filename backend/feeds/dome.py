"""
Dome API client — unified prediction market data across Polymarket + Kalshi.

Docs: https://docs.domeapi.io

Only a few endpoints are needed for cross-platform arb:
  GET /v1/polymarket/markets?search=...&status=open     — metadata search
  GET /v1/polymarket/markets?market_slug=...            — single market
  GET /v1/kalshi/markets?...                            — Kalshi mirror w/ prices

Dome's Polymarket markets endpoint does NOT include live prices — those come
from the Polymarket CLOB directly (see feeds/polymarket.py). Dome is used
for metadata, search, and resolving condition_id / token_id values.
"""
import asyncio
import logging
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# Circuit breaker: after this many consecutive failures, pause all Dome calls
# for COOLDOWN seconds. Dome sits behind Cloudflare bot protection (HTTP 403
# "error code: 1010") that blocks non-browser clients, so when it's blocking
# every call times out at 15s — without a breaker that's wasted time + log spam
# on every Kalshi scan cycle. Auto-retries after the cooldown in case access
# is restored.
_FAIL_LIMIT = 5
_COOLDOWN = 1800  # 30 minutes


class DomeClient:
    def __init__(self, api_key: str, base_url: str = "https://api.domeapi.io"):
        self.api_key  = api_key
        self.base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._fail_count = 0
        self._disabled_until = 0.0

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=aiohttp.ClientTimeout(total=15),
            )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _note_failure(self, path: str, reason: str) -> None:
        self._fail_count += 1
        if self._fail_count >= _FAIL_LIMIT:
            self._disabled_until = time.time() + _COOLDOWN
            self._fail_count = 0
            logger.warning(
                f"Dome paused for {_COOLDOWN // 60}min after {_FAIL_LIMIT} consecutive "
                f"failures (last {path}: {reason}) — likely Cloudflare bot block "
                f"(HTTP 403 code 1010). Cross-platform arb is degraded until access is restored."
            )
        else:
            logger.debug(f"Dome {path} failed ({reason}) [{self._fail_count}/{_FAIL_LIMIT}]")

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        if time.time() < self._disabled_until:
            return {}   # circuit breaker open — skip the call entirely
        await self._ensure_session()
        url = f"{self.base_url}{path}"
        try:
            async with self._session.get(url, params=params) as r:
                if r.status != 200:
                    body = await r.text()
                    self._note_failure(path, f"HTTP {r.status}: {body[:120]}")
                    return {}
                self._fail_count = 0   # success resets the breaker
                return await r.json()
        except Exception as e:
            self._note_failure(path, type(e).__name__ or "timeout")
            return {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # ── Polymarket ────────────────────────────────────────────────────────
    async def polymarket_search(
        self,
        search: str,
        status: str = "open",
        limit: int = 10,
    ) -> list[dict]:
        """Full-text search Polymarket markets by keywords."""
        data = await self._get(
            "/v1/polymarket/markets",
            {"search": search, "status": status, "limit": limit},
        )
        return data.get("markets", [])

    async def polymarket_by_slug(self, market_slug: str) -> Optional[dict]:
        """Fetch a single Polymarket market by slug (returns first match)."""
        data = await self._get(
            "/v1/polymarket/markets",
            {"market_slug": market_slug},
        )
        markets = data.get("markets", [])
        return markets[0] if markets else None

    # ── Kalshi mirror (with prices!) ──────────────────────────────────────
    async def kalshi_markets(
        self,
        market_ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        status: str = "open",
        limit: int = 100,
    ) -> list[dict]:
        params: dict = {"status": status, "limit": limit}
        if market_ticker: params["market_ticker"] = market_ticker
        if event_ticker:  params["event_ticker"]  = event_ticker
        data = await self._get("/v1/kalshi/markets", params)
        return data.get("markets", [])

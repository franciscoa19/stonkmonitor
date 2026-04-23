"""
Unusual Whales API client — REST polling + async streaming.
Correct endpoints sourced from https://unusualwhales.com/skill.md
Auth: Bearer token + UW-CLIENT-API-ID header required on every request.
"""
import asyncio
import json
import logging
import time
import aiohttp
from typing import Callable, Optional

from feeds.uw_budget import budget, current_session, interval_for

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
        """Shared GET helper with error logging, 429 backoff, and budget tracking."""
        # Hard safety: if we're over the pause threshold, refuse the call.
        # stream_flow already backs off proactively; this catches stray callers
        # (IV scanner, manual endpoints) before they push us over the limit.
        if budget.should_pause():
            logger.warning(
                f"UW budget pause ({budget.daily_count}/{budget.daily_limit}) "
                f"— skipping {path}"
            )
            return {}

        session = await self._get_session()
        url = f"{UW_BASE}{path}"
        async with session.get(url, params=params or {}) as resp:
            # Always update the budget tracker from response headers,
            # even on errors — UW returns the counters on 429 too.
            budget.update_from_headers(path, resp.headers)

            if resp.status == 401:
                logger.error(f"UW 401 Unauthorized — check your API key")
                return {}
            if resp.status == 429:
                retry_after = int(resp.headers.get("Retry-After", 30))
                logger.warning(
                    f"UW 429 rate limit on {path} "
                    f"(daily {budget.daily_count}/{budget.daily_limit}) "
                    f"— backing off {retry_after}s"
                )
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
        poll_interval: float = 15.0,       # retained for API compat, no longer used
        seed_seen_ids: set = None,
    ):
        """
        Poll UW REST endpoints to simulate streaming, but intelligently:

          * Each channel has its own per-session interval (see feeds/uw_budget.py).
            Interval -1 means "don't poll in this session at all" — we skip it.
          * We track the last-poll timestamp per channel and only call when
            the interval has elapsed, so the loop can sleep short (~5s) and
            still hit exactly the cadence we configured.
          * If the daily budget tracker says we're over the throttle threshold
            (default 80%), every channel interval is doubled.
          * If the budget tracker says we're over the pause threshold (95%),
            the whole loop idles for 5 minutes at a time until midnight rolls
            the counter.
          * Sessions transition (e.g. RTH closes) are logged so the operator
            can see why the cadence just changed.

        Dedup key behavior and seed_seen_ids are unchanged.
        """
        if channels is None:
            channels = ["options-flow", "darkpool", "insider-trades", "congress-trades"]

        seen_ids: set[str] = set(seed_seen_ids) if seed_seen_ids else set()
        logger.info(f"UW feed: seeded dedup set with {len(seen_ids)} existing IDs")

        async def fetch_and_emit(feed_type: str, items: list):
            for item in items:
                # Channel-specific dedup keys MUST match how db.save_*() builds row IDs,
                # so seed_seen_ids (loaded from DB on startup) actually matches what the
                # feed sees. Bug fixed 2026-04-22: congress trades had no `id`/`trade_id`
                # so the fallback `{ticker}-{date}-{premium}` was hit (date+premium absent
                # for congress data) → produced 73k duplicate signals/day after restart.
                if feed_type == "congress-trades":
                    # Mirrors db.save_congress_trade composite key
                    uid = (
                        f"{item.get('politician_id','')}_"
                        f"{item.get('transaction_date','')}_"
                        f"{(item.get('ticker','') or '').upper()}"
                    )
                elif feed_type == "insider-trades":
                    # insider has a real `id` (UUID) — use it; fallback if ever missing
                    uid = item.get("id") or (
                        f"{item.get('ticker','')}_"
                        f"{item.get('owner_name','')}_"
                        f"{item.get('transaction_date','')}_"
                        f"{item.get('shares','')}"
                    )
                else:
                    # options-flow / darkpool — UW returns reliable `id`/`trade_id`
                    uid = (
                        item.get("id") or
                        item.get("trade_id") or
                        item.get("filing_id") or
                        f"{item.get('ticker','')}-{item.get('date','')}-{item.get('premium','')}"
                    )

                if not uid or uid in seen_ids:
                    continue
                seen_ids.add(uid)
                # Cap memory growth. Old code cleared at 5000 → on the next poll
                # all 50 items per channel looked "new" again and re-fired through
                # the engine. Bumping to 50k means a clear is extremely rare under
                # normal flow; if it does happen, the seed_seen_ids reload on next
                # startup pulls slow-moving channels (congress, insider) from DB.
                if len(seen_ids) > 50000:
                    seen_ids.clear()

                event = {"channel": feed_type, "data": item}
                if asyncio.iscoroutinefunction(on_event):
                    await on_event(event)
                else:
                    on_event(event)

        CHANNEL_FUNCS = {
            "options-flow":   lambda: self.get_options_flow(limit=50),
            "darkpool":       lambda: self.get_darkpool_flow(limit=50),
            "insider-trades": lambda: self.get_insider_trades(limit=50),
            "congress-trades":lambda: self.get_congress_trades(limit=50),
        }
        active_channels = [ch for ch in CHANNEL_FUNCS if ch in channels]

        # Per-channel last-poll timestamps (monotonic seconds)
        last_poll: dict[str, float] = {ch: 0.0 for ch in active_channels}
        last_session: str = current_session()
        logger.info(
            f"UW feed starting — session={last_session} "
            f"channels={active_channels} "
            f"intervals={{{', '.join(f'{c}:{interval_for(c, last_session)}s' for c in active_channels)}}}"
        )

        while True:
            # Budget pause: sleep a long time then re-check. The daily counter
            # resets on UW's end at midnight UTC so we'll eventually recover.
            if budget.should_pause():
                logger.warning(
                    f"UW budget PAUSE {budget.daily_count}/{budget.daily_limit} "
                    f"({budget.usage_pct*100:.0f}%) — idling 5 min"
                )
                await asyncio.sleep(300)
                continue

            sess = current_session()
            if sess != last_session:
                logger.info(f"UW feed session change: {last_session} → {sess}")
                last_session = sess

            throttle_mult = 2.0 if budget.should_throttle() else 1.0
            now = time.monotonic()

            for feed_type in active_channels:
                base = interval_for(feed_type, sess)
                if base < 0:
                    continue   # channel disabled for this session
                due_in = (last_poll[feed_type] + base * throttle_mult) - now
                if due_in > 0:
                    continue

                try:
                    result = await CHANNEL_FUNCS[feed_type]()
                    if result:
                        await fetch_and_emit(feed_type, result)
                except Exception as e:
                    logger.warning(f"Poll error on {feed_type}: {e}")

                last_poll[feed_type] = time.monotonic()
                # 2s gap between channels so we never hit UW's 3-concurrent limit
                await asyncio.sleep(2)

            # Main-loop tick. Short enough to respect the tightest interval
            # (15s RTH) without drifting, long enough to be basically free.
            await asyncio.sleep(5)

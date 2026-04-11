"""
Kalshi prediction market client.
REST API — no WebSocket needed for our use case.

Docs: https://trading-api.readme.io/reference
Base: https://trading-api.kalshi.com/trade-api/v2   (live)
      https://demo-api.kalshi.co/trade-api/v2        (demo)

Auth: Bearer token via email+password login endpoint, or API key header.

Strategy: scan all open markets, score by:
  1. Edge  = |true_prob - market_mid|  where true_prob comes from our model
  2. Liquidity = min(yes_ask_size, no_bid_size)
  3. Kelly-sized bet, capped at MAX_RISK_PCT of bankroll
"""
import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

LIVE_BASE  = "https://trading-api.kalshi.com/trade-api/v2"
DEMO_BASE  = "https://demo-api.kalshi.co/trade-api/v2"


class KalshiClient:
    def __init__(self, email: str, password: str, demo: bool = True):
        self.email    = email
        self.password = password
        self.demo     = demo
        self.base     = DEMO_BASE if demo else LIVE_BASE
        self._session: Optional[aiohttp.ClientSession] = None
        self._token: Optional[str] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Auth ─────────────────────────────────────────────────────────────────

    async def login(self) -> bool:
        """Exchange email/password for a session token."""
        session = await self._get_session()
        try:
            async with session.post(
                f"{self.base}/login",
                json={"email": self.email, "password": self.password},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._token = data.get("token")
                    logger.info(f"Kalshi login OK ({'demo' if self.demo else 'live'})")
                    return True
                text = await resp.text()
                logger.error(f"Kalshi login failed {resp.status}: {text[:200]}")
                return False
        except Exception as e:
            logger.error(f"Kalshi login error: {e}")
            return False

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    # ── REST helpers ─────────────────────────────────────────────────────────

    async def _get(self, path: str, params: dict = None) -> dict:
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.base}{path}",
                params=params or {},
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    logger.warning("Kalshi 401 — re-logging in")
                    await self.login()
                    return {}
                if resp.status == 429:
                    await asyncio.sleep(5)
                    return {}
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"Kalshi {resp.status} on {path}: {text[:200]}")
                    return {}
                return await resp.json()
        except Exception as e:
            logger.error(f"Kalshi GET {path} error: {e}")
            return {}

    async def _post(self, path: str, body: dict) -> dict:
        session = await self._get_session()
        try:
            async with session.post(
                f"{self.base}{path}",
                json=body,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if resp.status not in (200, 201):
                    logger.error(f"Kalshi POST {path} {resp.status}: {data}")
                return data
        except Exception as e:
            logger.error(f"Kalshi POST {path} error: {e}")
            return {"error": str(e)}

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_markets(
        self,
        status: str = "open",
        limit: int = 200,
        cursor: str = None,
    ) -> list[dict]:
        """
        Fetch open markets. Returns list of market dicts.
        Key fields per market:
          ticker, title, status, close_time,
          yes_ask, yes_bid, no_ask, no_bid,  (prices in cents, 1-99)
          volume, open_interest, result
        """
        params = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = await self._get("/markets", params)
        return data.get("markets", [])

    async def get_market(self, ticker: str) -> dict:
        """Get a single market by ticker."""
        data = await self._get(f"/markets/{ticker}")
        return data.get("market", {})

    async def get_orderbook(self, ticker: str) -> dict:
        """Get full orderbook for a market."""
        data = await self._get(f"/markets/{ticker}/orderbook")
        return data.get("orderbook", {})

    async def get_balance(self) -> dict:
        """Get account balance."""
        data = await self._get("/portfolio/balance")
        return data  # {"balance": cents}

    async def get_positions(self) -> list[dict]:
        """Get all open positions."""
        data = await self._get("/portfolio/positions")
        return data.get("market_positions", [])

    async def get_fills(self, limit: int = 50) -> list[dict]:
        """Recent filled orders."""
        data = await self._get("/portfolio/fills", {"limit": limit})
        return data.get("fills", [])

    # ── Order execution ───────────────────────────────────────────────────────

    async def place_order(
        self,
        ticker: str,
        side: str,          # "yes" | "no"
        action: str,        # "buy" | "sell"
        count: int,         # number of contracts
        order_type: str = "limit",
        price: int = None,  # cents (1-99)
    ) -> dict:
        """
        Place a Kalshi order.
        count = number of contracts ($0.01 each at price cents)
        price in cents: e.g. 95 = $0.95/contract
        """
        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
        }
        if order_type == "limit" and price is not None:
            body["yes_price"] = price if side == "yes" else (100 - price)
        logger.info(f"Kalshi order: {action} {count}x {ticker} {side} @ {price}¢")
        return await self._post("/portfolio/orders", body)

    async def cancel_order(self, order_id: str) -> dict:
        session = await self._get_session()
        try:
            async with session.delete(
                f"{self.base}/portfolio/orders/{order_id}",
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return await resp.json()
        except Exception as e:
            return {"error": str(e)}

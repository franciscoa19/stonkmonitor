"""
Kalshi trading API client — RSA-PSS key authentication.

Auth flow (no email/password needed):
  1. Generate an API key pair in Kalshi dashboard → get Key ID + private key PEM
  2. Every request is signed: timestamp_ms + METHOD + path (no query string)
  3. Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE

Docs: https://trading-api.readme.io/reference
Base URLs:
  Live: https://api.elections.kalshi.com/trade-api/v2
  Demo: https://demo-api.kalshi.co/trade-api/v2
"""
import asyncio
import aiohttp
import base64
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

LIVE_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"


def _sign(timestamp_ms: str, method: str, path: str, private_key_pem: str) -> str:
    """
    RSA-PSS sign a Kalshi request.
    Message = timestamp_ms + METHOD + /trade-api/v2/path  (no query string)
    Returns base64-encoded signature.
    """
    from Crypto.PublicKey import RSA
    from Crypto.Signature import pss
    from Crypto.Hash import SHA256

    # Strip query params before signing
    path_no_query = path.split("?")[0]
    message = f"{timestamp_ms}{method.upper()}{path_no_query}"

    key = RSA.import_key(private_key_pem)
    h   = SHA256.new(message.encode("utf-8"))
    sig = pss.new(key).sign(h)
    return base64.b64encode(sig).decode("utf-8")


class KalshiClient:
    def __init__(self, key_id: str, private_key_pem: str, demo: bool = False):
        """
        key_id          — from Kalshi dashboard (looks like a UUID)
        private_key_pem — full PEM string including -----BEGIN RSA PRIVATE KEY----- etc.
                          Can also be a file path ending in .pem
        demo            — True = demo sandbox, False = live trading
        """
        self.key_id  = key_id
        self.demo    = demo
        self.base    = DEMO_BASE if demo else LIVE_BASE
        self._session: Optional[aiohttp.ClientSession] = None

        # Accept either a PEM string or a file path
        if private_key_pem.strip().endswith(".pem") or not private_key_pem.strip().startswith("-----"):
            try:
                with open(private_key_pem.strip(), "r") as f:
                    self._private_key = f.read()
                logger.info(f"Kalshi: loaded private key from {private_key_pem.strip()}")
            except FileNotFoundError:
                logger.error(f"Kalshi: private key file not found: {private_key_pem.strip()}")
                self._private_key = ""
        else:
            self._private_key = private_key_pem

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _auth_headers(self, method: str, path: str) -> dict:
        """Build the three Kalshi auth headers for a request."""
        ts = str(int(time.time() * 1000))
        sig = _sign(ts, method, path, self._private_key)
        return {
            "KALSHI-ACCESS-KEY":       self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type": "application/json",
        }

    # ── REST helpers ─────────────────────────────────────────────────────────

    async def _get(self, path: str, params: dict = None) -> dict:
        """Signed GET request. path should be relative, e.g. '/markets'."""
        full_path = f"/trade-api/v2{path}"
        query = ""
        if params:
            import urllib.parse
            query = "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})

        session = await self._get_session()
        url = self.base + path + query

        try:
            async with session.get(
                url,
                headers=self._auth_headers("GET", full_path),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    logger.error("Kalshi 401 — check key_id and private key")
                    return {}
                if resp.status == 429:
                    retry = int(resp.headers.get("Retry-After", 5))
                    logger.warning(f"Kalshi 429 — waiting {retry}s")
                    await asyncio.sleep(retry)
                    return {}
                if resp.status not in (200, 201):
                    text = await resp.text()
                    logger.error(f"Kalshi {resp.status} GET {path}: {text[:200]}")
                    return {}
                return await resp.json()
        except Exception as e:
            logger.error(f"Kalshi GET {path} error: {e}")
            return {}

    async def _post(self, path: str, body: dict) -> dict:
        """Signed POST request."""
        full_path = f"/trade-api/v2{path}"
        session   = await self._get_session()
        url       = self.base + path

        try:
            async with session.post(
                url,
                json=body,
                headers=self._auth_headers("POST", full_path),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if resp.status not in (200, 201):
                    logger.error(f"Kalshi {resp.status} POST {path}: {data}")
                return data
        except Exception as e:
            logger.error(f"Kalshi POST {path} error: {e}")
            return {"error": str(e)}

    async def _delete(self, path: str) -> dict:
        """Signed DELETE request."""
        full_path = f"/trade-api/v2{path}"
        session   = await self._get_session()
        url       = self.base + path

        try:
            async with session.delete(
                url,
                headers=self._auth_headers("DELETE", full_path),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return await resp.json()
        except Exception as e:
            logger.error(f"Kalshi DELETE {path} error: {e}")
            return {"error": str(e)}

    # ── Connection test ───────────────────────────────────────────────────────

    async def ping(self) -> bool:
        """Test auth — returns True if credentials are valid."""
        data = await self._get("/portfolio/balance")
        if data:
            bal = data.get("balance", 0) / 100
            logger.info(f"Kalshi auth OK — balance: ${bal:.2f} ({'demo' if self.demo else 'LIVE'})")
            return True
        logger.error("Kalshi ping failed — check key_id and private key PEM")
        return False

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_markets(
        self,
        status: str = "open",
        page_size: int = 200,
        max_markets: int = 5000,
    ) -> list[dict]:
        """
        Fetch ALL open markets by paginating the events endpoint.
        Returns a flat list of market dicts enriched with the parent
        event's title and category.

        Real field names (all prices in dollars 0.0–1.0):
          yes_ask_dollars, yes_bid_dollars, no_ask_dollars, no_bid_dollars
          volume_fp, open_interest_fp, close_time, ticker, title, category
        """
        markets: list[dict] = []
        cursor: Optional[str] = None

        while len(markets) < max_markets:
            params: dict = {
                "status": status,
                "limit": page_size,
                "with_nested_markets": "true",
            }
            if cursor:
                params["cursor"] = cursor

            data = await self._get("/events", params)
            events = data.get("events", [])
            if not events:
                break

            for event in events:
                event_title    = event.get("title", "")
                event_category = event.get("category", "")
                for m in event.get("markets", []):
                    m["event_title"]    = event_title
                    m["event_category"] = event_category
                    if not m.get("title"):
                        m["title"] = event_title
                    markets.append(m)

            cursor = data.get("cursor")
            if not cursor:
                break  # no more pages

        logger.info(f"Kalshi: fetched {len(markets)} markets across all categories")
        return markets[:max_markets]

    async def get_events(self, status: str = "open", limit: int = 200) -> list[dict]:
        """Raw events list (without flattening)."""
        data = await self._get("/events", {"status": status, "limit": limit, "with_nested_markets": "true"})
        return data.get("events", [])

    async def get_market(self, ticker: str) -> dict:
        data = await self._get(f"/markets/{ticker}")
        return data.get("market", {})

    async def get_orderbook(self, ticker: str) -> dict:
        data = await self._get(f"/markets/{ticker}/orderbook")
        return data.get("orderbook", {})

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_balance(self) -> dict:
        """Returns raw balance dict. Use balance_cents / 100 for dollars."""
        return await self._get("/portfolio/balance")

    async def get_positions(self) -> list[dict]:
        data = await self._get("/portfolio/positions")
        return data.get("market_positions", [])

    async def get_fills(self, limit: int = 50) -> list[dict]:
        data = await self._get("/portfolio/fills", {"limit": limit})
        return data.get("fills", [])

    async def get_orders(self, status: str = "resting") -> list[dict]:
        data = await self._get("/portfolio/orders", {"status": status})
        return data.get("orders", [])

    # ── Order execution ───────────────────────────────────────────────────────

    async def place_order(
        self,
        ticker: str,
        side: str,           # "yes" | "no"
        action: str,         # "buy" | "sell"
        count: int,          # number of contracts
        order_type: str = "limit",
        price: int = None,   # cents (1-99), the YES price
    ) -> dict:
        """
        Place a Kalshi order.
          count = contracts ($1.00 payout each)
          price = YES price in cents (e.g. 92 = $0.92/contract)
          For NO buys: Kalshi derives no_price = 100 - yes_price automatically
        """
        body: dict = {
            "ticker":  ticker,
            "side":    side,
            "action":  action,
            "count":   count,
            "type":    order_type,
        }
        if order_type == "limit" and price is not None:
            # API always takes yes_price regardless of side
            yes_price = price if side == "yes" else (100 - price)
            body["yes_price"] = yes_price

        mode = "DEMO" if self.demo else "LIVE"
        logger.info(f"[{mode}] Kalshi order: {action} {count}x {ticker} {side.upper()} @ {price}¢")
        return await self._post("/portfolio/orders", body)

    async def cancel_order(self, order_id: str) -> dict:
        return await self._delete(f"/portfolio/orders/{order_id}")

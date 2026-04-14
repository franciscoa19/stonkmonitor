"""
AutoTradeEngine — evaluates high-conviction signals and pattern hits,
builds specific Alpaca-ready trade suggestions (options or equity),
sizes positions based on portfolio equity, and queues them for
1-tap Telegram execution with a 5-minute expiry window.

Trigger logic:
  • Signal score >= AUTO_TRADE_SCORE_THRESHOLD  (default 8.5)
    → options sweep/golden_sweep → options trade
    → insider_buy / congress_trade bullish → equity trade

  • Pattern score >= AUTO_TRADE_PATTERN_THRESHOLD (default 9.0)
    AND pattern in AUTO_TRADE_PATTERNS set
    → options trade if options evidence exists, else equity

Position sizing:
  • max_risk = min(equity * max_risk_pct, max_risk_usd)
  • options: contracts = floor(max_risk / (limit_price * 100)), min 1
  • equity:  shares    = floor(max_risk / price), min 1

Options contract selection:
  • Uses strike + expiry from the UW signal (already market-selected)
  • Constructs OCC symbol: TICKER YYMMDD C/P STRIKE*1000 (8-digit)
  • Gets live bid/ask from Alpaca data API
  • Limit price = ask (or bid*1.15 if spread is extreme)

DTE guard:
  • Rejects < MIN_DTE days (default 2) unless score >= 9.5
  • Rejects > MAX_DTE days (default 21) — no LEAPS chasing
"""
import asyncio
import aiohttp
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Patterns that qualify for auto-trade queue
AUTO_TRADE_PATTERNS = {
    "triple_confluence",
    "insider_buy_plus_sweep",
    "sweep_plus_darkpool",
    "golden_sweep_cluster",
    "congress_plus_sweep",
}

ZERO_DTE_MIN_SCORE = 9.5   # allow 0-1 DTE only if this confident


@dataclass
class TradeSuggestion:
    id: int
    ticker: str
    trade_type: str           # "option" | "equity"
    symbol: str               # OCC symbol or equity ticker
    side: str                 # "bullish" | "bearish"
    option_type: Optional[str]
    strike: Optional[float]
    expiry: Optional[str]
    dte: Optional[int]
    qty: int
    limit_price: float
    risk_amount: float
    stop_pct: float
    target_pct: float
    score: float
    rationale: str
    expires_at: Optional[datetime] = None
    telegram_msg_id: Optional[int] = None


class AutoTradeEngine:
    def __init__(self, settings):
        self.settings = settings
        self._session: Optional[aiohttp.ClientSession] = None
        self._telegram = None
        self._db = None
        self._trader = None
        self._pending: dict[int, TradeSuggestion] = {}

    def set_dependencies(self, telegram, db, trader):
        self._telegram = telegram
        self._db = db
        self._trader = trader

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _session_get(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ── Symbol helpers ───────────────────────────────────────────────────────

    def _occ_symbol(self, ticker: str, expiry: str, opt_type: str, strike: float) -> str:
        """
        Build OCC option symbol.
        Example: SNDK + 2026-04-24 + call + 840 → SNDK260424C00840000
        """
        try:
            clean = expiry.replace("-", "")   # "20260424"
            date_part = clean[2:]             # "260424"
            cp = "C" if "call" in opt_type.lower() else "P"
            strike_int = int(round(strike * 1000))
            return f"{ticker.upper()}{date_part}{cp}{strike_int:08d}"
        except Exception as e:
            logger.warning(f"OCC symbol error: {e}")
            return ""

    def _calc_dte(self, expiry: str) -> Optional[int]:
        try:
            exp = datetime.strptime(expiry, "%Y-%m-%d")
            return (exp - datetime.utcnow()).days
        except Exception:
            return None

    # ── Market data (Alpaca) ─────────────────────────────────────────────────

    async def _get_option_quote(self, symbol: str) -> dict:
        """Get option snapshot from Alpaca data API."""
        try:
            session = await self._session_get()
            headers = {
                "APCA-API-KEY-ID":     self.settings.alpaca_api_key,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            }
            url = "https://data.alpaca.markets/v1beta1/options/snapshots"
            async with session.get(
                url, headers=headers,
                params={"symbols": symbol, "feed": "indicative"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    snap = (data.get("snapshots") or {}).get(symbol, {})
                    quote = snap.get("latestQuote") or {}
                    trade = snap.get("latestTrade") or {}
                    return {
                        "ask":  float(quote.get("ap") or 0),
                        "bid":  float(quote.get("bp") or 0),
                        "last": float(trade.get("p")  or 0),
                        "iv":   float(snap.get("impliedVolatility") or 0),
                    }
                logger.warning(f"Option quote HTTP {resp.status} for {symbol}")
        except asyncio.TimeoutError:
            logger.warning(f"Option quote timeout for {symbol}")
        except Exception as e:
            logger.warning(f"Option quote error for {symbol}: {e}")
        return {}

    async def _get_equity_price(self, ticker: str) -> float:
        """Get mid-price from Alpaca IEX feed."""
        try:
            session = await self._session_get()
            headers = {
                "APCA-API-KEY-ID":     self.settings.alpaca_api_key,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            }
            url = f"https://data.alpaca.markets/v2/stocks/{ticker}/quotes/latest"
            async with session.get(
                url, headers=headers,
                params={"feed": "iex"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    q   = data.get("quote") or {}
                    ask = float(q.get("ap") or 0)
                    bid = float(q.get("bp") or 0)
                    if ask and bid:
                        return round((ask + bid) / 2, 2)
                    return ask or bid
        except Exception as e:
            logger.warning(f"Equity quote error for {ticker}: {e}")
        return 0.0

    # ── Position sizing ──────────────────────────────────────────────────────

    def _size_options(self, equity: float, limit_price: float) -> tuple[int, float]:
        """Returns (contracts, risk_amount)."""
        max_risk = min(
            equity * self.settings.auto_trade_max_risk_pct,
            self.settings.auto_trade_max_risk_usd,
        )
        cost_per = limit_price * 100  # 1 contract = 100 shares
        if cost_per <= 0:
            return 0, 0.0
        qty = max(1, int(max_risk / cost_per))
        # If even 1 contract exceeds 1.5x max_risk, skip
        if cost_per > max_risk * 1.5:
            logger.debug(f"1 contract @ ${limit_price:.2f} = ${cost_per:.0f} > max ${max_risk:.0f}, skipping")
            return 0, 0.0
        risk = qty * cost_per
        return qty, round(risk, 2)

    def _size_equity(self, equity: float, price: float) -> tuple[int, float]:
        """Returns (shares, risk_amount)."""
        max_risk = min(
            equity * self.settings.auto_trade_max_risk_pct,
            self.settings.auto_trade_max_risk_usd,
        )
        if price <= 0:
            return 0, 0.0
        qty = max(1, int(max_risk / price))
        return qty, round(qty * price, 2)

    # ── Evaluation entry points ──────────────────────────────────────────────

    async def evaluate_signal(self, signal, account: dict):
        """Called after every scored signal. Routes qualifying signals to trade builders."""
        if not self.settings.auto_trade_enabled:
            return
        if signal is None:
            return
        if signal.score < self.settings.auto_trade_score_threshold:
            return

        sig_type = signal.type.value
        if sig_type in ("sweep", "golden_sweep", "options_flow"):
            await self._build_options_trade(signal, account)
        elif sig_type == "insider_buy":
            await self._build_equity_trade(
                signal.ticker, signal.side.value, signal.score, account,
                rationale=f"Insider open-market purchase | {signal.description[:80]}",
            )
        elif sig_type == "congress_trade" and signal.side.value == "bullish":
            await self._build_equity_trade(
                signal.ticker, "bullish", signal.score, account,
                rationale=f"Congressional buy | {signal.description[:80]}",
            )

    async def evaluate_pattern(self, pattern_name: str, ticker: str,
                               score: float, evidence: list, account: dict):
        """Called when a pattern fires. High-score qualifying patterns queue trades."""
        if not self.settings.auto_trade_enabled:
            return
        if pattern_name not in AUTO_TRADE_PATTERNS:
            return
        if score < self.settings.auto_trade_pattern_threshold:
            return

        # Check if pattern evidence includes options data → options trade
        options_ev = [e for e in evidence if any(
            kw in e.lower() for kw in ("sweep", "call", "put", "flow", "golden")
        )]

        if options_ev:
            await self._build_options_trade_from_db(ticker, score, evidence, account)
        else:
            await self._build_equity_trade(
                ticker, "bullish", score, account,
                rationale=f"Pattern: {pattern_name} | {'; '.join(evidence[:2])}",
            )

    # ── Trade builders ───────────────────────────────────────────────────────

    async def _build_options_trade(self, signal, account: dict):
        """Build an options trade from a signal with strike + expiry."""
        if not signal.strike or not signal.expiry:
            return

        dte = self._calc_dte(signal.expiry)
        if dte is None:
            return

        # DTE guard
        min_dte = self.settings.auto_trade_min_dte
        max_dte = self.settings.auto_trade_max_dte
        if dte < min_dte and signal.score < ZERO_DTE_MIN_SCORE:
            logger.debug(f"Auto-trade: {signal.ticker} DTE={dte} < {min_dte}, score {signal.score} < {ZERO_DTE_MIN_SCORE}")
            return
        if dte > max_dte:
            logger.debug(f"Auto-trade: {signal.ticker} DTE={dte} > {max_dte}, skipping LEAPS")
            return

        occ = self._occ_symbol(signal.ticker, signal.expiry, signal.option_type or "call", signal.strike)
        if not occ:
            return

        quote = await self._get_option_quote(occ)
        ask, bid = quote.get("ask", 0), quote.get("bid", 0)

        if ask <= 0 and bid <= 0:
            logger.warning(f"Auto-trade: no quote for {occ}")
            return

        # Limit price logic: pay ask but not if spread is > 2.5x bid
        if ask > 0 and bid > 0 and ask > bid * 2.5:
            limit_price = round(bid * 1.15, 2)
        elif ask > 0:
            limit_price = round(ask, 2)
        else:
            limit_price = round(bid * 1.10, 2)

        equity = float(account.get("equity", 100_000))
        qty, risk = self._size_options(equity, limit_price)
        if qty == 0:
            return

        await self._queue(
            ticker=signal.ticker,
            trade_type="option",
            symbol=occ,
            side=signal.side.value,
            option_type=signal.option_type or "call",
            strike=signal.strike,
            expiry=signal.expiry,
            dte=dte,
            qty=qty,
            limit_price=limit_price,
            risk_amount=risk,
            stop_pct=40.0,
            target_pct=80.0,
            score=signal.score,
            rationale=signal.description[:120],
        )

    async def _build_options_trade_from_db(self, ticker: str, score: float,
                                           evidence: list, account: dict):
        """For pattern-triggered trades: pull the best recent options signal from DB."""
        rows = await self._db.get_options_flow(
            ticker=ticker, min_premium=100_000, has_sweep=True, limit=1,
        )
        if not rows:
            rows = await self._db.get_options_flow(
                ticker=ticker, min_premium=50_000, limit=1,
            )
        if not rows:
            logger.debug(f"Auto-trade pattern: no recent options for {ticker}")
            return

        row = rows[0]

        # Build a minimal signal-like namespace
        class _S:
            pass
        s = _S()
        s.ticker      = ticker
        s.strike      = row.get("strike")
        s.expiry      = row.get("expiry")
        s.option_type = row.get("opt_type", "call")
        s.score       = score
        s.description = f"Pattern | {'; '.join(evidence[:2])}"

        class _Side:
            def __init__(self, v): self.value = v
        s.side = _Side("bullish" if s.option_type == "call" else "bearish")

        await self._build_options_trade(s, account)

    async def _build_equity_trade(self, ticker: str, side: str, score: float,
                                  account: dict, rationale: str = ""):
        """Build an equity (stock) trade suggestion."""
        price = await self._get_equity_price(ticker)
        if price <= 0:
            logger.warning(f"Auto-trade: no equity price for {ticker}")
            return

        limit_price = round(price * 1.005, 2)  # 0.5% above mid
        equity = float(account.get("equity", 100_000))
        qty, risk = self._size_equity(equity, limit_price)
        if qty == 0:
            return

        await self._queue(
            ticker=ticker,
            trade_type="equity",
            symbol=ticker,
            side=side,
            option_type=None,
            strike=None,
            expiry=None,
            dte=None,
            qty=qty,
            limit_price=limit_price,
            risk_amount=risk,
            stop_pct=5.0,
            target_pct=15.0,
            score=score,
            rationale=rationale[:120],
        )

    # ── Queue management ─────────────────────────────────────────────────────

    async def _queue(self, **kwargs):
        """Persist → in-memory → Telegram alert → schedule expiry."""
        ticker = kwargs.get("ticker", "")
        symbol = kwargs.get("symbol", "")

        # Deduplicate: don't queue same contract twice
        for pend in self._pending.values():
            if pend.symbol == symbol and pend.ticker == ticker:
                logger.debug(f"Auto-trade: already pending for {symbol}")
                return

        expires_at = datetime.utcnow() + timedelta(minutes=5)

        trade_id = await self._db.save_pending_trade(expires_at=expires_at, **kwargs)
        if not trade_id:
            return

        suggestion = TradeSuggestion(
            id=trade_id,
            ticker=ticker,
            trade_type=kwargs["trade_type"],
            symbol=symbol,
            side=kwargs["side"],
            option_type=kwargs.get("option_type"),
            strike=kwargs.get("strike"),
            expiry=kwargs.get("expiry"),
            dte=kwargs.get("dte"),
            qty=kwargs["qty"],
            limit_price=kwargs["limit_price"],
            risk_amount=kwargs["risk_amount"],
            stop_pct=kwargs.get("stop_pct", 40.0),
            target_pct=kwargs.get("target_pct", 80.0),
            score=kwargs.get("score", 0.0),
            rationale=kwargs.get("rationale", ""),
            expires_at=expires_at,
        )
        self._pending[trade_id] = suggestion

        # Fire Telegram alert
        if self._telegram and self._telegram.enabled:
            msg_id = await self._telegram.send_trade_alert({
                "id": trade_id, **kwargs,
                "expires_at": expires_at.isoformat(),
            })
            if msg_id:
                suggestion.telegram_msg_id = msg_id
                await self._db.update_pending_trade(trade_id, telegram_msg_id=msg_id)

        # Broadcast to frontend
        from api.websocket import manager
        await manager.broadcast({
            "type": "trade_queued",
            "data": {
                "id": trade_id,
                "ticker": ticker,
                "symbol": symbol,
                "trade_type": kwargs["trade_type"],
                "option_type": kwargs.get("option_type"),
                "strike": kwargs.get("strike"),
                "expiry": kwargs.get("expiry"),
                "dte": kwargs.get("dte"),
                "side": kwargs["side"],
                "qty": kwargs["qty"],
                "limit_price": kwargs["limit_price"],
                "risk_amount": kwargs["risk_amount"],
                "stop_pct": kwargs.get("stop_pct", 40),
                "target_pct": kwargs.get("target_pct", 80),
                "score": kwargs.get("score", 0),
                "rationale": kwargs.get("rationale", ""),
                "expires_at": expires_at.isoformat(),
                "status": "pending",
            }
        })

        asyncio.create_task(self._expire(trade_id))

        logger.info(
            f"TRADE QUEUED: {symbol} x{kwargs['qty']} @ ${kwargs['limit_price']:.2f} "
            f"| risk=${kwargs['risk_amount']:,.0f} | score={kwargs.get('score',0):.1f}"
        )

    async def _expire(self, trade_id: int):
        await asyncio.sleep(5 * 60)
        s = self._pending.pop(trade_id, None)
        if s is None:
            return  # already confirmed or skipped
        await self._db.update_pending_trade(trade_id, status="expired")
        if self._telegram and s.telegram_msg_id:
            await self._telegram.edit_message(
                s.telegram_msg_id,
                f"⏰ <b>EXPIRED</b> — {s.ticker} trade window closed\n"
                f"<i>Signal was not acted on within 5 minutes</i>",
            )
        logger.info(f"Trade expired: id={trade_id} {s.ticker}")

    # ── Confirm / Skip (called by Telegram callbacks + API) ──────────────────

    async def confirm_trade(self, trade_id: int, msg_id: int) -> dict:
        """User tapped EXECUTE. Place the Alpaca order."""
        s = self._pending.get(trade_id)
        if not s:
            if self._telegram and msg_id:
                await self._telegram.edit_message(
                    msg_id,
                    "⚠️ <b>Trade unavailable</b> — expired or already executed"
                )
            return {"error": "not_found"}

        # Compute bracket TP/SL prices from signal's target/stop percentages
        tp_price = round(s.limit_price * (1 + s.target_pct / 100), 2)
        sl_price = round(s.limit_price * (1 - s.stop_pct / 100), 2)

        try:
            result = self._trader.bracket_order(
                ticker=s.symbol,
                qty=s.qty,
                side="buy",
                limit_price=s.limit_price,
                take_profit_price=tp_price,
                stop_loss_price=sl_price,
            )
            # If bracket fails (e.g. options don't support it), fall back to plain limit
            if "error" in result:
                logger.warning(f"Bracket order failed, falling back to limit: {result['error']}")
                result = self._trader.limit_order(
                    ticker=s.symbol,
                    qty=s.qty,
                    side="buy",
                    limit_price=s.limit_price,
                )
        except Exception as e:
            logger.error(f"Order execution error: {e}")
            result = {"error": str(e)}

        if "error" in result:
            if self._telegram and msg_id:
                await self._telegram.edit_message(
                    msg_id,
                    f"❌ <b>ORDER FAILED</b>\n"
                    f"{s.ticker}: <code>{result['error']}</code>"
                )
            await self._db.update_pending_trade(trade_id, status="failed")
            return result

        # Success
        self._pending.pop(trade_id, None)
        order_id = result.get("id", "")
        await self._db.update_pending_trade(
            trade_id,
            status="confirmed",
            alpaca_order_id=order_id,
            executed_at=datetime.utcnow().isoformat(),
        )

        type_label = "CALL" if s.option_type == "call" else "PUT" if s.option_type == "put" else ""
        if self._telegram and msg_id:
            await self._telegram.edit_message(
                msg_id,
                f"✅ <b>ORDER PLACED (BRACKET)</b>\n"
                f"{s.ticker} {type_label}  {s.qty}x @ ${s.limit_price:.2f}\n"
                f"🎯 TP: ${tp_price:.2f} (+{s.target_pct:.0f}%)  |  🛑 SL: ${sl_price:.2f} (-{s.stop_pct:.0f}%)\n"
                f"Risk: ${s.risk_amount:,.0f}\n"
                f"Order ID: <code>{order_id[:12]}</code>"
            )

        logger.info(f"Trade executed: {s.symbol} x{s.qty} @ {s.limit_price} | order={order_id}")
        return result

    async def skip_trade(self, trade_id: int, msg_id: int):
        """User tapped SKIP."""
        s = self._pending.pop(trade_id, None)
        await self._db.update_pending_trade(trade_id, status="skipped")
        if self._telegram and msg_id:
            ticker = s.ticker if s else "Trade"
            await self._telegram.edit_message(
                msg_id,
                f"❌ <b>SKIPPED</b> — {ticker} passed"
            )
        logger.info(f"Trade skipped: id={trade_id}")

    async def get_pending(self) -> list[dict]:
        return await self._db.get_pending_trades(status="pending")

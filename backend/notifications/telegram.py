"""
Telegram bot notifier — sends trade alerts with inline Execute/Skip buttons
and polls for user responses via long-polling (no webhook server needed).
"""
import asyncio
import aiohttp
import logging
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)

TGAPI = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    def __init__(self, token: str, chat_id: int = 0):
        self.token = token
        self.chat_id = chat_id or None
        self.enabled = bool(token and not token.startswith("your_"))
        self._session: Optional[aiohttp.ClientSession] = None
        self._offset = 0
        self._polling = False
        self._on_confirm: Optional[Callable] = None
        self._on_skip: Optional[Callable] = None
        self._on_kalshi_confirm: Optional[Callable] = None
        self._on_kalshi_skip: Optional[Callable] = None
        self._on_kalshi_sell_all: Optional[Callable] = None
        self._on_kalshi_sell_half: Optional[Callable] = None
        self._on_kalshi_hold: Optional[Callable] = None

    def _url(self, method: str) -> str:
        return TGAPI.format(token=self.token, method=method)

    async def _session_get(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        self._polling = False
        if self._session and not self._session.closed:
            await self._session.close()

    async def _call(self, method: str, **kwargs) -> dict:
        if not self.enabled:
            return {}
        try:
            session = await self._session_get()
            async with session.post(self._url(method), json=kwargs, timeout=aiohttp.ClientTimeout(total=35)) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    logger.warning(f"Telegram {method} error: {data.get('description')}")
                return data
        except asyncio.TimeoutError:
            return {}
        except Exception as e:
            logger.error(f"Telegram call error ({method}): {e}")
            return {}

    # ── Setup ────────────────────────────────────────────────────────────────

    async def resolve_chat_id(self) -> bool:
        """Auto-detect chat_id from most recent message. Returns True if found."""
        if not self.enabled:
            return False
        data = await self._call("getUpdates", limit=20, timeout=5)
        updates = data.get("result", [])
        for upd in reversed(updates):
            msg = upd.get("message") or {}
            cb  = upd.get("callback_query", {})
            src_msg = msg or cb.get("message", {})
            if src_msg.get("chat", {}).get("id"):
                self.chat_id = src_msg["chat"]["id"]
                self._offset = max(self._offset, upd["update_id"] + 1)
                logger.info(f"Telegram chat_id resolved: {self.chat_id}")
                return True
        logger.warning(
            "Telegram: No chat found. Send /start to @stonktracker69_bot to activate alerts."
        )
        return False

    # ── Send ─────────────────────────────────────────────────────────────────

    async def send_message(self, text: str, reply_markup: dict = None) -> Optional[int]:
        """Send HTML message. Returns message_id."""
        if not self.enabled or not self.chat_id:
            return None
        kwargs = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            kwargs["reply_markup"] = reply_markup
        data = await self._call("sendMessage", **kwargs)
        return (data.get("result") or {}).get("message_id")

    async def edit_message(self, message_id: int, text: str):
        """Edit a previously sent message."""
        if not self.enabled or not self.chat_id or not message_id:
            return
        await self._call(
            "editMessageText",
            chat_id=self.chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
        )

    async def answer_callback(self, callback_query_id: str, text: str = ""):
        await self._call("answerCallbackQuery",
                         callback_query_id=callback_query_id, text=text, show_alert=False)

    async def send_trade_alert(self, trade: dict) -> Optional[int]:
        """
        Send a full trade suggestion card with Execute/Skip inline buttons.
        trade dict keys: id, ticker, trade_type, symbol, side, option_type,
                         strike, expiry, dte, qty, limit_price, risk_amount,
                         score, rationale, stop_pct, target_pct
        """
        if not self.enabled or not self.chat_id:
            return None

        score    = float(trade.get("score", 0))
        ticker   = trade.get("ticker", "")
        side     = trade.get("side", "bullish")
        otype    = (trade.get("option_type") or "").lower()
        risk     = float(trade.get("risk_amount", 0))
        limit_p  = float(trade.get("limit_price", 0))
        qty      = trade.get("qty", 1)
        symbol   = trade.get("symbol", ticker)

        side_emoji = "📈" if side == "bullish" else "📉"
        filled     = int(score)
        score_bar  = "█" * filled + "░" * (10 - filled)

        if trade.get("trade_type") == "option":
            type_label = "CALL" if otype == "call" else "PUT"
            contract_line = (
                f"Contract: <code>{symbol}</code>\n"
                f"Strike: <b>${trade.get('strike', 0):.0f}</b>"
                f"  Exp: <b>{trade.get('expiry', '')}</b>"
                f"  DTE: {trade.get('dte', '?')}d\n"
                f"Price:  <b>${limit_p:.2f}</b> x <b>{qty}</b> contracts\n"
                f"Risk:   <b>${risk:,.0f}</b>   "
                f"Stop: -{trade.get('stop_pct', 40):.0f}%  "
                f"Target: +{trade.get('target_pct', 80):.0f}%\n"
            )
        else:
            type_label = "EQUITY"
            contract_line = (
                f"Symbol: <b>{symbol}</b>\n"
                f"Qty:    <b>{qty} shares</b> @ ~${limit_p:.2f}\n"
                f"Risk:   <b>${risk:,.0f}</b>\n"
            )

        text = (
            f"<b>🚨 AUTO-TRADE CANDIDATE</b>\n"
            f"{'─' * 30}\n"
            f"{side_emoji} <b>{ticker}</b>  {type_label}\n"
            f"Score: <b>{score:.1f}/10</b>  <code>[{score_bar}]</code>\n"
            f"{'─' * 30}\n"
            f"{contract_line}"
            f"{'─' * 30}\n"
            f"<i>{trade.get('rationale', '')[:120]}</i>\n"
            f"⏳ <b>Expires in 5 min</b>"
        )

        keyboard = {
            "inline_keyboard": [[
                {"text": f"✅  EXECUTE  ${risk:,.0f}",
                 "callback_data": f"confirm_{trade['id']}"},
                {"text": "❌  SKIP",
                 "callback_data": f"skip_{trade['id']}"},
            ]]
        }
        return await self.send_message(text, reply_markup=keyboard)

    async def send_kalshi_alert(self, opp: dict, alert_id: int) -> Optional[int]:
        """
        Send a Kalshi opportunity card with Execute/Skip inline buttons.
        opp keys: ticker, title, side, market_price_cents, opportunity_type,
                  bet_contracts, bet_cost_usd, dte, volume, score, rationale
        """
        if not self.enabled or not self.chat_id:
            return None

        score    = float(opp.get("score", 0))
        filled   = int(score)
        score_bar = "█" * filled + "░" * (10 - filled)

        type_emoji = {
            "near_certain":     "🔒",
            "high_vol_extreme": "🔥",
            "mover":            "📈",
            "active":           "⚖️",
        }.get(opp.get("opportunity_type", ""), "🎰")

        side  = (opp.get("side") or "yes").upper()
        price = opp.get("market_price_cents", 0)
        cost  = float(opp.get("bet_cost_usd", 0))
        count = opp.get("bet_contracts", 1)

        text = (
            f"<b>{type_emoji} KALSHI OPPORTUNITY</b>\n"
            f"{'─' * 30}\n"
            f"<b>{opp.get('title', '')[:70]}</b>\n"
            f"Score: <b>{score:.1f}/10</b>  <code>[{score_bar}]</code>\n"
            f"{'─' * 30}\n"
            f"Side:  <b>{side}</b> @ <b>{price:.0f}¢</b>\n"
            f"Order: <b>{count}x contracts</b> = <b>${cost:.2f}</b>\n"
            f"DTE:   {opp.get('dte', 0):.0f}d  |  Vol: {int(opp.get('volume', 0)):,}\n"
            f"{'─' * 30}\n"
            f"<i>{opp.get('rationale', '')[:150]}</i>\n"
            f"⏳ <b>Offer expires in 10 min</b>"
        )

        keyboard = {
            "inline_keyboard": [[
                {"text": f"✅  EXECUTE  ${cost:.2f}",
                 "callback_data": f"kalshi_exec_{alert_id}"},
                {"text": "❌  SKIP",
                 "callback_data": f"kalshi_skip_{alert_id}"},
            ]]
        }
        return await self.send_message(text, reply_markup=keyboard)

    async def send_kalshi_position_alert(
        self,
        alert_id: int,
        ticker: str,
        title: str,
        side: str,
        contracts: int,
        entry_cents: float,
        current_cents: float,
    ) -> Optional[int]:
        """
        Send a position spike alert with Sell All / Sell Half / Hold buttons.
        Fires when a held position has moved significantly in our favor.
        """
        if not self.enabled or not self.chat_id:
            return None

        gain_x    = current_cents / entry_cents if entry_cents > 0 else 1.0
        entry_val = contracts * entry_cents / 100
        curr_val  = contracts * current_cents / 100
        gain_usd  = curr_val - entry_val
        half      = max(1, contracts // 2)

        if gain_x >= 10:
            emoji, urgency = "🚀", "MOON — consider taking profits!"
        elif gain_x >= 5:
            emoji, urgency = "📈", "Big move — strong sell signal"
        else:
            emoji, urgency = "💹", "Nice gain — trim or hold?"

        text = (
            f"<b>{emoji} KALSHI POSITION SPIKE</b>\n"
            f"{'─' * 30}\n"
            f"<b>{title[:65]}</b>\n"
            f"{'─' * 30}\n"
            f"Side:  <b>{side.upper()}</b> × {contracts} contracts\n"
            f"Entry: <b>{entry_cents:.0f}¢</b>  →  Now: <b>{current_cents:.0f}¢</b>\n"
            f"Gain:  <b>{gain_x:.1f}x</b>  (+${gain_usd:.2f})\n"
            f"Value: ${entry_val:.2f} → <b>${curr_val:.2f}</b>\n"
            f"{'─' * 30}\n"
            f"<i>{urgency}</i>"
        )

        keyboard = {
            "inline_keyboard": [[
                {"text": f"✅ SELL ALL ({contracts}x)",
                 "callback_data": f"ksell_all_{alert_id}"},
                {"text": f"✂️ SELL HALF ({half}x)",
                 "callback_data": f"ksell_half_{alert_id}"},
                {"text": "🚫 HOLD",
                 "callback_data": f"ksell_hold_{alert_id}"},
            ]]
        }
        return await self.send_message(text, reply_markup=keyboard)

    async def send_info(self, text: str):
        """Simple informational message (no buttons)."""
        await self.send_message(text)

    # ── Long-poll loop ───────────────────────────────────────────────────────

    async def start_polling(
        self,
        on_confirm: Callable[[int, int], Awaitable],
        on_skip: Callable[[int, int], Awaitable],
        on_kalshi_confirm: Optional[Callable[[int, int], Awaitable]] = None,
        on_kalshi_skip: Optional[Callable[[int, int], Awaitable]] = None,
        on_kalshi_sell_all: Optional[Callable[[int, int], Awaitable]] = None,
        on_kalshi_sell_half: Optional[Callable[[int, int], Awaitable]] = None,
        on_kalshi_hold: Optional[Callable[[int, int], Awaitable]] = None,
    ):
        """Start background task to poll for button taps."""
        self._on_confirm = on_confirm
        self._on_skip = on_skip
        self._on_kalshi_confirm = on_kalshi_confirm
        self._on_kalshi_skip = on_kalshi_skip
        self._on_kalshi_sell_all = on_kalshi_sell_all
        self._on_kalshi_sell_half = on_kalshi_sell_half
        self._on_kalshi_hold = on_kalshi_hold
        self._polling = True
        asyncio.create_task(self._poll_loop())
        logger.info("Telegram polling started")

    async def _poll_loop(self):
        while self._polling:
            try:
                data = await self._call(
                    "getUpdates",
                    offset=self._offset,
                    timeout=30,
                    allowed_updates=["callback_query", "message"],
                )
                for upd in data.get("result", []):
                    self._offset = upd["update_id"] + 1
                    await self._handle_update(upd)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Telegram poll error: {e}")
                await asyncio.sleep(5)

    async def _handle_update(self, update: dict):
        # ── Button tap (callback_query) ──────────────────────────────────
        cb = update.get("callback_query")
        if cb:
            cb_id  = cb["id"]
            data   = cb.get("data", "")
            msg_id = cb.get("message", {}).get("message_id")

            # Update chat_id if not set
            if not self.chat_id:
                self.chat_id = cb["message"]["chat"]["id"]

            if data.startswith("kalshi_exec_"):
                alert_id = int(data.split("_", 2)[2])
                await self.answer_callback(cb_id, "Placing Kalshi order...")
                if self._on_kalshi_confirm:
                    await self._on_kalshi_confirm(alert_id, msg_id)

            elif data.startswith("kalshi_skip_"):
                alert_id = int(data.split("_", 2)[2])
                await self.answer_callback(cb_id, "Skipped")
                if self._on_kalshi_skip:
                    await self._on_kalshi_skip(alert_id, msg_id)

            elif data.startswith("ksell_all_"):
                alert_id = int(data.split("_", 2)[2])
                await self.answer_callback(cb_id, "Selling all contracts...")
                if self._on_kalshi_sell_all:
                    await self._on_kalshi_sell_all(alert_id, msg_id)

            elif data.startswith("ksell_half_"):
                alert_id = int(data.split("_", 2)[2])
                await self.answer_callback(cb_id, "Selling half...")
                if self._on_kalshi_sell_half:
                    await self._on_kalshi_sell_half(alert_id, msg_id)

            elif data.startswith("ksell_hold_"):
                alert_id = int(data.split("_", 2)[2])
                await self.answer_callback(cb_id, "Holding position 💎")
                if self._on_kalshi_hold:
                    await self._on_kalshi_hold(alert_id, msg_id)

            elif data.startswith("confirm_"):
                trade_id = int(data.split("_", 1)[1])
                await self.answer_callback(cb_id, "Executing...")
                if self._on_confirm:
                    await self._on_confirm(trade_id, msg_id)

            elif data.startswith("skip_"):
                trade_id = int(data.split("_", 1)[1])
                await self.answer_callback(cb_id, "Skipped")
                if self._on_skip:
                    await self._on_skip(trade_id, msg_id)
            return

        # ── Regular message ──────────────────────────────────────────────
        msg = update.get("message", {})
        if msg:
            if not self.chat_id:
                self.chat_id = msg["chat"]["id"]
                logger.info(f"Telegram chat_id set: {self.chat_id}")

            text = msg.get("text", "")
            if text == "/start":
                await self.send_message(
                    "✅ <b>StonkMonitor connected!</b>\n\n"
                    "You'll receive trade alerts here with one-tap execution.\n"
                    "Send /status to check the connection."
                )
            elif text == "/status":
                await self.send_message(
                    "🟢 <b>StonkMonitor is live</b>\n"
                    "Monitoring: UW Flow, Dark Pool, Insider, Congress\n"
                    "Auto-trade: enabled"
                )

"""
StonkMonitor — Main FastAPI Application
Streams Unusual Whales feed, scores signals, fires notifications,
and broadcasts everything to the Next.js frontend via WebSocket.
"""
import asyncio
import logging
import json
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from db import Database
from feeds.unusual_whales import UnusualWhalesClient
from feeds.alpaca_feed import AlpacaFeed
from trading.alpaca_trader import AlpacaTrader
from signals.engine import SignalEngine
from signals.patterns import PatternEngine
from signals.auto_trade import AutoTradeEngine
from signals.kalshi_scanner import KalshiScanner
from feeds.kalshi import KalshiClient
from notifications.discord import DiscordNotifier
from notifications.pushover import PushoverNotifier
from notifications.telegram import TelegramNotifier
from api.routes import router
from api.websocket import manager

# ------------------------------------------------------------------ #
#  Logging Setup                                                       #
# ------------------------------------------------------------------ #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(
        stream=open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False)
    )],
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Global singletons (used by routes via import)                      #
# ------------------------------------------------------------------ #
settings    = get_settings()
db          = Database()
uw_client   = UnusualWhalesClient(settings.unusual_whales_api_key)
feed        = AlpacaFeed(settings.alpaca_api_key, settings.alpaca_secret_key)
trader      = AlpacaTrader(
    settings.alpaca_api_key,
    settings.alpaca_secret_key,
    paper=settings.alpaca_paper,
)
engine          = SignalEngine(settings)
pattern_engine  = PatternEngine(notify_threshold=8.0)
discord     = DiscordNotifier(settings.discord_webhook_url)
pushover    = PushoverNotifier(settings.pushover_api_token, settings.pushover_user_key)
telegram    = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
auto_trade  = AutoTradeEngine(settings)

# Kalshi — only init if credentials set
kalshi_client  = None
kalshi_scanner = KalshiScanner(settings)
if settings.kalshi_key_id and not settings.kalshi_key_id.startswith("your_"):
    kalshi_client = KalshiClient(
        key_id=settings.kalshi_key_id,
        private_key_pem=settings.kalshi_private_key,
        demo=settings.kalshi_demo,
    )

# In-memory signal store (last 500 signals)
signal_store: list[dict] = []

# Suppress notifications during initial backfill on startup
_startup_complete = False

# ── Kalshi pending orders ────────────────────────────────────────────────────
_kalshi_pending: dict[int, dict] = {}   # alert_id → order params
_kalshi_alerted: dict[str, float] = {}  # ticker → epoch when last buy-alerted
_kalshi_alert_counter = 0
KALSHI_ALERT_COOLDOWN = 3600  # don't re-alert same ticker within 1 hour

# ── Kalshi position tracking (for sell alerts) ────────────────────────────────
# Populated when we confirm a buy; monitored for exit signals
_kalshi_positions: dict[str, dict] = {}  # ticker → {entry_cents, contracts, side, sell_alerted_at}
_kalshi_sell_pending: dict[int, dict] = {}  # alert_id → sell params
KALSHI_SELL_ALERT_COOLDOWN = 1800  # 30 min between sell alerts on same position
KALSHI_SELL_THRESHOLDS = [3.0, 5.0, 10.0]  # alert at 3x, 5x, 10x gain

# ------------------------------------------------------------------ #
#  Signal Pipeline                                                     #
# ------------------------------------------------------------------ #
async def handle_signal(signal):
    """Score → store → broadcast → notify."""
    if signal is None:
        return

    sig_dict = signal.to_dict()

    # Store (ring buffer)
    signal_store.append(sig_dict)
    if len(signal_store) > 500:
        signal_store.pop(0)

    # Broadcast to all frontend WS clients
    await manager.broadcast_signal(sig_dict)

    # Persist to DB if score >= 7
    await db.save_signal(signal, min_score=7.0)

    # Only notify + auto-trade after startup backfill is done
    if _startup_complete:
        await discord.send_signal(signal, score_threshold=settings.sweep_score_threshold)
        await pushover.send_signal(signal, score_threshold=settings.sweep_score_threshold)
        # Auto-trade evaluation (non-blocking — don't let it crash the pipeline)
        try:
            account = trader.get_account()
            await auto_trade.evaluate_signal(signal, account)
        except Exception as e:
            logger.warning(f"Auto-trade eval error: {e}")

    logger.info(f"Signal: {signal.title} | Score {signal.score:.1f}")


async def process_uw_event(raw: dict):
    """Dispatch a raw UW WebSocket message to the signal engine."""
    try:
        channel = raw.get("channel") or raw.get("type") or ""
        data = raw.get("data") or raw

        # Broadcast raw feed to UI regardless of score
        await manager.broadcast_feed(channel, data)

        # Persist every event to its dedicated table
        if channel == "options-flow":
            await db.save_options_flow(data)
        elif channel == "darkpool":
            await db.save_dark_pool(data)
        elif channel == "insider-trades":
            await db.save_insider_trade(data)
        elif channel == "congress-trades":
            await db.save_congress_trade(data)

        # Score it → signal pipeline
        signal = engine.process_event(channel, data)
        if signal:
            await handle_signal(signal)

        # Run pattern engine — returns list of fired PatternResult
        ticker = data.get("ticker", "")
        fired_patterns = await pattern_engine.evaluate(ticker, channel, db)

        # Auto-trade evaluation for any high-score pattern hits
        if _startup_complete and fired_patterns:
            try:
                account = trader.get_account()
                for pat in fired_patterns:
                    await auto_trade.evaluate_pattern(
                        pat.pattern_name, pat.ticker,
                        pat.score, pat.evidence, account,
                    )
            except Exception as e:
                logger.warning(f"Auto-trade pattern eval error: {e}")

    except Exception as e:
        logger.warning(f"Error processing UW event: {e}")


# ------------------------------------------------------------------ #
#  Background Tasks                                                    #
# ------------------------------------------------------------------ #
async def start_uw_stream():
    """Background task: keep UW WebSocket alive forever."""
    logger.info("Starting Unusual Whales live stream...")
    await uw_client.stream_flow(
        on_event=process_uw_event,
        channels=["options-flow", "darkpool", "insider-trades", "congress-trades"],
    )


async def kalshi_scan_loop():
    """Periodically scan Kalshi for edge opportunities and broadcast to frontend."""
    import time as _time
    global _kalshi_alert_counter

    await asyncio.sleep(15)  # wait for startup
    while True:
        try:
            balance_data = await kalshi_client.get_balance()
            balance_usd  = balance_data.get("balance", 0) / 100  # cents → dollars
            markets      = await kalshi_client.get_markets()  # paginates all categories
            opps         = kalshi_scanner.scan(markets, balance_usd)

            if opps:
                top = opps[:10]
                await manager.broadcast({
                    "type": "kalshi_scan",
                    "data": {
                        "balance_usd": balance_usd,
                        "markets_scanned": len(markets),
                        "opportunities": [o.to_dict() for o in top],
                        "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
                    }
                })

                # Telegram alert with Execute/Skip buttons (score >= 7, not recently alerted)
                if _startup_complete:
                    now = _time.time()
                    for opp in top:
                        if opp.score() < 7.0:
                            break  # sorted by score, rest will be lower
                        last = _kalshi_alerted.get(opp.ticker, 0)
                        if now - last < KALSHI_ALERT_COOLDOWN:
                            continue  # skip — already alerted recently

                        _kalshi_alert_counter += 1
                        alert_id = _kalshi_alert_counter
                        opp_dict = opp.to_dict()
                        # Store params needed to place the order
                        _kalshi_pending[alert_id] = {
                            "ticker":    opp.ticker,
                            "side":      opp.side if opp.side != "watch" else "yes",
                            "count":     opp.bet_contracts,
                            "price_cents": round(opp.market_price * 100),
                            "title":     opp.title,
                            "opp_dict":  opp_dict,
                            "expires":   now + 600,  # 10-minute window
                        }
                        _kalshi_alerted[opp.ticker] = now

                        msg_id = await telegram.send_kalshi_alert(opp_dict, alert_id)
                        logger.info(
                            f"KALSHI ALERT #{alert_id}: {opp.ticker} {opp.side.upper()} "
                            f"@ {opp.market_price*100:.0f}¢ score={opp.score():.1f}"
                        )
                        break  # one alert per scan cycle to avoid spam

        except Exception as e:
            logger.error(f"Kalshi scan loop error: {e}")

        await asyncio.sleep(settings.kalshi_scan_interval)


async def confirm_kalshi(alert_id: int, msg_id: int):
    """User tapped Execute on a Kalshi alert — place the order."""
    import time as _time
    pending = _kalshi_pending.pop(alert_id, None)
    if not pending:
        await telegram.edit_message(msg_id, "⚠️ Order expired or already processed.")
        return

    if _time.time() > pending["expires"]:
        await telegram.edit_message(msg_id, "⏰ Order expired (10-min window passed).")
        return

    try:
        result = await kalshi_client.place_order(
            ticker=pending["ticker"],
            side=pending["side"],
            action="buy",
            count=pending["count"],
            order_type="limit",
            price=pending["price_cents"],
        )
        order_id = (result.get("order") or {}).get("order_id", "?")
        status   = (result.get("order") or {}).get("status", "submitted")
        await telegram.edit_message(
            msg_id,
            f"✅ <b>KALSHI ORDER PLACED</b>\n"
            f"Market: {pending['title'][:60]}\n"
            f"Side: {pending['side'].upper()} × {pending['count']} @ {pending['price_cents']}¢\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"Status: <b>{status}</b>"
        )
        logger.info(f"Kalshi order placed: {pending['ticker']} {pending['side']} "
                    f"×{pending['count']} @ {pending['price_cents']}¢ → {order_id}")

        # Register position for sell monitoring
        ticker = pending["ticker"]
        existing = _kalshi_positions.get(ticker)
        if existing:
            # Average down/up with new contracts
            total = existing["contracts"] + pending["count"]
            avg   = (existing["entry_cents"] * existing["contracts"] +
                     pending["price_cents"] * pending["count"]) / total
            existing["contracts"]  = total
            existing["entry_cents"] = avg
        else:
            _kalshi_positions[ticker] = {
                "ticker":         ticker,
                "title":          pending["title"],
                "side":           pending["side"],
                "contracts":      pending["count"],
                "entry_cents":    pending["price_cents"],
                "sell_alerted_at": 0.0,
                "alerted_threshold": 0.0,  # last threshold we already fired
            }
        logger.info(f"Position monitor registered: {ticker}")

    except Exception as e:
        logger.error(f"Kalshi order failed: {e}")
        await telegram.edit_message(msg_id, f"❌ Order failed: {e}")


async def skip_kalshi(alert_id: int, msg_id: int):
    """User tapped Skip on a Kalshi alert."""
    pending = _kalshi_pending.pop(alert_id, None)
    title = (pending or {}).get("title", "")[:50]
    await telegram.edit_message(msg_id, f"⏭ Skipped: {title}")


# ── Kalshi sell handlers ──────────────────────────────────────────────────────

async def _execute_kalshi_sell(alert_id: int, msg_id: int, fraction: float):
    """Place a sell order for fraction (1.0 = all, 0.5 = half) of the position."""
    pending = _kalshi_sell_pending.pop(alert_id, None)
    if not pending:
        await telegram.edit_message(msg_id, "⚠️ Position alert expired or already acted on.")
        return

    ticker    = pending["ticker"]
    side      = pending["side"]
    contracts = max(1, int(pending["contracts"] * fraction))
    price     = pending["current_cents"]

    try:
        result = await kalshi_client.place_order(
            ticker=ticker,
            side=side,
            action="sell",
            count=contracts,
            order_type="limit",
            price=round(price),
        )
        order_id = (result.get("order") or {}).get("order_id", "?")
        status   = (result.get("order") or {}).get("status", "submitted")

        # Update tracked position
        pos = _kalshi_positions.get(ticker)
        if pos:
            pos["contracts"] = max(0, pos["contracts"] - contracts)
            if pos["contracts"] == 0:
                _kalshi_positions.pop(ticker, None)

        sold_val = contracts * price / 100
        await telegram.edit_message(
            msg_id,
            f"{'✅' if fraction == 1.0 else '✂️'} <b>KALSHI SELL PLACED</b>\n"
            f"Market: {pending['title'][:55]}\n"
            f"Sold: <b>{contracts}x {side.upper()}</b> @ {price:.0f}¢\n"
            f"Proceeds: <b>${sold_val:.2f}</b>\n"
            f"Order ID: <code>{order_id}</code>  Status: <b>{status}</b>"
        )
        logger.info(f"Kalshi sell: {ticker} {side} ×{contracts} @ {price:.0f}¢ → {order_id}")
    except Exception as e:
        logger.error(f"Kalshi sell failed: {e}")
        await telegram.edit_message(msg_id, f"❌ Sell failed: {e}")


async def kalshi_sell_all(alert_id: int, msg_id: int):
    await _execute_kalshi_sell(alert_id, msg_id, fraction=1.0)


async def kalshi_sell_half(alert_id: int, msg_id: int):
    await _execute_kalshi_sell(alert_id, msg_id, fraction=0.5)


async def kalshi_hold(alert_id: int, msg_id: int):
    pending = _kalshi_sell_pending.pop(alert_id, None)
    title = (pending or {}).get("title", "")[:50]
    await telegram.edit_message(msg_id, f"💎 Holding: {title}")


# ── Kalshi position monitor ───────────────────────────────────────────────────

async def kalshi_position_monitor():
    """Every 2 min: check tracked positions for spike exits."""
    import time as _time
    await asyncio.sleep(60)  # let things settle
    while True:
        try:
            if not _kalshi_positions:
                await asyncio.sleep(120)
                continue

            now = _time.time()
            for ticker, pos in list(_kalshi_positions.items()):
                if pos["contracts"] <= 0:
                    _kalshi_positions.pop(ticker, None)
                    continue

                # Fetch live market price
                market = await kalshi_client.get_market(ticker)
                if not market:
                    continue

                side = pos["side"]
                if side == "yes":
                    # We hold YES; sell at YES bid (what buyers will pay us)
                    current = float(market.get("yes_bid_dollars") or 0) * 100
                else:
                    current = float(market.get("no_bid_dollars") or 0) * 100

                if current <= 0:
                    continue

                entry     = pos["entry_cents"]
                gain_x    = current / entry if entry > 0 else 1.0
                last_alert = pos.get("sell_alerted_at", 0)
                last_thresh = pos.get("alerted_threshold", 0.0)

                # Find the highest threshold we've crossed that we haven't alerted for
                triggered = None
                for thresh in KALSHI_SELL_THRESHOLDS:
                    if gain_x >= thresh and thresh > last_thresh:
                        triggered = thresh

                if triggered and (now - last_alert) > KALSHI_SELL_ALERT_COOLDOWN:
                    _kalshi_alert_counter += 1
                    alert_id = _kalshi_alert_counter
                    _kalshi_sell_pending[alert_id] = {
                        "ticker":        ticker,
                        "title":         pos["title"],
                        "side":          side,
                        "contracts":     pos["contracts"],
                        "entry_cents":   entry,
                        "current_cents": current,
                    }
                    pos["sell_alerted_at"]   = now
                    pos["alerted_threshold"] = triggered

                    await telegram.send_kalshi_position_alert(
                        alert_id=alert_id,
                        ticker=ticker,
                        title=pos["title"],
                        side=side,
                        contracts=pos["contracts"],
                        entry_cents=entry,
                        current_cents=current,
                    )
                    logger.info(
                        f"Sell alert #{alert_id}: {ticker} {side} "
                        f"entry={entry:.1f}¢ now={current:.1f}¢ ({gain_x:.1f}x)"
                    )

        except Exception as e:
            logger.error(f"Position monitor error: {e}")

        await asyncio.sleep(120)  # check every 2 minutes


async def iv_scanner_loop():
    """Poll IV rank for watchlist tickers every 5 minutes."""
    from api.routes import _watchlist
    await asyncio.sleep(30)  # give server time to start
    while True:
        for ticker in list(_watchlist):
            try:
                iv_data = await uw_client.get_iv_rank(ticker)
                iv_rank = float(iv_data.get("iv_rank", 0) or 0)
                iv_pct  = float(iv_data.get("iv_percentile", 0) or 0)
                signal  = engine.score_iv_rank(ticker, iv_rank, iv_pct)
                if signal:
                    await handle_signal(signal)
            except Exception as e:
                logger.warning(f"IV scanner error for {ticker}: {e}")
            await asyncio.sleep(1)
        await asyncio.sleep(300)  # 5 min between full scans


# ------------------------------------------------------------------ #
#  App Lifecycle                                                       #
# ------------------------------------------------------------------ #
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()

    logger.info("=" * 60)
    logger.info("  StonkMonitor starting up")
    logger.info(f"  Mode: {'PAPER' if settings.alpaca_paper else 'LIVE'} trading")
    logger.info(f"  DB:       {db.path}")
    logger.info(f"  Discord:  {'enabled' if discord.enabled else 'disabled'}")
    logger.info(f"  Pushover: {'enabled' if pushover.enabled else 'disabled'}")
    logger.info("=" * 60)

    # Start background tasks
    uw_task = asyncio.create_task(start_uw_stream())
    iv_task = asyncio.create_task(iv_scanner_loop())

    # Kalshi — login + start scan loop if configured
    kalshi_task = None
    kalshi_monitor_task = None
    if kalshi_client:
        ok = await kalshi_client.ping()
        if ok:
            kalshi_task = asyncio.create_task(kalshi_scan_loop())
            kalshi_monitor_task = asyncio.create_task(kalshi_position_monitor())
            logger.info("Kalshi scanner + position monitor started")

    # Give the first poll cycle time to backfill, then open notifications
    async def enable_notifications():
        global _startup_complete
        await asyncio.sleep(20)  # wait for first full poll to finish
        _startup_complete = True
        logger.info("Startup backfill complete — notifications now active")

    asyncio.create_task(enable_notifications())
    pattern_engine.set_notifiers(discord, pushover)

    # Wire auto-trade dependencies
    auto_trade.set_dependencies(telegram, db, trader)

    # Resolve Telegram chat_id (user must have sent /start to the bot)
    if telegram.enabled:
        await telegram.resolve_chat_id()
        await telegram.start_polling(
            on_confirm=auto_trade.confirm_trade,
            on_skip=auto_trade.skip_trade,
            on_kalshi_confirm=confirm_kalshi,
            on_kalshi_skip=skip_kalshi,
            on_kalshi_sell_all=kalshi_sell_all,
            on_kalshi_sell_half=kalshi_sell_half,
            on_kalshi_hold=kalshi_hold,
        )
        logger.info(f"Telegram: {'chat_id=' + str(telegram.chat_id) if telegram.chat_id else 'waiting for /start'}")

    yield  # app runs here

    uw_task.cancel()
    iv_task.cancel()
    if kalshi_task:
        kalshi_task.cancel()
    if kalshi_monitor_task:
        kalshi_monitor_task.cancel()
    await uw_client.close()
    if kalshi_client:
        await kalshi_client.close()
    await telegram.close()
    await auto_trade.close()
    await db.close()
    logger.info("StonkMonitor shutting down")


app = FastAPI(
    title="StonkMonitor API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")


# ------------------------------------------------------------------ #
#  WebSocket Endpoint                                                  #
# ------------------------------------------------------------------ #
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    # Send last 50 signals on connect so UI catches up
    if signal_store:
        for sig in signal_store[-50:]:
            await ws.send_text(json.dumps({"type": "signal", "data": sig}))
    try:
        while True:
            # Keep connection alive, receive any client messages
            data = await ws.receive_text()
            # Handle client commands (e.g. subscribe to ticker)
            try:
                msg = json.loads(data)
                if msg.get("action") == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
            except Exception:
                pass
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ------------------------------------------------------------------ #
#  Health Check                                                        #
# ------------------------------------------------------------------ #
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "paper_mode": settings.alpaca_paper,
        "signals_stored": len(signal_store),
        "ws_clients": len(manager.active),
    }


@app.get("/signals")
async def get_signals(limit: int = 100):
    """Get recent scored signals."""
    return {"signals": signal_store[-limit:]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.backend_host,
        port=settings.backend_port,
        reload=False,
        log_level="info",
    )

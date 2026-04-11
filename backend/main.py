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
if settings.kalshi_email and not settings.kalshi_email.startswith("your_"):
    kalshi_client = KalshiClient(
        settings.kalshi_email,
        settings.kalshi_password,
        demo=settings.kalshi_demo,
    )

# In-memory signal store (last 500 signals)
signal_store: list[dict] = []

# Suppress notifications during initial backfill on startup
_startup_complete = False

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
    await asyncio.sleep(15)  # wait for startup
    while True:
        try:
            balance_data = await kalshi_client.get_balance()
            balance_usd  = balance_data.get("balance", 0) / 100
            markets      = await kalshi_client.get_markets(limit=200)
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

                # Telegram alert for top opportunity if score >= 7
                best = top[0]
                if best.score() >= 7.0 and _startup_complete:
                    await telegram.send_info(
                        f"🎰 <b>KALSHI EDGE</b>\n"
                        f"{'─'*28}\n"
                        f"<b>{best.title[:60]}</b>\n"
                        f"Side: <b>{best.side.upper()}</b> @ {best.market_price*100:.0f}¢\n"
                        f"True prob: <b>{best.true_prob*100:.1f}%</b>  Edge: <b>{best.edge*100:.1f}%</b>\n"
                        f"Bet: <b>{best.bet_contracts}x</b> contracts = <b>${best.bet_cost_usd:.2f}</b>\n"
                        f"Confidence: <b>{best.confidence.upper()}</b>  DTE: {best.dte:.1f}d\n"
                        f"Score: <b>{best.score():.1f}/10</b>\n"
                        f"<i>Use dashboard Kalshi tab to execute</i>"
                    )
                    logger.info(
                        f"KALSHI: {best.ticker} {best.side.upper()} @ {best.market_price*100:.0f}¢ "
                        f"edge={best.edge*100:.1f}% score={best.score()}"
                    )

        except Exception as e:
            logger.error(f"Kalshi scan loop error: {e}")

        await asyncio.sleep(settings.kalshi_scan_interval)


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
    if kalshi_client:
        ok = await kalshi_client.login()
        if ok:
            kalshi_task = asyncio.create_task(kalshi_scan_loop())
            logger.info("Kalshi scanner started")

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
        )
        logger.info(f"Telegram: {'chat_id=' + str(telegram.chat_id) if telegram.chat_id else 'waiting for /start'}")

    yield  # app runs here

    uw_task.cancel()
    iv_task.cancel()
    if kalshi_task:
        kalshi_task.cancel()
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

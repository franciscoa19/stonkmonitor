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
from signals.kalshi_arb import KalshiArbScanner
from signals.kalshi_poly_arb import KalshiPolyArbScanner
from feeds.kalshi import KalshiClient
from feeds.dome import DomeClient
from feeds.polymarket import PolymarketClobClient
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
kalshi_client      = None
kalshi_scanner     = KalshiScanner(settings)
kalshi_arb_scanner = KalshiArbScanner(settings)
if settings.kalshi_key_id and not settings.kalshi_key_id.startswith("your_"):
    kalshi_client = KalshiClient(
        key_id=settings.kalshi_key_id,
        private_key_pem=settings.kalshi_private_key,
        demo=settings.kalshi_demo,
    )

# Dome + Polymarket — cross-platform prediction market arb
dome_client           = DomeClient(settings.dome_api_key, settings.dome_base_url)
polymarket_client     = PolymarketClobClient(settings.polymarket_clob_url)
cross_arb_scanner     = KalshiPolyArbScanner(
    dome_client, polymarket_client, min_edge=settings.cross_arb_min_edge
)

# In-memory signal store (last 500 signals)
signal_store: list[dict] = []

# Suppress notifications during initial backfill on startup
_startup_complete = False

# ── Kalshi pending orders ────────────────────────────────────────────────────
_kalshi_pending: dict[int, dict] = {}   # alert_id → order params
_kalshi_alert_counter = 0

# ── Kalshi alert suppression ─────────────────────────────────────────────────
# Tracks every market we've ever alerted on so we don't spam the same plays.
# Re-alert only when something *meaningfully* changes.
#
# _kalshi_seen[ticker] = {
#   "price_cents":  float   — price at time of last alert
#   "alerted_at":   float   — epoch of last alert
#   "outcome":      str     — "pending" | "executed" | "skipped" | "expired"
# }
#
# Re-alert rules:
#   - "executed"  → never re-alert for a buy (position monitor handles exits)
#   - "skipped" / "expired" → only re-alert if price moved ≥ SIGNIFICANT_MOVE_CENTS
#                             AND at least MIN_RESUPPRESS_HOURS have passed
#   - "pending"   → alert already live, don't send another
_kalshi_seen: dict[str, dict] = {}

SIGNIFICANT_MOVE_CENTS  = 10.0   # abs price change that warrants a new alert
SIGNIFICANT_MOVE_PCT    = 0.50   # OR 50% relative change (1¢→1.5¢ is big)
MIN_RESUPPRESS_HOURS    = 6      # even with a big move, wait at least 6h

# ── Kalshi position tracking (for sell alerts) ────────────────────────────────
# Populated when we confirm a buy; monitored for exit signals
_kalshi_positions: dict[str, dict] = {}  # ticker → {entry_cents, contracts, side, sell_alerted_at}
_kalshi_sell_pending: dict[int, dict] = {}  # alert_id → sell params
KALSHI_SELL_ALERT_COOLDOWN = 1800  # 30 min between sell alerts on same position
KALSHI_SELL_THRESHOLDS = [3.0, 5.0, 10.0]  # alert at 3x, 5x, 10x gain

# ------------------------------------------------------------------ #
#  Signal Pipeline                                                     #
# ------------------------------------------------------------------ #

# Slow-moving feeds (congress/insider) file days or weeks after the
# actual trade. Don't send notifications for events older than this.
_STALE_HOURS = 48
_STALE_SIGNAL_TYPES = {"congress_trade", "insider_buy", "insider_sell"}

def _is_stale(signal) -> bool:
    """
    Returns True if this is a congress/insider signal whose underlying
    transaction date is older than _STALE_HOURS. These get stored and
    shown in the dashboard but don't trigger Telegram/Discord/auto-trade.
    """
    if signal.type.value not in _STALE_SIGNAL_TYPES:
        return False
    try:
        from datetime import datetime, timezone, timedelta
        raw = signal.raw or {}
        # Try several date fields UW uses across feeds
        date_str = (
            raw.get("transaction_date") or
            raw.get("filed_at_date") or
            raw.get("date") or
            raw.get("created_at") or
            ""
        )
        if not date_str:
            return False
        # Parse — handles both date-only "2024-01-15" and ISO datetimes
        date_str = date_str.strip()[:10]  # take YYYY-MM-DD portion
        txn_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - txn_date).total_seconds() / 3600
        if age_hours > _STALE_HOURS:
            logger.debug(f"Stale {signal.type.value} suppressed: {signal.ticker} "
                         f"({date_str}, {age_hours:.0f}h old)")
            return True
    except Exception:
        pass
    return False


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
    if _startup_complete and not _is_stale(signal):
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
    # Pre-load seen IDs from DB so restarts don't replay old congress/insider events
    seed_ids = await db.get_seen_ids()
    logger.info(f"Starting Unusual Whales live stream (seeded {len(seed_ids)} seen IDs)...")
    await uw_client.stream_flow(
        on_event=process_uw_event,
        channels=["options-flow", "darkpool", "insider-trades", "congress-trades"],
        seed_seen_ids=seed_ids,
    )


async def kalshi_scan_loop():
    """Periodically scan Kalshi for edge opportunities and broadcast to frontend."""
    import time as _time
    global _kalshi_alert_counter

    await asyncio.sleep(15)  # wait for startup
    while True:
        try:
            # Mark any pending alerts whose 10-min window has passed as expired
            now_pre = _time.time()
            for alert_id, p in list(_kalshi_pending.items()):
                if now_pre > p["expires"]:
                    _kalshi_pending.pop(alert_id, None)
                    seen = _kalshi_seen.get(p["ticker"])
                    if seen and seen["outcome"] == "pending":
                        seen["outcome"] = "expired"
                        logger.debug(f"KALSHI alert #{alert_id} expired: {p['ticker']}")

            balance_data = await kalshi_client.get_balance()
            balance_usd  = balance_data.get("balance", 0) / 100  # cents → dollars
            markets      = await kalshi_client.get_markets()  # paginates all categories
            opps         = kalshi_scanner.scan(markets, balance_usd)

            # ── Arb scans (free, run every cycle) ────────────────────────
            try:
                arb_opps = kalshi_arb_scanner.scan(markets)
                if arb_opps:
                    top_arb = arb_opps[:10]
                    logger.info(
                        f"Kalshi arb: {len(arb_opps)} monotonicity/sum violations "
                        f"(top edge {top_arb[0].edge*100:.1f}¢)"
                    )
                    await manager.broadcast({
                        "type": "kalshi_arb",
                        "data": {
                            "opportunities": [o.to_dict() for o in top_arb],
                            "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
                        },
                    })
                    # Telegram alert only when the best edge is meaningful
                    if _startup_complete and top_arb[0].edge >= 0.03:
                        best = top_arb[0]
                        await telegram.send_info(
                            f"<b>⚖️ KALSHI ARB — {best.arb_type.upper()}</b>\n"
                            f"{best.event_title[:70]}\n"
                            f"Edge: <b>{best.edge*100:.1f}¢</b> | Score {best.score():.1f}\n"
                            f"<i>{best.rationale[:180]}</i>"
                        )
            except Exception as e:
                logger.warning(f"Kalshi arb scan error: {e}")

            # Cross-platform scan runs only if Dome is configured
            if dome_client.enabled:
                try:
                    cross_opps = await cross_arb_scanner.scan(markets)
                    if cross_opps:
                        top_cross = cross_opps[:10]
                        logger.info(
                            f"Cross-arb: {len(cross_opps)} K↔P candidates "
                            f"(top edge {top_cross[0].edge*100:.1f}¢)"
                        )
                        await manager.broadcast({
                            "type": "kalshi_cross_arb",
                            "data": {
                                "opportunities": [o.to_dict() for o in top_cross],
                                "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
                            },
                        })
                        if _startup_complete and top_cross[0].edge >= 0.05:
                            best = top_cross[0]
                            await telegram.send_info(
                                f"<b>🔀 CROSS-PLATFORM ARB</b>\n"
                                f"K: {best.kalshi_title[:60]}\n"
                                f"P: {best.poly_title[:60]}\n"
                                f"Edge: <b>{best.edge*100:.1f}¢</b> | "
                                f"Match: {best.match_confidence*100:.0f}%\n"
                                f"<i>{best.rationale[:180]}</i>"
                            )
                except Exception as e:
                    logger.warning(f"Cross-arb scan error: {e}")

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

                # Telegram alert with Execute/Skip buttons
                if _startup_complete:
                    now = _time.time()
                    for opp in top:
                        if opp.score() < 7.0:
                            break  # sorted by score, rest will be lower

                        ticker      = opp.ticker
                        price_cents = opp.market_price * 100
                        seen        = _kalshi_seen.get(ticker)

                        if seen:
                            outcome = seen["outcome"]

                            # Already have a position — position monitor handles this
                            if outcome == "executed" or ticker in _kalshi_positions:
                                continue

                            # Alert is still live (pending) — don't double-send
                            if outcome == "pending":
                                continue

                            # Skipped or expired — only re-alert on significant price move
                            if outcome in ("skipped", "expired"):
                                hours_since = (now - seen["alerted_at"]) / 3600
                                if hours_since < MIN_RESUPPRESS_HOURS:
                                    continue

                                prev_price = seen["price_cents"]
                                abs_move   = abs(price_cents - prev_price)
                                rel_move   = abs_move / prev_price if prev_price > 0 else 0
                                moved_enough = (abs_move >= SIGNIFICANT_MOVE_CENTS or
                                                rel_move >= SIGNIFICANT_MOVE_PCT)
                                if not moved_enough:
                                    continue  # same market, same price — stay quiet

                                logger.info(
                                    f"KALSHI re-alert {ticker}: price moved "
                                    f"{prev_price:.0f}¢ → {price_cents:.0f}¢ "
                                    f"({abs_move:.0f}¢ / {rel_move*100:.0f}%)"
                                )

                        # ── Send the alert ─────────────────────────────────
                        _kalshi_alert_counter += 1
                        alert_id = _kalshi_alert_counter
                        opp_dict = opp.to_dict()

                        # Maker pricing: use bid-side limit to earn the spread
                        # instead of paying it. Falls back to ask if bid is 0.
                        maker_cents = round((opp.maker_price or opp.market_price) * 100)
                        _kalshi_pending[alert_id] = {
                            "ticker":      ticker,
                            "side":        opp.side if opp.side != "watch" else "yes",
                            "count":       opp.bet_contracts,
                            "price_cents": maker_cents,          # maker-side limit
                            "ask_cents":   round(price_cents),    # reference only
                            "title":       opp.title,
                            "opp_dict":    opp_dict,
                            "expires":     now + 600,
                        }
                        _kalshi_seen[ticker] = {
                            "price_cents": price_cents,
                            "alerted_at":  now,
                            "outcome":     "pending",
                        }

                        await telegram.send_kalshi_alert(opp_dict, alert_id)
                        logger.info(
                            f"KALSHI ALERT #{alert_id}: {ticker} {opp.side.upper()} "
                            f"@ {price_cents:.0f}¢ score={opp.score():.1f}"
                        )
                        break  # one alert per scan cycle

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
        seen = _kalshi_seen.get(pending["ticker"])
        if seen:
            seen["outcome"] = "expired"
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

        # Mark as executed — suppress future buy alerts on this ticker
        seen = _kalshi_seen.get(pending["ticker"])
        if seen:
            seen["outcome"] = "executed"

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
    if pending:
        seen = _kalshi_seen.get(pending["ticker"])
        if seen:
            seen["outcome"] = "skipped"
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


# ── Alpaca position monitor (TP/SL) ──────────────────────────────────────────

# Per-symbol state so we don't re-fire the same action twice.
# Keys: symbol → {"trimmed": bool, "tp_fired": bool, "tp2_fired": bool, "sl_fired": bool}
_alpaca_pos_state: dict[str, dict] = {}


async def alpaca_position_monitor():
    """Every 2 min (configurable): check Alpaca positions for TP/trim/SL.

    Two-tier take-profit:
        POS_TP_PCT   = +80%  → sell POS_TP_SELL_PCT  (50%) — lock in gains
        POS_TP2_PCT  = +175% → sell POS_TP2_SELL_PCT (100%) — exit runner

    Loss management:
        POS_TRIM_PCT = -35%  → sell POS_TRIM_SELL_PCT (50%) — reduce exposure
        POS_SL_PCT   = -40%  → liquidate entire position

    No confirmation required — executes immediately as market orders,
    then sends a Telegram notification confirming what happened.

    Only runs during market hours (RTH + extended). Positions can't move
    when the market is closed, and market orders would reject anyway.
    """
    from feeds.uw_budget import current_session
    await asyncio.sleep(45)  # let startup finish

    tp_pct         = settings.pos_tp_pct           # +80
    tp_sell_frac   = settings.pos_tp_sell_pct       # 0.5
    tp2_pct        = settings.pos_tp2_pct           # +175
    tp2_sell_frac  = settings.pos_tp2_sell_pct      # 1.0
    trim_pct       = settings.pos_trim_pct          # -35
    trim_sell_frac = settings.pos_trim_sell_pct     # 0.5
    sl_pct         = settings.pos_sl_pct            # -40
    interval       = settings.pos_monitor_interval  # 120s

    logger.info(
        f"Alpaca position monitor started: "
        f"TP1={tp_pct:+.0f}% (sell {tp_sell_frac*100:.0f}%), "
        f"TP2={tp2_pct:+.0f}% (sell {tp2_sell_frac*100:.0f}%), "
        f"trim={trim_pct:+.0f}% (sell {trim_sell_frac*100:.0f}%), "
        f"SL={sl_pct:+.0f}% (liquidate)"
    )

    while True:
        try:
            sess = current_session()
            if sess in ("overnight", "weekend"):
                await asyncio.sleep(interval)
                continue

            positions = trader.get_positions()
            if not positions:
                await asyncio.sleep(interval)
                continue

            for pos in positions:
                symbol  = pos["symbol"]
                qty     = pos["qty"]
                pnl_pct = pos["pnl_pct"]   # already in percent (e.g. -23.5)
                pnl_usd = pos["pnl"]
                avg     = pos["avg_price"]
                cur     = pos["current"]

                if qty <= 0:
                    continue

                # Initialize state for new positions
                if symbol not in _alpaca_pos_state:
                    _alpaca_pos_state[symbol] = {
                        "trimmed": False,
                        "tp_fired": False,
                        "tp2_fired": False,
                        "sl_fired": False,
                    }
                state = _alpaca_pos_state[symbol]

                # ── STOP LOSS: -40% → liquidate everything ──
                if pnl_pct <= sl_pct and not state["sl_fired"]:
                    state["sl_fired"] = True
                    result = trader.close_position(symbol)
                    if "error" not in result:
                        msg = (
                            f"\U0001f6d1 <b>STOP LOSS</b> — {symbol}\n"
                            f"Sold ALL {qty:.0f} shares/contracts\n"
                            f"Entry: ${avg:.2f} → Exit: ${cur:.2f}\n"
                            f"P&L: {pnl_pct:+.1f}% (${pnl_usd:+,.2f})"
                        )
                        logger.info(f"SL fired: {symbol} {pnl_pct:+.1f}% — closed {qty:.0f}")
                    else:
                        msg = (
                            f"\U0001f6d1 <b>STOP LOSS FAILED</b> — {symbol}\n"
                            f"Tried to close at {pnl_pct:+.1f}% but got error:\n"
                            f"{result['error']}"
                        )
                        logger.error(f"SL failed: {symbol} — {result['error']}")
                    if telegram.enabled:
                        await telegram.send_info(msg)

                # ── TRIM: -35% → sell half ──
                elif pnl_pct <= trim_pct and not state["trimmed"] and not state["sl_fired"]:
                    state["trimmed"] = True
                    sell_qty = max(1, int(qty * trim_sell_frac))
                    result = trader.market_order(symbol, sell_qty, "sell")
                    if "error" not in result:
                        msg = (
                            f"\u2702\ufe0f <b>TRIM</b> — {symbol}\n"
                            f"Sold {sell_qty} of {qty:.0f} shares/contracts\n"
                            f"Entry: ${avg:.2f} → Now: ${cur:.2f}\n"
                            f"P&L: {pnl_pct:+.1f}% (${pnl_usd:+,.2f})"
                        )
                        logger.info(f"Trim fired: {symbol} {pnl_pct:+.1f}% — sold {sell_qty}/{qty:.0f}")
                    else:
                        msg = (
                            f"\u2702\ufe0f <b>TRIM FAILED</b> — {symbol}\n"
                            f"Tried to sell {sell_qty} at {pnl_pct:+.1f}% but got error:\n"
                            f"{result['error']}"
                        )
                        logger.error(f"Trim failed: {symbol} — {result['error']}")
                    if telegram.enabled:
                        await telegram.send_info(msg)

                # ── TAKE PROFIT T2: +175% → sell remaining (runner exit) ──
                elif pnl_pct >= tp2_pct and state["tp_fired"] and not state["tp2_fired"]:
                    state["tp2_fired"] = True
                    sell_qty = max(1, int(qty * tp2_sell_frac))
                    if tp2_sell_frac >= 1.0:
                        result = trader.close_position(symbol)
                    else:
                        result = trader.market_order(symbol, sell_qty, "sell")
                    if "error" not in result:
                        msg = (
                            f"\U0001f680 <b>TAKE PROFIT T2</b> — {symbol}\n"
                            f"Sold {sell_qty} of {qty:.0f} shares/contracts\n"
                            f"Entry: ${avg:.2f} → Now: ${cur:.2f}\n"
                            f"P&L: {pnl_pct:+.1f}% (+${pnl_usd:,.2f})"
                        )
                        logger.info(f"TP2 fired: {symbol} {pnl_pct:+.1f}% — sold {sell_qty}/{qty:.0f}")
                    else:
                        msg = (
                            f"\U0001f680 <b>TP2 FAILED</b> — {symbol}\n"
                            f"Tried to sell {sell_qty} at {pnl_pct:+.1f}% but got error:\n"
                            f"{result['error']}"
                        )
                        logger.error(f"TP2 failed: {symbol} — {result['error']}")
                    if telegram.enabled:
                        await telegram.send_info(msg)

                # ── TAKE PROFIT T1: +80% → sell half (lock in gains) ──
                elif pnl_pct >= tp_pct and not state["tp_fired"]:
                    state["tp_fired"] = True
                    sell_qty = max(1, int(qty * tp_sell_frac))
                    if tp_sell_frac >= 1.0:
                        result = trader.close_position(symbol)
                    else:
                        result = trader.market_order(symbol, sell_qty, "sell")
                    if "error" not in result:
                        msg = (
                            f"\U0001f3af <b>TAKE PROFIT</b> — {symbol}\n"
                            f"Sold {sell_qty} of {qty:.0f} shares/contracts\n"
                            f"Entry: ${avg:.2f} → Now: ${cur:.2f}\n"
                            f"P&L: {pnl_pct:+.1f}% (+${pnl_usd:,.2f})"
                        )
                        logger.info(f"TP fired: {symbol} {pnl_pct:+.1f}% — sold {sell_qty}/{qty:.0f}")
                    else:
                        msg = (
                            f"\U0001f3af <b>TP FAILED</b> — {symbol}\n"
                            f"Tried to sell {sell_qty} at {pnl_pct:+.1f}% but got error:\n"
                            f"{result['error']}"
                        )
                        logger.error(f"TP failed: {symbol} — {result['error']}")
                    if telegram.enabled:
                        await telegram.send_info(msg)

            # Clean up state for positions we no longer hold
            current_symbols = {p["symbol"] for p in positions if p["qty"] > 0}
            for sym in list(_alpaca_pos_state):
                if sym not in current_symbols:
                    _alpaca_pos_state.pop(sym, None)

        except Exception as e:
            logger.error(f"Alpaca position monitor error: {e}")

        await asyncio.sleep(interval)


async def iv_scanner_loop():
    """Poll IV rank + earnings setup for watchlist tickers every 5 minutes.

    Budget-aware: on weekends nothing moves, so we skip entirely. During
    throttle (UW >80% daily) we double the cycle time. IV snapshots are
    also useless outside market hours on weekdays — we slow to 30 min there.
    """
    import time as _time
    from api.routes import _watchlist
    from feeds.uw_budget import current_session, budget
    from signals.earnings_scanner import scan_ticker as earnings_scan
    await asyncio.sleep(30)  # give server time to start

    _earnings_last_run: dict[str, float] = {}  # ticker → epoch of last scan

    while True:
        sess = current_session()
        if sess == "weekend":
            await asyncio.sleep(1800)  # check again in 30 min
            continue
        if budget.should_pause():
            logger.info("IV scanner paused — UW budget exhausted")
            await asyncio.sleep(600)
            continue

        for ticker in list(_watchlist):
            try:
                # ── IV Rank (UW) ────────────────────────────────────────
                iv_data = await uw_client.get_iv_rank(ticker)
                iv_rank = float(iv_data.get("iv_rank", 0) or 0)
                iv_pct  = float(iv_data.get("iv_percentile", 0) or 0)
                signal  = engine.score_iv_rank(ticker, iv_rank, iv_pct)
                if signal:
                    await handle_signal(signal)

                # ── Earnings IV/RV setup (yfinance) — max once per 30 min ──
                now = _time.time()
                if now - _earnings_last_run.get(ticker, 0) >= 1800:
                    _earnings_last_run[ticker] = now
                    setup  = await earnings_scan(ticker)
                    signal = engine.score_earnings_setup(setup)
                    if signal:
                        await handle_signal(signal)
                        logger.info(
                            f"Earnings setup {ticker}: {setup.recommendation} "
                            f"IV/RV={setup.iv30_rv30:.2f}x score={signal.score}"
                        )

            except Exception as e:
                logger.warning(f"IV scanner error for {ticker}: {e}")
            await asyncio.sleep(2)  # 2s between tickers

        # Cycle cadence: 5 min RTH, 15 min extended, 30 min overnight.
        # Throttle doubles all of these.
        cycle = {"rth": 300, "extended": 900, "overnight": 1800}.get(sess, 900)
        if budget.should_throttle():
            cycle *= 2
        await asyncio.sleep(cycle)


async def uw_budget_monitor_loop():
    """Log and broadcast UW daily call budget every 10 min.

    Also fires a Telegram warning the first time we cross 80% so the
    operator knows to back off manual probing. No-op until the UW client
    has made at least one call (headers populate the tracker).
    """
    from feeds.uw_budget import budget, current_session
    warned_80 = False
    warned_95 = False
    await asyncio.sleep(60)
    while True:
        try:
            if budget.last_update_ts > 0:
                status = budget.status()
                logger.info(
                    f"UW budget: {status['daily_count']}/{status['daily_limit']} "
                    f"({status['usage_pct']*100:.1f}%) session={status['session']}"
                )
                await manager.broadcast({"type": "uw_budget", "data": status})
                pct = status["usage_pct"]
                if pct >= 0.95 and not warned_95 and telegram.enabled:
                    await telegram.send_info(
                        f"⛔ UW API at {pct*100:.0f}% "
                        f"({status['daily_count']}/{status['daily_limit']}) — feed paused"
                    )
                    warned_95 = True
                elif pct >= 0.80 and not warned_80 and telegram.enabled:
                    await telegram.send_info(
                        f"⚠️ UW API at {pct*100:.0f}% "
                        f"({status['daily_count']}/{status['daily_limit']}) — throttling"
                    )
                    warned_80 = True
                # Reset warning flags once usage falls back down (new day)
                if pct < 0.50:
                    warned_80 = False
                    warned_95 = False
        except Exception as e:
            logger.debug(f"uw_budget_monitor error: {e}")
        await asyncio.sleep(600)  # 10 min


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
    uw_budget_task = asyncio.create_task(uw_budget_monitor_loop())
    alpaca_monitor_task = asyncio.create_task(alpaca_position_monitor())

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

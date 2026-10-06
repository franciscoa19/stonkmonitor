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
from trading.performance import sync_trade_performance
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
    options_feed=settings.alpaca_options_feed,
)
engine          = SignalEngine(settings)
pattern_engine  = PatternEngine(notify_threshold=8.0)
discord     = DiscordNotifier(settings.discord_webhook_url,
                              alerts_enabled=settings.discord_alerts_enabled)
pushover    = PushoverNotifier(settings.pushover_api_token, settings.pushover_user_key)
telegram    = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id,
                               alerts_enabled=settings.telegram_alerts_enabled)
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
    """Score → store → broadcast → notify.

    Notification and auto-trade are gated by a time-of-day score bump:
      open_first_5 (09:30–09:35)  +2.0 — pure chaos, only exceptional signals
      open         (09:35–10:00)  +1.5 — still noisy
      close        (15:45–16:00)  +0.5 — MOC noise
      normal RTH                  +0.0 — baseline
    Extended-hours option flow is shown on the dashboard but never notified
    (options don't actually trade; the alerts are stale or erroneous).
    """
    if signal is None:
        return

    from feeds.uw_budget import market_subphase, score_bump_for_subphase

    sig_dict = signal.to_dict()

    # Store (ring buffer)
    signal_store.append(sig_dict)
    if len(signal_store) > 500:
        signal_store.pop(0)

    # Always broadcast to frontend (dashboard shows everything)
    await manager.broadcast_signal(sig_dict)

    # Persist to DB if score >= 7
    await db.save_signal(signal, min_score=7.0)

    if not _startup_complete or _is_stale(signal):
        return

    # ── Time-of-day noise gate ─────────────────────────────────────────────
    _cfg_bumps = {
        "open_first_5": settings.open_first5_bump,
        "open":         settings.open_bump,
        "close":        settings.close_bump,
    }
    subphase = market_subphase()
    bump     = score_bump_for_subphase(subphase, _cfg_bumps)

    # Options/darkpool notifications are meaningless outside RTH
    is_options_type = signal.type.value in ("options_flow", "dark_pool")
    if subphase in ("extended", "overnight", "weekend") and is_options_type:
        logger.debug(
            f"Suppressed {signal.type.value} notification outside RTH "
            f"(score={signal.score:.1f}, subphase={subphase})"
        )
        return

    # Intraday volatility bump — raises bar when market is moving hard.
    # Reuses auto_trade's cached SPY regime data (0 extra API calls).
    vol_bump = auto_trade.effective_vol_bump()

    # Effective thresholds = base + time-of-day bump + intraday vol bump
    base_threshold   = settings.sweep_score_threshold          # default 7.0
    notify_threshold = base_threshold + bump + vol_bump        # covers Discord/Pushover too
    auto_threshold   = settings.auto_trade_score_threshold + bump  # auto_trade._pre_flight handles vol internally

    if bump > 0 or vol_bump > 0:
        logger.debug(
            f"Score gates ({subphase}): notify≥{notify_threshold:.1f} "
            f"auto≥{auto_threshold:.1f} "
            f"[tod_bump={bump:+.1f} vol_bump={vol_bump:+.1f}] "
            f"(score={signal.score:.1f})"
        )

    # Notifications (Discord / Pushover) — gated by time-of-day + vol bump
    if signal.score >= notify_threshold:
        await discord.send_signal(signal, score_threshold=notify_threshold)
        await pushover.send_signal(signal, score_threshold=notify_threshold)

    # Auto-trade — vol gate handled inside _pre_flight with fresh regime data
    if signal.score >= auto_threshold:
        try:
            account = await asyncio.to_thread(trader.get_account)
            await auto_trade.evaluate_signal(signal, account)
        except Exception as e:
            logger.warning(f"Auto-trade eval error: {e}")

    logger.info(
        f"Signal: {signal.title} | Score {signal.score:.1f}"
        + (f" | {subphase}+{bump:.1f}" if bump > 0 else "")
        + (f" | vol+{vol_bump:.1f}" if vol_bump > 0 else "")
    )


async def _maybe_auto_watchlist(flow: dict):
    """Auto-add big, liquid names to the watchlist when we see options flow on
    them. Gated on market cap (WATCHLIST_AUTO_ADD_MIN_MKTCAP; 0 = off). The name
    is generating options flow (so it's liquid) and clears the size bar — exactly
    the universe worth IV/earnings-scanning. Persisted; the weekly review prunes
    any that go quiet. Idempotent (skips names already watchlisted)."""
    threshold = settings.watchlist_auto_add_min_mktcap
    if not threshold:
        return
    ticker = (flow.get("ticker") or flow.get("underlying_symbol") or "").upper()
    if not ticker:
        return
    try:
        mktcap = float(flow.get("marketcap") or 0)
    except (TypeError, ValueError):
        return
    if mktcap < threshold:
        return
    from api.routes import _watchlist
    if ticker in _watchlist:
        return
    _watchlist.append(ticker)
    await db.add_watchlist(ticker)
    logger.info(f"Watchlist auto-add: {ticker} (mktcap ${mktcap/1e9:.0f}B ≥ "
                f"${threshold/1e9:.0f}B threshold)")


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
            await _maybe_auto_watchlist(data)   # auto-cover big, liquid names
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
        # Apply the same time-of-day bump so open-hour patterns also need a higher bar
        if _startup_complete and fired_patterns:
            try:
                from feeds.uw_budget import market_subphase, score_bump_for_subphase
                _cfg_bumps = {
                    "open_first_5": settings.open_first5_bump,
                    "open":         settings.open_bump,
                    "close":        settings.close_bump,
                }
                _bump = score_bump_for_subphase(market_subphase(), _cfg_bumps)
                _pat_threshold = settings.auto_trade_pattern_threshold + _bump
                account = await asyncio.to_thread(trader.get_account)
                for pat in fired_patterns:
                    if pat.score >= _pat_threshold:
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
# Keys: symbol → {"trimmed", "tp_fired", "tp2_fired", "sl_fired", "trailing", "high_watermark"}
_alpaca_pos_state: dict[str, dict] = {}


async def alpaca_position_monitor():
    """Reconcile pending exits and manage long positions without blocking I/O."""
    from signals.position_exits import manage_position_exit
    from trading.ownership import option_ownership
    from feeds.uw_budget import current_session
    await asyncio.sleep(45)
    restored = False
    while True:
        try:
            if not restored:
                saved = await db.get_position_monitor_states()
                _alpaca_pos_state.clear()
                _alpaca_pos_state.update(saved)
                restored = True
            if current_session() not in ("overnight", "weekend"):
                positions = await asyncio.to_thread(trader.get_positions_raw)
                if positions is not None:
                    # The API wrapper returns our normalized P&L shape. Raw
                    # positions above distinguish fetch failure from an empty account.
                    normalized = [{"symbol": p["symbol"], "qty": float(p["qty"]),
                                   "pnl_pct": float(p.get("unrealized_plpc") or 0) * 100,
                                   "avg_price": float(p.get("avg_entry_price") or 0)}
                                  for p in positions if float(p.get("qty") or 0) > 0]
                    ownership = await option_ownership(db, trader, positions)
                    condor_legs = ownership["protected_symbols"]
                    if ownership["entry_block_reason"]:
                        logger.warning("Options quarantined: %s", ownership["entry_block_reason"])
                    unsettled = {c["ticker"] for c in await db.get_active_condors()
                                 if c["status"] == "awaiting_settlement"}
                    for pos in normalized:
                        symbol = pos["symbol"]
                        if symbol in condor_legs or symbol in unsettled:
                            continue
                        state = _alpaca_pos_state.setdefault(symbol, {})
                        message = await manage_position_exit(db, trader, pos, settings, state)
                        if message:
                            logger.info(message)
                            if telegram.enabled:
                                await telegram.send_info(message)
                    held = {p["symbol"] for p in normalized}
                    # Also reconcile a full exit after its position disappears.
                    for symbol, state in list(_alpaca_pos_state.items()):
                        if symbol not in held:
                            if state.get("pending"):
                                await manage_position_exit(db, trader,
                                    {"symbol": symbol, "qty": 0, "pnl_pct": 0}, settings, state)
                            if not state.get("pending"):
                                await db.delete_position_monitor_state(symbol)
                                _alpaca_pos_state.pop(symbol, None)
        except Exception as e:
            logger.error(f"Alpaca position monitor error: {e}")
        await asyncio.sleep(settings.pos_monitor_interval)


async def performance_sync_loop():
    """Sync Alpaca order history into trade_performance table every 15 min.

    Syncs closed and active orders plus individual FILL activities, including
    canceled partial executions, before recomputing FIFO performance.
    """
    await asyncio.sleep(60)  # let startup finish

    while True:
        try:
            await auto_trade.reconcile_submissions()
            # Book realized P&L for bracket/server-side exits that never hit record_exit.
            reconciled = await sync_trade_performance(db, trader)
            await auto_trade.refresh_risk_controls()
            if reconciled:
                logger.debug(f"Performance sync: reconciled {reconciled} closed trades")
        except Exception as e:
            logger.error(f"Performance sync error: {e}")

        await asyncio.sleep(900)  # every 15 minutes


async def daily_equity_loop():
    """Snapshot paper-account equity for the eval loop's equity curve.

    Upserts once per ET calendar day (keyed by date), refreshed hourly so
    'today' always holds the latest equity. Feeds the daily check-in report.
    """
    from market_time import et_now
    await asyncio.sleep(20)  # let startup settle
    while True:
        try:
            acct = await asyncio.to_thread(trader.get_account)
            equity = float(acct.get("equity", 0) or 0)
            if equity > 0:
                try:
                    n_pos = len(await asyncio.to_thread(trader.get_positions))
                except Exception:
                    n_pos = 0
                await db.record_daily_equity(
                    date_str=et_now().strftime("%Y-%m-%d"),
                    equity=equity,
                    cash=float(acct.get("cash", 0) or 0),
                    buying_power=float(acct.get("buying_power", 0) or 0),
                    open_positions=n_pos,
                )
        except Exception as e:
            logger.warning(f"Daily equity snapshot error: {e}")

        # Resolve any IV/RV evals whose window has elapsed: measure the realized
        # move vs the implied move logged at signal time (hypothetical straddle).
        try:
            today = et_now().strftime("%Y-%m-%d")
            for ev in await db.get_due_iv_evals(today):
                q = feed.get_latest_quote(ev["ticker"])
                bid, ask = float(q.get("bid") or 0), float(q.get("ask") or 0)
                px = (bid + ask) / 2 if (bid and ask) else (bid or ask)
                if px and ev["entry_price"]:
                    realized = abs(px / ev["entry_price"] - 1) * 100
                    await db.resolve_iv_eval(ev["id"], px, realized, ev["implied_move_pct"] or 0)
                    logger.info(f"IV/RV eval resolved: {ev['ticker']} realized "
                                f"±{realized:.1f}% vs implied ±{ev['implied_move_pct']:.1f}%")
        except Exception as e:
            logger.warning(f"IV eval resolve error: {e}")

        # Resolve due strategy-variant evals from the historical underlying close
        # on their actual option expiry (no execution).
        try:
            from signals.iv_variants import (variant_payoff, pin_risk,
                                             settlement_ready)
            today = et_now().strftime("%Y-%m-%d")
            due = await db.get_due_variant_evals(today)
            spot_cache: dict = {}
            loop = asyncio.get_running_loop()
            resolved = early = 0
            for ev in due:
                tk = ev["ticker"]
                expiry = ev.get("expiry")
                if not expiry:
                    logger.warning(f"Variant eval #{ev['id']} has no expiry; leaving unresolved")
                    continue
                # Settlement is the expiry session's CLOSE. Alpaca's Day bar for
                # a session in progress already carries that date, so without
                # this the first pass on expiry morning would book an intraday
                # price as the settled result.
                if not settlement_ready(expiry):
                    early += 1
                    continue
                key = (tk, expiry)
                if key not in spot_cache:
                    spot_cache[key] = await loop.run_in_executor(
                        None, feed.get_daily_close, tk, expiry)
                px = spot_cache[key]
                if px:
                    # Net of round-trip commission — a gross-of-fees result
                    # overstates every structure, and unevenly (a 4-leg condor
                    # pays twice the commission of a 2-leg straddle).
                    pnl = variant_payoff(ev, px) - float(ev["fees"] or 0)
                    await db.resolve_variant_eval(
                        ev["id"], px, pnl,
                        pin_risk=pin_risk(ev, px, ev["strike_step"]))
                    resolved += 1
            if due:
                logger.info(f"Variant evals resolved: {resolved}/{len(due)} rows across "
                            f"{len(spot_cache)} expiries"
                            + (f" ({early} held — expiry session still open)" if early else ""))
        except Exception as e:
            logger.warning(f"Variant eval resolve error: {e}")

        await asyncio.sleep(3600)  # hourly


async def maybe_execute_condor(setup):
    """Phase 2: sell a defined-risk iron condor into an imminent earnings print.

    Gated by IV_EXEC_ENABLED. Fires only when the setup is strong enough and the
    print is within iv_exec_entry_days_before, respecting position/day caps and a
    per-ticker one-condor rule. Logs its own outcome; returns None.
    """
    from datetime import date as _date
    from market_time import et_now, et_today
    from signals.iv_executor import build_iron_condor, is_pre_earnings_entry_window
    s = settings
    if not s.iv_exec_enabled:
        return
    rec = getattr(setup, "recommendation", "AVOID")
    if rec != "SELL_PREMIUM" and not (rec == "CONSIDER" and s.iv_exec_allow_consider):
        return
    # Entries are priced off the option book, which is two-sided only in regular
    # hours. The scanner also runs pre-market, after the close and overnight; a
    # condor planned there is sized and limited off stale marks and would rest
    # until the next open. Closes already wait for RTH — so must entries.
    if not is_rth_now():
        logger.debug("IV-exec %s: entry deferred to regular trading hours",
                     getattr(setup, "ticker", "?"))
        return
    # A same-day BMO print has already happened. Fail closed when report timing
    # is unknown, and permit same-day entries only before a confirmed post-close
    # report. This must remain independent of the broader alert/eval gate.
    if not is_pre_earnings_entry_window(
        getattr(setup, "next_earnings_date", None),
        getattr(setup, "earnings_report_time", None),
        s.iv_exec_entry_days_before,
        now=et_now(),
    ):
        # Distinguish the reasons. Simply being outside the entry window is the
        # routine case for every name in the 7-day scan and says nothing — at
        # INFO it produced hundreds of lines a day claiming prints had "already
        # occurred" for events still days away. The event-day block is the one
        # worth seeing: it means a print we tracked went by unsold.
        _tk = getattr(setup, "ticker", "?")
        _days = None
        try:
            _days = (_date.fromisoformat(str(getattr(setup, "next_earnings_date", None)))
                     - et_today()).days
        except (TypeError, ValueError):
            pass
        if _days == 0:
            logger.info(f"IV-exec skip {_tk}: earnings is TODAY but entry is closed "
                        f"(report_time={getattr(setup, 'earnings_report_time', None)!r}; "
                        "same-day entry needs a confirmed post-close print before 16:00 ET)")
        elif _days is None:
            logger.info(f"IV-exec skip {_tk}: no usable earnings date")
        else:
            logger.debug(f"IV-exec skip {_tk}: earnings in {_days}d, outside the "
                         f"{s.iv_exec_entry_days_before}d entry window")
        return

    ticker = setup.ticker
    if await db.has_open_condor(ticker):
        return
    today = et_today().isoformat()
    if await db.count_open_condors() >= s.iv_exec_max_positions:
        logger.info("IV-exec skip: max open condors reached")
        return
    if await db.count_condors_opened_today(today) >= s.iv_exec_max_per_day:
        logger.info("IV-exec skip: daily condor cap reached")
        return

    from math import isfinite
    acct = await asyncio.to_thread(trader.get_account)
    try:
        equity = float(acct["equity"]) if not acct.get("error") else 0
    except (KeyError, TypeError, ValueError):
        equity = 0
    if not isfinite(equity) or equity <= 0:
        logger.warning("IV-exec skip: verified broker equity unavailable")
        return
    from trading.ownership import option_ownership
    ownership = await option_ownership(db, trader)
    if ownership["entry_block_reason"]:
        logger.warning("IV-exec skip: %s", ownership["entry_block_reason"])
        return

    # Risk throttle: size down after losses, back up after wins, and refuse
    # outright on a losing streak. A streak is the signal that the regime moved,
    # which is precisely when the next trade should be smaller or not happen.
    mult = 1.0
    rs = await db.get_risk_state(s.iv_risk_loss_factor, s.iv_risk_win_factor,
                                 s.iv_risk_floor, s.iv_risk_halt_streak)
    if rs["halted"]:
        logger.warning(
            f"IV-exec HALTED — no new condors. {rs['halted_reason']} "
            f"(tripped {rs['halted_at']}). Clear it deliberately to resume.")
        return
    if s.iv_risk_throttle_enabled:
        mult = rs["multiplier"]
        if mult < 1.0:
            logger.info(f"IV-exec {ticker}: throttled to {mult:.0%} of normal size "
                        f"after {rs['loss_streak']} consecutive loss(es)")

    loop = asyncio.get_event_loop()
    plan = await loop.run_in_executor(
        None, build_iron_condor, trader, setup, equity, s, mult)
    if not plan.get("ok"):
        logger.info(f"IV-exec {ticker}: no condor ({plan.get('reason')})")
        return
    # Resolve the order namespace before a row exists: if it fails, nothing is
    # persisted or submitted.
    ns = await db.order_namespace()
    # Persist before submitting: an ambiguous POST must remain tracked and must
    # not be repeated as a new condor on the next scan.
    cid = await db.record_condor(
        ticker=ticker, earnings_date=setup.next_earnings_date, expiry=plan["expiry"],
        legs_json=plan["legs_json"], strikes=plan["strikes"], qty=plan["qty"],
        credit=plan["credit"], max_loss=plan["max_loss"],
        entry_order_id=None, entry_status="submitting")
    res = await loop.run_in_executor(
        None, lambda: trader.multileg_order(plan["legs"], plan["qty"], plan["limit_price"],
                                            client_order_id=_condor_entry_client_id(ns, cid)))
    if res.get("error"):
        if not res.get("ambiguous", True):
            await db.void_condor(cid, "rejected")
        logger.error(f"IV-exec {ticker} submit failed: {res['error']}")
        return
    await db._exec("UPDATE iv_condors SET entry_order_id=?, entry_status=? WHERE id=?",
                   (res.get("id"), res.get("status"), cid), strict=True, expected_rows=1)
    st = plan["strikes"]
    logger.info(
        f"IV-exec ✅ {ticker} iron condor #{cid}: "
        f"{st['long_put']}/{st['short_put']}--{st['short_call']}/{st['long_call']} "
        f"x{plan['qty']} credit ${plan['credit']:.2f} maxloss ${plan['max_loss']:.0f} "
        f"risk ${plan['risk_usd']:.0f} exp {plan['expiry']} order={res.get('id')}")


def is_rth_now() -> bool:
    """True during regular trading hours, when options actually have two-sided
    markets. Pricing a structure outside RTH yields nothing to price."""
    from feeds.uw_budget import current_session, market_subphase
    try:
        return current_session() == "rth" and market_subphase() != "closed"
    except Exception:
        return False


def days_to_print(earnings_date) -> int | None:
    """Calendar days from today (ET) to the print, or None if the date is unusable."""
    from datetime import date as _date
    from market_time import et_today
    try:
        return (_date.fromisoformat(str(earnings_date)) - et_today()).days
    except (TypeError, ValueError):
        return None


async def variant_pricing_action(ticker: str, earnings_date, days_to: int | None,
                                 source: str = "watchlist") -> tuple[str, int | None]:
    """What this earnings event needs from the variant logger right now:
    "fresh" (never priced), "reprice" (held rows were priced further from the
    print than we are, and we are now close enough for the earnings premium to
    be in the quotes), or "skip".

    Both callers need this answer — the watchlist scanner to decide whether to
    drop and re-price, the measurement universe to decide whether a candidate is
    worth scanning at all. The measurement loop's copy of the rule silently did
    not exist: it skipped on "rows already present", so CCL/JBL/ACN kept a
    7-day-out implied move while MU re-priced. One function, one rule.

    Returns (action, lead_days_of_held_rows_or_None).
    """
    existing = await db.get_open_variant_lead(ticker, earnings_date, source)
    if existing is None:
        return ("fresh", None)
    if days_to is None or days_to >= existing:
        return ("skip", existing)        # nothing closer to offer
    if days_to > settings.iv_variants_reprice_within_days:
        return ("skip", existing)        # closer, but still too far to be worth it
    return ("reprice", existing)


async def log_variant_evals(setup, gate_passed: bool = True,
                            source: str = "watchlist"):
    """Measurement only: price several hypothetical structures for this earnings
    event and log them for later resolution vs the realized move. No execution.
    One set per ticker+event+source (deduped). Each variant settles only after
    its own option expiry, using that session's historical underlying close.

    `gate_passed` records whether the setup cleared the three scanner gates, so
    filtered and indiscriminate selling can be scored against each other.
    `source` keeps the curated watchlist distinct from the measurement-only
    universe when the evidence is later analyzed.
    """
    from signals.earnings_scanner import is_near_earnings
    from signals.iv_variants import build_variants, expiry_settlement_date
    if not settings.iv_variants_log_enabled:
        return
    if not is_near_earnings(setup, settings.iv_setup_max_days_to_earnings):
        logger.debug("Variant-log %s skipped: earnings has occurred or is outside the window",
                     getattr(setup, "ticker", "?"))
        return
    edate = getattr(setup, "next_earnings_date", None)

    # Re-price as the print approaches. Logging once at first eligibility (7
    # days out) captures the QUIET front-month IV, well before the earnings
    # premium inflates — COST recorded a 1.22% implied move that way, which then
    # made a winning trade look like the market had underpriced the move. So:
    # keep the earliest capture (guarantees the event is never missed entirely),
    # but replace it with a fresh pass once we are inside the execution window
    # and genuinely closer to the print than the row we hold.
    _days_to = days_to_print(edate)
    _action, _existing_lead = await variant_pricing_action(
        setup.ticker, edate, _days_to, source)
    if _action == "skip":
        return
    # A re-price needs live option quotes. Outside regular hours there are no
    # two-sided markets, so build_variants() drops every structure as
    # invalid_input — and a delete-then-rebuild would then have destroyed a good
    # capture and replaced it with nothing. It did exactly that to JBL/MU/ACN/NKE
    # on 2026-09-29 before this check existed. A held capture is worth more than
    # a marginally closer one, so when in doubt, keep what we have.
    if _action == "reprice" and not is_rth_now():
        logger.debug("Variant-log %s: re-price deferred to market hours", setup.ticker)
        return
    loop = asyncio.get_event_loop()
    diagnostics: dict = {}
    variants = await loop.run_in_executor(
        None, build_variants, trader, setup, settings, diagnostics)
    from signals.measurement_capture import measurement_snapshot
    await db.record_variant_capture(setup.ticker, edate, source, measurement_snapshot(
        setup, settings, variants, diagnostics, action=_action, lead_days=_days_to, gate_passed=gate_passed))
    await db.record_variant_attempt(
        setup.ticker, edate, diagnostics.get("attempted", 0),
        diagnostics.get("priced", 0), diagnostics.get("dropped", {}), source=source)
    if not variants:
        # Nothing priced. On a re-price this is the critical path: the rows we
        # already hold are still the best evidence we have, so leave them alone.
        if _action == "reprice":
            logger.warning(
                f"Variant-log {setup.ticker}: re-price at {_days_to}d priced 0/"
                f"{diagnostics.get('attempted', 0)} structures — keeping the "
                f"{_existing_lead}d capture rather than dropping it")
        return
    # Validate every replacement before retiring anything. A partial chain is
    # useful as a quote-coverage observation, but cannot replace a complete
    # held event without silently dropping the variants it failed to price.
    priced_variants = []
    for v in variants:
        resolve_after = expiry_settlement_date(v.get("expiry"))
        if not resolve_after:
            logger.warning(f"Variant-log {setup.ticker} skipped {v['variant']}: invalid expiry")
            continue
        priced_variants.append((v, resolve_after))
    if not priced_variants:
        return

    # Only now, with a complete priced replacement in hand, retire this source's
    # older capture. The watchlist and measurement cohorts must remain distinct.
    if _action == "reprice":
        held = await db.get_open_variant_names(setup.ticker, edate, source)
        replacement = {v["variant"] for v, _ in priced_variants}
        missing = held - replacement
        if missing:
            logger.warning(
                f"Variant-log {setup.ticker}: re-price at {_days_to}d missing "
                f"{', '.join(sorted(missing))}; keeping the {source} capture")
            return
        dropped = await db.delete_open_variant_evals(setup.ticker, edate, source)
        logger.info(f"Variant-log {setup.ticker}: re-pricing {dropped} row(s) at "
                    f"{_days_to}d to print (was {_existing_lead}d — implied move "
                    "understated that far out)")
    im = 0.0
    try:
        im = float(str(setup.expected_move or "0").rstrip("%") or 0)
    except Exception:
        im = 0.0
    logged = []
    for v, resolve_after in priced_variants:
        await db.record_variant_eval(
            ticker=setup.ticker, earnings_date=edate, expiry=v["expiry"],
            variant=v["variant"], spot=setup.price, implied_move_pct=im,
            strikes=v["strikes"], credit=v["credit"], max_loss=v["max_loss"],
            resolve_after=resolve_after, credit_mid=v.get("credit_mid"),
            fees=v.get("fees"), strike_step=v.get("strike_step"),
            gate_passed=gate_passed, source=source,
            collapsed_with=v.get("collapsed_with"), lead_days=_days_to)
        logged.append(v["variant"])
    if logged:
        logger.info(f"Variant-log {setup.ticker} ({'gated' if gate_passed else 'baseline'}): "
                    f"{', '.join(logged)} → settle {resolve_after}")
    return len(logged)


def _has_front_expiry(ticker: str, earnings_date: str, s) -> bool:
    """Is there a listed expiry after the print and inside the DTE band?

    Names without weekly options fail this: their next expiry after a print is
    typically the following monthly, weeks past the front-month window the
    strategy depends on. Blocking synthetic fills for names whose options are
    effectively untradeable keeps the measurement sample honest.
    """
    from datetime import date as _d, timedelta as _td
    from market_time import et_today
    try:
        edate = _d.fromisoformat(str(earnings_date))
    except (TypeError, ValueError):
        return False
    today = et_today()
    start = max(today + _td(days=s.iv_exec_min_dte), edate + _td(days=1))
    end = today + _td(days=s.iv_exec_max_dte)
    if start > end:
        return False
    try:
        return bool(trader.get_option_contracts(ticker, start, end, "call", limit=5))
    except Exception:
        return False


async def measurement_universe_loop():
    """Price structures for near-earnings names BEYOND the tradeable watchlist.

    MEASUREMENT ONLY: these names are never alerted on and never executed — the
    loop calls nothing but the variant logger. The watchlist is 78 names (~312
    prints/yr, of which only ~50-90 clear the gates), which leaves the gated arm
    of the gates-vs-baseline comparison years short of its 100-event rail. The
    logger risks no capital, so it can cover far more prints than we would ever
    trade, and that is the cheapest way to make the comparison answerable this
    season rather than in 2028.

    Candidates come from the Nasdaq calendar already in memory, filtered by
    market cap as a free liquidity proxy and ordered by proximity to the print
    (where front-month IV is actually inflated), then capped per cycle so the
    scan load stays bounded.
    """
    from feeds.earnings_calendar import get_upcoming_reporters
    from feeds.uw_budget import current_session
    from signals.earnings_scanner import (scan_ticker as earnings_scan,
                                          is_sell_eligible, is_near_earnings)
    from api.routes import _watchlist
    s = settings
    await asyncio.sleep(90)          # let startup + the calendar warm-up finish
    while True:
        try:
            if not (s.iv_measure_universe_enabled and s.iv_variants_log_enabled):
                await asyncio.sleep(3600)
                continue
            if current_session() == "weekend":
                await asyncio.sleep(3600)
                continue
            loop = asyncio.get_event_loop()
            cands = await loop.run_in_executor(None, lambda: get_upcoming_reporters(
                s.iv_setup_max_days_to_earnings,
                min_market_cap=s.iv_measure_min_market_cap,
                limit=s.iv_measure_max_per_cycle * 4,   # headroom for dedup skips
                exclude=set(_watchlist)))
            logged = scanned = 0
            for c in cands:
                if logged >= s.iv_measure_max_per_cycle:
                    break
                tk = c["ticker"]
                # Already priced? Only skip if a fresh pass would not be closer
                # to the print than the rows we hold. A name priced 7 days out
                # still carries the quiet front-month IV, not the earnings
                # premium, so it has to be allowed back through.
                _dtp = days_to_print(c["date"])
                _act, _held = await variant_pricing_action(
                    tk, c["date"], _dtp, source="measurement")
                if _act == "skip":
                    continue
                # Cheap pre-filter before the expensive yfinance scan: market cap
                # is a poor proxy for OPTIONS liquidity. Plenty of $2B+ names list
                # monthlies only, so after their print the next expiry is weeks
                # out and no front-month structure exists at all. One contracts
                # call answers that directly; skipping here saves a full scan.
                has_front_expiry = await loop.run_in_executor(
                    None, _has_front_expiry, tk, c["date"], s)
                # This still hits the broker even when the candidate fails, so
                # pace every preflight request rather than only successful scans.
                await asyncio.sleep(3)
                if not has_front_expiry:
                    logger.debug(f"Measure-universe {tk} skipped: no expiry after the print")
                    continue
                try:
                    setup = await earnings_scan(tk)
                    scanned += 1
                    if not setup or not is_near_earnings(setup, s.iv_setup_max_days_to_earnings):
                        continue
                    n = await log_variant_evals(
                        setup,
                        gate_passed=is_sell_eligible(setup, s.iv_setup_max_days_to_earnings),
                        source="measurement")
                    if n:
                        logged += 1
                except Exception as e:
                    logger.debug(f"Measure-universe {tk} skipped: {e}")
            if scanned:
                logger.info(f"Measurement universe: {logged} event(s) logged from "
                            f"{scanned} scanned ({len(cands)} candidates)")
        except Exception as e:
            logger.warning(f"Measurement universe error: {e}")
        await asyncio.sleep(max(1, s.iv_measure_interval_hours) * 3600)


def _condor_close_debit(legs: list, quotes: dict) -> float | None:
    """Cost to buy the spread back at the touch (net debit), or None when the
    quotes cannot support a number. legs order: [short_call, long_call,
    short_put, long_put].

    Priced the way the order actually fills, which is also the conservative
    direction: the shorts we buy back cost their ASK, the long wings we sell out
    fetch their BID. That mirrors the entry convention (shorts at bid, longs at
    ask) instead of using mids, which flatter both ends of a round trip.

    A zero BID on a long wing is a real price — a worthless option — not a
    missing quote. Requiring every leg to be two-sided rejected exactly the
    books that matter most: on expiry day a WINNING condor has worthless wings
    by definition, so the gate blocked NKE from closing normally on 2026-10-02.
    What must still be refused is a short leg with no ASK, because then there is
    no price at which we can buy it back, and letting it default toward zero
    understates the debit — faking a profit target and setting a limit that
    cannot fill.
    """
    px = {}
    for l in legs:
        q = quotes.get(l["symbol"])
        if not q:
            return None
        bid, ask = float(q.get("bid") or 0), float(q.get("ask") or 0)
        if l.get("side") == "sell":          # short: we pay the ask to close
            if ask <= 0:
                return None
            px[l["symbol"]] = ask
        else:                                # long wing: we receive the bid
            px[l["symbol"]] = max(bid, 0.0)
    return (px[legs[0]["symbol"]] + px[legs[2]["symbol"]]) \
        - (px[legs[1]["symbol"]] + px[legs[3]["symbol"]])


async def _condor_risk_check(pnl: float) -> None:
    """Advance the anti-martingale throttle after a condor books P&L, and trip
    the breaker on a loss streak.

    Shared by every settlement path. A condor held to expiry books through a
    different branch than one closed by order fill, and a throttle that only
    advanced on order fills would quietly stop counting losses the moment we
    started letting positions expire.
    """
    if not settings.iv_risk_throttle_enabled:
        return
    rs = await db.get_risk_state(
        settings.iv_risk_loss_factor, settings.iv_risk_win_factor,
        settings.iv_risk_floor, settings.iv_risk_halt_streak)
    if rs["loss_streak"] >= settings.iv_risk_halt_streak and not rs["halted"]:
        reason = (f"{rs['loss_streak']} consecutive losing condors — "
                  "halted pending review")
        await db.set_halt(reason)
        logger.error(f"IV-exec CIRCUIT BREAKER TRIPPED: {reason}")
    elif pnl < 0:
        logger.warning(f"IV-exec loss streak now {rs['loss_streak']}; "
                       f"next size {rs['multiplier']:.0%} of normal")


def _condor_expiry_hold(c: dict, spot: float | None, buffer_pct: float) -> bool:
    """True when an expiring condor sits far enough inside its short strikes to
    be worth letting expire rather than paying to close.

    Taking the 50%-of-credit profit target on expiry day forfeits the rest of
    the premium to remove a risk that has largely already passed: NKE's TP close
    would have booked +$260 against the +$460 the position collects if all four
    legs expire worthless. Holding is only right while there is real room — the
    buffer is what separates "comfortably out of the money" from pin risk, where
    assignment becomes unpredictable and closing is the correct move.

    Re-evaluated on every monitor cycle, so a drift toward a short strike
    reinstates the close on its own.
    """
    try:
        sp, sc = float(c["short_put"]), float(c["short_call"])
    except (TypeError, ValueError, KeyError):
        return False
    if not spot or spot <= 0 or sp <= 0 or sc <= 0 or sc <= sp:
        return False
    return ((spot - sp) / spot >= buffer_pct
            and (sc - spot) / spot >= buffer_pct)


def _condor_entry_client_id(ns: str, cid: int) -> str:
    """Entry client order ID. `ns` is the per-database namespace, so a recreated
    DB's row ids can never collide with orders already on the broker account."""
    return f"sm-{ns}-condor-{cid}-entry"


def _condor_wing_width(c: dict) -> float:
    """Widest contractual payoff, used as a close-price ceiling.

    A price ceiling bounds the debit we authorize; it does not guarantee a fill.
    """
    widths = []
    for a, b in (("short_put", "long_put"), ("long_call", "short_call")):
        try:
            widths.append(abs(float(c[a]) - float(c[b])))
        except (TypeError, ValueError, KeyError):
            continue
    return max(widths) if widths else 0.0


def _expiry_force_close_due(expiry, now=None) -> bool:
    """Whether an expiry-day condor must close before its actual session close.

    The configured 15:00 ET deadline is preserved on a normal 16:00 session.
    A 13:00 early-close session instead forces at 12:00, rather than discovering
    the assignment risk after the market has already shut.
    """
    from datetime import date as _date
    from feeds.uw_budget import market_close_minute
    from market_time import et_now
    try:
        exp = _date.fromisoformat(str(expiry))
    except (TypeError, ValueError):
        return False
    n = et_now(now)
    if exp != n.date():
        return False
    close_minute = market_close_minute(n)
    if close_minute is None:
        return False
    configured_minute = max(0, int(settings.iv_exec_force_close_hour)) * 60
    force_minute = min(configured_minute, max(0, close_minute - 60))
    return n.hour * 60 + n.minute >= force_minute


async def _reconcile_condor_expiry(c: dict, legs: list, remaining: int):
    """Only finalize worthless expiry after the broker confirms every leg.

    Assignment/exercise changes the account's stock holdings and requires a
    reviewed accounting decision; stock-close intrinsic is not an actual fill.
    """
    cid = c["id"]
    activities = await asyncio.to_thread(trader.get_option_activities, c["opened_at"])
    positions = await asyncio.to_thread(trader.get_positions_raw)
    note = "Awaiting broker option-expiration activities"
    if activities is None or positions is None:
        await db.await_condor_settlement(cid, note)
        return
    symbols = {l["symbol"] for l in legs}
    events = [a for a in activities if a.get("symbol") in symbols
              and a.get("status") == "executed"]
    if any(a.get("activity_type") in ("OPASN", "OPEXC", "OPXRC") for a in events):
        note = f"Condor #{cid} {c['ticker']}: assignment/exercise requires settlement review"
        await db.await_condor_settlement(cid, note)
        risk = await db.get_risk_state()
        if not risk["halted"]:
            await db.set_halt(note)
            logger.error(note)
            if telegram.enabled:
                await telegram.send_info(note)
        return
    if any(p.get("symbol") in symbols and float(p.get("qty") or 0) != 0 for p in positions):
        await db.await_condor_settlement(cid, note)
        return
    # Activity IDs deduplicate broker pages; exact quantities avoid assigning
    # another structure's expiration to this condor.
    seen, expired = set(), {}
    for a in events:
        if a.get("activity_type") != "OPEXP" or str(a.get("date", ""))[:10] != c["expiry"]:
            continue
        if not a.get("id") or a["id"] in seen:
            continue
        seen.add(a["id"])
        try:
            q = abs(float(a["qty"]))
            from math import isfinite
            if not isfinite(q):
                continue
            expired[a["symbol"]] = expired.get(a["symbol"], 0) + q
        except (KeyError, TypeError, ValueError):
            continue
    if not all(expired.get(l["symbol"], 0) == remaining * l.get("ratio_qty", 1) for l in legs):
        await db.await_condor_settlement(cid, note)
        return
    pnl = float(c.get("close_pnl") or 0) + c["credit"] * 100 * remaining
    debit = c["credit"] - pnl / (100 * c["qty"])
    await db.close_condor(cid, max(0, debit), pnl)
    logger.info("Condor #%s broker-confirmed worthless expiry; P&L $%+.2f", cid, pnl)
    await _condor_risk_check(pnl)


async def _manage_condor(c: dict):
    """Confirm entry fill, then close on profit target / after the print."""
    import json as _json
    from datetime import date as _date
    from signals.iv_variants import settlement_ready
    loop = asyncio.get_event_loop()
    legs = _json.loads(c["legs_json"])
    cid, qty, credit = c["id"], int(c["qty"]), float(c["credit"])

    # ── Confirm the parent entry filled before managing it ──
    # A submitted MLeg is not a position.  Do not submit an inverse close while
    # it is pending, and use the actual net credit/filled quantity once it is
    # terminal.  This also handles legacy rows created before pending_entry.
    entry_terminal = {"filled", "canceled", "expired", "rejected"}
    needs_entry_sync = (
        c["status"] == "pending_entry"
        or (c["status"] == "open" and str(c.get("entry_status") or "").lower() not in entry_terminal)
    )
    if needs_entry_sync:
        if not c.get("entry_order_id"):
            found = await asyncio.to_thread(
                trader.get_order_by_client_id, _condor_entry_client_id(await db.order_namespace(), cid))
            if not found.get("id"):
                logger.warning("Condor #%s awaiting entry submission reconciliation", cid)
                return
            c = {**c, "entry_order_id": found["id"]}
            await db._exec("UPDATE iv_condors SET entry_order_id=? WHERE id=?", (found["id"], cid),
                           strict=True, expected_rows=1)
        o = await loop.run_in_executor(None, trader.get_order_raw, c["entry_order_id"])
        est = str((o or {}).get("status") or "unknown").lower()
        filled_qty = float((o or {}).get("filled_qty") or 0)
        filled_avg = float((o or {}).get("filled_avg_price") or 0)
        if est not in entry_terminal:
            await db.update_condor_entry_status(cid, est)
            if _expiry_force_close_due(c.get("expiry")) and est != "pending_cancel":
                await asyncio.to_thread(trader.cancel_order_raw, c["entry_order_id"])
            return
        if filled_qty <= 0:
            await db.void_condor(cid, est)
            logger.info(f"IV-exec condor #{cid} {c['ticker']} voided (entry {est}, no fill)")
            return
        try:
            actual = await db.activate_condor(cid, filled_qty, filled_avg, est)
        except ValueError as e:
            logger.warning(f"IV-exec condor #{cid} has unusable fill data: {e}")
            return
        qty, credit = actual["qty"], actual["credit"]
        c = {**c, "status": "open", "qty": qty, "credit": credit}
        logger.info(f"IV-exec condor #{cid} {c['ticker']} filled x{qty} "
                    f"credit ${credit:.2f}, max loss ${actual['max_loss']:.0f}")

    # ── Decide whether to close ──
    edate = None
    try:
        edate = _date.fromisoformat(c["earnings_date"]) if c.get("earnings_date") else None
    except Exception:
        edate = None
    from market_time import et_now, et_today
    expiring = False
    try:
        expiring = _date.fromisoformat(str(c.get("expiry"))) <= et_today()
    except (TypeError, ValueError):
        expiring = False
    forced = _expiry_force_close_due(c.get("expiry"), et_now())
    post_earnings = edate is not None and et_today() > edate

    # ── Reconcile an already-submitted close before reading another quote ──
    # A filled/expired option is often no longer returned by the live chain. The
    # broker's order is authoritative, so its fill check must not depend on an
    # option quote response.
    if c["status"] == "closing":
        if not c.get("close_order_id"):
            if not c.get("close_client_order_id"):
                logger.error("Condor #%s closing without an order identity; review required", cid)
                return
            found = await asyncio.to_thread(trader.get_order_by_client_id, c["close_client_order_id"])
            if not found.get("id"):
                logger.warning("Condor #%s awaiting close submission reconciliation", cid)
                return
            c = {**c, "close_order_id": found["id"]}
            await db.mark_condor_closing(cid, found["id"], c["close_client_order_id"])
        o = await asyncio.to_thread(trader.get_order_raw, c["close_order_id"])
        st = (o or {}).get("status")
        if st == "filled" or float((o or {}).get("filled_qty") or 0) > 0:
            try:
                filled_qty = float(o["filled_qty"])
                exit_debit = abs(float(o.get("filled_avg_price")))
                c = await db.record_condor_close_fill(cid, c["close_order_id"], filled_qty, exit_debit)
            except (KeyError, TypeError, ValueError):
                logger.warning("Condor #%s has unusable close fill data; retrying", cid)
                return
            if c["closed_qty"] == qty:
                pnl = c["close_pnl"]
                average_debit = credit - pnl / (100 * qty)
                await db.close_condor(cid, average_debit, pnl)
                await _condor_risk_check(pnl)
                return
        if st in ("canceled", "expired", "rejected"):
            await db._exec(
                "UPDATE iv_condors SET status='open', close_order_id=NULL, close_client_order_id=NULL WHERE id=?", (cid,),
                strict=True, expected_rows=1)
            c = {**c, "status": "open", "close_order_id": None}
        else:
            # Reprice only after cancellation is acknowledged on a later cycle.
            # The expiry deadline applies to orders already in flight as well.
            from datetime import datetime, timezone
            age = 0
            try:
                submitted = datetime.fromisoformat(o.get("created_at", "").replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - submitted.replace(tzinfo=submitted.tzinfo or timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                pass
            ceiling = max(_condor_wing_width(c), 0.05)
            old_limit = float((o or {}).get("limit_price") or 0)
            if st not in ("pending_cancel", "pending_replace") and is_rth_now() and (
                    (forced and old_limit < ceiling) or (not forced and age >= 300)):
                await asyncio.to_thread(trader.cancel_order_raw, c["close_order_id"])
            return

    qty = int(c["qty"]) - int(c.get("closed_qty") or 0)
    if qty <= 0:
        return
    if c["status"] == "awaiting_settlement" or (expiring and settlement_ready(c.get("expiry"))):
        await _reconcile_condor_expiry(c, legs, qty)
        return

    # Every remaining decision uses a live option book. A missing response makes
    # debit None; the forced expiry path below can still close at wing width.
    quotes = await loop.run_in_executor(None, trader.get_option_quotes,
                                        [l["symbol"] for l in legs]) or {}
    debit = _condor_close_debit(legs, quotes)

    # Everything except a forced expiry close needs a live executable book.
    # MU's post-print close was estimated at $8.07 from an overnight mid and
    # filled at $5.09 the next morning: a $596 swing on noise that happened to
    # break our way. The same noise sets a limit too low to fill, or fakes a
    # profit target off a stale print.
    if c["status"] == "open" and not forced:
        if not is_rth_now():
            logger.debug("IV-exec condor #%s close deferred: outside RTH", cid)
            return
        if debit is None:
            logger.debug("IV-exec condor #%s close deferred: book not executable", cid)
            return

    tp_hit = debit is not None and debit <= (1 - settings.iv_exec_tp_pct) * credit

    # ── Hold an expiring, comfortably-OTM condor instead of paying to close ──
    if c["status"] == "open" and expiring and (tp_hit or post_earnings or forced):
        spot = await loop.run_in_executor(None, feed.get_latest_quote, c["ticker"])
        spot_px = None
        if isinstance(spot, dict):
            b, a = float(spot.get("bid") or 0), float(spot.get("ask") or 0)
            spot_px = spot.get("mid") or ((b + a) / 2 if (b and a) else (b or a))
        if _condor_expiry_hold(c, spot_px, settings.iv_exec_expiry_hold_buffer_pct):
            logger.info(
                f"IV-exec condor #{cid} {c['ticker']} holding to expiry: spot "
                f"${spot_px:.2f} is >={settings.iv_exec_expiry_hold_buffer_pct:.0%} "
                f"inside {c['short_put']}–{c['short_call']}, so all four legs "
                f"should expire worthless for the full ${credit * 100 * qty:,.0f} "
                f"credit rather than paying ~${(debit or 0) * 100 * qty:,.0f} to close")
            return

    if c["status"] == "open" and not (post_earnings or tp_hit or forced):
        return

    # Submit the close (marketable-ish debit limit) and mark closing.
    reason = "TP" if tp_hit else ("expiry" if forced and not post_earnings else "post-earnings")
    if forced or debit is None:
        # At the expiry deadline, use the bounded-risk ceiling even when a
        # quote exists. A pending TP order must not strand this close.
        # Only reachable on the forced expiry-day path. Cap the limit at the wing
        # width: a condor cannot cost more than that to buy back, so this fills
        # at or better than the worst case instead of guessing a price.
        limit = round(max(_condor_wing_width(c), 0.05), 2)
        reason += " (no book — wing-width limit)"
    else:
        # The debit is already at the touch, so it needs only a small cushion for
        # movement between quote and submit — but it must be at least one tick.
        # A percentage cushion vanishes on penny-priced options: 5% of $0.07 is
        # $0.0035, which rounds to zero and leaves the limit exactly AT the
        # touch, so NKE's close sat unfilled on expiry day. Never more than the
        # wing width, the most a condor can cost to buy back.
        base = max(debit, 0.01)
        limit = round(min(max(base * 1.05, base + 0.01),
                          max(_condor_wing_width(c), 0.05)), 2)
    from uuid import uuid4
    client_id = f"sm-condor-{cid}-{uuid4().hex[:16]}"
    await db.mark_condor_closing(cid, None, client_id)
    res = await loop.run_in_executor(
        None, lambda: trader.close_multileg(legs, qty, limit, client_order_id=client_id))
    if res.get("error"):
        if not res.get("ambiguous", True):
            await db._exec("UPDATE iv_condors SET status='open', close_client_order_id=NULL WHERE id=?", (cid,),
                           strict=True, expected_rows=1)
        logger.warning(f"IV-exec condor #{cid} close submit failed: {res['error']}")
        return
    await db.mark_condor_closing(cid, res.get("id"), client_id)
    logger.info(f"IV-exec condor #{cid} {c['ticker']} closing ({reason}) "
                f"debit{'≈$%.2f' % debit if debit is not None else ' unknown'} "
                f"limit ${limit:.2f} order={res.get('id')}")


async def iv_condor_monitor_loop():
    """Manage open iron condors: confirm fills, take profit, close after prints."""
    await asyncio.sleep(45)
    while True:
        try:
            # Entry arming never disables exits or settlement of existing risk.
            for c in await db.get_active_condors():
                try:
                    await _manage_condor(c)
                except Exception as e:
                    logger.warning(f"Condor #{c.get('id')} manage error: {e}")
                await asyncio.sleep(1)
        except Exception as e:
            logger.warning(f"Condor monitor error: {e}")
        await asyncio.sleep(300)  # every 5 min


async def iv_scanner_loop():
    """Poll IV rank + earnings setup for watchlist tickers every 5 minutes.

    Budget-aware: on weekends nothing moves, so we skip entirely. During
    throttle (UW >80% daily) we double the cycle time. IV snapshots are
    also useless outside market hours on weekdays — we slow to 30 min there.
    """
    import time as _time
    from api.routes import _watchlist
    from feeds.uw_budget import current_session, budget
    from feeds.unusual_whales import iv_summary_from_termstructure
    from signals.earnings_scanner import (scan_ticker as earnings_scan,
                                          is_sell_eligible, is_near_earnings)
    await asyncio.sleep(30)  # give server time to start

    _earnings_last_run: dict[str, float] = {}  # ticker → epoch of last scan

    while True:
        sess = current_session()
        if sess == "weekend":
            await asyncio.sleep(1800)  # check again in 30 min
            continue

        for ticker in list(_watchlist):
            try:
                # ── IV Rank (UW) ────────────────────────────────────────
                # Secondary signal — UW only. Skipped when UW is disabled; the
                # yfinance earnings IV/RV setup below is the primary edge.
                # UW returns a term-structure list, not a dict — reduce it to
                # the 30-day point. UW gives IV *percentile*, not a separate
                # rank, so we use it for both args of score_iv_rank.
                # The UW quota gates only this call. It used to pause the whole
                # loop, stopping the yfinance earnings scan below along with it.
                # Checked per ticker: once a refresh probe has gone out, the rest
                # of the cycle skips quietly instead of logging a block each.
                if settings.uw_enabled and (not budget.should_pause() or budget.probe_due()):
                    iv_data = await uw_client.get_iv_rank(ticker)
                    iv = iv_summary_from_termstructure(iv_data)
                    if iv:
                        iv_pct = iv["iv_percentile"]
                        signal = engine.score_iv_rank(ticker, iv_pct, iv_pct)
                        if signal:
                            await handle_signal(signal)

                # ── Earnings IV/RV setup (yfinance) — max once per 30 min ──
                now = _time.time()
                if now - _earnings_last_run.get(ticker, 0) >= 1800:
                    _earnings_last_run[ticker] = now
                    setup  = await earnings_scan(ticker)
                    signal = engine.score_earnings_setup(setup)
                    # Only act on a scored setup when it's a real earnings play —
                    # a known print within the window, valid IV/RV. Filters the
                    # far-dated / cheap-IV / ETF noise from alerts, evals, and trades.
                    _max_days = settings.iv_setup_max_days_to_earnings
                    eligible = bool(signal) and is_sell_eligible(setup, _max_days)
                    if eligible:
                        await handle_signal(signal)
                        logger.info(
                            f"Earnings setup {ticker}: {setup.recommendation} "
                            f"IV/RV={setup.iv30_rv30:.2f}x score={signal.score} "
                            f"earnings {setup.next_earnings_date}"
                        )
                        # Log for IV/RV edge validation (hypothetical short straddle):
                        # record the implied move now; resolve vs realized after the
                        # actual earnings print (yfinance calendar), so IV crush and
                        # the realized move are both captured. Falls back to +7d when
                        # no earnings date is known.
                        try:
                            from datetime import timedelta as _td, date as _date
                            from market_time import et_today
                            imp = float(str(setup.expected_move or "0").rstrip("%") or 0)
                            edate = setup.next_earnings_date
                            resolve_after = None
                            if edate:
                                try:
                                    resolve_after = (
                                        _date.fromisoformat(edate) + _td(days=2)
                                    ).isoformat()
                                except Exception:
                                    resolve_after = None
                            if not resolve_after:
                                resolve_after = (et_today() + _td(days=7)).isoformat()
                            if await db.record_iv_eval(
                                ticker=ticker, recommendation=setup.recommendation,
                                iv30_rv30=setup.iv30_rv30, implied_move_pct=imp,
                                entry_price=setup.price, resolve_after=resolve_after,
                                earnings_date=edate):
                                logger.info(
                                    f"IV/RV eval logged: {ticker} implied ±{imp:.1f}% "
                                    f"@ ${setup.price:.2f} — earnings {edate or 'n/a'}, "
                                    f"resolves {resolve_after}")
                        except Exception as e:
                            logger.debug(f"IV eval record skipped for {ticker}: {e}")

                        # Phase 2: sell a defined-risk iron condor into the print
                        # (gated by IV_EXEC_ENABLED — no-op until armed).
                        try:
                            await maybe_execute_condor(setup)
                        except Exception as e:
                            logger.warning(f"IV-exec {ticker} error: {e}")

                    # Measurement only (never executed): price the structures for
                    # EVERY near-earnings name — gated or not — so the three gates
                    # can be scored against "sell everything indiscriminately".
                    # If the ungated baseline matches, the gates are noise.
                    try:
                        if is_near_earnings(setup, _max_days) and (
                                eligible or settings.iv_log_ungated_baseline):
                            await log_variant_evals(setup, gate_passed=eligible)
                    except Exception as e:
                        logger.debug(f"variant-log {ticker} skipped: {e}")

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


def _git_push_eval_data(day: str) -> bool:
    """Publish only report snapshots in a temporary clone of remote main.

    Neither the developer's index nor unpushed application commits are used.
    A concurrent remote update fails the fast-forward push; no force push or
    workspace rebase is performed. Returns True when the remote holds these
    snapshots (pushed, or already identical) and False on any failure, so the
    scheduler can retry the publish stage.
    """
    import subprocess
    import tempfile
    from pathlib import Path
    repo = Path(__file__).parent.parent
    files = ["backend/reports/history.jsonl", "backend/reports/trades.csv",
             "backend/reports/latest.json"]
    def git(args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True,
                              capture_output=True, timeout=60)
    try:
        snapshots = {f: (repo / f).read_bytes() for f in files}
        remote = git(["remote", "get-url", "origin"], repo).stdout.decode().strip()
        identity = {k: git(["config", k], repo).stdout.decode().strip()
                    for k in ("user.name", "user.email")}
        with tempfile.TemporaryDirectory(prefix="stonkmonitor-reports-") as tmp:
            checkout = Path(tmp) / "repo"
            git(["clone", "--quiet", "--depth=1", "--single-branch", "--branch", "main",
                 remote, str(checkout)], repo)
            for key, value in identity.items():
                git(["config", key, value], checkout)
            for file, data in snapshots.items():
                (checkout / file).parent.mkdir(parents=True, exist_ok=True)
                (checkout / file).write_bytes(data)
            git(["add", "--", *files], checkout)
            changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=checkout,
                                     capture_output=True, timeout=30)
            if changed.returncode == 0:
                return True
            if changed.returncode != 1:
                raise RuntimeError("Unable to compare report snapshot")
            git(["commit", "-m", f"eval-data: daily report {day}"], checkout)
            git(["push", "origin", "HEAD:main"], checkout)
        logger.info(f"eval-data pushed to GitHub ({day})")
        return True
    except Exception as e:
        logger.warning(f"eval-data git push failed: {e}")
        return False


# ── Daily-report stage tracking ─────────────────────────────────────────────
# The report has four stages: generate → export → publish (git) → notify. The
# scheduler used to treat daily_<day>.html as "done", but that file is written
# first — a failure in any later stage was never retried that day. Each stage's
# outcome is now recorded in daily_<day>.state.json and the scheduler keeps
# going until the state says complete.
def _report_state_path(reports_dir, day: str):
    return reports_dir / f"daily_{day}.state.json"


def _load_report_state(reports_dir, day: str) -> dict:
    import json as _json
    try:
        state = _json.loads(_report_state_path(reports_dir, day).read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_write(path, text: str) -> None:
    """Replace `path` in one step so a reader never sees a half-written file."""
    import os
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _save_report_state(reports_dir, day: str, state: dict) -> None:
    import json as _json
    _atomic_write(_report_state_path(reports_dir, day), _json.dumps(state, indent=2))


def _report_complete(reports_dir, day: str) -> bool:
    """Whether every stage of `day`'s report has finished."""
    if _report_state_path(reports_dir, day).exists():
        return bool(_load_report_state(reports_dir, day).get("complete"))
    # Reports written before stage tracking have no state file; for those the
    # HTML was the completion marker. New runs write the state file first.
    return (reports_dir / f"daily_{day}.html").exists()


async def _finish_report(reports_dir, day: str, state: dict, always_notify: bool) -> None:
    """Run the publish and notify stages that are still pending, then record
    the result. Each stage runs at most once per day unless it failed."""
    if settings.report_git_push and not state.get("published"):
        state["published"] = bool(await asyncio.to_thread(_git_push_eval_data, day))
        if not state["published"]:
            logger.warning(f"Daily report {day}: publish failed — will retry")
    if always_notify or not state.get("notified"):
        try:
            if pushover.enabled:
                state["notified"] = bool(await pushover.send_alert(
                    state.get("title", "StonkMonitor daily check-in"),
                    state.get("summary", "")))
                if not state["notified"]:
                    logger.warning("Daily report %s: notification failed — will retry", day)
            else:
                state["notified"] = True
        except Exception as e:
            state["notified"] = False
            logger.warning(f"Report Pushover send failed: {e}")
    state["complete"] = bool(state.get("exported") and state.get("notified")
                             and (state.get("published") or not settings.report_git_push))
    _save_report_state(reports_dir, day, state)


async def generate_daily_report(is_weekly: bool = False, scheduled: bool = False) -> dict:
    """Build the daily check-in, persist it to backend/reports/, publish it and
    Pushover a one-line summary. Autonomous — no Claude needed. Returns the
    report data (a scheduled Claude routine reads latest.json to republish the
    artifact + surface proposals for approval).

    `scheduled=True` is the scheduler's call: it resumes a report whose files
    were written but whose publish/notify stage failed, without rebuilding it
    and without notifying twice. A manual run (the API route) always rebuilds
    and always notifies.
    """
    from pathlib import Path
    import json as _json
    from daily_report import (build_report_data, render_html, build_watchlist_review,
                              export_history, report_day)
    from api.routes import _watchlist
    from market_time import et_today

    reports_dir = Path(__file__).parent / "reports"
    reports_dir.mkdir(exist_ok=True)

    if scheduled:
        today = et_today().isoformat()
        pending = _load_report_state(reports_dir, today)
        if (pending.get("exported") and not pending.get("complete")
                and (reports_dir / f"daily_{today}.html").exists()):
            await _finish_report(reports_dir, today, pending, always_notify=False)
            return {"resumed": True, "day": today, "complete": pending["complete"]}

    data = await build_report_data(db, trader, thresholds={
        "score": settings.auto_trade_score_threshold,
        "pattern": settings.auto_trade_pattern_threshold,
    })

    # Guard: a transient Alpaca get_account() failure returns equity 0, which
    # would produce a bogus "$0 / -100%" report — and push/email it. Bail out
    # (keeping the last good report) so the scheduler just retries next cycle.
    acct = data["account"]
    if acct.get("error") or not acct.get("equity"):
        logger.warning(
            f"Report skipped — account fetch failed (equity={acct.get('equity')}, "
            f"error={acct.get('error')}). Keeping last good report; will retry.")
        return data

    if is_weekly:
        data["proposals"] = (await build_watchlist_review(db, list(_watchlist))) + data["proposals"]

    day = report_day(data)
    a, m = data["account"], data["metrics"]
    summary = (f"Equity ${a['equity']:,.0f} ({a['total_pnl_pct']:+.2f}%) | "
               f"{m['closed_trades']} closed {m['win_rate']:.0f}%WR | "
               f"{a['open_positions']} open | {len(data['proposals'])} proposal(s)")
    # Record the run as incomplete BEFORE writing the HTML, so a failure below
    # is seen as unfinished rather than mistaken for a completed legacy report.
    state = {"exported": False, "published": False, "notified": False, "complete": False,
             "title": f"StonkMonitor {'weekly' if is_weekly else 'daily'} check-in",
             "summary": summary}
    _save_report_state(reports_dir, day, state)

    html = render_html(data)
    _atomic_write(reports_dir / f"daily_{day}.html", html)
    _atomic_write(reports_dir / "latest.html", html)
    _atomic_write(reports_dir / "latest.json", _json.dumps(data, default=str, indent=2))

    # Durable history export (history.jsonl + trades.csv) → committed to git.
    await export_history(db, reports_dir)
    state["exported"] = True
    _save_report_state(reports_dir, day, state)

    await _finish_report(reports_dir, day, state, always_notify=not scheduled)
    logger.info(f"Daily report generated ({day}): {summary}"
                + ("" if state["complete"] else " — stages pending, will retry"))
    return data


async def report_scheduler_loop():
    """Fire the daily check-in once per weekday at REPORT_HOUR_ET (and the weekly
    watchlist review on Mondays). Checks every 10 min and keeps going until
    every stage of the day's report has completed, so a failed export or push
    is retried rather than waiting for tomorrow. Restart-safe: completion is
    read from the day's state file."""
    from zoneinfo import ZoneInfo
    from datetime import datetime
    from pathlib import Path
    _ET = ZoneInfo("America/New_York")
    await asyncio.sleep(45)
    while True:
        try:
            now = datetime.now(_ET)
            done = _report_complete(Path(__file__).parent / "reports", f"{now:%Y-%m-%d}")
            if (settings.report_enabled and now.weekday() < 5
                    and now.hour >= settings.report_hour_et and not done):
                await generate_daily_report(is_weekly=(now.weekday() == 0), scheduled=True)
        except Exception as e:
            logger.error(f"Report scheduler error: {e}")
        await asyncio.sleep(600)  # re-check every 10 min


# ------------------------------------------------------------------ #
#  App Lifecycle                                                       #
# ------------------------------------------------------------------ #
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()

    # Sweep stale pending trades whose 5-min window elapsed while the process
    # was down. The per-trade expiry is an in-memory task lost on restart, so
    # without this rows can stay 'pending' indefinitely.
    try:
        expired = await db.expire_stale_pending_trades()
        if expired:
            logger.info(f"Startup sweep: expired {expired} stale pending trade(s)")
    except Exception as e:
        logger.error(f"Startup pending-trade sweep failed: {e}")

    # Restore the persisted watchlist into the in-memory list the scanners read.
    try:
        from api.routes import _watchlist
        saved = await db.get_watchlist()
        _watchlist[:] = saved
        if saved:
            logger.info(f"Watchlist restored: {len(saved)} names")
    except Exception as e:
        logger.error(f"Watchlist restore failed: {e}")

    # Ensure the curated earnings universe is on the watchlist (declarative source
    # of truth for the IV/RV scanner — edit signals/earnings_universe.py + restart).
    try:
        from signals.earnings_universe import tickers as _universe_tickers
        existing = set(_watchlist)
        added = 0
        for t in _universe_tickers():
            if t not in existing:
                await db.add_watchlist(t)
                _watchlist.append(t)
                added += 1
        if added:
            logger.info(f"Watchlist: seeded {added} from earnings universe ({len(_watchlist)} total)")
    except Exception as e:
        logger.error(f"Earnings-universe seed failed: {e}")

    logger.info("=" * 60)
    logger.info("  StonkMonitor starting up")
    logger.info(f"  Mode: {'PAPER' if settings.alpaca_paper else 'LIVE'} trading")
    logger.info(f"  DB:       {db.path}")
    logger.info(f"  Discord:  {'enabled' if discord.enabled else 'disabled'}")
    logger.info(f"  Pushover: {'enabled' if pushover.enabled else 'disabled'}")
    logger.info("=" * 60)

    # Start background tasks
    # UW stream + budget monitor are gated on uw_enabled: the earnings IV/RV
    # edge runs on yfinance + Alpaca only, so UW can be cleanly disabled (e.g.
    # after cancelling the sub) with no error spam. Plumbing stays intact.
    uw_task = None
    uw_budget_task = None
    if settings.uw_enabled:
        uw_task = asyncio.create_task(start_uw_stream())
        uw_budget_task = asyncio.create_task(uw_budget_monitor_loop())
    else:
        logger.info("Unusual Whales DISABLED (UW_ENABLED=false) — "
                    "earnings IV/RV scanner runs on yfinance + Alpaca only")
    iv_task = asyncio.create_task(iv_scanner_loop())
    alpaca_monitor_task = asyncio.create_task(alpaca_position_monitor())
    condor_monitor_task = asyncio.create_task(iv_condor_monitor_loop())
    measure_task = asyncio.create_task(measurement_universe_loop())

    async def _warm_earnings_calendar():
        try:
            from feeds.earnings_calendar import _ensure_fresh
            await asyncio.get_event_loop().run_in_executor(None, _ensure_fresh)
            from feeds.earnings_calendar import _cache
            logger.info(f"Earnings calendar warmed: {len(_cache['map'])} names (Nasdaq)")
        except Exception as e:
            logger.warning(f"Earnings calendar warm-up failed: {e}")
    calendar_task = asyncio.create_task(_warm_earnings_calendar())
    perf_sync_task = asyncio.create_task(performance_sync_loop())
    daily_equity_task = asyncio.create_task(daily_equity_loop())
    report_task = asyncio.create_task(report_scheduler_loop())

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

    notifications_task = asyncio.create_task(enable_notifications())
    pattern_engine.set_notifiers(discord, pushover)

    # Wire auto-trade dependencies
    auto_trade.set_dependencies(telegram, db, trader)

    # Seed cached equity immediately so circuit breaker % is correct from the start
    try:
        _startup_account = await asyncio.to_thread(trader.get_account)
        _startup_equity = float(_startup_account.get("equity", 0) or 0)
        if _startup_equity > 0:
            auto_trade._cached_equity = _startup_equity
            logger.info(f"Auto-trade equity seeded: ${_startup_equity:,.2f}")
    except Exception as _e:
        logger.warning(f"Could not seed startup equity: {_e}")

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

    tasks = [t for t in (uw_task, uw_budget_task, iv_task, alpaca_monitor_task,
              condor_monitor_task, measure_task, perf_sync_task, daily_equity_task,
              report_task, calendar_task, notifications_task, kalshi_task,
              kalshi_monitor_task) if t is not None]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
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

from api.local_access import LocalAccessMiddleware, local_access_allowed
app.add_middleware(LocalAccessMiddleware, origins=settings.cors_origins.split(","))

app.include_router(router, prefix="/api")


# ------------------------------------------------------------------ #
#  WebSocket Endpoint                                                  #
# ------------------------------------------------------------------ #
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if not local_access_allowed(ws, {x.strip() for x in settings.cors_origins.split(",")}):
        await ws.close(code=1008)
        return
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

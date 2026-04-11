# CLAUDE.md — StonkMonitor Project Context

This file is read automatically by Claude Code on startup. It gives you full context
about the project so you can pick up where we left off on any machine.

---

## What This Project Is

**StonkMonitor** — a real-time market signal monitor and semi-automated trader.
Two markets: traditional equities/options via **Alpaca** + prediction markets via **Kalshi**.

- Ingests Unusual Whales live feed (options flow, dark pool, insider, congress trades)
- Scores every event 1–10 and broadcasts to a Next.js dashboard via WebSocket
- Detects cross-feed patterns (e.g. sweep + dark pool + insider on same ticker)
- Auto-trade engine: signal ≥ 8.5 → Telegram card → one-tap Alpaca execution
- Kalshi scanner: surfaces prediction market opportunities → Telegram → one-tap buy
- Position monitor: watches Kalshi holdings, alerts at 3x/5x/10x gain with sell buttons
- Earnings scanner: Yang-Zhang IV/RV analysis identifies premium-selling setups

Owner: francisco (franesqu on Telegram, @franciscoa19 on GitHub)
Repo: https://github.com/franciscoa19/stonkmonitor

---

## Stack

```
Backend:  Python 3.13, FastAPI, uvicorn, aiohttp, aiosqlite
Frontend: Next.js 14, Tailwind CSS, TypeScript (dark terminal theme)
DB:       SQLite (stonkmonitor.db) — 7 tables
Broker:   Alpaca (paper + live), Kalshi (live, RSA-PSS auth)
Data:     Unusual Whales API, yfinance (for earnings scanner)
Alerts:   Telegram bot (@stonktracker69_bot), Discord webhook, Pushover
```

---

## Running Locally

```bash
# Backend (from backend/)
python -m uvicorn main:app --host 0.0.0.0 --port 8000

# Frontend (from frontend/)
npm run dev   # → http://localhost:3000
```

**Python path on this machine:** `C:\Users\franc\AppData\Local\Programs\Python\Python313\python.exe`
**No virtualenv** — packages installed globally into Python313.

---

## Environment Variables (`backend/.env`)

```env
UNUSUAL_WHALES_API_KEY=<your_uw_key>
ALPACA_API_KEY=<your_alpaca_key>
ALPACA_SECRET_KEY=<your_alpaca_secret>
ALPACA_PAPER=true
DISCORD_WEBHOOK_URL=<your_webhook_url>
TELEGRAM_BOT_TOKEN=<your_bot_token>
TELEGRAM_CHAT_ID=<your_chat_id>   # resolved automatically on startup from getUpdates
KALSHI_KEY_ID=<your_kalshi_key_id>
KALSHI_PRIVATE_KEY=<path_to_kalshi_private.pem>
KALSHI_DEMO=false
KALSHI_SCAN_INTERVAL=60
```

`.env` and `*.pem` are gitignored. On a new machine, copy `.env.example` → `.env`
and place the RSA private key at the path in `KALSHI_PRIVATE_KEY`.

---

## Key Files

### Backend

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app, lifespan, all background tasks, WebSocket broadcast |
| `config.py` | Pydantic settings loaded from `.env` |
| `db.py` | aiosqlite wrapper — 7 tables (signals, options_flow, dark_pool, insider_trades, congress_trades, pending_trades, watchlist) |
| `feeds/unusual_whales.py` | UW REST polling — **sequential per channel** (2s gap) to avoid 3-concurrent limit |
| `feeds/kalshi.py` | Kalshi REST client with RSA-PSS signing |
| `feeds/alpaca_feed.py` | Alpaca market data |
| `signals/engine.py` | Signal scorer — converts raw events to Signal objects (1–10) |
| `signals/patterns.py` | Cross-feed pattern detector (9 patterns, up to score 10) |
| `signals/auto_trade.py` | Alpaca auto-trade: sizes positions, queues trades, handles confirm/skip |
| `signals/kalshi_scanner.py` | Kalshi opportunity surfacer (4 types: near_certain, high_vol_extreme, mover, active) |
| `signals/earnings_scanner.py` | Yang-Zhang IV/RV earnings premium-selling screener (from trade calculator) |
| `notifications/telegram.py` | Telegram bot — send_trade_alert, send_kalshi_alert, send_kalshi_position_alert, long-poll loop |
| `notifications/discord.py` | Discord webhook notifier |
| `notifications/pushover.py` | Pushover push notifications |
| `trading/alpaca_trader.py` | Alpaca order execution |
| `api/routes.py` | FastAPI REST routes |
| `api/websocket.py` | WebSocket manager (broadcast_signal, broadcast_feed, broadcast) |

### Frontend (`frontend/src/`)

| File | Purpose |
|------|---------|
| `app/page.tsx` | Main dashboard — tab bar, filter bar, WS hook |
| `components/SignalFeed.tsx` | Live scored signal stream |
| `components/Analytics.tsx` | Pattern hits + ticker deep-dives |
| `components/History.tsx` | Persisted signal DB browser |
| `components/TradeQueue.tsx` | Alpaca pending trades with countdown timers |
| `components/TradePanel.tsx` | Positions + manual order entry |
| `components/Watchlist.tsx` | IV scanner watchlist |
| `components/KalshiPanel.tsx` | Kalshi opportunities tab (filter chips, execute buttons) |
| `lib/useWebSocket.ts` | WS hook — routes signal/feed/kalshi_scan/trade_queued messages |

---

## Architecture: How Data Flows

```
Unusual Whales WebSocket
    → process_uw_event()
        → db.save_*()                    (persist raw event)
        → manager.broadcast_feed()       (→ frontend raw feed)
        → engine.process_event()         (score 1-10)
            → handle_signal()
                → signal_store (ring buffer 500)
                → manager.broadcast_signal()   (→ frontend signal feed)
                → db.save_signal()             (if score >= 7)
                → discord/pushover alert       (if score >= threshold)
                → auto_trade.evaluate_signal() (if score >= 8.5)
                    → _queue() → DB + Telegram card + WS
        → pattern_engine.evaluate()     (cross-feed patterns)
            → auto_trade.evaluate_pattern()

Kalshi scan loop (every 60s):
    → kalshi_client.get_markets()       (paginated, all categories)
    → kalshi_scanner.scan()             (4 opportunity types)
    → manager.broadcast(kalshi_scan)    (→ frontend Kalshi tab)
    → telegram.send_kalshi_alert()      (score >= 7, 1hr cooldown per ticker)

Kalshi position monitor (every 2 min):
    → kalshi_client.get_market(ticker)  (for each tracked position)
    → if gain >= 3x/5x/10x → telegram.send_kalshi_position_alert()

IV scanner loop (every 5 min per watchlist ticker):
    → uw_client.get_iv_rank()           (UW IV rank)
    → engine.score_iv_rank()
    → earnings_scanner.scan_ticker()    (every 30 min, yfinance)
    → engine.score_earnings_setup()
```

---

## Kalshi API — Critical Details

**Auth:** RSA-PSS signed requests — NO email/password.
- Sign message: `timestamp_ms + METHOD + /trade-api/v2/path` (strip query string before signing)
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`
- Library: `pycryptodome` (`from Crypto.PublicKey import RSA`)

**Base URLs:**
- Live: `https://api.elections.kalshi.com/trade-api/v2`
- Demo: `https://demo-api.kalshi.co/trade-api/v2`

**Correct endpoint:** `/events?status=open&with_nested_markets=true`
- Do NOT use `/markets` — returns wrong/empty data
- `get_markets()` paginates via `cursor` field until no more pages (up to 5000 markets)

**Real price field names (all dollars, 0.0–1.0):**
- `yes_ask_dollars`, `yes_bid_dollars`, `no_ask_dollars`, `no_bid_dollars`
- `volume_fp` (float), `last_price_dollars`, `previous_yes_ask_dollars`
- `liquidity_dollars` — always 0, useless, do not filter on it

**Order price:** cents integer (1–99) even though market data returns dollars.
- `place_order(price=92)` means 92¢ YES price
- For NO buys: API derives no_price = 100 - yes_price automatically

**Balance:** returned in cents — divide by 100 for dollars.

---

## Unusual Whales API — Critical Details

**Rate limit:** 3 concurrent requests max.
- Fixed by polling channels **sequentially** with 2s gaps (not `asyncio.gather`)
- `Retry-After` header respected on 429s

**Polling:** REST polling (not WebSocket) every 15s per channel.
- Channels: `options-flow`, `darkpool`, `insider-trades`, `congress-trades`

---

## Telegram Bot — Critical Details

**Bot:** @stonktracker69_bot
**Token:** in `.env` as `TELEGRAM_BOT_TOKEN`
**Chat ID:** REDACTED_CHAT_ID (franesqu) — stored in `.env`, resolved on startup

**Key methods:**
- `send_trade_alert(trade)` — Alpaca trade card with ✅ EXECUTE / ❌ SKIP
- `send_kalshi_alert(opp, alert_id)` — Kalshi buy card with ✅ EXECUTE / ❌ SKIP
- `send_kalshi_position_alert(...)` — Position spike card with ✅ SELL ALL / ✂️ SELL HALF / 🚫 HOLD
- `send_info(text)` — plain text, no buttons

**Critical bug fixed:** Telegram rejects `http://localhost:3000` as an inline button URL.
Never put localhost URLs in inline keyboards — the entire sendMessage fails silently.

**Callback routing in `_handle_update()`:**
- `confirm_{id}` → Alpaca execute
- `skip_{id}` → Alpaca skip
- `kalshi_exec_{id}` → Kalshi buy execute
- `kalshi_skip_{id}` → Kalshi buy skip
- `ksell_all_{id}` → Kalshi sell all
- `ksell_half_{id}` → Kalshi sell half
- `ksell_hold_{id}` → Kalshi hold (clear alert, keep tracking)

**Conflict:** Only ONE process can call `getUpdates` at a time. If backend is running,
don't run test scripts that also call getUpdates — you'll get a 409 conflict error.

---

## Kalshi Scanner — Strategy

Markets have 3M+ contracts — efficiently priced. We don't try to model probabilities.
Instead we **surface** interesting markets in 4 categories:

| Type | Criteria | Play |
|------|----------|------|
| `near_certain` | DTE ≤ 30, price ≤ 5¢ or ≥ 95¢ | Buy cheap side for lotto upside, OR buy expensive side for near-guaranteed yield |
| `high_vol_extreme` | vol > 100k, price ≤ 8¢ or ≥ 92¢ | Crowd has decided — fade or follow |
| `mover` | price_move ≥ 8¢, vol > 10k | Momentum or mean-reversion |
| `active` | vol > 500k, price 30–70¢ | Active debate — take a side |

**Score formula:** extremeness × volume × time-urgency + type bonus
**Alerts:** score ≥ 7, 1-hour cooldown per ticker, max 1 per scan cycle

**Position tracking:** When user taps Execute on Telegram:
- `_kalshi_positions[ticker]` stores entry_cents, contracts, side
- Monitor checks `yes_bid_dollars` every 2 min
- Alerts at 3x, 5x, 10x gain — each threshold fires once (won't re-fire same threshold)

---

## Earnings Scanner — Strategy

Adapted from `C:\Users\franc\OneDrive\Documents\trade calculator\calculator.py`

**Three conditions for a sell-premium setup:**
1. `avg_volume >= 1,500,000` — liquidity
2. `iv30_rv30 >= 1.25` — options priced 25%+ above Yang-Zhang realized vol
3. `ts_slope_0_45 <= -0.00406` — term structure inverted (front-month IV spike = earnings)

**Yang-Zhang HV** is better than close-to-close because it uses OHLC data and
correctly handles overnight gaps. The original calculator used it; we kept it verbatim.

**When it fires:** Add tickers to watchlist → scanner runs every 30 min → fires
`earnings_setup` signal (🎯 in feed) with IV30%, RV30%, IV/RV ratio, expected move.

**Play:** Sell ATM straddle before earnings, collect IV crush. Expected move = straddle price ÷ stock price.

---

## Auto-Trade Engine (Alpaca)

**Triggers:** signal score ≥ 8.5 OR pattern score ≥ 9.0

**Options flow:** Builds OCC symbol, fetches live bid/ask from Alpaca data API
(`/v1beta1/options/snapshots`), sizes to min(2% equity, $2,500 cap).

**OCC symbol format:** `TICKER YYMMDD C/P STRIKE*1000 (8-digit zero-padded)`
Example: `SNDK260424C00840000`

**DTE guard:** rejects < 2d or > 21d expiries.

**Equity trades:** insider_buy / congress_trade → market buy, sized to min(2% equity, $2,500).

**Expiry:** pending trades expire after 5 minutes if not confirmed.

---

## Patterns Engine (9 patterns)

| Pattern | Score | Trigger |
|---------|-------|---------|
| `triple_confluence` | 10.0 | Sweep + dark pool + insider aligned |
| `insider_buy_plus_sweep` | 9.5 | CEO open-market buy + bullish sweep |
| `sweep_plus_darkpool` | 9.0 | Institutions in options AND shares |
| `golden_sweep_cluster` | 9.0 | 2+ golden sweeps same ticker in 3d |
| `insider_cluster_buy` | 9.0 | 3+ insiders buying within 30d |
| `congress_plus_sweep` | 8.5 | Congress buy + unusual sweep |
| `size_sweep` | 8.5 | Single sweep > $1M |
| `congress_plus_darkpool` | 8.0 | Congress buy + dark pool accumulation |
| `size_darkpool` | 8.0 | Single dark pool print > $10M |

---

## Database Schema

```sql
signals            -- scored signals (score >= 7 persisted)
options_flow       -- raw UW options flow events
dark_pool_prints   -- raw UW dark pool events
insider_trades     -- raw UW insider trade events
congress_trades    -- raw UW congress trade events
pending_trades     -- Alpaca auto-trade queue (status: pending/confirmed/skipped/expired/failed)
watchlist          -- tickers for IV + earnings scanning
```

---

## Dashboard Tabs

| Tab | Content |
|-----|---------|
| 📡 Watch | Live signal feed — filter by type, min score slider |
| 🗄️ History | Persisted signals from DB, ticker leaderboard |
| 🎯 Patterns | Pattern hits with evidence |
| 💹 Trade | Alpaca trade queue (countdown timers) + positions |
| 🎰 Kalshi | Live opportunity scanner with execute buttons |

**Earnings filter** in the signal feed filters to `earnings_setup` signals only.

---

## Known Issues / Past Bugs Fixed

1. **UW 429 rate limit** — was using `asyncio.gather` (concurrent). Fixed: sequential 2s gaps.
2. **Kalshi wrong endpoint** — `/markets` returns bad data. Must use `/events?with_nested_markets=true`.
3. **Kalshi `status=active`** — returns 400. Use `status=open`.
4. **Kalshi liquidity_dollars** — always 0 in API. Don't filter on it.
5. **Telegram localhost URL** — `http://localhost:3000` in inline buttons causes silent sendMessage failure. Never do this.
6. **Telegram getUpdates conflict** — only one process can long-poll at a time. Stop backend before running test scripts.
7. **Kalshi scanner finding 0 opps** — original approach tried to model probability edge. Markets with 3M+ contracts are efficiently priced. Switched to surfacer strategy.

---

## Recent Work (last session)

- ✅ Kalshi scanner rewritten as opportunity surfacer (4 types)
- ✅ Kalshi Telegram alerts with Execute/Skip buttons (fixed localhost URL bug)
- ✅ Position monitor — tracks buys, alerts at 3x/5x/10x for exits
- ✅ Full market pagination — scans all categories (was capped at 200)
- ✅ Earnings scanner integrated from trade calculator (Yang-Zhang, IV/RV, term structure)
- ✅ KalshiPanel frontend tab with live WS updates and filter chips

---

## TODOs / Next Ideas

- Kalshi position tracking survives restarts (currently in-memory, lost on restart)
- More earnings scanner signal detail in dashboard (show pass/fail table inline)
- Options chain viewer for earnings_setup signals (show the actual straddle to sell)
- Backtest mode for earnings scanner signals
- Alert when Kalshi position goes against you (stop-loss style)
- Auto-execute Kalshi buys without confirmation (optional, flag in .env)

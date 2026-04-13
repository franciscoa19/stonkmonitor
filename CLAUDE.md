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
KALSHI_SCAN_INTERVAL=300
KALSHI_AUTO_EXECUTE=false
# Optional — cross-platform Kalshi ↔ Polymarket arb (leave blank to disable)
DOME_API_KEY=<your_dome_key>
DOME_BASE_URL=https://api.domeapi.io
POLYMARKET_CLOB_URL=https://clob.polymarket.com
CROSS_ARB_MIN_EDGE=0.05
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
| `feeds/unusual_whales.py` | UW REST polling — **session-aware per-channel scheduler**, budget-gated |
| `feeds/uw_budget.py` | UW daily call budget tracker + US/Eastern session classifier (rth/extended/overnight/weekend) |
| `feeds/kalshi.py` | Kalshi REST client with RSA-PSS signing |
| `feeds/dome.py` | Dome API client (Polymarket + Kalshi metadata search, `/v1/polymarket/markets`, `/v1/kalshi/markets`) |
| `feeds/polymarket.py` | Polymarket CLOB client (public, no auth) — `/midpoint`, `/price`, YES-side price helper |
| `feeds/alpaca_feed.py` | Alpaca market data |
| `signals/engine.py` | Signal scorer — converts raw events to Signal objects (1–10) |
| `signals/patterns.py` | Cross-feed pattern detector (9 patterns, up to score 10) |
| `signals/auto_trade.py` | Alpaca auto-trade: sizes positions, queues trades, handles confirm/skip |
| `signals/kalshi_scanner.py` | Kalshi opportunity surfacer (6 types: near_certain, yield_farm, smart_money, high_vol_extreme, mover, active; maker-price limit orders) |
| `signals/kalshi_arb.py` | Internal monotonicity arb (same-event threshold inversions, normalized-prefix grouping) |
| `signals/kalshi_poly_arb.py` | Cross-platform Kalshi ↔ Polymarket arb via Dome (Jaccard+SequenceMatcher title match, threshold 0.70) |
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
Unusual Whales REST polling (session-aware, budget-gated):
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

Kalshi scan loop (every 5 min):
    → kalshi_client.get_markets()       (paginated, all categories)
    → kalshi_scanner.scan()             (6 opportunity types)
    → manager.broadcast(kalshi_scan)    (→ frontend Kalshi tab)
    → telegram.send_kalshi_alert()      (score >= 7, 1hr cooldown per ticker)
    → kalshi_arb_scanner.scan()         (monotonicity arb on threshold markets)
    → cross_arb_scanner.scan()          (Kalshi ↔ Polymarket arb via Dome)
    → manager.broadcast(kalshi_arb)     (→ frontend + Telegram if edge >= 3¢)

Kalshi position monitor (every 2 min):
    → kalshi_client.get_market(ticker)  (for each tracked position)
    → if gain >= 3x/5x/10x → telegram.send_kalshi_position_alert()

IV scanner loop (every 5 min RTH, 15 min extended, 30 min overnight, off weekends):
    → uw_client.get_iv_rank()           (UW IV rank)
    → engine.score_iv_rank()
    → earnings_scanner.scan_ticker()    (every 30 min, yfinance)
    → engine.score_earnings_setup()

UW budget monitor (every 10 min):
    → budget.status()                   (daily count, limit, usage %, session)
    → manager.broadcast(uw_budget)      (→ frontend)
    → telegram.send_info()              (warns at 80% / 95%)
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

**Rate limit:** 3 concurrent requests + **15,000 requests/day** on Standard tier.
- Fixed concurrency by polling channels **sequentially** with 2s gaps (not `asyncio.gather`)
- `Retry-After` header respected on 429s
- `x-uw-daily-req-count` / `x-uw-token-req-limit` response headers tracked live by `feeds/uw_budget.py`

**Polling is session-aware — no longer a flat 15s round the clock.**
Per-channel intervals per session (see `SCHEDULE` in `feeds/uw_budget.py`):

| Session | options-flow | darkpool | insider-trades | congress-trades |
|---------|--------------|----------|----------------|-----------------|
| RTH (M–F 09:30–16:00 ET) | 15s | 15s | 60s | 60s |
| Extended (M–F 04:00–09:30 + 16:00–20:00) | 60s | 60s | 300s | 300s |
| Overnight (M–F 20:00–04:00) | **off** | **off** | 900s | 1800s |
| Weekend | **off** | **off** | 3600s | 3600s |

- **Throttle** at ≥80% daily: all intervals doubled
- **Pause** at ≥95% daily: `stream_flow()` idles 5 min; `_get()` hard-blocks stray callers
- `uw_budget_monitor_loop` logs + broadcasts every 10 min, Telegram warns at 80%/95%
- `GET /api/uw/budget` returns live snapshot
- Projected: ~5,300 calls/weekday, ~48 calls/weekend day (was ~11,250 flat)

---

## Kalshi Arb Scanners

**`signals/kalshi_arb.py` — Internal monotonicity arb**
- Groups markets by `event_ticker` → `(direction, normalized_title_prefix)` so Janet Mills never gets compared to Graham Platner
- Regex handles `above/at least/at or above/greater than/over/more than`, `below/under/less than/at or below`, `X or more/X+`, `before/by end of X`, `between $X and $Y`
- Spread guard: both legs must have spread < 10¢ and non-zero bids
- Conservative edge: `yb_hi - ya_lo` (sell rich @ bid, buy cheap @ ask); min 3¢
- Sum-violation check present but **DISABLED** — fires on non-MECE cumulative brackets
- Broadcasts `{type: "kalshi_arb"}`; Telegram warns when top edge ≥ 3¢

**`signals/kalshi_poly_arb.py` — Cross-platform Kalshi ↔ Polymarket arb**
- Requires `DOME_API_KEY`; silently disabled if blank
- Top 30 Kalshi markets by volume, 1-hour per-ticker match cache
- Similarity = (Jaccard ∪ SequenceMatcher on sorted keyword stream) / 2
- Threshold **0.70** (verified: Newsom 0.73 ✅, Bulgarian president 0.63 ❌)
- Two edge directions: `edge_a = kyb - pya` and `edge_b = pyb - kya`; surfaces with `match_confidence` + **both** full titles for operator verification
- Polymarket price semantics (critical): `/price?side=BUY` returns best **bid**, `/price?side=SELL` returns best **ask** (book-maker perspective). Verified against `/midpoint`. `feeds/polymarket.py` swaps the labels before returning.

Both run every Kalshi scan cycle and are silent most of the time — that's the correct steady state on efficient markets.

---

## Telegram Bot — Critical Details

**Bot:** configured via `TELEGRAM_BOT_TOKEN` in `.env`
**Chat ID:** stored in `.env` as `TELEGRAM_CHAT_ID` — auto-resolved on startup via `getUpdates` if not set

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
Instead we **surface** interesting markets in 6 categories:

| Type | Criteria | Play |
|------|----------|------|
| `near_certain` | DTE ≤ 30, price ≤ 5¢ or ≥ 95¢ | Buy cheap side for lotto upside, OR buy expensive side for near-guaranteed yield |
| `yield_farm` | price 88–94.9¢, DTE ≤ 3d, annualized yield ≥ 100% | Short-dated almost-certain contracts with triple-digit annualized return |
| `smart_money` | volume Z-score ≥ 2.5 vs 20-scan rolling mean + price move ≥ 3¢ | Unusual size hitting against a visible price move |
| `high_vol_extreme` | vol > 100k, price ≤ 8¢ or ≥ 92¢ | Crowd has decided — fade or follow |
| `mover` | price_move ≥ 8¢, vol > 10k | Momentum or mean-reversion |
| `active` | vol > 500k, price 30–70¢ | Active debate — take a side |

**Score formula:** extremeness × volume × time-urgency + type bonus
**Alerts:** score ≥ 7, 1-hour cooldown per ticker, max 1 per scan cycle

**Maker pricing:** For each opportunity the scanner also computes a `maker_price` (limit at bid-side so you earn the spread instead of crossing it). Telegram card shows both ask and maker; execution uses `maker_cents`. Per-ticker volume rolling window: `_volume_history: dict[str, deque]` with `VOL_HIST_LEN=20`.

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
8. **Kalshi arb 126 false positives** — sum_violation was firing on non-MECE cumulative brackets (e.g. "retire before 2027/28/29"). Fix: disabled sum_violation, added `_normalize_prefix` grouping so only markets with identical prefix after number-stripping get compared.
9. **Polymarket bid/ask reversed** — `/price?side=BUY` returns best *bid*, `/price?side=SELL` returns best *ask* (book-maker perspective). Verified against `/midpoint`. `feeds/polymarket.py` swaps labels.
10. **UW API at 75% daily burn** — was polling all 4 channels every 15s round the clock. Fix: session-aware per-channel schedule in `feeds/uw_budget.py` (options/darkpool off overnight + weekends; insider/congress stretched; throttle at 80%, pause at 95%). Weekday burn: 11,250 → ~5,300.

---

## Recent Work

- ✅ Kalshi scanner rewritten as opportunity surfacer (expanded to 6 types — added `yield_farm`, `smart_money`)
- ✅ Maker pricing on Kalshi execute cards (earn the spread, not pay it)
- ✅ Kalshi internal monotonicity arb (`signals/kalshi_arb.py`) with normalized-prefix grouping
- ✅ Cross-platform Kalshi ↔ Polymarket arb (`signals/kalshi_poly_arb.py`) via Dome API + public Polymarket CLOB
- ✅ Similarity scoring combines Jaccard + SequenceMatcher, threshold 0.70
- ✅ **UW API budget governor** — `feeds/uw_budget.py` tracks daily call count via response headers, enforces session-aware per-channel polling cadence, auto-throttles at 80%, auto-pauses at 95%, Telegram warns, `/api/uw/budget` endpoint
- ✅ IV scanner skips weekends, slows in extended/overnight sessions
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

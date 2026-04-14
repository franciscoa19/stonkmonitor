# ⚡ StonkMonitor

A real-time market signal monitor and semi-automated trader across **two markets**: traditional equities/options via Alpaca and prediction markets via Kalshi. Ingests Unusual Whales live feed, scores every event 1–10, detects cross-feed patterns, and fires actionable Telegram alerts with one-tap execution.

![dark terminal dashboard](https://img.shields.io/badge/UI-dark%20terminal-00ff88?style=flat-square) ![python](https://img.shields.io/badge/backend-Python%203.13-3776AB?style=flat-square&logo=python) ![nextjs](https://img.shields.io/badge/frontend-Next.js%2014-black?style=flat-square&logo=next.js) ![alpaca](https://img.shields.io/badge/broker-Alpaca-FFCE00?style=flat-square) ![kalshi](https://img.shields.io/badge/predictions-Kalshi-8B5CF6?style=flat-square)

---

## What it does

### 📡 Market Signal Monitor (Unusual Whales feeds)

| Feed | What it catches |
|------|----------------|
| **Options Flow** | Sweeps, golden sweeps, large block bets |
| **Dark Pool** | Large institutional off-exchange prints |
| **Insider Trades** | Open-market buys/sells by officers & directors |
| **Congress Trades** | Congressional disclosures (STOCK Act filings) |

Every event is scored **1–10** based on premium size, sweep type, insider role, Vol/OI ratio, and IV conviction.

---

### 🧠 Pattern Engine (9 cross-feed patterns)

Correlates signals across all feeds for the same ticker within rolling time windows:

| Pattern | Score | What it means |
|---------|-------|---------------|
| `triple_confluence` | 10.0 | Sweep + dark pool + insider all aligned |
| `insider_buy_plus_sweep` | 9.5 | CEO open-market buy + bullish sweep |
| `sweep_plus_darkpool` | 9.0 | Institutions loading derivatives AND shares |
| `golden_sweep_cluster` | 9.0 | 2+ golden sweeps on same ticker in 3 days |
| `insider_cluster_buy` | 9.0 | 3+ insiders buying within 30 days |
| `congress_plus_sweep` | 8.5 | Congress buy + unusual options sweep |
| `size_sweep` | 8.5 | Single sweep > $1M |
| `congress_plus_darkpool` | 8.0 | Congress buy + dark pool accumulation |
| `size_darkpool` | 8.0 | Single dark pool print > $10M |

---

### 💹 Alpaca Auto-Trade Flow

```
Signal ≥ 8.5 or Pattern ≥ 9.0
        ↓
Auto-Trade Engine
  • fetches live bid/ask from Alpaca data API
  • builds OCC options symbol automatically
  • sizes position: min(2% equity, $2,500 hard cap)
  • DTE guard: rejects <2d or >21d expiries
        ↓
Telegram card on your phone
  [✅ EXECUTE $840]  [❌ SKIP]
        ↓ tap
Alpaca limit order placed instantly
```

**Position Monitor (TP/SL)** — runs every 2 min during market hours:

| Trigger | Action | Notification |
|---------|--------|--------------|
| **+80% gain** | Sell 50% (market order) | 🎯 TAKE PROFIT → Telegram |
| **-20% loss** | Sell 50% (trim) | ✂️ TRIM → Telegram |
| **-40% loss** | Liquidate 100% | 🛑 STOP LOSS → Telegram |

All thresholds configurable via `.env` (`POS_TP_PCT`, `POS_TRIM_PCT`, `POS_SL_PCT`, etc). Each action fires once per position — no double-triggers. Pauses overnight and weekends.

---

### 🎰 Kalshi Prediction Market Scanner

Scans **all open Kalshi markets** (paginated, 5000+ markets) every 5 minutes. Surfaces six types of opportunities for human evaluation — the scanner doesn't claim to beat efficiently priced markets, it finds the *interesting* ones:

| Type | Criteria | Play |
|------|----------|------|
| 🔒 **Near Certain** | DTE ≤ 30d, price ≤ 5¢ or ≥ 95¢ | Buy cheap side for lotto upside on a catalyst, or farm near-guaranteed yield on the certain side |
| 🌾 **Yield Farm** | price 88–94.9¢, DTE ≤ 3d, annualized yield ≥ 100% | Short-dated almost-certain contracts where the bid/ask implies triple-digit annualized return |
| 🐋 **Smart Money** | volume Z-score ≥ 2.5 vs 20-scan rolling mean + price move ≥ 3¢ | Unusual size hitting a market against a visible price move — someone knows something |
| 🔥 **High Vol Extreme** | vol > 100k, price ≤ 8¢ or ≥ 92¢ | Crowd has made a strong call — fade or follow |
| 📈 **Mover** | price moved ≥ 8¢, vol > 10k | Momentum or mean-reversion on a catalyst |
| ⚖️ **Active** | vol > 500k, price 30–70¢ | Active debate — research and take a side |

**Maker pricing**: when the spread is fat enough, the scanner proposes a limit at the bid-side instead of crossing the spread, so you earn the spread instead of paying it. The Telegram card shows both the ask and the maker limit.

**Telegram alert flow:**
```
Scanner finds score ≥ 7.0 opportunity
        ↓
Telegram card (1-hour cooldown per ticker)
  [✅ EXECUTE $X.XX]  [❌ SKIP]
        ↓ tap Execute
Kalshi limit order placed instantly
        ↓
Position registered for monitoring

Every 2 minutes:
  Price check on all held positions
  3x gain → [✅ SELL ALL]  [✂️ SELL HALF]  [🚫 HOLD]
  5x gain → alert again
  10x gain → 🚀 MOON alert
```

---

### ⚖️ Kalshi Arbitrage Scanners

Two independent scanners run every Kalshi scan cycle and surface guaranteed-edge trades. Both are silent most of the time by design — efficient markets mean real arbs are rare, and false positives are worse than misses.

**1. Internal monotonicity arb** (`signals/kalshi_arb.py`)

Kalshi events group related threshold markets (`"above $50k"`, `"above $60k"`, …). For mutually consistent thresholds the YES prices *must* be monotonic: pricier thresholds can't be cheaper. When they're not, it's a mechanical arb.

- Groups markets by `event_ticker`, then by `(direction, normalized_title_prefix)` so *only* true threshold siblings are compared (Janet Mills vs Graham Platner never cross).
- Regex parser handles `above $X`, `at least $X`, `below $X`, `X or more`, `by end of X`, `between $X and $Y`, plus punctuated numbers (`$1,000,000`, `100000`, `3.5%`).
- Requires both legs to have tight spreads (< 10¢) and non-zero bids before flagging — wide stale books are ignored.
- Conservative edge calc: sell the rich leg at its bid, buy the cheap leg at its ask. Minimum 3¢ edge to fire.
- Sum-violation (MECE bucket set totaling < $1) check is present but **disabled** because Kalshi doesn't flag mutual exclusivity in the API — it fires on cumulative brackets.

**2. Cross-platform Kalshi ↔ Polymarket arb** (`signals/kalshi_poly_arb.py`)

Uses the [Dome API](https://domeapi.io) to find Polymarket markets that likely resolve the same question as a high-volume Kalshi market, then fetches live Polymarket prices from the public CLOB and compares.

- Scans top 30 Kalshi markets by volume per cycle (avoids spamming Dome).
- Title-match via Jaccard overlap **+** SequenceMatcher on sorted keyword stream. Short keyword sets are hard-capped at 0.5 so they always reject.
- Similarity threshold **0.70** — deliberately aggressive. Verified against real Polymarket titles (Newsom 0.73 ✅, Bulgarian president 0.63 ❌, Walz 0.33 ❌).
- 1-hour match cache per Kalshi ticker (dedupes Dome lookups).
- Both edge directions considered: `buy_poly_sell_kalshi` when Poly YES ask < Kalshi YES bid, `buy_kalshi_sell_poly` when Kalshi YES ask < Poly YES bid.
- Always surfaces with `match_confidence` and **both** full titles so the operator can sanity-check resolution criteria before executing.

> **Polymarket CLOB quirk** (learned the hard way): `/price?side=BUY` returns the best *bid*, not the ask, because prices are quoted from a book-maker perspective. `feeds/polymarket.py` swaps the labels and validates against `/midpoint`.

---

### 🎯 Earnings IV/RV Scanner

Runs on every watchlist ticker every 30 minutes. Adapted from a Yang-Zhang volatility calculator — identifies when options are expensive relative to realized vol with an inverted term structure (classic pre-earnings setup):

| Condition | Threshold | Meaning |
|-----------|-----------|---------|
| `avg_volume` | ≥ 1.5M (30d) | Enough liquidity to trade |
| `iv30_rv30` | ≥ 1.25 | IV is 25%+ above Yang-Zhang realized vol → rich premium |
| `ts_slope_0_45` | ≤ -0.00406 | Term structure inverted (front-month spike = earnings approaching) |

**All 3 pass → SELL_PREMIUM signal (score 8.0+)** → sell ATM straddle before earnings, collect IV crush.
**ts_slope + 1 other → CONSIDER signal (score 6.0)**

Yang-Zhang HV uses OHLC data (handles overnight gaps) — more accurate than close-to-close standard deviation.

---

### 📊 UW API Budget Governor

The Unusual Whales API caps at 15,000 requests/day on the Standard tier and nothing on the feed updates overnight or on weekends. A budget governor tracks usage and adapts poll cadence to the current market session:

| Session | Hours (ET) | Options Flow | Dark Pool | Insider | Congress |
|---------|-----------|--------------|-----------|---------|----------|
| **RTH** | Mon–Fri 09:30–16:00 | 15s | 15s | 60s | 60s |
| **Extended** | Mon–Fri 04:00–09:30 + 16:00–20:00 | 60s | 60s | 5 min | 5 min |
| **Overnight** | Mon–Fri 20:00–04:00 | *off* | *off* | 15 min | 30 min |
| **Weekend** | Sat + Sun | *off* | *off* | 1 hr | 1 hr |

- `feeds/uw_budget.py` parses the `x-uw-daily-req-count` and `x-uw-token-req-limit` headers off every response and tracks daily usage live.
- **Throttle** (≥ 80%): all channel intervals double automatically.
- **Pause** (≥ 95%): the feed idles 5 min at a time; `uw_client._get()` hard-blocks any stray caller (IV scanner, REST endpoints) from burning more calls.
- `uw_budget_monitor_loop` logs + broadcasts status every 10 min and fires a Telegram warning the first time you cross 80% / 95% each day.
- `GET /api/uw/budget` exposes a live snapshot (count, limit, usage %, session, throttle/pause flags).

Projected daily burn: **~5,300 calls weekday**, **~48 calls weekend day** (was ~11,250 round the clock).

---

## Stack

```
backend/
  feeds/
    unusual_whales.py   UW REST polling, session-aware per-channel schedule + budget gating
    uw_budget.py        Daily-call-budget tracker + US/Eastern session classifier
    kalshi.py           Kalshi REST client (RSA-PSS signed, full pagination)
    dome.py             Dome API client (Polymarket + Kalshi metadata lookup)
    polymarket.py       Polymarket CLOB client (public, no auth; /midpoint, /price, /book)
    alpaca_feed.py      Alpaca market data
  signals/
    engine.py           Signal scorer (1-10), all signal types
    patterns.py         Cross-feed pattern detector (9 patterns)
    auto_trade.py       Alpaca trade sizing + queue + execution
    kalshi_scanner.py   Kalshi opportunity surfacer (6 types incl. yield_farm, smart_money)
    kalshi_arb.py       Internal monotonicity arb (same-event threshold inversions)
    kalshi_poly_arb.py  Cross-platform Kalshi ↔ Polymarket arb via Dome
    earnings_scanner.py Yang-Zhang IV/RV earnings premium screener
  notifications/
    telegram.py         Bot with inline Execute/Skip/Sell buttons + long-poll
    discord.py          Rich embed webhooks
    pushover.py         Phone push notifications
  trading/
    alpaca_trader.py    Order execution (paper + live)
  db.py                 SQLite (7 tables via aiosqlite)
  main.py               FastAPI app + WebSocket broadcast + all background tasks
  config.py             Pydantic settings from .env

frontend/               Next.js 14 + Tailwind CSS (dark terminal theme)
  components/
    SignalFeed          Live scored signal stream (with Earnings filter)
    Analytics           Pattern hits + ticker deep-dives
    History             Persisted signal DB browser
    TradeQueue          Pending Alpaca trades with countdown timers
    TradePanel          Positions + manual order entry
    Watchlist           IV + earnings scanner watchlist
    KalshiPanel         Live Kalshi opportunities with execute buttons
```

---

## Quick Start

### Prerequisites
- Python 3.11+
- Node.js 18+
- [Unusual Whales API key](https://unusualwhales.com) (paid)
- [Alpaca account](https://alpaca.markets) (free paper trading)
- [Kalshi account + API key pair](https://kalshi.com/profile/api-keys) (optional)
- Telegram bot token from [@BotFather](https://t.me/BotFather) (optional but recommended)

### 1. Clone & configure

```bash
git clone https://github.com/franciscoa19/stonkmonitor
cd stonkmonitor

cp backend/.env.example backend/.env
# Edit backend/.env with your keys
```

### 2. Backend

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 3. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000)

### 4. Telegram setup
1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → get token
2. Add token to `.env` as `TELEGRAM_BOT_TOKEN`
3. Open your bot in Telegram → send `/start`
4. Bot resolves your chat ID automatically on startup

### 5. Kalshi API key (optional)
1. Go to [kalshi.com/profile/api-keys](https://kalshi.com/profile/api-keys)
2. Generate a key pair → save private key as `backend/kalshi_private.pem`
3. Add to `.env`:
```env
KALSHI_KEY_ID=your-key-uuid
KALSHI_PRIVATE_KEY=/path/to/kalshi_private.pem
KALSHI_DEMO=false
```

---

## Configuration

All thresholds in `backend/.env` — no code changes needed:

### Signal / Alpaca
| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_PREMIUM_ALERT` | 50000 | Min options premium ($) to process |
| `SWEEP_SCORE_THRESHOLD` | 7.0 | Min score for Discord/Pushover alert |
| `AUTO_TRADE_SCORE_THRESHOLD` | 8.5 | Min score to queue Alpaca trade |
| `AUTO_TRADE_MAX_RISK_PCT` | 0.02 | Max % of equity per trade |
| `AUTO_TRADE_MAX_RISK_USD` | 2500 | Hard cap $ per trade |
| `AUTO_TRADE_MIN_DTE` | 2 | Min days to expiry |
| `AUTO_TRADE_MAX_DTE` | 21 | Max days to expiry (no LEAPS) |

### Kalshi
| Variable | Default | Description |
|----------|---------|-------------|
| `KALSHI_DEMO` | false | Use demo sandbox |
| `KALSHI_SCAN_INTERVAL` | 300 | Seconds between market scans |
| `KALSHI_AUTO_EXECUTE` | false | Auto-execute Kalshi buys without Telegram confirmation |
| `KALSHI_MIN_EDGE` | 0.05 | Minimum probability edge (5%) |
| `KALSHI_MAX_BET_USD` | 500 | Hard cap $ per market |

### Cross-Platform Arb (optional)
| Variable | Default | Description |
|----------|---------|-------------|
| `DOME_API_KEY` | *empty* | Dome API key — leave blank to disable cross-platform arb scanner |
| `DOME_BASE_URL` | `https://api.domeapi.io` | Dome API base URL |
| `POLYMARKET_CLOB_URL` | `https://clob.polymarket.com` | Polymarket CLOB base URL (public, no auth) |
| `CROSS_ARB_MIN_EDGE` | 0.05 | Minimum spread (dollars) to surface a Kalshi↔Polymarket arb |

---

## Dashboard Tabs

| Tab | What's there |
|-----|-------------|
| **📡 Watch** | Live signal feed — filter by type (Sweeps / Dark Pool / Insider / Congress / IV / Earnings) + min score slider |
| **🗄️ History** | Persisted signals from DB, top tickers leaderboard |
| **🎯 Patterns** | Pattern hits with evidence, ticker deep-dives |
| **💹 Trade** | Alpaca trade queue with countdown timers + trade history |
| **🎰 Kalshi** | Prediction market opportunities — filter by type, one-click execute |

---

## Telegram Alert Types

| Alert | Buttons | Action |
|-------|---------|--------|
| Alpaca trade | ✅ EXECUTE / ❌ SKIP | Places Alpaca limit order |
| Kalshi buy | ✅ EXECUTE / ❌ SKIP | Places Kalshi limit order |
| Kalshi position spike | ✅ SELL ALL / ✂️ SELL HALF / 🚫 HOLD | Sells contracts at current bid |

---

## Running as a Windows Service

The backend can run as a persistent background service that auto-starts on login and auto-restarts on crash:

| File | Location | Role |
|------|----------|------|
| `start_service.bat` | `backend/` | Restart loop — if uvicorn exits, waits 10s and relaunches. Logs to `backend/logs/service.log` |
| `StonkMonitor.vbs` | Windows `Startup` folder | Launches the bat file hidden (no CMD window) on every Windows login |

**Manage:**
- **Stop**: `taskkill /F /IM python.exe` or find the process in Task Manager → Details
- **Disable auto-start**: delete `StonkMonitor.vbs` from `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`
- **View logs**: `type backend\logs\service.log`
- **Health check**: `curl http://localhost:8000/api/uw/budget`

---

## Disclaimer

This is a personal research tool, not financial advice. Auto-trading real money carries significant risk. Always start with **paper trading** (`ALPACA_PAPER=true`) and Kalshi demo mode (`KALSHI_DEMO=true`) before going live. Past signal performance does not guarantee future results.

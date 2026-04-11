# ⚡ StonkMonitor

A real-time market signal monitor and semi-automated trader across **two markets**: traditional equities/options via Alpaca and prediction markets via Kalshi. Tracks unusual options flow, dark pool prints, insider trades, and congressional disclosures — scoring every signal 1–10 and alerting you on your phone via Telegram with **one-tap execution**.

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

### 🧠 Pattern Engine (9 cross-feed patterns)
Correlates signals across all feeds for the same ticker:

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

### 🎰 Kalshi Prediction Market Scanner
Scans all open Kalshi markets every 5 minutes for **mispriced probability** — contracts where the true likelihood differs meaningfully from what the market is pricing.

**Edge model tiers:**
| Tier | Criteria | Example |
|------|----------|---------|
| 🔒 **Locked** | <12h to close, price ≥ 95¢ | Market has essentially decided |
| 🔴 **High** | Fed/CPI/NFP category, <2d, price ≥ 90¢ | Economic release nearly certain |
| 🟡 **Medium** | High volume, price extreme, <3d | Crowd wisdom at extremes |

**Kelly sizing:** `f = (edge × payout - loss) / payout` — capped at 25% Kelly and $500 hard max per market.

**Auth:** RSA-PSS signed requests (API key, no password stored).

---

## Stack

```
backend/
  feeds/
    unusual_whales.py   UW REST polling (staggered, 15s interval)
    kalshi.py           Kalshi REST client (RSA-PSS signed)
    alpaca_feed.py      Alpaca market data
  signals/
    engine.py           Signal scorer (1-10)
    patterns.py         Cross-feed pattern detector (9 patterns)
    auto_trade.py       Alpaca trade sizing + queue + execution
    kalshi_scanner.py   Probability edge model + Kelly sizer
  notifications/
    telegram.py         Bot with inline Execute/Skip buttons
    discord.py          Rich embed webhooks
    pushover.py         Phone push notifications
  trading/
    alpaca_trader.py    Order execution (paper + live)
  db.py                 SQLite (7 tables via aiosqlite)
  main.py               FastAPI app + WebSocket broadcast

frontend/               Next.js 14 + Tailwind CSS (dark terminal theme)
  components/
    SignalFeed          Live scored signal stream
    Analytics           Pattern hits + ticker deep-dives
    History             Persisted signal DB browser
    TradeQueue          Pending Alpaca trades (confirm/skip)
    TradePanel          Positions + manual order entry
    Watchlist           IV scanner watchlist
```

---

## Quick start

### Prerequisites
- Python 3.11+
- Node.js 18+
- [Unusual Whales API key](https://unusualwhales.com) (paid)
- [Alpaca account](https://alpaca.markets) (free paper trading)
- [Kalshi account + API key](https://kalshi.com/profile/api-keys) (optional)
- Telegram bot token from [@BotFather](https://t.me/BotFather) (optional but recommended)

### 1. Clone & configure

```bash
git clone https://github.com/YOUR_USERNAME/stonkmonitor
cd stonkmonitor

cp backend/.env.example backend/.env
# Edit backend/.env with your keys
```

### 2. Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Mac/Linux

pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 3. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000)

### 4. Activate Telegram
Open Telegram → find your bot → send `/start`  
Chat ID is auto-resolved. Next qualifying trade alert hits your phone as a card with inline Execute/Skip buttons.

### 5. Kalshi API key (optional)
1. Go to [kalshi.com/profile/api-keys](https://kalshi.com/profile/api-keys)
2. Generate a key pair — save the private key as `backend/kalshi_private.pem`
3. Add to `.env`:
```env
KALSHI_KEY_ID=your-key-uuid
KALSHI_PRIVATE_KEY=C:/path/to/kalshi_private.pem
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
| `KALSHI_MIN_EDGE` | 0.05 | Minimum probability edge (5%) |
| `KALSHI_MAX_BET_USD` | 500 | Hard cap $ per market |

---

## Dashboard tabs

| Tab | What's there |
|-----|-------------|
| **📡 Watch** | Live signal feed, filter by type + min score slider |
| **🗄️ History** | Persisted signals from DB, top tickers leaderboard |
| **🎯 Patterns** | Pattern hits with evidence, ticker deep-dives |
| **💹 Trade** | Alpaca trade queue with countdown timers + trade history |

---

## Disclaimer

This is a personal research tool, not financial advice. Auto-trading real money carries significant risk. Always start with **paper trading** (`ALPACA_PAPER=true`) and Kalshi demo mode (`KALSHI_DEMO=true`) before going live. Past signal performance does not guarantee future results.

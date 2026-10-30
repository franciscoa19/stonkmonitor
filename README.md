# StonkMonitor

A self-hosted bot that **sells defined-risk iron condors into earnings announcements** on an
Alpaca **paper** account, and measures whether that edge is real. It runs on a Mac as two
local services (a FastAPI backend and a Next.js dashboard) and trades without anyone tapping
a button.

The project started as a real-time signal monitor (Unusual Whales flow, Kalshi prediction
markets, Telegram one-tap trades). That code is still here, but it is **switched off**: the flow
strategy lost money and was paused in September 2026. What runs today is described below.

> Paper trading only. Nothing here is investment advice. See [Before real money](#before-real-money).

---

## What it does

### 1. Finds earnings setups
Every few minutes during the trading day it scans a curated watchlist of 78 liquid names
(`signals/earnings_universe.py`) whose earnings are within 7 days, and scores each with a
Yang-Zhang implied-vs-realized volatility screen (`signals/earnings_scanner.py`):

| Gate | Threshold | Meaning |
|------|-----------|---------|
| Liquidity | 30-day average volume ≥ 1.5M | tight enough markets to trade |
| Rich options | 30-day implied vol ÷ Yang-Zhang realized vol ≥ 1.25 | options price a bigger move than the stock has been making |
| Earnings bump | term structure slope 0–45 days ≤ −0.00406 | front-month volatility inflated by the print |

All three → **SELL_PREMIUM**. The slope plus one other → **CONSIDER** (borderline). Otherwise **AVOID**.
Earnings dates come from yfinance and the Nasdaq earnings calendar.

### 2. Opens an iron condor (SELL_PREMIUM only)
`signals/iv_executor.py`, `main.maybe_execute_condor`:

- **When:** earnings within 2 days, in regular hours, not in the first 15 minutes after the open
  (opening quotes are not prices). Same-day entry only before a confirmed after-close report.
  Nothing is opened until the bot has run for an hour without the Mac sleeping (also after a
  restart): a shut lid still wakes the Mac briefly every ~15 minutes, and a condor opened in one of
  those wakes would go unmanaged until the next.
- **Structure:** short put and short call at the options' implied move (1.0×), protective wings
  3% beyond, in the first expiry after the print that is 1–10 days out. One multi-leg order.
- **Price:** a limit at 90% of the mid credit; skipped if the credit is under $0.10.
- **Size:** maximum loss = 10% of *current* account equity × the risk throttle; at most 4 contracts
  per $10k of equity.
- **Limits:** at most 3 condors open (pending entries count), 4 new per day, one per ticker, one
  attempt per ticker per day.
- **Borderline setups** (CONSIDER) are logged and scored but not traded (`IV_EXEC_ALLOW_CONSIDER=false`).

**Account-wide limits** (`trading/account_risk.py`) apply to every automated entry: everything at
risk (condors, other holdings, pending orders) stays under 30% of equity, and at least 20% of
equity stays as free cash. They only shrink or skip an entry; they never close a position.

**Risk throttle:** size halves after a loss and doubles back after a win (never above normal,
never below 25%); three losses in a row halt new condors until a person re-arms them.

### 3. Manages and closes it
`main._manage_condor`, every few minutes:

- **Unfilled entry:** cancelled after 60 minutes so it does not hold a slot all day (a partial fill
  is kept). An entry whose submission never reached the broker is voided once its session closes.
- **Profit target:** buy back at half the credit or less. The limit never exceeds the target.
- **After the print:** close from 09:45 ET the morning after the earnings date. Each unfilled
  retry raises the limit a step, up to what the two short legs cost on their own.
- **Expiry day:** hold to expiry if the stock is at least 3% inside both short strikes; otherwise
  close, with a forced close at 15:00 ET (12:00 on a 13:00 half day).
- Worthless long wings are left out of the close (they cannot be sold) and expire on their own.
- Settlement is booked from the broker's own expiry records (`OPEXP`); an assignment halts new entries.

### 4. Measures whether the edge is real
None of this trades; it builds the evidence.

- **Hypothetical structures** (`iv_variant_evals`): for every setup, and for up to 25 extra large
  companies every 6 hours from the Nasdaq calendar, it prices condors at 0.7, 1.0 and 1.3 standard
  deviations, a butterfly and a straddle, and settles them at the expiry close.
- **IV/RV observations** (`iv_rv_evals`): the move the options priced against the move that happened.
- **Gates vs baseline:** whether setups that pass all three gates do better than all events.
- **Market-event tags:** days the whole market moved on news (e.g. a hot CPI print) can be tagged;
  the report then shows its figures with and without records open on those days. Nothing is deleted.
- **Stop what-if study** (`backtest/stop_study.py`): replays closed condors on five-minute history
  under hold-to-expiry, the bot's own rules, a strike-breach stop and a half-max-loss stop.

### 5. Reports
A daily check-in at 08:00 ET on weekdays (`daily_report.py`): equity, condor results, open risk
against the account limits, research hit rates, and proposals (a weekly watchlist review on
Mondays). View it at <http://localhost:8000/api/report/daily?format=html>. With
`REPORT_GIT_PUSH=true` the evaluation data (`backend/reports/history.jsonl`, `latest.json`,
`trades.csv`) is also committed to GitHub as an off-machine backup.

---

## Safety rails

- **One ledger per broker account.** The database is bound to a fingerprint of the Alpaca account
  on first use; the bot refuses to start against a different account (`bind_account.py`).
- **Restart-safe orders.** Every order carries a client order ID saved before it is sent; an
  ambiguous submission is looked up, never resent.
- **Ownership checks.** Broker positions and orders are reconciled against the ledger; anything
  unknown is quarantined and blocks new automated entries.
- **Local only.** Both services listen on 127.0.0.1; the API also checks the Host and Origin headers.
- **Secrets** live in `backend/.env` (owner-only, never committed). Outbound HTTPS trusts certifi's
  CA bundle on every path (`tls_trust.py`).
- **Backups:** `venv/bin/python backup_db.py stonkmonitor.db backups/<name>.db` (online, verified,
  never overwrites).

---

## Switched off (still in the code)

| Feature | Setting | Notes |
|---------|---------|-------|
| Unusual Whales flow, dark pool, insider and congress feeds; signal scoring; 9 cross-feed patterns | `UW_ENABLED=false` | subscription not needed for the condor strategy |
| Flow/pattern auto-trades (options and stock, bracket orders, trailing stops) | `AUTO_TRADE_FLOW_ENABLED=false` | paused after `triple_confluence` went 0-for-5 |
| Kalshi scanner, Kalshi arbitrage, Kalshi ↔ Polymarket arbitrage | no Kalshi / Dome key | |
| Telegram, Discord, Pushover alerts | `TELEGRAM_ALERTS_ENABLED=false`, `DISCORD_ALERTS_ENABLED=false`, no Pushover key | |

The dashboard's tabs (Watch, History, Patterns, Trade, Kalshi) date from that era. The Trade tab
shows the live Alpaca account and positions, including condor legs; the condor strategy itself is
followed through the daily report.

---

## Running it

macOS (Apple Silicon), Python 3.13 via [uv](https://docs.astral.sh/uv/), Node 20 LTS.

```bash
git clone https://github.com/franciscoa19/stonkmonitor && cd stonkmonitor
cp backend/.env.example backend/.env          # add Alpaca paper keys; keep ALPACA_PAPER=true

cd backend
uv venv --python 3.13 venv
uv pip install --python venv/bin/python -r requirements.txt -r requirements-dev.txt
venv/bin/python -m pytest -q                  # the full suite runs offline

cd ../frontend
npm ci --ignore-scripts && npm run build
```

Both services run as launchd agents that start at login, restart on a crash and listen on
127.0.0.1 only; install steps are in [`deploy/README.md`](deploy/README.md).

```bash
launchctl kickstart -k gui/$(id -u)/com.stonkmonitor.backend    # restart (or .frontend)
tail -f backend/logs/backend.log
curl http://localhost:8000/health
```

**The Mac must stay awake during market hours.** The backend holds a sleep assertion
(`caffeinate -i`), but a closed lid or a drained battery still puts the Mac to sleep, and a sleeping
Mac runs nothing: no entries, no profit targets, no post-earnings or expiry-day closes. Keep it on
power with the lid open.

---

## Main settings (`backend/.env`)

| Setting | Default | |
|---------|---------|---|
| `ALPACA_PAPER` | `true` | paper account |
| `IV_EXEC_ENABLED` | `false` | trade condors (the live setup turns this on) |
| `IV_EXEC_ALLOW_CONSIDER` | `false` | also trade borderline setups |
| `IV_EXEC_RISK_PCT` | `0.10` | max loss per condor as a share of equity |
| `IV_EXEC_MAX_POSITIONS` / `IV_EXEC_MAX_PER_DAY` | `3` / `4` | open condors / new per day |
| `IV_EXEC_ENTRY_DAYS_BEFORE` | `2` | enter this many days before earnings at most |
| `IV_EXEC_OPEN_DELAY_MINUTES` | `15` | wait after the opening bell |
| `IV_EXEC_ENTRY_TIMEOUT_MINUTES` | `60` | cancel an unfilled entry (0 = wait for the close) |
| `IV_EXEC_REQUIRE_AWAKE_MINUTES` | `60` | unbroken running time needed before a new entry (0 = off) |
| `IV_EXEC_TP_PCT` | `0.5` | profit target as a share of the credit |
| `IV_EXEC_FORCE_CLOSE_HOUR` | `15` | expiry-day forced close (ET) |
| `ACCOUNT_MAX_RISK_PCT` / `ACCOUNT_CASH_RESERVE_PCT` | `0.30` / `0.20` | account-wide limits |
| `IV_RISK_THROTTLE_ENABLED` | `true` | loss throttle and halt after 3 losses |
| `IV_VARIANTS_LOG_ENABLED`, `IV_MEASURE_UNIVERSE_ENABLED` | | research logging |
| `REPORT_HOUR_ET` / `REPORT_GIT_PUSH` | `8` / `false` | daily report; publish evaluation data to GitHub |

Every setting is in `backend/config.py`. History of the strategy, its bugs and fixes:
[`CLAUDE.md`](CLAUDE.md). Operations: [`deploy/README.md`](deploy/README.md). Research:
[`backend/backtest/README.md`](backend/backtest/README.md).

---

## Before real money

- `REPORT_GIT_PUSH=true` publishes account equity and trades to this **public** repository.
  Turn it off, or point it at a private one.
- A few weeks of paper results are an anecdote, and paper fills are kinder than a live market:
  a live account pays more of the spread on every entry and exit.
- The bot needs a machine that does not sleep or travel.

## Disclaimer

A personal research project, not financial advice. Options can lose more than expected even when
defined-risk. Use paper trading.

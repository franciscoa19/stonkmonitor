# stonkmonitor — Earnings IV/RV Validation Spec

**For:** Codex
**Target:** `backend/signals/` in `franciscoa19/stonkmonitor`
**Existing code:** `backend/signals/earnings_scanner.py` (live scanner, unchanged by this work)

---

## Context

`earnings_scanner.py` currently emits a `SELL_PREMIUM` / `CONSIDER` / `AVOID` signal per ticker
based on three gates:

| Gate | Condition | Constant |
|---|---|---|
| `vol_ok` | 30d avg share volume ≥ 1.5M | `1_500_000` |
| `iv_expensive` | `iv30 / yang_zhang_rv30` ≥ 1.25 | `1.25` |
| `ts_inverted` | `(term(45) - term(dte[0])) / (45 - dte[0])` ≤ -0.00406 | `-0.00406` |

Those three thresholds are **currently unvalidated magic numbers**. Nothing in the repo
establishes that the signal has positive expectancy, and there is no record of how a signal
performed after it fired.

This spec asks for the validation layer, not changes to the live signal. **Do not tune the
thresholds in `earnings_scanner.py` as part of this work.** Produce the evidence first.

---

## Non-negotiable design rules

These exist because the failure mode for a short-premium strategy is a small, steady equity
curve punctuated by a single catastrophic loss. A backtest that reports only total return will
hide exactly the risk that matters.

1. **The backtest harness is fixed and dumb.** It takes a strategy config in, produces metrics
   out. It must not choose parameters, rank strategies, or "recommend" anything. The only
   variable across runs is the config.
2. **Fees and slippage are mandatory and explicit**, not optional flags defaulting to zero.
   Model per-contract commission plus a fill assumption worse than mid (see §3).
3. **Hold out the most recent N quarters** from every optimization run. Any threshold chosen
   on the in-sample period must be reported against the holdout, unchanged.
4. **Report the full metric set on every run.** A run that emits only P&L is a bug.
5. **No look-ahead.** Options and price data must be as-of the signal timestamp. Using an
   IV or close that postdates the entry invalidates the run silently — assert on this.

---

## 1. Event dataset (`backend/backtest/earnings_events.py`)

Build the historical event set the backtest replays.

- Universe: `backend/signals/earnings_universe.py` (78 tickers, tiers 1–3).
- Period: as far back as options data allows; **minimum 5 years**.
- One row per ticker per earnings event:
  - `ticker`, `report_date`, `report_time` (`bmo` / `amc`)
  - `entry_ts` — close of the last session before the print
  - `exit_ts` — open or close of the first session after
  - the three scanner inputs as-of `entry_ts`: `iv30`, `rv30`, `ts_slope`, plus `avg_volume`
  - front-month ATM straddle mid at entry and at exit
  - realized underlying move over the event
- **Target: ≥ 1,200 events.** 78 tickers × 4 prints/yr × 5 yrs ≈ 1,560 before data gaps.

### Why the count matters
The video's headline finding was that several "profitable" strategies had opened **2–4 trades
in six years** and were discarded as statistically meaningless despite excellent returns. The
same trap applies here and is worse: this scanner fires roughly 4×/yr/ticker at most, and the
three gates together will reject most of those. Log the count of events surviving each gate.
**If the all-three-pass population is under ~100 events, the strategy is untestable as
specified** — report that as the finding rather than producing a curve from 30 trades.

---

## 2. Data sourcing

`yfinance` is fine for OHLC (feeds Yang-Zhang, which is stable). It is **not** adequate for
historical options: no term structure history, intermittent zero/None IVs.

- **Earnings calendar:** Finnhub `/calendar/earnings` — free tier, 60 req/min, and critically
  it returns the `hour` field (`bmo`/`amc`/`dmh`). Timing is load-bearing: the entire trade is
  being short the front expiry through the print.
  - FMP's `/earnings-confirmed` is the alternative and is better in one respect — it only
    returns dates the company has actually announced. Unconfirmed dates are the single most
    common way an earnings-premium bot ends up short into the wrong session.
- **Historical options / IV:** needs a real vendor. Polygon options aggregates, ORATS, or
  CBOE DataShop. Put the choice behind an interface — `backend/backtest/data/` with a
  provider protocol — so the vendor can be swapped without touching the harness.
- Cache every fetch to local parquet keyed by `(ticker, date)`. Re-running the backtest must
  not re-hit the API.

---

## 3. Execution model

Short premium dies on fills, not on signal quality.

- Entry: sell the ATM straddle (or the configured structure) at **mid minus half the spread**,
  never at mid.
- Exit: buy back at **mid plus half the spread**.
- Commission: configurable, default $0.65/contract each way, 4 contract-legs per straddle
  round trip.
- Assignment / pin risk: if the underlying closes within one strike increment of the short
  strike at expiry, flag the event. Do not silently assume a clean cash settlement.
- Structures to support: `short_straddle`, `short_strangle` (configurable delta), and
  `iron_condor` (configurable wing width). Tier 3 names in the universe should only ever be
  run defined-risk.

---

## 4. Metrics (`backend/backtest/metrics.py`)

Every run emits **all** of these. Total return alone is not an acceptable output.

- `n_events`, `n_wins`, `win_rate`
- `profit_factor` — gross wins / gross losses
- `avg_win`, `avg_loss`, `largest_single_loss`
- `max_drawdown` (on the event-sequence equity curve)
- `sharpe`, `sortino`
- **`tail_ratio`** — mean of the worst 5% of events vs. mean of the rest. This is the metric
  that catches the short-premium failure mode; a strategy can show a 70% win rate and still
  be negative-expectancy because of it.
- `pct_events_exceeding_implied` — how often realized move > the entry straddle price. Break
  this out by universe tier; expect it to be materially worse for tier 3.
- Per-event log written to parquet so any single trade can be inspected.

### Benchmark
Every run reports against two baselines:
1. **Do nothing** (flat).
2. **Sell every event in the universe indiscriminately**, ignoring all three gates.

Baseline 2 is the one that matters. The gates only justify their existence if filtering
*beats* selling everything on a risk-adjusted basis. If indiscriminate selling produces a
similar profit factor with more events, the gates are noise and should be reported as such.

---

## 5. Threshold sensitivity (`backend/backtest/sweep.py`)

Sweep each gate independently, then jointly:

- `iv_rv_ratio`: 1.00 → 1.60, step 0.05
- `ts_slope`: -0.010 → 0.000, step 0.001
- `avg_volume`: 0.5M → 5M

Output a sensitivity surface, not a single best point. **A threshold that only works in a
narrow band is overfit** — report the width of the plateau around any candidate value. The
existing `-0.00406` is suspiciously precise; the sweep should show whether anything about that
specific value is real or whether the whole region from -0.003 to -0.006 behaves identically.

Run the sweep on the in-sample period only. Then evaluate the selected thresholds on the
holdout **once**, and report both numbers side by side.

---

## 6. Deliverables

1. `backend/backtest/` — harness, data providers, metrics, sweep.
2. `backend/backtest/README.md` — how to run it, what the metrics mean, what the vendor
   requirements are.
3. A results report covering: event counts by gate, the full metric set for the current
   thresholds, the comparison against both baselines, the sensitivity surface, and the
   in-sample vs. holdout numbers.
4. Tests: look-ahead assertion, fee/slippage applied, metrics correct on a hand-built fixture.

---

## Explicitly out of scope

- Changing thresholds in `earnings_scanner.py`.
- Live execution / broker integration.
- Any component that both generates and evaluates a strategy. The harness evaluates; humans
  and the spec decide. Keeping those separate is the point.

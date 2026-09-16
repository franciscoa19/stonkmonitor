# backtest/ — short-premium validation

Partial implementation of `VALIDATION_SPEC.md`. This README states plainly what
exists, what does not, and why — so nobody mistakes a forward test for a
backtest.

## What exists

**`metrics.py`** — the spec's §4 metric set as pure functions over per-event
P&L: `profit_factor`, `avg_win` / `avg_loss`, `largest_single_loss`,
`max_drawdown`, `sharpe`, `sortino`, `tail_ratio`, `pct_events_exceeding_implied`,
plus a `sufficient_sample` rail.

It has no opinions: metrics in, numbers out. It never ranks a strategy or picks
a parameter (spec: "explicitly out of scope").

Today it runs over **live forward-test data** — `iv_variant_evals` (hypothetical
structures priced on the real chain and settled at expiry) and `iv_condors`
(actual paper fills). The same module would serve a historical harness unchanged.

### Why `tail_ratio` is the one to read
Selling premium produces a tidy equity curve punctuated by one disaster. An iron
condor wins ~65-70% of the time *by construction*, so win rate says almost
nothing. `tail_ratio` — mean of the worst 5% of events over the mean of the rest —
is what exposes negative expectancy hiding behind a 70% win rate. Read it as
"one tail event costs N typical events."

### The `sufficient_sample` rail
`MIN_EVENTS_FOR_CONFIDENCE = 100`. The scanner fires at most ~4×/yr/ticker and
the gates reject most of those. Below ~100 resolved events, the honest output is
the shortfall, not a curve through a handful of trades. The daily report surfaces
this so a 2-for-2 start is never read as evidence.

## What does NOT exist, and why

**There is no historical backtest.** Spec §1 asks for ≥1,200 events over ≥5
years. That is blocked on data we do not have:

- `yfinance` is fine for OHLC (it feeds Yang-Zhang) but has **no historical
  options term structure** and returns intermittent zero/None IVs. The three
  gates need `iv30`, `rv30`, and `ts_slope` *as of* each historical entry —
  yfinance cannot supply two of those retroactively.
- Real coverage needs a paid vendor: Polygon options aggregates, ORATS, or CBOE
  DataShop.

Building a harness on top of data that cannot answer the question would produce
a confident-looking curve that means nothing — the precise failure the spec is
written to prevent. So: not built, deliberately.

**Consequence:** the three thresholds in `earnings_scanner.py` — `1.25` (iv/rv),
`-0.00406` (ts_slope), `1_500_000` (volume) — remain **unvalidated magic
numbers**. Nothing here establishes they have positive expectancy. Treat them as
untested until either a vendor backfill or enough forward events exist.

## What we do instead: a forward test with the spec's discipline

We cannot replay 5 years, but we can collect the same evidence going forward,
and the parts of the spec that do not need history are implemented:

| Spec idea | Where |
|---|---|
| §4 full metric set, incl. `tail_ratio` | `metrics.py`, surfaced in the daily report |
| §4 Baseline 2 — *sell everything indiscriminately* | `gate_passed` on `iv_variant_evals`; every near-earnings name is priced, gated or not |
| §3 fees and slippage are mandatory, never zero | `iv_variants.build_variants` — shorts filled at bid, longs at ask, plus round-trip commission |
| §3 pin / assignment risk flagged, not assumed away | `pin_risk` on resolved variant rows |
| §5 the value of a gate is an empirical question | gated-vs-ungated comparison in the report |
| §1 no look-ahead | variants settle on the underlying's **close on the option's expiry**, never a live quote |

### Baseline 2 is the one that matters
The three gates only justify themselves if filtering **beats selling everything**
on a risk-adjusted basis. If indiscriminate selling shows a similar profit factor
across more events, the gates are noise. That is why every near-earnings name is
logged whether or not it passes — the comparison is the experiment.

## Running it

Metrics are computed automatically for the daily report. Directly:

```python
from backtest.metrics import compute_metrics
compute_metrics([120.0, -180.0, 95.0])
```

Tests: `cd backend && ./venv/bin/python -m pytest -q`

## If we license options history later

Add a provider under `backend/backtest/data/` behind a protocol (so the vendor
swaps without touching callers), build the event set per spec §1, cache to
parquet keyed by `(ticker, date)`, and reuse `metrics.py` as-is. The sweep (§5)
should report a **plateau width**, not a best point — a threshold that only works
in a narrow band is overfit, and `-0.00406` is suspiciously precise.

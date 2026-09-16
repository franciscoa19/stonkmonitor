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
(actual paper fills). Variant aggregates include only rows tagged with the
current `conservative_bid_ask_v2` pricing model; earlier rows remain in SQLite
for audit, but are excluded because they used a different fill/fee methodology.
The same module would serve a historical harness unchanged.

### Why `tail_ratio` is the one to read
Selling premium produces a tidy equity curve punctuated by one disaster. An iron
condor wins ~65-70% of the time *by construction*, so win rate says almost
nothing. `tail_ratio` — mean of the worst 5% of events over the mean of the rest —
is what exposes negative expectancy hiding behind a 70% win rate. Read it as
"one tail event costs N typical events." The report also shows the effective
tail count and percentage: below 20 events, "worst 5%" is necessarily one event.

### The `sufficient_sample` rail
`MIN_EVENTS_FOR_CONFIDENCE = 100`. The scanner fires at most ~4×/yr/ticker and
the gates reject most of those. Below ~100 resolved events, the honest output is
the shortfall, not a curve through a handful of trades. The daily report surfaces
this so a 2-for-2 start is never read as evidence.

### Sample size: measured, not estimated
The 78-name tradeable watchlist collects roughly 312 ungated events/year, of
which only ~50–90 pass all three gates — which would leave the gated arm of the
comparison years short of its 100-event rail. That is why the measurement-only
universe exists (`measurement_universe_loop`): the logger risks no capital, so
it can price prints we would never trade.

Measured against the live 60-day Nasdaq calendar (2026-09-16):

| Cap floor | Extra names | With a usable front expiry |
|---|---|---|
| $10B+ | 729 | **133 (18%)** |
| $30B+ | 328 | **102 (31%)** |

Two things follow. First, most large caps **cannot** host this structure: they
list monthlies only, so after a print the next expiry is weeks past the
front-month band. Market cap is not a proxy for options liquidity. Second, the
$10B–$30B band contributes 401 extra candidates for only 31 extra usable names
(a 7.7% hit rate), so `_has_front_expiry` runs first and rejects them for the
price of one contracts call instead of a full scan.

**Upper bound only.** "Usable front expiry" means an expiry exists, not that the
legs have real bids. A live pass found names that clear this check and still
price zero structures once the strict-quote rule refuses to invent a fill. The
post-quote conversion rate is unknown until the October wave supplies volume, so
no completion date is projected from it. The rail stays at 100 events per arm
and the report shows the remaining count rather than a forecast. Each event now
also records structures attempted, structures priced, and dropped variants; the
daily report surfaces that quote-coverage ratio. A priceable result is therefore
conditional on entry liquidity, not a random draw from near-earnings events.

Variant rows retain their source (`watchlist` or `measurement`) as well. The
two populations have different selection rules, so gate results must be checked
within each source before treating a pooled result as generalizable.

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
| §3 fees and slippage are mandatory, never zero | `iv_variants.build_variants` — shorts filled at bid and longs at ask (a missing executable quote skips the structure), plus round-trip commission |
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

"""
SQLite persistence layer.

Tables (one per feed + signals + pattern_hits):
  options_flow    — every UW flow alert
  dark_pool       — every dark pool print
  insider_trades  — P/S/D code insider transactions only
  congress_trades — every congressional disclosure with a ticker
  signals         — scored signals (any score)
  pattern_hits    — fired pattern matches (for dedup + history)
"""
import json
import asyncio
import logging
import math
import aiosqlite
from datetime import datetime, timezone, date, timedelta
from market_time import et_today
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


class DatabaseError(RuntimeError):
    """A required execution-state read or write could not be completed."""


class AccountMismatch(RuntimeError):
    """This database belongs to a different broker account than the one connected."""


class AccountBindingRequired(AccountMismatch):
    """A populated legacy ledger needs an explicit, verified account migration."""


def _et_hour(iso: Optional[str]):
    """Hour-of-day (0-23) in US/Eastern for an ISO timestamp; None if unparseable."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_ET).hour
    except (ValueError, TypeError):
        return None


import re
_OCC_RE = re.compile(r"^[A-Z.]{1,6}\d{6}[CP]\d{8}$")


def _is_occ(symbol: Optional[str]) -> bool:
    """True if `symbol` is an OCC option symbol (→ ×100 contract multiplier)."""
    return bool(_OCC_RE.match(symbol or ""))


def _minutes_between(start_iso: Optional[str], end_iso: Optional[str]):
    """Minutes between two ISO timestamps; None if either is unparseable."""
    try:
        a = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
        if a.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
        if b.tzinfo is None:
            b = b.replace(tzinfo=timezone.utc)
        return round((b - a).total_seconds() / 60.0, 1)
    except (ValueError, TypeError):
        return None

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "stonkmonitor.db"


def resolve_db_path(configured: str = "") -> Path:
    """The database file for this deployment: DB_PATH when unset, and a relative
    setting is taken from backend/ rather than the process's working directory."""
    if not configured or not configured.strip():
        return DB_PATH
    path = Path(configured.strip()).expanduser()
    return path if path.is_absolute() else Path(__file__).parent / path

# The first validation implementation priced some unavailable bid/ask sides at
# mid and did not consistently charge fees. Its rows are retained for audit but
# excluded from forward-test reporting. A new model version makes the reset
# explicit rather than mixing incompatible P&L series.
VALIDATED_VARIANT_PRICING_MODEL = "conservative_bid_ask_v2"
LEGACY_VARIANT_PRICING_MODEL = "legacy_excluded"
VARIANT_SOURCES = ("watchlist", "measurement")

# An implied move read a week before the print is the QUIET front-month IV, not
# the earnings premium — measured at 2-2.4x understatement on CCL/JBL/MU. Events
# captured further out than this cannot answer "did the stock move more than the
# market priced in", so they are reported apart from the headline rather than
# averaged into it. Mirrors settings.iv_variants_reprice_within_days, which the
# report passes in explicitly; this is the fallback for direct/test calls.
MAX_TRUSTED_CAPTURE_LEAD_DAYS = 2

# Shared by API and daily-report metrics. Closing purchases are executions,
# not long entries. Nullable quantities let old ledgers be read until the next
# complete FIFO pass backfills their actual entry and remaining quantities.
LONG_ENTRY_QTY_SQL = (
    "CASE WHEN position_intent='buy_to_close' THEN 0 "
    "ELSE COALESCE(long_entry_qty,filled_qty,0) END")
LONG_ENTRY_PREDICATE = f"side='buy' AND ({LONG_ENTRY_QTY_SQL})>0"
OPEN_LONG_QTY_SQL = (
    "CASE WHEN position_intent='buy_to_close' THEN 0 ELSE COALESCE(open_qty,"
    "CASE WHEN realized_pnl IS NULL THEN filled_qty ELSE 0 END,0) END")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ── Options flow ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS options_flow (
    id              TEXT PRIMARY KEY,          -- UW id field
    ticker          TEXT NOT NULL,
    premium         REAL NOT NULL,             -- total_premium in $
    opt_type        TEXT,                      -- call | put
    alert_rule      TEXT,                      -- GoldenSweep, Sweep, RepeatedHits…
    has_sweep       INTEGER DEFAULT 0,
    strike          REAL,
    expiry          TEXT,
    volume          INTEGER DEFAULT 0,
    open_interest   INTEGER DEFAULT 0,
    vol_oi_ratio    REAL DEFAULT 0,
    iv              REAL DEFAULT 0,
    ask_prem        REAL DEFAULT 0,            -- aggressive buy side
    bid_prem        REAL DEFAULT 0,            -- aggressive sell side
    underlying_price REAL DEFAULT 0,
    sector          TEXT,
    raw             TEXT,
    created_at      TEXT NOT NULL
);

-- ── Dark pool ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dark_pool (
    tracking_id     INTEGER PRIMARY KEY,
    ticker          TEXT NOT NULL,
    size            REAL NOT NULL,             -- shares
    price           REAL NOT NULL,             -- price per share
    premium         REAL NOT NULL,             -- size * price
    nbbo_bid        REAL,
    nbbo_ask        REAL,
    market_center   TEXT,
    executed_at     TEXT,
    raw             TEXT,
    created_at      TEXT NOT NULL
);

-- ── Insider trades ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS insider_trades (
    id              TEXT PRIMARY KEY,          -- UW id field
    ticker          TEXT NOT NULL,
    owner_name      TEXT,
    officer_title   TEXT,
    transaction_code TEXT NOT NULL,            -- P, S, D
    shares          REAL NOT NULL,
    price_per_share REAL DEFAULT 0,
    dollar_value    REAL DEFAULT 0,
    is_officer      INTEGER DEFAULT 0,
    is_director     INTEGER DEFAULT 0,
    is_10b5_1       INTEGER DEFAULT 0,
    transaction_date TEXT,
    filing_date     TEXT,
    raw             TEXT,
    created_at      TEXT NOT NULL
);

-- ── Congress trades ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS congress_trades (
    id              TEXT PRIMARY KEY,          -- politician_id + transaction_date composite
    ticker          TEXT NOT NULL,
    member_name     TEXT,
    chamber         TEXT,                      -- house | senate
    txn_type        TEXT,                      -- Buy | Sell | Exchange
    amounts         TEXT,                      -- "$1,001 - $15,000"
    transaction_date TEXT,
    filed_at_date   TEXT,
    raw             TEXT,
    created_at      TEXT NOT NULL
);

-- ── Signals (all scored signals, not just >= 7) ───────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    score           REAL NOT NULL,
    side            TEXT NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    premium         REAL DEFAULT 0,
    expiry          TEXT,
    strike          REAL,
    option_type     TEXT,
    raw             TEXT,
    created_at      TEXT NOT NULL
);

-- ── Pattern hits ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pattern_hits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_name    TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    score           REAL NOT NULL,
    description     TEXT,
    evidence        TEXT,                      -- JSON list of contributing events
    notified        INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL
);

-- ── Pending trades (auto-trade queue) ────────────────────────────────
CREATE TABLE IF NOT EXISTS pending_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    trade_type      TEXT NOT NULL,       -- "option" | "equity"
    symbol          TEXT NOT NULL,       -- OCC symbol or equity ticker
    side            TEXT NOT NULL,       -- "bullish" | "bearish"
    option_type     TEXT,                -- "call" | "put" | NULL
    strike          REAL,
    expiry          TEXT,
    dte             INTEGER,
    qty             INTEGER NOT NULL,
    limit_price     REAL NOT NULL,
    risk_amount     REAL NOT NULL,
    stop_pct        REAL DEFAULT 40.0,
    target_pct      REAL DEFAULT 80.0,
    score           REAL DEFAULT 0,
    rationale       TEXT,
    strategy        TEXT,                    -- setup that triggered it (signal type or pattern name)
    status          TEXT DEFAULT 'pending',  -- pending/confirmed/skipped/expired/failed
    telegram_msg_id INTEGER,
    alpaca_order_id TEXT,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    executed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_pt_status    ON pending_trades(status);
CREATE INDEX IF NOT EXISTS idx_pt_ticker    ON pending_trades(ticker);
CREATE INDEX IF NOT EXISTS idx_pt_created   ON pending_trades(created_at DESC);

-- ── Trade performance (closed + open position tracking) ─────────────
CREATE TABLE IF NOT EXISTS trade_performance (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    alpaca_order_id TEXT UNIQUE,                   -- Alpaca order UUID (dedup key)
    symbol          TEXT NOT NULL,
    ticker          TEXT NOT NULL,                  -- underlying ticker
    side            TEXT NOT NULL,                  -- buy/sell
    position_intent TEXT,                           -- broker opening/closing intent
    qty             REAL NOT NULL,
    filled_qty      REAL DEFAULT 0,
    filled_avg_price REAL DEFAULT 0,
    order_type      TEXT,                           -- market/limit/stop/trailing_stop
    order_status    TEXT,                           -- filled/canceled/expired/etc
    submitted_at    TEXT,
    filled_at       TEXT,
    -- Position-level P&L (filled in by position monitor actions)
    exit_price      REAL,
    exit_reason     TEXT,                           -- tp1/trailing_stop/tp2/trim/sl/manual
    realized_pnl    REAL,
    realized_pnl_pct REAL,
    -- Metadata
    signal_score    REAL,                           -- original signal score if auto-trade
    trade_type      TEXT,                           -- option/equity
    strategy        TEXT,                           -- setup that triggered it (joined from pending_trades)
    entry_hour_et   INTEGER,                        -- hour-of-day (ET) the entry was submitted
    hold_minutes    REAL,                           -- minutes held (submitted_at -> exit)
    long_entry_qty  REAL,                           -- filled buys left after short covers
    open_qty        REAL,                           -- remaining FIFO long quantity
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tp_symbol    ON trade_performance(symbol);
CREATE INDEX IF NOT EXISTS idx_tp_ticker    ON trade_performance(ticker);
CREATE INDEX IF NOT EXISTS idx_tp_status    ON trade_performance(order_status);
CREATE INDEX IF NOT EXISTS idx_tp_created   ON trade_performance(created_at DESC);

-- Individual broker executions, including canceled/active partial orders.
CREATE TABLE IF NOT EXISTS trade_fills (
    activity_id     TEXT PRIMARY KEY,
    alpaca_order_id TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty             REAL NOT NULL CHECK (qty > 0),
    price           REAL NOT NULL CHECK (price > 0),
    executed_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tf_order ON trade_fills(alpaca_order_id);
CREATE INDEX IF NOT EXISTS idx_tf_symbol_time ON trade_fills(symbol, executed_at, activity_id);

-- Realized FIFO matches keyed by execution, not the entry's aggregate P&L.
CREATE TABLE IF NOT EXISTS realized_trade_exits (
    activity_id TEXT NOT NULL,
    entry_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    ticker TEXT NOT NULL,
    realized_pnl REAL NOT NULL,
    executed_at TEXT NOT NULL,
    PRIMARY KEY (activity_id, entry_order_id)
);

CREATE TABLE IF NOT EXISTS manual_order_requests (
    request_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    client_order_id TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    alpaca_order_id TEXT,
    broker_order_status TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    ticker     TEXT PRIMARY KEY,               -- upper-cased symbol, scanned for IV + earnings
    added_at   TEXT NOT NULL
);

-- ── Indexes ───────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_of_ticker    ON options_flow(ticker);
CREATE INDEX IF NOT EXISTS idx_of_created   ON options_flow(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_of_premium   ON options_flow(premium DESC);
CREATE INDEX IF NOT EXISTS idx_of_rule      ON options_flow(alert_rule);

CREATE INDEX IF NOT EXISTS idx_dp_ticker    ON dark_pool(ticker);
CREATE INDEX IF NOT EXISTS idx_dp_created   ON dark_pool(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dp_premium   ON dark_pool(premium DESC);

CREATE INDEX IF NOT EXISTS idx_it_ticker    ON insider_trades(ticker);
CREATE INDEX IF NOT EXISTS idx_it_created   ON insider_trades(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_it_code      ON insider_trades(transaction_code);

CREATE INDEX IF NOT EXISTS idx_ct_ticker    ON congress_trades(ticker);
CREATE INDEX IF NOT EXISTS idx_ct_created   ON congress_trades(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sig_ticker   ON signals(ticker);
CREATE INDEX IF NOT EXISTS idx_sig_score    ON signals(score DESC);
CREATE INDEX IF NOT EXISTS idx_sig_type     ON signals(type);
CREATE INDEX IF NOT EXISTS idx_sig_created  ON signals(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ph_ticker    ON pattern_hits(ticker);
CREATE INDEX IF NOT EXISTS idx_ph_pattern   ON pattern_hits(pattern_name);
CREATE INDEX IF NOT EXISTS idx_ph_created   ON pattern_hits(created_at DESC);

-- ── IV/RV edge validation (hypothetical short-straddle outcomes) ──────────
-- When an earnings/IV-rich setup fires we log the IMPLIED move; after the event
-- we compare it to the REALIZED move. A premium seller wins when realized <
-- implied. This validates the IV/RV edge on paper before we build execution.
CREATE TABLE IF NOT EXISTS iv_rv_evals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    signal_date       TEXT NOT NULL,          -- date the setup fired
    earnings_date     TEXT,                   -- next earnings date (yfinance calendar)
    recommendation    TEXT,                   -- SELL_PREMIUM | CONSIDER
    iv30_rv30         REAL,                   -- IV/RV ratio at signal
    implied_move_pct  REAL,                   -- expected (straddle) move at signal
    entry_price       REAL,
    resolve_after     TEXT,                   -- date to measure the realized move
    resolved          INTEGER DEFAULT 0,
    exit_price        REAL,
    realized_move_pct REAL,                   -- abs % move entry->exit
    hypo_win          INTEGER,                -- 1 if realized < implied (seller wins)
    hypo_edge_pct     REAL,                   -- implied - realized (positive = seller edge)
    created_at        TEXT NOT NULL,
    resolved_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_ive_ticker  ON iv_rv_evals(ticker);
CREATE INDEX IF NOT EXISTS idx_ive_open    ON iv_rv_evals(resolved, resolve_after);

-- ── Daily equity snapshot (paper-trading equity curve for the eval loop) ──
CREATE TABLE IF NOT EXISTS daily_equity (
    date          TEXT PRIMARY KEY,   -- YYYY-MM-DD (ET)
    equity        REAL NOT NULL,      -- latest account equity that day
    open_equity   REAL,               -- first snapshot of the day (immutable; the day's open)
    cash          REAL,
    buying_power  REAL,
    open_positions INTEGER,
    realized_pnl_day REAL,            -- realized P&L booked that day (from trade_performance)
    created_at    TEXT NOT NULL,
    -- Touched on EVERY hourly upsert (created_at only marks the day's first).
    -- This is the liveness heartbeat: a process that is up but wedged stops
    -- moving this while looking identical to a quiet day.
    updated_at    TEXT
);

-- ── IV/RV earnings execution (Phase 2): defined-risk iron condors ──
-- One row per condor sold before an earnings print. Legs are stored as JSON so
-- the exit can submit the inverse. P&L = (credit - exit_debit) * 100 * qty.
CREATE TABLE IF NOT EXISTS iv_condors (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker         TEXT NOT NULL,
    earnings_date  TEXT,                    -- the print we're selling into
    expiry         TEXT NOT NULL,           -- option expiry (YYYY-MM-DD)
    legs_json      TEXT NOT NULL,           -- [{symbol, side, position_intent, ratio_qty}]
    short_put      REAL, long_put   REAL,
    short_call     REAL, long_call  REAL,
    qty            INTEGER NOT NULL,
    credit         REAL NOT NULL,           -- net credit collected per spread ($)
    max_loss       REAL NOT NULL,           -- per-spread max loss ($)
    entry_order_id TEXT,
    entry_status   TEXT,
    opened_at      TEXT NOT NULL,
    status         TEXT DEFAULT 'open',     -- pending_entry | open | closing | closed | void
    close_order_id TEXT,
    exit_debit     REAL,                    -- net debit paid to close per spread
    pnl            REAL,                    -- realized $ P&L (all spreads)
    closed_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_condor_open ON iv_condors(status);
CREATE INDEX IF NOT EXISTS idx_condor_tkr  ON iv_condors(ticker, status);

CREATE TABLE IF NOT EXISTS iv_condor_close_fills (
    order_id TEXT PRIMARY KEY,
    condor_id INTEGER NOT NULL,
    filled_qty INTEGER NOT NULL,
    debit REAL NOT NULL,
    legs_json TEXT            -- legs this order closed; NULL = all four
);
CREATE TABLE IF NOT EXISTS position_monitor_state (
    symbol TEXT PRIMARY KEY,
    state_json TEXT NOT NULL
);

-- Append-only measurement inputs: repricing never erases the original book.
CREATE TABLE IF NOT EXISTS iv_variant_captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    earnings_date TEXT NOT NULL,
    source TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_variant_capture_event
    ON iv_variant_captures(ticker, earnings_date, source, captured_at);
CREATE TABLE IF NOT EXISTS db_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- The broker's complete list of cash transfers in and out of the account.
CREATE TABLE IF NOT EXISTS cash_transfers (
    activity_id   TEXT PRIMARY KEY,
    activity_type TEXT NOT NULL,
    amount        REAL NOT NULL,
    date          TEXT NOT NULL
);

-- ── IV/RV strategy-variant logger (measurement only, NO execution) ──
-- For each earnings event we log several hypothetical structures side by side
-- (condor at 0.7/1.0/1.3× the implied move, iron fly, short straddle) priced off
-- the live chain, then resolve each vs the realized move to compare expectancy
-- before promoting any variant to real execution. Zero capital at risk.
CREATE TABLE IF NOT EXISTS iv_variant_evals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker         TEXT NOT NULL,
    earnings_date  TEXT,
    signal_date    TEXT NOT NULL,
    expiry         TEXT,
    variant        TEXT NOT NULL,          -- condor_1.0sd | condor_0.7sd | ... | fly | straddle
    spot           REAL,                   -- underlying at signal
    implied_move_pct REAL,
    short_put      REAL, long_put  REAL,   -- long_* NULL for straddle/strangle
    short_call     REAL, long_call REAL,
    credit         REAL NOT NULL,          -- credit at a CONSERVATIVE fill (short=bid, long=ask)
    credit_mid     REAL,                   -- same structure priced at mid, for reference only
    fees           REAL,                   -- round-trip commission ($ per spread)
    strike_step    REAL,                   -- listed strike increment (pin-risk yardstick)
    -- Days between pricing and the print. Implied move inflates as earnings
    -- approach, so a row priced 7 days out understates it badly and makes the
    -- market look like it underprices moves. Stored so the bias is visible.
    lead_days      INTEGER,
    pricing_model  TEXT NOT NULL DEFAULT 'legacy_excluded',
                                             -- comparable fill/fee methodology version
    max_loss       REAL,                   -- per-spread $ incl. fees (NULL = undefined risk)
    -- 1 = passed the three scanner gates, 0 = logged purely as the
    -- "sell everything indiscriminately" baseline (VALIDATION_SPEC §4).
    gate_passed    INTEGER DEFAULT 1,
    recommendation TEXT,                  -- SELL_PREMIUM | CONSIDER | AVOID; NULL = legacy unknown
    -- Curated execution universe or measurement-only expansion. Keeping this
    -- provenance makes it possible to test the gates in each population.
    source         TEXT NOT NULL DEFAULT 'watchlist',
    -- Set when a coarse strike grid rounded this shape onto strikes already
    -- used by an earlier variant: a real observation, but one that says nothing
    -- about which shape is better. Excluded from shape-vs-shape comparisons.
    collapsed_with TEXT,
    resolve_after  TEXT,
    resolved       INTEGER DEFAULT 0,
    exit_spot      REAL,                   -- underlying close on the option's expiry
    realized_pnl   REAL,                   -- $ per 1 spread, expiry intrinsic NET of fees
    win            INTEGER,
    pin_risk       INTEGER,                -- settled within one strike of a short leg
    created_at     TEXT NOT NULL,
    resolved_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_variant_open ON iv_variant_evals(resolved, resolve_after);

-- Risk throttle. Sizing is DERIVED from closed-condor history rather than kept
-- as a counter, so it cannot drift out of sync with reality and is auditable
-- after the fact. The only stored state is the halt latch: a circuit breaker
-- must require a human to clear it, or it is not a circuit breaker.
CREATE TABLE IF NOT EXISTS risk_control (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    halted        INTEGER NOT NULL DEFAULT 0,
    halted_at     TEXT,
    halted_reason TEXT,
    rearmed_at    TEXT      -- streak counts only closes AFTER this
);
INSERT OR IGNORE INTO risk_control (id, halted) VALUES (1, 0);
CREATE INDEX IF NOT EXISTS idx_variant_kind ON iv_variant_evals(variant, resolved);

-- One latest quote-coverage observation per event/source/methodology. This
-- records skipped structures too, which the priceable rows above cannot do.
CREATE TABLE IF NOT EXISTS iv_variant_attempts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker               TEXT NOT NULL,
    earnings_date        TEXT NOT NULL,
    source               TEXT NOT NULL,
    pricing_model        TEXT NOT NULL,
    structures_attempted INTEGER NOT NULL,
    structures_priced    INTEGER NOT NULL,
    dropped_variants     TEXT NOT NULL DEFAULT '{}',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    UNIQUE(ticker, earnings_date, source, pricing_model)
);
CREATE INDEX IF NOT EXISTS idx_variant_attempt_source
    ON iv_variant_attempts(source, pricing_model);
"""

# Columns added after initial release — applied by _migrate() on connect for
# databases created before the column existed (SQLite has no ADD COLUMN IF NOT EXISTS).
_MIGRATIONS = {
    "iv_condors": {"closed_qty": "INTEGER NOT NULL DEFAULT 0",
                   "close_pnl": "REAL NOT NULL DEFAULT 0",
                   "close_client_order_id": "TEXT",
                   "settlement_note": "TEXT",
                   "close_legs_json": "TEXT"},
    "iv_condor_close_fills": {"legs_json": "TEXT"},
    "pending_trades":    {"strategy": "TEXT", "entry_order_status": "TEXT"},
    "manual_order_requests": {"broker_order_status": "TEXT"},
    "trade_performance": {"strategy": "TEXT", "entry_hour_et": "INTEGER", "hold_minutes": "REAL",
                          "position_intent": "TEXT", "long_entry_qty": "REAL", "open_qty": "REAL"},
    "daily_equity":      {"open_equity": "REAL", "updated_at": "TEXT"},
    "iv_rv_evals":       {"earnings_date": "TEXT"},
    "iv_variant_evals":  {"credit_mid": "REAL", "fees": "REAL", "strike_step": "REAL",
                          "gate_passed": "INTEGER", "pin_risk": "INTEGER",
                          "pricing_model": "TEXT",
                          "source": "TEXT NOT NULL DEFAULT 'watchlist'",
                          "collapsed_with": "TEXT",
                          "lead_days": "INTEGER", "recommendation": "TEXT"},
}


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._condor_fill_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._order_ns: Optional[str] = None

    async def order_namespace(self) -> str:
        """Random prefix for broker client order IDs, fixed per database file.

        Client IDs are built from row ids, which restart at 1 when the DB is
        recreated (as on 2026-10-02). Without a per-DB namespace, a new row's
        ID can equal one already used on the broker account: the POST is
        rejected as a duplicate and the by-client-ID recovery then adopts the
        OLD order as this row's. Raises rather than returning '' so no order is
        ever submitted under an unnamespaced ID.
        """
        if self._order_ns:
            return self._order_ns
        from uuid import uuid4
        await self._exec("INSERT OR IGNORE INTO db_meta (key, value) VALUES ('order_namespace', ?)",
                         (uuid4().hex[:12],), strict=True)
        row = await self._scalar("SELECT value FROM db_meta WHERE key='order_namespace'", strict=True)
        if not row.get("value"):
            raise RuntimeError("order namespace unavailable; refusing to build client order IDs")
        self._order_ns = row["value"]
        return self._order_ns

    async def bind_account(self, fingerprint: str, *, legacy_fingerprint: Optional[str] = None) -> None:
        """Tie this database to one broker account, once, and verify it after.

        The ledger here — open condors, order IDs, fills, risk state — describes
        one account. Run against another (paper keys swapped for live ones, or a
        copied database), the bot would try to manage positions that account
        does not hold. The first run records the account; every later run must
        match it. A populated unbound ledger requires the operator to supply
        its independently verified fingerprint through the migration tool.
        """
        if not fingerprint:
            raise ValueError("account fingerprint required")
        if legacy_fingerprint is not None and legacy_fingerprint != fingerprint:
            raise AccountMismatch("The expected legacy account does not match the connected broker account")
        # The emptiness test is part of the INSERT, not a preceding read: a
        # concurrent ledger write must not slip between checking and binding.
        tables = ("iv_condors", "pending_trades", "manual_order_requests",
                  "position_monitor_state", "trade_performance", "trade_fills",
                  "realized_trade_exits", "daily_equity", "cash_transfers")
        ledger_rows = " UNION ALL ".join(f"SELECT 1 FROM {table}" for table in tables)
        await self._exec(
            "INSERT OR IGNORE INTO db_meta (key, value) SELECT 'broker_account', ? "
            f"WHERE ? OR NOT EXISTS ({ledger_rows})",
            (fingerprint, legacy_fingerprint == fingerprint), strict=True)
        row = await self._scalar("SELECT value FROM db_meta WHERE key='broker_account'", strict=True)
        if not row:
            raise AccountBindingRequired(
                "This populated database has no verified broker account. "
                "Use bind_account.py with its independently verified --expected-fingerprint, "
                "or set DB_PATH to a new file for a different account.")
        if row.get("value") != fingerprint:
            raise AccountMismatch(
                "This database was created for a different broker account. "
                "Set DB_PATH to a separate file for this account.")

    async def replace_cash_transfers(self, transfers: list[dict]) -> None:
        """Mirror the broker's complete transfer list, atomically. The list is
        fetched whole each time, so corrections and cancellations carry over."""
        values = []
        for t in transfers:
            amount = float(t["amount"])
            if (not isinstance(t.get("id"), str) or not t["id"] or not t.get("activity_type")
                    or not math.isfinite(amount)):
                raise ValueError("Invalid cash transfer")
            values.append((t["id"], t["activity_type"], amount, str(t.get("date") or "")))
        async with self._write_lock:
            try:
                await self._conn.execute("DELETE FROM cash_transfers")
                await self._conn.executemany("INSERT INTO cash_transfers VALUES (?,?,?,?)", values)
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise

    async def get_contributed_capital(self) -> Optional[float]:
        """Net cash put into the account (deposits minus withdrawals), or None
        when no transfer has been recorded yet."""
        row = await self._scalar(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total FROM cash_transfers")
        return float(row["total"]) if row.get("n") else None

    async def connect(self):
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._repair_open_variant_resolution_dates()
        await self._backfill_collapsed_variants()
        await self._backfill_variant_lead_days()
        await self._conn.commit()
        logger.info(f"Database ready: {self.path}")

    async def _migrate(self):
        """Add columns introduced after a table's initial release (SQLite lacks
        ADD COLUMN IF NOT EXISTS, so we diff against PRAGMA table_info)."""
        for table, cols in _MIGRATIONS.items():
            async with self._conn.execute(f"PRAGMA table_info({table})") as cur:
                existing = {r["name"] for r in await cur.fetchall()}
            for col, coltype in cols.items():
                if col not in existing:
                    try:
                        await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
                        logger.info(f"Migration: added {table}.{col}")
                    except Exception as e:
                        logger.error(f"Migration failed for {table}.{col}: {e}")
        # This index references a migrated column, so it must be created after
        # the column exists on databases from before source provenance.
        try:
            await self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_variant_source ON iv_variant_evals(source, resolved)")
        except Exception as e:
            logger.error(f"Migration failed for iv_variant_evals.source index: {e}")
        # Rows written before the strict bid/ask + fee model are not comparable
        # to the new forward test. Label them once so the raw audit trail stays
        # intact while aggregate metrics begin from a clean, known methodology.
        try:
            cur = await self._conn.execute(
                """UPDATE iv_variant_evals SET pricing_model=?
                   WHERE pricing_model IS NULL OR pricing_model=''""",
                (LEGACY_VARIANT_PRICING_MODEL,))
            if cur.rowcount:
                logger.info("Migration: excluded %s legacy variant evals from validated metrics",
                            cur.rowcount)
        except Exception as e:
            logger.error(f"Variant pricing-model migration failed: {e}")

    async def _backfill_collapsed_variants(self):
        """Tag pre-existing rows whose strikes duplicate an earlier shape.

        The collapse was always a property of these rows — only the column is
        new. Without the backfill the first logged events (CTAS priced 1.0σ and
        1.3σ on identical 195/205 strikes) would silently count as independent
        evidence about which shape is better.
        """
        try:
            from signals.iv_variants import VARIANTS
            order = {v: i for i, v in enumerate(VARIANTS)}
            async with self._conn.execute(
                """SELECT id, ticker, earnings_date, expiry, variant,
                          short_put, long_put, short_call, long_call
                   FROM iv_variant_evals WHERE collapsed_with IS NULL"""
            ) as cur:
                rows = await cur.fetchall()
            groups: dict = {}
            for r in rows:
                groups.setdefault(
                    (r["ticker"], r["earnings_date"], r["expiry"]), []).append(r)
            updates = []
            for grp in groups.values():
                seen: dict = {}
                for r in sorted(grp, key=lambda x: order.get(x["variant"], 99)):
                    key = (r["short_put"], r["long_put"], r["short_call"], r["long_call"])
                    if key in seen:
                        updates.append((seen[key], r["id"]))
                    else:
                        seen[key] = r["variant"]
            if updates:
                await self._conn.executemany(
                    "UPDATE iv_variant_evals SET collapsed_with=? WHERE id=?", updates)
                logger.info(f"Migration: tagged {len(updates)} collapsed variant rows")
        except Exception as e:
            logger.error(f"Collapsed-variant backfill failed: {e}")

    async def _backfill_variant_lead_days(self):
        """Recover the capture lead for rows written before the column existed.

        lead_days was always a property of these rows: signal_date is the session
        the structure was priced in and earnings_date is the print, so the lead is
        their difference. Leaving them NULL treats every pre-column row as
        maximally stale, which would throw away the captures that were in fact
        taken at the print (LEN was priced the same day) alongside the ones that
        really were a week early. The derivation reproduces the stored values on
        every row written since the column shipped, which is what makes it
        trustworthy here.
        """
        try:
            async with self._conn.execute(
                """SELECT id, earnings_date, signal_date FROM iv_variant_evals
                   WHERE lead_days IS NULL AND earnings_date IS NOT NULL
                     AND signal_date IS NOT NULL"""
            ) as cur:
                rows = await cur.fetchall()
            updates = []
            for r in rows:
                try:
                    lead = (date.fromisoformat(r["earnings_date"])
                            - date.fromisoformat(r["signal_date"])).days
                except (TypeError, ValueError):
                    continue
                if lead >= 0:        # a negative lead means the dates disagree; leave it
                    updates.append((lead, r["id"]))
            if updates:
                await self._conn.executemany(
                    "UPDATE iv_variant_evals SET lead_days=? WHERE id=?", updates)
                logger.info(f"Migration: derived lead_days for {len(updates)} variant rows")
        except Exception as e:
            logger.error(f"Variant lead_days backfill failed: {e}")

    async def _repair_open_variant_resolution_dates(self):
        """Put open variant rows on the current settlement schedule.

        Earlier builds used earnings+2d, which can land before the selected
        option expiry, and then expiry+1d, which pushed every Friday expiry into
        the weekend. The schedule is now expiry_settlement_date()'s answer, so
        this reads that function instead of keeping a second copy of the rule.
        Only unresolved records are changed; resolved historical measurements
        remain intact for auditability.
        """
        try:
            from signals.iv_variants import expiry_settlement_date
            async with self._conn.execute(
                """SELECT id, expiry, resolve_after FROM iv_variant_evals
                   WHERE resolved=0 AND pricing_model=?""",
                (VALIDATED_VARIANT_PRICING_MODEL,)
            ) as cur:
                rows = await cur.fetchall()
            updates = []
            for row in rows:
                target = expiry_settlement_date(row["expiry"])
                if not target:
                    continue
                if row["resolve_after"] != target:
                    updates.append((target, row["id"]))
            if updates:
                await self._conn.executemany(
                    "UPDATE iv_variant_evals SET resolve_after=? WHERE id=?", updates)
                logger.info(f"Migration: rescheduled {len(updates)} open variant evals at expiry")
        except Exception as e:
            logger.error(f"Variant resolution migration failed: {e}")

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def _exec(self, sql: str, params=(), *, strict: bool = False,
                    expected_rows: Optional[int] = None):
        """Commit one write; execution state must opt into propagating failures.

        Serialize writes through commit/rollback so a failed critical write
        cannot roll back another coroutine's write on the shared connection.
        Return the INSERT cursor's identity, never connection-wide last_insert_rowid.
        """
        async with self._write_lock:
            try:
                async with self._conn.execute(sql, params) as cur:
                    if expected_rows is not None and cur.rowcount != expected_rows:
                        raise DatabaseError("Execution-state write did not match its expected row")
                    inserted_id = cur.lastrowid
                    await self._conn.commit()
                    return inserted_id
            except BaseException as e:
                # Also roll back a cancelled write before releasing the lock.
                try:
                    await self._conn.rollback()
                except Exception as rollback_error:
                    logger.error(f"DB rollback error: {rollback_error}")
                    # An unusable connection must not expose uncommitted state.
                    await self._conn.close()
                if not isinstance(e, Exception):
                    raise
                if strict:
                    raise DatabaseError("Required database write failed") from e
                if not isinstance(e, aiosqlite.IntegrityError):
                    logger.error(f"DB write error: {e} | sql={sql[:60]}")

    async def _query(self, sql: str, params=(), *, strict: bool = False) -> list[dict]:
        try:
            async with self._write_lock:
                async with self._conn.execute(sql, params) as cur:
                    rows = await cur.fetchall()
                    return [dict(r) for r in rows]
        except Exception as e:
            if strict:
                raise DatabaseError("Required database query failed") from e
            logger.error(f"DB query error: {e}")
            return []

    async def _scalar(self, sql: str, params=(), *, strict: bool = False):
        try:
            async with self._write_lock:
                async with self._conn.execute(sql, params) as cur:
                    row = await cur.fetchone()
                    return dict(row) if row else {}
        except Exception as e:
            if strict:
                raise DatabaseError("Required database query failed") from e
            logger.error(f"DB scalar error: {e}")
            return {}

    # ── Watchlist (persisted across restarts) ────────────────────────────
    async def get_watchlist(self) -> list[str]:
        rows = await self._query("SELECT ticker FROM watchlist ORDER BY added_at")
        return [r["ticker"] for r in rows]

    async def add_watchlist(self, ticker: str) -> None:
        await self._exec(
            "INSERT OR IGNORE INTO watchlist (ticker, added_at) VALUES (?, ?)",
            (ticker.upper(), datetime.utcnow().isoformat()),
        )

    async def remove_watchlist(self, ticker: str) -> None:
        await self._exec("DELETE FROM watchlist WHERE ticker = ?", (ticker.upper(),))

    # ── Daily equity snapshot (paper equity curve) ───────────────────────
    async def record_daily_equity(self, date_str: str, equity: float, cash: float = 0.0,
                                  buying_power: float = 0.0, open_positions: int = 0,
                                  realized_pnl_day: float = 0.0) -> None:
        """Upsert one day's equity snapshot (idempotent on date)."""
        now = datetime.utcnow().isoformat()
        # open_equity is set on the first snapshot of the day and never updated,
        # so it preserves the day's true open (the hourly upsert only moves `equity`).
        await self._exec(
            """INSERT INTO daily_equity
                 (date, equity, open_equity, cash, buying_power, open_positions, realized_pnl_day, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(date) DO UPDATE SET
                 equity=excluded.equity, cash=excluded.cash,
                 buying_power=excluded.buying_power, open_positions=excluded.open_positions,
                 realized_pnl_day=excluded.realized_pnl_day,
                 updated_at=excluded.updated_at""",
            (date_str, equity, equity, cash, buying_power, open_positions, realized_pnl_day,
             now, now),
        )

    async def get_heartbeat(self, stale_after_minutes: int = 180) -> dict:
        """Is the hourly loop actually running?

        launchd restarts a process that DIES. It cannot tell that a process
        which is still up has stopped doing work — a wedged event loop, a hung
        broker call. That failure is invisible: the report renders, the account
        still reads, and a silent outage looks exactly like a quiet day. The
        equity upsert runs hourly, so the age of its updated_at is the cheapest
        honest liveness signal we have.
        """
        row = await self._scalar(
            "SELECT MAX(COALESCE(updated_at, created_at)) AS last_seen FROM daily_equity")
        last = (row or {}).get("last_seen")
        if not last:
            return {"last_seen": None, "age_minutes": None, "stale": True,
                    "threshold_minutes": stale_after_minutes,
                    "note": "no equity snapshot has ever been written"}
        try:
            age = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds() / 60.0
        except (TypeError, ValueError):
            return {"last_seen": last, "age_minutes": None, "stale": True,
                    "threshold_minutes": stale_after_minutes,
                    "note": "unparseable heartbeat timestamp"}
        age = max(0.0, age)
        stale = age > stale_after_minutes
        return {"last_seen": last, "age_minutes": round(age, 1), "stale": stale,
                "threshold_minutes": stale_after_minutes,
                "note": (f"hourly loop last wrote {age/60:.1f}h ago — expected hourly"
                         if stale else "hourly loop is current")}

    async def get_daily_equity(self, limit: int = 60) -> list[dict]:
        rows = await self._query(
            "SELECT * FROM daily_equity ORDER BY date DESC LIMIT ?", (limit,)
        )
        return list(reversed(rows))

    # ── IV/RV edge validation ────────────────────────────────────────────
    async def record_iv_eval(self, ticker: str, recommendation: str, iv30_rv30: float,
                             implied_move_pct: float, entry_price: float,
                             resolve_after: str, earnings_date: str = None) -> Optional[int]:
        """Log a new IV/RV setup to validate. Deduped: skips if this ticker
        already has an unresolved eval open (one event at a time)."""
        open_ = await self._query(
            "SELECT id FROM iv_rv_evals WHERE ticker=? AND resolved=0 LIMIT 1", (ticker,))
        if open_:
            return None
        now = datetime.utcnow().isoformat()
        # created_at stays a UTC instant, but signal_date is a TRADING day and
        # must be ET: after 20:00 ET the UTC calendar has already rolled, so
        # utcnow()[:10] stamped an evening entry with TOMORROW's session.
        today_et = et_today().isoformat()
        await self._exec(
            """INSERT INTO iv_rv_evals
                 (ticker, signal_date, earnings_date, recommendation, iv30_rv30,
                  implied_move_pct, entry_price, resolve_after, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (ticker, today_et, earnings_date, recommendation, iv30_rv30,
             implied_move_pct, entry_price, resolve_after, now))
        return 1

    async def get_due_iv_evals(self, today: str) -> list[dict]:
        return await self._query(
            "SELECT * FROM iv_rv_evals WHERE resolved=0 AND resolve_after <= ?", (today,))

    async def resolve_iv_eval(self, eval_id: int, exit_price: float,
                              realized_move_pct: float, implied_move_pct: float) -> None:
        hypo_win = 1 if realized_move_pct < implied_move_pct else 0
        edge = round(implied_move_pct - realized_move_pct, 2)
        await self._exec(
            """UPDATE iv_rv_evals SET resolved=1, exit_price=?, realized_move_pct=?,
                 hypo_win=?, hypo_edge_pct=?, resolved_at=? WHERE id=?""",
            (round(exit_price, 2), round(realized_move_pct, 2), hypo_win, edge,
             datetime.utcnow().isoformat(), eval_id))

    async def get_iv_eval_summary(self) -> dict:
        rows = await self._query("SELECT * FROM iv_rv_evals WHERE resolved=1")
        n = len(rows)
        wins = sum(1 for r in rows if r["hypo_win"])
        avg_edge = round(sum(r["hypo_edge_pct"] or 0 for r in rows) / n, 2) if n else 0.0
        open_n = (await self._scalar("SELECT COUNT(*) AS n FROM iv_rv_evals WHERE resolved=0")).get("n", 0)
        return {"resolved": n, "wins": wins,
                "win_rate": round(wins / n * 100, 1) if n else 0.0,
                "avg_edge_pct": avg_edge, "open": open_n}

    # ── IV/RV execution: iron condors (Phase 2) ─────────────────────────
    async def has_open_condor(self, ticker: str) -> bool:
        r = await self._query(
            """SELECT id FROM iv_condors
               WHERE ticker=? AND status IN ('pending_entry','open','closing','awaiting_settlement') LIMIT 1""",
            (ticker,), strict=True)
        return bool(r)

    async def count_condors_opened_today(self, today: str) -> int:
        row = await self._scalar(
            "SELECT COUNT(*) AS n FROM iv_condors WHERE substr(opened_at,1,10)=?", (today,), strict=True)
        return int(row.get("n", 0))

    async def count_open_condors(self) -> int:
        row = await self._scalar(
            "SELECT COUNT(*) AS n FROM iv_condors WHERE status IN ('pending_entry','open','closing','awaiting_settlement')",
            strict=True,
        )
        return int(row.get("n", 0))

    async def record_condor(self, ticker: str, earnings_date: Optional[str], expiry: str,
                            legs_json: str, strikes: dict, qty: int, credit: float,
                            max_loss: float, entry_order_id: Optional[str],
                            entry_status: Optional[str]) -> int:
        now = datetime.utcnow().isoformat()
        inserted_id = await self._exec(
            """INSERT INTO iv_condors
                 (ticker, earnings_date, expiry, legs_json, short_put, long_put,
                  short_call, long_call, qty, credit, max_loss, entry_order_id,
                  entry_status, opened_at, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending_entry')""",
            (ticker, earnings_date, expiry, legs_json,
             strikes.get("short_put"), strikes.get("long_put"),
             strikes.get("short_call"), strikes.get("long_call"),
             int(qty), round(credit, 2), round(max_loss, 2),
             entry_order_id, entry_status, now), strict=True, expected_rows=1)
        return int(inserted_id)

    async def get_open_condors(self) -> list[dict]:
        return await self._query("SELECT * FROM iv_condors WHERE status='open'")

    async def get_active_condors(self) -> list[dict]:
        return await self._query(
            "SELECT * FROM iv_condors WHERE status IN ('pending_entry','open','closing','awaiting_settlement')", strict=True)

    async def get_active_condor_leg_symbols(self) -> set[str]:
        """OCC symbols owned by an active condor.

        The generic single-leg TP/SL monitor must never manage one of these
        symbols independently; condors are opened and closed as a four-leg unit.
        Unreadable ownership fails closed: ignoring it could sell a wing.
        """
        rows = await self._query(
            "SELECT legs_json FROM iv_condors WHERE status IN ('pending_entry','open','closing','awaiting_settlement')",
            strict=True,
        )
        symbols: set[str] = set()
        for row in rows:
            try:
                legs = json.loads(row["legs_json"])
                names = [leg["symbol"].strip().upper() for leg in legs]
                if (len(legs) != 4 or len(set(names)) != 4 or not all(names)
                        or sorted(leg["side"] for leg in legs) != ["buy", "buy", "sell", "sell"]):
                    raise ValueError("invalid condor legs")
                symbols.update(names)
            except (KeyError, AttributeError, TypeError, ValueError) as e:
                raise DatabaseError("Active condor ownership is malformed; automated trading deferred") from e
        return symbols

    async def get_flow_risk_state(self, now=None) -> dict:
        """Actual closing executions, with ET-day P&L and durable loss times."""
        from market_time import et_now
        now = et_now(now)
        try:
            async with self._write_lock:
                async with self._conn.execute("SELECT value FROM db_meta WHERE key='flow_risk_ready'") as cur:
                    ready = await cur.fetchone()
                if ready and ready["value"] != "1":
                    raise DatabaseError("Broker fill synchronization has not completed")
                async with self._conn.execute("""SELECT t.id FROM trade_performance t
               LEFT JOIN (SELECT alpaca_order_id, SUM(qty) qty, MIN(symbol) symbol_min,
                                 MAX(symbol) symbol_max, MIN(side) side_min, MAX(side) side_max FROM trade_fills
                          GROUP BY alpaca_order_id) f USING (alpaca_order_id)
               WHERE ABS(COALESCE(t.filled_qty,0)-COALESCE(f.qty,0)) > 0.00000001
                  OR f.symbol_min <> t.symbol OR f.symbol_max <> t.symbol
                  OR f.side_min <> t.side OR f.side_max <> t.side
               LIMIT 1""") as cur:
                    if await cur.fetchone():
                        raise DatabaseError("Incomplete broker fill history; flow risk controls unavailable")
                async with self._conn.execute(
                    "SELECT activity_id,ticker,executed_at,SUM(realized_pnl) realized_pnl "
                    "FROM realized_trade_exits GROUP BY activity_id,ticker,executed_at") as cur:
                    rows = await cur.fetchall()
        except Exception as e:
            raise DatabaseError("Flow risk controls unavailable") from e
        pnl, losses = 0.0, {}
        for row in rows:
            at = datetime.fromisoformat(row["executed_at"].replace("Z", "+00:00"))
            at = at.replace(tzinfo=at.tzinfo or timezone.utc).astimezone(_ET)
            if at > now:
                continue
            value = float(row["realized_pnl"])
            if not math.isfinite(value):
                raise DatabaseError("Nonfinite realized P&L; flow risk controls unavailable")
            if at.date() == now.date():
                pnl += value
            if value < 0:
                ticker = row["ticker"].upper()
                losses[ticker] = max(losses.get(ticker, 0), at.timestamp())
        return {"date": now.date().isoformat(), "daily_pnl": round(pnl, 2), "loss_times": losses}

    async def record_variant_capture(self, ticker, earnings_date, source, snapshot):
        if source not in VARIANT_SOURCES:
            raise ValueError(f"unknown variant source: {source}")
        await self._exec(
            "INSERT INTO iv_variant_captures (ticker,earnings_date,source,captured_at,snapshot_json) VALUES (?,?,?,?,?)",
            (ticker, earnings_date, source, snapshot["captured_at"],
             json.dumps(snapshot, default=str, allow_nan=False)), strict=True, expected_rows=1)

    async def ensure_manual_order_request(self, request_id: str, payload_json: str, client_id: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        await self._exec(
            """INSERT OR IGNORE INTO manual_order_requests
               (request_id,payload_json,client_order_id,created_at,updated_at) VALUES (?,?,?,?,?)""",
            (request_id, payload_json, client_id, now, now), strict=True)
        row = await self.get_manual_order_request(request_id)
        if row["payload_json"] != payload_json or row["client_order_id"] != client_id:
            raise ValueError("Request ID already belongs to a different order")
        return row

    async def get_manual_order_request(self, request_id: str) -> Optional[dict]:
        rows = await self._query("SELECT * FROM manual_order_requests WHERE request_id=?", (request_id,), strict=True)
        return rows[0] if rows else None

    async def get_manual_entry_reservations(self) -> list[dict]:
        return await self._query(
            """SELECT * FROM manual_order_requests
               WHERE status IN ('submitting','pending') OR
               (status='confirmed' AND COALESCE(broker_order_status,'unknown')
                NOT IN ('filled','canceled','expired','rejected','replaced'))""", strict=True)

    async def resolve_manual_order_status(self, request_id: str, broker_status: str):
        await self._exec(
            "UPDATE manual_order_requests SET broker_order_status=?,updated_at=? "
            "WHERE request_id=? AND status='confirmed'",
            (broker_status, datetime.now(timezone.utc).isoformat(), request_id),
            strict=True, expected_rows=1)

    async def update_manual_order_request(self, request_id: str, status: str, order_id=None, error=None,
                                          *, claim: bool = False):
        await self._exec(
            """UPDATE manual_order_requests SET status=?,alpaca_order_id=?,error=?,updated_at=?
               WHERE request_id=?""" + (" AND status='new'" if claim else ""),
            (status, order_id, error, datetime.now(timezone.utc).isoformat(), request_id),
            strict=True, expected_rows=1)

    async def update_condor_entry_status(self, condor_id: int, entry_status: str) -> None:
        await self._exec(
            "UPDATE iv_condors SET entry_status=? WHERE id=?", (entry_status, condor_id),
            strict=True, expected_rows=1)

    async def activate_condor(self, condor_id: int, filled_qty: float,
                              filled_avg_price: float, entry_status: str = "filled") -> dict:
        """Mark a filled parent MLeg as manageable using actual fill economics.

        Alpaca reports MLeg parent price as net debit/credit.  A credit order is
        negative, so its absolute value is the credit collected per spread.
        Recalculate max loss from that actual credit rather than the planning mid.
        """
        row = await self._scalar(
            "SELECT short_put,long_put,short_call,long_call FROM iv_condors WHERE id=?", (condor_id,), strict=True)
        raw_qty = float(filled_qty or 0)
        credit = abs(float(filled_avg_price or 0))
        from math import isfinite
        if (not row or not isfinite(raw_qty) or not raw_qty.is_integer()
                or raw_qty < 1 or not isfinite(credit) or credit <= 0):
            raise ValueError("filled condor requires positive quantity and net credit")
        qty = int(raw_qty)
        width = max(
            float(row["long_call"] or 0) - float(row["short_call"] or 0),
            float(row["short_put"] or 0) - float(row["long_put"] or 0),
        )
        max_loss = max(0.0, width - credit) * 100
        await self._exec(
            """UPDATE iv_condors
               SET status='open', qty=?, credit=?, max_loss=?, entry_status=?
               WHERE id=?""",
            (qty, round(credit, 2), round(max_loss, 2), entry_status, condor_id), strict=True, expected_rows=1,
        )
        return {"qty": qty, "credit": round(credit, 2), "max_loss": round(max_loss, 2)}

    async def void_condor(self, condor_id: int, entry_status: str) -> None:
        """Close the bookkeeping record for an entry that never filled."""
        await self._exec(
            """UPDATE iv_condors
               SET status='void', entry_status=?, closed_at=? WHERE id=?""",
            (entry_status, datetime.utcnow().isoformat(), condor_id), strict=True, expected_rows=1,
        )

    async def mark_condor_closing(self, condor_id: int, close_order_id: Optional[str],
                                  client_order_id: Optional[str] = None) -> None:
        await self._exec(
            "UPDATE iv_condors SET status='closing', close_order_id=?, close_client_order_id=? WHERE id=?",
            (close_order_id, client_order_id, condor_id), strict=True, expected_rows=1)

    async def begin_condor_close(self, condor_id: int, client_order_id: str,
                                 close_legs_json: Optional[str] = None) -> None:
        """Claim a close before it is submitted, with the legs it will contain.

        `close_legs_json` is NULL for the usual four-leg close. A close that
        leaves a worthless wing out records the legs it does include, so its
        fills can be told apart later: the wings it left behind stay in the
        account until they expire (get_condor_residual_legs).
        """
        await self._exec(
            """UPDATE iv_condors SET status='closing', close_order_id=NULL,
                      close_client_order_id=?, close_legs_json=? WHERE id=?""",
            (client_order_id, close_legs_json, condor_id), strict=True, expected_rows=1)

    async def close_condor(self, condor_id: int, exit_debit: float, pnl: float) -> None:
        await self._exec(
            """UPDATE iv_condors SET status='closed', exit_debit=?, pnl=?, closed_at=?
               WHERE id=?""",
            (round(exit_debit, 2), round(pnl, 2), datetime.utcnow().isoformat(), condor_id),
            strict=True, expected_rows=1)

    async def record_condor_close_fill(self, condor_id: int, order_id: str,
                                       filled_qty: float, debit: float, *,
                                       legs_json: Optional[str] = None) -> dict:
        """Store cumulative fills once per broker order, including partial fills.

        Quantity on iv_condors remains the entry quantity. closed_qty is the sum
        across all replacement closes; close_pnl books only those actual fills.
        `legs_json` is the set of legs this order closed (NULL = all four); it is
        written when the order is first seen and never changed afterwards.
        """
        from math import isfinite
        q, d = float(filled_qty), float(debit)
        if not isfinite(q) or q < 0 or not q.is_integer() or not isfinite(d) or d < 0:
            raise ValueError("invalid condor close fill")
        async with self._condor_fill_lock:
            row = await self._scalar("SELECT * FROM iv_condors WHERE id=?", (condor_id,), strict=True)
            old = await self._scalar("SELECT * FROM iv_condor_close_fills WHERE order_id=?", (order_id,), strict=True)
            if not row or (old and old["condor_id"] != condor_id):
                raise ValueError("close fill has no matching condor")
            if old and q < old["filled_qty"]:
                return row  # an older broker snapshot must not unbook fills
            total = await self._scalar(
                "SELECT COALESCE(SUM(filled_qty),0) AS n FROM iv_condor_close_fills WHERE condor_id=? AND order_id<>?",
                (condor_id, order_id), strict=True)
            if total["n"] + q > row["qty"]:
                raise ValueError("close fills exceed entry quantity")
            await self._exec(
                """INSERT INTO iv_condor_close_fills (order_id, condor_id, filled_qty, debit, legs_json)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(order_id) DO UPDATE SET filled_qty=excluded.filled_qty, debit=excluded.debit""",
                (order_id, condor_id, int(q), d, legs_json), strict=True, expected_rows=1)
            totals = await self._scalar(
                """SELECT COALESCE(SUM(filled_qty),0) AS n,
                          COALESCE(SUM(filled_qty * debit),0) AS cost
                   FROM iv_condor_close_fills WHERE condor_id=?""", (condor_id,), strict=True)
            pnl = (row["credit"] * totals["n"] - totals["cost"]) * 100
            await self._exec("UPDATE iv_condors SET closed_qty=?, close_pnl=? WHERE id=?",
                             (totals["n"], round(pnl, 2), condor_id), strict=True, expected_rows=1)
            return await self._scalar("SELECT * FROM iv_condors WHERE id=?", (condor_id,), strict=True)

    async def get_condor_residual_legs(self, condor_id: Optional[int] = None, *,
                                       min_expiry: Optional[str] = None) -> dict[str, float]:
        """Long wings a close deliberately left in the account, by OCC symbol.

        A winning condor's wings are worthless and have no bid, so they cannot
        be sold and the close buys back the short legs only. Those wings then
        sit in the account until they expire. They are derived from the close
        fills rather than stored: for every fill whose order left a leg out,
        that leg is still held in the filled quantity. Nothing needs clearing
        when they expire — a symbol that is no longer held matches nothing.

        Only a long leg can be residual. A fill that claims to have left a
        short leg open is corrupt and fails closed.
        """
        sql = """SELECT f.legs_json AS closed_legs, f.filled_qty, c.legs_json AS condor_legs
                 FROM iv_condor_close_fills f JOIN iv_condors c ON c.id = f.condor_id
                 WHERE f.legs_json IS NOT NULL AND f.filled_qty > 0"""
        params: tuple = ()
        if condor_id is not None:
            sql, params = sql + " AND c.id=?", params + (condor_id,)
        if min_expiry is not None:        # an expired option cannot still be held
            sql, params = sql + " AND c.expiry>=?", params + (min_expiry,)
        rows = await self._query(sql, params, strict=True)
        residual: dict[str, float] = {}
        for row in rows:
            try:
                closed = {leg["symbol"].strip().upper() for leg in json.loads(row["closed_legs"])}
                for leg in json.loads(row["condor_legs"]):
                    symbol = leg["symbol"].strip().upper()
                    if symbol in closed:
                        continue
                    if leg["side"] != "buy":
                        raise ValueError("a close left a short leg open")
                    residual[symbol] = (residual.get(symbol, 0.0)
                                        + float(row["filled_qty"]) * float(leg.get("ratio_qty", 1)))
            except (KeyError, AttributeError, TypeError, ValueError) as e:
                raise DatabaseError("Condor close legs are malformed; automated trading deferred") from e
        return residual

    async def await_condor_settlement(self, condor_id: int, note: str) -> None:
        await self._exec(
            "UPDATE iv_condors SET status='awaiting_settlement', settlement_note=? WHERE id=?",
            (note, condor_id), strict=True, expected_rows=1)

    async def get_position_monitor_states(self) -> dict:
        rows = await self._query("SELECT * FROM position_monitor_state", strict=True)
        return {r["symbol"]: json.loads(r["state_json"]) for r in rows}

    async def save_position_monitor_state(self, symbol: str, state: dict) -> None:
        await self._exec(
            """INSERT INTO position_monitor_state VALUES (?,?)
               ON CONFLICT(symbol) DO UPDATE SET state_json=excluded.state_json""",
            (symbol, json.dumps(state)), strict=True, expected_rows=1)

    async def delete_position_monitor_state(self, symbol: str) -> None:
        await self._exec("DELETE FROM position_monitor_state WHERE symbol=?", (symbol,), strict=True)

    async def get_condor_summary(self) -> dict:
        rows = await self._query("SELECT * FROM iv_condors WHERE status='closed'")
        n = len(rows)
        wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
        pnl = round(sum(r["pnl"] or 0 for r in rows), 2)
        open_row = await self._scalar(
            "SELECT COUNT(*) AS n FROM iv_condors WHERE status IN ('open','closing','awaiting_settlement')")
        pending = await self._scalar(
            "SELECT COUNT(*) AS n FROM iv_condors WHERE status='pending_entry'")
        return {"closed": n, "wins": wins,
                "win_rate": round(wins / n * 100, 1) if n else 0.0,
                "total_pnl": pnl, "open": int(open_row.get("n", 0)),
                "pending": int(pending.get("n", 0))}

    # ── IV/RV strategy-variant logger (measurement only) ────────────────
    async def has_variant_evals(self, ticker: str, earnings_date: Optional[str]) -> bool:
        """True if this ticker+event has a current-model open evaluation."""
        r = await self._query(
            """SELECT id FROM iv_variant_evals
               WHERE ticker=? AND resolved=0 AND pricing_model=?
                 AND (earnings_date IS ? OR earnings_date=?) LIMIT 1""",
            (ticker, VALIDATED_VARIANT_PRICING_MODEL, earnings_date, earnings_date))
        return bool(r)

    async def get_open_variant_lead(self, ticker: str, earnings_date: Optional[str],
                                    source: str = "watchlist") -> Optional[int]:
        """Smallest lead_days among this event's UNRESOLVED rows, or None if the
        event has not been priced yet. Used to decide whether a fresh pass is
        closer to the print than what this source already holds."""
        if source not in VARIANT_SOURCES:
            raise ValueError(f"unknown variant source: {source}")
        r = await self._query(
            """SELECT MIN(COALESCE(lead_days, 999)) AS lead FROM iv_variant_evals
               WHERE ticker=? AND resolved=0 AND pricing_model=?
                 AND source=? AND (earnings_date IS ? OR earnings_date=?)""",
            (ticker, VALIDATED_VARIANT_PRICING_MODEL, source, earnings_date, earnings_date))
        if not r or r[0]["lead"] is None:
            return None
        return int(r[0]["lead"])

    async def get_open_variant_names(self, ticker: str, earnings_date: Optional[str],
                                     source: str = "watchlist") -> set[str]:
        """Variants held for one source/event, used to make repricing atomic."""
        if source not in VARIANT_SOURCES:
            raise ValueError(f"unknown variant source: {source}")
        rows = await self._query(
            """SELECT DISTINCT variant FROM iv_variant_evals
               WHERE ticker=? AND resolved=0 AND pricing_model=?
                 AND source=? AND (earnings_date IS ? OR earnings_date=?)""",
            (ticker, VALIDATED_VARIANT_PRICING_MODEL, source, earnings_date, earnings_date))
        return {str(r["variant"]) for r in rows if r.get("variant")}

    async def delete_open_variant_evals(self, ticker: str, earnings_date: Optional[str],
                                        source: str = "watchlist") -> int:
        """Drop one source's unresolved event rows after a complete re-price.

        Resolved rows and the other source's cohort are never touched.
        """
        if source not in VARIANT_SOURCES:
            raise ValueError(f"unknown variant source: {source}")
        rows = await self._query(
            """SELECT id FROM iv_variant_evals
               WHERE ticker=? AND resolved=0 AND pricing_model=?
                 AND source=? AND (earnings_date IS ? OR earnings_date=?)""",
            (ticker, VALIDATED_VARIANT_PRICING_MODEL, source, earnings_date, earnings_date))
        if rows:
            await self._exec(
                """DELETE FROM iv_variant_evals
                   WHERE ticker=? AND resolved=0 AND pricing_model=?
                     AND source=? AND (earnings_date IS ? OR earnings_date=?)""",
                (ticker, VALIDATED_VARIANT_PRICING_MODEL, source, earnings_date, earnings_date))
        return len(rows)

    async def record_variant_eval(self, ticker: str, earnings_date: Optional[str],
                                  expiry: Optional[str], variant: str, spot: float,
                                  implied_move_pct: float, strikes: dict, credit: float,
                                  max_loss: Optional[float], resolve_after: str,
                                  credit_mid: Optional[float] = None,
                                  fees: Optional[float] = None,
                                  strike_step: Optional[float] = None,
                                  gate_passed: bool = True,
                                  pricing_model: Optional[str] = None,
                                  source: str = "watchlist",
                                  collapsed_with: Optional[str] = None,
                                  lead_days: Optional[int] = None,
                                  recommendation: Optional[str] = None) -> None:
        # Do not accidentally certify a direct/legacy call that did not record
        # both the reference mid and explicit commission assumption.
        pricing_model = pricing_model or (
            VALIDATED_VARIANT_PRICING_MODEL
            if credit_mid is not None and fees is not None
            else LEGACY_VARIANT_PRICING_MODEL)
        if source not in VARIANT_SOURCES:
            raise ValueError(f"unknown variant source: {source}")
        # Explicit boolean-only callers certify the strict three-gate contract.
        # Existing rows gain a NULL recommendation on migration: their old flag
        # also admitted CONSIDER and cannot establish which gates passed.
        recommendation = recommendation or ("SELL_PREMIUM" if gate_passed else "AVOID")
        if recommendation not in ("SELL_PREMIUM", "CONSIDER", "AVOID"):
            raise ValueError("unknown variant recommendation")
        gate_passed = recommendation == "SELL_PREMIUM"
        now = datetime.utcnow().isoformat()
        today_et = et_today().isoformat()      # trading day, not the UTC day
        await self._exec(
            """INSERT INTO iv_variant_evals
                 (ticker, earnings_date, signal_date, expiry, variant, spot,
                  implied_move_pct, short_put, long_put, short_call, long_call,
                  credit, credit_mid, fees, strike_step, pricing_model, gate_passed,
                  source, collapsed_with, lead_days, max_loss, resolve_after, created_at, recommendation)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, earnings_date, today_et, expiry, variant, round(spot, 2),
             round(implied_move_pct, 2), strikes.get("short_put"), strikes.get("long_put"),
             strikes.get("short_call"), strikes.get("long_call"),
             round(credit, 2),
             (round(credit_mid, 2) if credit_mid is not None else None),
             (round(fees, 2) if fees is not None else None),
             strike_step, pricing_model, 1 if gate_passed else 0,
             source, collapsed_with, lead_days,
             (round(max_loss, 2) if max_loss is not None else None),
             resolve_after, now, recommendation))

    async def record_variant_attempt(self, ticker: str, earnings_date: str,
                                     structures_attempted: int, structures_priced: int,
                                     dropped_variants: dict[str, str],
                                     source: str = "watchlist",
                                     pricing_model: str = VALIDATED_VARIANT_PRICING_MODEL) -> None:
        """Store the latest quote-coverage outcome for one event.

        A retry updates the same event instead of counting its five structures
        repeatedly; the daily coverage ratio therefore measures events, not
        scheduler retries.
        """
        if source not in VARIANT_SOURCES:
            raise ValueError(f"unknown variant source: {source}")
        if not earnings_date:
            raise ValueError("variant attempt requires an earnings date")
        now = datetime.utcnow().isoformat()
        await self._exec(
            """INSERT INTO iv_variant_attempts
                 (ticker, earnings_date, source, pricing_model,
                  structures_attempted, structures_priced, dropped_variants,
                  created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ticker, earnings_date, source, pricing_model) DO UPDATE SET
                 structures_attempted=excluded.structures_attempted,
                 structures_priced=excluded.structures_priced,
                 dropped_variants=excluded.dropped_variants,
                 updated_at=excluded.updated_at""",
            (ticker, earnings_date, source, pricing_model,
             max(0, int(structures_attempted)), max(0, int(structures_priced)),
             json.dumps(dropped_variants, sort_keys=True), now, now))

    async def get_variant_quote_coverage(self, source: Optional[str] = None) -> dict:
        """Latest per-event priceability, including variants skipped for quotes."""
        if source is not None and source not in VARIANT_SOURCES:
            raise ValueError(f"unknown variant source: {source}")
        where = "WHERE pricing_model=?"
        params: tuple = (VALIDATED_VARIANT_PRICING_MODEL,)
        if source is not None:
            where += " AND source=?"
            params += (source,)
        rows = await self._query(
            f"""SELECT structures_attempted, structures_priced, dropped_variants
                FROM iv_variant_attempts {where}""", params)
        attempted = sum(int(r["structures_attempted"] or 0) for r in rows)
        priced = sum(int(r["structures_priced"] or 0) for r in rows)
        dropped: dict[str, int] = {}
        for row in rows:
            try:
                variants = json.loads(row.get("dropped_variants") or "{}")
            except (TypeError, ValueError):
                variants = {}
            if not isinstance(variants, dict):
                continue
            for variant in variants:
                dropped[str(variant)] = dropped.get(str(variant), 0) + 1
        return {
            "events": len(rows),
            "structures_attempted": attempted,
            "structures_priced": priced,
            "priced_pct": round(priced / attempted * 100, 1) if attempted else None,
            "dropped_variants": dropped,
        }

    async def get_due_variant_evals(self, today: str) -> list[dict]:
        return await self._query(
            """SELECT * FROM iv_variant_evals
               WHERE resolved=0 AND pricing_model=? AND resolve_after <= ?""",
            (VALIDATED_VARIANT_PRICING_MODEL, today))

    async def resolve_variant_eval(self, eval_id: int, exit_spot: float,
                                   realized_pnl: float,
                                   pin_risk: Optional[bool] = None) -> None:
        await self._exec(
            """UPDATE iv_variant_evals SET resolved=1, exit_spot=?, realized_pnl=?,
                 win=?, pin_risk=?, resolved_at=? WHERE id=?""",
            (round(exit_spot, 2), round(realized_pnl, 2),
             1 if realized_pnl > 0 else 0,
             (None if pin_risk is None else (1 if pin_risk else 0)),
             datetime.utcnow().isoformat(), eval_id))

    async def get_implied_vs_realized(self, source: Optional[str] = None,
                                      max_lead_days: Optional[int] = None) -> dict:
        """Did the stock move more than the option market priced in?

        This is the structural question underneath every premium-selling
        strategy, and it is a far better estimator than counting max-loss
        events: the tail is what kills you, but tail events are rare, so
        waiting to observe enough of them takes hundreds of trades. Realized-vs-
        implied is observable on EVERY event and converges far faster.

        Computed per EVENT, not per variant row. All five structures on one
        print share the same underlying move, so averaging across rows would
        count each event five times and shrink the error bars fraudulently.

        Only events whose implied move was captured within `max_lead_days` of
        the print count toward the headline. Events priced further out are
        returned under `stale_capture` and deliberately excluded: their implied
        move is understated, which inflates `pct_exceeding_implied` toward a
        pessimistic answer for free. A resolved measurement is never rewritten,
        so the early captures stay on the books — they just do not get to speak
        for the edge.
        """
        cutoff = (MAX_TRUSTED_CAPTURE_LEAD_DAYS if max_lead_days is None
                  else int(max_lead_days))
        where = "WHERE resolved=1 AND pricing_model=? AND spot>0 AND implied_move_pct>0"
        params: tuple = (VALIDATED_VARIANT_PRICING_MODEL,)
        if source is not None:
            if source not in VARIANT_SOURCES:
                raise ValueError(f"unknown variant source: {source}")
            where += " AND source=?"
            params += (source,)
        rows = await self._query(
            f"""SELECT ticker, earnings_date,
                       MAX(spot) AS spot, MAX(exit_spot) AS exit_spot,
                       MAX(implied_move_pct) AS implied,
                       MIN(COALESCE(lead_days, 999)) AS lead
                FROM iv_variant_evals {where}
                GROUP BY ticker, earnings_date""", params)

        events, stale = [], []
        for r in rows:
            spot, exit_spot, implied = r["spot"] or 0, r["exit_spot"] or 0, r["implied"] or 0
            if not (spot and exit_spot and implied):
                continue
            realized = abs(exit_spot / spot - 1) * 100
            lead = r["lead"] if r["lead"] is not None else 999
            ev = {"ticker": r["ticker"], "earnings_date": r["earnings_date"],
                  "implied_pct": round(implied, 2),
                  "realized_pct": round(realized, 2),
                  "edge_pct": round(implied - realized, 2),
                  "exceeded": realized > implied,
                  "lead_days": lead}
            (events if lead <= cutoff else stale).append(ev)

        def summarize(evs: list) -> dict:
            k = len(evs)
            if not k:
                return {"n_events": 0, "pct_exceeding_implied": None, "n_exceeding": 0,
                        "avg_implied_pct": None, "avg_realized_pct": None,
                        "avg_edge_pct": None, "events": []}
            hit = sum(1 for e in evs if e["exceeded"])
            return {
                "n_events": k,
                "pct_exceeding_implied": round(hit / k * 100, 1),
                "n_exceeding": hit,
                "avg_implied_pct": round(sum(e["implied_pct"] for e in evs) / k, 2),
                "avg_realized_pct": round(sum(e["realized_pct"] for e in evs) / k, 2),
                "avg_edge_pct": round(sum(e["edge_pct"] for e in evs) / k, 2),
                "events": sorted(evs, key=lambda e: e["edge_pct"]),
            }

        out = summarize(events)
        out["lead_cutoff_days"] = cutoff
        out["stale_capture"] = summarize(stale)
        return out

    async def get_risk_state(self, loss_factor: float = 0.5, win_factor: float = 2.0,
                            floor: float = 0.25, halt_streak: int = 3) -> dict:
        """Current size multiplier + loss streak, derived from closed condors.

        Anti-martingale: every loss halves the next bet, every win ratchets it
        back toward full. Derived rather than stored so it is self-healing and
        auditable — a stored counter drifts the first time a close is replayed
        or a row is corrected by hand.

        Only closes AFTER `rearmed_at` count, so clearing a halt genuinely
        resets the streak instead of leaving the bot one loss from re-halting.
        """
        ctl = await self._scalar("SELECT * FROM risk_control WHERE id=1", strict=True)
        if not ctl:
            raise DatabaseError("Risk control row is missing")
        rearmed = ctl.get("rearmed_at")
        params: tuple = ()
        where = "WHERE status='closed' AND pnl IS NOT NULL"
        if rearmed:
            where += " AND closed_at > ?"
            params = (rearmed,)
        rows = await self._query(
            f"SELECT ticker, pnl, closed_at FROM iv_condors {where} ORDER BY closed_at, id",
            params, strict=True)

        mult, streak = 1.0, 0
        for r in rows:
            if (r["pnl"] or 0) < 0:
                mult = max(floor, mult * loss_factor)
                streak += 1
            elif (r["pnl"] or 0) > 0:
                mult = min(1.0, mult * win_factor)
                streak = 0
            # A breakeven is neither evidence that the losing regime ended nor
            # a reason to increase size. Preserve the existing throttle/streak.
        return {
            "multiplier": round(mult, 4),
            "loss_streak": streak,
            "halt_streak": halt_streak,
            "halted": bool(ctl.get("halted")),
            "halted_at": ctl.get("halted_at"),
            "halted_reason": ctl.get("halted_reason"),
            "rearmed_at": rearmed,
            "closes_counted": len(rows),
        }

    async def set_halt(self, reason: str) -> None:
        await self._exec(
            "UPDATE risk_control SET halted=1, halted_at=?, halted_reason=? WHERE id=1",
            (datetime.utcnow().isoformat(), reason), strict=True, expected_rows=1)

    async def clear_halt(self) -> None:
        """Re-arm. Also resets the streak so the bot is not one loss from
        re-halting the moment a human says it may trade again."""
        await self._exec(
            """UPDATE risk_control SET halted=0, halted_at=NULL, halted_reason=NULL,
                 rearmed_at=? WHERE id=1""",
            (datetime.utcnow().isoformat(),), strict=True, expected_rows=1)

    async def get_variant_summary(self, gate_passed: Optional[bool] = True,
                                  source: Optional[str] = None,
                                  distinct_only: bool = False) -> list[dict]:
        """Full metric set per variant over resolved events, best expectancy first.

        `gate_passed=True` scores only setups that cleared the three scanner
        gates; False scores only failed gates; None scores the indiscriminate
        "sell everything" baseline. Comparing True vs None decides
        whether the gates earn their keep (VALIDATION_SPEC §4).

        `distinct_only` drops rows whose strikes collapsed onto an earlier
        shape. Use it when comparing shapes against each other: a collapsed row
        is the same structure under two names, so including it drags the
        comparison toward "no difference" for reasons of strike granularity
        rather than strategy. Each result reports `collapsed_n` either way.
        """
        from backtest.metrics import compute_metrics
        where = "WHERE resolved=1 AND pricing_model=?"
        params: tuple = (VALIDATED_VARIANT_PRICING_MODEL,)
        if gate_passed is not None:
            where += (" AND recommendation='SELL_PREMIUM'" if gate_passed
                      else " AND recommendation IN ('CONSIDER','AVOID')")
        if source is not None:
            if source not in VARIANT_SOURCES:
                raise ValueError(f"unknown variant source: {source}")
            where += " AND source=?"
            params += (source,)
        if distinct_only:
            where += " AND collapsed_with IS NULL"
        rows = await self._query(
            # max_drawdown is path-dependent, so this event order is part of
            # the metric definition rather than cosmetic presentation sorting.
            f"""SELECT variant, realized_pnl, max_loss, pin_risk, credit,
                       implied_move_pct, spot, exit_spot, collapsed_with
                FROM iv_variant_evals {where}
                ORDER BY COALESCE(expiry, resolved_at, signal_date), id""", params)

        by_variant: dict = {}
        for r in rows:
            by_variant.setdefault(r["variant"], []).append(r)

        out = []
        for variant, evs in by_variant.items():
            pnls = [float(e["realized_pnl"] or 0) for e in evs]
            # Did the underlying move more than the premium implied at entry?
            exceeded = []
            for e in evs:
                spot, exit_spot = e["spot"] or 0, e["exit_spot"] or 0
                im = e["implied_move_pct"] or 0
                exceeded.append(bool(spot and im and
                                     abs(exit_spot / spot - 1) * 100 > im))
            m = compute_metrics(pnls, exceeded_implied=exceeded)
            rors = [float(e["realized_pnl"]) / float(e["max_loss"]) * 100
                    for e in evs if e["max_loss"]]
            m.update({
                "variant": variant,
                "avg_pnl": m["expectancy"],
                "avg_ror_pct": (round(sum(rors) / len(rors), 1) if rors else None),
                "n": m["n_events"],
                "pin_events": sum(1 for e in evs if e["pin_risk"]),
                # How many of these events were the same structure under
                # another name — i.e. carried no shape information.
                "collapsed_n": sum(1 for e in evs if e["collapsed_with"]),
            })
            out.append(m)
        out.sort(key=lambda x: (x["expectancy"] is None, -(x["expectancy"] or 0)))
        return out

    async def get_gate_comparison(self, source: Optional[str] = None) -> dict:
        """Baseline 2: strict three-gate setups vs all events. CONSIDER is a
        subset of failed gates; legacy unknowns only enter the all-event baseline.
        If selling everything matches the filtered set, the gates are noise."""
        from backtest.metrics import compute_metrics
        if source is not None and source not in VARIANT_SOURCES:
            raise ValueError(f"unknown variant source: {source}")
        out = {}
        for label, gate_where in (
                ("gated", " AND recommendation='SELL_PREMIUM'"),
                ("ungated", ""),
                ("failed_gates", " AND recommendation IN ('CONSIDER','AVOID')"),
                ("consider", " AND recommendation='CONSIDER'"),
                ("unknown_gates", " AND recommendation IS NULL")):
            source_where = ""
            params: tuple = (VALIDATED_VARIANT_PRICING_MODEL, "condor_1.0sd")
            if source is not None:
                source_where = " AND source=?"
                params += (source,)
            rows = await self._query(
                # Keep the gated/ungated drawdown paths chronological too.
                """SELECT realized_pnl FROM iv_variant_evals
                   WHERE resolved=1 AND pricing_model=? AND variant=?
                   """ + gate_where + source_where +
                " ORDER BY COALESCE(expiry, resolved_at, signal_date), id",
                params)
            out[label] = compute_metrics([float(r["realized_pnl"] or 0) for r in rows])
        return out

    # ── Write: Options Flow ──────────────────────────────────────────────
    async def save_options_flow(self, event: dict):
        uid = event.get("id")
        if not uid:
            return
        await self._exec(
            """INSERT OR IGNORE INTO options_flow
               (id, ticker, premium, opt_type, alert_rule, has_sweep, strike,
                expiry, volume, open_interest, vol_oi_ratio, iv, ask_prem,
                bid_prem, underlying_price, sector, raw, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                uid,
                (event.get("ticker") or "").upper(),
                float(event.get("total_premium", 0) or 0),
                event.get("type", ""),
                event.get("alert_rule", ""),
                1 if event.get("has_sweep") else 0,
                float(event.get("strike", 0) or 0),
                event.get("expiry", ""),
                int(event.get("volume", 0) or 0),
                int(event.get("open_interest", 0) or 0),
                float(event.get("volume_oi_ratio", 0) or 0),
                float(event.get("iv_start", 0) or 0),
                float(event.get("total_ask_side_prem", 0) or 0),
                float(event.get("total_bid_side_prem", 0) or 0),
                float(event.get("underlying_price", 0) or 0),
                event.get("sector", ""),
                json.dumps(event),
                datetime.utcnow().isoformat(),
            ),
        )

    # ── Write: Dark Pool ─────────────────────────────────────────────────
    async def save_dark_pool(self, event: dict):
        tracking_id = event.get("tracking_id")
        if not tracking_id:
            return
        size    = float(event.get("size", 0) or 0)
        price   = float(event.get("price", 0) or 0)
        premium = float(event.get("premium", 0) or 0) or (size * price)
        await self._exec(
            """INSERT OR IGNORE INTO dark_pool
               (tracking_id, ticker, size, price, premium, nbbo_bid,
                nbbo_ask, market_center, executed_at, raw, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                tracking_id,
                (event.get("ticker") or "").upper(),
                size,
                price,
                premium,
                float(event.get("nbbo_bid", 0) or 0),
                float(event.get("nbbo_ask", 0) or 0),
                event.get("market_center", ""),
                event.get("executed_at", ""),
                json.dumps(event),
                datetime.utcnow().isoformat(),
            ),
        )

    # ── Write: Insider Trades ────────────────────────────────────────────
    async def save_insider_trade(self, event: dict):
        uid  = event.get("id")
        code = (event.get("transaction_code") or "").upper()
        if not uid or code not in ("P", "S", "D"):
            return  # skip awards, exercises, tax withholding
        shares    = abs(float(event.get("amount", 0) or 0))
        per_share = float(event.get("price", 0) or 0)
        dollar_val = shares * per_share if per_share > 0 else 0
        await self._exec(
            """INSERT OR IGNORE INTO insider_trades
               (id, ticker, owner_name, officer_title, transaction_code,
                shares, price_per_share, dollar_value, is_officer,
                is_director, is_10b5_1, transaction_date, filing_date, raw, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                uid,
                (event.get("ticker") or "").upper(),
                event.get("owner_name", ""),
                event.get("officer_title", ""),
                code,
                shares,
                per_share,
                dollar_val,
                1 if event.get("is_officer") else 0,
                1 if event.get("is_director") else 0,
                1 if event.get("is_10b5_1") else 0,
                event.get("transaction_date", ""),
                event.get("filing_date", ""),
                json.dumps(event),
                datetime.utcnow().isoformat(),
            ),
        )

    # ── Write: Congress Trades ───────────────────────────────────────────
    async def save_congress_trade(self, event: dict):
        ticker = (event.get("ticker") or "").upper()
        if not ticker:
            return
        # Composite PK: politician_id + transaction_date
        pol_id   = event.get("politician_id", "")
        txn_date = event.get("transaction_date", "")
        uid      = f"{pol_id}_{txn_date}_{ticker}"
        await self._exec(
            """INSERT OR IGNORE INTO congress_trades
               (id, ticker, member_name, chamber, txn_type, amounts,
                transaction_date, filed_at_date, raw, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                uid, ticker,
                event.get("name", ""),
                event.get("member_type", ""),
                event.get("txn_type", ""),
                event.get("amounts", ""),
                txn_date,
                event.get("filed_at_date", ""),
                json.dumps(event),
                datetime.utcnow().isoformat(),
            ),
        )

    # ── Write: Signals ───────────────────────────────────────────────────
    async def save_signal(self, signal, min_score: float = 0.0):
        if signal.score < min_score:
            return
        await self._exec(
            """INSERT INTO signals
               (type, ticker, score, side, title, description,
                premium, expiry, strike, option_type, raw, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal.type.value, signal.ticker,
                round(signal.score, 4), signal.side.value,
                signal.title, signal.description,
                signal.premium, signal.expiry, signal.strike,
                signal.option_type, json.dumps(signal.raw),
                datetime.utcnow().isoformat(),
            ),
        )

    # ── Write: Pattern Hits ──────────────────────────────────────────────
    async def save_pattern_hit(
        self, pattern_name: str, ticker: str,
        score: float, description: str, evidence: list
    ):
        await self._exec(
            """INSERT INTO pattern_hits
               (pattern_name, ticker, score, description, evidence, created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                pattern_name, ticker, score, description,
                json.dumps(evidence), datetime.utcnow().isoformat(),
            ),
        )

    async def was_pattern_recently_hit(
        self, pattern_name: str, ticker: str, within_hours: int = 24
    ) -> bool:
        """Prevent re-alerting same pattern+ticker within cooldown window."""
        rows = await self._query(
            """SELECT id FROM pattern_hits
               WHERE pattern_name=? AND ticker=?
               AND created_at >= datetime('now', ?)
               LIMIT 1""",
            (pattern_name, ticker, f"-{within_hours} hours"),
        )
        return len(rows) > 0

    # ── Write/Read: Pending Trades ───────────────────────────────────────
    async def save_pending_trade(self, expires_at, **kwargs) -> Optional[int]:
        """Insert a pending trade and return its auto-increment id."""
        try:
            sql = """
                INSERT INTO pending_trades
                  (ticker, trade_type, symbol, side, option_type, strike, expiry, dte,
                   qty, limit_price, risk_amount, stop_pct, target_pct, score,
                   rationale, strategy, created_at, expires_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """
            params = (
                kwargs.get("ticker", ""),
                kwargs.get("trade_type", ""),
                kwargs.get("symbol", ""),
                kwargs.get("side", "bullish"),
                kwargs.get("option_type"),
                kwargs.get("strike"),
                kwargs.get("expiry"),
                kwargs.get("dte"),
                kwargs.get("qty", 1),
                kwargs.get("limit_price", 0),
                kwargs.get("risk_amount", 0),
                kwargs.get("stop_pct", 40.0),
                kwargs.get("target_pct", 80.0),
                kwargs.get("score", 0),
                kwargs.get("rationale", ""),
                kwargs.get("strategy", ""),
                datetime.utcnow().isoformat(),
                expires_at.isoformat() if hasattr(expires_at, "isoformat") else str(expires_at),
            )
            return await self._exec(sql, params, strict=True, expected_rows=1)
        except Exception as e:
            logger.error(f"save_pending_trade error: {e}")
            return None

    async def update_pending_trade(self, trade_id: int, **kwargs):
        """Update arbitrary columns on a pending trade by id."""
        if not kwargs:
            return
        allowed = {
            "status", "telegram_msg_id", "alpaca_order_id", "executed_at", "entry_order_status",
            "qty", "risk_amount",
        }
        cols = {k: v for k, v in kwargs.items() if k in allowed}
        if not cols:
            return
        set_clause = ", ".join(f"{k}=?" for k in cols)
        params = list(cols.values()) + [trade_id]
        # Claim and resized economics commit together before the broker POST.
        # An accepted/uncertain row can never be claimed as a fresh submission.
        condition = " AND status='pending'" if cols.get("status") == "submitting" else ""
        await self._exec(f"UPDATE pending_trades SET {set_clause} WHERE id=?{condition}", params,
                         strict=True, expected_rows=1)

    async def get_pending_trades(self, status: str = "pending") -> list[dict]:
        return await self._query(
            "SELECT * FROM pending_trades WHERE status=? ORDER BY created_at DESC",
            (status,), strict=True,
        )

    async def get_entry_reservations(self) -> list[dict]:
        """Entries not yet reconciled to terminal broker state, including crashes."""
        return await self._query(
            """SELECT * FROM pending_trades
               WHERE status IN ('submitting','submission_unknown')
                  OR (status='confirmed' AND COALESCE(entry_order_status,'unknown')
                      NOT IN ('filled','canceled','expired','rejected','replaced'))""",
            strict=True)

    async def expire_stale_pending_trades(self) -> int:
        """Mark any still-'pending' trades whose expires_at is in the past as 'expired'.

        The per-trade expiry is an in-memory asyncio task (see auto_trade._expire),
        which is lost on process restart. Without this startup sweep, trades that
        were pending when the backend restarted stay 'pending' in the DB forever
        (observed: rows from 2026-04-21 still pending in June). Compares against
        UTC now since expires_at is stored as UTC isoformat.
        """
        now_iso = datetime.utcnow().isoformat()
        stale = await self._scalar(
            "SELECT COUNT(*) AS n FROM pending_trades "
            "WHERE status='pending' AND expires_at < ?",
            (now_iso,),
        )
        n = int(stale.get("n") or 0) if stale else 0
        if n:
            await self._exec(
                "UPDATE pending_trades SET status='expired' "
                "WHERE status='pending' AND expires_at < ?",
                (now_iso,),
            )
        return n

    async def get_trade_history(self, limit: int = 50) -> list[dict]:
        return await self._query(
            "SELECT * FROM pending_trades ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    async def count_confirmed_today(self, date_str: str, *, include_unresolved: bool = False) -> int:
        """Count submissions in an ET day; unresolved attempts reserve daily capacity.

        Use execution time rather than queue creation time. Naive stored timestamps
        are UTC; the day bounds account for ET's changing UTC offset.
        """
        start = datetime.combine(date.fromisoformat(date_str), datetime.min.time(), _ET)
        end = start + timedelta(days=1)
        extra = " OR status IN ('submitting','submission_unknown')" if include_unresolved else ""
        result = await self._scalar(
            f"""SELECT COUNT(*) AS n FROM pending_trades
                WHERE (status='confirmed'
                  AND julianday(COALESCE(executed_at,created_at)) >= julianday(?)
                  AND julianday(COALESCE(executed_at,created_at)) < julianday(?)){extra}""",
            (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
            strict=True,
        )
        return int(result["n"])

    # ── Write/Read: Trade Performance ──────────────────────────────────────
    async def get_performance_orders(self) -> list[dict]:
        """All persisted order snapshots; a bounded UI history is insufficient."""
        return await self._query("SELECT * FROM trade_performance", strict=True)

    async def get_trade_fill_sync_start(self) -> Optional[str]:
        """First sync reads all history; later syncs overlap by at least a day.

        Order/activity quantity gaps pull the start back to the affected order,
        including partial fills arriving late. A watermark advances only after
        a complete broker fetch has been persisted.
        """
        meta = await self._scalar(
            "SELECT value FROM db_meta WHERE key='trade_fill_sync_at'", strict=True)
        if not meta:
            return None
        start = datetime.fromisoformat(meta["value"]) - timedelta(days=1)
        gaps = await self._query(
            """SELECT t.submitted_at, t.filled_at FROM trade_performance t
               LEFT JOIN (SELECT alpaca_order_id, SUM(qty) AS qty FROM trade_fills
                          GROUP BY alpaca_order_id) f ON f.alpaca_order_id=t.alpaca_order_id
               WHERE ABS(COALESCE(t.filled_qty,0) - COALESCE(f.qty,0)) > 0.00000001""", strict=True)
        for row in gaps:
            for field in ("submitted_at", "filled_at"):
                if row.get(field):
                    at = datetime.fromisoformat(row[field].replace("Z", "+00:00"))
                    start = min(start, at.replace(tzinfo=at.tzinfo or timezone.utc))
        # 'after' is exclusive; pad the earliest date rather than lose a fill
        # exactly at an order's submitted_at (or exactly at midnight).
        return (start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                - timedelta(days=1)).isoformat()

    async def mark_trade_fill_sync(self, started_at: str) -> None:
        await self._exec(
            """INSERT INTO db_meta (key, value) VALUES ('trade_fill_sync_at', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (started_at,), strict=True)

    async def record_trade_fills(self, activities: list[dict]) -> None:
        """Persist a complete, validated activity batch atomically and by ID.

        Alpaca's qty/price are for each execution, not the order's cumulative
        filled quantity/average. Replayed pages and restarts cannot duplicate
        fills. Broker corrections replace the same activity's previous values.
        """
        values = []
        for a in activities:
            qty, price = float(a["qty"]), float(a["price"])
            at = datetime.fromisoformat(a["transaction_time"].replace("Z", "+00:00"))
            at = at.replace(tzinfo=at.tzinfo or timezone.utc).astimezone(timezone.utc)
            if (not all(isinstance(a.get(k), str) and a[k] for k in ("id", "order_id", "symbol"))
                    or a.get("side") not in ("buy", "sell")
                    or not math.isfinite(qty) or not math.isfinite(price) or qty <= 0 or price <= 0):
                raise ValueError("Invalid broker fill activity")
            values.append((a["id"], a["order_id"], a["symbol"], a["side"], qty, price, at.isoformat()))
        if not values:
            return
        async with self._write_lock:
            try:
                async with self._conn.executemany(
                    """INSERT INTO trade_fills
                       (activity_id, alpaca_order_id, symbol, side, qty, price, executed_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(activity_id) DO UPDATE SET
                         alpaca_order_id=excluded.alpaca_order_id, symbol=excluded.symbol,
                         side=excluded.side, qty=excluded.qty, price=excluded.price,
                         executed_at=excluded.executed_at""", values):
                    pass
                await self._conn.commit()
            except BaseException:
                try:
                    await self._conn.rollback()
                except Exception:
                    await self._conn.close()
                raise

    async def upsert_trade_performance(self, *, strict: bool = False, **kwargs):
        """Insert or update a trade performance record by alpaca_order_id."""
        order_id = kwargs.get("alpaca_order_id")
        if not order_id:
            return
        now = datetime.utcnow().isoformat()
        # Check if exists
        existing = await self._query(
            "SELECT id FROM trade_performance WHERE alpaca_order_id=?", (order_id,), strict=strict
        )
        if existing:
            # Update mutable fields
            updatable = {
                "filled_qty", "filled_avg_price", "order_status", "filled_at",
                "exit_price", "exit_reason", "realized_pnl", "realized_pnl_pct",
                "position_intent",
            }
            cols = {k: v for k, v in kwargs.items() if k in updatable and v is not None}
            if cols:
                cols["updated_at"] = now
                set_clause = ", ".join(f"{k}=?" for k in cols)
                params = list(cols.values()) + [order_id]
                await self._exec(
                    f"UPDATE trade_performance SET {set_clause} WHERE alpaca_order_id=?",
                    params, strict=strict
                )
        else:
            # Attribution: pull the strategy that queued this order (joined on the
            # order id the confirm flow wrote back) + the entry hour in ET.
            strategy = ""
            pend = await self._query(
                "SELECT strategy FROM pending_trades WHERE alpaca_order_id=? LIMIT 1",
                (order_id,), strict=strict,
            )
            if pend:
                strategy = pend[0].get("strategy") or ""
            await self._exec(
                """INSERT OR IGNORE INTO trade_performance
                   (alpaca_order_id, symbol, ticker, side, qty, filled_qty,
                    filled_avg_price, order_type, order_status, submitted_at,
                    filled_at, signal_score, trade_type, strategy, entry_hour_et,
                    created_at, updated_at, position_intent)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    order_id,
                    kwargs.get("symbol", ""),
                    kwargs.get("ticker", ""),
                    kwargs.get("side", ""),
                    kwargs.get("qty", 0),
                    kwargs.get("filled_qty", 0),
                    kwargs.get("filled_avg_price", 0),
                    kwargs.get("order_type", ""),
                    kwargs.get("order_status", ""),
                    kwargs.get("submitted_at", ""),
                    kwargs.get("filled_at", ""),
                    kwargs.get("signal_score"),
                    kwargs.get("trade_type", ""),
                    strategy,
                    _et_hour(kwargs.get("submitted_at", "")),
                    now, now,
                    kwargs.get("position_intent"),
                ), strict=strict,
            )

    async def record_exit(self, symbol: str, exit_price: float, exit_reason: str,
                          realized_pnl: float, realized_pnl_pct: float):
        """Record exit info on the most recent open entry for this symbol."""
        now = datetime.utcnow().isoformat()
        # Find most recent entry without an exit
        rows = await self._query(
            f"""SELECT id, submitted_at FROM trade_performance
               WHERE (symbol=? OR ticker=?) AND {LONG_ENTRY_PREDICATE} AND exit_reason IS NULL
               ORDER BY created_at DESC LIMIT 1""",
            (symbol, symbol),
        )
        if rows:
            hold_minutes = _minutes_between(rows[0].get("submitted_at"), now)
            await self._exec(
                """UPDATE trade_performance
                   SET exit_price=?, exit_reason=?, realized_pnl=?,
                       realized_pnl_pct=?, hold_minutes=?, updated_at=?
                   WHERE id=?""",
                (exit_price, exit_reason, realized_pnl, realized_pnl_pct,
                 hold_minutes, now, rows[0]["id"]),
            )

    async def reconcile_trades(self, *, require_fills: bool = False) -> int:
        """Match executions FIFO and attribute realized long P&L to buy orders.

        The broker sync requires the actual activity ledger. Legacy callers may
        use completed order snapshots only when a symbol has no activities;
        partial orders never use submission time as an execution timestamp.
        Incomplete activity coverage defers the whole symbol, preserving its
        last result until the broker catches up. Options use a ×100 multiplier.

        Broker opening/closing intent takes precedence. An orphan buy-to-close
        never creates a long, even when its short opened outside this ledger.
        Without explicit intent, unmatched sells establish short inventory and
        later buys cover that first. Short P&L remains outside the
        existing long-entry metrics. Returns the number of buy rows changed.
        """
        from collections import deque

        rows = await self.get_performance_orders()
        activities = await self._query("SELECT * FROM trade_fills", strict=True)
        by_order: dict[str, list[dict]] = {}
        for a in activities:
            by_order.setdefault(a["alpaca_order_id"], []).append(a)
        by_symbol: dict[str, list[dict]] = {}
        for r in rows:
            if (r["filled_qty"] or 0) > 0 or r["alpaca_order_id"] in by_order:
                by_symbol.setdefault(r["symbol"], []).append(r)

        own_reasons = (None, "", "closed_win", "closed_loss")
        updated = 0
        now = datetime.utcnow().isoformat()
        for symbol, orders in by_symbol.items():
            use_activities = require_fills or any(o["alpaca_order_id"] in by_order for o in orders)
            fills = []
            complete = True
            for row in orders:
                order_fills = by_order.get(row["alpaca_order_id"], [])
                if use_activities:
                    qty = sum(f["qty"] for f in order_fills)
                    if (not order_fills or not math.isclose(qty, row["filled_qty"] or 0, abs_tol=1e-8, rel_tol=1e-8)
                            or any(f["symbol"] != symbol or f["side"] != row["side"] for f in order_fills)):
                        complete = False
                        break
                    fills.extend({**f, "row": row, "at": f["executed_at"]} for f in order_fills)
                else:
                    if (row["order_status"] != "filled" or not row["filled_at"]
                            or not math.isfinite(row["filled_avg_price"] or 0) or (row["filled_avg_price"] or 0) <= 0):
                        complete = False
                        break
                    try:
                        at = datetime.fromisoformat(row["filled_at"].replace("Z", "+00:00"))
                        at = at.replace(tzinfo=at.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
                    except (TypeError, ValueError):
                        complete = False
                        break
                    fills.append({"row": row, "side": row["side"], "qty": row["filled_qty"],
                                  "price": row["filled_avg_price"], "at": at,
                                  "activity_id": f"{row['id']:020d}"})
            if not complete:
                logger.warning("Performance reconciliation deferred for %s: incomplete fill history", symbol)
                continue
            fills.sort(key=lambda f: (f["at"], f["activity_id"]))
            lots = deque()
            exits = {}
            order_ids = {r["id"]: r["alpaca_order_id"] for r in orders}
            short_qty = 0.0
            stats = {r["id"]: {"sold": 0.0, "cost": 0.0, "proceeds": 0.0, "hold": 0.0}
                     for r in orders if r["side"] == "buy"}
            for f in fills:
                qty, price = f["qty"], f["price"]
                if f["side"] == "buy":
                    intent = f["row"].get("position_intent")
                    cover = min(qty, short_qty) if intent != "buy_to_open" else 0
                    short_qty -= cover
                    qty -= cover
                    if intent == "buy_to_close":
                        continue  # Never invent a long when its short entry is outside this ledger.
                    if qty > 1e-8:
                        lots.append({"id": f["row"]["id"], "price": price, "open": qty, "at": f["at"]})
                elif f["side"] == "sell":
                    intent = f["row"].get("position_intent")
                    while qty > 1e-8 and lots and intent != "sell_to_open":
                        lot = lots[0]
                        take = min(qty, lot["open"])
                        s = stats[lot["id"]]
                        s["sold"] += take
                        s["cost"] += take * lot["price"]
                        s["proceeds"] += take * price
                        s["hold"] += take * _minutes_between(lot["at"], f["at"])
                        key = (f["activity_id"], order_ids[lot["id"]])
                        event = exits.setdefault(key, {"pnl": 0.0, "at": f["at"]})
                        event["pnl"] += (price - lot["price"]) * take * (100 if _is_occ(symbol) else 1)
                        lot["open"] -= take
                        qty -= take
                        if lot["open"] <= 1e-8:
                            lots.popleft()
                    if intent != "sell_to_close":
                        short_qty += max(qty, 0.0)

            remaining = {}
            for lot in lots:
                remaining[lot["id"]] = remaining.get(lot["id"], 0.0) + lot["open"]

            # Rebuild each complete symbol atomically. Corrections and replayed
            # activities replace matches rather than incrementing a counter.
            if use_activities:
                async with self._write_lock:
                    try:
                        await self._conn.execute("DELETE FROM realized_trade_exits WHERE symbol=?", (symbol,))
                        await self._conn.executemany(
                            "INSERT INTO realized_trade_exits VALUES (?,?,?,?,?,?)",
                            [(aid, oid, symbol, symbol[:-15] if _is_occ(symbol) else symbol,
                              round(event["pnl"], 2), event["at"])
                             for (aid, oid), event in exits.items()])
                        await self._conn.commit()
                    except BaseException:
                        await self._conn.rollback()
                        raise

            mult = 100 if _is_occ(symbol) else 1
            for row in orders:
                if row["side"] != "buy":
                    continue
                s = stats[row["id"]]
                open_qty = remaining.get(row["id"], 0.0)
                entry_qty = s["sold"] + open_qty
                if (row.get("long_entry_qty"), row.get("open_qty")) != (entry_qty, open_qty):
                    await self._exec(
                        "UPDATE trade_performance SET long_entry_qty=?,open_qty=? WHERE id=?",
                        (entry_qty, open_qty, row["id"]), strict=True)
                if s["sold"] <= 0:
                    # Clear stale matcher P&L on open entries and short covers.
                    if row["realized_pnl"] is not None and row["exit_reason"] in ("closed_win", "closed_loss"):
                        await self._exec(
                            """UPDATE trade_performance
                               SET realized_pnl=NULL, realized_pnl_pct=NULL, exit_price=NULL,
                                   exit_reason=NULL, hold_minutes=NULL, updated_at=?
                               WHERE id=?""", (now, row["id"]), strict=True)
                        updated += 1
                    continue
                pnl = round((s["proceeds"] - s["cost"]) * mult, 2)
                pnl_pct = round((s["proceeds"] / s["cost"] - 1) * 100, 2)
                exit_price = round(s["proceeds"] / s["sold"], 4)
                hold = round(s["hold"] / s["sold"], 1)
                reason = "closed_win" if pnl >= 0 else "closed_loss"
                if row["exit_reason"] not in own_reasons:
                    reason = row["exit_reason"]
                    hold = row["hold_minutes"] if row["hold_minutes"] is not None else hold
                if (row["realized_pnl"], row["realized_pnl_pct"], row["exit_price"],
                        row["exit_reason"], row["hold_minutes"]) == (pnl, pnl_pct, exit_price, reason, hold):
                    continue
                await self._exec(
                    """UPDATE trade_performance
                       SET realized_pnl=?, realized_pnl_pct=?, exit_price=?,
                           exit_reason=?, hold_minutes=?, updated_at=? WHERE id=?""",
                    (pnl, pnl_pct, exit_price, reason, hold, now, row["id"]), strict=True)
                updated += 1
        return updated

    async def get_trade_performance(self, limit: int = 100, ticker: str = None,
                                     status: str = None) -> list[dict]:
        conds, params = ["1=1"], []
        if ticker:
            conds.append("ticker=?")
            params.append(ticker.upper())
        if status:
            conds.append("order_status=?")
            params.append(status)
        params.append(limit)
        return await self._query(
            f"""SELECT * FROM trade_performance
                WHERE {' AND '.join(conds)}
                ORDER BY created_at DESC LIMIT ?""",
            params,
        )

    async def get_performance_summary(self) -> dict:
        """Aggregate performance stats across all closed trades."""
        summary = await self._scalar(
            f"""SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as winners,
                SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losers,
                SUM(CASE WHEN ({OPEN_LONG_QTY_SQL})>0 THEN 1 ELSE 0 END) as open_trades,
                SUM(CASE WHEN realized_pnl IS NOT NULL THEN 1 ELSE 0 END) as closed_trades,
                SUM(realized_pnl) as total_pnl,
                AVG(realized_pnl) as avg_pnl,
                AVG(realized_pnl_pct) as avg_pnl_pct,
                MAX(realized_pnl) as best_trade,
                MIN(realized_pnl) as worst_trade,
                AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END) as avg_win,
                AVG(CASE WHEN realized_pnl < 0 THEN realized_pnl END) as avg_loss,
                SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END) as gross_win,
                SUM(CASE WHEN realized_pnl < 0 THEN -realized_pnl ELSE 0 END) as gross_loss
               FROM trade_performance WHERE {LONG_ENTRY_PREDICATE}"""
        )
        for key in ("total_trades", "winners", "losers", "open_trades", "closed_trades"):
            summary[key] = summary.get(key) or 0
        # Breakeven closes still belong in the win-rate denominator.
        winners = summary["winners"]
        total_closed = summary["closed_trades"]
        summary["win_rate"] = round(winners / total_closed * 100, 1) if total_closed > 0 else 0
        # Profit factor
        gross_win = summary.pop("gross_win") or 0
        gross_loss = summary.pop("gross_loss") or 0
        summary["profit_factor"] = round(gross_win / gross_loss, 2) if gross_loss else None
        return summary

    # ── Read: Per-feed queries ───────────────────────────────────────────
    async def get_options_flow(
        self, ticker=None, min_premium=0, alert_rule=None,
        has_sweep=None, limit=100, offset=0
    ) -> list[dict]:
        conds, params = ["premium >= ?"], [min_premium]
        if ticker:      conds.append("ticker=?");      params.append(ticker.upper())
        if alert_rule:  conds.append("alert_rule LIKE ?"); params.append(f"%{alert_rule}%")
        if has_sweep is not None:
            conds.append("has_sweep=?"); params.append(1 if has_sweep else 0)
        params += [limit, offset]
        return await self._query(
            f"SELECT * FROM options_flow WHERE {' AND '.join(conds)} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params
        )

    async def get_dark_pool(
        self, ticker=None, min_premium=0, limit=100, offset=0
    ) -> list[dict]:
        conds, params = ["premium >= ?"], [min_premium]
        if ticker: conds.append("ticker=?"); params.append(ticker.upper())
        params += [limit, offset]
        return await self._query(
            f"SELECT * FROM dark_pool WHERE {' AND '.join(conds)} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params
        )

    async def get_insider_trades(
        self, ticker=None, code=None, min_value=0, limit=100, offset=0
    ) -> list[dict]:
        conds, params = ["dollar_value >= ?"], [min_value]
        if ticker: conds.append("ticker=?"); params.append(ticker.upper())
        if code:   conds.append("transaction_code=?"); params.append(code.upper())
        params += [limit, offset]
        return await self._query(
            f"SELECT * FROM insider_trades WHERE {' AND '.join(conds)} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params
        )

    async def get_congress_trades(
        self, ticker=None, txn_type=None, limit=100, offset=0
    ) -> list[dict]:
        conds, params = ["1=1"], []
        if ticker:   conds.append("ticker=?");   params.append(ticker.upper())
        if txn_type: conds.append("txn_type LIKE ?"); params.append(f"%{txn_type}%")
        params += [limit, offset]
        return await self._query(
            f"SELECT * FROM congress_trades WHERE {' AND '.join(conds)} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params
        )

    async def get_signals(
        self, ticker=None, signal_type=None, min_score=0,
        limit=100, offset=0
    ) -> list[dict]:
        conds, params = ["score >= ?"], [min_score]
        if ticker:      conds.append("ticker=?"); params.append(ticker.upper())
        if signal_type: conds.append("type=?");   params.append(signal_type)
        params += [limit, offset]
        return await self._query(
            f"SELECT * FROM signals WHERE {' AND '.join(conds)} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params
        )

    async def get_pattern_hits(
        self, ticker=None, pattern=None, limit=50
    ) -> list[dict]:
        conds, params = ["1=1"], []
        if ticker:  conds.append("ticker=?");       params.append(ticker.upper())
        if pattern: conds.append("pattern_name=?"); params.append(pattern)
        params.append(limit)
        return await self._query(
            f"SELECT * FROM pattern_hits WHERE {' AND '.join(conds)} ORDER BY created_at DESC LIMIT ?",
            params
        )

    # ── Analytics queries ────────────────────────────────────────────────
    async def get_seen_ids(self) -> set:
        """
        Return a set of all IDs already persisted across slow-moving feeds
        (congress + insider). Used to pre-populate the UW feed's dedup set
        on startup so restarts don't re-fire old events as notifications.
        """
        ids: set = set()
        for sql in [
            "SELECT id FROM congress_trades",
            "SELECT id FROM insider_trades",
        ]:
            rows = await self._query(sql)
            for r in rows:
                if r.get("id"):
                    ids.add(r["id"])
        return ids

    async def get_db_stats(self) -> dict:
        stats = {}
        for tbl in ["options_flow", "dark_pool", "insider_trades",
                    "congress_trades", "signals", "pattern_hits"]:
            r = await self._scalar(f"SELECT COUNT(*) as n FROM {tbl}")
            stats[tbl] = r.get("n", 0)
        return stats

    async def get_signal_stats(self) -> dict:
        """Counts over every persisted signal — the History tab's `Stats` shape."""
        empty = {"total": 0, "elite": 0, "high": 0, "bull": 0, "bear": 0,
                 "avg_score": None, "last_signal": None}
        row = await self._scalar(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN score >= 9 THEN 1 ELSE 0 END), 0) AS elite,
                      COALESCE(SUM(CASE WHEN score >= 7 THEN 1 ELSE 0 END), 0) AS high,
                      COALESCE(SUM(CASE WHEN side='bullish' THEN 1 ELSE 0 END), 0) AS bull,
                      COALESCE(SUM(CASE WHEN side='bearish' THEN 1 ELSE 0 END), 0) AS bear,
                      AVG(score) AS avg_score,
                      MAX(created_at) AS last_signal
               FROM signals""")
        return {**empty, **row}

    async def get_signal_top_tickers(self, limit: int = 10) -> list[dict]:
        """All-time ranking by signal count — the History tab's `TopTicker` shape.
        (get_top_tickers below is the Analytics tab's windowed ranking, with
        different field names; the two are not interchangeable.)"""
        return await self._query(
            """SELECT ticker,
                      COUNT(*) AS signal_count,
                      MAX(score) AS max_score,
                      AVG(score) AS avg_score,
                      SUM(CASE WHEN side='bullish' THEN 1 ELSE 0 END) AS bull_count,
                      SUM(CASE WHEN side='bearish' THEN 1 ELSE 0 END) AS bear_count
               FROM signals
               GROUP BY ticker
               ORDER BY signal_count DESC, max_score DESC
               LIMIT ?""",
            (int(limit),))

    async def get_top_tickers(self, days: int = 7, limit: int = 20) -> list[dict]:
        """Cross-feed ticker ranking by total signal activity."""
        return await self._query(
            """
            SELECT ticker,
                   COUNT(*) AS total_signals,
                   MAX(score) AS max_score,
                   AVG(score) AS avg_score,
                   SUM(CASE WHEN side='bullish' THEN 1 ELSE 0 END) AS bull,
                   SUM(CASE WHEN side='bearish' THEN 1 ELSE 0 END) AS bear,
                   MAX(created_at) AS last_seen
            FROM signals
            WHERE created_at >= datetime('now', ?)
            GROUP BY ticker
            ORDER BY total_signals DESC, max_score DESC
            LIMIT ?
            """,
            (f"-{days} days", limit),
        )

    async def get_ticker_profile(self, ticker: str) -> dict:
        """Full cross-feed summary for a single ticker."""
        t = ticker.upper()
        of  = await self._scalar("SELECT COUNT(*) as n, MAX(premium) as max_prem, SUM(CASE WHEN opt_type='call' THEN 1 ELSE 0 END) as calls, SUM(CASE WHEN opt_type='put' THEN 1 ELSE 0 END) as puts FROM options_flow WHERE ticker=?", (t,))
        dp  = await self._scalar("SELECT COUNT(*) as n, SUM(premium) as total, MAX(premium) as max FROM dark_pool WHERE ticker=?", (t,))
        it  = await self._scalar("SELECT COUNT(*) as n, SUM(CASE WHEN transaction_code='P' THEN 1 ELSE 0 END) as buys, SUM(CASE WHEN transaction_code IN ('S','D') THEN 1 ELSE 0 END) as sells FROM insider_trades WHERE ticker=?", (t,))
        ct  = await self._scalar("SELECT COUNT(*) as n, SUM(CASE WHEN txn_type='Buy' THEN 1 ELSE 0 END) as buys FROM congress_trades WHERE ticker=?", (t,))
        sig = await self._scalar("SELECT COUNT(*) as n, MAX(score) as max_score, AVG(score) as avg_score FROM signals WHERE ticker=?", (t,))
        ph  = await self._scalar("SELECT COUNT(*) as n FROM pattern_hits WHERE ticker=?", (t,))
        return {
            "ticker": t,
            "options_flow": of, "dark_pool": dp,
            "insider_trades": it, "congress_trades": ct,
            "signals": sig, "pattern_hits": ph,
        }

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
import logging
import aiosqlite
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "stonkmonitor.db"

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
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tp_symbol    ON trade_performance(symbol);
CREATE INDEX IF NOT EXISTS idx_tp_ticker    ON trade_performance(ticker);
CREATE INDEX IF NOT EXISTS idx_tp_status    ON trade_performance(order_status);
CREATE INDEX IF NOT EXISTS idx_tp_created   ON trade_performance(created_at DESC);

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
"""


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        logger.info(f"Database ready: {self.path}")

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def _exec(self, sql: str, params=()):
        try:
            await self._conn.execute(sql, params)
            await self._conn.commit()
        except aiosqlite.IntegrityError:
            pass  # duplicate primary key — already stored
        except Exception as e:
            logger.error(f"DB write error: {e} | sql={sql[:60]}")

    async def _query(self, sql: str, params=()) -> list[dict]:
        try:
            async with self._conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"DB query error: {e}")
            return []

    async def _scalar(self, sql: str, params=()):
        try:
            async with self._conn.execute(sql, params) as cur:
                row = await cur.fetchone()
                return dict(row) if row else {}
        except Exception as e:
            logger.error(f"DB scalar error: {e}")
            return {}

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
                   rationale, created_at, expires_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                datetime.utcnow().isoformat(),
                expires_at.isoformat() if hasattr(expires_at, "isoformat") else str(expires_at),
            )
            async with self._conn.execute(sql, params) as cur:
                await self._conn.commit()
                return cur.lastrowid
        except Exception as e:
            logger.error(f"save_pending_trade error: {e}")
            return None

    async def update_pending_trade(self, trade_id: int, **kwargs):
        """Update arbitrary columns on a pending trade by id."""
        if not kwargs:
            return
        allowed = {
            "status", "telegram_msg_id", "alpaca_order_id", "executed_at"
        }
        cols = {k: v for k, v in kwargs.items() if k in allowed}
        if not cols:
            return
        set_clause = ", ".join(f"{k}=?" for k in cols)
        params = list(cols.values()) + [trade_id]
        await self._exec(f"UPDATE pending_trades SET {set_clause} WHERE id=?", params)

    async def get_pending_trades(self, status: str = "pending") -> list[dict]:
        return await self._query(
            "SELECT * FROM pending_trades WHERE status=? ORDER BY created_at DESC",
            (status,),
        )

    async def get_trade_history(self, limit: int = 50) -> list[dict]:
        return await self._query(
            "SELECT * FROM pending_trades ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    # ── Write/Read: Trade Performance ──────────────────────────────────────
    async def upsert_trade_performance(self, **kwargs):
        """Insert or update a trade performance record by alpaca_order_id."""
        order_id = kwargs.get("alpaca_order_id")
        if not order_id:
            return
        now = datetime.utcnow().isoformat()
        # Check if exists
        existing = await self._query(
            "SELECT id FROM trade_performance WHERE alpaca_order_id=?", (order_id,)
        )
        if existing:
            # Update mutable fields
            updatable = {
                "filled_qty", "filled_avg_price", "order_status", "filled_at",
                "exit_price", "exit_reason", "realized_pnl", "realized_pnl_pct",
            }
            cols = {k: v for k, v in kwargs.items() if k in updatable and v is not None}
            if cols:
                cols["updated_at"] = now
                set_clause = ", ".join(f"{k}=?" for k in cols)
                params = list(cols.values()) + [order_id]
                await self._exec(
                    f"UPDATE trade_performance SET {set_clause} WHERE alpaca_order_id=?",
                    params
                )
        else:
            await self._exec(
                """INSERT OR IGNORE INTO trade_performance
                   (alpaca_order_id, symbol, ticker, side, qty, filled_qty,
                    filled_avg_price, order_type, order_status, submitted_at,
                    filled_at, signal_score, trade_type, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    now, now,
                ),
            )

    async def record_exit(self, symbol: str, exit_price: float, exit_reason: str,
                          realized_pnl: float, realized_pnl_pct: float):
        """Record exit info on the most recent open entry for this symbol."""
        now = datetime.utcnow().isoformat()
        # Find most recent entry without an exit
        rows = await self._query(
            """SELECT id FROM trade_performance
               WHERE (symbol=? OR ticker=?) AND side='buy' AND exit_reason IS NULL
               ORDER BY created_at DESC LIMIT 1""",
            (symbol, symbol),
        )
        if rows:
            await self._exec(
                """UPDATE trade_performance
                   SET exit_price=?, exit_reason=?, realized_pnl=?,
                       realized_pnl_pct=?, updated_at=?
                   WHERE id=?""",
                (exit_price, exit_reason, realized_pnl, realized_pnl_pct, now, rows[0]["id"]),
            )

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
            """SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as winners,
                SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losers,
                SUM(CASE WHEN realized_pnl IS NULL THEN 1 ELSE 0 END) as open_trades,
                SUM(realized_pnl) as total_pnl,
                AVG(realized_pnl) as avg_pnl,
                AVG(realized_pnl_pct) as avg_pnl_pct,
                MAX(realized_pnl) as best_trade,
                MIN(realized_pnl) as worst_trade,
                AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END) as avg_win,
                AVG(CASE WHEN realized_pnl < 0 THEN realized_pnl END) as avg_loss
               FROM trade_performance WHERE side='buy'"""
        )
        # Win rate
        winners = summary.get("winners") or 0
        losers = summary.get("losers") or 0
        total_closed = winners + losers
        summary["win_rate"] = round(winners / total_closed * 100, 1) if total_closed > 0 else 0
        # Profit factor
        avg_win = abs(summary.get("avg_win") or 0)
        avg_loss = abs(summary.get("avg_loss") or 1)
        summary["profit_factor"] = round(avg_win / avg_loss, 2) if avg_loss > 0 else 0
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

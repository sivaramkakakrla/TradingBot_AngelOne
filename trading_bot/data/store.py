"""
data/store.py — SQLite database layer for Project Candles.

Tables:
    candles          – OHLCV data per timeframe
    orders           – order log (placed, filled, rejected)
    trades           – trade lifecycle (entry → exit)
    daily_pnl        – per‑day profit/loss summary
    signals          – every signal emitted by the signal engine
    backtest_results – backtest run summaries

All timestamps stored as ISO‑8601 TEXT in IST.
"""

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from trading_bot import config
from trading_bot.utils.logger import get_logger

log = get_logger(__name__)

_VALID_COL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_columns(keys):
    """Ensure dict keys are safe SQL column names."""
    for k in keys:
        if not _VALID_COL_RE.match(k):
            raise ValueError(f"Invalid column name: {k!r}")


_DB_PATH = config.DB_PATH
_local = threading.local()


# ═══════════════════════════════════════════════════════════════════════════════
#  CONNECTION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _get_connection() -> sqlite3.Connection:
    """Return a thread‑local connection (created once per thread)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


@contextmanager
def get_cursor():
    """Yield a cursor that auto‑commits on success, rolls back on error."""
    conn = _get_connection()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ═══════════════════════════════════════════════════════════════════════════════
#  SCHEMA CREATION
# ═══════════════════════════════════════════════════════════════════════════════

_SCHEMA_SQL = """
-- OHLCV candle data (multi-timeframe)
CREATE TABLE IF NOT EXISTS candles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    token       TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL,          -- '1m','5m','15m','1D'
    timestamp   TEXT    NOT NULL,          -- ISO-8601 IST
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(symbol, token, timeframe, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_candles_lookup
    ON candles(symbol, timeframe, timestamp);

-- Order log
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT,                  -- broker order id
    symbol          TEXT    NOT NULL,
    token           TEXT    NOT NULL,
    exchange        TEXT    NOT NULL DEFAULT 'NFO',
    side            TEXT    NOT NULL,       -- 'BUY' / 'SELL'
    order_type      TEXT    NOT NULL DEFAULT 'MARKET',
    quantity        INTEGER NOT NULL,
    price           REAL,
    status          TEXT    NOT NULL DEFAULT 'PENDING',  -- PENDING/COMPLETE/REJECTED
    placed_at       TEXT    NOT NULL,
    completed_at    TEXT,
    remarks         TEXT
);

-- Trade lifecycle
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        TEXT    NOT NULL UNIQUE,
    symbol          TEXT    NOT NULL,
    token           TEXT    NOT NULL,
    option_type     TEXT    NOT NULL,       -- 'CE' / 'PE'
    strike          REAL    NOT NULL,
    direction       TEXT    NOT NULL,       -- 'LONG'
    entry_price     REAL,
    exit_price      REAL,
    quantity        INTEGER NOT NULL,
    entry_order_id  TEXT,
    exit_order_id   TEXT,
    entry_time      TEXT,
    exit_time       TEXT,
    exit_reason     TEXT,                   -- 'SL','TRAIL','PARTIAL','TARGET','TIME','SUPERTREND'
    pnl             REAL,
    status          TEXT    NOT NULL DEFAULT 'OPEN',  -- OPEN / CLOSED
    stop_loss       REAL,                              -- auto-exit premium
    target          REAL,                              -- auto-exit premium
    expiry          TEXT,                              -- option expiry YYYY-MM-DD
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);

-- Daily PnL summary
CREATE TABLE IF NOT EXISTS daily_pnl (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL UNIQUE,    -- YYYY-MM-DD
    total_pnl   REAL    NOT NULL DEFAULT 0,
    trades      INTEGER NOT NULL DEFAULT 0,
    wins        INTEGER NOT NULL DEFAULT 0,
    losses      INTEGER NOT NULL DEFAULT 0
);

-- Signal log
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    direction   TEXT    NOT NULL,           -- 'BULLISH' / 'BEARISH'
    strength    REAL,
    filters     TEXT,                       -- JSON of filter results
    action      TEXT,                       -- 'ENTER' / 'SKIP'
    reason      TEXT
);

-- Backtest results
CREATE TABLE IF NOT EXISTS backtest_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL UNIQUE,
    start_date      TEXT    NOT NULL,
    end_date        TEXT    NOT NULL,
    total_trades    INTEGER,
    win_rate        REAL,
    profit_factor   REAL,
    max_drawdown    REAL,
    sharpe_ratio    REAL,
    total_pnl       REAL,
    params_json     TEXT,                   -- JSON of strategy params used
    created_at      TEXT    NOT NULL
);

-- Paper trading portfolio
CREATE TABLE IF NOT EXISTS portfolio (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    initial_capital REAL    NOT NULL,
    current_balance REAL    NOT NULL,
    total_pnl       REAL    NOT NULL DEFAULT 0,
    total_trades    INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
"""


def init_db() -> None:
    """Create all tables and indices if they don't exist."""
    with get_cursor() as cur:
        cur.executescript(_SCHEMA_SQL)
    _migrate()
    log.info("Database initialized at %s", _DB_PATH)


def _migrate() -> None:
    """Add columns that may be missing in older databases."""
    migrations = [
        ("trades", "stop_loss", "REAL"),
        ("trades", "target",    "REAL"),
        ("trades", "expiry",    "TEXT"),
        ("trades", "source",    "TEXT DEFAULT 'MANUAL'"),
    ]
    with get_cursor() as cur:
        for table, col, col_type in migrations:
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                log.info("Migration: added %s.%s", table, col)
            except sqlite3.OperationalError:
                pass  # column already exists


# ═══════════════════════════════════════════════════════════════════════════════
#  CANDLE STORAGE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def upsert_candles(rows: list[dict]) -> int:
    """Insert or ignore candle rows. Return count of new rows inserted."""
    if not rows:
        return 0
    sql = """
        INSERT OR IGNORE INTO candles
            (symbol, token, timeframe, timestamp, open, high, low, close, volume)
        VALUES
            (:symbol, :token, :timeframe, :timestamp,
             :open, :high, :low, :close, :volume)
    """
    with get_cursor() as cur:
        before = cur.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
        cur.executemany(sql, rows)
        after = cur.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
        return after - before


def fetch_candles_by_date(
    symbol: str, timeframe: str, date_str: str
) -> list[sqlite3.Row]:
    """Return all candles for symbol/timeframe on a specific date (YYYY-MM-DD), oldest first."""
    sql = """
        SELECT * FROM candles
        WHERE symbol = ? AND timeframe = ? AND DATE(timestamp) = ?
        ORDER BY timestamp ASC
    """
    with get_cursor() as cur:
        cur.execute(sql, (symbol, timeframe, date_str))
        return cur.fetchall()


def fetch_candles(
    symbol: str, timeframe: str, limit: int = 500
) -> list[sqlite3.Row]:
    """Return the most recent `limit` candles for symbol/timeframe."""
    sql = """
        SELECT * FROM candles
        WHERE symbol = ? AND timeframe = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """
    with get_cursor() as cur:
        cur.execute(sql, (symbol, timeframe, limit))
        rows = cur.fetchall()
    return list(reversed(rows))  # oldest first


# ═══════════════════════════════════════════════════════════════════════════════
#  ORDER HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def insert_order(order: dict) -> int:
    """Insert an order record. Return the lastrowid."""
    _validate_columns(order.keys())
    cols = ", ".join(order.keys())
    placeholders = ", ".join(f":{k}" for k in order.keys())
    sql = f"INSERT INTO orders ({cols}) VALUES ({placeholders})"
    with get_cursor() as cur:
        cur.execute(sql, order)
        rowid = cur.lastrowid
    # Sync to Redis for Vercel persistence
    try:
        from trading_bot.redis_sync import sync_order_to_redis
        sync_order_to_redis(order)
    except Exception:
        pass
    return rowid


def update_order_status(order_id: str, status: str, completed_at: str = "") -> None:
    """Update broker order status."""
    sql = "UPDATE orders SET status = ?, completed_at = ? WHERE order_id = ?"
    with get_cursor() as cur:
        cur.execute(sql, (status, completed_at, order_id))


# ═══════════════════════════════════════════════════════════════════════════════
#  TRADE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def insert_trade(trade: dict) -> int:
    """Insert a new trade record."""
    _validate_columns(trade.keys())
    cols = ", ".join(trade.keys())
    placeholders = ", ".join(f":{k}" for k in trade.keys())
    sql = f"INSERT INTO trades ({cols}) VALUES ({placeholders})"
    with get_cursor() as cur:
        cur.execute(sql, trade)
        rowid = cur.lastrowid
    # Sync to Redis for Vercel persistence
    try:
        from trading_bot.redis_sync import sync_trade_to_redis
        sync_trade_to_redis(trade)
    except Exception:
        pass
    return rowid


def close_trade(
    trade_id: str,
    exit_price: float,
    exit_time: str,
    exit_reason: str,
    pnl: float,
    exit_order_id: str = "",
) -> None:
    """Mark a trade as closed with exit details."""
    sql = """
        UPDATE trades
        SET exit_price = ?, exit_time = ?, exit_reason = ?,
            pnl = ?, status = 'CLOSED', exit_order_id = ?
        WHERE trade_id = ?
    """
    with get_cursor() as cur:
        cur.execute(sql, (exit_price, exit_time, exit_reason, pnl, exit_order_id, trade_id))
    # Sync closed trade to Redis
    try:
        from trading_bot.redis_sync import sync_trade_to_redis
        # Re-read the full trade row so Redis has all fields
        with get_cursor() as cur:
            cur.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,))
            row = cur.fetchone()
            if row:
                sync_trade_to_redis(dict(row))
    except Exception:
        pass


def get_open_trades() -> list[dict]:
    """Return all currently open trades as dicts."""
    sql = "SELECT * FROM trades WHERE status = 'OPEN'"
    with get_cursor() as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def get_today_pnl(date_str: str) -> float:
    """Sum of PnL for trades closed today."""
    sql = "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status = 'CLOSED' AND DATE(exit_time) = ?"
    with get_cursor() as cur:
        cur.execute(sql, (date_str,))
        row = cur.fetchone()
        return float(row[0]) if row else 0.0


# ═══════════════════════════════════════════════════════════════════════════════
#  SIGNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def insert_signal(signal: dict) -> int:
    """Log a signal."""
    _validate_columns(signal.keys())
    cols = ", ".join(signal.keys())
    placeholders = ", ".join(f":{k}" for k in signal.keys())
    sql = f"INSERT INTO signals ({cols}) VALUES ({placeholders})"
    with get_cursor() as cur:
        cur.execute(sql, signal)
        return cur.lastrowid


# ═══════════════════════════════════════════════════════════════════════════════
#  DAILY PNL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def upsert_daily_pnl(date_str: str, total_pnl: float, trades: int, wins: int, losses: int) -> None:
    """Insert or update daily PnL summary."""
    sql = """
        INSERT INTO daily_pnl (date, total_pnl, trades, wins, losses)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            total_pnl = excluded.total_pnl,
            trades    = excluded.trades,
            wins      = excluded.wins,
            losses    = excluded.losses
    """
    with get_cursor() as cur:
        cur.execute(sql, (date_str, total_pnl, trades, wins, losses))


# ═══════════════════════════════════════════════════════════════════════════════
#  BACKTEST HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def insert_backtest_result(result: dict) -> int:
    """Store a backtest run summary."""
    cols = ", ".join(result.keys())
    placeholders = ", ".join(f":{k}" for k in result.keys())
    sql = f"INSERT INTO backtest_results ({cols}) VALUES ({placeholders})"
    with get_cursor() as cur:
        cur.execute(sql, result)
        return cur.lastrowid


def get_latest_backtest() -> sqlite3.Row | None:
    """Return the most recent backtest result."""
    sql = "SELECT * FROM backtest_results ORDER BY created_at DESC LIMIT 1"
    with get_cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()


# ═══════════════════════════════════════════════════════════════════════════════
#  PORTFOLIO HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_portfolio() -> dict | None:
    """Return the portfolio row as a dict, or None if not initialised."""
    sql = "SELECT * FROM portfolio ORDER BY id LIMIT 1"
    with get_cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
        if row:
            return dict(row)
        return None


def init_portfolio(initial_capital: float) -> dict:
    """Create the portfolio row if it doesn't exist. Return portfolio dict."""
    existing = get_portfolio()
    if existing:
        return existing
    from trading_bot.utils.time_utils import now_ist
    now_str = now_ist().isoformat()
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO portfolio (initial_capital, current_balance, total_pnl, "
            "total_trades, wins, losses, created_at, updated_at) "
            "VALUES (?, ?, 0, 0, 0, 0, ?, ?)",
            (initial_capital, initial_capital, now_str, now_str),
        )
    portfolio = get_portfolio()
    try:
        from trading_bot.redis_sync import sync_portfolio_to_redis
        if portfolio:
            sync_portfolio_to_redis(portfolio)
    except Exception:
        pass
    return portfolio


def update_portfolio_after_trade(pnl: float, is_win: bool) -> dict:
    """Update portfolio balance after a trade closes."""
    from trading_bot.utils.time_utils import now_ist
    now_str = now_ist().isoformat()
    win_inc = 1 if is_win else 0
    loss_inc = 0 if is_win else 1
    with get_cursor() as cur:
        cur.execute(
            "UPDATE portfolio SET "
            "current_balance = current_balance + ?, "
            "total_pnl = total_pnl + ?, "
            "total_trades = total_trades + 1, "
            "wins = wins + ?, "
            "losses = losses + ?, "
            "updated_at = ? "
            "WHERE id = (SELECT id FROM portfolio ORDER BY id LIMIT 1)",
            (pnl, pnl, win_inc, loss_inc, now_str),
        )
    portfolio = get_portfolio()
    # Sync portfolio to Redis for Vercel persistence
    try:
        from trading_bot.redis_sync import sync_portfolio_to_redis
        if portfolio:
            sync_portfolio_to_redis(portfolio)
    except Exception:
        pass
    return portfolio


def reset_portfolio(initial_capital: float) -> dict:
    """Delete portfolio and recreate with fresh capital."""
    from trading_bot.utils.time_utils import now_ist
    now_str = now_ist().isoformat()
    with get_cursor() as cur:
        cur.execute("DELETE FROM portfolio")
        cur.execute(
            "INSERT INTO portfolio (initial_capital, current_balance, total_pnl, "
            "total_trades, wins, losses, created_at, updated_at) "
            "VALUES (?, ?, 0, 0, 0, 0, ?, ?)",
            (initial_capital, initial_capital, now_str, now_str),
        )
    portfolio = get_portfolio()
    try:
        from trading_bot.redis_sync import sync_portfolio_to_redis
        if portfolio:
            sync_portfolio_to_redis(portfolio)
    except Exception:
        pass
    return portfolio

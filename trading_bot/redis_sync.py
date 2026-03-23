"""
redis_sync.py — Persist trades/orders/portfolio to Upstash Redis.

On Vercel, SQLite lives in ephemeral /tmp and is wiped on cold start.
This module mirrors every trade write to Redis so data survives restarts.

On cold start, restore_from_redis() rebuilds the SQLite DB from Redis.
"""

import json
import os

from trading_bot.cache import _get_client
from trading_bot.utils.logger import get_logger

log = get_logger(__name__)

_TRADES_KEY = "trades:all"        # Redis Hash: trade_id -> JSON trade dict
_ORDERS_KEY = "orders:all"        # Redis Hash: order_id -> JSON order dict
_PORTFOLIO_KEY = "portfolio:data" # Redis String: JSON portfolio dict


def _redis():
    """Get the Upstash Redis client (or None)."""
    return _get_client()


# ═══════════════════════════════════════════════════════════════════════════════
#  TRADE SYNC
# ═══════════════════════════════════════════════════════════════════════════════

def sync_trade_to_redis(trade: dict) -> bool:
    """Save/update a trade in Redis. Called after insert_trade or close_trade."""
    r = _redis()
    if not r:
        return False
    try:
        trade_id = trade.get("trade_id", "")
        if not trade_id:
            return False
        r.hset(_TRADES_KEY, trade_id, json.dumps(trade, default=str))
        return True
    except Exception as e:
        log.warning("Redis sync_trade error: %s", e)
        return False


def get_all_trades_from_redis() -> list[dict]:
    """Return all trades stored in Redis."""
    r = _redis()
    if not r:
        return []
    try:
        raw = r.hgetall(_TRADES_KEY)
        if not raw:
            return []
        trades = []
        for _tid, val in raw.items():
            if isinstance(val, (bytes, bytearray)):
                val = val.decode()
            trades.append(json.loads(val))
        return trades
    except Exception as e:
        log.warning("Redis get_all_trades error: %s", e)
        return []


def delete_trade_from_redis(trade_id: str) -> bool:
    """Remove a trade from Redis."""
    r = _redis()
    if not r:
        return False
    try:
        r.hdel(_TRADES_KEY, trade_id)
        return True
    except Exception as e:
        log.warning("Redis delete_trade error: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
#  ORDER SYNC
# ═══════════════════════════════════════════════════════════════════════════════

def sync_order_to_redis(order: dict) -> bool:
    """Save an order in Redis."""
    r = _redis()
    if not r:
        return False
    try:
        order_id = order.get("order_id", "")
        if not order_id:
            return False
        r.hset(_ORDERS_KEY, order_id, json.dumps(order, default=str))
        return True
    except Exception as e:
        log.warning("Redis sync_order error: %s", e)
        return False


def get_all_orders_from_redis() -> list[dict]:
    """Return all orders stored in Redis."""
    r = _redis()
    if not r:
        return []
    try:
        raw = r.hgetall(_ORDERS_KEY)
        if not raw:
            return []
        orders = []
        for _oid, val in raw.items():
            if isinstance(val, (bytes, bytearray)):
                val = val.decode()
            orders.append(json.loads(val))
        return orders
    except Exception as e:
        log.warning("Redis get_all_orders error: %s", e)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
#  PORTFOLIO SYNC
# ═══════════════════════════════════════════════════════════════════════════════

def sync_portfolio_to_redis(portfolio: dict) -> bool:
    """Save portfolio state to Redis."""
    r = _redis()
    if not r:
        return False
    try:
        r.set(_PORTFOLIO_KEY, json.dumps(portfolio, default=str))
        return True
    except Exception as e:
        log.warning("Redis sync_portfolio error: %s", e)
        return False


def get_portfolio_from_redis() -> dict | None:
    """Return portfolio from Redis."""
    r = _redis()
    if not r:
        return None
    try:
        raw = r.get(_PORTFOLIO_KEY)
        if raw is None:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        return json.loads(raw)
    except Exception as e:
        log.warning("Redis get_portfolio error: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  COLD START RESTORE
# ═══════════════════════════════════════════════════════════════════════════════

def restore_from_redis() -> int:
    """
    On Vercel cold start, rebuild /tmp SQLite DB from Redis data.
    Merges Redis trades into the existing DB (from deployed snapshot).
    Returns count of trades restored.
    """
    from trading_bot.data.store import get_cursor, init_db

    init_db()

    restored = 0

    # Restore trades
    trades = get_all_trades_from_redis()
    if trades:
        for t in trades:
            try:
                with get_cursor() as cur:
                    # Use INSERT OR REPLACE to merge with deployed snapshot
                    cur.execute("""
                        INSERT OR REPLACE INTO trades
                            (trade_id, symbol, token, option_type, strike,
                             direction, entry_price, exit_price, quantity,
                             entry_order_id, exit_order_id, entry_time, exit_time,
                             exit_reason, pnl, status, stop_loss, target,
                             expiry, source, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        t.get("trade_id", ""), t.get("symbol", ""), t.get("token", ""),
                        t.get("option_type", ""), t.get("strike", 0),
                        t.get("direction", "LONG"), t.get("entry_price", 0),
                        t.get("exit_price"), t.get("quantity", 0),
                        t.get("entry_order_id", ""), t.get("exit_order_id", ""),
                        t.get("entry_time", ""), t.get("exit_time"),
                        t.get("exit_reason"), t.get("pnl"),
                        t.get("status", "OPEN"), t.get("stop_loss"),
                        t.get("target"), t.get("expiry"),
                        t.get("source", "AUTO"), t.get("created_at", ""),
                    ))
                    restored += 1
            except Exception as e:
                log.warning("Redis restore trade %s: %s", t.get("trade_id"), e)

    # Restore orders
    orders = get_all_orders_from_redis()
    if orders:
        for o in orders:
            try:
                with get_cursor() as cur:
                    cur.execute("""
                        INSERT OR IGNORE INTO orders
                            (order_id, symbol, token, exchange, side,
                             order_type, quantity, price, status,
                             placed_at, completed_at, remarks)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        o.get("order_id", ""), o.get("symbol", ""),
                        o.get("token", ""), o.get("exchange", "NFO"),
                        o.get("side", ""), o.get("order_type", ""),
                        o.get("quantity", 0), o.get("price", 0),
                        o.get("status", ""), o.get("placed_at", ""),
                        o.get("completed_at", ""), o.get("remarks", ""),
                    ))
            except Exception:
                pass

    # Restore portfolio
    portfolio = get_portfolio_from_redis()
    if portfolio:
        try:
            with get_cursor() as cur:
                cur.execute("DELETE FROM portfolio")
                cur.execute("""
                    INSERT INTO portfolio
                        (initial_capital, current_balance, total_pnl,
                         total_trades, wins, losses, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    portfolio.get("initial_capital", 30000),
                    portfolio.get("current_balance", 30000),
                    portfolio.get("total_pnl", 0),
                    portfolio.get("total_trades", 0),
                    portfolio.get("wins", 0),
                    portfolio.get("losses", 0),
                    portfolio.get("created_at", ""),
                    portfolio.get("updated_at", ""),
                ))
        except Exception as e:
            log.warning("Redis restore portfolio: %s", e)

    log.info("Redis restore: %d trades, %d orders restored", restored, len(orders))
    return restored

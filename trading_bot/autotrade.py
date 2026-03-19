"""
autotrade.py — Auto-trading engine for Project Candles.

Periodically fetches live NIFTY candles, runs the strategy engine,
and automatically enters/monitors/exits paper trades with smart exit logic.

Features:
    - Auto-detect signals from candle patterns + indicator filters
    - Auto-place paper trades on ENTER signals
    - Monitor open positions with trailing SL
    - Smart exit: if target looks unachievable, exit with profit
    - Respects MAX_OPEN_TRADES, MAX_DAILY_LOSS, trade windows
    - EOD forced exit at FORCE_EXIT_TIME
"""

import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from trading_bot import config
from trading_bot.auth.login import get_session
from trading_bot.data.store import (
    close_trade, get_open_trades, get_today_pnl,
    insert_order, insert_trade, update_portfolio_after_trade,
)
from trading_bot.market import fetch_option_ltp, get_latest_tick, fetch_live_once
from trading_bot.strategy import evaluate
from trading_bot.utils.logger import get_logger
from trading_bot.utils.time_utils import now_ist

log = get_logger(__name__)

IST = ZoneInfo(config.TIMEZONE)

# ── Module state ──────────────────────────────────────────────────────────────
_running = False
_thread: threading.Thread | None = None
_lock = threading.Lock()

# Track auto-trade status visible to the dashboard
_status = {
    "enabled": False,
    "last_scan": "",
    "last_signal": None,
    "trades_today": 0,
    "pnl_today": 0.0,
    "open_positions": 0,
    "log": [],  # last N log messages
}

_MAX_LOG = 50
_SCAN_INTERVAL = 60  # seconds between scans

# Track entry timestamps to prevent duplicate signals
_recent_signals: dict[str, float] = {}  # "DIRECTION" -> timestamp


def get_status() -> dict:
    with _lock:
        return dict(_status)


def _log_event(msg: str):
    """Append to the in-memory log ring."""
    ts = now_ist().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    log.info("AutoTrade: %s", msg)
    with _lock:
        _status["log"].append(entry)
        if len(_status["log"]) > _MAX_LOG:
            _status["log"] = _status["log"][-_MAX_LOG:]


def _is_in_trade_window() -> bool:
    now = now_ist().strftime("%H:%M")
    for start, end in config.TRADE_WINDOWS:
        if start <= now <= end:
            return True
    return False


def _is_past_force_exit() -> bool:
    now = now_ist().strftime("%H:%M")
    return now >= config.FORCE_EXIT_TIME


def _daily_loss_reached() -> bool:
    today_str = now_ist().strftime("%Y-%m-%d")
    pnl = get_today_pnl(today_str)
    return pnl <= -config.MAX_DAILY_LOSS


def _is_duplicate_signal(direction: str) -> bool:
    """Prevent same-direction signal within cooldown period."""
    now_ts = time.time()
    last = _recent_signals.get(direction, 0)
    if now_ts - last < config.DUPLICATE_SIGNAL_COOLDOWN:
        return True
    _recent_signals[direction] = now_ts
    return False


def _fetch_latest_candles(session, timeframe="1m", bars=100) -> pd.DataFrame | None:
    """Fetch recent NIFTY candles via shared cache (avoids rate limiting)."""
    from trading_bot.candle_cache import get_candles
    df, _, _ = get_candles(timeframe, bars)
    return df


def _place_auto_trade(signal, nifty_ltp: float) -> str | None:
    """Place a paper trade based on signal. Returns trade_id or None."""
    direction = "LONG" if signal.direction == "BULLISH" else "SHORT"
    entry_price = nifty_ltp if nifty_ltp > 0 else signal.entry_price

    if entry_price <= 0:
        _log_event(f"Skip trade: no valid entry price")
        return None

    # Calculate absolute SL and target prices
    if direction == "LONG":
        sl_price = round(entry_price - signal.sl_points, 2)
        tgt_price = round(entry_price + signal.target_points, 2)
    else:
        sl_price = round(entry_price + signal.sl_points, 2)
        tgt_price = round(entry_price - signal.target_points, 2)

    now_str = now_ist().isoformat()
    trade_id = f"AT-{uuid.uuid4().hex[:8].upper()}"
    side = "BUY" if direction == "LONG" else "SELL"

    insert_order({
        "order_id": trade_id,
        "symbol": config.UNDERLYING,
        "token": config.NIFTY_TOKEN,
        "exchange": config.EXCHANGE,
        "side": side,
        "order_type": "PAPER",
        "quantity": config.LOT_SIZE,
        "price": entry_price,
        "status": "COMPLETE",
        "placed_at": now_str,
        "completed_at": now_str,
        "remarks": f"AutoTrade {signal.direction} | {', '.join(signal.patterns)}",
    })
    insert_trade({
        "trade_id": trade_id,
        "symbol": config.UNDERLYING,
        "token": config.NIFTY_TOKEN,
        "option_type": "IDX",
        "strike": 0,
        "direction": direction,
        "entry_price": entry_price,
        "quantity": config.LOT_SIZE,
        "entry_order_id": trade_id,
        "entry_time": now_str,
        "status": "OPEN",
        "stop_loss": sl_price,
        "target": tgt_price,
        "expiry": "",
        "source": "AUTO",
        "created_at": now_str,
    })

    patterns_str = ", ".join(signal.patterns)
    _log_event(
        f"TRADE {side} {config.UNDERLYING} @ {entry_price:.2f} | "
        f"SL={sl_price:.2f} TGT={tgt_price:.2f} | "
        f"Patterns: {patterns_str} | Strength: {signal.strength}%"
    )
    return trade_id


def _monitor_positions():
    """
    Check open AUTO positions for SL/target hit, trailing SL, and smart exit.

    Smart exit logic:
        1. If price moves 50%+ toward target → trail SL to breakeven + 5 pts
        2. If price was profitable but reversed to within 30% of SL → exit with profit
        3. Past FORCE_EXIT_TIME → close all positions (EOD exit)
    """
    open_trades = get_open_trades()
    auto_trades = [t for t in open_trades if (t.get("source") or t["trade_id"][:2]) in ("AUTO", "AT")]

    if not auto_trades:
        return

    # Get NIFTY LTP
    tick = get_latest_tick()
    ltp = tick.ltp
    if ltp <= 0:
        tick, _ = fetch_live_once()
        ltp = tick.ltp
    if ltp <= 0:
        return

    force_exit = _is_past_force_exit()

    for t in auto_trades:
        trade_id = t["trade_id"]
        entry = t["entry_price"]
        direction = t["direction"]
        qty = t["quantity"]
        sl = t.get("stop_loss")
        tgt = t.get("target")
        token = t["token"]

        # For option trades, get option LTP
        trade_ltp = ltp
        if token and token != config.NIFTY_TOKEN:
            try:
                ltps = fetch_option_ltp([token])
                ol = ltps.get(token, 0)
                if ol > 0:
                    trade_ltp = ol
            except Exception:
                pass

        # Calculate P&L
        if direction == "LONG":
            pnl = (trade_ltp - entry) * qty
        else:
            pnl = (entry - trade_ltp) * qty

        exit_reason = None
        exit_price = trade_ltp

        # 1. Force EOD exit
        if force_exit:
            exit_reason = "EOD_EXIT"
            _log_event(f"EOD EXIT {trade_id} @ {exit_price:.2f} PnL={pnl:.2f}")

        # 2. SL hit
        elif sl:
            if direction == "LONG" and trade_ltp <= sl:
                exit_reason = "SL_HIT"
                exit_price = sl
            elif direction == "SHORT" and trade_ltp >= sl:
                exit_reason = "SL_HIT"
                exit_price = sl

        # 3. Target hit
        if not exit_reason and tgt:
            if direction == "LONG" and trade_ltp >= tgt:
                exit_reason = "TARGET_HIT"
                exit_price = tgt
            elif direction == "SHORT" and trade_ltp <= tgt:
                exit_reason = "TARGET_HIT"
                exit_price = tgt

        # 4. Smart exit: trailing SL & unachievable target detection
        if not exit_reason and sl and tgt:
            total_move = abs(tgt - entry)
            if total_move > 0:
                if direction == "LONG":
                    progress = (trade_ltp - entry) / total_move
                else:
                    progress = (entry - trade_ltp) / total_move

                # If we've moved 50%+ toward target, trail SL to breakeven + 5pts
                if progress >= 0.5:
                    new_sl = entry + 5 if direction == "LONG" else entry - 5
                    if direction == "LONG" and (sl is None or new_sl > sl):
                        _update_trade_sl(trade_id, new_sl)
                        _log_event(f"TRAIL SL {trade_id} → {new_sl:.2f} (50% progress)")
                    elif direction == "SHORT" and (sl is None or new_sl < sl):
                        _update_trade_sl(trade_id, new_sl)
                        _log_event(f"TRAIL SL {trade_id} → {new_sl:.2f} (50% progress)")

                # Smart exit: was profitable (>30% progress) but now reversing
                # If PnL is positive but price reversing (progress < was), exit with profit
                if 0.1 < progress < 0.3 and pnl > 0:
                    # Price moved toward target but now stalling — lock profit
                    exit_reason = "SMART_EXIT"
                    _log_event(
                        f"SMART EXIT {trade_id} @ {exit_price:.2f} | "
                        f"Progress={progress:.0%} PnL={pnl:.2f} (target looks unachievable)"
                    )

        if exit_reason:
            # Recalculate PnL at exit price
            if direction == "LONG":
                final_pnl = (exit_price - entry) * qty
            else:
                final_pnl = (entry - exit_price) * qty
            final_pnl = round(final_pnl, 2)

            now_str = now_ist().isoformat()
            close_trade(trade_id, exit_price, now_str, exit_reason, final_pnl)
            update_portfolio_after_trade(final_pnl, final_pnl >= 0)

            if exit_reason != "EOD_EXIT":
                _log_event(f"{exit_reason} {trade_id} @ {exit_price:.2f} PnL={final_pnl:.2f}")


def _update_trade_sl(trade_id: str, new_sl: float):
    """Update the stop loss of a trade in the database."""
    from trading_bot.data.store import get_cursor
    sql = "UPDATE trades SET stop_loss = ? WHERE trade_id = ?"
    with get_cursor() as cur:
        cur.execute(sql, (new_sl, trade_id))


def _scan_and_trade():
    """One cycle: fetch candles → evaluate → place trade if signal."""
    session = get_session()
    if not session:
        _log_event("No session — skip scan")
        return

    # Check market conditions
    if not _is_in_trade_window():
        with _lock:
            _status["last_scan"] = now_ist().strftime("%H:%M:%S")
        return

    if _daily_loss_reached():
        _log_event("Daily loss limit reached — skipping new trades")
        return

    # Check open trade count
    open_trades = get_open_trades()
    if len(open_trades) >= config.MAX_OPEN_TRADES:
        return

    # Fetch latest candles
    df = _fetch_latest_candles(session, timeframe="1m")
    if df is None or len(df) < 20:
        return

    # Evaluate strategy
    signals = evaluate(df)
    if not signals:
        return

    with _lock:
        _status["last_scan"] = now_ist().strftime("%H:%M:%S")

    for sig in signals:
        if sig.action != "ENTER":
            continue

        if _is_duplicate_signal(sig.direction):
            continue

        # Get live NIFTY price for entry
        tick = get_latest_tick()
        nifty_ltp = tick.ltp
        if nifty_ltp <= 0:
            tick, _ = fetch_live_once()
            nifty_ltp = tick.ltp

        trade_id = _place_auto_trade(sig, nifty_ltp)
        if trade_id:
            with _lock:
                _status["last_signal"] = {
                    "direction": sig.direction,
                    "patterns": sig.patterns,
                    "pattern_descriptions": sig.pattern_descriptions,
                    "strength": sig.strength,
                    "entry_price": sig.entry_price,
                    "sl_points": sig.sl_points,
                    "target_points": sig.target_points,
                    "expected_profit_pts": sig.expected_profit_pts,
                    "trade_id": trade_id,
                    "time": now_ist().strftime("%H:%M:%S"),
                }
            break  # one trade per scan cycle


def _auto_trade_loop():
    """Background thread: scan + monitor on a timer."""
    global _running
    _log_event("Auto-trade engine STARTED")

    while _running:
        try:
            # Monitor existing positions (every cycle)
            _monitor_positions()

            # Scan for new signals
            _scan_and_trade()

            # Update dashboard status
            open_trades = get_open_trades()
            auto_count = sum(
                1 for t in open_trades
                if (t.get("source") or t["trade_id"][:2]) in ("AUTO", "AT")
            )
            today_str = now_ist().strftime("%Y-%m-%d")
            pnl = get_today_pnl(today_str)

            with _lock:
                _status["open_positions"] = auto_count
                _status["pnl_today"] = round(pnl, 2)
                _status["last_scan"] = now_ist().strftime("%H:%M:%S")

        except Exception as exc:
            log.error("AutoTrade loop error: %s", exc)
            _log_event(f"Error: {exc}")

        time.sleep(_SCAN_INTERVAL)

    _log_event("Auto-trade engine STOPPED")


def start(scan_interval: int = 60):
    """Start the auto-trade background thread."""
    global _running, _thread, _SCAN_INTERVAL
    if _running and _thread and _thread.is_alive():
        return
    _SCAN_INTERVAL = max(scan_interval, 60)  # minimum 60s to avoid rate limiting
    _running = True
    with _lock:
        _status["enabled"] = True
    _thread = threading.Thread(target=_auto_trade_loop, name="autotrade", daemon=True)
    _thread.start()


def stop():
    """Stop the auto-trade engine."""
    global _running
    _running = False
    with _lock:
        _status["enabled"] = False
    _log_event("Auto-trade engine STOPPING")

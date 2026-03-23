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
from trading_bot.strategy import evaluate, is_sideways_market, get_htf_bias
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
        # Sync enabled flag with actual thread state
        thread_alive = _thread is not None and _thread.is_alive()
        if _status["enabled"] and not thread_alive:
            _status["enabled"] = False
        return dict(_status)


def _log_event(msg: str, *, console: bool = True):
    """Append to the in-memory log ring, print to console, and push to Redis."""
    ts = now_ist().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    log.info("AutoTrade: %s", msg)
    if console:
        print(f"[AutoTrade {ts}] {msg}", flush=True)
    with _lock:
        _status["log"].append(entry)
        if len(_status["log"]) > _MAX_LOG:
            _status["log"] = _status["log"][-_MAX_LOG:]
    # Persist to Redis for Vercel dashboard visibility
    try:
        from trading_bot.redis_sync import push_scan_log
        push_scan_log(entry)
    except Exception:
        pass


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
    """Prevent same-direction signal within cooldown period.

    On Vercel, in-memory _recent_signals resets on every cold start, so we
    also check/set a Redis key with a TTL equal to the cooldown period.
    """
    now_ts = time.time()
    last = _recent_signals.get(direction, 0)

    # Redis check — survives Vercel cold starts
    try:
        from trading_bot.cache import _get_client
        r = _get_client()
        if r:
            redis_val = r.get(f"autotrade:signal:{direction}")
            if redis_val:
                if isinstance(redis_val, (bytes, bytearray)):
                    redis_val = redis_val.decode()
                last = max(last, float(redis_val))
    except Exception:
        pass

    if now_ts - last < config.DUPLICATE_SIGNAL_COOLDOWN:
        return True

    # Record in both memory and Redis
    _recent_signals[direction] = now_ts
    try:
        from trading_bot.cache import _get_client
        r = _get_client()
        if r:
            r.set(f"autotrade:signal:{direction}", str(now_ts),
                  ex=config.DUPLICATE_SIGNAL_COOLDOWN)
    except Exception:
        pass
    return False


def _is_sl_blocked(direction: str) -> bool:
    """Return True if this direction is blocked after a recent SL hit."""
    try:
        from trading_bot.cache import _get_client
        r = _get_client()
        if r:
            return r.get(f"autotrade:sl_block:{direction}") is not None
    except Exception:
        pass
    return False


def _set_sl_block(direction: str):
    """Block entries in `direction` for SL_BLOCK_DURATION seconds after SL hit."""
    try:
        from trading_bot.cache import _get_client
        r = _get_client()
        if r:
            r.set(f"autotrade:sl_block:{direction}", "1",
                  ex=config.SL_BLOCK_DURATION)
            mins = config.SL_BLOCK_DURATION // 60
            _log_event(f"SL BLOCK: {direction} blocked for {mins} min after SL hit")
    except Exception:
        pass


def _fetch_latest_candles(session, timeframe="1m", bars=100) -> pd.DataFrame | None:
    """Fetch recent NIFTY candles via shared cache (avoids rate limiting)."""
    from trading_bot.candle_cache import get_candles
    df, _, _ = get_candles(timeframe, bars)
    return df


def _count_today_auto_trades() -> int:
    """Count auto-trades already placed today (to enforce MAX_DAILY_TRADES)."""
    today_str = now_ist().strftime("%Y-%m-%d")
    try:
        from trading_bot.data.store import get_cursor
        with get_cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM trades "
                "WHERE source='AUTO' AND DATE(entry_time)=? AND status!='CANCELLED'",
                (today_str,),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def _count_trades_last_n_minutes(minutes: int = 15) -> int:
    """Count auto-trades placed in the last N minutes (overtrading guard)."""
    try:
        from trading_bot.data.store import get_cursor
        with get_cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM trades "
                "WHERE source='AUTO' AND entry_time >= datetime('now', ?)",
                (f"-{minutes} minutes",),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def _place_auto_trade(signal, nifty_ltp: float) -> str | None:
    """
    Place a paper trade: BUY CE for BULLISH, BUY PE for BEARISH.
    Always LONG (buy options) — no shorting.

    Resolves ATM option from instrument master, fetches option LTP,
    and stores the actual option details (symbol, strike, expiry).
    """
    # Determine option type based on signal direction
    if signal.direction == "BULLISH":
        option_type = "CE"
    else:
        option_type = "PE"

    # All trades are LONG (buy) — no shorting
    direction = "LONG"

    if nifty_ltp <= 0:
        nifty_ltp = signal.entry_price
    if nifty_ltp <= 0:
        _log_event("Skip trade: no valid NIFTY price")
        return None

    # Find ATM option contract
    from trading_bot.options import find_atm_option, format_option_name, get_option_ltp
    opt = find_atm_option(nifty_ltp, option_type)
    if not opt:
        _log_event(f"Skip trade: could not find ATM {option_type} option")
        return None

    opt_name = format_option_name(opt["strike"], opt["option_type"], opt["expiry"])

    # Fetch option premium (LTP) for entry price
    session = get_session()
    entry_price = 0.0
    if session:
        try:
            ltps = get_option_ltp(session, [opt["token"]])
            entry_price = ltps.get(opt["token"], 0.0)
        except Exception as e:
            _log_event(f"Option LTP fetch failed: {e}")

    if entry_price <= 0:
        # Fallback: estimate premium from SL/target points
        entry_price = signal.sl_points * 3  # rough estimate ~60-90 premium
        _log_event(f"Using estimated premium {entry_price:.2f} for {opt_name}")

    # SL and target in option premium terms
    sl_price = round(max(entry_price - signal.sl_points, 1.0), 2)
    tgt_price = round(entry_price + signal.target_points, 2)

    now_str = now_ist().isoformat()
    trade_id = f"AT-{uuid.uuid4().hex[:8].upper()}"
    lot_size = opt["lotsize"]

    insert_order({
        "order_id": trade_id,
        "symbol": opt_name,
        "token": opt["token"],
        "exchange": config.NFO_EXCHANGE,
        "side": "BUY",
        "order_type": "PAPER",
        "quantity": lot_size,
        "price": entry_price,
        "status": "COMPLETE",
        "placed_at": now_str,
        "completed_at": now_str,
        "remarks": f"AutoTrade BUY {opt_name} | {', '.join(signal.patterns)}",
    })
    insert_trade({
        "trade_id": trade_id,
        "symbol": opt_name,
        "token": opt["token"],
        "option_type": option_type,
        "strike": opt["strike"],
        "direction": direction,
        "entry_price": entry_price,
        "quantity": lot_size,
        "entry_order_id": trade_id,
        "entry_time": now_str,
        "status": "OPEN",
        "stop_loss": sl_price,
        "target": tgt_price,
        "expiry": opt["expiry_date"],
        "source": "AUTO",
        "created_at": now_str,
    })

    patterns_str = ", ".join(signal.patterns)
    _log_event(
        f"BUY {opt_name} @ ₹{entry_price:.2f} | "
        f"SL=₹{sl_price:.2f} TGT=₹{tgt_price:.2f} | "
        f"Patterns: {patterns_str} | Strength: {signal.strength}%"
    )
    return trade_id


def _monitor_positions():
    """
    Check open AUTO positions for SL/target hit, trailing SL, and smart exit.
    All positions are LONG options (BUY CE or PE) — exit = sell back.

    Smart exit logic:
        1. If premium moves 50%+ toward target → trail SL to breakeven + 5
        2. If premium was profitable but stalling at 10-30% progress → exit with profit
        3. Past FORCE_EXIT_TIME → close all positions (EOD exit)
    """
    open_trades = get_open_trades()
    auto_trades = [t for t in open_trades if (t.get("source") or t["trade_id"][:2]) in ("AUTO", "AT")]

    if not auto_trades:
        return

    # Batch-fetch option LTPs for all open positions
    option_tokens = [t["token"] for t in auto_trades if t.get("token")]
    option_ltps = {}
    if option_tokens:
        try:
            option_ltps = fetch_option_ltp(option_tokens)
        except Exception as e:
            log.warning("Monitor: option LTP fetch failed: %s", e)

    # Fallback: also get NIFTY LTP for any IDX trades (legacy)
    tick = get_latest_tick()
    nifty_ltp = tick.ltp

    force_exit = _is_past_force_exit()

    for t in auto_trades:
        trade_id = t["trade_id"]
        entry = t["entry_price"]
        direction = t["direction"]
        qty = t["quantity"]
        sl = t.get("stop_loss")
        tgt = t.get("target")
        token = t["token"]

        # Get LTP for this option
        trade_ltp = option_ltps.get(token, 0)
        if trade_ltp <= 0 and token == config.NIFTY_TOKEN:
            trade_ltp = nifty_ltp  # legacy IDX fallback
        if trade_ltp <= 0:
            continue

        # All trades are LONG (bought options) — P&L = (LTP - entry) * qty
        pnl = (trade_ltp - entry) * qty

        exit_reason = None
        exit_price = trade_ltp

        # 1. Force EOD exit
        if force_exit:
            exit_reason = "EOD_EXIT"
            _log_event(f"EOD EXIT {trade_id} @ {exit_price:.2f} PnL={pnl:.2f}")

        # 2. SL hit (option premium dropped below SL)
        elif sl:
            if trade_ltp <= sl:
                exit_reason = "SL_HIT"
                exit_price = sl

        # 3. Target hit (option premium rose to target)
        if not exit_reason and tgt:
            if trade_ltp >= tgt:
                exit_reason = "TARGET_HIT"
                exit_price = tgt

        # 4. Smart exit: trailing SL & unachievable target detection
        if not exit_reason and sl and tgt:
            total_move = abs(tgt - entry)
            if total_move > 0:
                progress = (trade_ltp - entry) / total_move

                # If premium moved 50%+ toward target, trail SL to breakeven + 5
                if progress >= 0.5:
                    new_sl = entry + 5
                    if sl is None or new_sl > sl:
                        _update_trade_sl(trade_id, new_sl)
                        _log_event(f"TRAIL SL {trade_id} → ₹{new_sl:.2f} (50% progress)")

                # Smart exit: premium was profitable but now stalling
                if 0.1 < progress < 0.3 and pnl > 0:
                    exit_reason = "SMART_EXIT"
                    _log_event(
                        f"SMART EXIT {trade_id} @ ₹{exit_price:.2f} | "
                        f"Progress={progress:.0%} PnL=₹{pnl:.2f} (target looks unachievable)"
                    )

        if exit_reason:
            # All trades are LONG — P&L = (exit - entry) * qty
            final_pnl = round((exit_price - entry) * qty, 2)

            now_str = now_ist().isoformat()
            close_trade(trade_id, exit_price, now_str, exit_reason, final_pnl)
            update_portfolio_after_trade(final_pnl, final_pnl >= 0)

            if exit_reason != "EOD_EXIT":
                _log_event(f"{exit_reason} {trade_id} @ {exit_price:.2f} PnL={final_pnl:.2f}")

            # After SL hit, block same direction to prevent immediate re-entry
            if exit_reason == "SL_HIT":
                opt_type = t.get("option_type", "CE")
                block_dir = "BULLISH" if opt_type == "CE" else "BEARISH"
                _set_sl_block(block_dir)


def _update_trade_sl(trade_id: str, new_sl: float):
    """Update the stop loss of a trade in the database."""
    from trading_bot.data.store import get_cursor
    sql = "UPDATE trades SET stop_loss = ? WHERE trade_id = ?"
    with get_cursor() as cur:
        cur.execute(sql, (new_sl, trade_id))


def _scan_and_trade():
    """One cycle: fetch candles → regime check → evaluate → AI filter → place trade."""
    scan_time = now_ist().strftime("%H:%M:%S")
    with _lock:
        _status["last_scan"] = scan_time

    session = get_session()
    if not session:
        _log_event("No session — skip scan")
        return

    # ── Gate 1: Trade window ─────────────────────────────────────────────
    if not _is_in_trade_window():
        _log_event(f"Outside trade window — next scan in {_SCAN_INTERVAL}s", console=False)
        return

    # ── Gate 2: Daily loss limit ─────────────────────────────────────────
    if _daily_loss_reached():
        _log_event("Daily loss limit reached — skipping new trades")
        return

    # ── Gate 3: Daily trade cap (anti-overtrading hard stop) ─────────────
    today_count = _count_today_auto_trades()
    if today_count >= config.MAX_DAILY_TRADES:
        _log_event(
            f"Daily trade cap ({config.MAX_DAILY_TRADES}) reached ({today_count} trades today) — done",
            console=False,
        )
        return

    # ── Gate 4: Per-15min overtrading cap ────────────────────────────────
    recent_count = _count_trades_last_n_minutes(15)
    if recent_count >= config.MAX_TRADES_PER_15MIN:
        _log_event(
            f"Per-15min cap: {recent_count} trade(s) in last 15 min — cooling off",
            console=False,
        )
        return

    # ── Gate 5: Max open trades ──────────────────────────────────────────
    open_trades = get_open_trades()
    auto_count = sum(
        1 for t in open_trades
        if (t.get("source") or t["trade_id"][:2]) in ("AUTO", "AT")
    )
    if auto_count >= config.MAX_OPEN_TRADES:
        _log_event(f"Max open trades ({config.MAX_OPEN_TRADES}) reached — skip", console=False)
        return

    # ── Fetch 1m candles ─────────────────────────────────────────────────
    df = _fetch_latest_candles(session, timeframe="1m")
    if df is None or len(df) < 20:
        bar_count = 0 if df is None else len(df)
        _log_event(f"Insufficient candles ({bar_count} bars, need 20) — skip")
        return

    # ── Fetch 15m candles for HTF bias ───────────────────────────────────
    df_15m: pd.DataFrame | None = None
    if config.HTF_ENABLED:
        try:
            df_15m = _fetch_latest_candles(session, timeframe="15m", bars=50)
        except Exception as e:
            log.warning("15m candle fetch failed (HTF bias unavailable): %s", e)

    _log_event(f"Scanning {len(df)} 1m candles | HTF={"15m/" + str(len(df_15m)) + "bars" if df_15m is not None else "N/A"}...")

    # ── Evaluate strategy (with all gates wired in) ──────────────────────
    signals = evaluate(df, df_15m=df_15m)
    if not signals:
        _log_event("No signals found this cycle", console=False)
        return

    _log_event(f"Found {len(signals)} signal(s) after all rule-based gates")

    for sig in signals:
        if sig.action != "ENTER":
            continue

        # ── Gate 6: Post-SL block ────────────────────────────────────────
        if _is_sl_blocked(sig.direction):
            _log_event(f"SKIP {sig.direction}: SL-blocked (cooling off after recent SL hit)", console=False)
            continue

        # ── Gate 7: Duplicate signal cooldown ───────────────────────────
        if _is_duplicate_signal(sig.direction):
            _log_event(f"SKIP {sig.direction}: duplicate within cooldown window", console=False)
            continue

        # ── Gate 8: AI Confidence Filter ────────────────────────────────
        # AI is used as a FILTER after rule-based approval — not as a trigger.
        # If AI is unavailable or rate-limited → fall through (do not block).
        if config.AI_FILTER_ENABLED:
            ai_confidence = _get_ai_confidence(sig, df)
            if ai_confidence is not None and ai_confidence < config.AI_MIN_CONFIDENCE:
                _log_event(
                    f"SKIP {sig.direction}: AI confidence {ai_confidence}/100 < "
                    f"threshold {config.AI_MIN_CONFIDENCE}"
                )
                continue
            elif ai_confidence is not None:
                _log_event(f"AI filter PASS: confidence={ai_confidence}/100")

        # ── All gates passed → place trade ──────────────────────────────
        tick = get_latest_tick()
        nifty_ltp = tick.ltp
        if nifty_ltp <= 0:
            tick, _ = fetch_live_once()
            nifty_ltp = tick.ltp

        trade_id = _place_auto_trade(sig, nifty_ltp)
        if trade_id:
            opt_type = "CE" if sig.direction == "BULLISH" else "PE"
            with _lock:
                _status["last_signal"] = {
                    "direction": sig.direction,
                    "option_type": opt_type,
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


def _get_ai_confidence(sig, df: pd.DataFrame) -> int | None:
    """
    Use LLM as a FILTER: ask for a 0-100 confidence score on the signal.

    Returns:
        int  — confidence score (0–100)
        None — AI unavailable/rate-limited → caller falls through

    The AI receives a structured prompt with:
      - market regime (ADX, EMA trend)
      - recent candle data (last 5 bars OHLCV summary)
      - signal details (pattern, direction, strength, filters)
    It must ONLY reply with a JSON {"confidence": <int 0-100>, "reason": "..."}

    On any error or timeout → return None (fail open).
    """
    import signal as _signal_mod
    import json as _json

    try:
        from trading_bot.llm.analyzer import get_ai_confidence_score
        last5 = df.tail(5)[["open", "high", "low", "close"]].round(2).to_dict("records")
        prompt_data = {
            "direction": sig.direction,
            "patterns": sig.patterns,
            "strength": sig.strength,
            "confirmations": sig.confirmations,
            "filters": sig.filters,
            "last_5_candles": last5,
            "sl_points": sig.sl_points,
            "target_points": sig.target_points,
        }
        score = get_ai_confidence_score(prompt_data, timeout=config.AI_FILTER_TIMEOUT)
        return score
    except ImportError:
        return None   # analyzer not available
    except Exception as exc:
        log.warning("AI confidence filter error: %s", exc)
        return None   # fail open — don't block trade on AI failure


def _auto_trade_loop():
    """Background thread: scan + monitor on a timer."""
    global _running
    _log_event("Auto-trade engine STARTED")
    _consecutive_errors = 0

    # Preload option instrument master
    try:
        from trading_bot.options import load_options
        opts = load_options()
        _log_event(f"Loaded {len(opts)} NIFTY option contracts")
    except Exception as e:
        _log_event(f"Warning: could not load options: {e}")

    try:
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

                _consecutive_errors = 0  # reset on success

            except Exception as exc:
                _consecutive_errors += 1
                log.error("AutoTrade loop error (#%d): %s", _consecutive_errors, exc, exc_info=True)
                _log_event(f"Error (#{_consecutive_errors}): {exc}")
                # Back off on repeated errors to prevent tight loop
                if _consecutive_errors >= 5:
                    backoff = min(_consecutive_errors * 10, 120)
                    _log_event(f"Too many errors — backing off {backoff}s")
                    time.sleep(backoff)

            time.sleep(_SCAN_INTERVAL)
    finally:
        # Always reset status when thread exits (crash or normal stop)
        with _lock:
            _status["enabled"] = False
        _log_event("Auto-trade engine STOPPED")


def start(scan_interval: int = 60):
    """Start the auto-trade background thread."""
    global _running, _thread, _SCAN_INTERVAL
    if _running and _thread and _thread.is_alive():
        _log_event("Auto-trade engine already running — skipping start")
        return
    # Reset state in case of previous crash
    _running = True
    _SCAN_INTERVAL = max(scan_interval, 30)  # minimum 30s
    with _lock:
        _status["enabled"] = True
    _thread = threading.Thread(target=_auto_trade_loop, name="autotrade", daemon=True)
    _thread.start()
    _log_event(f"Engine thread started (scan every {_SCAN_INTERVAL}s)")


def stop():
    """Stop the auto-trade engine."""
    global _running
    _running = False
    with _lock:
        _status["enabled"] = False
    _log_event("Auto-trade engine STOPPING")


def is_alive() -> bool:
    """Check if the auto-trade thread is actually running."""
    return _running and _thread is not None and _thread.is_alive()


def ensure_running(scan_interval: int = 60):
    """Restart the engine if it died unexpectedly."""
    if _running and _thread and not _thread.is_alive():
        _log_event("Engine thread died — restarting...")
        start(scan_interval)
    elif not _running:
        start(scan_interval)

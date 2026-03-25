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
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from trading_bot import config
from trading_bot.auth.login import get_session, force_reauth
from trading_bot.data.store import (
    close_trade, get_open_trades, get_today_pnl,
    insert_order, insert_trade, update_portfolio_after_trade,
)
from trading_bot.market import fetch_option_ltp, get_latest_tick, fetch_live_once
from trading_bot.strategy import evaluate, is_sideways_market, get_htf_bias, Signal
from trading_bot.scoring import fetch_daily_closes, analyze_live
from trading_bot.utils.logger import get_logger
from trading_bot.utils.time_utils import now_ist

log = get_logger(__name__)

IST = ZoneInfo(config.TIMEZONE)

# ── Daily candle cache (avoid re-fetching every 30s — daily bars change once/day) ──
_daily_df_cache: tuple = (None, 0.0)   # (DataFrame | None, epoch_of_last_fetch)
_DAILY_CACHE_TTL = 300                  # 5 minutes

# ── High-watermark tracker: max option LTP seen since entry ──────────────────────
# Persisted in Redis for Vercel (stateless per invocation)
_trade_max_price: dict[str, float] = {}

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


def _is_live_market_hours() -> bool:
    """Return True only during live NSE session (Mon-Fri, 09:15-15:30 IST)."""
    now = now_ist()
    if now.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    hhmm = now.strftime("%H:%M")
    return config.MARKET_OPEN_TIME <= hhmm <= config.MARKET_CLOSE_TIME


def _is_past_force_exit() -> bool:
    now = now_ist().strftime("%H:%M")
    return now >= config.FORCE_EXIT_TIME


def _daily_loss_reached() -> bool:
    today_str = now_ist().strftime("%Y-%m-%d")
    pnl = get_today_pnl(today_str)
    return pnl <= -config.MAX_DAILY_LOSS


def _is_duplicate_signal(direction: str) -> bool:
    """Check if same-direction signal was recently acted upon.

    On Vercel, in-memory _recent_signals resets on every cold start, so we
    also check a Redis key with a TTL equal to the cooldown period.

    NOTE: This function ONLY checks — it does NOT record the signal.
    Call _record_signal() after a trade is successfully placed.
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
    return False


def _record_signal(direction: str):
    """Record that a signal was acted upon (trade placed).
    Call this AFTER successful trade placement, not before."""
    now_ts = time.time()
    _recent_signals[direction] = now_ts
    try:
        from trading_bot.cache import _get_client
        r = _get_client()
        if r:
            r.set(f"autotrade:signal:{direction}", str(now_ts),
                  ex=config.DUPLICATE_SIGNAL_COOLDOWN)
    except Exception:
        pass


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


def _get_daily_closes(session) -> "pd.DataFrame | None":
    """Return NIFTY daily closes with 5-min in-memory cache."""
    global _daily_df_cache
    df_cached, fetched_at = _daily_df_cache
    if df_cached is not None and time.time() - fetched_at < _DAILY_CACHE_TTL:
        return df_cached
    df = fetch_daily_closes(session)
    _daily_df_cache = (df, time.time())
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
        cutoff = now_ist().timestamp() - (minutes * 60)
        from trading_bot.data.store import get_cursor
        with get_cursor() as cur:
            cur.execute(
                "SELECT entry_time FROM trades WHERE source='AUTO' AND entry_time IS NOT NULL",
            )
            rows = cur.fetchall()
        cnt = 0
        for r in rows:
            ts_raw = r[0]
            if not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_raw)).timestamp()
            except Exception:
                continue
            if ts >= cutoff:
                cnt += 1
        return cnt
    except Exception:
        return 0


def _consecutive_sl_losses() -> int:
    """Count consecutive SL losses from latest closed AUTO trades (today)."""
    today = now_ist().strftime("%Y-%m-%d")
    sl_tags = {"SL", "SL_HIT", "SL HIT", "STOPLOSS", "STOP_LOSS"}
    try:
        from trading_bot.data.store import get_cursor
        with get_cursor() as cur:
            cur.execute(
                "SELECT pnl, COALESCE(exit_reason,'') FROM trades "
                "WHERE source='AUTO' AND status='CLOSED' AND DATE(exit_time)=? "
                "ORDER BY exit_time DESC LIMIT 10",
                (today,),
            )
            rows = cur.fetchall()
        streak = 0
        for row in rows:
            pnl = float(row[0] or 0.0)
            reason = str(row[1] or "").upper().strip()
            if pnl < 0 and reason in sl_tags:
                streak += 1
            else:
                break
        return streak
    except Exception:
        return 0


def _intraday_drawdown() -> float:
    """Return max closed-trade drawdown for today's AUTO trades in rupees."""
    today = now_ist().strftime("%Y-%m-%d")
    try:
        from trading_bot.data.store import get_cursor
        with get_cursor() as cur:
            cur.execute(
                "SELECT pnl FROM trades "
                "WHERE source='AUTO' AND status='CLOSED' AND DATE(exit_time)=? "
                "ORDER BY exit_time ASC",
                (today,),
            )
            rows = cur.fetchall()
        equity = 0.0
        peak = 0.0
        dd = 0.0
        for row in rows:
            equity += float(row[0] or 0.0)
            if equity > peak:
                peak = equity
            dd = max(dd, peak - equity)
        return round(dd, 2)
    except Exception:
        return 0.0


def _load_dynamic_config() -> dict:
    """Read config/auto_config.json — updated daily by analysis scripts. Returns {} on any error."""
    import json
    from pathlib import Path
    cfg_path = Path(__file__).parent.parent / "config" / "auto_config.json"
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _count_today_losses() -> int:
    """Count total closing-loss AUTO trades today (for MAX_DAILY_LOSSES cap)."""
    today = now_ist().strftime("%Y-%m-%d")
    try:
        from trading_bot.data.store import get_cursor
        with get_cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM trades "
                "WHERE source='AUTO' AND status='CLOSED' AND DATE(exit_time)=? AND pnl < 0",
                (today,),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def risk_manager() -> tuple[bool, str]:
    """
    Global risk gate for new entries.

    Returns:
        (True, "") if safe to trade
        (False, reason) when any risk brake is active
    """
    if _daily_loss_reached():
        return False, "daily loss limit reached"

    today_count = _count_today_auto_trades()
    if today_count >= config.MAX_DAILY_TRADES:
        return False, f"daily trade cap reached ({today_count}/{config.MAX_DAILY_TRADES})"

    # 3 losses today → stop for the day (overtrading protection)
    losses_today = _count_today_losses()
    max_losses = getattr(config, 'MAX_DAILY_LOSSES', 999)
    if losses_today >= max_losses:
        return False, f"daily loss count {losses_today}/{max_losses} — no more trades today"

    recent_count = _count_trades_last_n_minutes(15)
    if recent_count >= config.MAX_TRADES_PER_15MIN:
        return False, f"per-15min cap hit ({recent_count}/{config.MAX_TRADES_PER_15MIN})"

    # Dynamic config: honour tighter maxConsecutiveLoss if set in auto_config.json
    dyn = _load_dynamic_config()
    dyn_max_losses = dyn.get("maxConsecutiveLoss", getattr(config, 'MAX_DAILY_LOSSES', 999))
    effective_max = min(getattr(config, 'MAX_DAILY_LOSSES', 999), dyn_max_losses)
    losses_dyn = _count_today_losses()
    if losses_dyn >= effective_max:
        return False, f"daily losses {losses_dyn}/{effective_max} reached (config+dynamic)"

    dd = _intraday_drawdown()
    if dd >= config.MAX_INTRADAY_DRAWDOWN:
        return False, f"intraday drawdown {dd:.0f} >= {config.MAX_INTRADAY_DRAWDOWN}"

    return True, ""


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
        # Fallback: estimate premium from SL/target points.
        # ATM options typically cost 6-10× the SL points on 1m NIFTY.
        entry_price = signal.sl_points * 8
        _log_event(f"Using estimated premium {entry_price:.2f} for {opt_name}")

    # Premium quality gate: avoid very cheap theta-burners and over-expensive entries
    if not (config.MIN_ENTRY_PREMIUM <= entry_price <= config.MAX_ENTRY_PREMIUM):
        _log_event(
            f"Skip trade: premium ₹{entry_price:.2f} outside allowed band "
            f"₹{config.MIN_ENTRY_PREMIUM:.0f}-₹{config.MAX_ENTRY_PREMIUM:.0f}"
        )
        return None

    # SL and target in option premium terms
    # SL: clamp between SL_MIN_POINTS and SL_MAX_POINTS (strict capital protection)
    if config.SL_MODE == "percent" and entry_price > 0:
        raw_sl = round(entry_price * config.SL_PCT_OF_PREMIUM, 2)
    else:
        raw_sl = signal.sl_points
    sl_pts = max(config.SL_MIN_POINTS, min(raw_sl, config.SL_MAX_POINTS))
    sl_price = round(max(entry_price - sl_pts, 1.0), 2)
    tgt_pts = getattr(config, 'FIXED_TARGET_POINTS', sl_pts * 2)
    tgt_price = round(entry_price + tgt_pts, 2)

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

        # Track high watermark (max price reached since entry)
        _redis_key = f"autotrade:max_price:{trade_id}"
        prev_max = _trade_max_price.get(trade_id, 0.0)
        # Also restore from Redis (for Vercel stateless invocations)
        try:
            from trading_bot.cache import _get_client as _rc
            r = _rc()
            if r:
                v = r.get(_redis_key)
                if v:
                    prev_max = max(prev_max, float(v if isinstance(v, str) else v.decode()))
        except Exception:
            pass
        current_max = max(prev_max, trade_ltp)
        _trade_max_price[trade_id] = current_max
        try:
            from trading_bot.cache import _get_client as _rc
            r = _rc()
            if r:
                r.set(_redis_key, str(current_max), ex=86400)  # expire after 1 day
        except Exception:
            pass

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

        # 4. Smart exit: early profit-taking at 65% of target & trailing SL
        if not exit_reason and sl and tgt:
            total_move = abs(tgt - entry)
            if total_move > 0:
                progress = (trade_ltp - entry) / total_move

                # EARLY PROFIT EXIT: sell at 65-70% of target — don't chase full target
                if progress >= 0.65:
                    exit_reason = "EARLY_PROFIT"
                    _log_event(
                        f"EARLY PROFIT {trade_id} @ ₹{exit_price:.2f} | "
                        f"Progress={progress:.0%} PnL=₹{pnl:.2f} (65%+ of target reached)"
                    )

                # If premium moved 40%+ but not yet 65%, trail SL to breakeven + 5
                elif progress >= 0.4:
                    new_sl = entry + 5
                    if sl is None or new_sl > sl:
                        _update_trade_sl(trade_id, new_sl)
                        _log_event(f"TRAIL SL {trade_id} → ₹{new_sl:.2f} ({progress:.0%} progress)")

        if exit_reason:
            # All trades are LONG — P&L = (exit - entry) * qty
            final_pnl = round((exit_price - entry) * qty, 2)

            now_str = now_ist().isoformat()
            max_px = _trade_max_price.get(trade_id) if exit_reason == "SL_HIT" else None
            actually_closed = close_trade(trade_id, exit_price, now_str, exit_reason, final_pnl,
                                          max_price_reached=max_px)
            if actually_closed:
                update_portfolio_after_trade(final_pnl, final_pnl >= 0)

                if exit_reason != "EOD_EXIT":
                    max_note = f" MaxReached={max_px:.2f}" if max_px else ""
                    _log_event(f"{exit_reason} {trade_id} @ {exit_price:.2f} PnL={final_pnl:.2f}{max_note}")

                # Clean up high-watermark state for closed trade
                _trade_max_price.pop(trade_id, None)
                try:
                    from trading_bot.cache import _get_client as _rc
                    r = _rc()
                    if r:
                        r.delete(f"autotrade:max_price:{trade_id}")
                except Exception:
                    pass

                # SL hit logged — direction block intentionally disabled; re-entry allowed immediately


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

    # Strict live-market-hours gate: do not scan outside real session time.
    if not _is_live_market_hours():
        _log_event(
            f"Market closed ({config.MARKET_OPEN_TIME}-{config.MARKET_CLOSE_TIME} IST only) — scan paused",
            console=False,
        )
        return

    # ── Gate 1: Trade window ─────────────────────────────────────────────
    if not _is_in_trade_window():
        _log_event(f"Outside trade window — next scan in {_SCAN_INTERVAL}s", console=False)
        return

    # ── Gate 2: Global risk manager ──────────────────────────────────────
    ok, reason = risk_manager()
    if not ok:
        _log_event(f"Risk manager blocked new entry: {reason}", console=False)
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
    if df is None or len(df) < 10:
        bar_count = 0 if df is None else len(df)
        _log_event(f"Insufficient candles ({bar_count} bars, need 10) — candle strategy skipped")
        # Candles unavailable (rate limit or token issue) — still attempt 20D
        if bar_count == 0:
            # Likely a token error — force fresh login for next cycle
            try:
                force_reauth()
                _log_event("Token refresh triggered due to candle fetch failure")
            except Exception as _rea:
                log.warning("force_reauth failed: %s", _rea)
        tick = get_latest_tick()
        fallback_px = tick.ltp if tick.ltp > 0 else 0.0
        _try_20d_trade(session, df_1m=None, live_px=fallback_px)
        return

    # ── Fetch 15m candles for HTF bias ───────────────────────────────────
    df_15m: pd.DataFrame | None = None
    if config.HTF_ENABLED:
        try:
            df_15m = _fetch_latest_candles(session, timeframe="15m", bars=50)
        except Exception as e:
            log.warning("15m candle fetch failed (HTF bias unavailable): %s", e)

    htf_label = f"15m/{len(df_15m)} bars" if df_15m is not None else "N/A"
    _log_event(f"Scanning {len(df)} 1m candles | HTF={htf_label}...")

    # ── Evaluate strategy (with all gates wired in) ──────────────────────
    signals = evaluate(df, df_15m=df_15m)

    enter_signals = [s for s in signals if s.action == "ENTER"] if signals else []
    skip_signals = [s for s in signals if s.action != "ENTER"] if signals else []

    has_candle_signals = bool(signals or enter_signals)

    # ── Midday elevated-quality gate ────────────────────────────────────────
    # During 11:25–13:30 (chop zone) require a much stronger signal to trade.
    now_hhmm = now_ist().strftime("%H:%M")
    if config.MIDDAY_START <= now_hhmm <= config.MIDDAY_END:
        before = len(enter_signals)
        enter_signals = [
            s for s in enter_signals
            if s.strength >= config.MIDDAY_MIN_STRENGTH
            and s.confirmations >= config.MIDDAY_MIN_CONFIRMATIONS
        ]
        filtered = before - len(enter_signals)
        if filtered:
            _log_event(
                f"Midday quality gate: dropped {filtered} weak signal(s) "
                f"(need str≥{config.MIDDAY_MIN_STRENGTH}, conf≥{config.MIDDAY_MIN_CONFIRMATIONS})",
                console=False,
            )

    _log_event(
        f"Signals this cycle: total={len(signals)} enter={len(enter_signals)} skip={len(skip_signals)}"
    )

    if skip_signals:
        reasons = [s.reason for s in skip_signals[:2] if s.reason]
        if reasons:
            _log_event(f"Skip reasons: {' | '.join(reasons)}", console=False)

    if not has_candle_signals:
        _log_event("No candle signals this cycle — checking 20-day avg...", console=False)

    placed_trade = False
    for sig in enter_signals:
        # ── Gate 7: Duplicate signal cooldown ───────────────────────────
        if _is_duplicate_signal(sig.direction):
            _log_event(f"SKIP {sig.direction}: duplicate within cooldown window", console=False)
            continue

        # ── Gate 8: AI Confidence Filter ────────────────────────────────
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
            # Record signal ONLY after successful trade placement
            _record_signal(sig.direction)
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
            placed_trade = True
            break  # one trade per scan cycle

    # ── 20-Day Avg strategy signal ─────────────────────────────────────────────
    if not placed_trade:
        live_px = float(df["close"].iloc[-1]) if df is not None and len(df) > 0 else 0.0
        if live_px <= 0:
            tick = get_latest_tick()
            live_px = tick.ltp if tick.ltp > 0 else 0.0
        placed_trade = _try_20d_trade(session, df_1m=df, live_px=live_px)

    if not placed_trade:
        _log_event("No position opened this scan (all ENTER candidates were blocked)", console=False)


def _try_20d_trade(session, df_1m, live_px: float) -> bool:
    """
    Run the 20-Day Avg strategy analysis and place a trade if the signal fires.
    Called both from the normal scan path and as a fallback when 1m candles fail.
    Returns True if a trade was placed.
    """
    try:
        daily_df = _get_daily_closes(session)
        if daily_df is None or len(daily_df) < 20:
            _log_event("20D AVG: [SKIP] insufficient daily data", console=False)
            return False
        if live_px <= 0:
            _log_event("20D AVG: [SKIP] no live price", console=False)
            return False

        sig_20d = analyze_live(daily_df, df_1m, live_px)
        action_tag = "ENTER" if sig_20d.should_enter else "SKIP"
        skip_info = (f" ({'; '.join(sig_20d.skip_reasons)})"
                     if not sig_20d.should_enter and sig_20d.skip_reasons else "")
        _log_event(
            f"20D AVG: [{action_tag}] {sig_20d.direction} {sig_20d.option_type} "
            f"| SMA={sig_20d.sma_value:.0f} LTP={live_px:.0f} "
            f"Dist={sig_20d.distance_pct:+.2f}%{skip_info}",
            console=False,
        )

        if not sig_20d.should_enter:
            return False

        if _is_duplicate_signal(sig_20d.direction):
            _log_event(f"SKIP 20D {sig_20d.direction}: duplicate within cooldown", console=False)
            return False

        sig_adapter = SimpleNamespace(
            direction=sig_20d.direction,
            entry_price=live_px,
            sl_points=config.INITIAL_SL_POINTS,
            patterns=[f"20D-{sig_20d.signal_type}"],
            strength=80,
        )
        tick = get_latest_tick()
        nifty_ltp = tick.ltp if tick.ltp > 0 else live_px
        trade_id = _place_auto_trade(sig_adapter, nifty_ltp)
        if trade_id:
            _record_signal(sig_20d.direction)
            with _lock:
                _status["last_signal"] = {
                    "direction": sig_20d.direction,
                    "option_type": sig_20d.option_type,
                    "patterns": [f"20D-{sig_20d.signal_type}"],
                    "strength": 80,
                    "trade_id": trade_id,
                    "time": now_ist().strftime("%H:%M:%S"),
                }
            return True
    except Exception as exc:
        log.warning("20D signal check failed: %s", exc)
    return False


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


_MONITOR_INTERVAL = 5   # seconds between position checks (SL/target)


def _auto_trade_loop():
    """Background thread: monitor every 5s, scan for new signals every SCAN_INTERVAL."""
    global _running
    _log_event("Auto-trade engine STARTED")
    _consecutive_errors = 0
    _last_scan_time = 0.0  # epoch; 0 ensures first loop scans immediately

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
                # --- Monitor existing positions every 5 seconds ---
                _monitor_positions()

                # --- Scan for new signals only every SCAN_INTERVAL ---
                now_ts = time.time()
                if now_ts - _last_scan_time >= _SCAN_INTERVAL:
                    _scan_and_trade()
                    _last_scan_time = now_ts

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

            time.sleep(_MONITOR_INTERVAL)   # wake every 5s to check positions
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
    _SCAN_INTERVAL = max(scan_interval, 5)   # minimum 5s
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

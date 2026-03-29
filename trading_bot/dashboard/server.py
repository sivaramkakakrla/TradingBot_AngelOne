from trading_bot.strategy import evaluate
# ─────────────────────────────────────────────────────────────────────────────
import datetime
import json
import os
import threading
import csv
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

import uuid

import numpy as np
import pandas as pd

from trading_bot import config
from trading_bot.data.store import (
    close_trade, fetch_candles, fetch_candles_by_date,
    get_cursor, get_open_trades, get_today_pnl, init_db,
    insert_order, insert_trade,
    get_portfolio, init_portfolio, update_portfolio_after_trade, reset_portfolio,
    get_pnl_between, get_weekly_pnl_breakdown,
)
from trading_bot.indicators import sma, linear_regression
from trading_bot.candles import detect_all, scan_signals
from trading_bot.strategy import evaluate, evaluate_latest, evaluate_historical
from trading_bot.market import get_latest_tick, get_latest_sensex_tick, start_feed, stop_feed, fetch_option_ltp, fetch_live_once
from trading_bot.options import (
    build_option_chain, get_available_expiries, get_weekly_expiries,
    load_options, get_option_ltp as _opt_ltp,
)
from trading_bot.utils.logger import get_logger
from trading_bot.utils.time_utils import now_ist
from trading_bot.cache import get_cached, set_cached

log = get_logger(__name__)

IST = ZoneInfo(config.TIMEZONE)
_HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(_HERE, "templates"))
CORS(app)


# ─── Server-Side Background Scanner (runs WITHOUT browser) ───────────────────
# This is the PERMANENT solution: a background thread inside the Flask process
# that scans every 30s during market hours. Works on Railway, local, Render, etc.
# No browser tab needed. No external cron needed.

_bg_scanner_started = False
_bg_scanner_lock = threading.Lock()


def _server_side_scanner():
    """Background thread: triggers autotrade scan every 30s during market hours.
    Runs independently of any browser connection."""
    import time as _time
    _scan_log = get_logger("bg_scanner")
    _scan_log.info("Server-side background scanner STARTED")

    while True:
        try:
            from trading_bot.autotrade import (
                _is_live_market_hours, _is_in_trade_window,
                _monitor_positions, _scan_and_trade,
                is_alive, start as at_start,
            )

            if _is_live_market_hours():
                # Auto-restart the engine thread if it died
                if not is_alive():
                    _scan_log.warning("Engine thread dead — auto-restarting")
                    at_start(scan_interval=60)

                _monitor_positions()
                if _is_in_trade_window():
                    _scan_and_trade()
        except Exception as exc:
            get_logger("bg_scanner").error("BG scan error: %s", exc, exc_info=True)

        _time.sleep(30)


def start_background_scanner():
    """Start the server-side scanner once. Safe to call multiple times."""
    global _bg_scanner_started
    if os.getenv("VERCEL"):
        return  # Vercel is serverless — use cron instead
    with _bg_scanner_lock:
        if _bg_scanner_started:
            return
        _bg_scanner_started = True
    t = threading.Thread(target=_server_side_scanner, name="bg_scanner", daemon=True)
    t.start()
    log.info("Background scanner thread launched (scans every 30s, no browser needed)")


@app.before_request
def _ensure_background_scanner():
    """Auto-start the background scanner on the very first request."""
    if not _bg_scanner_started and not os.getenv("VERCEL"):
        start_background_scanner()


@app.after_request
def _no_cache_html(response):
    """Prevent browser from caching HTML pages so template changes take effect immediately."""
    if response.content_type and 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

# ─────────────────────────────────────────────────────────────────────────────
# Strategy Test API — returns signals for a given date (for dashboard test tab)
@app.route("/api/strategy/test")
def api_strategy_test():
    """Test the strategy on a given date. Full error handling for Vercel."""
    import traceback
    try:
        date_str = request.args.get("date")
        timeframe = request.args.get("timeframe", "1m")
        mode = request.args.get("mode", "candle")   # "candle" or "20day"
        if not date_str:
            date_str = now_ist().strftime("%Y-%m-%d")

        # Ensure DB schema exists (Vercel cold starts wipe /tmp)
        init_db()

        # ── mode=20day: 20-Day Avg + LinReg backtest ─────────────────
        if mode == "20day":
            return _backtest_20day(date_str, timeframe)

        # Step 1: Try DB first
        rows = fetch_candles_by_date(config.UNDERLYING, timeframe, date_str)

        # Step 2: If no DB data, fetch live from AngelOne API
        if not rows:
            from trading_bot.data.historical import fetch_candles_for_day
            from trading_bot.data.store import upsert_candles
            from trading_bot.auth.login import get_session
            from datetime import date as _date

            session = get_session()
            if not session:
                return jsonify({"error": "Could not authenticate with AngelOne. Check API credentials.", "date": date_str}), 500

            trade_date = _date.fromisoformat(date_str)
            api_rows = fetch_candles_for_day(session, trade_date, timeframe)
            if not api_rows:
                return jsonify({"error": f"No candle data for {date_str}. Market may be closed (holiday/weekend).", "date": date_str}), 404

            upsert_candles(api_rows)
            rows = fetch_candles_by_date(config.UNDERLYING, timeframe, date_str)

        if not rows:
            return jsonify({"error": "No candle data for date", "date": date_str}), 404

        # Step 3: Build DataFrame (include timestamp for signal bar timestamps)
        df = pd.DataFrame(
            [{"open": r["open"], "high": r["high"], "low": r["low"],
              "close": r["close"], "volume": r["volume"],
              "timestamp": r["timestamp"]} for r in rows],
        )
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)

        # Step 4+5: Walk-forward simulation — ONE trade at a time, no overlaps
        # Rules:
        #   - Take the STRONGEST signal from each bar (highest strength), not duplicates
        #   - After exit: 10-bar SL cooldown, 2-bar target cooldown
        #   - Max 6 trades per day to prevent overtrading
        #   - Minimum 3 bars between entries (signal cooldown)
        MAX_DAILY_TRADES   = 6
        SL_COOLDOWN_BARS   = 10   # ~10 min pause after SL hit
        TGT_COOLDOWN_BARS  = 2    # ~2 min pause after target hit
        WARMUP_BARS        = 30   # skip first 30 bars (indicator warmup)

        trades     = []
        scan_from  = WARMUP_BARS
        last_entry_bar = -5  # track last entry to avoid same-bar duplicates

        while scan_from < len(df) and len(trades) < MAX_DAILY_TRADES:
            # Find next entry signal scanning bar by bar
            entry_sig  = None
            entry_idx  = -1

            for i in range(scan_from, len(df)):
                if i == last_entry_bar:
                    continue
                try:
                    subdf = df.iloc[:i + 1].copy()
                    sigs  = evaluate(subdf, backtest=True)
                    # Take highest-strength ENTER signal at this bar (no duplicates)
                    enter_sigs = [s for s in sigs if s.action == "ENTER"]
                    if not enter_sigs:
                        continue
                    best = max(enter_sigs, key=lambda s: s.strength)
                    entry_sig = best.to_dict()
                    entry_idx = i
                    break
                except Exception:
                    continue

            if entry_sig is None or entry_idx < 0:
                break  # no more signals today

            # Simulate the trade
            entry_px  = entry_sig["entry_price"]
            sl_pts    = entry_sig["sl_points"]
            tgt_pts   = entry_sig["target_points"]
            direction = entry_sig["direction"]

            if direction == "BULLISH":
                sl_price  = entry_px - sl_pts
                tgt_price = entry_px + tgt_pts
            else:
                sl_price  = entry_px + sl_pts
                tgt_price = entry_px - tgt_pts

            exit_price  = None
            exit_reason = "EOD"
            exit_idx    = len(df) - 1
            exit_time   = str(df["timestamp"].iloc[-1]) if "timestamp" in df.columns else ""

            for j in range(entry_idx + 1, len(df)):
                h = float(df["high"].iloc[j])
                l = float(df["low"].iloc[j])
                if direction == "BULLISH":
                    if l <= sl_price:
                        exit_price, exit_reason, exit_idx = sl_price, "SL_HIT", j
                        exit_time = str(df["timestamp"].iloc[j]) if "timestamp" in df.columns else ""
                        break
                    if h >= tgt_price:
                        exit_price, exit_reason, exit_idx = tgt_price, "TARGET_HIT", j
                        exit_time = str(df["timestamp"].iloc[j]) if "timestamp" in df.columns else ""
                        break
                else:
                    if h >= sl_price:
                        exit_price, exit_reason, exit_idx = sl_price, "SL_HIT", j
                        exit_time = str(df["timestamp"].iloc[j]) if "timestamp" in df.columns else ""
                        break
                    if l <= tgt_price:
                        exit_price, exit_reason, exit_idx = tgt_price, "TARGET_HIT", j
                        exit_time = str(df["timestamp"].iloc[j]) if "timestamp" in df.columns else ""
                        break

            if exit_price is None:
                exit_price = float(df["close"].iloc[-1])

            pnl = (exit_price - entry_px) if direction == "BULLISH" else (entry_px - exit_price)

            entry_sig["sl_price"]    = round(sl_price, 2)
            entry_sig["target_price"] = round(tgt_price, 2)
            entry_sig["exit_price"]  = round(exit_price, 2)
            entry_sig["exit_reason"] = exit_reason
            entry_sig["exit_time"]   = exit_time
            entry_sig["pnl_points"]  = round(pnl, 2)
            trades.append(entry_sig)

            last_entry_bar = entry_idx
            # Cooldown: longer pause after SL to prevent revenge trading
            if exit_reason == "SL_HIT":
                scan_from = exit_idx + SL_COOLDOWN_BARS
            else:
                scan_from = exit_idx + TGT_COOLDOWN_BARS

        # Step 6: Summary stats
        wins = [t for t in trades if t["pnl_points"] > 0]
        losses = [t for t in trades if t["pnl_points"] <= 0]
        total_pnl = sum(t["pnl_points"] for t in trades)
        summary = {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(trades), 2) if trades else 0,
        }

        candles = [{"open": r["open"], "high": r["high"], "low": r["low"],
                    "close": r["close"], "volume": r["volume"],
                    "time": r["timestamp"]} for r in rows]
        return jsonify(_sanitize({"candles": candles, "signals": trades, "summary": summary}))

    except Exception as exc:
        tb = traceback.format_exc()
        log.error("api_strategy_test crash: %s\n%s", exc, tb)
        return jsonify({"error": f"{type(exc).__name__}: {exc}", "traceback": tb}), 500


def _backtest_20day(date_str: str, timeframe: str):
    """Run 20-Day Avg + LinReg(14) + Theta backtest for a single day."""
    import traceback
    from datetime import date as _date, datetime as _dt
    from trading_bot.scoring import fetch_daily_closes, analyze_live, compute_20day_avg

    try:
        session = None
        try:
            from trading_bot.auth.login import get_session
            session = get_session()
        except Exception:
            pass
        if not session:
            return jsonify({"error": "no session", "signals": [], "summary": {}})

        # 1) Fetch daily data (for 20-day SMA + LinReg daily)
        daily_df = fetch_daily_closes(session)
        if daily_df is None or len(daily_df) < 20:
            return jsonify({"error": "insufficient daily data", "signals": [], "summary": {}})

        # Slice daily_df to only include rows UP TO the test date.
        # Without this, the SMA/LinReg slope is computed from today's data,
        # making every historical date show today's bias (FALLING → all SELL).
        daily_df_as_of = daily_df[daily_df["timestamp"] <= date_str].copy()
        if len(daily_df_as_of) < 20:
            # Fall-through: not enough historical rows — use full df (live mode)
            daily_df_as_of = daily_df

        # 2) Fetch 1m candles for the test date
        rows = fetch_candles_by_date(config.UNDERLYING, timeframe, date_str)
        if not rows:
            from trading_bot.data.historical import fetch_candles_for_day
            from trading_bot.data.store import upsert_candles
            trade_date = _date.fromisoformat(date_str)
            api_rows = fetch_candles_for_day(session, trade_date, timeframe)
            if not api_rows:
                return jsonify({"error": f"No candle data for {date_str}", "signals": [], "summary": {}}), 404
            upsert_candles(api_rows)
            rows = fetch_candles_by_date(config.UNDERLYING, timeframe, date_str)

        if not rows:
            return jsonify({"error": "No candle data", "signals": [], "summary": {}}), 404

        df_1m = pd.DataFrame(
            [{"open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
              "close": float(r["close"]), "volume": float(r["volume"]),
              "timestamp": r["timestamp"]} for r in rows],
        )

        # 3) Walk through 1m bars — find entries and simulate P&L
        trades = []
        SL_PTS  = 20.0   # tight SL — NIFTY 20-pt max adverse move per signal
        TGT_PTS = 30.0   # 1:1.5 R:R — only need 40% win rate to break even
        MAX_DAILY_TRADES = 8   # prevent overtrading (30 trades/day was destroying edge)
        SL_COOLDOWN_BARS = 10  # 10-min pause after SL (let market settle)
        TGT_COOLDOWN_BARS = 3  # 3-min pause after target (allow brief consolidation)
        step = 5
        scan_from = 14  # skip first 14 bars (LinReg warmup)

        while scan_from < len(df_1m) and len(trades) < MAX_DAILY_TRADES:
            # Scan for next entry signal
            entry_found = False
            for i in range(scan_from, len(df_1m), step):
                subdf = df_1m.iloc[:i + 1].copy()
                live_price = float(subdf["close"].iloc[-1])
                bar_ts = str(subdf["timestamp"].iloc[-1])
                try:
                    bar_time = _dt.fromisoformat(bar_ts)
                except Exception:
                    bar_time = None

                result = analyze_live(daily_df_as_of, subdf, live_price, bar_time=bar_time)
                if not result.should_enter:
                    continue

                # --- Entry found — simulate P&L ---
                sig = result.to_dict()
                sig["bar_timestamp"] = bar_ts
                sig["entry_idx"] = i
                entry_px = sig["entry_price"]
                direction = sig["direction"]
                sig["sl_points"] = SL_PTS
                sig["target_points"] = TGT_PTS

                if direction == "BULLISH":
                    sl_price = entry_px - SL_PTS
                    tgt_price = entry_px + TGT_PTS
                else:
                    sl_price = entry_px + SL_PTS
                    tgt_price = entry_px - TGT_PTS

                exit_price = None
                exit_reason = "EOD"
                exit_idx = len(df_1m) - 1
                exit_time = str(df_1m["timestamp"].iloc[-1])

                for j in range(i + 1, len(df_1m)):
                    h = float(df_1m["high"].iloc[j])
                    l = float(df_1m["low"].iloc[j])
                    if direction == "BULLISH":
                        if l <= sl_price:
                            exit_price, exit_reason, exit_idx = sl_price, "SL_HIT", j
                            exit_time = str(df_1m["timestamp"].iloc[j])
                            break
                        if h >= tgt_price:
                            exit_price, exit_reason, exit_idx = tgt_price, "TARGET_HIT", j
                            exit_time = str(df_1m["timestamp"].iloc[j])
                            break
                    else:
                        if h >= sl_price:
                            exit_price, exit_reason, exit_idx = sl_price, "SL_HIT", j
                            exit_time = str(df_1m["timestamp"].iloc[j])
                            break
                        if l <= tgt_price:
                            exit_price, exit_reason, exit_idx = tgt_price, "TARGET_HIT", j
                            exit_time = str(df_1m["timestamp"].iloc[j])
                            break

                if exit_price is None:
                    exit_price = float(df_1m["close"].iloc[-1])

                pnl = (exit_price - entry_px) if direction == "BULLISH" else (entry_px - exit_price)
                sig["sl_price"] = round(sl_price, 2)
                sig["target_price"] = round(tgt_price, 2)
                sig["exit_price"] = round(exit_price, 2)
                sig["exit_reason"] = exit_reason
                sig["exit_time"] = exit_time
                sig["exit_idx"] = exit_idx
                sig["pnl_points"] = round(pnl, 2)
                trades.append(sig)

                # Continue scanning after this trade's exit
                # Apply cooldown: longer after SL to avoid revenge trading
                if exit_reason == "SL_HIT":
                    scan_from = exit_idx + SL_COOLDOWN_BARS
                else:
                    scan_from = exit_idx + TGT_COOLDOWN_BARS
                entry_found = True
                break

            if not entry_found:
                break  # no more signals found

        # 4) Summary
        wins = [t for t in trades if t["pnl_points"] > 0]
        losses = [t for t in trades if t["pnl_points"] <= 0]
        total_pnl = sum(t["pnl_points"] for t in trades)
        summary = {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(trades), 2) if trades else 0,
        }

        candles = [{"open": r["open"], "high": r["high"], "low": r["low"],
                     "close": r["close"], "volume": r["volume"],
                     "time": r["timestamp"]} for r in rows]
        return jsonify(_sanitize({"candles": candles, "signals": trades, "summary": summary}))

    except Exception as exc:
        tb = traceback.format_exc()
        log.error("_backtest_20day crash: %s\n%s", exc, tb)
        return jsonify({"error": f"{type(exc).__name__}: {exc}", "traceback": tb}), 500


# ─── Numpy-safe JSON helper ───────────────────────────────────────────────────

def _sanitize(obj):
    """Recursively convert numpy scalars/arrays to native Python types."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ts_to_ist_parts(ts_str: str) -> dict:
    """
    Convert an ISO-8601 timestamp to IST components for Lightweight Charts.

    Returns dict with:
        epoch  – fake-UTC epoch that renders as IST on the chart
        ist    – 'HH:MM' IST string for tooltip labels
    """
    dt = datetime.datetime.fromisoformat(ts_str)
    ist_dt = dt.astimezone(IST)
    # Build a fake-UTC datetime with IST wall-clock values so LC displays IST
    fake_utc = datetime.datetime(
        ist_dt.year, ist_dt.month, ist_dt.day,
        ist_dt.hour, ist_dt.minute, ist_dt.second,
        tzinfo=datetime.timezone.utc,
    )
    return {
        "epoch": int(fake_utc.timestamp()),
        "ist":   ist_dt.strftime("%H:%M"),
    }


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")





@app.route("/orders")
def orders_page():
    return render_template("orders.html")


@app.route("/history")
@app.route("/backtest")
def history_page():
    return render_template("history.html")


@app.route("/180-rule")
def rule180_page():
    return render_template("rule180.html")


@app.route("/orb-strategy")
def orb_strategy_page():
    return render_template("orb_strategy.html")


@app.route("/pnl")
def pnl_page():
    return render_template("pnl.html")


@app.route("/scalping")
def scalping_page():
    return render_template("scalping.html")


@app.route("/option-chain")
def option_chain_page():
    return render_template("option_chain.html")


@app.route("/api/candles")
def api_candles():
    """
    Return OHLCV candles for a given date and timeframe.

    Query params:
        date      – YYYY-MM-DD  (default: today IST)
        timeframe – 1m | 5m | 15m | 1D  (default: 1m)
    """
    date_str  = request.args.get("date",      now_ist().strftime("%Y-%m-%d"))
    timeframe = request.args.get("timeframe", "1m")

    if timeframe not in config.TIMEFRAMES:
        return jsonify({"error": f"Invalid timeframe. Valid: {list(config.TIMEFRAMES)}"}), 400

    rows = fetch_candles_by_date(config.UNDERLYING, timeframe, date_str)
    candles = []
    for row in rows:
        parts = _ts_to_ist_parts(row["timestamp"])
        candles.append({
            "time":   parts["epoch"],
            "ist":    parts["ist"],
            "open":   row["open"],
            "high":   row["high"],
            "low":    row["low"],
            "close":  row["close"],
            "volume": row["volume"],
        })

    # Day close = last candle's close (for the close price line)
    day_close = candles[-1]["close"] if candles else None

    # ── Compute overlay indicators ────────────────────────────────────────
    overlays = {"sma20": [], "sma200": [], "linreg": [], "linreg_forecast": []}
    if candles:
        closes = pd.Series([c["close"] for c in candles], dtype=float)
        times  = [c["time"] for c in candles]

        sma20  = sma(closes, 20)
        sma200 = sma(closes, 200)
        lr     = linear_regression(closes, period=min(20, len(closes)))

        for i, t in enumerate(times):
            v20 = sma20.iloc[i]
            overlays["sma20"].append({"time": t, "value": round(v20, 2)} if not np.isnan(v20) else None)

            v200 = sma200.iloc[i]
            if i >= 199 and not np.isnan(v200):
                overlays["sma200"].append({"time": t, "value": round(v200, 2)})
            else:
                overlays["sma200"].append(None)

            vlr = lr["linreg"].iloc[i]
            overlays["linreg"].append({"time": t, "value": round(vlr, 2)} if not np.isnan(vlr) else None)

            vfc = lr["linreg_forecast"].iloc[i]
            overlays["linreg_forecast"].append({"time": t, "value": round(vfc, 2)} if not np.isnan(vfc) else None)

        # Filter out None entries for clean JSON
        overlays = {k: [p for p in v if p is not None] for k, v in overlays.items()}

    return jsonify({
        "date": date_str,
        "timeframe": timeframe,
        "candles": candles,
        "day_close": day_close,
        "overlays": overlays,
    })


@app.route("/api/dates")
def api_dates():
    """Return distinct trading dates (newest first) that have 1m candle data."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT DATE(timestamp) AS d FROM candles "
            "WHERE symbol = ? AND timeframe = '1m' ORDER BY d DESC LIMIT 90",
            (config.UNDERLYING,),
        )
        dates = [row["d"] for row in cur.fetchall()]
    return jsonify({"dates": dates})


@app.route("/api/pnl")
def api_pnl():
    """Return today's PnL and open-trade count."""
    if os.getenv("VERCEL"):
        from trading_bot.redis_sync import sync_trades_from_redis
        sync_trades_from_redis()
    today       = now_ist().strftime("%Y-%m-%d")
    pnl         = get_today_pnl(today)
    open_trades = len(get_open_trades())
    return jsonify({
        "date":        today,
        "total_pnl":   round(pnl, 2),
        "open_trades": open_trades,
        "mode":        config.TRADING_MODE.upper(),
    })


@app.route("/api/pnl/summary")
def api_pnl_summary():
    """Return P&L summary: today, this month, and last 4 weeks breakdown."""
    import datetime as _dt
    today = now_ist().date()
    today_str = today.isoformat()

    # Today's P&L
    today_pnl = get_today_pnl(today_str)

    # This month P&L (1st of month to today)
    month_start = today.replace(day=1).isoformat()
    month_summary = get_pnl_between(month_start, today_str)

    # Last 4 weeks breakdown
    weekly = get_weekly_pnl_breakdown(4)

    # Last 5 days daily breakdown
    from trading_bot.data.store import get_daily_pnl_breakdown
    daily = get_daily_pnl_breakdown(5)

    return jsonify({
        "today": {"date": today_str, "pnl": round(today_pnl, 2)},
        "this_month": {
            "from": month_start,
            "to": today_str,
            **month_summary,
        },
        "weekly": weekly,
        "daily": daily,
    })


@app.route("/api/signals")
def api_signals():
    """
    Scan current candle data for confirmed candlestick signals.

    Query params:
        date      – YYYY-MM-DD  (default: today)
        timeframe – 1m | 5m | 15m  (default: 5m)
    """
    date_str  = request.args.get("date", now_ist().strftime("%Y-%m-%d"))
    timeframe = request.args.get("timeframe", "5m")

    rows = fetch_candles_by_date(config.UNDERLYING, timeframe, date_str)
    if not rows:
        return jsonify({"signals": [], "message": "No candle data"})

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(int)

    from trading_bot.strategy import evaluate
    signals = evaluate(df)
    out = []
    for sig in signals:
        d = sig.to_dict()
        d["patterns"] = d["patterns"]       # already a list
        out.append(d)

    # Also include raw pattern markers for chart overlay
    raw_patterns = scan_signals(df)

    return jsonify({
        "date": date_str,
        "timeframe": timeframe,
        "signals": out,
        "pattern_markers": raw_patterns,
    })


# ─── Paper Trading APIs ───────────────────────────────────────────────────────

# ─── Market Status & Live Feed ─────────────────────────────────────────────────

def _is_market_open() -> bool:
    """Return True if current IST time is within market hours (Mon-Fri 9:15-15:30)."""
    n = now_ist()
    if n.weekday() >= 5:  # Sat/Sun
        return False
    t = n.time()
    return datetime.time(9, 15) <= t <= datetime.time(15, 30)


@app.route("/api/live")
def api_live():
    """Return latest NIFTY + SENSEX market ticks + market status."""
    # ── Redis cache (30 s) ──
    cached = get_cached("nifty:live")
    if cached:
        return jsonify(cached)

    tick = get_latest_tick()
    sensex = get_latest_sensex_tick()
    # Re-fetch when: no data yet, OR cached data is older than 5 seconds
    # (background feed not running — serverless / not authenticated)
    needs_refresh = tick.ltp <= 0
    if not needs_refresh and tick.fetched_at:
        try:
            fa = datetime.datetime.fromisoformat(tick.fetched_at)
            age = (now_ist() - fa).total_seconds()
            needs_refresh = age > 5
        except Exception:
            needs_refresh = True
    if needs_refresh:
        tick, sensex = fetch_live_once()
    result = {
        "ltp":          tick.ltp,
        "open":         tick.open,
        "high":         tick.high,
        "low":          tick.low,
        "prev_close":   tick.close,
        "volume":       tick.volume,
        "timestamp":    tick.timestamp,
        "fetched_at":   tick.fetched_at,
        "market_open":  _is_market_open(),
        "sensex": {
            "ltp":        sensex.ltp,
            "open":       sensex.open,
            "high":       sensex.high,
            "low":        sensex.low,
            "prev_close": sensex.close,
            "timestamp":  sensex.timestamp,
        },
    }
    set_cached("nifty:live", result, ttl=30)
    return jsonify(result)


@app.route("/api/feed/start", methods=["POST"])
def api_feed_start():
    """Start the live market data feed (requires login first)."""
    from trading_bot.auth.login import authenticate
    try:
        session = authenticate()
        start_feed(session, interval=2.0)
        return jsonify({"status": "ok", "message": "Feed started"})
    except Exception as exc:
        log.error("Feed start failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/feed/stop", methods=["POST"])
def api_feed_stop():
    """Stop the live market data feed."""
    stop_feed()
    return jsonify({"status": "ok", "message": "Feed stopped"})


# ─── Portfolio APIs ────────────────────────────────────────────────────────────

@app.route("/api/portfolio")
def api_portfolio():
    """Return portfolio state (initialise if needed)."""
    if os.getenv("VERCEL"):
        from trading_bot.redis_sync import sync_portfolio_from_redis
        sync_portfolio_from_redis()
    p = get_portfolio()
    if not p:
        p = init_portfolio(config.PAPER_INITIAL_CAPITAL)
    return jsonify({
        "initial_capital": p["initial_capital"],
        "balance":         round(p["current_balance"], 2),
        "total_pnl":       round(p["total_pnl"], 2),
        "total_trades":    p["total_trades"],
        "wins":            p["wins"],
        "losses":          p["losses"],
        "win_rate":        round(p["wins"] / p["total_trades"] * 100, 1) if p["total_trades"] > 0 else 0,
    })


@app.route("/api/portfolio/reset", methods=["POST"])
def api_portfolio_reset():
    """Reset portfolio to initial capital."""
    p = reset_portfolio(config.PAPER_INITIAL_CAPITAL)
    return jsonify({"status": "ok", "balance": p["current_balance"]})


# ─── Paper Trading APIs ───────────────────────────────────────────────────────

@app.route("/api/paper/buy", methods=["POST"])
def api_paper_buy():
    """
    Place a paper BUY order.
    Body JSON:
        symbol, token      – option symbol & token (or defaults to NIFTY index)
        option_type        – "CE" / "PE" / "IDX"
        strike, expiry     – option details
        qty                – quantity (default LOT_SIZE)
        price              – custom entry price (0 or absent → use live LTP)
        stop_loss, target  – optional auto-exit levels
    """
    data = request.get_json(force=True) if request.is_json else {}

    symbol      = data.get("symbol", config.UNDERLYING)
    token       = data.get("token", config.NIFTY_TOKEN)
    option_type = data.get("option_type", "IDX")
    strike      = float(data.get("strike", 0))
    expiry      = data.get("expiry", "")
    qty         = int(data.get("qty", config.LOT_SIZE))
    custom_px   = float(data.get("price", 0))
    sl          = float(data.get("stop_loss", 0)) or None
    tgt         = float(data.get("target", 0)) or None
    source      = data.get("source", "MANUAL")  # MANUAL | AUTO | GPT

    # Enforce lot-size multiples for options
    if option_type in ("CE", "PE"):
        if qty < config.LOT_SIZE:
            qty = config.LOT_SIZE
        elif qty % config.LOT_SIZE != 0:
            qty = (qty // config.LOT_SIZE) * config.LOT_SIZE or config.LOT_SIZE

    # Determine entry price — use provided price first (avoids slow AngelOne call)
    if custom_px > 0:
        entry_price = custom_px
    elif token != config.NIFTY_TOKEN and token:
        ltps = fetch_option_ltp([token])
        entry_price = ltps.get(token, 0)
        if entry_price <= 0:
            tick = get_latest_tick()
            entry_price = tick.ltp
    else:
        tick = get_latest_tick()
        entry_price = tick.ltp

    if entry_price <= 0:
        return jsonify({"error": "No live price available. Market may be closed."}), 400

    now_str = now_ist().isoformat()
    trade_id = f"PT-{uuid.uuid4().hex[:8].upper()}"

    insert_order({
        "order_id":    trade_id,
        "symbol":      symbol,
        "token":       token,
        "exchange":    config.NFO_EXCHANGE if option_type in ("CE", "PE") else config.EXCHANGE,
        "side":        "BUY",
        "order_type":  "PAPER",
        "quantity":    qty,
        "price":       entry_price,
        "status":      "COMPLETE",
        "placed_at":   now_str,
        "completed_at": now_str,
        "remarks":     f"Paper {'limit' if custom_px > 0 else 'market'}",
    })
    insert_trade({
        "trade_id":       trade_id,
        "symbol":         symbol,
        "token":          token,
        "option_type":    option_type,
        "strike":         strike,
        "direction":      "LONG",
        "entry_price":    entry_price,
        "quantity":       qty,
        "entry_order_id": trade_id,
        "entry_time":     now_str,
        "status":         "OPEN",
        "stop_loss":      sl,
        "target":         tgt,
        "expiry":         expiry,
        "source":         source,
        "created_at":     now_str,
    })
    # Seed the "highest" watermark with entry price
    _update_highest(trade_id, entry_price)

    log.info("Paper BUY  %s  %s  qty=%d  px=%.2f  sl=%s  tgt=%s",
             trade_id, symbol, qty, entry_price, sl, tgt)
    return jsonify({
        "trade_id": trade_id, "side": "BUY", "symbol": symbol,
        "price": entry_price, "qty": qty, "stop_loss": sl, "target": tgt,
    })


@app.route("/api/paper/sell", methods=["POST"])
def api_paper_sell():
    """Place a paper SELL (SHORT) order — same body params as /buy."""
    data = request.get_json(force=True) if request.is_json else {}

    symbol      = data.get("symbol", config.UNDERLYING)
    token       = data.get("token", config.NIFTY_TOKEN)
    option_type = data.get("option_type", "IDX")
    strike      = float(data.get("strike", 0))
    expiry      = data.get("expiry", "")
    qty         = int(data.get("qty", config.LOT_SIZE))
    custom_px   = float(data.get("price", 0))
    sl          = float(data.get("stop_loss", 0)) or None
    tgt         = float(data.get("target", 0)) or None
    source      = data.get("source", "MANUAL")  # MANUAL | AUTO | GPT

    # Enforce lot-size multiples for options
    if option_type in ("CE", "PE"):
        if qty < config.LOT_SIZE:
            qty = config.LOT_SIZE
        elif qty % config.LOT_SIZE != 0:
            qty = (qty // config.LOT_SIZE) * config.LOT_SIZE or config.LOT_SIZE

    if custom_px > 0:
        entry_price = custom_px
    elif token != config.NIFTY_TOKEN and token:
        ltps = fetch_option_ltp([token])
        entry_price = ltps.get(token, 0)
        if entry_price <= 0:
            tick = get_latest_tick()
            entry_price = tick.ltp
    else:
        tick = get_latest_tick()
        entry_price = tick.ltp

    if entry_price <= 0:
        return jsonify({"error": "No live price available. Market may be closed."}), 400

    now_str = now_ist().isoformat()
    trade_id = f"PT-{uuid.uuid4().hex[:8].upper()}"

    insert_order({
        "order_id":    trade_id,
        "symbol":      symbol,
        "token":       token,
        "exchange":    config.NFO_EXCHANGE if option_type in ("CE", "PE") else config.EXCHANGE,
        "side":        "SELL",
        "order_type":  "PAPER",
        "quantity":    qty,
        "price":       entry_price,
        "status":      "COMPLETE",
        "placed_at":   now_str,
        "completed_at": now_str,
        "remarks":     f"Paper {'limit' if custom_px > 0 else 'market'}",
    })
    insert_trade({
        "trade_id":       trade_id,
        "symbol":         symbol,
        "token":          token,
        "option_type":    option_type,
        "strike":         strike,
        "direction":      "SHORT",
        "entry_price":    entry_price,
        "quantity":       qty,
        "entry_order_id": trade_id,
        "entry_time":     now_str,
        "status":         "OPEN",
        "stop_loss":      sl,
        "target":         tgt,
        "expiry":         expiry,
        "source":         source,
        "created_at":     now_str,
    })
    # Seed the "highest" watermark with entry price
    _update_highest(trade_id, entry_price)

    log.info("Paper SELL %s  %s  qty=%d  px=%.2f  sl=%s  tgt=%s",
             trade_id, symbol, qty, entry_price, sl, tgt)
    return jsonify({
        "trade_id": trade_id, "side": "SELL", "symbol": symbol,
        "price": entry_price, "qty": qty, "stop_loss": sl, "target": tgt,
    })


# ── Helper: update the "highest" (max premium) watermark in Redis ──────────
def _update_highest(trade_id: str, price: float):
    """Update the running highest-price watermark for a trade in Redis."""
    if price <= 0:
        return
    _key = f"autotrade:max_price:{trade_id}"
    try:
        from trading_bot.cache import _get_client as _rc
        r = _rc()
        if not r:
            return
        prev = r.get(_key)
        prev_f = float(prev if isinstance(prev, str) else prev.decode()) if prev else 0.0
        new_max = max(prev_f, price)
        r.set(_key, str(new_max), ex=86400)  # 1 day TTL
    except Exception:
        pass


# ── Helper: compute max premium reached during a trade's lifetime ──────────
def _get_max_price_reached(trade_id: str, token: str, entry_time_str: str,
                           entry_price: float, exit_price: float) -> float | None:
    """
    Return the peak option premium between entry and now.
    1. Check Redis watermark (fast, from autotrade monitor)
    2. Fallback: fetch 1-min option candles from AngelOne and compute max high
    """
    max_px = None

    # Step 1: try Redis
    try:
        from trading_bot.cache import _get_client as _rc
        r = _rc()
        if r:
            v = r.get(f"autotrade:max_price:{trade_id}")
            if v:
                max_px = float(v if isinstance(v, str) else v.decode())
    except Exception:
        pass

    # Step 2: fallback — fetch 1-min candles for the option token
    if max_px is None and token and token != config.NIFTY_TOKEN:
        try:
            from trading_bot.auth.login import get_session
            from trading_bot.data.historical import _fetch_raw
            session = get_session()
            # Parse entry time → "YYYY-MM-DD HH:MM" format for API
            if entry_time_str:
                entry_dt = datetime.datetime.fromisoformat(entry_time_str)
            else:
                entry_dt = now_ist().replace(hour=9, minute=15)
            from_str = entry_dt.strftime("%Y-%m-%d %H:%M")
            to_str = now_ist().strftime("%Y-%m-%d %H:%M")
            bars = _fetch_raw(session, config.EXCHANGE, token, "ONE_MINUTE",
                              from_str, to_str)
            if bars:
                # Each bar: [timestamp, open, high, low, close, volume]
                max_px = max(float(b[2]) for b in bars)
                log.info("Max price from candles for %s: %.2f (%d bars)", trade_id, max_px, len(bars))
        except Exception as e:
            log.warning("Failed to fetch candles for max_price %s: %s", trade_id, e)

    # Ensure max_px is at least the higher of entry/exit
    if max_px is not None:
        max_px = max(max_px, entry_price, exit_price)
    else:
        # Last resort: use max of entry and exit (we know it was at least entry)
        max_px = max(entry_price, exit_price)

    return max_px


@app.route("/api/paper/exit", methods=["POST"])
def api_paper_exit():
    """Exit (close) an open paper trade at current LTP or custom price."""
    data = request.get_json(force=True)
    trade_id = data.get("trade_id")
    if not trade_id:
        return jsonify({"error": "trade_id required"}), 400

    open_trades = get_open_trades()
    trade = None
    for t in open_trades:
        if t["trade_id"] == trade_id:
            trade = t
            break
    if not trade:
        return jsonify({"error": f"Trade {trade_id} not found or already closed."}), 404

    entry_price = trade["entry_price"]
    qty = trade["quantity"]
    direction = trade["direction"]
    token = trade["token"]
    exit_reason = data.get("reason", "MANUAL")

    # Get exit price: custom or live
    custom_exit = float(data.get("price", 0))
    if custom_exit > 0:
        exit_price = custom_exit
    elif token and token != config.NIFTY_TOKEN:
        ltps = fetch_option_ltp([token])
        exit_price = ltps.get(token, 0)
        if exit_price <= 0:
            exit_price = get_latest_tick().ltp
    else:
        exit_price = get_latest_tick().ltp

    if exit_price <= 0:
        return jsonify({"error": "No live price available."}), 400

    if direction == "LONG":
        pnl = (exit_price - entry_price) * qty
    else:
        pnl = (entry_price - exit_price) * qty
    pnl = round(pnl, 2)

    now_str = now_ist().isoformat()

    # Compute max premium reached during the trade lifetime
    max_px = _get_max_price_reached(trade_id, token, trade.get("entry_time", ""),
                                    entry_price, exit_price)

    actually_closed = close_trade(trade_id, exit_price, now_str, exit_reason, pnl,
                                  max_price_reached=max_px)
    if actually_closed:
        update_portfolio_after_trade(pnl, pnl >= 0)
        # Clean up Redis watermark key
        try:
            from trading_bot.cache import _get_client as _rc
            r = _rc()
            if r:
                r.delete(f"autotrade:max_price:{trade_id}")
        except Exception:
            pass
    log.info("Paper EXIT %s  exit=%.2f  pnl=%.2f  reason=%s", trade_id, exit_price, pnl, exit_reason)
    return jsonify({"trade_id": trade_id, "exit_price": exit_price, "pnl": pnl})


def _check_sl_target(positions_out: list[dict]) -> list[dict]:
    """
    Check each open position for SL / Target hit.
    Auto-close if triggered, and append a notification to the returned list.
    """
    auto_closed = []
    for p in list(positions_out):
        sl = p.get("stop_loss")
        tgt = p.get("target")
        ltp = p.get("ltp", 0)
        if ltp <= 0:
            continue

        triggered = None
        if sl and ltp <= sl:
            triggered = "SL"
        elif tgt and ltp >= tgt:
            triggered = "TARGET"

        if triggered:
            trade_id = p["trade_id"]
            entry = p["entry_price"]
            qty = p["qty"]
            pnl = round((ltp - entry) * qty, 2)
            now_str = now_ist().isoformat()
            token = p.get("token", "")
            max_px = _get_max_price_reached(trade_id, token,
                                            p.get("entry_time", ""), entry, ltp)
            actually_closed = close_trade(trade_id, ltp, now_str, triggered, pnl,
                                          max_price_reached=max_px)
            if actually_closed:
                update_portfolio_after_trade(pnl, pnl >= 0)
                log.info("Auto %s %s  exit=%.2f  pnl=%.2f", triggered, trade_id, ltp, pnl)
                try:
                    from trading_bot.cache import _get_client as _rc
                    r = _rc()
                    if r:
                        r.delete(f"autotrade:max_price:{trade_id}")
                except Exception:
                    pass
                auto_closed.append({
                    "trade_id": trade_id, "reason": triggered,
                    "exit_price": ltp, "pnl": pnl,
                })
    return auto_closed


@app.route("/api/paper/positions")
def api_paper_positions():
    """Return open positions with live P&L, market value, and SL/Target check."""
    # On Vercel, cron and dashboard are separate functions with separate /tmp.
    # Sync trades from Redis so this instance sees the latest state.
    if os.getenv("VERCEL"):
        from trading_bot.redis_sync import sync_trades_from_redis
        sync_trades_from_redis()

    market_closed = not _is_market_open()

    tick = get_latest_tick()
    nifty_ltp = tick.ltp
    # Only do a live fetch when market is open and we have no cached value
    if nifty_ltp <= 0 and not market_closed:
        tick, _ = fetch_live_once()
        nifty_ltp = tick.ltp

    open_trades = get_open_trades()

    # Batch-fetch live option LTPs only when market is open
    option_tokens = set()
    if not market_closed:
        for t in open_trades:
            tk = t["token"]
            if tk and tk != config.NIFTY_TOKEN:
                option_tokens.add(tk)

    option_ltps = {}
    if option_tokens:
        try:
            option_ltps = fetch_option_ltp(list(option_tokens))
        except Exception as e:
            log.warning("Option LTP fetch failed (using entry price fallback): %s", e)

    positions = []
    for t in open_trades:
        entry = t["entry_price"]
        qty = t["quantity"]
        direction = t["direction"]
        token = t["token"]

        # Get LTP for this specific instrument; fall back to entry price when
        # market is closed or live data is unavailable so positions show instantly.
        if token and token != config.NIFTY_TOKEN:
            ltp = option_ltps.get(token, 0)
            if ltp <= 0:
                ltp = entry  # fallback: entry price (0 P&L shown)
        else:
            ltp = nifty_ltp if nifty_ltp > 0 else entry

        if direction == "LONG":
            unrealized = (ltp - entry) * qty
        else:
            unrealized = (ltp - entry) * qty  # all trades are LONG (buy options)

        positions.append({
            "trade_id":    t["trade_id"],
            "symbol":      t["symbol"],
            "token":       token,
            "option_type": t["option_type"],
            "strike":      t["strike"],
            "expiry":      t["expiry"] or "",
            "direction":   direction,
            "entry_price": entry,
            "qty":         qty,
            "entry_time":  t["entry_time"],
            "ltp":         ltp,
            "market_value": round(ltp * qty, 2),
            "unrealized":  round(unrealized, 2),
            "stop_loss":   t["stop_loss"],
            "target":      t["target"],
            "source":      t.get("source") or "MANUAL",
        })

    # Update "highest" watermark for every open trade on each poll
    for p in positions:
        _update_highest(p["trade_id"], p["ltp"])

    # Auto-close positions hitting SL / Target
    auto_closed = _check_sl_target(positions)

    # Remove auto-closed from positions list
    closed_ids = {ac["trade_id"] for ac in auto_closed}
    positions = [p for p in positions if p["trade_id"] not in closed_ids]

    return jsonify({
        "positions": positions,
        "ltp": nifty_ltp,
        "auto_closed": auto_closed,
    })


@app.route("/api/paper/history")
def api_paper_history():
    """Return closed paper trades (most recent first). Optional ?source=MANUAL|AUTO|GPT filter."""
    if os.getenv("VERCEL"):
        from trading_bot.redis_sync import sync_trades_from_redis
        sync_trades_from_redis()

    source_filter = request.args.get("source", "").upper()
    if source_filter in ("MANUAL", "AUTO", "GPT"):
        sql = "SELECT * FROM trades WHERE status = 'CLOSED' AND (source = ? OR source IS NULL AND ? = 'MANUAL') ORDER BY exit_time DESC LIMIT 200"
        params = (source_filter, source_filter)
    else:
        sql = "SELECT * FROM trades WHERE status = 'CLOSED' ORDER BY exit_time DESC LIMIT 200"
        params = ()
    with get_cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
    has_source = "source" in cols
    trades = []
    for r in rows:
        src = "MANUAL"
        if has_source:
            try:
                src = r["source"] or "MANUAL"
            except (IndexError, KeyError):
                src = "MANUAL"
        trades.append({
            "trade_id":    r["trade_id"],
            "symbol":      r["symbol"],
            "option_type": r["option_type"],
            "strike":      r["strike"],
            "direction":   r["direction"],
            "entry_price": r["entry_price"],
            "exit_price":  r["exit_price"],
            "qty":         r["quantity"],
            "pnl":         r["pnl"],
            "entry_time":  r["entry_time"],
            "exit_time":   r["exit_time"],
            "exit_reason": r["exit_reason"],
            "source":      src,
            "target":      r["target"] if "target" in cols else None,
            "stop_loss":   r["stop_loss"] if "stop_loss" in cols else None,
            "max_price_reached": r["max_price_reached"] if "max_price_reached" in cols else None,
        })
    return jsonify({"trades": trades})


# ─── In-memory cache for opportunities (avoids rate-limiting) ─────────────────
_opp_cache: dict = {}          # {"data": ..., "ts": float}
_OPP_CACHE_TTL = 30            # seconds

# ─── Opportunities API ────────────────────────────────────────────────────────

@app.route("/api/opportunities")
def api_opportunities():
    """Analyse recent NIFTY 1-minute candles and return confirmed signals."""
    import time as _time

    # ── Return empty when market is closed ──
    if not _is_market_open():
        return jsonify({"signals": [], "market_closed": True, "candle_count": 0})

    # ── In-memory cache (30 s) — prevents rate-limit storms ──
    if _opp_cache.get("data") and (_time.time() - _opp_cache.get("ts", 0)) < _OPP_CACHE_TTL:
        return jsonify(_opp_cache["data"])

    # ── Redis cache (60 s) ──
    cached = get_cached("nifty:opportunities")
    if cached:
        return jsonify(cached)

    try:
        # Use shared candle cache to avoid rate-limit storms (AB1019)
        from trading_bot.candle_cache import get_candles as _get_shared_candles
        df, data_date, _ = _get_shared_candles("1m", 100)

        # Fallback: stored candles (populated by main.py when running locally)
        if df is None or len(df) == 0:
            db_rows = fetch_candles(config.UNDERLYING, "1m", limit=100)
            if db_rows:
                rows_raw = [dict(r) for r in db_rows]
                data_date = "DB cache"
                df = pd.DataFrame(rows_raw[-100:])
                for col in ("close", "open", "high", "low", "volume"):
                    df[col] = df[col].astype(float)

        if df is None or len(df) == 0:
            return jsonify({"signals": [], "error": "No candle data available"})

        signals = evaluate(df)
        result = [_sanitize(s.to_dict()) for s in signals]
        response = {
            "signals":      result,
            "candle_count": len(df),
            "data_date":    data_date,
        }
        set_cached("nifty:opportunities", response, ttl=60)
        _opp_cache["data"] = response
        _opp_cache["ts"] = _time.time()
        return jsonify(response)
    except Exception as e:
        log.error("api_opportunities error: %s", e)
        return jsonify({"signals": [], "error": str(e)})


# ─── Historical Analysis API ──────────────────────────────────────────────────

@app.route("/api/historical_analysis")
def api_historical_analysis():
    """
    Analyse NIFTY candle patterns across a date range.

    Query params:
        from      – YYYY-MM-DD  (required)
        to        – YYYY-MM-DD  (required)
        timeframe – 1m | 5m | 15m  (default: 5m)

    Fetches candles day-by-day from AngelOne via getCandleData, runs the
    strategy engine on each day, and returns all signals + per-day summary.
    Results are cached in Redis for 5 minutes (keyed by date range + tf).
    """
    import datetime as _dt
    from trading_bot.auth.login import get_session

    from_str = request.args.get("from", "")
    to_str   = request.args.get("to", "")
    tf       = request.args.get("timeframe", "5m")

    if not from_str or not to_str:
        return jsonify({"error": "Missing 'from' and 'to' query params"}), 400

    try:
        from_date = _dt.date.fromisoformat(from_str)
        to_date   = _dt.date.fromisoformat(to_str)
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    if from_date > to_date:
        return jsonify({"error": "'from' must be before 'to'"}), 400

    # Cap to 30 calendar days to stay within Vercel timeout
    max_days = 30
    if (to_date - from_date).days > max_days:
        return jsonify({"error": f"Date range too large. Max {max_days} days."}), 400

    # Map user timeframe to AngelOne interval string
    tf_map = {"1m": "ONE_MINUTE", "5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE"}
    interval = tf_map.get(tf)
    if not interval:
        return jsonify({"error": f"Invalid timeframe '{tf}'. Use 1m, 5m, or 15m."}), 400

    # ── Redis cache (5 min) ──
    cache_key = f"hist:{from_str}:{to_str}:{tf}"
    cached = get_cached(cache_key)
    if cached:
        return jsonify(cached)

    try:
        session = get_session()
        if not session:
            return jsonify({"error": "Could not authenticate with AngelOne"}), 500

        all_signals = []
        date_results = []
        total_candles = 0
        days_analysed = 0

        current = from_date
        while current <= to_date:
            # Skip weekends
            if current.weekday() >= 5:
                current += _dt.timedelta(days=1)
                continue

            days_analysed += 1
            d_str = current.strftime("%Y-%m-%d")
            params = {
                "exchange":    config.EXCHANGE,
                "symboltoken": config.NIFTY_TOKEN,
                "interval":    interval,
                "fromdate":    f"{d_str} 09:15",
                "todate":      f"{d_str} 15:30",
            }

            rows_raw = []
            try:
                resp = session.getCandleData(params)
                if resp and resp.get("status") is not False:
                    raw = resp.get("data") or []
                    for bar in raw:
                        if len(bar) >= 6:
                            rows_raw.append({
                                "timestamp": str(bar[0]),
                                "open":      float(bar[1]),
                                "high":      float(bar[2]),
                                "low":       float(bar[3]),
                                "close":     float(bar[4]),
                                "volume":    int(bar[5]),
                            })
            except Exception as exc:
                log.warning("historical fetch %s: %s", d_str, exc)

            day_signals = []
            if len(rows_raw) >= 20:   # need minimum bars for indicators
                total_candles += len(rows_raw)
                df = pd.DataFrame(rows_raw)
                for col in ("close", "open", "high", "low", "volume"):
                    df[col] = df[col].astype(float)
                try:
                    sigs = evaluate_historical(df)
                    for s in sigs:
                        sd = _sanitize(s.to_dict())
                        sd["date"] = d_str

                        # ── Simulate trade: walk forward from signal bar ──
                        if s.action == "ENTER" and s.bar_index >= 0 and s.entry_price > 0:
                            entry_px = s.entry_price
                            is_bull = (s.direction == "BULLISH")
                            tgt_px = entry_px + s.target_points if is_bull else entry_px - s.target_points
                            sl_px  = entry_px - s.sl_points     if is_bull else entry_px + s.sl_points
                            exit_px = entry_px
                            exit_ts = sd.get("bar_timestamp", "")
                            outcome = "OPEN"

                            for bi in range(s.bar_index + 1, len(df)):
                                bar_high  = float(df["high"].iloc[bi])
                                bar_low   = float(df["low"].iloc[bi])
                                bar_close = float(df["close"].iloc[bi])
                                bar_ts    = str(df["timestamp"].iloc[bi])

                                if is_bull:
                                    if bar_low <= sl_px:
                                        exit_px, exit_ts, outcome = sl_px, bar_ts, "SL_HIT"
                                        break
                                    if bar_high >= tgt_px:
                                        exit_px, exit_ts, outcome = tgt_px, bar_ts, "TARGET_HIT"
                                        break
                                else:
                                    if bar_high >= sl_px:
                                        exit_px, exit_ts, outcome = sl_px, bar_ts, "SL_HIT"
                                        break
                                    if bar_low <= tgt_px:
                                        exit_px, exit_ts, outcome = tgt_px, bar_ts, "TARGET_HIT"
                                        break

                                # EOD — last bar
                                if bi == len(df) - 1:
                                    exit_px, exit_ts, outcome = bar_close, bar_ts, "EOD_EXIT"

                            pnl = (exit_px - entry_px) if is_bull else (entry_px - exit_px)
                            sd["entry_price"]  = round(entry_px, 2)
                            sd["exit_price"]   = round(exit_px, 2)
                            sd["exit_time"]    = exit_ts
                            sd["target_price"] = round(tgt_px, 2)
                            sd["sl_price"]     = round(sl_px, 2)
                            sd["pnl_points"]   = round(pnl, 2)
                            sd["outcome"]      = outcome

                        day_signals.append(sd)
                        all_signals.append(sd)
                except Exception as exc:
                    log.warning("evaluate %s: %s", d_str, exc)

            enter_cnt = sum(1 for s in day_signals if s.get("action") == "ENTER")
            date_results.append({
                "date":         d_str,
                "candle_count": len(rows_raw),
                "signal_count": len(day_signals),
                "enter_count":  enter_cnt,
            })

            current += _dt.timedelta(days=1)

        # ── Aggregate P&L for ENTER trades ──
        total_pnl = sum(s.get("pnl_points", 0) for s in all_signals if s.get("outcome"))
        wins = sum(1 for s in all_signals if s.get("outcome") == "TARGET_HIT")
        losses = sum(1 for s in all_signals if s.get("outcome") == "SL_HIT")
        trades_taken = sum(1 for s in all_signals if s.get("outcome"))

        response = {
            "signals":       all_signals,
            "date_results":  date_results,
            "from_date":     from_str,
            "to_date":       to_str,
            "timeframe":     tf,
            "days_analysed": days_analysed,
            "candle_count":  total_candles,
            "total_pnl":     round(total_pnl, 2),
            "wins":          wins,
            "losses":        losses,
            "trades_taken":  trades_taken,
        }
        set_cached(cache_key, response, ttl=300)
        return jsonify(response)

    except Exception as e:
        log.error("historical_analysis error: %s", e)
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
#  UNIFIED BACKTEST API
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    """Run backtest for 180 Rule, ORB, or Scalping strategy over a date range.

    JSON body:
        strategy  – "180rule" | "orb" | "scalping"
        from_date – YYYY-MM-DD
        to_date   – YYYY-MM-DD
    """
    import datetime as _dt
    from trading_bot.auth.login import get_session

    body = request.get_json(silent=True) or {}
    strategy = body.get("strategy", "")
    from_str = body.get("from_date", "")
    to_str   = body.get("to_date", "")

    if not strategy or not from_str or not to_str:
        return jsonify({"error": "Missing strategy, from_date, or to_date"}), 400

    if strategy not in ("180rule", "orb", "scalping"):
        return jsonify({"error": "Invalid strategy. Use: 180rule, orb, scalping"}), 400

    try:
        from_date = _dt.date.fromisoformat(from_str)
        to_date   = _dt.date.fromisoformat(to_str)
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    if from_date > to_date:
        return jsonify({"error": "'from' must be <= 'to'"}), 400
    if (to_date - from_date).days > 30:
        return jsonify({"error": "Max 30 days per backtest"}), 400

    # Redis cache
    cache_key = f"bt:{strategy}:{from_str}:{to_str}"
    cached = get_cached(cache_key)
    if cached:
        return jsonify(cached)

    try:
        session = get_session()
        if not session:
            return jsonify({"error": "Could not authenticate with AngelOne"}), 500

        all_trades = []
        day_summaries = []
        days_tested = 0

        # Choose interval based on strategy
        if strategy == "180rule":
            interval = "FIVE_MINUTE"
        elif strategy == "orb":
            interval = "ONE_MINUTE"
        else:  # scalping
            interval = "FIVE_MINUTE"

        current = from_date
        while current <= to_date:
            if current.weekday() >= 5:
                current += _dt.timedelta(days=1)
                continue

            days_tested += 1
            d_str = current.strftime("%Y-%m-%d")
            params = {
                "exchange":    config.EXCHANGE,
                "symboltoken": config.NIFTY_TOKEN,
                "interval":    interval,
                "fromdate":    f"{d_str} 09:15",
                "todate":      f"{d_str} 15:30",
            }

            rows_raw = []
            try:
                resp = session.getCandleData(params)
                if resp and resp.get("status") is not False:
                    raw = resp.get("data") or []
                    for bar in raw:
                        if len(bar) >= 6:
                            rows_raw.append({
                                "timestamp": str(bar[0]),
                                "open":      float(bar[1]),
                                "high":      float(bar[2]),
                                "low":       float(bar[3]),
                                "close":     float(bar[4]),
                                "volume":    int(bar[5]),
                            })
            except Exception as exc:
                log.warning("backtest fetch %s %s: %s", strategy, d_str, exc)

            if len(rows_raw) < 20:
                day_summaries.append({"date": d_str, "trades": 0, "pnl": 0})
                current += _dt.timedelta(days=1)
                continue

            df = pd.DataFrame(rows_raw)
            for col in ("close", "open", "high", "low", "volume"):
                df[col] = df[col].astype(float)

            day_trades = []

            if strategy == "180rule":
                from trading_bot.reversal180.backtest import run_backtest as bt_180
                from trading_bot.reversal180.config import Reversal180Config
                result = bt_180(df, Reversal180Config())
                for t in result.get("trades", []):
                    day_trades.append({
                        "date": d_str,
                        "entry_time": t.get("entry_ts", ""),
                        "exit_time": t.get("exit_ts", ""),
                        "side": t.get("side", ""),
                        "entry": t.get("entry", 0),
                        "exit": t.get("exit", 0),
                        "sl": t.get("sl", 0),
                        "target": t.get("tg", 0),
                        "pnl": t.get("pnl", 0),
                        "reason": t.get("reason", ""),
                    })

            elif strategy == "orb":
                from trading_bot.orb_strategy.strategy_orb import backtest_orb
                from trading_bot.orb_strategy.config import ORBConfig
                result = backtest_orb(df, ORBConfig())
                for t in result.get("trades", []):
                    day_trades.append({
                        "date": d_str,
                        "entry_time": t.get("ts", ""),
                        "exit_time": "",
                        "side": t.get("side", ""),
                        "entry": t.get("entry", 0),
                        "exit": t.get("exit", 0),
                        "sl": t.get("sl", 0),
                        "target": t.get("tg", 0),
                        "pnl": t.get("pnl", 0),
                        "reason": t.get("reason", ""),
                    })

            else:  # scalping — use existing historical_analysis engine
                try:
                    sigs = evaluate_historical(df)
                    for s in sigs:
                        if s.action != "ENTER" or s.bar_index < 0 or s.entry_price <= 0:
                            continue
                        entry_px = s.entry_price
                        is_bull = (s.direction == "BULLISH")
                        tgt_px = entry_px + s.target_points if is_bull else entry_px - s.target_points
                        sl_px  = entry_px - s.sl_points     if is_bull else entry_px + s.sl_points
                        exit_px = entry_px
                        exit_ts = ""
                        outcome = "OPEN"

                        for bi in range(s.bar_index + 1, len(df)):
                            bar_high  = float(df["high"].iloc[bi])
                            bar_low   = float(df["low"].iloc[bi])
                            bar_close = float(df["close"].iloc[bi])
                            bar_ts    = str(df["timestamp"].iloc[bi])

                            if is_bull:
                                if bar_low <= sl_px:
                                    exit_px, exit_ts, outcome = sl_px, bar_ts, "SL_HIT"
                                    break
                                if bar_high >= tgt_px:
                                    exit_px, exit_ts, outcome = tgt_px, bar_ts, "TARGET_HIT"
                                    break
                            else:
                                if bar_high >= sl_px:
                                    exit_px, exit_ts, outcome = sl_px, bar_ts, "SL_HIT"
                                    break
                                if bar_low <= tgt_px:
                                    exit_px, exit_ts, outcome = tgt_px, bar_ts, "TARGET_HIT"
                                    break
                            if bi == len(df) - 1:
                                exit_px, exit_ts, outcome = bar_close, bar_ts, "EOD_EXIT"

                        pnl = (exit_px - entry_px) if is_bull else (entry_px - exit_px)
                        day_trades.append({
                            "date": d_str,
                            "entry_time": s.bar_timestamp if hasattr(s, "bar_timestamp") else "",
                            "exit_time": exit_ts,
                            "side": "BUY_CE" if is_bull else "BUY_PE",
                            "entry": round(entry_px, 2),
                            "exit": round(exit_px, 2),
                            "sl": round(sl_px, 2),
                            "target": round(tgt_px, 2),
                            "pnl": round(pnl, 2),
                            "reason": outcome,
                        })
                except Exception as exc:
                    log.warning("scalping bt %s: %s", d_str, exc)

            day_pnl = round(sum(t["pnl"] for t in day_trades), 2)
            day_wins = sum(1 for t in day_trades if t["pnl"] > 0)
            day_losses = len(day_trades) - day_wins
            day_summaries.append({
                "date": d_str,
                "trades": len(day_trades),
                "wins": day_wins,
                "losses": day_losses,
                "pnl": day_pnl,
            })
            all_trades.extend(day_trades)
            current += _dt.timedelta(days=1)

        total_pnl = round(sum(t["pnl"] for t in all_trades), 2)
        wins = sum(1 for t in all_trades if t["pnl"] > 0)
        losses = len(all_trades) - wins
        win_rate = round(wins / len(all_trades) * 100, 1) if all_trades else 0

        response = {
            "strategy":    strategy,
            "from_date":   from_str,
            "to_date":     to_str,
            "days_tested": days_tested,
            "total_trades": len(all_trades),
            "wins":        wins,
            "losses":      losses,
            "win_rate":    win_rate,
            "total_pnl":   total_pnl,
            "trades":      all_trades,
            "day_summaries": day_summaries,
        }
        set_cached(cache_key, response, ttl=300)
        return jsonify(response)

    except Exception as e:
        log.error("backtest error: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ─── LLM Trade Analysis API ────────────────────────────────────────────────────

@app.route("/api/llm/analyze-trade")
def api_llm_analyze_trade():
    """
    POST-MORTEM LLM analysis of a completed paper trade.

    Fetches NIFTY candles for the trade date, re-runs the strategy engine
    at the entry bar, then asks GPT to explain why the trade succeeded or
    failed and what could have been done differently.

    Query params:
        trade_id – e.g. PT-ABCD1234
        timeframe – 1m | 5m | 15m (default: 5m)
    """
    import datetime as _dt
    from trading_bot.auth.login import get_session
    from trading_bot.llm.analyzer import analyze_failed_trade

    trade_id = request.args.get("trade_id", "").strip()
    tf = request.args.get("timeframe", "5m")

    if not trade_id:
        return jsonify({"error": "Missing trade_id query param"}), 400

    # ── Redis cache (1 hour) — LLM calls are expensive ──
    cache_key = f"llm:trade:{trade_id}:{tf}"
    cached = get_cached(cache_key)
    if cached:
        return jsonify(cached)

    # ── Step 1: Look up the trade ──────────────────────────────────────────
    trade = None
    with get_cursor() as cur:
        cur.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,))
        row = cur.fetchone()
        if row:
            trade = dict(row)

    # Fallback to Redis if not in local DB (Vercel cold start)
    if not trade:
        try:
            from trading_bot.redis_sync import get_all_trades_from_redis
            all_trades = get_all_trades_from_redis()
            for t in all_trades:
                if t.get("trade_id") == trade_id:
                    trade = t
                    break
        except Exception:
            pass

    if not trade:
        return jsonify({"error": f"Trade {trade_id} not found"}), 404

    # ── Step 2: Determine trade date ──────────────────────────────────────
    entry_time_str = str(trade.get("entry_time", ""))
    if not entry_time_str:
        return jsonify({"error": "Trade has no entry_time"}), 400

    try:
        entry_dt = _dt.datetime.fromisoformat(entry_time_str)
    except ValueError:
        return jsonify({"error": f"Cannot parse entry_time: {entry_time_str}"}), 400

    trade_date = entry_dt.date()
    d_str = trade_date.strftime("%Y-%m-%d")

    # ── Step 3: Fetch NIFTY candles for that date ─────────────────────────
    # Try store (locally run bot) first; fall back to AngelOne API
    candle_rows = fetch_candles_by_date(config.UNDERLYING, tf, d_str)
    candles_raw = []

    if candle_rows:
        # From stored DB rows
        for r in candle_rows:
            parts = _ts_to_ist_parts(r["timestamp"])
            candles_raw.append({
                "timestamp": r["timestamp"],
                "ist":   parts["ist"],
                "open":  float(r["open"]),
                "high":  float(r["high"]),
                "low":   float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r["volume"]),
            })
    else:
        # Live fetch from AngelOne
        tf_map = {"1m": "ONE_MINUTE", "5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE"}
        interval = tf_map.get(tf, "FIVE_MINUTE")
        try:
            session = get_session()
            if session:
                params = {
                    "exchange":    config.EXCHANGE,
                    "symboltoken": config.NIFTY_TOKEN,
                    "interval":    interval,
                    "fromdate":    f"{d_str} 09:15",
                    "todate":      f"{d_str} 15:30",
                }
                resp = session.getCandleData(params)
                if resp and resp.get("status") is not False:
                    for bar in (resp.get("data") or []):
                        if len(bar) >= 6:
                            parts = _ts_to_ist_parts(str(bar[0]))
                            candles_raw.append({
                                "timestamp": str(bar[0]),
                                "ist":   parts["ist"],
                                "open":  float(bar[1]),
                                "high":  float(bar[2]),
                                "low":   float(bar[3]),
                                "close": float(bar[4]),
                                "volume": int(bar[5]),
                            })
        except Exception as exc:
            log.warning("LLM analyze-trade candle fetch: %s", exc)

    if len(candles_raw) < 10:
        return jsonify({
            "error": f"Insufficient candle data for {d_str} ({len(candles_raw)} bars found). "
                     "The data may not be stored for this date."
        }), 400

    # ── Step 4: Find entry bar index ──────────────────────────────────────
    # Find the candle bar whose timestamp is closest to (and not after) entry_time
    entry_epoch = entry_dt.timestamp()

    def _bar_epoch(c):
        try:
            return _dt.datetime.fromisoformat(c["timestamp"]).timestamp()
        except Exception:
            return 0.0

    entry_bar_index = 0
    min_diff = float("inf")
    for i, c in enumerate(candles_raw):
        be = _bar_epoch(c)
        diff = abs(be - entry_epoch)
        # prefer the bar just before or at entry
        if be <= entry_epoch and diff < min_diff:
            min_diff = diff
            entry_bar_index = i

    # ── Step 5: Run strategy evaluation up to entry bar ──────────────────
    strategy_eval = None
    if entry_bar_index >= 5:
        try:
            eval_candles = candles_raw[: entry_bar_index + 2]  # +2 for confirmation bar
            df_eval = pd.DataFrame(eval_candles)
            for col in ("open", "high", "low", "close", "volume"):
                df_eval[col] = pd.to_numeric(df_eval[col], errors="coerce")
            from trading_bot.strategy import evaluate_historical
            sigs = evaluate_historical(df_eval)
            if sigs:
                # Take the signal closest to the entry bar
                best = min(sigs, key=lambda s: abs(s.bar_index - entry_bar_index))
                strategy_eval = _sanitize(best.to_dict())
        except Exception as exc:
            log.warning("strategy eval for trade analysis: %s", exc)

    # ── Step 6: Call LLM ──────────────────────────────────────────────────
    model = config.OPENAI_MODEL if hasattr(config, "OPENAI_MODEL") else "gpt-4o-mini"
    result = analyze_failed_trade(
        trade=trade,
        candles=candles_raw,
        entry_bar_index=entry_bar_index,
        strategy_eval=strategy_eval,
        timeframe=tf,
        model=model,
    )

    response = {
        "trade_id":         trade_id,
        "trade_date":       d_str,
        "entry_time":       entry_time_str,
        "entry_bar_index":  entry_bar_index,
        "candles_fetched":  len(candles_raw),
        "strategy_eval":    strategy_eval,
        "analysis":         result.get("analysis", ""),
        "suggestions":      result.get("suggestions", []),
        "model":            result.get("model", model),
        "error":            result.get("error"),
    }

    if not result.get("error"):
        set_cached(cache_key, response, ttl=3600)  # cache 1 hour
    elif result.get("error") == "RATE_LIMIT":
        # Cache rate-limit result for 30s so rapid re-clicks don't hammer OpenAI
        response["error"] = "RATE_LIMIT"
        set_cached(cache_key, response, ttl=30)

    return jsonify(response)


# ─── LLM Apply Suggestion API ─────────────────────────────────────────────────

# Safe parameters and their allowed value ranges (min, max)
_SAFE_PARAMS: dict[str, tuple] = {
    "RSI_BULL_THRESHOLD":        (40.0, 75.0),
    "RSI_BEAR_THRESHOLD":        (25.0, 60.0),
    "DUPLICATE_SIGNAL_COOLDOWN": (300.0, 3600.0),
    "SL_BLOCK_DURATION":         (300.0, 7200.0),
    "MAX_OPEN_TRADES":           (1.0,  5.0),
    "MAX_DAILY_LOSS":            (500.0, 10000.0),
    "VOLUME_EXPANSION_MULT":     (1.0,  3.0),
    "INITIAL_SL_POINTS":         (5.0,  60.0),
}


# ─── AI Chat Page + API ───────────────────────────────────────────────────────

@app.route("/chat")
def page_chat():
    return render_template("chat.html")


_CHAT_SYSTEM = """You are the AI assistant for "Project Candles" — an automated NIFTY 50 options paper-trading bot.

## CURRENT LIVE CONFIG
- RSI_BULL_THRESHOLD     = {RSI_BULL}   (RSI must be above this to enter a CE/bullish trade)
- RSI_BEAR_THRESHOLD     = {RSI_BEAR}   (RSI must be below this to enter a PE/bearish trade)
- VOLUME_EXPANSION_MULT  = {VOL}        (volume must be {VOL}× its average to confirm pattern)
- INITIAL_SL_POINTS      = {SL}         (stop-loss distance in NIFTY index points)
- DUPLICATE_SIGNAL_COOLDOWN = {COOL}s   (minimum gap before same-direction re-entry)
- SL_BLOCK_DURATION      = {SLBLK}s    (all entries blocked this long after a stop-loss hit)
- MAX_OPEN_TRADES        = {MAXOT}      (max concurrent positions)
- MAX_DAILY_LOSS         = ₹{MDL}       (daily loss limit — bot stops after this)
- EMA_FAST=20 / EMA_SLOW=50, SUPERTREND(10, 3.0), LOT_SIZE=65, TRADING_MODE=paper

## PARAMETERS YOU CAN MODIFY
Only these 8, within the stated safe ranges:
RSI_BULL_THRESHOLD (40-75), RSI_BEAR_THRESHOLD (25-60),
DUPLICATE_SIGNAL_COOLDOWN (300-3600s), SL_BLOCK_DURATION (300-7200s),
MAX_OPEN_TRADES (1-5), MAX_DAILY_LOSS (500-10000),
VOLUME_EXPANSION_MULT (1.0-3.0), INITIAL_SL_POINTS (5-60)

## STRICT INTRADAY ANALYSIS MODE (ORB + VWAP + MA + PREMIUM)
When user asks for market analysis or trade decision, behave as an expert intraday trader and risk manager.

Non-negotiable principles:
- Rule-based only. No assumptions or predictions.
- Capital protection over trading frequency.
- React to price action, do not forecast.
- One trade per day maximum.
- Follow VWAP direction strictly.

### 1) PRE-MARKET FILTER (09:15-09:25)
Required checks:
- Opening range >= 45 points
- Strong candle bodies (avoid long wicks)
- Clear direction (not sideways)
- No random CE/PE premium spikes
- Avoid gap-up near resistance

Output fields:
- Trade decision: TRADE / NO TRADE
- Direction bias: BULLISH / BEARISH / SIDEWAYS
- Reason: strict and rule-based

### 2) ENTRY CONFIRMATION (09:25-09:35)
Required checks:
- Strong breakout candle (large body, small wicks)
- Structure confirmation: HH/HL for bullish, LH/LL for bearish
- Reject fake breakouts (no follow-through)
- Trade only in VWAP direction

Output fields:
- Entry: BUY CE / BUY PE / NO TRADE
- Confidence: HIGH / MEDIUM / LOW
- Reason: max 2 lines

### 3) 180 PREMIUM BREAKOUT STRATEGY
Rules:
- Active only after 09:25
- BUY CE only if bullish and above VWAP
- BUY PE only if bearish and below VWAP
- Premium must cross 180 with sustained momentum (not one-candle spike)

Output fields:
- Action: BUY CE / BUY PE / NO TRADE
- Target: 30-40 points
- Stop loss: 20 points
- Short reason

### 4) NO-TRADE DETECTION (CAPITAL PROTECTION)
If any of these are true, bias strongly to NO TRADE:
- Range < 45 points
- Multiple wicks / indecision candles
- VWAP frequent back-and-forth touches
- No HH/HL or LH/LL structure

Output fields:
- Decision: NO TRADE / TRADE OK
- Strict reason

### 5) EDGE DETECTION (ADVANCED)
Look for confluence:
- Candle compression (4-8 small candles)
- Liquidity sweep / fake move
- Strong reclaim of 20 MA
- First strong breakout candle
- Volume expansion

Output fields:
- Setup detected: YES / NO
- Direction
- Entry timing suggestion

### 6) POST-TRADE ANALYSIS
Always evaluate:
1. Was trend clear?
2. Was entry correct or early/late?
3. Any rule violations?
4. Was trade avoidable?

Output fields:
- Mistake (if any)
- Improvement for next trade
- Confidence score: 0-100

### GLOBAL TRADE GATES
At least 4 conditions must align before TRADE:
- Trend + Structure + Momentum + Support/Resistance

Avoid trading when:
- 09:15-09:20 opening volatility
- Sideways market
- Flat moving averages
- After large spikes

If user input misses required variables, do NOT assume values.
Ask for only missing fields, then evaluate.

### FINAL OUTPUT FORMAT (STRICT)
Always end decision with:
- Final decision: TRADE / NO TRADE
- If TRADE:
    - Direction: CE / PE
    - Entry timing
    - Confidence
- If NO TRADE:
    - Clear reason

## ACTION FORMAT
When you want to apply a config change, append EXACTLY this line at the end — nothing after it:
%%ACTION:config_change:PARAM_NAME:NEW_VALUE:one-sentence reason%%
Example: %%ACTION:config_change:INITIAL_SL_POINTS:25:Wider SL will reduce stop-outs from normal intraday noise%%
Include at most ONE action. Only suggest a change when the user explicitly asks or when a benefit is clear.

## STYLE
- Be concise (this is a trading terminal, not a blog)
- Use actual numbers from the config above when helpful
- If asked for code changes beyond the 8 parameters, explain you can only modify those 8
- ₹ for rupees, answer in plain English"""


def _norm_text(v) -> str:
    if v is None:
        return ""
    return str(v).strip().lower()


def _is_strong_candle_desc(desc: str) -> bool:
    d = _norm_text(desc)
    if not d:
        return False
    weak_terms = ["long wick", "wicks", "doji", "indecision", "choppy"]
    if any(t in d for t in weak_terms):
        return False
    return True


def _has_clear_direction(pre_vwap: str, trend: str, candle_desc: str) -> bool:
    v = _norm_text(pre_vwap)
    t = _norm_text(trend)
    c = _norm_text(candle_desc)
    if "sideways" in t or "sideways" in c:
        return False
    if "near" in v:
        return False
    if any(x in t for x in ["flat", "mixed", "no trend"]):
        return False
    return True


def _direction_bias(pre_vwap: str, trend: str, candle_desc: str) -> str:
    v = _norm_text(pre_vwap)
    t = _norm_text(trend)
    c = _norm_text(candle_desc)
    bull_hits = sum([
        "above" in v,
        "bull" in t or "up" in t,
        "bull" in c,
    ])
    bear_hits = sum([
        "below" in v,
        "bear" in t or "down" in t,
        "bear" in c,
    ])
    if bull_hits > bear_hits and bull_hits >= 1:
        return "BULLISH"
    if bear_hits > bull_hits and bear_hits >= 1:
        return "BEARISH"
    return "SIDEWAYS"


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _contains_any(txt: str, keys: list[str]) -> bool:
    t = _norm_text(txt)
    return any(k in t for k in keys)


@app.route("/api/rule-analysis", methods=["POST"])
def api_rule_analysis():
    """
    Deterministic, strict intraday rule engine.

    Request JSON supports these sections:
      pre_market, entry_confirmation, premium_180, no_trade, edge, post_trade
    """
    body = request.get_json(force=True) or {}

    pre = body.get("pre_market") or {}
    ent = body.get("entry_confirmation") or {}
    p180 = body.get("premium_180") or {}
    nt = body.get("no_trade") or {}
    edge = body.get("edge") or {}
    post = body.get("post_trade") or {}

    # ── 1) PRE-MARKET FILTER ─────────────────────────────────────────────
    range_points = _safe_float(pre.get("range_points"))
    candle_desc = str(pre.get("candle_description", ""))
    pre_vwap = str(pre.get("vwap_position", ""))
    trend = str(pre.get("trend", ""))
    premiums_state = str(pre.get("option_premiums", ""))
    gap_sr = str(pre.get("gap_sr_details", ""))

    pm_checks = {
        "range_ok": range_points >= 45,
        "candle_quality_ok": _is_strong_candle_desc(candle_desc),
        "direction_clear_ok": _has_clear_direction(pre_vwap, trend, candle_desc),
        "premium_stability_ok": not _contains_any(premiums_state, ["spiky", "random spike", "unstable"]),
        "gap_resistance_ok": not (_contains_any(gap_sr, ["gap up", "gap-up"]) and _contains_any(gap_sr, ["resistance", "near resistance"])),
    }
    pm_bias = _direction_bias(pre_vwap, trend, candle_desc)
    pm_trade_decision = "TRADE" if all(pm_checks.values()) else "NO TRADE"
    pm_failed = [k for k, v in pm_checks.items() if not v]

    # ── 2) ENTRY CONFIRMATION ────────────────────────────────────────────
    current_price = _safe_float(ent.get("current_price"))
    breakout_level = _safe_float(ent.get("breakout_level"))
    ent_vwap = str(ent.get("vwap_position", ""))
    mkt_structure = str(ent.get("market_structure", ""))
    latest_candles = str(ent.get("latest_candles", ""))
    volume_state = str(ent.get("volume", ""))

    bullish_structure = _contains_any(mkt_structure, ["hh/hl", "hh hl", "higher high", "higher low"])
    bearish_structure = _contains_any(mkt_structure, ["lh/ll", "lh ll", "lower high", "lower low"])
    strong_breakout_candle = _contains_any(latest_candles, ["strong", "large body", "marubozu", "breakout"]) and not _contains_any(latest_candles, ["doji", "long wick", "weak"])
    follow_through_ok = not _contains_any(latest_candles, ["fake breakout", "no follow", "rejected immediately"])
    volume_ok = _contains_any(volume_state, ["increasing", "expanding", "high", "surge"])

    entry_action = "NO TRADE"
    entry_reason = []
    if strong_breakout_candle and follow_through_ok and volume_ok:
        if bullish_structure and _contains_any(ent_vwap, ["above"]) and current_price > breakout_level:
            entry_action = "BUY CE"
        elif bearish_structure and _contains_any(ent_vwap, ["below"]) and current_price < breakout_level:
            entry_action = "BUY PE"

    if entry_action == "NO TRADE":
        if not strong_breakout_candle:
            entry_reason.append("breakout candle not strong")
        if not follow_through_ok:
            entry_reason.append("fake breakout / no follow-through")
        if not volume_ok:
            entry_reason.append("volume not supportive")
        if bullish_structure is False and bearish_structure is False:
            entry_reason.append("structure not HH/HL or LH/LL")

    entry_score = sum([
        1 if strong_breakout_candle else 0,
        1 if follow_through_ok else 0,
        1 if volume_ok else 0,
        1 if (bullish_structure or bearish_structure) else 0,
        1 if ((_contains_any(ent_vwap, ["above"]) and entry_action == "BUY CE") or (_contains_any(ent_vwap, ["below"]) and entry_action == "BUY PE")) else 0,
    ])
    if entry_score >= 5:
        entry_conf = "HIGH"
    elif entry_score >= 3:
        entry_conf = "MEDIUM"
    else:
        entry_conf = "LOW"

    # ── 3) 180 PREMIUM BREAKOUT ──────────────────────────────────────────
    ce_prem = _safe_float(p180.get("ce_premium"))
    pe_prem = _safe_float(p180.get("pe_premium"))
    nifty_dir = str(p180.get("nifty_direction", ""))
    p180_vwap = str(p180.get("vwap_position", ""))
    p180_momentum = str(p180.get("momentum", ""))
    analysis_time = str(p180.get("time", body.get("time", "")))

    after_925 = _contains_any(analysis_time, ["09:25", "09:26", "09:27", "09:28", "09:29", "09:3", "09:4", "09:5", "10:", "11:", "12:", "13:", "14:", "15:"])
    action_180 = "NO TRADE"
    reason_180 = "conditions not aligned"
    if after_925:
        if _contains_any(nifty_dir, ["bull"]) and _contains_any(p180_vwap, ["above"]) and _contains_any(p180_momentum, ["strong"]) and ce_prem >= 180:
            action_180 = "BUY CE"
            reason_180 = "bullish direction, above VWAP, CE premium crossed 180 with momentum"
        elif _contains_any(nifty_dir, ["bear"]) and _contains_any(p180_vwap, ["below"]) and _contains_any(p180_momentum, ["strong"]) and pe_prem >= 180:
            action_180 = "BUY PE"
            reason_180 = "bearish direction, below VWAP, PE premium crossed 180 with momentum"
        else:
            reason_180 = "180 premium crossover lacks full directional confirmation"
    else:
        reason_180 = "trade allowed only after 09:25"

    # ── 4) NO-TRADE DETECTION ────────────────────────────────────────────
    nt_range = _safe_float(nt.get("range_points", range_points))
    nt_candle = str(nt.get("candle_structure", candle_desc))
    nt_vwap = str(nt.get("vwap_behavior", pre_vwap))
    nt_price_action = str(nt.get("price_action", ""))
    nt_structure = str(nt.get("market_structure", mkt_structure))

    no_trade_hits = {
        "range_lt_45": nt_range < 45,
        "indecision_wicks": _contains_any(nt_candle, ["wicks", "indecision", "choppy", "doji"]),
        "vwap_frequent_touches": _contains_any(nt_vwap, ["frequent", "repeated", "back and forth", "touches"]),
        "no_clear_structure": not (_contains_any(nt_structure, ["hh/hl", "higher high", "higher low"]) or _contains_any(nt_structure, ["lh/ll", "lower high", "lower low"])),
        "sideways_or_volatile": _contains_any(nt_price_action, ["sideways", "volatile", "whipsaw"]),
    }
    no_trade_triggered = any(no_trade_hits.values())
    no_trade_decision = "NO TRADE" if no_trade_triggered else "TRADE OK"

    # ── 5) EDGE DETECTION ────────────────────────────────────────────────
    compression = str(edge.get("compression", ""))
    sweep = str(edge.get("liquidity_sweep", ""))
    reclaim20 = str(edge.get("reclaim_20ma", ""))
    first_break = str(edge.get("first_breakout_candle", ""))
    vol_exp = str(edge.get("volume_expansion", ""))
    edge_dir = str(edge.get("direction", pm_bias))

    edge_checks = {
        "compression_4_8": _contains_any(compression, ["yes", "true", "4", "5", "6", "7", "8", "small candles"]),
        "liquidity_sweep": _contains_any(sweep, ["yes", "true", "fake", "sweep"]),
        "reclaim_20ma": _contains_any(reclaim20, ["yes", "true", "reclaim", "strong"]),
        "first_breakout": _contains_any(first_break, ["yes", "true", "strong", "breakout"]),
        "volume_expansion": _contains_any(vol_exp, ["yes", "true", "expansion", "increasing", "surge"]),
    }
    edge_score = sum(1 for v in edge_checks.values() if v)
    edge_detected = "YES" if edge_score >= 4 else "NO"
    edge_timing = "Enter on first retest after breakout close" if edge_detected == "YES" else "No advanced edge setup"

    # ── Global alignment + final decision ────────────────────────────────
    trend_aligned = pm_bias in ("BULLISH", "BEARISH")
    structure_aligned = entry_action in ("BUY CE", "BUY PE")
    momentum_aligned = volume_ok and _contains_any(str(p180_momentum), ["strong"]) and action_180 in ("BUY CE", "BUY PE")
    sr_aligned = pm_checks["gap_resistance_ok"] and pm_checks["direction_clear_ok"]
    alignment_count = sum([1 if trend_aligned else 0, 1 if structure_aligned else 0, 1 if momentum_aligned else 0, 1 if sr_aligned else 0])

    final_decision = "NO TRADE"
    final_direction = ""
    final_reason = "capital protection filters not satisfied"

    if no_trade_triggered or pm_trade_decision == "NO TRADE":
        final_decision = "NO TRADE"
        final_reason = "pre-market/no-trade filters failed"
    elif alignment_count >= 4 and entry_action in ("BUY CE", "BUY PE"):
        final_decision = "TRADE"
        final_direction = "CE" if entry_action == "BUY CE" else "PE"
        final_reason = "trend + structure + momentum + S/R aligned"
    else:
        final_decision = "NO TRADE"
        final_reason = "fewer than 4 core conditions aligned"

    # ── 6) POST-TRADE ANALYSIS ────────────────────────────────────────────
    post_entry = _safe_float(post.get("entry_price"))
    post_exit = _safe_float(post.get("exit_price"))
    post_dir = str(post.get("direction", final_direction))
    post_result = str(post.get("result", ""))
    post_structure = str(post.get("market_structure", mkt_structure))
    post_ind = str(post.get("indicators", ""))

    trend_clear = _contains_any(post_structure, ["hh/hl", "lh/ll", "higher high", "lower low"])
    entry_quality = "correct"
    if entry_conf == "LOW":
        entry_quality = "early/low quality"
    elif entry_conf == "MEDIUM":
        entry_quality = "slightly early"

    violations = []
    if final_decision == "TRADE" and no_trade_triggered:
        violations.append("entered despite no-trade environment")
    if final_decision == "TRADE" and alignment_count < 4:
        violations.append("fewer than 4 aligned conditions")
    if action_180 == "NO TRADE" and _contains_any(post_dir, ["ce", "pe"]):
        violations.append("180 premium rule not satisfied at entry")

    avoidable = bool(violations) or not trend_clear
    pnl_pts = 0.0
    if post_entry > 0 and post_exit > 0 and _contains_any(post_dir, ["ce", "long", "bull"]):
        pnl_pts = post_exit - post_entry
    elif post_entry > 0 and post_exit > 0 and _contains_any(post_dir, ["pe", "short", "bear"]):
        pnl_pts = post_exit - post_entry

    conf_score = 78
    if not trend_clear:
        conf_score -= 20
    if entry_conf == "LOW":
        conf_score -= 18
    elif entry_conf == "MEDIUM":
        conf_score -= 8
    conf_score -= min(20, 8 * len(violations))
    if _contains_any(post_result, ["loss"]):
        conf_score -= 10
    conf_score = int(max(0, min(100, conf_score)))

    post_mistake = "None"
    if violations:
        post_mistake = "; ".join(violations)
    elif not trend_clear:
        post_mistake = "trend clarity was weak"

    post_improve = "Wait for full 4-factor alignment and strict VWAP confirmation before entry"
    if entry_conf == "LOW":
        post_improve = "Delay entry until strong breakout candle + follow-through + rising volume"

    response = {
        "pre_market_filter": {
            "trade_decision": pm_trade_decision,
            "direction_bias": pm_bias,
            "checks": pm_checks,
            "reason": "all checks passed" if pm_trade_decision == "TRADE" else f"failed: {', '.join(pm_failed)}",
        },
        "entry_confirmation": {
            "entry": entry_action,
            "confidence": entry_conf,
            "reason": "confirmed breakout with structure and VWAP" if entry_action != "NO TRADE" else "; ".join(entry_reason[:3]) or "entry rules not satisfied",
        },
        "premium_180_strategy": {
            "action": action_180,
            "target_points": "30-40",
            "stop_loss_points": 20,
            "reason": reason_180,
        },
        "no_trade_detection": {
            "decision": no_trade_decision,
            "checks": no_trade_hits,
            "reason": "no protection triggers" if no_trade_decision == "TRADE OK" else "one or more no-trade triggers active",
        },
        "edge_detection": {
            "setup_detected": edge_detected,
            "direction": edge_dir if edge_detected == "YES" else "NONE",
            "entry_timing_suggestion": edge_timing,
            "score": edge_score,
        },
        "post_trade_analysis": {
            "trend_clear": trend_clear,
            "entry_quality": entry_quality,
            "rule_violations": violations,
            "trade_avoidable": avoidable,
            "mistake": post_mistake,
            "improvement": post_improve,
            "confidence_score": conf_score,
            "pnl_points": round(pnl_pts, 2),
            "indicator_context": post_ind,
        },
        "global_alignment": {
            "trend": trend_aligned,
            "structure": structure_aligned,
            "momentum": momentum_aligned,
            "support_resistance": sr_aligned,
            "aligned_count": alignment_count,
            "required_minimum": 4,
        },
        "final_summary": {
            "final_decision": final_decision,
            "direction": final_direction if final_decision == "TRADE" else "",
            "entry_timing": "09:25-09:35 breakout follow-through" if final_decision == "TRADE" else "",
            "confidence": entry_conf if final_decision == "TRADE" else "LOW",
            "reason": final_reason,
        },
    }
    return jsonify(response)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Conversational AI assistant with ability to suggest+apply config changes."""
    import re as _re
    import openai

    body = request.get_json(force=True) or {}
    messages = (body.get("messages") or [])[-20:]   # cap history

    if not config.OPENAI_API_KEY:
        return jsonify({"error": "OpenAI API key not configured."}), 503
    if not messages:
        return jsonify({"error": "No messages provided."}), 400

    system = _CHAT_SYSTEM.format(
        RSI_BULL=config.RSI_BULL_THRESHOLD,
        RSI_BEAR=config.RSI_BEAR_THRESHOLD,
        VOL=config.VOLUME_EXPANSION_MULT,
        SL=config.INITIAL_SL_POINTS,
        COOL=config.DUPLICATE_SIGNAL_COOLDOWN,
        SLBLK=config.SL_BLOCK_DURATION,
        MAXOT=config.MAX_OPEN_TRADES,
        MDL=config.MAX_DAILY_LOSS,
    )

    try:
        client = openai.OpenAI(api_key=config.OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[{"role": "system", "content": system}] + messages,
            temperature=0.4,
            max_tokens=600,
        )
        raw = resp.choices[0].message.content or ""

        # Parse optional %%ACTION...%% block
        action = None
        m = _re.search(r'%%ACTION:config_change:(\w+):([\d.]+):(.+?)%%', raw, _re.DOTALL)
        if m:
            param, val_str, reason = m.group(1), m.group(2), m.group(3).strip()
            reply = _re.sub(r'\s*%%ACTION:[^%]+%%', '', raw).strip()
            if param in _SAFE_PARAMS:
                lo, hi = _SAFE_PARAMS[param]
                val = float(val_str)
                if lo <= val <= hi:
                    action = {
                        "type":      "config_change",
                        "param":     param,
                        "current":   getattr(config, param, None),
                        "suggested": val,
                        "reason":    reason,
                    }
        else:
            reply = raw

        return jsonify({"reply": reply, "action": action})

    except openai.RateLimitError:
        return jsonify({"error": "RATE_LIMIT"}), 429
    except openai.AuthenticationError:
        return jsonify({"error": "OpenAI authentication failed. Check your API key."}), 503
    except Exception as exc:
        log.error("chat API error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/llm/apply-suggestion", methods=["POST"])
def api_llm_apply_suggestion():
    """
    Apply an AI config suggestion by committing a change to config.py via
    the GitHub REST API. Vercel auto-detects the push and redeploys.

    Request JSON:
        param    – parameter name (must be in _SAFE_PARAMS allowlist)
        value    – new numeric value
        trade_id – trade this suggestion came from (for commit message)
        reason   – one-sentence reason (embedded in commit message)
    """
    import base64
    import re as _re
    import json as _json_mod
    import urllib.request
    import urllib.error

    body = request.get_json(force=True) or {}
    param    = str(body.get("param", "")).strip()
    value    = body.get("value")
    trade_id = str(body.get("trade_id", "")).strip()
    reason   = str(body.get("reason", "AI suggestion")).strip()[:120]

    # ── Security: validate param is in the allowlist ───────────────────────
    if param not in _SAFE_PARAMS:
        return jsonify({"error": f"'{param}' is not a modifiable parameter."}), 400

    lo, hi = _SAFE_PARAMS[param]
    try:
        num_val = float(value)
    except (TypeError, ValueError):
        return jsonify({"error": "value must be numeric."}), 400

    if not (lo <= num_val <= hi):
        return jsonify({"error": f"Value {num_val} out of safe range [{lo}, {hi}] for {param}."}), 400

    # Format value: integer if whole number, else 1-decimal float
    if num_val == int(num_val):
        formatted_val = str(int(num_val))
    else:
        formatted_val = f"{num_val:.1f}"

    # ── GitHub credentials ────────────────────────────────────────────────
    gh_token = getattr(config, "GITHUB_TOKEN", "") or ""
    gh_repo  = getattr(config, "GITHUB_REPO", "") or ""

    if not gh_token or not gh_repo:
        return jsonify({"error": "GITHUB_TOKEN / GITHUB_REPO not configured. Add them to Vercel environment variables."}), 503

    file_path = "trading_bot/config.py"
    api_url   = f"https://api.github.com/repos/{gh_repo}/contents/{file_path}"
    headers   = {
        "Authorization": f"token {gh_token}",
        "Accept":        "application/vnd.github+json",
        "Content-Type":  "application/json",
        "User-Agent":    "TradingBot-AutoApply/1.0",
    }

    try:
        # ── GET current file ──────────────────────────────────────────
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            file_data = _json_mod.loads(resp.read())

        sha             = file_data["sha"]
        current_content = base64.b64decode(file_data["content"]).decode("utf-8")

        # ── Regex-replace the parameter line ─────────────────────────
        line_pattern = _re.compile(
            rf'^({_re.escape(param)}\s*[:=]\s*)([^\s#\n]+)',
            _re.MULTILINE,
        )
        new_content, count = line_pattern.subn(rf'\g<1>{formatted_val}', current_content)
        if count == 0:
            return jsonify({"error": f"Could not locate '{param}' in config.py — pattern not found."}), 400

        # ── PUT updated file back ─────────────────────────────────────
        commit_msg = f"bot: AI suggests {param}={formatted_val} (trade {trade_id})"
        put_body = _json_mod.dumps({
            "message": commit_msg,
            "content": base64.b64encode(new_content.encode("utf-8")).decode("utf-8"),
            "sha":     sha,
        }).encode("utf-8")
        put_req = urllib.request.Request(api_url, data=put_body, headers=headers, method="PUT")
        with urllib.request.urlopen(put_req, timeout=20) as resp:
            resp_data = _json_mod.loads(resp.read())

        commit_sha = (resp_data.get("commit") or {}).get("sha", "")[:7]
        log.info("AI suggestion applied: %s=%s (commit %s)", param, formatted_val, commit_sha)
        return jsonify({
            "success": True,
            "param":   param,
            "value":   formatted_val,
            "commit":  commit_sha,
            "message": f"✓ {param} → {formatted_val} committed. Vercel will redeploy in ~30s. ({commit_sha})",
        })

    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode(errors="replace")
        try:
            gh_msg = _json_mod.loads(err_body).get("message", err_body[:200])
        except Exception:
            gh_msg = err_body[:200]
        log.error("GitHub API %d: %s", exc.code, gh_msg)
        return jsonify({"error": f"GitHub API {exc.code}: {gh_msg}"}), 500
    except Exception as exc:
        log.error("apply_suggestion error: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ─── Options Chain APIs ───────────────────────────────────────────────────────

def _compute_rule180_bias() -> dict:
    """
    Determine directional bias to avoid opposite-side picks at 09:25.

    Combines:
      - 5m EMA(9/21) trend
      - 1m short-term momentum (last 5 bars)
      - 1m candle breadth (green vs red in last 5 bars)
    """
    from trading_bot.candle_cache import get_candles

    df1m, _, _ = get_candles("1m", 80)
    df5m, _, _ = get_candles("5m", 80)
    if df1m is None or len(df1m) < 30 or df5m is None or len(df5m) < 30:
        return {
            "bias": "NEUTRAL",
            "confidence": 45,
            "reason": "insufficient candle data",
            "metrics": {},
        }

    c1 = df1m["close"].astype(float)
    c5 = df5m["close"].astype(float)

    ema5_fast = c5.ewm(span=9, adjust=False).mean().iloc[-1]
    ema5_slow = c5.ewm(span=21, adjust=False).mean().iloc[-1]
    m1_move = float(c1.iloc[-1] - c1.iloc[-6])

    last5 = df1m.tail(5)
    green = int((last5["close"] > last5["open"]).sum())
    red = int((last5["close"] < last5["open"]).sum())

    bull = 0
    bear = 0
    notes = []

    if ema5_fast > ema5_slow:
        bull += 1
        notes.append("5m EMA9 > EMA21")
    else:
        bear += 1
        notes.append("5m EMA9 < EMA21")

    if m1_move > 0:
        bull += 1
        notes.append("1m momentum up")
    elif m1_move < 0:
        bear += 1
        notes.append("1m momentum down")

    if green >= 3:
        bull += 1
        notes.append(f"last5 breadth green={green}")
    elif red >= 3:
        bear += 1
        notes.append(f"last5 breadth red={red}")

    if bull >= 2 and bull > bear:
        bias = "BULLISH"
    elif bear >= 2 and bear > bull:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    conf = 55 + abs(bull - bear) * 12
    conf = max(35, min(90, conf))

    return {
        "bias": bias,
        "confidence": int(conf),
        "reason": ", ".join(notes[:3]),
        "metrics": {
            "ema5_fast": round(float(ema5_fast), 2),
            "ema5_slow": round(float(ema5_slow), 2),
            "m1_move_5bars": round(float(m1_move), 2),
            "green_last5": green,
            "red_last5": red,
            "score_bull": bull,
            "score_bear": bear,
        },
    }


def _rule180_time_state() -> dict:
    now = now_ist()
    t = now.strftime("%H:%M")
    entry_open = "09:25" <= t <= "09:45"
    monitor_open = "09:25" <= t <= "10:00"
    return {
        "now": t,
        "entry_open": entry_open,
        "monitor_open": monitor_open,
        "entry_window": "09:25-09:45",
        "exit_by": "10:00",
    }


def _pick_rule180_contract(chain: dict, side: str) -> tuple[dict | None, list[dict]]:
    """
    Pick contract for the 180 rule.

    Priority:
      1) Sustained above 180 (two consecutive polls)
      2) LTP closest to 180 on desired side
    """
    contracts: list[dict] = []
    for row in chain.get("chain", []):
        leg = row.get(side)
        if not leg:
            continue
        ltp = float(leg.get("ltp") or 0)
        if ltp <= 0:
            continue
        token = str(leg.get("token", ""))
        key = f"rule180:sustain:{token}"
        prev = get_cached(key) or {"count": 0}
        count = int(prev.get("count", 0))
        count = count + 1 if ltp >= 180 else 0
        set_cached(key, {"count": count, "last": ltp}, ttl=7200)
        contracts.append({
            "strike": row.get("strike"),
            "token": token,
            "symbol": leg.get("symbol"),
            "ltp": round(ltp, 2),
            "sustain_count": count,
            "sustained": count >= 2,
            "distance_to_180": round(abs(ltp - 180), 2),
        })

    if not contracts:
        return None, []

    sustained = [c for c in contracts if c["sustained"] and c["ltp"] >= 180]
    sustained.sort(key=lambda x: x["distance_to_180"])
    if sustained:
        return sustained[0], contracts

    # Fallback: nearest to 180 above 170
    near = [c for c in contracts if c["ltp"] >= 170]
    near.sort(key=lambda x: (abs(x["ltp"] - 180), -x["ltp"]))
    return (near[0] if near else None), contracts


@app.route("/api/rule180/recommendation")
def api_rule180_recommendation():
    """
    Rule-180 assistant:
      - Detect trend bias so user avoids opposite CE/PE selection
      - Pick option contract near/sustaining above 180 premium
      - Return fixed plan: target 220, stoploss 160, exit by 10:00
    """
    from trading_bot.auth.login import get_session
    import datetime as _dt

    expiry_str = request.args.get("expiry", "")
    expiry_date = None
    if expiry_str:
        try:
            expiry_date = _dt.date.fromisoformat(expiry_str)
        except ValueError:
            return jsonify({"error": f"Invalid expiry date: {expiry_str}"}), 400

    bias_data = _compute_rule180_bias()
    side = "CE" if bias_data["bias"] == "BULLISH" else "PE" if bias_data["bias"] == "BEARISH" else ""
    tstate = _rule180_time_state()

    tick = get_latest_tick()
    nifty_spot = tick.ltp
    if nifty_spot <= 0:
        tick, _ = fetch_live_once()
        nifty_spot = tick.ltp
    if nifty_spot <= 0:
        return jsonify({"error": "No NIFTY spot available"}), 400

    session = get_session()
    chain = build_option_chain(session, nifty_spot, expiry_date)

    recommendation = None
    candidates = []
    action = "WAIT"
    message = "No trade"

    if side:
        recommendation, candidates = _pick_rule180_contract(chain, side)
        if recommendation is None:
            message = f"{side} side has no valid premium near 180"
        elif not tstate["entry_open"]:
            action = "MONITOR"
            message = f"Entry window closed ({tstate['entry_window']}). Monitor only until {tstate['exit_by']}"
        elif recommendation["ltp"] < 180:
            action = "WAIT"
            message = f"{side} not yet above 180. Wait for sustain above 180"
        elif recommendation["sustain_count"] < 2:
            action = "WAIT"
            message = f"{side} touched 180 but not sustained yet (count={recommendation['sustain_count']})"
        else:
            action = "BUY"
            message = f"BUY {side}: trend-aligned and sustained above 180"
    else:
        message = "Trend is neutral. Skip to avoid wrong-side CE/PE selection"

    return jsonify({
        "time": now_ist().strftime("%H:%M:%S"),
        "nifty_spot": round(float(nifty_spot), 2),
        "expiry": chain.get("expiry"),
        "atm": chain.get("atm"),
        "bias": bias_data,
        "preferred_side": side or "NONE",
        "time_state": tstate,
        "plan": {
            "entry_level": 180,
            "target": 220,
            "stoploss": 160,
            "exit_by": "10:00",
        },
        "action": action,
        "message": message,
        "recommendation": recommendation,
        "top_candidates": sorted(candidates, key=lambda x: x["distance_to_180"])[:8],
    })


def _rule180_trade_csv_paths() -> list[str]:
    """Possible locations for reversal180 trade sheet."""
    proj_root = os.path.abspath(os.path.join(_HERE, "..", ".."))
    return [
        os.path.join(proj_root, "logs", "reversal180_trades.csv"),
        os.path.join(proj_root, "trading_bot", "logs", "reversal180_trades.csv"),
        "/tmp/reversal180_trades.csv",
    ]


def _load_rule180_trades() -> list[dict]:
    for p in _rule180_trade_csv_paths():
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", newline="") as f:
                return list(csv.DictReader(f))
        except Exception:
            continue
    return []


@app.route("/api/rule180/pnl")
def api_rule180_pnl():
    """Return Rule180 strategy-only PnL summary and recent trades."""
    trades = _load_rule180_trades()
    today = now_ist().strftime("%Y-%m-%d")
    try:
        budget_per_trade = float(os.getenv("R180_CAPITAL_PER_TRADE", "30000"))
    except Exception:
        budget_per_trade = 30000.0

    total_pnl = 0.0
    today_pnl = 0.0
    wins = 0
    losses = 0
    recent: list[dict] = []

    for row in trades:
        try:
            pnl = float(row.get("pnl") or 0.0)
        except Exception:
            pnl = 0.0
        total_pnl += pnl

        ts = str(row.get("timestamp") or "")
        if ts.startswith(today):
            today_pnl += pnl
        elif len(ts) == 5 and ":" in ts:
            # Backward compatibility: older rows had HH:MM only
            today_pnl += pnl

        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

        recent.append({
            "timestamp": ts,
            "instrument": row.get("instrument", ""),
            "side": row.get("side", ""),
            "entry": float(row.get("entry") or 0.0),
            "exit": float(row.get("exit") or 0.0),
            "pnl": pnl,
            "reason": row.get("reason", ""),
        })

    total = len(trades)
    win_rate = (wins / total * 100.0) if total else 0.0

    recent = list(reversed(recent))[:20]

    return jsonify({
        "date": today,
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 2),
        "total_pnl": round(total_pnl, 2),
        "today_pnl": round(today_pnl, 2),
        "budget_per_trade": round(budget_per_trade, 2),
        "recent": recent,
        "source": "rule180_trades_csv",
    })


def _orb_trade_csv_paths() -> list[str]:
    """Possible locations for ORB strategy trade sheet."""
    proj_root = os.path.abspath(os.path.join(_HERE, "..", ".."))
    return [
        os.path.join(proj_root, "logs", "orb_strategy_trades.csv"),
        os.path.join(proj_root, "trading_bot", "logs", "orb_strategy_trades.csv"),
        "/tmp/orb_strategy_trades.csv",
    ]


def _load_orb_trades() -> list[dict]:
    for p in _orb_trade_csv_paths():
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", newline="") as f:
                return list(csv.DictReader(f))
        except Exception:
            continue
    return []


def _get_orb_runtime_snapshot() -> dict:
    """Best-effort ORB levels + spot snapshot for dashboard display."""
    try:
        from trading_bot.orb_strategy.config import ORBConfig
        from trading_bot.orb_strategy.data_handler import ORBDataHandler
        from trading_bot.orb_strategy.strategy_orb import ORBStrategy

        cfg = ORBConfig.from_env()
        dh = ORBDataHandler(cfg)
        st = ORBStrategy(cfg)
        df = dh.get_1m_candles_today()
        if df is None or len(df) < 5:
            return {
                "orb_high": None,
                "orb_low": None,
                "spot_ltp": None,
                "note": "Waiting for 1m candles",
            }

        orb = st.compute_orb(df)
        spot = dh.get_spot_ltp()
        return {
            "orb_high": float(orb.high),
            "orb_low": float(orb.low),
            "spot_ltp": float(spot) if spot > 0 else None,
            "note": f"ORB window {cfg.orb_start}-{cfg.orb_end} | entry till {cfg.last_entry_time}",
        }
    except Exception as exc:
        return {
            "orb_high": None,
            "orb_low": None,
            "spot_ltp": None,
            "note": f"Runtime snapshot unavailable: {exc}",
        }


@app.route("/api/orb/pnl")
def api_orb_pnl():
    """Return ORB strategy-only PnL summary and recent trades."""
    trades = _load_orb_trades()
    today = now_ist().strftime("%Y-%m-%d")
    try:
        from trading_bot.orb_strategy.config import ORBConfig
        budget_per_trade = float(ORBConfig.from_env().capital_per_trade)
    except Exception:
        try:
            budget_per_trade = float(os.getenv("ORB_CAPITAL_PER_TRADE", "30000"))
        except Exception:
            budget_per_trade = 30000.0

    total_pnl = 0.0
    today_pnl = 0.0
    wins = 0
    losses = 0
    recent: list[dict] = []

    for row in trades:
        try:
            pnl = float(row.get("pnl") or 0.0)
        except Exception:
            pnl = 0.0
        total_pnl += pnl

        ts = str(row.get("timestamp") or "")
        if ts.startswith(today):
            today_pnl += pnl
        elif len(ts) == 5 and ":" in ts:
            today_pnl += pnl

        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

        recent.append({
            "timestamp": ts,
            "instrument": row.get("instrument", ""),
            "side": row.get("side", ""),
            "entry": float(row.get("entry") or 0.0),
            "exit": float(row.get("exit") or 0.0),
            "pnl": pnl,
            "reason": row.get("reason", ""),
        })

    total = len(trades)
    win_rate = (wins / total * 100.0) if total else 0.0

    recent = list(reversed(recent))[:20]

    return jsonify({
        "date": today,
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 2),
        "total_pnl": round(total_pnl, 2),
        "today_pnl": round(today_pnl, 2),
        "budget_per_trade": round(budget_per_trade, 2),
        "recent": recent,
        "runtime": _get_orb_runtime_snapshot(),
        "source": "orb_strategy_trades_csv",
        "time": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
    })

@app.route("/api/options/expiries")
def api_options_expiries():
    """Return available expiry dates for NIFTY options.

    Returns computed weekly Thursdays immediately (no instrument master needed)
    so the page loads instantly. Full expiry list from instrument master is
    used only if already cached; otherwise falls back to computed dates.
    """
    # Always try real expiries from instrument master first
    try:
        real_expiries = get_available_expiries()
    except Exception:
        real_expiries = []

    this_week, next_week = get_weekly_expiries()

    # Smart default: if today IS expiry day and market is closed → next week
    default = this_week
    n = now_ist()
    if (n.date() == this_week and n.time() > datetime.time(15, 30)) or n.date() > this_week:
        default = next_week

    if real_expiries:
        expiries = real_expiries
    else:
        # Fallback: computed weekly dates
        computed = []
        d = this_week
        for _ in range(8):
            computed.append(d.isoformat())
            d += datetime.timedelta(days=7)
        expiries = computed

    return jsonify({
        "expiries":       expiries,
        "this_week":      this_week.isoformat(),
        "next_week":      next_week.isoformat(),
        "default_expiry": default.isoformat(),
    })


@app.route("/api/options/chain")
def api_options_chain():
    """
    Return NIFTY option chain for a given expiry.
    Query params:
        expiry – YYYY-MM-DD (default: this week's Thursday)
    """
    from trading_bot.auth.login import get_session
    import datetime as _dt

    expiry_str = request.args.get("expiry", "")
    expiry_date = None
    if expiry_str:
        try:
            expiry_date = _dt.date.fromisoformat(expiry_str)
        except ValueError:
            return jsonify({"error": f"Invalid expiry date: {expiry_str}"}), 400

    # ── Redis cache (60 s, keyed by expiry) ──
    cache_key = f"nifty:chain:{expiry_str or 'default'}"
    cached = get_cached(cache_key)
    if cached:
        return jsonify(cached)

    # Get NIFTY spot for ATM calculation
    tick = get_latest_tick()
    nifty_spot = tick.ltp
    # On serverless (no background feed), fetch on-demand
    if nifty_spot <= 0:
        tick, _ = fetch_live_once()
        nifty_spot = tick.ltp
    if nifty_spot <= 0:
        return jsonify({"error": "No NIFTY spot price available. Check API credentials."}), 400

    try:
        session = get_session()
        chain = build_option_chain(session, nifty_spot, expiry_date)
        set_cached(cache_key, chain, ttl=60)
        return jsonify(chain)
    except Exception as e:
        log.error("Option chain error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/options/load", methods=["POST"])
def api_options_load():
    """
    Pre-download instrument master (one-time daily).
    Call this before first option chain request to avoid delay.
    """
    try:
        load_options(force=False)
        return jsonify({"status": "ok", "message": "Instruments loaded"})
    except Exception as e:
        log.error("Instrument load error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/ai/analyze", methods=["POST"])
def api_ai_analyze():
    """
    Send chart candle data to ChatGPT for AI pattern analysis.

    Body JSON (optional):
        date      – YYYY-MM-DD  (default: today)
        timeframe – 1m | 5m | 15m  (default: 5m)
        model     – OpenAI model  (default: from config)
        signals   – existing signal context to include
    """
    data = request.get_json(force=True) if request.is_json else {}

    date_str  = data.get("date", now_ist().strftime("%Y-%m-%d"))
    timeframe = data.get("timeframe", "5m")
    model     = data.get("model", config.OPENAI_MODEL or "gpt-4o-mini")
    signals   = data.get("signals", "")

    # ── Gather candle data ──
    rows = fetch_candles_by_date(config.UNDERLYING, timeframe, date_str)

    # Fallback: try fetching from API if no local data
    if not rows:
        import datetime as _dt
        from trading_bot.auth.login import get_session
        tf_map = {"1m": "ONE_MINUTE", "5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE"}
        interval = tf_map.get(timeframe, "FIVE_MINUTE")
        try:
            session = get_session()
            if session:
                params = {
                    "exchange":    config.EXCHANGE,
                    "symboltoken": config.NIFTY_TOKEN,
                    "interval":    interval,
                    "fromdate":    f"{date_str} 09:15",
                    "todate":      f"{date_str} 15:30",
                }
                resp = session.getCandleData(params)
                if resp and resp.get("status") is not False:
                    raw = resp.get("data") or []
                    rows = [
                        {"timestamp": str(bar[0]), "open": float(bar[1]),
                         "high": float(bar[2]), "low": float(bar[3]),
                         "close": float(bar[4]), "volume": int(bar[5])}
                        for bar in raw if len(bar) >= 6
                    ]
        except Exception as exc:
            log.warning("AI analyze fallback fetch: %s", exc)

    if not rows:
        return jsonify({"error": f"No candle data for {date_str} ({timeframe})"}), 400

    # Convert sqlite3.Row to dicts if needed
    candles = [dict(r) if not isinstance(r, dict) else r for r in rows]

    # Build extra context from existing signals
    extra = ""
    if signals:
        extra = f"Existing algorithm signals detected:\n{signals}"

    from trading_bot.llm.analyzer import analyze_candles
    result = analyze_candles(
        candles=candles,
        timeframe=timeframe,
        extra_context=extra,
        model=model,
    )

    if result["error"]:
        return jsonify(result), 400 if "not configured" in (result["error"] or "") else 200

    return jsonify({
        "analysis":     result["analysis"],
        "model":        result["model"],
        "candles_sent": result["candles_sent"],
        "date":         date_str,
        "timeframe":    timeframe,
    })


@app.route("/api/options/debug")
def api_options_debug():
    """Temporary debug endpoint: show instrument master state."""
    try:
        opts = load_options()
        sample_expiries = set()
        for o in opts[:200]:
            sample_expiries.add(o.get("expiry", ""))
        return jsonify({
            "total_options": len(opts),
            "sample_expiries": sorted(sample_expiries),
            "sample": opts[:3] if opts else [],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Auto-Trade API ───────────────────────────────────────────────────────────

@app.route("/api/autotrade/start", methods=["POST"])
def api_autotrade_start():
    """Start the auto-trade engine."""
    import os
    if os.getenv("VERCEL"):
        # On Vercel, auto-trade runs via cron — no thread needed
        return jsonify({
            "enabled": True,
            "mode": "cron",
            "message": "Auto-trade runs via Vercel Cron (every 1 min during market hours)",
            "is_vercel": True,
            "thread_alive": False,
        })
    from trading_bot.autotrade import start as at_start, get_status
    data = request.get_json(force=True) if request.is_json else {}
    interval = int(data.get("interval", 60))
    at_start(scan_interval=interval)
    return jsonify(get_status())


@app.route("/api/autotrade/stop", methods=["POST"])
def api_autotrade_stop():
    """Stop the auto-trade engine."""
    from trading_bot.autotrade import stop as at_stop, get_status
    at_stop()
    return jsonify(get_status())


@app.route("/api/autotrade/status")
def api_autotrade_status():
    """Return current auto-trade status, log, and last signal."""
    import os
    is_vercel = bool(os.getenv("VERCEL"))
    if is_vercel:
        # On Vercel, report status from DB and scan log from Redis
        from trading_bot.data.store import get_open_trades, get_today_pnl
        from trading_bot.utils.time_utils import now_ist
        from trading_bot.autotrade import _is_live_market_hours
        from trading_bot.redis_sync import sync_trades_from_redis
        sync_trades_from_redis()
        open_trades = get_open_trades()
        auto_count = sum(
            1 for t in open_trades
            if (t.get("source") or t["trade_id"][:2]) in ("AUTO", "AT")
        )
        today_str = now_ist().strftime("%Y-%m-%d")
        pnl = get_today_pnl(today_str)

        # Get scan log from Redis
        live_now = _is_live_market_hours()
        scan_log = [
            "Auto-scan active during live market hours"
            if live_now else
            f"Auto-scan paused (live hours: {config.MARKET_OPEN_TIME}-{config.MARKET_CLOSE_TIME} IST)"
        ]
        try:
            from trading_bot.redis_sync import get_scan_log
            redis_log = get_scan_log(20)
            if live_now and redis_log:
                scan_log = redis_log
        except Exception:
            pass

        return jsonify({
            "enabled": True,
            "mode": "vercel",
            "thread_alive": True,
            "is_vercel": True,
            "market_live": live_now,
            "open_positions": auto_count,
            "pnl_today": round(pnl, 2),
            "last_scan": now_ist().strftime("%H:%M:%S"),
            "last_signal": None,
            "log": scan_log,
        })
    from trading_bot.autotrade import get_status, is_alive
    status = get_status()
    status["thread_alive"] = is_alive()
    status["is_vercel"] = False
    status["bg_scanner_active"] = _bg_scanner_started
    return jsonify(status)


@app.route("/api/autotrade/scan", methods=["POST", "GET"])
@app.route("/api/cron/autotrade", methods=["GET"])
def api_autotrade_scan():
    """Trigger one auto-trade scan cycle (works on Vercel and local)."""
    import os
    from trading_bot.utils.time_utils import now_ist

    # On Vercel, verify QStash signature or dashboard origin
    if os.environ.get("VERCEL"):
        is_qstash = request.headers.get("Upstash-Signature")
        is_dashboard = request.referrer and "angelonetradingbot" in request.referrer
        is_github = request.headers.get("User-Agent", "").startswith("github-actions")
        if not (is_qstash or is_dashboard or is_github):
            return jsonify({"error": "unauthorized"}), 403

    try:
        from trading_bot.autotrade import (
            _monitor_positions,
            _scan_and_trade,
            _is_in_trade_window,
            _is_live_market_hours,
        )
        from trading_bot.data.store import get_open_trades, get_today_pnl

        # Sync trades from Redis so this Vercel instance has the latest state
        if os.environ.get("VERCEL"):
            from trading_bot.redis_sync import sync_trades_from_redis
            sync_trades_from_redis()

        result = {"time": now_ist().strftime("%H:%M:%S")}

        live_now = _is_live_market_hours()
        result["market_live"] = live_now

        # Strict live-hours gate: no monitor/scan work outside market hours.
        if live_now:
            # Monitor existing positions (SL/target/EOD exit)
            _monitor_positions()
            result["monitored"] = True

            # Scan for new signals
            if _is_in_trade_window():
                _scan_and_trade()
                result["scanned"] = True
            else:
                result["scanned"] = False
                result["reason"] = "outside_trade_window"
        else:
            result["monitored"] = False
            result["scanned"] = False
            result["reason"] = "market_closed"

        open_trades = get_open_trades()
        auto_count = sum(
            1 for t in open_trades
            if (t.get("source") or t["trade_id"][:2]) in ("AUTO", "AT")
        )
        today_pnl = get_today_pnl(now_ist().strftime("%Y-%m-%d"))
        result["open_positions"] = auto_count
        result["pnl_today"] = round(today_pnl, 2)
        result["status"] = "ok"

        # Log only real scans to Redis to avoid off-hours noise spam.
        try:
            from trading_bot.redis_sync import push_scan_log
            if result.get("scanned"):
                ts = now_ist().strftime("%H:%M:%S")
                log_entry = f"[{ts}] pos={auto_count} pnl=₹{today_pnl:.2f} (scanned)"
                push_scan_log(log_entry)
        except Exception:
            pass

        return jsonify(result)
    except Exception as e:
        log.error("Scan error: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
#  SCORING ENGINE ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

# /api/scoring/pnl removed — P&L cards no longer shown on 20-day avg page


@app.route("/api/scoring/live")
def api_scoring_live():
    """Return live 20-day average analysis for NIFTY 50."""
    from trading_bot.scoring import fetch_daily_closes, analyze_live

    cached = get_cached("scoring_live", ttl=30)
    if cached:
        return jsonify(cached)

    try:
        session = None
        try:
            from trading_bot.auth.login import get_session
            session = get_session()
        except Exception:
            pass

        if not session:
            return jsonify({"error": "no session", "should_enter": False})

        # Fetch daily NIFTY closes for 20-day SMA
        daily_df = fetch_daily_closes(session)
        if daily_df is None or len(daily_df) < 20:
            return jsonify({"error": "insufficient daily data", "should_enter": False})

        # Fetch 1m candles for intraday confirmation + live price
        from trading_bot.data.historical import fetch_candle_data
        df_1m = None
        live_price = 0.0
        try:
            df_1m = fetch_candle_data(session, "NIFTY", config.NIFTY_TOKEN,
                                      "ONE_MINUTE", days=2)
            if df_1m is not None and len(df_1m) > 0:
                live_price = float(df_1m["close"].iloc[-1])
        except Exception:
            pass

        result = analyze_live(daily_df, df_1m, live_price)
        data = result.to_dict()
        set_cached("scoring_live", data, ttl=30)
        return jsonify(_sanitize(data))
    except Exception as exc:
        log.warning("api_scoring_live error: %s", exc)
        return jsonify({"error": str(exc), "should_enter": False})


# ─── Start helper ─────────────────────────────────────────────────────────────

def start_dashboard(
    host: str | None = None,
    port: int | None = None,
) -> threading.Thread:
    """Launch Flask in a background daemon thread and return it."""
    host = host or config.DASHBOARD_HOST
    port = port or config.DASHBOARD_PORT
    log.info("Dashboard → http://%s:%d", host, port)

    t = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False),
        name="dashboard",
        daemon=True,
    )
    t.start()
    return t


# ─── Standalone launch ────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print(f"Dashboard → http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    app.run(
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        debug=True,
        use_reloader=False,
    )

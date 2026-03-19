"""
dashboard/server.py — Flask web dashboard for Project Candles.

Endpoints:
    GET /                   — Main chart UI
    GET /api/candles        — OHLCV data  (?date=YYYY-MM-DD&timeframe=1m)
    GET /api/dates          — List of dates that have candle data
    GET /api/pnl            — Today's PnL summary
"""

import datetime
import json
import os
import threading
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
)
from trading_bot.indicators import sma, linear_regression
from trading_bot.candles import detect_all, scan_signals
from trading_bot.strategy import evaluate, evaluate_latest
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


@app.route("/trade")
def trade_page():
    return render_template("trade.html")


@app.route("/orders")
def orders_page():
    return render_template("orders.html")


@app.route("/opportunities")
def opportunities_page():
    return render_template("opportunities.html")


@app.route("/history")
def history_page():
    return render_template("history.html")


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
    today       = now_ist().strftime("%Y-%m-%d")
    pnl         = get_today_pnl(today)
    open_trades = len(get_open_trades())
    return jsonify({
        "date":        today,
        "total_pnl":   round(pnl, 2),
        "open_trades": open_trades,
        "mode":        config.TRADING_MODE.upper(),
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
    source      = data.get("source", "MANUAL")  # MANUAL | AUTO

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
    source      = data.get("source", "MANUAL")  # MANUAL | AUTO

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
    log.info("Paper SELL %s  %s  qty=%d  px=%.2f  sl=%s  tgt=%s",
             trade_id, symbol, qty, entry_price, sl, tgt)
    return jsonify({
        "trade_id": trade_id, "side": "SELL", "symbol": symbol,
        "price": entry_price, "qty": qty, "stop_loss": sl, "target": tgt,
    })


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
    close_trade(trade_id, exit_price, now_str, exit_reason, pnl)
    update_portfolio_after_trade(pnl, pnl >= 0)
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
        if p["direction"] == "LONG":
            if sl and ltp <= sl:
                triggered = "SL"
            elif tgt and ltp >= tgt:
                triggered = "TARGET"
        else:  # SHORT
            if sl and ltp >= sl:
                triggered = "SL"
            elif tgt and ltp <= tgt:
                triggered = "TARGET"

        if triggered:
            trade_id = p["trade_id"]
            entry = p["entry_price"]
            qty = p["qty"]
            if p["direction"] == "LONG":
                pnl = (ltp - entry) * qty
            else:
                pnl = (entry - ltp) * qty
            pnl = round(pnl, 2)
            now_str = now_ist().isoformat()
            close_trade(trade_id, ltp, now_str, triggered, pnl)
            update_portfolio_after_trade(pnl, pnl >= 0)
            log.info("Auto %s %s  exit=%.2f  pnl=%.2f", triggered, trade_id, ltp, pnl)
            auto_closed.append({
                "trade_id": trade_id, "reason": triggered,
                "exit_price": ltp, "pnl": pnl,
            })
    return auto_closed


@app.route("/api/paper/positions")
def api_paper_positions():
    """Return open positions with live P&L, market value, and SL/Target check."""
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
            unrealized = (entry - ltp) * qty

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
    """Return closed paper trades (most recent first). Optional ?source=MANUAL|AUTO filter."""
    source_filter = request.args.get("source", "").upper()
    if source_filter in ("MANUAL", "AUTO"):
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
                    sigs = evaluate(df, backtest=True)
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


# ─── Options Chain APIs ───────────────────────────────────────────────────────

@app.route("/api/options/expiries")
def api_options_expiries():
    """Return available expiry dates for NIFTY options.

    Returns computed weekly Thursdays immediately (no instrument master needed)
    so the page loads instantly. Full expiry list from instrument master is
    used only if already cached; otherwise falls back to computed dates.
    """
    this_week, next_week = get_weekly_expiries()

    # Smart default: if today IS expiry day and market is closed → next week
    default = this_week
    n = now_ist()
    if (n.date() == this_week and n.time() > datetime.time(15, 30)) or n.date() > this_week:
        default = next_week

    # Build 8-week computed expiry list (guaranteed fast, no API call)
    computed = []
    d = this_week
    for _ in range(8):
        computed.append(d.isoformat())
        d += datetime.timedelta(days=7)

    # Try to augment with instrument master expiries only if already cached
    expiries = computed
    try:
        from trading_bot.options import _CACHE_FILE, _nifty_options
        if _nifty_options:  # already loaded in memory — free to use
            full = get_available_expiries()
            if full:
                expiries = full
    except Exception:
        pass

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
    from trading_bot.autotrade import get_status
    return jsonify(get_status())


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

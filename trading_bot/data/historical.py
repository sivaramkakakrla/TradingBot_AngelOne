"""
data/historical.py — Fetch OHLCV candle data from AngelOne SmartAPI
and persist to SQLite via store.upsert_candles().

NOTE — Closing‑price fix
    AngelOne getCandleData() returns 1m candles only up to 15:29.
    The official NIFTY closing price is set during the NSE pre‑close
    session (15:30‑15:40) and is NOT included as a 1m candle.
    We therefore fetch the 1D candle for the same day and inject a
    synthetic "15:30" candle whose close matches the day‑candle close.
    This ensures all indicators and strategy signals use the true
    closing value that matches the AngelOne app / NSE EOD price.
"""

import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from trading_bot import config
from trading_bot.data.store import upsert_candles
from trading_bot.utils.logger import get_logger

log = get_logger(__name__)

IST = ZoneInfo(config.TIMEZONE)
_MARKET_OPEN  = "09:15"
_MARKET_CLOSE = "15:30"


# ═══════════════════════════════════════════════════════════════════════════════
#  RAW CANDLE FETCH
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_raw(session, exchange: str, token: str, interval: str,
               from_dt: str, to_dt: str) -> list[list]:
    """Low‑level wrapper around getCandleData. Returns list of bar arrays."""
    params = {
        "exchange":    exchange,
        "symboltoken": token,
        "interval":    interval,
        "fromdate":    from_dt,
        "todate":      to_dt,
    }
    try:
        resp = session.getCandleData(params)
    except Exception as exc:
        log.error("getCandleData error: %s", exc)
        return []

    if not resp or resp.get("status") is False:
        log.debug("No data — %s", resp)
        return []

    return resp.get("data") or []


def _fetch_day_close(session, trade_date: date) -> float | None:
    """Fetch the 1D candle for `trade_date` and return its close price."""
    d_str = trade_date.strftime("%Y-%m-%d")
    bars = _fetch_raw(
        session, config.EXCHANGE, config.NIFTY_TOKEN, "ONE_DAY",
        f"{d_str} 00:00", f"{d_str} 23:59",
    )
    if not bars:
        return None
    # Last daily bar for the date
    return float(bars[-1][4])


# ═══════════════════════════════════════════════════════════════════════════════
#  INTRADAY FETCH WITH CLOSING‑CANDLE FIX
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_candles_for_day(
    session,
    trade_date: date,
    timeframe: str = "1m",
) -> list[dict]:
    """
    Fetch OHLCV candles for a single trading day from AngelOne.

    For intraday timeframes (1m, 5m, 15m) the API returns bars only up to
    15:29.  We fetch the 1D candle independently and, if its close differs
    from the last intraday bar's close, inject a synthetic "15:30" candle
    so the stored data reflects the true NSE closing price.

    Parameters
    ----------
    session    : active SmartConnect object
    trade_date : the calendar date to fetch
    timeframe  : key from config.TIMEFRAMES  ('1m', '5m', '15m', '1D')

    Returns
    -------
    List of row-dicts ready for upsert_candles(). Empty list on error.
    """
    interval = config.TIMEFRAMES[timeframe]["interval"]
    d_str    = trade_date.strftime("%Y-%m-%d")

    raw = _fetch_raw(
        session, config.EXCHANGE, config.NIFTY_TOKEN, interval,
        f"{d_str} {_MARKET_OPEN}", f"{d_str} {_MARKET_CLOSE}",
    )
    if not raw:
        return []

    rows: list[dict] = []
    for bar in raw:
        if len(bar) < 6:
            continue
        rows.append({
            "symbol":    config.UNDERLYING,
            "token":     config.NIFTY_TOKEN,
            "timeframe": timeframe,
            "timestamp": str(bar[0]),
            "open":      float(bar[1]),
            "high":      float(bar[2]),
            "low":       float(bar[3]),
            "close":     float(bar[4]),
            "volume":    int(bar[5]),
        })

    # ── Closing‑candle fix (intraday only) ────────────────────────────────
    if timeframe in ("1m", "5m", "15m") and rows:
        day_close = _fetch_day_close(session, trade_date)
        if day_close is not None:
            last_close = rows[-1]["close"]
            if abs(day_close - last_close) > 0.05:
                close_ts = f"{d_str}T15:30:00+05:30"
                rows.append({
                    "symbol":    config.UNDERLYING,
                    "token":     config.NIFTY_TOKEN,
                    "timeframe": timeframe,
                    "timestamp": close_ts,
                    "open":      last_close,
                    "high":      max(last_close, day_close),
                    "low":       min(last_close, day_close),
                    "close":     day_close,
                    "volume":    0,
                })
                log.info(
                    "Injected closing candle for %s %s: "
                    "last_1m_close=%.2f → day_close=%.2f (delta=%.2f)",
                    timeframe, d_str, last_close, day_close,
                    day_close - last_close,
                )

    return rows


# ═══════════════════════════════════════════════════════════════════════════════
#  BULK HISTORICAL FETCH
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_and_store_history(
    session,
    days: int = 30,
    timeframe: str = "1m",
) -> int:
    """
    Fetch the last `days` calendar days of NIFTY candles and store in SQLite.

    Skips weekends automatically. Respects config.API_RATE_LIMIT.
    Injects synthetic closing candles where needed so stored data
    matches the official NSE closing price.

    Returns total new rows inserted.
    """
    today = datetime.now(tz=IST).date()
    start = today - timedelta(days=days)
    delay = 1.0 / max(config.API_RATE_LIMIT, 1)
    total = 0

    cur = start
    while cur <= today:
        if cur.weekday() < 5:           # Mon–Fri only
            rows = fetch_candles_for_day(session, cur, timeframe)
            if rows:
                inserted = upsert_candles(rows)
                total += inserted
                log.info(
                    "Stored %3d new %s bars for %s  (fetched: %d)",
                    inserted, timeframe, cur, len(rows),
                )
        cur += timedelta(days=1)
        time.sleep(delay)               # rate limiting

    log.info("Historical fetch done — %d new rows stored.", total)
    return total

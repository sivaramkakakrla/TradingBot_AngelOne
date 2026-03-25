"""
candle_cache.py — Centralized candle data cache.

Prevents multiple components (dashboard, auto-trade engine) from
hammering AngelOne's getCandleData API simultaneously.

Single source of truth: fetch once, share with all consumers.
API rate limit is ~1 call per 2 seconds; we cache for 30s.
"""

import threading
import time

import pandas as pd

from trading_bot import config
from trading_bot.auth.login import get_session
from trading_bot.utils.logger import get_logger
from trading_bot.utils.time_utils import now_ist

log = get_logger(__name__)

_lock = threading.Lock()
_cache: dict = {}          # {"df": pd.DataFrame, "ts": float, "raw": list, "data_date": str}
_CACHE_TTL = 30            # seconds


def get_candles(timeframe: str = "1m", bars: int = 100) -> tuple[pd.DataFrame | None, str | None, list | None]:
    """
    Return (DataFrame, data_date_str, raw_rows) from cache or fresh API call.

    Returns cached data if it's less than 30 seconds old.
    Only ONE thread fetches at a time; others wait and share the result.
    """
    key = timeframe
    now = time.time()

    with _lock:
        cached = _cache.get(key)
        if cached and (now - cached["ts"]) < _CACHE_TTL:
            return cached["df"], cached["data_date"], cached["raw"]

    # Fetch outside the lock to avoid blocking other threads
    df, data_date, raw = _fetch_candles_from_api(timeframe, bars)

    if df is not None and len(df) > 0:
        with _lock:
            _cache[key] = {
                "df": df,
                "ts": time.time(),
                "raw": raw,
                "data_date": data_date,
            }

    return df, data_date, raw


def _fetch_candles_from_api(timeframe: str, bars: int) -> tuple[pd.DataFrame | None, str | None, list | None]:
    """Fetch candles from AngelOne API. Called at most once per TTL."""
    tf_map = {"1m": "ONE_MINUTE", "5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE"}
    interval = tf_map.get(timeframe, "ONE_MINUTE")

    session = get_session()
    if not session:
        return None, None, None

    today = now_ist()
    from_str = today.strftime("%Y-%m-%d 09:15")
    to_str = today.strftime("%Y-%m-%d %H:%M")

    try:
        resp = session.getCandleData({
            "exchange": config.EXCHANGE,
            "symboltoken": config.NIFTY_TOKEN,
            "interval": interval,
            "fromdate": from_str,
            "todate": to_str,
        })

        if not resp or resp.get("status") is False:
            code = (resp or {}).get("errorcode", "")
            msg = (resp or {}).get("message", "")
            if code == "AB1019":
                log.warning("Rate limited (AB1019) — using cached data if available")
                # Return cached if exists, even if stale
                with _lock:
                    cached = _cache.get(timeframe)
                    if cached:
                        return cached["df"], cached["data_date"], cached["raw"]
            log.warning("getCandleData failed: %s (code=%s)", msg, code)
            return None, None, None

        raw_data = resp.get("data") or []
        if len(raw_data) < 10:
            return None, None, None

        rows = []
        for bar in raw_data:
            if len(bar) >= 6:
                rows.append({
                    "timestamp": str(bar[0]),
                    "open": float(bar[1]),
                    "high": float(bar[2]),
                    "low": float(bar[3]),
                    "close": float(bar[4]),
                    "volume": int(bar[5]),
                })

        data_date = today.strftime("%d %b %Y")
        df = pd.DataFrame(rows[-bars:])
        for col in ("close", "open", "high", "low", "volume"):
            df[col] = df[col].astype(float)

        return df, data_date, rows

    except Exception as exc:
        log.warning("Candle fetch error: %s", exc)
        return None, None, None

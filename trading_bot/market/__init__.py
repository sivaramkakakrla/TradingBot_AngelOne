"""
market/ — Live market data helpers for Project Candles.

Provides real-time NIFTY LTP and OHLC via AngelOne SmartAPI polling.
"""

import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from trading_bot import config
from trading_bot.utils.logger import get_logger

log = get_logger(__name__)

IST = ZoneInfo(config.TIMEZONE)


@dataclass
class MarketTick:
    """Snapshot of the latest NIFTY market data."""
    ltp: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0          # previous day close
    volume: int = 0
    timestamp: str = ""         # ISO-8601 IST
    fetched_at: str = ""        # when we polled


# ── Module-level state ────────────────────────────────────────────────────────
_lock = threading.Lock()
_latest: MarketTick = MarketTick()
_latest_sensex: MarketTick = MarketTick()
_running = False
_thread: threading.Thread | None = None


def get_latest_tick() -> MarketTick:
    """Return the most recent NIFTY market snapshot (thread-safe)."""
    with _lock:
        return _latest


def get_latest_sensex_tick() -> MarketTick:
    """Return the most recent SENSEX market snapshot (thread-safe)."""
    with _lock:
        return _latest_sensex


def _poll_once(session) -> MarketTick | None:
    """Fetch NIFTY LTP + OHLC from AngelOne. Returns MarketTick or None."""
    try:
        resp = session.ltpData(
            exchange=config.EXCHANGE,
            tradingsymbol=config.UNDERLYING,
            symboltoken=config.NIFTY_TOKEN,
        )
        if not resp or resp.get("status") is False:
            log.debug("LTP call returned: %s", resp)
            return None

        d = resp.get("data", {})
        now_str = datetime.now(tz=IST).isoformat()

        return MarketTick(
            ltp=float(d.get("ltp", 0)),
            open=float(d.get("open", 0)),
            high=float(d.get("high", 0)),
            low=float(d.get("low", 0)),
            close=float(d.get("close", 0)),
            volume=0,
            timestamp=now_str,
            fetched_at=now_str,
        )
    except Exception as exc:
        log.warning("LTP poll error: %s", exc)
        return None


def _poll_sensex_once(session) -> MarketTick | None:
    """Fetch SENSEX LTP + OHLC from AngelOne."""
    try:
        # Try BSE first, then NSE fallback for SENSEX
        for exch, sym, tok in [
            ("BSE", "SENSEX", config.SENSEX_TOKEN),
            ("NSE", "SENSEX", config.SENSEX_TOKEN),
        ]:
            try:
                resp = session.ltpData(
                    exchange=exch,
                    tradingsymbol=sym,
                    symboltoken=tok,
                )
                if resp and resp.get("status") is not False:
                    d = resp.get("data", {})
                    ltp = float(d.get("ltp", 0))
                    if ltp > 0:
                        now_str = datetime.now(tz=IST).isoformat()
                        return MarketTick(
                            ltp=ltp,
                            open=float(d.get("open", 0)),
                            high=float(d.get("high", 0)),
                            low=float(d.get("low", 0)),
                            close=float(d.get("close", 0)),
                            volume=0,
                            timestamp=now_str,
                            fetched_at=now_str,
                        )
            except Exception:
                continue
        log.debug("SENSEX LTP: no valid response from any exchange")
        return None
    except Exception as exc:
        log.warning("SENSEX LTP poll error: %s", exc)
        return None


def _poll_loop(session, interval: float):
    """Background loop that polls LTP every `interval` seconds."""
    global _latest, _latest_sensex, _running
    log.info("Market feed started — polling every %.1fs", interval)
    while _running:
        tick = _poll_once(session)
        sensex_tick = _poll_sensex_once(session)
        with _lock:
            if tick:
                _latest = tick
            if sensex_tick:
                _latest_sensex = sensex_tick
        _time.sleep(interval)
    log.info("Market feed stopped.")


def start_feed(session, interval: float = 2.0) -> threading.Thread:
    """Start background polling thread. Returns the thread."""
    global _running, _thread, _session_ref
    if _running and _thread and _thread.is_alive():
        return _thread
    _session_ref = session
    _running = True
    _thread = threading.Thread(
        target=_poll_loop, args=(session, interval),
        name="market-feed", daemon=True,
    )
    _thread.start()
    return _thread


def stop_feed():
    """Signal the polling thread to stop."""
    global _running
    _running = False


# ── Option LTP helpers ────────────────────────────────────────────────────────

_session_ref = None          # cached session for option LTP calls


def _get_session():
    """Return the cached SmartConnect session (set by start_feed)."""
    global _session_ref
    if _session_ref is not None:
        return _session_ref
    try:
        from trading_bot.auth.login import get_session
        _session_ref = get_session()
    except Exception:
        pass
    return _session_ref


def fetch_live_once() -> tuple[MarketTick, MarketTick]:
    """
    One-shot fetch of NIFTY + SENSEX ticks (authenticates if needed).
    Updates module-level cache and returns (nifty_tick, sensex_tick).
    Used by serverless environments where background threads can't run.
    """
    global _latest, _latest_sensex
    session = _get_session()
    if not session:
        return _latest, _latest_sensex
    tick = _poll_once(session)
    sensex_tick = _poll_sensex_once(session)
    with _lock:
        if tick:
            _latest = tick
        if sensex_tick:
            _latest_sensex = sensex_tick
    return _latest, _latest_sensex


def fetch_option_ltp(tokens: list[str]) -> dict[str, float]:
    """
    Fetch LTP for a list of NFO option tokens.
    Returns {token: ltp}.
    """
    session = _get_session()
    if not session or not tokens:
        return {}
    from trading_bot.options import get_option_ltp
    return get_option_ltp(session, tokens)

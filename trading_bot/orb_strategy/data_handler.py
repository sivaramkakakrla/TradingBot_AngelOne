from __future__ import annotations

import datetime
import time
from typing import Any

import pandas as pd

from trading_bot import config
from trading_bot.auth.login import get_session
from trading_bot.market import get_latest_tick, fetch_live_once
from trading_bot.options import get_option_ltp
from trading_bot.utils.logger import get_logger

from .config import ORBConfig

log = get_logger(__name__)


class ORBDataHandler:
    def __init__(self, cfg: ORBConfig):
        self.cfg = cfg

    def _retry(self, fn, *args, **kwargs):
        last_exc = None
        for _ in range(self.cfg.retry_attempts):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                time.sleep(self.cfg.retry_sleep_seconds)
        if last_exc:
            raise last_exc
        raise RuntimeError("retry failed")

    def get_1m_candles_today(self) -> pd.DataFrame | None:
        session = get_session()
        now = datetime.datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        params = {
            "exchange": config.EXCHANGE,
            "symboltoken": config.NIFTY_TOKEN,
            "interval": self.cfg.candle_interval,
            "fromdate": f"{date_str} 09:15",
            "todate": f"{date_str} 15:30",
        }

        resp = self._retry(session.getCandleData, params)
        if not resp or resp.get("status") is False:
            return None
        data = resp.get("data") or []
        if not data:
            return None

        rows = []
        for b in data:
            if len(b) < 6:
                continue
            rows.append({
                "timestamp": str(b[0]),
                "open": float(b[1]),
                "high": float(b[2]),
                "low": float(b[3]),
                "close": float(b[4]),
                "volume": float(b[5]),
            })

        if not rows:
            return None
        return pd.DataFrame(rows)

    def get_spot_ltp(self) -> float:
        tick = get_latest_tick()
        if tick.ltp > 0:
            return float(tick.ltp)
        t, _ = fetch_live_once()
        return float(t.ltp)

    def get_option_ltp(self, token: str) -> float:
        session = get_session()
        m = self._retry(get_option_ltp, session, [token])
        return float(m.get(token, 0.0))


class SmartWebSocketAdapter:
    """
    Optional websocket adapter scaffold.

    Strategy engine currently runs on robust 1m candle polling to avoid missing
    candle-closes due to websocket reconnects. This adapter can be wired later for
    lower-latency tick stream enrichment.
    """

    def __init__(self):
        self.is_connected = False

    def start(self):
        self.is_connected = True
        log.info("SmartWebSocket adapter placeholder started")

    def stop(self):
        self.is_connected = False
        log.info("SmartWebSocket adapter placeholder stopped")

from __future__ import annotations

import datetime
import pandas as pd

from trading_bot.auth.login import get_session
from trading_bot.candle_cache import get_candles
from trading_bot.market import get_latest_tick, fetch_live_once
from trading_bot.options import get_option_ltp
from trading_bot.utils.time_utils import now_ist


class DataFeed:
    """
    5-minute candles + live LTP provider.

    Uses existing cache/feed in this codebase; can be swapped to true SmartWebSocketV2
    later without changing strategy logic.
    """

    def get_5m(self, bars: int = 120) -> pd.DataFrame | None:
        df, _, _ = get_candles("5m", bars)
        return df

    def get_nifty_ltp(self) -> float:
        tick = get_latest_tick()
        if tick.ltp > 0:
            return float(tick.ltp)
        tick, _ = fetch_live_once()
        return float(tick.ltp)

    def get_option_ltp(self, token: str) -> float:
        session = get_session()
        m = get_option_ltp(session, [token])
        return float(m.get(token, 0.0))

    @staticmethod
    def now_hhmm() -> str:
        return now_ist().strftime("%H:%M")

    @staticmethod
    def today_str() -> str:
        return now_ist().strftime("%Y-%m-%d")

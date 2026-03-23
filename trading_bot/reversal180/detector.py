from __future__ import annotations

from dataclasses import dataclass
import pandas as pd

from .config import Reversal180Config
from .models import ORBRange, ReversalSignal


@dataclass(slots=True)
class BreakoutState:
    up_seen: bool = False
    down_seen: bool = False
    up_break_high: float = 0.0
    up_break_low: float = 0.0
    down_break_high: float = 0.0
    down_break_low: float = 0.0
    last_signal_bar: str = ""


def _hhmm(ts: str) -> str:
    s = str(ts).replace("T", " ")
    t = s.split(" ")[-1]
    return t[:5]


def _body_ratio(row: pd.Series) -> float:
    h = float(row["high"])
    l = float(row["low"])
    o = float(row["open"])
    c = float(row["close"])
    rng = max(h - l, 1e-9)
    return abs(c - o) / rng


def _is_opposite_strong(row: pd.Series, direction: str, min_ratio: float) -> bool:
    o = float(row["open"])
    c = float(row["close"])
    r = _body_ratio(row)
    if direction == "BEARISH":
        return c < o and r >= min_ratio
    return c > o and r >= min_ratio


def _volume_ok(df: pd.DataFrame, idx: int, cfg: Reversal180Config) -> bool:
    if not cfg.require_volume_spike:
        return True
    if "volume" not in df.columns:
        return False
    if idx < cfg.volume_lookback:
        return False
    look = df["volume"].iloc[idx - cfg.volume_lookback:idx]
    avg = float(look.mean()) if len(look) > 0 else 0.0
    if avg <= 0:
        return False
    cur = float(df["volume"].iloc[idx])
    return cur >= avg * cfg.volume_spike_mult


def generate_failed_breakout_signal(
    df_5m: pd.DataFrame,
    orb: ORBRange,
    state: BreakoutState,
    cfg: Reversal180Config,
) -> ReversalSignal | None:
    """
    180-degree reversal logic:
      1) observe breakout above/below ORB
      2) wait for close back inside range with strong opposite candle
      3) emit BUY_PE after failed upside breakout, BUY_CE after failed downside breakout
    """
    if df_5m is None or len(df_5m) < 2:
        return None

    i = len(df_5m) - 1
    row = df_5m.iloc[i]
    ts = str(row["timestamp"])
    hhmm = _hhmm(ts)

    if hhmm < cfg.signal_start or hhmm > cfg.last_entry_time:
        return None
    if state.last_signal_bar == ts:
        return None

    h = float(row["high"])
    l = float(row["low"])
    c = float(row["close"])

    # breakout observations
    if h > orb.high:
        state.up_seen = True
        state.up_break_high = h
        state.up_break_low = l
    if l < orb.low:
        state.down_seen = True
        state.down_break_high = h
        state.down_break_low = l

    inside_orb = orb.low <= c <= orb.high

    # Failed upside breakout -> BUY PE
    if state.up_seen and inside_orb:
        if _is_opposite_strong(row, "BEARISH", cfg.min_reversal_body_ratio) and _volume_ok(df_5m, i, cfg):
            state.up_seen = False
            state.last_signal_bar = ts
            return ReversalSignal(
                timestamp=ts,
                side="BUY_PE",
                reason="Failed upside breakout: close back inside ORB with strong bearish candle",
                orb_high=orb.high,
                orb_low=orb.low,
                breakout_high=state.up_break_high,
                breakout_low=state.up_break_low,
                underlying_price=c,
            )

    # Failed downside breakout -> BUY CE
    if state.down_seen and inside_orb:
        if _is_opposite_strong(row, "BULLISH", cfg.min_reversal_body_ratio) and _volume_ok(df_5m, i, cfg):
            state.down_seen = False
            state.last_signal_bar = ts
            return ReversalSignal(
                timestamp=ts,
                side="BUY_CE",
                reason="Failed downside breakout: close back inside ORB with strong bullish candle",
                orb_high=orb.high,
                orb_low=orb.low,
                breakout_high=state.down_break_high,
                breakout_low=state.down_break_low,
                underlying_price=c,
            )

    return None

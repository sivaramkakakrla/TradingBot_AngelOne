"""
candles/ — Candlestick pattern recognition engine for Project Candles.

Detects high-probability "confirmed" patterns on a pandas DataFrame
of OHLCV data.  Each detector returns a Series of signals:
    +1  = bullish pattern confirmed
    -1  = bearish pattern confirmed
     0  = no pattern

A pattern is "confirmed" only when the candle after the pattern
closes in the expected direction.

Public API
----------
    detect_all(df)  -> dict[pattern_name, pd.Series]
    scan_signals(df) -> list[dict]   (human-readable signal list)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from trading_bot import config

# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _body(o: pd.Series, c: pd.Series) -> pd.Series:
    return (c - o).abs()

def _range(h: pd.Series, l: pd.Series) -> pd.Series:
    return (h - l).replace(0, 1e-9)

def _upper_wick(o: pd.Series, h: pd.Series, c: pd.Series) -> pd.Series:
    return h - np.maximum(o, c)

def _lower_wick(o: pd.Series, l: pd.Series, c: pd.Series) -> pd.Series:
    return np.minimum(o, c) - l

def _is_bullish(o: pd.Series, c: pd.Series) -> pd.Series:
    return c > o

def _is_bearish(o: pd.Series, c: pd.Series) -> pd.Series:
    return c < o

def _avg_body(o: pd.Series, c: pd.Series, n: int = 20) -> pd.Series:
    return _body(o, c).rolling(n, min_periods=1).mean()

def _avg_range(h: pd.Series, l: pd.Series, n: int = 20) -> pd.Series:
    return _range(h, l).rolling(n, min_periods=1).mean()


# ═══════════════════════════════════════════════════════════════════════════════
#  SINGLE-CANDLE PATTERNS
# ═══════════════════════════════════════════════════════════════════════════════

def hammer(df: pd.DataFrame) -> pd.Series:
    """
    Hammer (bullish reversal at bottom).
    - Small real body in upper third of range
    - Lower wick >= 2x body
    - Little/no upper wick
    Confirmed: next candle closes above hammer's high.
    """
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = _body(o, c)
    rng = _range(h, l)
    lw = _lower_wick(o, l, c)
    uw = _upper_wick(o, h, c)

    raw = (
        (lw >= 2 * body) &
        (uw <= body * 0.5) &
        (body <= rng * 0.35)
    )
    # Confirm: next candle closes above this candle's high
    confirm = df["close"].shift(-1) > h
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[raw & confirm] = 1
    return sig


def shooting_star(df: pd.DataFrame) -> pd.Series:
    """
    Shooting Star (bearish reversal at top).
    - Small body in lower third
    - Upper wick >= 2x body
    - Little/no lower wick
    Confirmed: next candle closes below star's low.
    """
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = _body(o, c)
    rng = _range(h, l)
    uw = _upper_wick(o, h, c)
    lw = _lower_wick(o, l, c)

    raw = (
        (uw >= 2 * body) &
        (lw <= body * 0.5) &
        (body <= rng * 0.35)
    )
    confirm = df["close"].shift(-1) < l
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[raw & confirm] = -1
    return sig


def doji(df: pd.DataFrame) -> pd.Series:
    """
    Doji — indecision candle. Body < 10% of range.
    Not a signal by itself; used as input for doji-star combos.
    Returns 1 where doji detected (direction neutral).
    """
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = _body(o, c)
    rng = _range(h, l)
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[body <= rng * 0.10] = 1
    return sig


def marubozu(df: pd.DataFrame) -> pd.Series:
    """
    Marubozu — strong conviction candle.
    Body > 80% of range (almost no wicks).
    +1 bullish marubozu, -1 bearish.
    Confirmed by itself (the candle IS the conviction).
    """
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = _body(o, c)
    rng = _range(h, l)
    strong = body >= rng * 0.80

    sig = pd.Series(0, index=df.index, dtype=int)
    sig[strong & _is_bullish(o, c)] = 1
    sig[strong & _is_bearish(o, c)] = -1
    return sig


# ═══════════════════════════════════════════════════════════════════════════════
#  TWO-CANDLE PATTERNS
# ═══════════════════════════════════════════════════════════════════════════════

def bullish_engulfing(df: pd.DataFrame) -> pd.Series:
    """
    Bullish Engulfing — bearish candle followed by larger bullish candle
    that fully engulfs the prior body.
    Confirmed: engulfing candle's close is the confirmation.
    """
    o, c = df["open"], df["close"]
    prev_o, prev_c = o.shift(1), c.shift(1)

    raw = (
        _is_bearish(prev_o, prev_c) &          # prior was red
        _is_bullish(o, c) &                     # current is green
        (o <= prev_c) &                         # opens at/below prior close
        (c >= prev_o)                           # closes at/above prior open
    )
    # Body size confirmation: current body > prior body
    curr_body = _body(o, c)
    prev_body = _body(prev_o, prev_c)
    raw = raw & (curr_body > prev_body)

    sig = pd.Series(0, index=df.index, dtype=int)
    sig[raw] = 1
    return sig


def bearish_engulfing(df: pd.DataFrame) -> pd.Series:
    """
    Bearish Engulfing — bullish candle followed by larger bearish candle
    that fully engulfs the prior body.
    """
    o, c = df["open"], df["close"]
    prev_o, prev_c = o.shift(1), c.shift(1)

    raw = (
        _is_bullish(prev_o, prev_c) &
        _is_bearish(o, c) &
        (o >= prev_c) &
        (c <= prev_o)
    )
    curr_body = _body(o, c)
    prev_body = _body(prev_o, prev_c)
    raw = raw & (curr_body > prev_body)

    sig = pd.Series(0, index=df.index, dtype=int)
    sig[raw] = -1
    return sig


def piercing_line(df: pd.DataFrame) -> pd.Series:
    """
    Piercing Line (bullish) — bearish candle, then bullish candle that
    opens below prior low and closes above 50% of prior body.
    """
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    prev_o, prev_c, prev_l = o.shift(1), c.shift(1), l.shift(1)
    prev_mid = (prev_o + prev_c) / 2

    raw = (
        _is_bearish(prev_o, prev_c) &
        _is_bullish(o, c) &
        (o < prev_l) &
        (c > prev_mid) &
        (c < prev_o)
    )
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[raw] = 1
    return sig


def dark_cloud_cover(df: pd.DataFrame) -> pd.Series:
    """
    Dark Cloud Cover (bearish) — bullish candle, then bearish candle that
    opens above prior high and closes below 50% of prior body.
    """
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    prev_o, prev_c, prev_h = o.shift(1), c.shift(1), h.shift(1)
    prev_mid = (prev_o + prev_c) / 2

    raw = (
        _is_bullish(prev_o, prev_c) &
        _is_bearish(o, c) &
        (o > prev_h) &
        (c < prev_mid) &
        (c > prev_o)
    )
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[raw] = -1
    return sig


# ═══════════════════════════════════════════════════════════════════════════════
#  THREE-CANDLE PATTERNS
# ═══════════════════════════════════════════════════════════════════════════════

def morning_star(df: pd.DataFrame) -> pd.Series:
    """
    Morning Star (bullish reversal) — 3-candle pattern:
    1. Large bearish candle
    2. Small-body candle (star) that gaps down
    3. Large bullish candle that closes above midpoint of candle 1
    """
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    o1, c1 = o.shift(2), c.shift(2)
    o2, c2, h2, l2 = o.shift(1), c.shift(1), h.shift(1), l.shift(1)
    avg_b = _avg_body(o, c)

    # Candle 1: large bearish
    big1 = _is_bearish(o1, c1) & (_body(o1, c1) > avg_b.shift(2))
    # Candle 2: small body, gaps down from candle 1 close
    small2 = _body(o2, c2) < avg_b.shift(1) * 0.5
    gap2 = np.maximum(o2, c2) < c1
    # Candle 3: bullish, closes above midpoint of candle 1 body
    mid1 = (o1 + c1) / 2
    big3 = _is_bullish(o, c) & (c > mid1) & (_body(o, c) > avg_b)

    sig = pd.Series(0, index=df.index, dtype=int)
    sig[big1 & small2 & gap2 & big3] = 1
    return sig


def evening_star(df: pd.DataFrame) -> pd.Series:
    """
    Evening Star (bearish reversal) — 3-candle pattern:
    1. Large bullish candle
    2. Small-body candle (star) that gaps up
    3. Large bearish candle that closes below midpoint of candle 1
    """
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    o1, c1 = o.shift(2), c.shift(2)
    o2, c2 = o.shift(1), c.shift(1)
    avg_b = _avg_body(o, c)

    big1 = _is_bullish(o1, c1) & (_body(o1, c1) > avg_b.shift(2))
    small2 = _body(o2, c2) < avg_b.shift(1) * 0.5
    gap2 = np.minimum(o2, c2) > c1
    mid1 = (o1 + c1) / 2
    big3 = _is_bearish(o, c) & (c < mid1) & (_body(o, c) > avg_b)

    sig = pd.Series(0, index=df.index, dtype=int)
    sig[big1 & small2 & gap2 & big3] = -1
    return sig


def three_white_soldiers(df: pd.DataFrame) -> pd.Series:
    """
    Three White Soldiers — three consecutive bullish candles, each
    opening within prior body and closing progressively higher.
    """
    o, c = df["open"], df["close"]
    o1, c1 = o.shift(2), c.shift(2)
    o2, c2 = o.shift(1), c.shift(1)

    raw = (
        _is_bullish(o1, c1) &
        _is_bullish(o2, c2) &
        _is_bullish(o, c) &
        (o2 > o1) & (o2 < c1) &     # candle 2 opens within candle 1 body
        (o > o2) & (o < c2) &        # candle 3 opens within candle 2 body
        (c2 > c1) &                   # progressive higher closes
        (c > c2)
    )
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[raw] = 1
    return sig


def three_black_crows(df: pd.DataFrame) -> pd.Series:
    """
    Three Black Crows — three consecutive bearish candles, each
    opening within prior body and closing progressively lower.
    """
    o, c = df["open"], df["close"]
    o1, c1 = o.shift(2), c.shift(2)
    o2, c2 = o.shift(1), c.shift(1)

    raw = (
        _is_bearish(o1, c1) &
        _is_bearish(o2, c2) &
        _is_bearish(o, c) &
        (o2 < o1) & (o2 > c1) &
        (o < o2) & (o > c2) &
        (c2 < c1) &
        (c < c2)
    )
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[raw] = -1
    return sig


# ═══════════════════════════════════════════════════════════════════════════════
#  MOMENTUM / STRUCTURE PATTERNS
# ═══════════════════════════════════════════════════════════════════════════════

def strong_momentum_candle(df: pd.DataFrame) -> pd.Series:
    """
    Momentum burst — candle with:
    - Body > STRONG_BODY_RATIO of range
    - Range > RANGE_BURST_MULTIPLIER * avg range
    - Volume > VOLUME_BURST_MULTIPLIER * avg volume (if volume available)
    """
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = _body(o, c)
    rng = _range(h, l)
    avg_r = _avg_range(h, l, config.AVG_LOOKBACK)

    strong_body = body >= rng * config.STRONG_BODY_RATIO
    range_burst = rng >= avg_r * config.RANGE_BURST_MULTIPLIER

    vol_ok = pd.Series(True, index=df.index)
    if "volume" in df.columns:
        vol = df["volume"].astype(float)
        avg_vol = vol.rolling(config.AVG_LOOKBACK, min_periods=1).mean()
        vol_ok = vol >= avg_vol * config.VOLUME_BURST_MULTIPLIER

    raw = strong_body & range_burst & vol_ok
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[raw & _is_bullish(o, c)] = 1
    sig[raw & _is_bearish(o, c)] = -1
    return sig


def wick_rejection(df: pd.DataFrame) -> pd.Series:
    """
    Wick rejection — long wick showing strong rejection of a level.
    Bullish: lower wick >= WICK_REJECTION_MULTIPLIER * body, small upper wick
    Bearish: upper wick >= WICK_REJECTION_MULTIPLIER * body, small lower wick
    Confirmed by next candle closing in the rejection direction.
    """
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = _body(o, c)
    lw = _lower_wick(o, l, c)
    uw = _upper_wick(o, h, c)
    mult = config.WICK_REJECTION_MULTIPLIER

    bull_wick = (lw >= mult * body) & (uw < body)
    bear_wick = (uw >= mult * body) & (lw < body)

    next_close = c.shift(-1)
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[bull_wick & (next_close > c)] = 1
    sig[bear_wick & (next_close < c)] = -1
    return sig


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════════

_PATTERN_FUNCS = {
    "hammer":              hammer,
    "shooting_star":       shooting_star,
    "marubozu":            marubozu,
    "bullish_engulfing":   bullish_engulfing,
    "bearish_engulfing":   bearish_engulfing,
    "piercing_line":       piercing_line,
    "dark_cloud_cover":    dark_cloud_cover,
    "morning_star":        morning_star,
    "evening_star":        evening_star,
    "three_white_soldiers":three_white_soldiers,
    "three_black_crows":   three_black_crows,
    "strong_momentum":     strong_momentum_candle,
    "wick_rejection":      wick_rejection,
}

# Reliability weight for scoring (higher = more reliable)
_PATTERN_WEIGHT = {
    "hammer": 2, "shooting_star": 2,
    "marubozu": 3,
    "bullish_engulfing": 3, "bearish_engulfing": 3,
    "piercing_line": 2, "dark_cloud_cover": 2,
    "morning_star": 4, "evening_star": 4,
    "three_white_soldiers": 4, "three_black_crows": 4,
    "strong_momentum": 3,
    "wick_rejection": 2,
}

# Human-readable descriptions for each pattern
_PATTERN_DESC = {
    "hammer": "Hammer — Bullish reversal at support. Long lower wick shows buyers rejected lower prices aggressively.",
    "shooting_star": "Shooting Star — Bearish reversal at resistance. Long upper wick shows sellers rejected higher prices.",
    "marubozu": "Marubozu — Strong momentum candle with little/no wicks. Full body indicates dominant buying (green) or selling (red) pressure.",
    "bullish_engulfing": "Bullish Engulfing — Large green candle fully engulfs prior red candle. Strong reversal signal showing buyers overwhelmed sellers.",
    "bearish_engulfing": "Bearish Engulfing — Large red candle fully engulfs prior green candle. Strong reversal signal showing sellers overwhelmed buyers.",
    "piercing_line": "Piercing Line — Green candle opens below prior low but closes above midpoint. Bullish reversal showing buying pressure after a gap down.",
    "dark_cloud_cover": "Dark Cloud Cover — Red candle opens above prior high but closes below midpoint. Bearish reversal showing selling pressure after a gap up.",
    "morning_star": "Morning Star — 3-bar bullish reversal: large red → small body (indecision) → large green. Very reliable bottom reversal pattern.",
    "evening_star": "Evening Star — 3-bar bearish reversal: large green → small body (indecision) → large red. Very reliable top reversal pattern.",
    "three_white_soldiers": "Three White Soldiers — Three consecutive large green candles. Powerful bullish continuation showing sustained buying pressure.",
    "three_black_crows": "Three Black Crows — Three consecutive large red candles. Powerful bearish continuation showing sustained selling pressure.",
    "strong_momentum": "Strong Momentum — Unusually large body relative to recent candles. Indicates a breakout or institutional activity.",
    "wick_rejection": "Wick Rejection — Long wick on one side with small body. Price tested a level but was strongly rejected, indicating reversal.",
}


def detect_all(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Run every pattern detector. Returns {name: signal_series}."""
    if len(df) < 5:
        return {}
    return {name: func(df) for name, func in _PATTERN_FUNCS.items()}


def scan_signals(df: pd.DataFrame) -> list[dict]:
    """
    Scan the latest candles and return a list of active (non-zero) signals
    at the most recent confirmed bar.

    Each dict: {
        "pattern": str, "direction": "BULLISH"|"BEARISH",
        "weight": int,  "bar_index": int
    }

    The last bar is index -1 (latest candle). We check the last 3 bars
    since some patterns confirm 1 bar later.
    """
    if len(df) < 5:
        return []

    patterns = detect_all(df)
    signals = []

    # Check last 3 bars for recently confirmed signals
    for name, series in patterns.items():
        for offset in range(1, 4):
            idx = len(df) - offset
            if idx < 0:
                continue
            val = series.iloc[idx]
            if val != 0:
                signals.append({
                    "pattern": name,
                    "direction": "BULLISH" if val > 0 else "BEARISH",
                    "weight": _PATTERN_WEIGHT.get(name, 1),
                    "bar_index": idx,
                    "bars_ago": offset - 1,
                })

    # Sort by weight descending, then most recent first
    signals.sort(key=lambda s: (-s["weight"], s["bars_ago"]))
    return signals

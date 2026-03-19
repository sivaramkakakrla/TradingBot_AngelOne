"""
strategy/ — Signal engine for Project Candles.

Combines candlestick pattern detection with multi-indicator confirmations
to produce high-conviction ("sure shot") trade signals.

A signal is CONFIRMED only when:
    1. A candlestick pattern fires  (candles module)
    2. At least 2 of 5 indicator filters agree (confluence)
    3. The candle is within an allowed trade window

Indicator Filters
-----------------
    RSI        — above BULL threshold for longs / below BEAR for shorts
    MACD       — histogram positive for longs / negative for shorts
    Supertrend — direction aligned with signal
    EMA Trend  — fast EMA above slow EMA for longs / below for shorts
    Volume     — current bar volume > VOLUME_EXPANSION_MULT × avg volume

Strength Score (0–100)
----------------------
    pattern_weight × 5  +  confirmed_filters × 10  +  volume_bonus (10)

Public API
----------
    evaluate(df)           -> list[Signal]
    Signal dataclass       — direction, strength, patterns, filters, action, reason
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import List

import pandas as pd

from trading_bot import config
from trading_bot.candles import detect_all, scan_signals, _PATTERN_WEIGHT, _PATTERN_DESC
from trading_bot.indicators import (
    rsi as calc_rsi,
    macd as calc_macd,
    supertrend as calc_supertrend,
    ema as calc_ema,
    volume_sma as calc_vol_sma,
    atr as calc_atr,
)
from trading_bot.data.store import insert_signal
from trading_bot.utils.logger import get_logger
from trading_bot.utils.time_utils import now_ist

log = get_logger(__name__)

MIN_CONFIRMATIONS = 2          # need ≥2 filters to call it "confirmed"
MAX_STRENGTH = 100


# ═══════════════════════════════════════════════════════════════════════════════
#  SIGNAL DATA CLASS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Signal:
    direction: str                       # "BULLISH" | "BEARISH"
    strength: int                        # 0–100 composite score
    patterns: list[str]                  # pattern names that fired
    filters: dict[str, bool]             # {filter_name: pass/fail}
    confirmations: int                   # how many filters passed
    action: str                          # "ENTER" | "SKIP"
    reason: str                          # human readable explanation
    sl_points: float = 0.0              # suggested stop-loss distance
    target_points: float = 0.0          # suggested target distance
    bar_timestamp: str = ""             # timestamp of the signal bar
    entry_price: float = 0.0            # close of the signal bar (entry level)
    bar_index: int = -1                 # index in the DF for backtesting
    pattern_descriptions: list[str] = field(default_factory=list)  # human-readable pattern explanations
    expected_profit_pts: float = 0.0    # expected profit in points (= target_points)

    def to_dict(self) -> dict:
        return asdict(self)


# ═══════════════════════════════════════════════════════════════════════════════
#  INDICATOR FILTER CHECKS
# ═══════════════════════════════════════════════════════════════════════════════

def _check_rsi(rsi_val: float, direction: str) -> bool:
    if pd.isna(rsi_val):
        return False
    if direction == "BULLISH":
        return rsi_val >= config.RSI_BULL_THRESHOLD
    return rsi_val <= config.RSI_BEAR_THRESHOLD


def _check_macd(hist_val: float, direction: str) -> bool:
    if pd.isna(hist_val):
        return False
    if direction == "BULLISH":
        return hist_val > 0
    return hist_val < 0


def _check_supertrend(st_dir: int, direction: str) -> bool:
    if pd.isna(st_dir):
        return False
    if direction == "BULLISH":
        return st_dir == 1
    return st_dir == -1


def _check_ema_trend(ema_fast_val: float, ema_slow_val: float, direction: str) -> bool:
    if pd.isna(ema_fast_val) or pd.isna(ema_slow_val):
        return False
    if direction == "BULLISH":
        return ema_fast_val > ema_slow_val
    return ema_fast_val < ema_slow_val


def _check_volume(vol: float, avg_vol: float) -> bool:
    if pd.isna(vol) or pd.isna(avg_vol) or avg_vol == 0:
        return False
    return vol >= avg_vol * config.VOLUME_EXPANSION_MULT


# ═══════════════════════════════════════════════════════════════════════════════
#  TIME WINDOW CHECK
# ═══════════════════════════════════════════════════════════════════════════════

def _in_trade_window() -> bool:
    """Check if current IST time is within allowed trade windows."""
    now = now_ist()
    now_time = now.strftime("%H:%M")
    for start, end in config.TRADE_WINDOWS:
        if start <= now_time <= end:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
#  STRENGTH SCORING
# ═══════════════════════════════════════════════════════════════════════════════

def _calc_strength(pattern_names: list[str], confirmations: int, vol_ok: bool) -> int:
    """
    Compute composite strength score 0–100.
    pattern weight × 5  +  confirmations × 10  +  volume bonus (10)
    """
    max_weight = max((_PATTERN_WEIGHT.get(p, 1) for p in pattern_names), default=1)
    score = max_weight * 5 + confirmations * 10
    if vol_ok:
        score += 10
    return min(score, MAX_STRENGTH)


# ═══════════════════════════════════════════════════════════════════════════════
#  SL / TARGET CALCULATION
# ═══════════════════════════════════════════════════════════════════════════════

def _calc_sl_target(atr_val: float) -> tuple[float, float]:
    """
    SL = 1.5 × ATR (or config default), Target = 2 × SL (1:2 R:R).
    """
    if pd.isna(atr_val) or atr_val <= 0:
        sl = config.INITIAL_SL_POINTS
    else:
        sl = round(max(atr_val * 1.5, config.INITIAL_SL_POINTS), 2)
    target = round(sl * 2, 2)
    return sl, target


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN EVALUATION
# ═══════════════════════════════════════════════════════════════════════════════

def evaluate(df: pd.DataFrame, backtest: bool = False) -> list[Signal]:
    """
    Evaluate the most recent candle data for confirmed signals.

    Parameters
    ----------
    df : DataFrame with OHLCV columns (timestamp, open, high, low, close, volume)
         Must have at least 20 rows for meaningful indicator computation.
    backtest : if True, skip trade-window check (all signals get ENTER/SKIP
               based on confirmations only).

    Returns
    -------
    List of Signal objects (usually 0 or 1 per evaluation, but can be multiple
    if several patterns fire simultaneously).
    """
    if len(df) < 20:
        return []

    # ── Step 1: Detect candle patterns ────────────────────────────────────
    raw_signals = scan_signals(df)
    if not raw_signals:
        return []

    # ── Step 2: Compute indicators on the full DF (once) ─────────────────
    close = df["close"]
    rsi_s = calc_rsi(close, config.RSI_PERIOD)
    macd_df = calc_macd(close)
    st_df = calc_supertrend(df, config.SUPERTREND_PERIOD, config.SUPERTREND_MULTIPLIER)
    ema_fast = calc_ema(close, config.EMA_FAST)
    ema_slow = calc_ema(close, config.EMA_SLOW)
    atr_s = calc_atr(df)

    vol_avg = None
    has_volume = "volume" in df.columns and df["volume"].sum() > 0
    if has_volume:
        vol_avg = calc_vol_sma(df, config.AVG_LOOKBACK)

    # ── Step 3: Group patterns by direction (latest bar window) ──────────
    # Collapse multiple patterns in same direction into one signal
    directions: dict[str, list[dict]] = {}
    for sig in raw_signals:
        d = sig["direction"]
        directions.setdefault(d, []).append(sig)

    results: list[Signal] = []

    for direction, pattern_group in directions.items():
        # Use the most recent bar among these patterns
        ref_idx = max(p["bar_index"] for p in pattern_group)
        pattern_names = list({p["pattern"] for p in pattern_group})

        # ── Step 4: Run indicator filters at the reference bar ───────
        rsi_val = rsi_s.iloc[ref_idx]
        macd_hist = macd_df["macd_histogram"].iloc[ref_idx]
        st_dir = st_df["supertrend_direction"].iloc[ref_idx]
        ef = ema_fast.iloc[ref_idx]
        es = ema_slow.iloc[ref_idx]
        atr_val = atr_s.iloc[ref_idx]

        vol_ok = False
        if has_volume and vol_avg is not None:
            vol_ok = _check_volume(
                df["volume"].iloc[ref_idx],
                vol_avg.iloc[ref_idx],
            )

        filters = {
            "rsi": _check_rsi(rsi_val, direction),
            "macd": _check_macd(macd_hist, direction),
            "supertrend": _check_supertrend(st_dir, direction),
            "ema_trend": _check_ema_trend(ef, es, direction),
            "volume": vol_ok,
        }

        confirmations = sum(filters.values())
        strength = _calc_strength(pattern_names, confirmations, vol_ok)
        sl, target = _calc_sl_target(atr_val)

        # ── Step 5: Decide ENTER vs SKIP ─────────────────────────────
        in_window = backtest or _in_trade_window()
        reasons = []
        if confirmations < MIN_CONFIRMATIONS:
            reasons.append(f"only {confirmations}/{MIN_CONFIRMATIONS} confirmations")
        if not in_window:
            reasons.append("outside trade window")

        if confirmations >= MIN_CONFIRMATIONS and in_window:
            action = "ENTER"
            reason_str = (
                f"{', '.join(pattern_names)} confirmed by "
                f"{confirmations}/5 filters [{', '.join(k for k,v in filters.items() if v)}]"
            )
        else:
            action = "SKIP"
            reason_str = f"Skipped: {'; '.join(reasons)}"

        # Get bar timestamp if available
        bar_ts = ""
        if "timestamp" in df.columns:
            bar_ts = str(df["timestamp"].iloc[ref_idx])

        entry_px = float(df["close"].iloc[ref_idx])

        # Build pattern descriptions list
        pat_descs = [_PATTERN_DESC.get(p, p) for p in pattern_names]

        signal = Signal(
            direction=direction,
            strength=strength,
            patterns=pattern_names,
            filters=filters,
            confirmations=confirmations,
            action=action,
            reason=reason_str,
            sl_points=sl,
            target_points=target,
            bar_timestamp=bar_ts,
            entry_price=entry_px,
            bar_index=int(ref_idx),
            pattern_descriptions=pat_descs,
            expected_profit_pts=target,
        )
        results.append(signal)

        # ── Step 6: Persist to DB ────────────────────────────────────
        try:
            insert_signal({
                "timestamp": bar_ts or now_ist().isoformat(),
                "direction": direction,
                "strength": strength,
                "filters": json.dumps(filters),
                "action": action,
                "reason": reason_str,
            })
        except Exception as e:
            log.warning("Failed to persist signal: %s", e)

        log.info(
            "SIGNAL %s | %s | strength=%d | action=%s | %s",
            direction, pattern_names, strength, action, reason_str,
        )

    # Sort by strength descending
    results.sort(key=lambda s: -s.strength)
    return results


def evaluate_latest(df: pd.DataFrame) -> dict | None:
    """
    Convenience wrapper: run evaluate() and return the top signal as a dict,
    or None if no signal.
    """
    signals = evaluate(df)
    if not signals:
        return None
    return signals[0].to_dict()

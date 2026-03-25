"""
strategy/ — Signal engine for Project Candles.

Combines candlestick pattern detection with multi-indicator confirmations
to produce high-conviction ("sure shot") trade signals.

A signal is CONFIRMED only when:
    1. A candlestick pattern fires  (candles module)
    2. At least 3 of 5 indicator filters agree (raised from 2 — reduces noise)
    3. The candle is within an allowed trade window
    4. Market is NOT sideways (ADX ≥ ADX_SIDEWAYS_THRESHOLD)
    5. Signal direction aligns with 15m HTF bias (when HTF_ENABLED)
    6. Composite strength score ≥ MIN_SIGNAL_STRENGTH

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
    is_sideways_market(df)   -> bool   (ADX-based chop detection)
    get_htf_bias(df_15m)     -> str | None  ("BULLISH" | "BEARISH" | None)
    evaluate(df, df_15m)     -> list[Signal]
    Signal dataclass         — direction, strength, patterns, filters, action, reason
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import List

import pandas as pd

from trading_bot import config
from trading_bot.candles import detect_all, scan_signals, scan_signals_all, _PATTERN_WEIGHT, _PATTERN_DESC
from trading_bot.indicators import (
    rsi as calc_rsi,
    macd as calc_macd,
    supertrend as calc_supertrend,
    ema as calc_ema,
    sma as calc_sma,
    vwap as calc_vwap_ind,
    volume_sma as calc_vol_sma,
    atr as calc_atr,
    adx as calc_adx,
)
from trading_bot.data.store import insert_signal
from trading_bot.utils.logger import get_logger
from trading_bot.utils.time_utils import now_ist

log = get_logger(__name__)

MIN_CONFIRMATIONS = 2          # need ≥2 of 5 filters (lowered from 3 — 3 blocked everything on 1m)
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
#  MARKET REGIME DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def is_sideways_market(df: pd.DataFrame) -> bool:
    """
    Return True when the 1m candles show a choppy/sideways regime.

    Method:
        • ADX(14) on the last bar < ADX_SIDEWAYS_THRESHOLD (default 20)
        • ALSO checks the 20-bar price range as a fraction of ATR:
          if range/ATR < 3 the market is too compressed for clean signals.

    When sideways → caller should skip ALL entry signals.
    """
    if len(df) < 20:
        return False  # not enough data → assume trending (don't block)

    try:
        adx_series = calc_adx(df, config.ADX_PERIOD)
        adx_val = float(adx_series.iloc[-1])
        if pd.isna(adx_val):
            return False
        sideways = adx_val < config.ADX_SIDEWAYS_THRESHOLD
        log.debug("is_sideways_market: ADX=%.1f → %s", adx_val, "SIDEWAYS" if sideways else "TRENDING")
        return sideways
    except Exception as exc:
        log.warning("is_sideways_market error: %s", exc)
        return False  # fail open — don't block on errors


def trend_detection(df_15m: pd.DataFrame) -> dict:
    """Return higher-timeframe trend metrics used for directional bias."""
    if df_15m is None or len(df_15m) < config.HTF_EMA_SLOW + 5:
        return {"bias": None, "ema_fast": None, "ema_slow": None}
    try:
        fast = calc_ema(df_15m["close"], config.HTF_EMA_FAST)
        slow = calc_ema(df_15m["close"], config.HTF_EMA_SLOW)
        f_val = float(fast.iloc[-1])
        s_val = float(slow.iloc[-1])
        if pd.isna(f_val) or pd.isna(s_val):
            return {"bias": None, "ema_fast": None, "ema_slow": None}
        return {
            "bias": "BULLISH" if f_val > s_val else "BEARISH",
            "ema_fast": round(f_val, 2),
            "ema_slow": round(s_val, 2),
        }
    except Exception as exc:
        log.warning("trend_detection error: %s", exc)
        return {"bias": None, "ema_fast": None, "ema_slow": None}


def get_htf_bias(df_15m: pd.DataFrame) -> str | None:
    """
    Compute higher-timeframe (15m) trend bias using EMA9 vs EMA21 cross.

    Returns:
        "BULLISH"  — EMA9 > EMA21 on last bar of df_15m
        "BEARISH"  — EMA9 < EMA21
        None       — insufficient data or indeterminate
    """
    t = trend_detection(df_15m)
    bias = t.get("bias")
    if bias:
        log.debug(
            "get_htf_bias: EMA%d=%s EMA%d=%s -> %s",
            config.HTF_EMA_FAST,
            t.get("ema_fast"),
            config.HTF_EMA_SLOW,
            t.get("ema_slow"),
            bias,
        )
    return bias


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


def _extract_hhmm(ts: str) -> str:
    """Return HH:MM from timestamp-like strings (ISO or broker format)."""
    s = str(ts or "")
    if not s:
        return ""
    s = s.replace("T", " ")
    if " " in s:
        t = s.split(" ")[-1]
    else:
        t = s
    return t[:5]


def _opening_range_breakout_ok(df: pd.DataFrame, ref_idx: int, direction: str) -> bool:
    """
    Require valid breakout beyond opening range for morning trades.

    OR definition: 09:15 to OPENING_RANGE_END.
    Applied only for entries up to 11:30.
    """
    if not config.OPENING_RANGE_FILTER_ENABLED:
        return True
    if "timestamp" not in df.columns or ref_idx < 0 or ref_idx >= len(df):
        return True

    hhmm = _extract_hhmm(df["timestamp"].iloc[ref_idx])
    if not hhmm:
        return True
    if hhmm <= config.OPENING_RANGE_END:
        return False  # wait until OR is formed
    if hhmm > "11:30":
        return True   # filter is for morning breakouts only

    or_rows = []
    for i, ts in enumerate(df["timestamp"]):
        t = _extract_hhmm(ts)
        if "09:15" <= t <= config.OPENING_RANGE_END:
            or_rows.append(i)
    if len(or_rows) < 5:
        return True

    or_high = float(df["high"].iloc[or_rows].max())
    or_low = float(df["low"].iloc[or_rows].min())
    close = float(df["close"].iloc[ref_idx])
    buf = float(config.OPENING_RANGE_BUFFER)

    if direction == "BULLISH":
        return close >= (or_high + buf)
    return close <= (or_low - buf)


def _bar_not_overextended(df: pd.DataFrame, ref_idx: int, atr_val: float) -> bool:
    """Reject entry bars that are too stretched versus ATR (late/chasing entries)."""
    if ref_idx < 0 or ref_idx >= len(df):
        return True
    if pd.isna(atr_val) or atr_val <= 0:
        return True
    bar_range = float(df["high"].iloc[ref_idx]) - float(df["low"].iloc[ref_idx])
    return (bar_range / float(atr_val)) <= float(config.MAX_ENTRY_BAR_ATR_MULT)


# ═══════════════════════════════════════════════════════════════════════════════
#  TREND REGIME GATES  (VWAP + MA20 slope + Price Structure + Late Entry)
# ═══════════════════════════════════════════════════════════════════════════════

def _get_trend_regime(df: pd.DataFrame) -> tuple[str, float, float]:
    """
    Determine intraday trend regime from VWAP position + MA20 slope.

    Returns: (regime, vwap_val, slope_pct)
        regime = "BULLISH" | "BEARISH" | "SIDEWAYS"

    Rules:
        BULLISH  : price > VWAP  AND  MA20 slope UP
        BEARISH  : price < VWAP  AND  MA20 slope DOWN
        SIDEWAYS : everything else (conflicting signals)
    """
    min_bars = config.AVG_LOOKBACK + config.MA20_SLOPE_BARS + 2
    if len(df) < min_bars:
        return "SIDEWAYS", 0.0, 0.0

    try:
        close_now = float(df["close"].iloc[-1])

        # VWAP
        has_vol = "volume" in df.columns and float(df["volume"].sum()) > 0
        vwap_val = 0.0
        vwap_valid = False
        if has_vol:
            vwap_s = calc_vwap_ind(df)
            v = float(vwap_s.iloc[-1])
            if not pd.isna(v) and v > 0:
                vwap_val = v
                vwap_valid = True

        # MA20 slope: compare SMA20 now vs N bars ago
        sma20 = calc_sma(df["close"], 20)
        sma_now = float(sma20.iloc[-1])
        sma_prev = float(sma20.iloc[-1 - config.MA20_SLOPE_BARS])
        if pd.isna(sma_now) or pd.isna(sma_prev) or sma_prev == 0:
            return "SIDEWAYS", vwap_val, 0.0

        slope_pct = (sma_now - sma_prev) / sma_prev  # fractional
        thresh = config.MA20_SLOPE_FLAT_THRESH
        slope_up = slope_pct > thresh
        slope_down = slope_pct < -thresh

        if vwap_valid:
            above_vwap = close_now > vwap_val
            if above_vwap and slope_up:
                return "BULLISH", vwap_val, slope_pct
            if not above_vwap and slope_down:
                return "BEARISH", vwap_val, slope_pct
            return "SIDEWAYS", vwap_val, slope_pct
        else:
            # No VWAP (NIFTY index has no volume) — use MA20 slope only
            if slope_up:
                return "BULLISH", 0.0, slope_pct
            if slope_down:
                return "BEARISH", 0.0, slope_pct
            return "SIDEWAYS", 0.0, slope_pct

    except Exception as exc:
        log.warning("_get_trend_regime error: %s", exc)
        return "SIDEWAYS", 0.0, 0.0


def _check_price_structure(df: pd.DataFrame, direction: str, n: int = 5) -> bool:
    """
    Verify price structure supports the intended trade direction.
    BULLISH: majority of recent N bars show Higher Highs + Higher Lows.
    BEARISH: majority of recent N bars show Lower Highs + Lower Lows.
    Uses slope of high/low series over last N bars.
    """
    if len(df) < n + 2:
        return True  # not enough data — allow (don't block on insufficient history)
    try:
        highs = [float(df["high"].iloc[-i]) for i in range(1, n + 1)]
        lows  = [float(df["low"].iloc[-i])  for i in range(1, n + 1)]
        # highs[0] is most recent — check if sequence is mostly increasing/decreasing
        hi_slope = highs[0] - highs[-1]   # positive = recents > older → highs rising
        lo_slope = lows[0]  - lows[-1]

        if direction == "BULLISH":
            return hi_slope >= 0 and lo_slope >= 0   # HH + HL
        else:
            return hi_slope <= 0 and lo_slope <= 0   # LH + LL
    except Exception:
        return True


def _is_late_entry(df: pd.DataFrame, direction: str, lookback: int = 10) -> bool:
    """
    Return True if price has already moved > LATE_ENTRY_MAX_MOVE in the
    signal direction over the last `lookback` 1m bars — chasing prevention.
    """
    if len(df) < lookback + 1:
        return False
    try:
        recent = float(df["close"].iloc[-1])
        past   = float(df["close"].iloc[-lookback])
        move   = recent - past
        limit  = config.LATE_ENTRY_MAX_MOVE
        if direction == "BULLISH" and move > limit:
            return True
        if direction == "BEARISH" and -move > limit:
            return True
        return False
    except Exception:
        return False

def evaluate(df: pd.DataFrame, backtest: bool = False,
             df_15m: pd.DataFrame | None = None) -> list[Signal]:
    """
    Evaluate the most recent candle data for confirmed signals.

    Parameters
    ----------
    df      : 1m DataFrame with OHLCV columns. Must have ≥20 rows.
    backtest: if True, skip time-window check and regime gates.
    df_15m  : optional 15m DataFrame for higher-timeframe bias check.

    Gate sequence (any failure → action = SKIP):
        G1  Time window (09:20–11:30 or 13:30–14:45)
        G2  Sideways filter (ADX < 20 → skip)
        G3  HTF bias (15m EMA9 vs EMA21 — signal must align)
        G4  Indicator confluences ≥ MIN_CONFIRMATIONS (3)
        G5  Confirmation candle quality (body ratio, direction)
        G6  Composite strength ≥ MIN_SIGNAL_STRENGTH (50)

    Returns
    -------
    List of Signal objects (usually 0 or 1 per evaluation cycle).
    """
    if len(df) < 10:
        return []

    # ── Gate G0: Trend regime (VWAP + MA20 slope) ─────────────────────────────────
    # BULLISH: price>VWAP + MA20 slope UP   → ONLY CE
    # BEARISH: price<VWAP + MA20 slope DOWN → ONLY PE
    # SIDEWAYS: no trade
    trend_regime = "UNKNOWN"
    if not backtest and getattr(config, 'TREND_FILTER_ENABLED', False):
        trend_regime, _vwap_v, _slope_v = _get_trend_regime(df)
        log.debug("evaluate: trend_regime=%s vwap=%.2f slope=%.5f",
                  trend_regime, _vwap_v, _slope_v)
        if trend_regime == "SIDEWAYS":
            log.info("evaluate: SIDEWAYS regime (VWAP+MA20) — no trade")
            return []

    # ── Gate G2: Sideways / chop filter ───────────────────────────────────
    # DISABLED on 1m: ADX(14) is unreliable on 1-minute candles — frequently
    # reads below threshold even during strong trends, blocking all signals.
    # TODO: re-enable with higher timeframe ADX or higher threshold if needed.
    # if not backtest and is_sideways_market(df):
    #     log.info("evaluate: SIDEWAYS market (ADX < %d) — skipping all signals",
    #              config.ADX_SIDEWAYS_THRESHOLD)
    #     return []

    # ── Gate G3: Higher-timeframe bias ────────────────────────────────────
    htf_bias: str | None = None
    if not backtest and config.HTF_ENABLED and df_15m is not None:
        htf_bias = get_htf_bias(df_15m)
        log.debug("evaluate: HTF bias = %s", htf_bias)

    # ── Step 1: Detect candle patterns ────────────────────────────────────
    raw_signals = scan_signals(df)
    if not raw_signals:
        log.info("evaluate: no candlestick patterns detected in last 3 bars")
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

        # ── Step 3b: Confirmation candle quality check ───────────────
        # Verify the next candle has adequate body (not a doji).
        # Direction check removed — pattern's built-in shift(-1)
        # already provides directional confirmation.
        confirm_idx = ref_idx + 1
        confirmation_ok = True
        confirmation_reason = ""
        if confirm_idx < len(df):
            c_open = float(df["open"].iloc[confirm_idx])
            c_close = float(df["close"].iloc[confirm_idx])
            c_high = float(df["high"].iloc[confirm_idx])
            c_low = float(df["low"].iloc[confirm_idx])
            c_body = abs(c_close - c_open)
            c_range = c_high - c_low if c_high != c_low else 1e-9
            body_ratio = c_body / c_range

            min_body = getattr(config, 'CONFIRM_CANDLE_MIN_BODY_RATIO', 0.10)
            if body_ratio < min_body:
                confirmation_ok = False
                confirmation_reason = f"confirmation candle is a doji/weak body (ratio={body_ratio:.2f}<{min_body})"
        else:
            # No next candle yet — skip confirmation check for the most
            # recent bar.  The pattern's own confirmation logic (e.g.
            # engulfing body-engulfs-prior, shift(-1) for hammer) is
            # sufficient.  Blocking here caused every fresh signal to be
            # rejected with "no confirmation candle yet".
            pass

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
            "rsi": bool(_check_rsi(rsi_val, direction)),
            "macd": bool(_check_macd(macd_hist, direction)),
            "supertrend": bool(_check_supertrend(st_dir, direction)),
            "ema_trend": bool(_check_ema_trend(ef, es, direction)),
            "volume": bool(vol_ok),
        }

        # When NIFTY index has no volume data, exclude volume from the
        # denominator so we need MIN_CONFIRMATIONS out of 4 (not 5).
        active_filters = {k: v for k, v in filters.items()
                          if k != "volume" or has_volume}
        confirmations = sum(active_filters.values())
        strength = _calc_strength(pattern_names, confirmations, vol_ok)
        sl, target = _calc_sl_target(atr_val)

        # ── Step 5: Decide ENTER vs SKIP ─────────────────────────────
        in_window = backtest or _in_trade_window()
        reasons = []
        if confirmations < MIN_CONFIRMATIONS:
            reasons.append(f"only {confirmations}/{MIN_CONFIRMATIONS} confirmations")
        if not in_window:
            reasons.append("outside trade window")
        if not confirmation_ok:
            reasons.append(confirmation_reason)

        # Gate G3: HTF bias alignment (1m signal must agree with 15m trend)
        htf_aligned = True
        if not backtest and config.HTF_ENABLED and htf_bias is not None:
            htf_aligned = (htf_bias == direction)
            if not htf_aligned:
                reasons.append(f"HTF bias is {htf_bias} but signal is {direction}")

        # Gate G6: Composite strength floor
        strength_ok = backtest or (strength >= config.MIN_SIGNAL_STRENGTH)
        if not strength_ok:
            reasons.append(f"strength {strength} < floor {config.MIN_SIGNAL_STRENGTH}")

        # Morning opening-range breakout quality gate
        or_ok = backtest or _opening_range_breakout_ok(df, ref_idx, direction)
        if not or_ok:
            reasons.append("opening-range breakout not validated")

        # Avoid chasing over-extended bars
        bar_shape_ok = backtest or _bar_not_overextended(df, ref_idx, atr_val)
        if not bar_shape_ok:
            reasons.append("entry candle over-extended vs ATR")

        # ── Gate: Trend regime alignment (counter-trend block) ──────────────
        trend_ok = True
        if not backtest and trend_regime not in ("UNKNOWN", "SIDEWAYS"):
            trend_ok = (direction == trend_regime)
            if not trend_ok:
                reasons.append(
                    f"counter-trend: market={trend_regime}, signal={direction} — never trade opposite trend"
                )

        # ── Gate: Price structure (HH/HL for CE, LH/LL for PE, last 5 bars) ─
        structure_ok = backtest or _check_price_structure(df, direction)
        if not structure_ok:
            reasons.append(
                "no supporting structure (need HH+HL for CE, LH+LL for PE)"
            )

        # ── Gate: Late entry filter (already moved > 45 pts in direction) ───
        not_late = backtest or not _is_late_entry(df, direction)
        if not not_late:
            reasons.append(
                f"late entry: price moved >{config.LATE_ENTRY_MAX_MOVE}pts already — wait for pullback"
            )

        all_gates = (
            confirmations >= MIN_CONFIRMATIONS
            and in_window
            and confirmation_ok
            and htf_aligned
            and strength_ok
            and or_ok
            and bar_shape_ok
            and trend_ok
            and structure_ok
            and not_late
        )

        if all_gates:
            action = "ENTER"
            htf_note = f" | HTF:{htf_bias}" if htf_bias else ""
            reason_str = (
                f"{', '.join(pattern_names)} confirmed by "
                f"{confirmations}/5 filters [{', '.join(k for k,v in filters.items() if v)}]"
                f"{htf_note} | strength={strength}"
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
            bar_index=ref_idx,
        )

        # ── Step 6: Persist to DB ────────────────────────────────────
        try:
            insert_signal({
                "timestamp": bar_ts or now_ist().isoformat(),
                "direction": direction,
                "strength": strength,
                "filters": json.dumps({k: bool(v) for k, v in filters.items()}),
                "action": action,
                "reason": reason_str,
            })
        except Exception as e:
            log.warning("Failed to persist signal: %s", e)

        log.info(
            "SIGNAL %s | %s | strength=%d | action=%s | %s",
            direction, pattern_names, strength, action, reason_str,
        )

        results.append(signal)

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


def evaluate_historical(df: pd.DataFrame) -> list[Signal]:
    """
    Evaluate ALL bars in the DataFrame for signals (not just last 3).
    Used for historical analysis / backtest where we need to find every
    pattern that fired throughout the trading day.

    Unlike evaluate(), this scans every candle in the DataFrame.
    """
    if len(df) < 20:
        return []

    # ── Step 1: Detect patterns across ALL bars ──────────────────────
    raw_signals = scan_signals_all(df)
    if not raw_signals:
        return []

    # ── Step 2: Compute indicators on the full DF (once) ────────────
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

    # ── Step 3: Group by (direction, bar_index) to collapse co-located patterns
    from collections import defaultdict
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for sig in raw_signals:
        key = (sig["direction"], sig["bar_index"])
        groups[key].append(sig)

    results: list[Signal] = []

    for (direction, ref_idx), pattern_group in groups.items():
        pattern_names = list({p["pattern"] for p in pattern_group})

        # ── Step 4: Run indicator filters at the reference bar ──────
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
            "rsi": bool(_check_rsi(rsi_val, direction)),
            "macd": bool(_check_macd(macd_hist, direction)),
            "supertrend": bool(_check_supertrend(st_dir, direction)),
            "ema_trend": bool(_check_ema_trend(ef, es, direction)),
            "volume": bool(vol_ok),
        }

        # Exclude volume from denominator when no volume data (index)
        active_filters = {k: v for k, v in filters.items()
                          if k != "volume" or has_volume}
        confirmations = sum(active_filters.values())
        strength = _calc_strength(pattern_names, confirmations, vol_ok)
        sl, target = _calc_sl_target(atr_val)

        # ── Step 5: Decide ENTER vs SKIP (backtest mode: skip window check)
        if confirmations >= MIN_CONFIRMATIONS:
            action = "ENTER"
            reason_str = (
                f"{', '.join(pattern_names)} confirmed by "
                f"{confirmations}/5 filters [{', '.join(k for k,v in filters.items() if v)}]"
            )
        else:
            action = "SKIP"
            reason_str = f"Skipped: only {confirmations}/{MIN_CONFIRMATIONS} confirmations"

        bar_ts = ""
        if "timestamp" in df.columns:
            bar_ts = str(df["timestamp"].iloc[ref_idx])

        entry_px = float(df["close"].iloc[ref_idx])
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

    # Sort by bar_index (chronological), then strength descending
    results.sort(key=lambda s: (s.bar_index, -s.strength))
    return results

"""
scoring/ — Probabilistic scoring engine for NIFTY options auto-trading.

Replaces rigid binary gates with a weighted scoring system based on:
  1. 15m EMA(20) slope (trend engine)
  2. 1m candle structure (momentum + micro-structure)
  3. Volatility filter (ATR-based)
  4. Trend efficiency (directional ratio)
  5. Pullback detection (mean-reversion entries)

Entry when TOTAL SCORE >= ENTRY_THRESHOLD (default 6).

Public API
----------
    compute_trend_score(df_15m)       -> TrendResult
    compute_momentum_score(df_1m)     -> MomentumResult
    compute_volatility_score(df_1m)   -> VolatilityResult
    compute_efficiency_score(df_1m)   -> EfficiencyResult
    compute_structure_score(df_1m)    -> StructureResult
    compute_pullback_score(df_1m, df_15m) -> PullbackResult
    score_signal(df_1m, df_15m)       -> ScoredSignal
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import pandas as pd

from trading_bot.indicators import ema as calc_ema, atr as calc_atr
from trading_bot.utils.logger import get_logger

log = get_logger(__name__)

# ── Scoring thresholds (tunable) ─────────────────────────────────────────────
ENTRY_THRESHOLD = 6          # minimum total score to enter
STRONG_SLOPE_THRESH = 0.5    # normalized slope for STRONG trend
WEAK_SLOPE_THRESH = 0.2      # below this = SIDEWAYS
MOMENTUM_BODY_RATIO = 0.6    # body/range for strong momentum
MOMENTUM_STRENGTH_MULT = 1.2 # body vs avg body for momentum burst
VOLATILITY_MIN_RATIO = 0.7   # current range / ATR floor
EFFICIENCY_PERIOD = 10        # candles for directional efficiency
EFFICIENCY_TREND_THRESH = 0.6 # above = trending
EFFICIENCY_CHOP_THRESH = 0.4  # below = choppy


# ── Result dataclasses ───────────────────────────────────────────────────────

@dataclass
class TrendResult:
    slope: float = 0.0
    normalized_slope: float = 0.0
    label: str = "SIDEWAYS"   # STRONG_BULLISH | WEAK_BULLISH | SIDEWAYS | WEAK_BEARISH | STRONG_BEARISH
    score: int = 0            # 0, 1, or 3
    direction: str = "NEUTRAL"  # BULLISH | BEARISH | NEUTRAL


@dataclass
class MomentumResult:
    body_ratio: float = 0.0
    momentum_strength: float = 0.0
    label: str = "WEAK"       # STRONG | MODERATE | WEAK
    score: int = 0            # 0, 1, or 2


@dataclass
class VolatilityResult:
    volatility_ratio: float = 0.0
    atr: float = 0.0
    score: int = 0            # 0 or 1


@dataclass
class EfficiencyResult:
    efficiency: float = 0.0
    label: str = "CHOPPY"     # TRENDING | CHOPPY
    score: int = 0            # 0 or 2


@dataclass
class StructureResult:
    pattern: str = "NONE"     # HH_HL | LH_LL | NONE
    breakout_strength: float = 0.0
    score: int = 0            # 0 or 2


@dataclass
class PullbackResult:
    is_pullback: bool = False
    score: int = 0            # 0 or 2


@dataclass
class ScoredSignal:
    direction: str = "NEUTRAL"   # BULLISH | BEARISH
    total_score: int = 0
    entry_threshold: int = ENTRY_THRESHOLD
    should_enter: bool = False
    trend: TrendResult = field(default_factory=TrendResult)
    momentum: MomentumResult = field(default_factory=MomentumResult)
    volatility: VolatilityResult = field(default_factory=VolatilityResult)
    efficiency: EfficiencyResult = field(default_factory=EfficiencyResult)
    structure: StructureResult = field(default_factory=StructureResult)
    pullback: PullbackResult = field(default_factory=PullbackResult)
    bar_timestamp: str = ""
    entry_price: float = 0.0
    log_line: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 1: TREND ENGINE (15M EMA SLOPE)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_trend_score(df_15m: pd.DataFrame | None) -> TrendResult:
    """
    Compute 15m EMA(20) slope normalized by ATR.
    Returns TrendResult with direction, label, and score.
    """
    result = TrendResult()
    if df_15m is None or len(df_15m) < 21:
        return result

    try:
        ema_20 = calc_ema(df_15m["close"], 20)
        atr_15m = calc_atr(df_15m, 14)

        ema_curr = float(ema_20.iloc[-1])
        ema_prev = float(ema_20.iloc[-2])
        atr_val = float(atr_15m.iloc[-1])

        if pd.isna(ema_curr) or pd.isna(ema_prev) or pd.isna(atr_val) or atr_val <= 0:
            return result

        slope = ema_curr - ema_prev
        norm_slope = slope / atr_val

        result.slope = round(slope, 4)
        result.normalized_slope = round(norm_slope, 4)

        if norm_slope > STRONG_SLOPE_THRESH:
            result.label = "STRONG_BULLISH"
            result.score = 3
            result.direction = "BULLISH"
        elif norm_slope > WEAK_SLOPE_THRESH:
            result.label = "WEAK_BULLISH"
            result.score = 1
            result.direction = "BULLISH"
        elif norm_slope < -STRONG_SLOPE_THRESH:
            result.label = "STRONG_BEARISH"
            result.score = 3
            result.direction = "BEARISH"
        elif norm_slope < -WEAK_SLOPE_THRESH:
            result.label = "WEAK_BEARISH"
            result.score = 1
            result.direction = "BEARISH"
        else:
            result.label = "SIDEWAYS"
            result.score = 0
            result.direction = "NEUTRAL"

    except Exception as exc:
        log.warning("compute_trend_score error: %s", exc)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 2: MOMENTUM ENGINE (1M)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_momentum_score(df_1m: pd.DataFrame) -> MomentumResult:
    """
    Evaluate 1m candle momentum via body_ratio and momentum_strength.
    """
    result = MomentumResult()
    if len(df_1m) < 6:
        return result

    try:
        last = df_1m.iloc[-1]
        body = abs(float(last["close"]) - float(last["open"]))
        rng = float(last["high"]) - float(last["low"])
        if rng <= 0:
            return result

        body_ratio = body / rng
        bodies = df_1m["close"].iloc[-6:-1].values - df_1m["open"].iloc[-6:-1].values
        avg_body = float(np.mean(np.abs(bodies)))
        momentum_strength = (body / avg_body) if avg_body > 0 else 0.0

        result.body_ratio = round(body_ratio, 4)
        result.momentum_strength = round(momentum_strength, 4)

        if body_ratio > MOMENTUM_BODY_RATIO and momentum_strength > MOMENTUM_STRENGTH_MULT:
            result.label = "STRONG"
            result.score = 2
        elif body_ratio > 0.4 or momentum_strength > 1.0:
            result.label = "MODERATE"
            result.score = 1
        else:
            result.label = "WEAK"
            result.score = 0

    except Exception as exc:
        log.warning("compute_momentum_score error: %s", exc)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 3: VOLATILITY FILTER (ATR-BASED)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_volatility_score(df_1m: pd.DataFrame) -> VolatilityResult:
    """
    Check if current bar range is sufficient relative to ATR.
    Rejects low-volatility (dead) candles.
    """
    result = VolatilityResult()
    if len(df_1m) < 15:
        return result

    try:
        atr_s = calc_atr(df_1m, 14)
        atr_val = float(atr_s.iloc[-1])
        if pd.isna(atr_val) or atr_val <= 0:
            return result

        last = df_1m.iloc[-1]
        current_range = float(last["high"]) - float(last["low"])
        vol_ratio = current_range / atr_val

        result.volatility_ratio = round(vol_ratio, 4)
        result.atr = round(atr_val, 4)

        if vol_ratio >= VOLATILITY_MIN_RATIO:
            result.score = 1

    except Exception as exc:
        log.warning("compute_volatility_score error: %s", exc)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 4: TREND EFFICIENCY (DIRECTIONAL RATIO)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_efficiency_score(df_1m: pd.DataFrame) -> EfficiencyResult:
    """
    Directional efficiency ratio over last N candles.
    efficiency = abs(close_now - close_n_ago) / sum(abs(close[i] - close[i-1]))

    > 0.6 → trending, < 0.4 → choppy
    """
    result = EfficiencyResult()
    n = EFFICIENCY_PERIOD
    if len(df_1m) < n + 1:
        return result

    try:
        closes = df_1m["close"].iloc[-(n + 1):].values.astype(float)
        net_move = abs(closes[-1] - closes[0])
        sum_moves = float(np.sum(np.abs(np.diff(closes))))

        if sum_moves <= 0:
            return result

        efficiency = net_move / sum_moves
        result.efficiency = round(efficiency, 4)

        if efficiency >= EFFICIENCY_TREND_THRESH:
            result.label = "TRENDING"
            result.score = 2
        elif efficiency < EFFICIENCY_CHOP_THRESH:
            result.label = "CHOPPY"
            result.score = 0
        else:
            result.label = "MODERATE"
            result.score = 1

    except Exception as exc:
        log.warning("compute_efficiency_score error: %s", exc)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 5: MICRO-STRUCTURE (HH/HL or LH/LL detection)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_structure_score(df_1m: pd.DataFrame) -> StructureResult:
    """
    Detect Higher High / Higher Low (bullish) or Lower High / Lower Low (bearish)
    over the last 3 candles. Also measures breakout/breakdown strength.
    """
    result = StructureResult()
    if len(df_1m) < 4:
        return result

    try:
        h = df_1m["high"].iloc[-3:].values.astype(float)
        l = df_1m["low"].iloc[-3:].values.astype(float)
        c = float(df_1m["close"].iloc[-1])

        hh = h[-1] > h[-2] > h[-3]  # higher highs
        hl = l[-1] > l[-2] > l[-3]  # higher lows
        lh = h[-1] < h[-2] < h[-3]  # lower highs
        ll = l[-1] < l[-2] < l[-3]  # lower lows

        if hh and hl:
            result.pattern = "HH_HL"
            result.breakout_strength = round(c - h[-2], 2)
            result.score = 2
        elif lh and ll:
            result.pattern = "LH_LL"
            result.breakout_strength = round(l[-2] - c, 2)
            result.score = 2

    except Exception as exc:
        log.warning("compute_structure_score error: %s", exc)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 6: PULLBACK DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def compute_pullback_score(df_1m: pd.DataFrame, df_15m: pd.DataFrame | None) -> PullbackResult:
    """
    Detect pullback to EMA(20) on 1m timeframe in the direction of 15m trend.

    Bullish pullback: trend bullish, price retraces to EMA(20), next candle bullish.
    Bearish pullback: trend bearish, price retraces up, next candle bearish.
    """
    result = PullbackResult()
    if len(df_1m) < 21:
        return result

    try:
        ema_20 = calc_ema(df_1m["close"], 20)
        ema_val = float(ema_20.iloc[-2])  # EMA at pullback candle
        atr_s = calc_atr(df_1m, 14)
        atr_val = float(atr_s.iloc[-1])

        if pd.isna(ema_val) or pd.isna(atr_val) or atr_val <= 0:
            return result

        prev_low = float(df_1m["low"].iloc[-2])
        prev_high = float(df_1m["high"].iloc[-2])
        curr_close = float(df_1m["close"].iloc[-1])
        curr_open = float(df_1m["open"].iloc[-1])

        # Get trend direction from 15m
        trend = compute_trend_score(df_15m)
        touch_band = atr_val * 0.3  # price within 0.3 ATR of EMA = "touch"

        if trend.direction == "BULLISH":
            touched_ema = abs(prev_low - ema_val) <= touch_band
            bullish_candle = curr_close > curr_open
            if touched_ema and bullish_candle:
                result.is_pullback = True
                result.score = 2

        elif trend.direction == "BEARISH":
            touched_ema = abs(prev_high - ema_val) <= touch_band
            bearish_candle = curr_close < curr_open
            if touched_ema and bearish_candle:
                result.is_pullback = True
                result.score = 2

    except Exception as exc:
        log.warning("compute_pullback_score error: %s", exc)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN SCORING FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

def score_signal(df_1m: pd.DataFrame,
                 df_15m: pd.DataFrame | None = None) -> ScoredSignal:
    """
    Run all scoring components and produce a final ScoredSignal.

    Returns ScoredSignal with total_score, should_enter, and component breakdowns.
    """
    sig = ScoredSignal()

    if len(df_1m) < 20:
        return sig

    # Compute all components
    sig.trend = compute_trend_score(df_15m)
    sig.momentum = compute_momentum_score(df_1m)
    sig.volatility = compute_volatility_score(df_1m)
    sig.efficiency = compute_efficiency_score(df_1m)
    sig.structure = compute_structure_score(df_1m)
    sig.pullback = compute_pullback_score(df_1m, df_15m)

    # Total score
    sig.total_score = (
        sig.trend.score
        + sig.momentum.score
        + sig.volatility.score
        + sig.efficiency.score
        + sig.structure.score
        + sig.pullback.score
    )

    # Direction from trend engine (primary), structure (secondary)
    if sig.trend.direction != "NEUTRAL":
        sig.direction = sig.trend.direction
    elif sig.structure.pattern == "HH_HL":
        sig.direction = "BULLISH"
    elif sig.structure.pattern == "LH_LL":
        sig.direction = "BEARISH"

    # Sideways hard block
    if (sig.trend.label == "SIDEWAYS"
            and sig.efficiency.label == "CHOPPY"
            and sig.volatility.score == 0):
        sig.should_enter = False
        sig.total_score = 0
    else:
        sig.should_enter = (
            sig.total_score >= ENTRY_THRESHOLD
            and sig.direction != "NEUTRAL"
        )

    # Entry price and timestamp
    sig.entry_price = float(df_1m["close"].iloc[-1])
    if "timestamp" in df_1m.columns:
        sig.bar_timestamp = str(df_1m["timestamp"].iloc[-1])

    # Build log line
    action = "ENTRY" if sig.should_enter else "SKIP"
    opt_type = "CE" if sig.direction == "BULLISH" else "PE"
    reason = ""
    if not sig.should_enter:
        parts = []
        if sig.trend.label == "SIDEWAYS":
            parts.append("sideways trend")
        if sig.efficiency.label == "CHOPPY":
            parts.append(f"low efficiency={sig.efficiency.efficiency:.2f}")
        if sig.volatility.score == 0:
            parts.append(f"low volatility={sig.volatility.volatility_ratio:.2f}")
        if sig.total_score < ENTRY_THRESHOLD:
            parts.append(f"score {sig.total_score}<{ENTRY_THRESHOLD}")
        if sig.direction == "NEUTRAL":
            parts.append("no direction")
        reason = "; ".join(parts) if parts else "below threshold"

    sig.log_line = (
        f"[{action}] Type={opt_type} | Score={sig.total_score}/{ENTRY_THRESHOLD} | "
        f"Slope={sig.trend.normalized_slope:.2f} | "
        f"Momentum={sig.momentum.momentum_strength:.2f} | "
        f"Efficiency={sig.efficiency.efficiency:.2f} | "
        f"Volatility={sig.volatility.volatility_ratio:.2f} | "
        f"Structure={sig.structure.pattern} | "
        f"Pullback={'Yes' if sig.pullback.is_pullback else 'No'}"
    )
    if reason:
        sig.log_line += f" | Reason={reason}"

    log.info("SCORE_ENGINE: %s", sig.log_line)

    return sig

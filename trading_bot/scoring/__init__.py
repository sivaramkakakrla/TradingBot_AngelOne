
"""
scoring/ — 20-Day Average + LinReg(14) + Theta strategy engine.

Signal Decision Model (Trend + Timing):
    1. SMA(20) slope sets the MAJOR trend direction (RISING → BULLISH, FALLING → BEARISH)
    2. LinReg(14) daily slope confirms TIMING — must turn in SMA direction
    3. LinReg(14) on 1m candles provides intraday entry precision
    4. Theta filter adjusts confidence based on time of day
    5. Existing sub-types (CROSSOVER, BOUNCE, TREND_RIDE) detected on top
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from trading_bot import config
from trading_bot.indicators import linear_regression as calc_linreg
from trading_bot.utils.logger import get_logger
from trading_bot.utils.time_utils import now_ist

log = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
SMA_PERIOD = 20
SLOPE_LOOKBACK = 5
FLAT_SLOPE_THRESH = 0.005        # ±0.5%  for SMA slope
CROSSOVER_LOOKBACK = 2               # ±2 days for fresh crossovers
BOUNCE_BAND_PCT = 4.0            # ±4% of SMA (wider catch zone)
STRETCH_BLOCK_PCT = 10.0
STRETCH_WARN_PCT = 5.0
MOMENTUM_BODY_RATIO = 0.05       # relaxed — dojis/small candles OK


# ═══════════════════════════════════════════════════════════════════════════════
#  DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class AvgState:
    sma_value: float = 0.0
    sma_prev: float = 0.0
    slope_pct: float = 0.0
    slope_label: str = "FLAT"          # RISING / FALLING / FLAT
    # LinReg(14) daily
    linreg_value: float = 0.0
    linreg_slope: float = 0.0
    linreg_direction: str = "FLAT"     # RISING / FALLING / FLAT
    linreg_forecast: float = 0.0
    # Chart data
    daily_closes: list = field(default_factory=list)
    daily_dates: list = field(default_factory=list)
    sma_series: list = field(default_factory=list)
    linreg_series: list = field(default_factory=list)


@dataclass
class TwentyDaySignal:
    # Core
    direction: str = "NEUTRAL"         # BULLISH / BEARISH / NEUTRAL
    signal_type: str = "NONE"          # CROSSOVER / BOUNCE / TREND_RIDE / NONE
    should_enter: bool = False
    option_type: str = ""              # CE / PE
    skip_reasons: list = field(default_factory=list)
    log_line: str = ""
    # Price vs SMA
    live_price: float = 0.0
    sma_value: float = 0.0
    sma_slope_label: str = "FLAT"
    sma_slope_pct: float = 0.0
    distance_pts: float = 0.0
    distance_pct: float = 0.0
    price_vs_sma: str = "AT"          # ABOVE / BELOW / AT
    entry_price: float = 0.0
    bar_timestamp: str = ""
    # LinReg daily
    linreg_daily_slope: float = 0.0
    linreg_daily_direction: str = "FLAT"
    linreg_daily_value: float = 0.0
    # LinReg 1m
    linreg_1m_slope: float = 0.0
    linreg_1m_direction: str = "FLAT"
    # Theta
    theta_zone: str = "MORNING"        # MORNING / MIDDAY / AFTERNOON / EOD
    theta_note: str = ""
    theta_passed: bool = True
    # Timing
    timing_confirmed: bool = False
    # Intraday momentum (legacy)
    intraday_bias: str = "NEUTRAL"
    intraday_body_ratio: float = 0.0
    intraday_momentum: str = ""
    # Chart data
    daily_closes: list = field(default_factory=list)
    daily_dates: list = field(default_factory=list)
    sma_series: list = field(default_factory=list)
    linreg_series: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "signal_type": self.signal_type,
            "should_enter": self.should_enter,
            "option_type": self.option_type,
            "skip_reasons": list(self.skip_reasons),
            "log_line": self.log_line,
            "live_price": self.live_price,
            "sma_value": self.sma_value,
            "sma_slope_label": self.sma_slope_label,
            "sma_slope_pct": self.sma_slope_pct,
            "distance_pts": self.distance_pts,
            "distance_pct": self.distance_pct,
            "price_vs_sma": self.price_vs_sma,
            "entry_price": self.entry_price,
            "bar_timestamp": self.bar_timestamp,
            "linreg_daily_slope": self.linreg_daily_slope,
            "linreg_daily_direction": self.linreg_daily_direction,
            "linreg_daily_value": self.linreg_daily_value,
            "linreg_1m_slope": self.linreg_1m_slope,
            "linreg_1m_direction": self.linreg_1m_direction,
            "theta_zone": self.theta_zone,
            "theta_note": self.theta_note,
            "theta_passed": self.theta_passed,
            "timing_confirmed": self.timing_confirmed,
            "intraday_bias": self.intraday_bias,
            "intraday_body_ratio": self.intraday_body_ratio,
            "intraday_momentum": self.intraday_momentum,
            "daily_closes": self.daily_closes,
            "daily_dates": self.daily_dates,
            "sma_series": self.sma_series,
            "linreg_series": self.linreg_series,
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 1: FETCH DAILY CLOSES
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_daily_closes(session) -> pd.DataFrame | None:
    """
    Fetch last 30 trading days of NIFTY daily OHLCV via AngelOne API.
    Returns DataFrame with columns: timestamp, open, high, low, close, volume
    """
    if session is None:
        return None

    try:
        today = now_ist().date()
        from_date = today - timedelta(days=45)

        from_str = from_date.strftime("%Y-%m-%d 09:15")
        to_str = today.strftime("%Y-%m-%d 15:30")

        resp = session.getCandleData({
            "exchange": config.EXCHANGE,
            "symboltoken": config.NIFTY_TOKEN,
            "interval": "ONE_DAY",
            "fromdate": from_str,
            "todate": to_str,
        })

        if not resp or resp.get("status") is False:
            log.warning("fetch_daily_closes: no data from API")
            return None

        bars = resp.get("data") or []
        if not bars:
            return None

        rows = []
        for bar in bars:
            if len(bar) < 6:
                continue
            rows.append({
                "timestamp": str(bar[0])[:10],
                "open": float(bar[1]),
                "high": float(bar[2]),
                "low": float(bar[3]),
                "close": float(bar[4]),
                "volume": int(bar[5]),
            })

        df = pd.DataFrame(rows)
        if df.empty:
            return None

        df = df.drop_duplicates(subset=["timestamp"], keep="last")
        df = df.sort_values("timestamp").reset_index(drop=True)

        log.info("fetch_daily_closes: got %d daily bars (from %s to %s)",
                 len(df), df["timestamp"].iloc[0], df["timestamp"].iloc[-1])
        return df

    except Exception as exc:
        log.warning("fetch_daily_closes error: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 2: COMPUTE 20-DAY SMA + SLOPE + LinReg(14) DAILY
# ═══════════════════════════════════════════════════════════════════════════════

def compute_20day_avg(daily_df: pd.DataFrame) -> AvgState:
    """Compute 20-day SMA, its slope, AND LinReg(14) on daily closes."""
    state = AvgState()

    if daily_df is None or len(daily_df) < SMA_PERIOD:
        log.warning("compute_20day_avg: need %d bars, got %d",
                     SMA_PERIOD, 0 if daily_df is None else len(daily_df))
        return state

    closes = daily_df["close"].values.astype(float)
    dates = daily_df["timestamp"].values

    # ── SMA(20) ──────────────────────────────────────────────────
    sma_vals = []
    for i in range(len(closes)):
        if i < SMA_PERIOD - 1:
            sma_vals.append(None)
        else:
            sma_vals.append(float(np.mean(closes[i - SMA_PERIOD + 1: i + 1])))

    state.sma_value = sma_vals[-1] if sma_vals[-1] is not None else 0.0
    state.sma_prev = sma_vals[-2] if len(sma_vals) > 1 and sma_vals[-2] is not None else state.sma_value

    lookback_idx = -1 - SLOPE_LOOKBACK
    if abs(lookback_idx) <= len(sma_vals) and sma_vals[lookback_idx] is not None and state.sma_value > 0:
        slope_change = state.sma_value - sma_vals[lookback_idx]
        state.slope_pct = round((slope_change / state.sma_value) * 100, 4)
    else:
        state.slope_pct = 0.0

    if state.slope_pct > FLAT_SLOPE_THRESH * 100:
        state.slope_label = "RISING"
    elif state.slope_pct < -FLAT_SLOPE_THRESH * 100:
        state.slope_label = "FALLING"
    else:
        state.slope_label = "FLAT"

    # ── LinReg(14) on daily closes ───────────────────────────────
    lr_period = config.LINREG_PERIOD
    if len(closes) >= lr_period:
        close_series = pd.Series(closes)
        lr_df = calc_linreg(close_series, period=lr_period)

        lr_slope_val = float(lr_df["linreg_slope"].iloc[-1])
        lr_val = float(lr_df["linreg"].iloc[-1])
        lr_forecast = float(lr_df["linreg_forecast"].iloc[-1])

        state.linreg_value = round(lr_val, 2)
        state.linreg_slope = round(lr_slope_val, 4)
        state.linreg_forecast = round(lr_forecast, 2)

        flat_thresh = config.LINREG_FLAT_SLOPE_THRESH
        if lr_slope_val > flat_thresh:
            state.linreg_direction = "RISING"
        elif lr_slope_val < -flat_thresh:
            state.linreg_direction = "FALLING"
        else:
            state.linreg_direction = "FLAT"

        lr_vals = lr_df["linreg"].values
        state.linreg_series = [
            round(float(v), 2) if not np.isnan(v) else None
            for v in lr_vals[-SMA_PERIOD:]
        ]
    else:
        state.linreg_series = [None] * min(SMA_PERIOD, len(closes))

    # Chart data
    state.daily_closes = [float(c) for c in closes[-SMA_PERIOD:]]
    state.daily_dates = [str(d)[:10] for d in dates[-SMA_PERIOD:]]
    state.sma_series = [round(v, 2) if v is not None else None for v in sma_vals[-SMA_PERIOD:]]

    return state


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 3: DETECT SIGNAL TYPE (CROSSOVER / BOUNCE / TREND RIDE)
# ═══════════════════════════════════════════════════════════════════════════════

def _detect_crossover(daily_df: pd.DataFrame, sma_value: float) -> tuple[bool, str]:
    """Check if NIFTY crossed above/below the 20-day SMA in last 5 days."""
    if daily_df is None or len(daily_df) < SMA_PERIOD + CROSSOVER_LOOKBACK:
        return False, ""

    closes = daily_df["close"].values.astype(float)
    recent = closes[-(CROSSOVER_LOOKBACK + 1):]
    sma_at = []
    for i in range(len(recent)):
        end = len(closes) - len(recent) + i + 1
        start = end - SMA_PERIOD
        if start >= 0:
            sma_at.append(float(np.mean(closes[start:end])))
        else:
            sma_at.append(None)

    for i in range(1, len(recent)):
        if sma_at[i] is None or sma_at[i - 1] is None:
            continue
        prev_above = recent[i - 1] > sma_at[i - 1]
        curr_above = recent[i] > sma_at[i]
        if not prev_above and curr_above:
            return True, "BULLISH"
        if prev_above and not curr_above:
            return True, "BEARISH"

    return False, ""


def _detect_bounce(live_price: float, sma_value: float, slope_label: str,
                   intraday_bias: str) -> bool:
    """Check if live price is bouncing off the 20-day SMA in trend direction."""
    if sma_value <= 0:
        return False

    distance_pct = abs(live_price - sma_value) / sma_value * 100
    if distance_pct > BOUNCE_BAND_PCT:
        return False

    if slope_label == "RISING" and intraday_bias == "BULLISH" and live_price >= sma_value:
        return True
    if slope_label == "FALLING" and intraday_bias == "BEARISH" and live_price <= sma_value:
        return True

    return False


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 4a: INTRADAY MOMENTUM (LEGACY — candle counting)
# ═══════════════════════════════════════════════════════════════════════════════

def _intraday_confirmation(df_1m: pd.DataFrame) -> tuple[str, float, str]:
    """Legacy 1m momentum: 3-bar net + green/red count + body ratio."""
    if df_1m is None or len(df_1m) < 5:
        return "NEUTRAL", 0.0, "insufficient 1m data"

    last = df_1m.iloc[-1]
    body = abs(float(last["close"]) - float(last["open"]))
    rng = float(last["high"]) - float(last["low"])
    body_ratio = body / rng if rng > 0 else 0.0

    c3 = df_1m["close"].iloc[-3:].values.astype(float)
    net_move = c3[-1] - c3[0]

    last5 = df_1m.iloc[-5:]
    greens = sum(1 for _, r in last5.iterrows() if float(r["close"]) > float(r["open"]))
    reds = 5 - greens

    if net_move > 0 and greens >= 3 and body_ratio >= MOMENTUM_BODY_RATIO:
        desc = f"3-bar up +{net_move:.1f}pts, {greens}/5 green, body={body_ratio:.2f}"
        return "BULLISH", body_ratio, desc
    elif net_move < 0 and reds >= 3 and body_ratio >= MOMENTUM_BODY_RATIO:
        desc = f"3-bar down {net_move:.1f}pts, {reds}/5 red, body={body_ratio:.2f}"
        return "BEARISH", body_ratio, desc
    else:
        desc = f"mixed: net={net_move:.1f}pts, G/R={greens}/{reds}, body={body_ratio:.2f}"
        return "NEUTRAL", body_ratio, desc


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 4b: LinReg(14) ON 1m CANDLES
# ═══════════════════════════════════════════════════════════════════════════════

def _linreg_1m_confirmation(df_1m: pd.DataFrame) -> tuple[str, float, str]:
    """
    Compute LinReg(14) on 1m closes for intraday trend precision.
    Returns: (direction, slope, description)
    """
    lr_period = config.LINREG_PERIOD

    if df_1m is None or len(df_1m) < lr_period:
        return "FLAT", 0.0, "insufficient 1m data for LinReg"

    close_series = df_1m["close"].astype(float)
    lr_df = calc_linreg(close_series, period=lr_period)

    slope = float(lr_df["linreg_slope"].iloc[-1])
    lr_val = float(lr_df["linreg"].iloc[-1])

    flat_thresh = config.LINREG_1M_FLAT_SLOPE_THRESH
    if slope > flat_thresh:
        direction = "RISING"
    elif slope < -flat_thresh:
        direction = "FALLING"
    else:
        direction = "FLAT"

    desc = f"LinReg(14) 1m: slope={slope:.4f}, value={lr_val:.2f}, dir={direction}"
    return direction, round(slope, 4), desc


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 5: THETA DIRECTIONAL FILTER
# ═══════════════════════════════════════════════════════════════════════════════

def _theta_filter(sma_label: str, linreg_daily_dir: str,
                  linreg_1m_dir: str, bar_time=None) -> tuple[str, str, bool]:
    """
    Relaxed time-based theta filter for option buyers.

    Morning   (09:30–11:30): Full confidence — no extra gate
    Midday    (11:30–13:30): SMA slope must not be FLAT
    Afternoon (13:30–15:15): SMA + LinReg daily must agree
    EOD       (15:15–15:30): Block all new entries

    bar_time: Optional datetime for backtesting (defaults to now_ist())

    Returns: (theta_zone, theta_note, passed)
    """
    now = bar_time or now_ist()
    hhmm = now.strftime("%H:%M")

    if hhmm < "09:30":
        return "PRE_MARKET", "Pre-market — no trades", False

    if hhmm <= config.THETA_MORNING_END:
        return "MORNING", "Morning session — full confidence, no theta adjustment", True

    if hhmm <= config.THETA_MIDDAY_END:
        if sma_label != "FLAT":
            return "MIDDAY", f"Midday — SMA trend clear ({sma_label})", True
        return "MIDDAY", "Midday — SMA is FLAT, no clear trend", False

    if hhmm <= config.THETA_BLOCK_HOUR:
        if sma_label == linreg_daily_dir and sma_label != "FLAT":
            return "AFTERNOON", f"Afternoon — SMA({sma_label}) + LinReg({linreg_daily_dir}) aligned", True
        note = f"Afternoon — need SMA + LinReg daily agreement (SMA={sma_label}, LR_D={linreg_daily_dir})"
        return "AFTERNOON", note, False

    return "EOD", f"After {config.THETA_BLOCK_HOUR} — no new entries", False


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN ANALYSIS FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_live(daily_df: pd.DataFrame, df_1m: pd.DataFrame | None = None,
                 live_price: float = 0.0, bar_time=None) -> TwentyDaySignal:
    """
    Main 20-Day Avg + LinReg(14) + Theta analysis.

    Trend + Timing Model:
        SMA(20) slope → major direction
        LinReg(14) daily → timing confirmation (slope must turn in SMA direction)
        LinReg(14) 1m → entry precision
        Theta → time-based confidence adjustment
        Existing CROSSOVER / BOUNCE / TREND_RIDE → signal type
    """
    sig = TwentyDaySignal()

    # ── STEP A: Compute 20-day SMA + LinReg(14) daily ────────────────────
    avg = compute_20day_avg(daily_df)
    if avg.sma_value <= 0:
        sig.skip_reasons.append("insufficient daily data for 20-day SMA")
        sig.log_line = "[SKIP] No 20-day SMA — insufficient data"
        return sig

    sig.sma_value = round(avg.sma_value, 2)
    sig.sma_slope_label = avg.slope_label
    sig.sma_slope_pct = avg.slope_pct
    sig.daily_closes = avg.daily_closes
    sig.daily_dates = avg.daily_dates
    sig.sma_series = avg.sma_series
    sig.linreg_series = avg.linreg_series

    sig.linreg_daily_slope = avg.linreg_slope
    sig.linreg_daily_direction = avg.linreg_direction
    sig.linreg_daily_value = avg.linreg_value

    # ── STEP B: Get live price ───────────────────────────────────────────
    if live_price <= 0 and daily_df is not None and len(daily_df) > 0:
        live_price = float(daily_df["close"].iloc[-1])
    if live_price <= 0:
        sig.skip_reasons.append("no live price")
        sig.log_line = "[SKIP] No live price available"
        return sig

    sig.live_price = round(live_price, 2)
    sig.distance_pts = round(live_price - avg.sma_value, 2)
    sig.distance_pct = round((sig.distance_pts / avg.sma_value) * 100, 4)

    if live_price > avg.sma_value:
        sig.price_vs_sma = "ABOVE"
    elif live_price < avg.sma_value:
        sig.price_vs_sma = "BELOW"
    else:
        sig.price_vs_sma = "AT"

    sig.entry_price = live_price
    sig.bar_timestamp = (bar_time or now_ist()).strftime("%Y-%m-%d %H:%M:%S")

    # ── STEP C: Intraday confirmations (legacy + LinReg 1m) ─────────────
    intra_bias, intra_body, intra_desc = _intraday_confirmation(df_1m)
    sig.intraday_bias = intra_bias
    sig.intraday_body_ratio = round(intra_body, 4)
    sig.intraday_momentum = intra_desc

    lr1m_dir, lr1m_slope, lr1m_desc = _linreg_1m_confirmation(df_1m)
    sig.linreg_1m_slope = lr1m_slope
    sig.linreg_1m_direction = lr1m_dir

    # ── STEP D: Theta filter ─────────────────────────────────────────────
    theta_zone, theta_note, theta_passed = _theta_filter(
        avg.slope_label, avg.linreg_direction, lr1m_dir, bar_time=bar_time
    )
    sig.theta_zone = theta_zone
    sig.theta_note = theta_note
    sig.theta_passed = theta_passed

    if not theta_passed:
        sig.skip_reasons.append(f"theta filter: {theta_note}")

    # ── STEP E: Timing confirmation (LinReg daily must agree with SMA) ──
    sma_dir = avg.slope_label
    lr_dir = avg.linreg_direction

    timing_confirmed = False
    if sma_dir == "FLAT":
        sig.skip_reasons.append("SMA(20) is FLAT — no clear trend direction")
    elif sma_dir == lr_dir:
        timing_confirmed = True
    else:
        sig.skip_reasons.append(
            f"timing not confirmed: SMA={sma_dir} but LinReg(14)={lr_dir}"
        )

    sig.timing_confirmed = timing_confirmed

    # ── STEP F: LinReg 1m must agree or be neutral ───────────────────────
    lr_1m_ok = True
    if lr1m_dir != "FLAT":
        expected_1m = "RISING" if sma_dir == "RISING" else ("FALLING" if sma_dir == "FALLING" else "FLAT")
        if lr1m_dir != expected_1m and expected_1m != "FLAT":
            lr_1m_ok = False
            sig.skip_reasons.append(
                f"1m LinReg diverges: expected {expected_1m} but got {lr1m_dir}"
            )

    # ── STEP G: Signal type detection ────────────────────────────────────
    crossed, cross_dir = _detect_crossover(daily_df, avg.sma_value)
    bounced = _detect_bounce(live_price, avg.sma_value, avg.slope_label, intra_bias)

    # Overextension
    if abs(sig.distance_pct) > STRETCH_BLOCK_PCT:
        sig.skip_reasons.append(
            f"price {sig.distance_pct:+.2f}% from SMA — overextended (>{STRETCH_BLOCK_PCT}%)"
        )
    elif abs(sig.distance_pct) > STRETCH_WARN_PCT:
        sig.skip_reasons.append(
            f"WARNING: price {sig.distance_pct:+.2f}% from SMA — mean reversion risk"
        )

    if crossed and cross_dir:
        sig.signal_type = "CROSSOVER"
        sig.direction = cross_dir
    elif bounced:
        sig.signal_type = "BOUNCE"
        sig.direction = "BULLISH" if avg.slope_label == "RISING" else "BEARISH"
    elif sig.price_vs_sma == "ABOVE" and avg.slope_label == "RISING":
        sig.signal_type = "TREND_RIDE"
        sig.direction = "BULLISH"
    elif sig.price_vs_sma == "BELOW" and avg.slope_label == "FALLING":
        sig.signal_type = "TREND_RIDE"
        sig.direction = "BEARISH"
    else:
        sig.signal_type = "NONE"
        if "no alignment" not in " ".join(sig.skip_reasons):
            sig.skip_reasons.append("no alignment between price position and SMA trend")

    if sig.direction != "NEUTRAL" and intra_bias != "NEUTRAL":
        if intra_bias != sig.direction:
            sig.skip_reasons.append(
                f"intraday momentum ({intra_bias}) contradicts daily bias ({sig.direction})"
            )

    if sig.direction == "BULLISH":
        sig.option_type = "CE"
    elif sig.direction == "BEARISH":
        sig.option_type = "PE"

    # ── STEP H: Final entry decision (relaxed gates for higher win-rate) ──
    sig.should_enter = (
        sig.direction != "NEUTRAL"
        and sig.signal_type != "NONE"
        and avg.slope_label != "FLAT"
        and theta_passed
        and abs(sig.distance_pct) <= STRETCH_BLOCK_PCT
        and not (intra_bias != "NEUTRAL" and intra_bias != sig.direction)
    )

    # Re-run theta with actual direction (was called before direction was set)
    if sig.should_enter:
        theta_zone, theta_note, theta_passed = _theta_filter(
            avg.slope_label, avg.linreg_direction, lr1m_dir, bar_time=bar_time
        )
        sig.theta_zone = theta_zone
        sig.theta_note = theta_note
        sig.theta_passed = theta_passed
        if not theta_passed:
            sig.should_enter = False
            sig.skip_reasons.append(f"theta re-check: {theta_note}")

    # ── Build log line ───────────────────────────────────────────────────
    action = "ENTRY" if sig.should_enter else "SKIP"
    opt = sig.option_type or "--"
    reasons = "; ".join(sig.skip_reasons) if sig.skip_reasons else "all clear"
    timing_tag = "CONFIRMED" if timing_confirmed else "WAITING"
    sig.log_line = (
        f"[{action}] {opt} | 20D-SMA={sig.sma_value:.2f} | "
        f"Live={sig.live_price:.2f} | Dist={sig.distance_pct:+.2f}% | "
        f"SMA_Slope={sig.sma_slope_label} | "
        f"LR_Daily={avg.linreg_direction}(slope={avg.linreg_slope:+.4f}) | "
        f"LR_1m={lr1m_dir}(slope={lr1m_slope:+.4f}) | "
        f"Timing={timing_tag} | "
        f"Theta={theta_zone} | "
        f"Signal={sig.signal_type} | "
        f"Reasons={reasons}"
    )

    log.info("20DAY_AVG: %s", sig.log_line)

    return sig


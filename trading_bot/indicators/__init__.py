"""
indicators/ — Technical indicator computations for Project Candles.

All functions accept a pandas DataFrame with columns:
    timestamp, open, high, low, close, volume
and return indicator values as additional columns or a dict of Series.

Uses the `ta` library + numpy for linear regression.
"""

import numpy as np
import pandas as pd
import ta


# ═══════════════════════════════════════════════════════════════════════════════
#  MOVING AVERAGES
# ═══════════════════════════════════════════════════════════════════════════════

def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(window=period, min_periods=1).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Volume-Weighted Average Price (resets each day for intraday)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cum_tp_vol = (typical * df["volume"]).cumsum()
    cum_vol = df["volume"].cumsum()
    return cum_tp_vol / cum_vol.replace(0, np.nan)


# ═══════════════════════════════════════════════════════════════════════════════
#  TREND INDICATORS
# ═══════════════════════════════════════════════════════════════════════════════

def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """
    Compute Supertrend indicator.

    Returns DataFrame with columns: supertrend, supertrend_direction
        direction: 1 = bullish (price above), -1 = bearish (price below)
    """
    hl2 = (df["high"] + df["low"]) / 2
    atr = ta.volatility.AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=period
    ).average_true_range()

    upper_band = hl2 + (multiplier * atr)
    lower_band = hl2 - (multiplier * atr)

    st = pd.Series(np.nan, index=df.index, dtype=float)
    direction = pd.Series(1, index=df.index, dtype=int)

    for i in range(1, len(df)):
        # Clamp bands
        if lower_band.iloc[i] > lower_band.iloc[i - 1] or df["close"].iloc[i - 1] < lower_band.iloc[i - 1]:
            pass  # keep current lower_band
        else:
            lower_band.iloc[i] = lower_band.iloc[i - 1]

        if upper_band.iloc[i] < upper_band.iloc[i - 1] or df["close"].iloc[i - 1] > upper_band.iloc[i - 1]:
            pass  # keep current upper_band
        else:
            upper_band.iloc[i] = upper_band.iloc[i - 1]

        if st.iloc[i - 1] == upper_band.iloc[i - 1]:
            # was bearish
            if df["close"].iloc[i] > upper_band.iloc[i]:
                st.iloc[i] = lower_band.iloc[i]
                direction.iloc[i] = 1
            else:
                st.iloc[i] = upper_band.iloc[i]
                direction.iloc[i] = -1
        else:
            # was bullish
            if df["close"].iloc[i] < lower_band.iloc[i]:
                st.iloc[i] = upper_band.iloc[i]
                direction.iloc[i] = -1
            else:
                st.iloc[i] = lower_band.iloc[i]
                direction.iloc[i] = 1

    # First bar
    st.iloc[0] = upper_band.iloc[0]
    direction.iloc[0] = -1

    return pd.DataFrame({"supertrend": st, "supertrend_direction": direction}, index=df.index)


def linear_regression(series: pd.Series, period: int = 20) -> pd.DataFrame:
    """
    Rolling linear regression line + upper/lower channel (±2 std dev).
    Also returns the slope for trend detection and a 5-bar forecast.

    Returns DataFrame: linreg, linreg_upper, linreg_lower, linreg_slope, linreg_forecast
    """
    n = len(series)
    lr_val = pd.Series(np.nan, index=series.index, dtype=float)
    lr_upper = pd.Series(np.nan, index=series.index, dtype=float)
    lr_lower = pd.Series(np.nan, index=series.index, dtype=float)
    lr_slope = pd.Series(np.nan, index=series.index, dtype=float)
    lr_forecast = pd.Series(np.nan, index=series.index, dtype=float)

    for i in range(period - 1, n):
        window = series.iloc[i - period + 1: i + 1].values
        x = np.arange(period)
        coeffs = np.polyfit(x, window, 1)
        slope, intercept = coeffs
        fitted = intercept + slope * (period - 1)  # value at end of window
        residuals = window - (intercept + slope * x)
        std = np.std(residuals)

        lr_val.iloc[i] = fitted
        lr_upper.iloc[i] = fitted + 2 * std
        lr_lower.iloc[i] = fitted - 2 * std
        lr_slope.iloc[i] = slope
        lr_forecast.iloc[i] = intercept + slope * (period + 4)  # 5-bar ahead

    return pd.DataFrame({
        "linreg": lr_val,
        "linreg_upper": lr_upper,
        "linreg_lower": lr_lower,
        "linreg_slope": lr_slope,
        "linreg_forecast": lr_forecast,
    }, index=series.index)


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — trend strength (0–100)."""
    return ta.trend.ADXIndicator(
        high=df["high"], low=df["low"], close=df["close"], window=period
    ).adx()


# ═══════════════════════════════════════════════════════════════════════════════
#  MOMENTUM INDICATORS
# ═══════════════════════════════════════════════════════════════════════════════

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (0–100)."""
    return ta.momentum.RSIIndicator(close=series, window=period).rsi()


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """
    MACD: line, signal, histogram.
    Returns DataFrame: macd_line, macd_signal, macd_histogram
    """
    m = ta.trend.MACD(close=series, window_slow=slow, window_fast=fast, window_sign=signal)
    return pd.DataFrame({
        "macd_line": m.macd(),
        "macd_signal": m.macd_signal(),
        "macd_histogram": m.macd_diff(),
    }, index=series.index)


def stochastic_rsi(series: pd.Series, period: int = 14, smooth_k: int = 3, smooth_d: int = 3) -> pd.DataFrame:
    """Stochastic RSI: %K and %D lines."""
    s = ta.momentum.StochRSIIndicator(
        close=series, window=period, smooth1=smooth_k, smooth2=smooth_d
    )
    return pd.DataFrame({
        "stoch_rsi_k": s.stochrsi_k() * 100,
        "stoch_rsi_d": s.stochrsi_d() * 100,
    }, index=series.index)


# ═══════════════════════════════════════════════════════════════════════════════
#  VOLATILITY INDICATORS
# ═══════════════════════════════════════════════════════════════════════════════

def bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands: upper, middle (SMA), lower."""
    bb = ta.volatility.BollingerBands(close=series, window=period, window_dev=std_dev)
    return pd.DataFrame({
        "bb_upper": bb.bollinger_hband(),
        "bb_middle": bb.bollinger_mavg(),
        "bb_lower": bb.bollinger_lband(),
        "bb_width": bb.bollinger_wband(),
        "bb_pct": bb.bollinger_pband(),
    }, index=series.index)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    return ta.volatility.AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=period
    ).average_true_range()


# ═══════════════════════════════════════════════════════════════════════════════
#  VOLUME INDICATORS
# ═══════════════════════════════════════════════════════════════════════════════

def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume."""
    return ta.volume.OnBalanceVolumeIndicator(
        close=df["close"], volume=df["volume"]
    ).on_balance_volume()


def volume_sma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Volume SMA for expansion detection."""
    return sma(df["volume"].astype(float), period)


# ═══════════════════════════════════════════════════════════════════════════════
#  COMPUTE ALL — main entry point
# ═══════════════════════════════════════════════════════════════════════════════

def compute_all(df: pd.DataFrame) -> dict:
    """
    Compute all indicators on a candle DataFrame.

    Parameters
    ----------
    df : DataFrame with columns: timestamp, open, high, low, close, volume

    Returns
    -------
    dict of indicator_name → list of values (same length as df, NaN-filled where insufficient data).
    Values are JSON-safe (NaN → None).
    """
    if df.empty:
        return {}

    close = df["close"]
    n = len(df)
    result = {}

    # ── Moving Averages ──
    result["sma_20"] = sma(close, 20)
    result["sma_200"] = sma(close, 200)
    result["ema_20"] = ema(close, 20)
    result["ema_50"] = ema(close, 50)
    result["vwap"] = vwap(df)

    # ── Trend ──
    st = supertrend(df, period=10, multiplier=3.0)
    result["supertrend"] = st["supertrend"]
    result["supertrend_direction"] = st["supertrend_direction"]

    lr = linear_regression(close, period=min(20, n))
    result["linreg"] = lr["linreg"]
    result["linreg_upper"] = lr["linreg_upper"]
    result["linreg_lower"] = lr["linreg_lower"]
    result["linreg_slope"] = lr["linreg_slope"]
    result["linreg_forecast"] = lr["linreg_forecast"]

    result["adx"] = adx(df, period=14)

    # ── Momentum ──
    result["rsi"] = rsi(close, period=14)

    m = macd(close)
    result["macd_line"] = m["macd_line"]
    result["macd_signal"] = m["macd_signal"]
    result["macd_histogram"] = m["macd_histogram"]

    sr = stochastic_rsi(close, period=14)
    result["stoch_rsi_k"] = sr["stoch_rsi_k"]
    result["stoch_rsi_d"] = sr["stoch_rsi_d"]

    # ── Volatility ──
    bb = bollinger_bands(close, period=20, std_dev=2.0)
    result["bb_upper"] = bb["bb_upper"]
    result["bb_middle"] = bb["bb_middle"]
    result["bb_lower"] = bb["bb_lower"]
    result["bb_width"] = bb["bb_width"]
    result["bb_pct"] = bb["bb_pct"]

    result["atr"] = atr(df, period=14)

    # ── Volume ──
    result["obv"] = obv(df)
    result["volume_sma_20"] = volume_sma(df, period=20)

    # Convert all Series to JSON-safe lists (NaN → None)
    for key in result:
        s = result[key]
        result[key] = [None if pd.isna(v) else round(float(v), 4) for v in s]

    return result




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
        from_date = today - timedelta(days=45)  # fetch 45 calendar days to get ~30 trading days

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
                "timestamp": str(bar[0])[:10],  # YYYY-MM-DD
                "open": float(bar[1]),
                "high": float(bar[2]),
                "low": float(bar[3]),
                "close": float(bar[4]),
                "volume": int(bar[5]),
            })

        df = pd.DataFrame(rows)
        if df.empty:
            return None

        # Remove duplicates — keep last entry per date
        df = df.drop_duplicates(subset=["timestamp"], keep="last")
        df = df.sort_values("timestamp").reset_index(drop=True)

        log.info("fetch_daily_closes: got %d daily bars (from %s to %s)",
                 len(df), df["timestamp"].iloc[0], df["timestamp"].iloc[-1])
        return df

    except Exception as exc:
        log.warning("fetch_daily_closes error: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 2: COMPUTE 20-DAY SMA + SLOPE
# ═══════════════════════════════════════════════════════════════════════════════

def compute_20day_avg(daily_df: pd.DataFrame) -> AvgState:
    """
    Compute 20-day SMA of daily closes and its slope.

    Returns AvgState with current SMA, slope direction, and history for charting.
    """
    state = AvgState()

    if daily_df is None or len(daily_df) < SMA_PERIOD:
        log.warning("compute_20day_avg: need %d bars, got %d",
                     SMA_PERIOD, 0 if daily_df is None else len(daily_df))
        return state

    closes = daily_df["close"].values.astype(float)
    dates = daily_df["timestamp"].values

    # Compute rolling SMA
    sma_vals = []
    for i in range(len(closes)):
        if i < SMA_PERIOD - 1:
            sma_vals.append(None)
        else:
            sma_vals.append(float(np.mean(closes[i - SMA_PERIOD + 1: i + 1])))

    state.sma_value = sma_vals[-1] if sma_vals[-1] is not None else 0.0
    state.sma_prev = sma_vals[-2] if len(sma_vals) > 1 and sma_vals[-2] is not None else state.sma_value

    # Slope over SLOPE_LOOKBACK days
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

    # Store for chart / display
    state.daily_closes = [float(c) for c in closes[-SMA_PERIOD:]]
    state.daily_dates = [str(d)[:10] for d in dates[-SMA_PERIOD:]]
    state.sma_series = [round(v, 2) if v is not None else None for v in sma_vals[-SMA_PERIOD:]]

    return state


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 3: DETECT SIGNAL TYPE (CROSSOVER / BOUNCE / TREND RIDE)
# ═══════════════════════════════════════════════════════════════════════════════

def _detect_crossover(daily_df: pd.DataFrame, sma_value: float) -> tuple[bool, str]:
    """
    Check if NIFTY crossed above/below the 20-day SMA in last CROSSOVER_LOOKBACK days.

    Returns (crossed, direction): ("BULLISH" | "BEARISH" | "")
    """
    if daily_df is None or len(daily_df) < SMA_PERIOD + CROSSOVER_LOOKBACK:
        return False, ""

    closes = daily_df["close"].values.astype(float)

    # Compute SMA at recent points
    recent = closes[-(CROSSOVER_LOOKBACK + 1):]
    sma_at = []
    for i in range(len(recent)):
        end = len(closes) - len(recent) + i + 1
        start = end - SMA_PERIOD
        if start >= 0:
            sma_at.append(float(np.mean(closes[start:end])))
        else:
            sma_at.append(None)

    # Check for crossover
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
    """
    Check if live price is bouncing off the 20-day SMA in trend direction.

    Bounce = price within BOUNCE_BAND_PCT of SMA + intraday candle confirms direction.
    """
    if sma_value <= 0:
        return False

    distance_pct = abs(live_price - sma_value) / sma_value * 100
    near_sma = distance_pct <= BOUNCE_BAND_PCT

    if not near_sma:
        return False

    # Bounce must be in trend direction
    if slope_label == "RISING" and intraday_bias == "BULLISH" and live_price >= sma_value:
        return True
    if slope_label == "FALLING" and intraday_bias == "BEARISH" and live_price <= sma_value:
        return True

    return False


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 4: INTRADAY MOMENTUM CONFIRMATION (1M CANDLES)
# ═══════════════════════════════════════════════════════════════════════════════

def _intraday_confirmation(df_1m: pd.DataFrame) -> tuple[str, float, str]:
    """
    Analyze last few 1m candles for directional momentum.

    Returns: (bias, body_ratio, description)
        bias: "BULLISH" | "BEARISH" | "NEUTRAL"
    """
    if df_1m is None or len(df_1m) < 5:
        return "NEUTRAL", 0.0, "insufficient 1m data"

    # Last candle
    last = df_1m.iloc[-1]
    body = abs(float(last["close"]) - float(last["open"]))
    rng = float(last["high"]) - float(last["low"])
    body_ratio = body / rng if rng > 0 else 0.0

    # Last 3 candles trend (net movement)
    c3 = df_1m["close"].iloc[-3:].values.astype(float)
    net_move = c3[-1] - c3[0]

    # Last 5 candles — count green vs red
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
#  MAIN ANALYSIS FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_live(daily_df: pd.DataFrame, df_1m: pd.DataFrame | None = None,
                 live_price: float = 0.0) -> TwentyDaySignal:
    """
    Main analysis: compare live NIFTY price against 20-day average.

    Parameters
    ----------
    daily_df   : Daily OHLCV DataFrame (25+ rows)
    df_1m      : Live 1m candle DataFrame for intraday confirmation
    live_price : Current NIFTY spot price (if 0, uses last daily close)

    Returns
    -------
    TwentyDaySignal with direction, signal type, entry decision, and chart data.
    """
    sig = TwentyDaySignal()

    # Compute 20-day average
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

    # Get live price
    if live_price <= 0 and daily_df is not None and len(daily_df) > 0:
        live_price = float(daily_df["close"].iloc[-1])
    if live_price <= 0:
        sig.skip_reasons.append("no live price")
        sig.log_line = "[SKIP] No live price available"
        return sig

    sig.live_price = round(live_price, 2)

    # Distance from SMA
    sig.distance_pts = round(live_price - avg.sma_value, 2)
    sig.distance_pct = round((sig.distance_pts / avg.sma_value) * 100, 4)

    if live_price > avg.sma_value:
        sig.price_vs_sma = "ABOVE"
    elif live_price < avg.sma_value:
        sig.price_vs_sma = "BELOW"
    else:
        sig.price_vs_sma = "AT"

    sig.entry_price = live_price
    sig.bar_timestamp = now_ist().strftime("%Y-%m-%d %H:%M:%S")

    # Intraday confirmation
    intra_bias, intra_body, intra_desc = _intraday_confirmation(df_1m)
    sig.intraday_bias = intra_bias
    sig.intraday_body_ratio = round(intra_body, 4)
    sig.intraday_momentum = intra_desc

    # ── Signal detection ─────────────────────────────────────────────────

    # 1. Check for crossover (strongest signal)
    crossed, cross_dir = _detect_crossover(daily_df, avg.sma_value)

    # 2. Check for bounce off SMA
    bounced = _detect_bounce(live_price, avg.sma_value, avg.slope_label, intra_bias)

    # 3. Determine signal
    if avg.slope_label == "FLAT":
        sig.skip_reasons.append("20-day SMA is flat — no clear trend")

    # Overextension check
    if abs(sig.distance_pct) > STRETCH_BLOCK_PCT:
        sig.skip_reasons.append(
            f"price {sig.distance_pct:+.2f}% from SMA — overextended (>{STRETCH_BLOCK_PCT}%)"
        )

    stretch_warn = abs(sig.distance_pct) > STRETCH_WARN_PCT

    # Determine direction and signal type
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
        sig.skip_reasons.append("no alignment between price position and SMA trend")

    # Intraday must confirm direction
    if sig.direction != "NEUTRAL" and intra_bias != "NEUTRAL":
        if intra_bias != sig.direction:
            sig.skip_reasons.append(
                f"intraday momentum ({intra_bias}) contradicts daily bias ({sig.direction})"
            )

    # Set option type
    if sig.direction == "BULLISH":
        sig.option_type = "CE"
    elif sig.direction == "BEARISH":
        sig.option_type = "PE"

    # Stretch warning (doesn't block but adds risk note)
    if stretch_warn and abs(sig.distance_pct) <= STRETCH_BLOCK_PCT:
        sig.skip_reasons.append(
            f"WARNING: price {sig.distance_pct:+.2f}% from SMA — mean reversion risk"
        )

    # Final entry decision
    sig.should_enter = (
        sig.direction != "NEUTRAL"
        and sig.signal_type != "NONE"
        and avg.slope_label != "FLAT"
        and abs(sig.distance_pct) <= STRETCH_BLOCK_PCT
        and not (intra_bias != "NEUTRAL" and intra_bias != sig.direction)
    )

    # Build log line
    action = "ENTRY" if sig.should_enter else "SKIP"
    opt = sig.option_type or "--"
    reasons = "; ".join(sig.skip_reasons) if sig.skip_reasons else "all clear"
    sig.log_line = (
        f"[{action}] {opt} | 20D-SMA={sig.sma_value:.2f} | "
        f"Live={sig.live_price:.2f} | Dist={sig.distance_pct:+.2f}% ({sig.distance_pts:+.1f}pts) | "
        f"Slope={sig.sma_slope_label} ({sig.sma_slope_pct:+.4f}%) | "
        f"Signal={sig.signal_type} | "
        f"Intraday={intra_bias} | "
        f"Reasons={reasons}"
    )

    log.info("20DAY_AVG: %s", sig.log_line)

    return sig


import pandas as pd


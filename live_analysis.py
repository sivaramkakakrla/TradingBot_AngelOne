"""Quick live NIFTY analysis script."""
import pandas as pd
from trading_bot.auth.login import get_session
from trading_bot import config
from trading_bot.strategy import evaluate
from trading_bot.utils.time_utils import now_ist

session = get_session()
now = now_ist()
today = now.strftime("%Y-%m-%d")
now_str = now.strftime("%Y-%m-%d %H:%M")

print("=" * 60)
print("  LIVE NIFTY ANALYSIS — " + now.strftime("%d %b %Y %H:%M:%S IST"))
print("=" * 60)
print()

# Fetch today's 1-minute candles
resp = session.getCandleData({
    "exchange": config.EXCHANGE,
    "symboltoken": config.NIFTY_TOKEN,
    "interval": "ONE_MINUTE",
    "fromdate": today + " 09:15",
    "todate": now_str,
})

if not resp or resp.get("status") is False:
    print("ERROR: Could not fetch candle data:", resp)
    exit()

raw = resp.get("data") or []
print("Candles fetched: %d (1-min bars)" % len(raw))

if len(raw) < 20:
    print("Not enough candles for analysis (need 20+)")
    exit()

rows = []
for bar in raw:
    if len(bar) >= 6:
        rows.append({
            "timestamp": str(bar[0]),
            "open": float(bar[1]),
            "high": float(bar[2]),
            "low": float(bar[3]),
            "close": float(bar[4]),
            "volume": int(bar[5]),
        })

df = pd.DataFrame(rows)
for col in ("close", "open", "high", "low", "volume"):
    df[col] = df[col].astype(float)

last_close = df["close"].iloc[-1]
day_open = df["open"].iloc[0]
day_high = df["high"].max()
day_low = df["low"].min()
chg = last_close - day_open
pct = (chg / day_open) * 100
sign = "+" if chg >= 0 else ""

print()
print("NIFTY 50:  %.2f  (%s%.2f  %s%.2f%%)" % (last_close, sign, chg, sign, pct))
print("Day Range: %.2f - %.2f" % (day_low, day_high))
print("Open:      %.2f" % day_open)
print()

# Run strategy on 1-min
signals = evaluate(df, backtest=True)

if not signals:
    print("--- 1-MIN TIMEFRAME ---")
    print("NO SIGNALS DETECTED at this time.")
    print("Waiting for confirmed candle patterns + indicator confluence.")
else:
    print("=== %d SIGNAL(S) DETECTED (1-MIN) ===" % len(signals))
    print()
    for i, sig in enumerate(signals, 1):
        print("--- Signal #%d ---" % i)
        print("  Direction:       %s" % sig.direction)
        print("  Action:          %s" % sig.action)
        print("  Strength:        %d%%" % sig.strength)
        print("  Patterns:        %s" % ", ".join(sig.patterns))
        print()
        for desc in sig.pattern_descriptions:
            print("    >> %s" % desc)
        print()
        print("  Entry Price:     %.2f" % sig.entry_price)
        if sig.direction == "BULLISH":
            tgt_px = sig.entry_price + sig.target_points
            sl_px = sig.entry_price - sig.sl_points
        else:
            tgt_px = sig.entry_price - sig.target_points
            sl_px = sig.entry_price + sig.sl_points
        print("  Target Price:    %.2f  (+%.1f pts)" % (tgt_px, sig.target_points))
        print("  Stop Loss:       %.2f  (-%.1f pts)" % (sl_px, sig.sl_points))
        print("  Expected Profit: +%.1f pts" % sig.expected_profit_pts)
        print("  Risk:Reward:     1:2")
        print("  Confirmations:   %d/5" % sig.confirmations)
        fp = [k for k, v in sig.filters.items() if v]
        ff = [k for k, v in sig.filters.items() if not v]
        print("  Filters PASS:    %s" % (", ".join(fp) if fp else "none"))
        print("  Filters FAIL:    %s" % (", ".join(ff) if ff else "none"))
        print("  Signal Time:     %s" % sig.bar_timestamp)
        print("  Reason:          %s" % sig.reason)
        print()

# 5-min analysis
print("=" * 60)
print("=== 5-MINUTE TIMEFRAME ANALYSIS ===")
print()

resp5 = session.getCandleData({
    "exchange": config.EXCHANGE,
    "symboltoken": config.NIFTY_TOKEN,
    "interval": "FIVE_MINUTE",
    "fromdate": today + " 09:15",
    "todate": now_str,
})

raw5 = []
if resp5 and resp5.get("status") is not False:
    raw5 = resp5.get("data") or []
print("5-min candles fetched: %d" % len(raw5))

if len(raw5) >= 20:
    rows5 = []
    for bar in raw5:
        if len(bar) >= 6:
            rows5.append({
                "timestamp": str(bar[0]),
                "open": float(bar[1]),
                "high": float(bar[2]),
                "low": float(bar[3]),
                "close": float(bar[4]),
                "volume": int(bar[5]),
            })
    df5 = pd.DataFrame(rows5)
    for col in ("close", "open", "high", "low", "volume"):
        df5[col] = df5[col].astype(float)

    signals5 = evaluate(df5, backtest=True)
    if not signals5:
        print("No signals on 5-min timeframe.")
    else:
        print("%d signal(s) on 5-min:" % len(signals5))
        print()
        for sig in signals5:
            print("  %s | %s | Strength %d%% | Patterns: %s" % (
                sig.direction, sig.action, sig.strength, ", ".join(sig.patterns)))
            for desc in sig.pattern_descriptions:
                print("    >> %s" % desc)
            if sig.direction == "BULLISH":
                tgt_px = sig.entry_price + sig.target_points
                sl_px = sig.entry_price - sig.sl_points
            else:
                tgt_px = sig.entry_price - sig.target_points
                sl_px = sig.entry_price + sig.sl_points
            print("    Entry: %.2f | Target: %.2f (+%.1f) | SL: %.2f (-%.1f) | Profit: +%.1f pts" % (
                sig.entry_price, tgt_px, sig.target_points, sl_px, sig.sl_points, sig.expected_profit_pts))
            print()
else:
    print("Not enough 5-min candles yet.")

print()
print("=" * 60)
print("Analysis complete.")

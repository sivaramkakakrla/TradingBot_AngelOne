"""Quick diagnostic: test 20-day avg strategy with live session."""
import sys
sys.path.insert(0, ".")

from trading_bot.auth.login import get_session
from trading_bot.scoring import fetch_daily_closes, analyze_live, compute_20day_avg

session = get_session()
if not session:
    print("ERROR: No session")
    sys.exit(1)
print("Session: OK")

daily_df = fetch_daily_closes(session)
if daily_df is None:
    print("ERROR: No daily data")
    sys.exit(1)

print(f"Daily bars: {len(daily_df)}")
print(daily_df.tail(5).to_string())

avg = compute_20day_avg(daily_df)
print(f"\n20-Day SMA: {avg.sma_value:.2f}")
print(f"SMA Slope: {avg.slope_label} ({avg.slope_pct:.4f}%)")

live = float(daily_df["close"].iloc[-1])
dist = live - avg.sma_value
dist_pct = (dist / avg.sma_value * 100) if avg.sma_value > 0 else 0
print(f"Last close: {live:.2f}")
print(f"Distance: {dist:+.1f} pts ({dist_pct:+.2f}%)")

sig = analyze_live(daily_df, None, live)
print(f"\n=== SIGNAL ===")
print(f"Direction: {sig.direction}")
print(f"Signal Type: {sig.signal_type}")
print(f"Should Enter: {sig.should_enter}")
print(f"Option Type: {sig.option_type}")
print(f"Price vs SMA: {sig.price_vs_sma}")
print(f"Slope: {sig.sma_slope_label}")
print(f"Intraday: {sig.intraday_bias}")
print(f"Skip Reasons: {sig.skip_reasons}")
print(f"Log: {sig.log_line}")

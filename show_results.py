"""Quick script to show trade positions and history."""
import requests
import json

# Open positions
r = requests.get('http://127.0.0.1:5000/api/paper/positions')
d = r.json()
print("=" * 60)
print("  OPEN POSITIONS")
print("=" * 60)
for p in d.get('positions', []):
    print(f"  {p['trade_id']} | {p['direction']} | Entry: {p['entry_price']:.2f} | LTP: {p.get('ltp', 'N/A')} | PnL: Rs.{p.get('unrealized_pnl', 0):.2f}")
    print(f"    SL: {p.get('stop_loss', 'N/A')} | Target: {p.get('target', 'N/A')}")
if not d.get('positions'):
    print("  No open positions")

# Trade history
r2 = requests.get('http://127.0.0.1:5000/api/paper/history')
d2 = r2.json()
print()
print("=" * 60)
print("  TRADE HISTORY (Today)")
print("=" * 60)
total_pnl = 0
for t in d2.get('trades', []):
    pnl = t.get('pnl', 0) or 0
    total_pnl += pnl
    exit_info = f"Exit: {t.get('exit_price', '--')}" if t.get('exit_price') else "OPEN"
    reason = t.get('exit_reason', '')
    source = t.get('source', '')
    if source != 'AUTO':
        continue  # only show auto-trades
    print(f"  {t['trade_id']} | {t['direction']} @ {t['entry_price']:.2f} | {exit_info} | PnL: Rs.{pnl:.2f} | {reason}")

print()
print(f"  Total Realized PnL: Rs.{total_pnl:.2f}")

# Auto-trade status
r3 = requests.get('http://127.0.0.1:5000/api/autotrade/status')
d3 = r3.json()
print()
print("=" * 60)
print("  AUTO-TRADE ENGINE STATUS")
print("=" * 60)
print(f"  Enabled: {d3.get('enabled')}")
print(f"  Open Positions: {d3.get('open_positions')}")
print(f"  PnL Today: Rs.{d3.get('pnl_today', 0):.2f}")
print(f"  Last Scan: {d3.get('last_scan')}")
sig = d3.get('last_signal')
if sig:
    print(f"  Last Signal: {sig['direction']} | Patterns: {', '.join(sig['patterns'])} | Strength: {sig['strength']}%")
print()
print("  Recent Activity:")
for entry in d3.get('log', [])[-5:]:
    print(f"    {entry}")

# Opportunities
r4 = requests.get('http://127.0.0.1:5000/api/opportunities')
d4 = r4.json()
print()
print("=" * 60)
print("  CURRENT OPPORTUNITIES")
print("=" * 60)
print(f"  Data Date: {d4.get('data_date')}")
print(f"  Candle Count: {d4.get('candle_count')}")
for s in d4.get('signals', []):
    print(f"  {s['direction']} | {s['action']} | Patterns: {', '.join(s['patterns'])} | Strength: {s['strength']}%")
    print(f"    Entry: {s['entry_price']:.2f} | Target: +{s['target_points']:.1f} pts | SL: -{s['sl_points']:.1f} pts")

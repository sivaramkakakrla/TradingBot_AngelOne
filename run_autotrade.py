"""
run_autotrade.py — Start the auto-trade engine with live dashboard.

Authenticates with AngelOne, starts the Flask dashboard,
and runs the auto-trade engine that scans every 30 seconds.

All trades are PAPER ONLY — no real orders are placed.

Usage:
    python run_autotrade.py
"""
import signal
import sys
import time

from trading_bot import config
from trading_bot.data.store import init_db
from trading_bot.auth.login import authenticate, is_logged_in
from trading_bot.utils.time_utils import now_ist
from trading_bot.utils.logger import get_logger

log = get_logger("autotrade_runner")

_shutdown = False

def _handle_signal(signum, frame):
    global _shutdown
    print("\nShutdown signal received. Stopping...")
    _shutdown = True


def main():
    global _shutdown

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    print("=" * 60)
    print("  PROJECT CANDLES — AUTO PAPER TRADE ENGINE")
    print("  Mode: PAPER (no real orders)")
    print("  Time: %s" % now_ist().strftime("%Y-%m-%d %H:%M:%S IST"))
    print("=" * 60)
    print()

    # 1. Init database
    init_db()
    print("[OK] Database initialized")

    # 2. Authenticate
    print("[..] Authenticating with AngelOne...")
    try:
        session = authenticate()
        print("[OK] Authenticated — client=%s" % config.ANGEL_CLIENT_ID)
    except Exception as exc:
        print("[FAIL] Authentication failed: %s" % exc)
        return

    # 3. Start market data feed
    from trading_bot.market import start_feed
    start_feed(session, interval=3.0)
    print("[OK] Market feed started (polling every 3s)")

    # 4. Start Flask dashboard
    from trading_bot.dashboard.server import start_dashboard
    dash_thread = start_dashboard()
    print("[OK] Dashboard running at http://%s:%d" % (config.DASHBOARD_HOST, config.DASHBOARD_PORT))

    # 5. Start auto-trade engine
    from trading_bot.autotrade import start as at_start, get_status, stop as at_stop, is_alive
    at_start(scan_interval=60)
    print("[OK] Auto-trade engine STARTED (scanning every 60s)")
    print()
    print("Trade Windows: %s" % config.TRADE_WINDOWS)
    print("Max Open Trades: %d" % config.MAX_OPEN_TRADES)
    print("Max Daily Loss: Rs.%d" % config.MAX_DAILY_LOSS)
    print("Force Exit Time: %s IST" % config.FORCE_EXIT_TIME)
    print()
    print("Watching for signals... Press CTRL+C to stop.")
    print("-" * 60)

    last_print = 0
    try:
        while not _shutdown:
            now = time.time()

            # Check engine health every cycle and restart if dead
            if not is_alive():
                print("[WARN] Auto-trade thread died — restarting...")
                at_start(scan_interval=60)

            # Print status every 30 seconds
            if now - last_print >= 30:
                status = get_status()
                ist = now_ist()
                alive_str = "ALIVE" if is_alive() else "DEAD"
                enabled_str = "YES" if status.get("enabled") else "NO"
                print("[%s] Engine=%s | Open: %d | PnL: Rs.%.2f | Scan: %s | Enabled: %s" % (
                    ist.strftime("%H:%M:%S"),
                    alive_str,
                    status.get("open_positions", 0),
                    status.get("pnl_today", 0),
                    status.get("last_scan", "--"),
                    enabled_str,
                ))
                # Print recent log entries
                log_entries = status.get("log", [])
                if log_entries:
                    recent = log_entries[-5:]  # last 5 entries
                    for entry in recent:
                        print("       %s" % entry)
                last_print = now
            time.sleep(5)
    except KeyboardInterrupt:
        pass

    # Shutdown
    print()
    print("Stopping auto-trade engine...")
    at_stop()
    print("Auto-trade engine stopped.")

    from trading_bot.auth.login import logout
    logout()
    print("Logged out. Goodbye.")


if __name__ == "__main__":
    main()

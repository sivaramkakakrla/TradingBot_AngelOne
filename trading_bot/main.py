"""
main.py — Project Candles orchestrator.

Boot sequence:
    1. Load config + init logger
    2. Init SQLite database
    3. Authenticate with AngelOne SmartAPI (08:30 IST auto‑login)
    4. Fetch historical data → store in SQLite
    5. Compute indicators
    6. Start WebSocket live feed
    7. Run signal engine on each candle close
    8. Manage orders + risk
    9. Start Flask dashboard
    10. Graceful shutdown on SIGINT / CTRL+C

Usage:
    python -m trading_bot.main            # full live / paper mode
    python -m trading_bot.main --backtest # run backtest then exit
"""

import argparse
import signal
import sys
import time

from trading_bot import config
from trading_bot.utils.logger import get_logger
from trading_bot.utils.time_utils import now_ist, market_open_today, seconds_until
from trading_bot.data.store import init_db
from trading_bot.auth.login import authenticate, logout, is_logged_in

log = get_logger("main")

# ─── Graceful shutdown flag ───────────────────────────────────────────────────
_shutdown = False


def _handle_signal(signum, frame):
    """Set shutdown flag on CTRL+C or SIGTERM."""
    global _shutdown
    log.info("Shutdown signal received (sig=%s). Cleaning up …", signum)
    _shutdown = True


def _wait_for_login_time() -> None:
    """Block until AUTO_LOGIN_TIME if we're early."""
    secs = seconds_until(config.AUTO_LOGIN_TIME)
    if secs > 0:
        log.info("Waiting %.0f s until %s IST auto‑login …", secs, config.AUTO_LOGIN_TIME)
        while secs > 0 and not _shutdown:
            time.sleep(min(secs, 5))
            secs = seconds_until(config.AUTO_LOGIN_TIME)


def run_live() -> None:
    """Main live/paper trading loop."""
    log.info("=" * 60)
    log.info("  PROJECT CANDLES — NIFTY OPTIONS AUTO TRADING SYSTEM")
    log.info("  Mode : %s", config.TRADING_MODE.upper())
    log.info("  Time : %s", now_ist().strftime("%Y-%m-%d %H:%M:%S IST"))
    log.info("=" * 60)

    # 1 — Init database
    init_db()

    # 2 — Wait for login window (if started before 08:30)
    if not is_logged_in():
        _wait_for_login_time()
        if _shutdown:
            return

    # 3 — Authenticate
    try:
        session = authenticate()
        log.info("SmartAPI session ready.")
    except Exception as exc:
        log.critical("Authentication FAILED — aborting: %s", exc)
        return

    # 4 — Start Flask dashboard immediately (before fetch so browser works right away)
    from trading_bot.dashboard.server import start_dashboard
    start_dashboard()
    log.info("Dashboard → http://%s:%d", config.DASHBOARD_HOST, config.DASHBOARD_PORT)

    # 5 — Fetch historical candles in background thread (non-blocking)
    import threading as _threading
    from trading_bot.data.historical import fetch_and_store_history

    def _fetch_history():
        try:
            log.info("Background: fetching last 30 days of 1m candles …")
            fetch_and_store_history(session, days=30, timeframe="1m")
        except Exception as exc:
            log.warning("Historical fetch error (non‑fatal): %s", exc)

    _threading.Thread(target=_fetch_history, name="hist-fetch", daemon=True).start()

    # 6 — TODO: Compute indicators     (indicators/indicators.py)
    log.info("[STUB] Compute indicators …")

    # 7 — TODO: Start WebSocket feed   (data/live.py)
    log.info("[STUB] Start WebSocket live feed …")

    # 8 — Main loop
    log.info("Entering main trading loop. Press CTRL+C to stop.")
    try:
        while not _shutdown:
            if not market_open_today():
                log.info("Market closed today (weekend). Sleeping 60 s …")
                time.sleep(60)
                continue

            # TODO: This loop will be driven by WebSocket candle‑close events
            # For now, heartbeat every 5 seconds
            time.sleep(5)
    except KeyboardInterrupt:
        pass

    # 9 — Cleanup
    log.info("Logging out …")
    logout()
    log.info("Shutdown complete.")


def run_backtest() -> None:
    """Run the backtest engine and print results."""
    log.info("=" * 60)
    log.info("  PROJECT CANDLES — BACKTEST MODE")
    log.info("=" * 60)
    init_db()
    # TODO: Import and run backtest.engine
    log.info("[STUB] Backtest engine not yet implemented.")


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Project Candles Trading Bot")
    parser.add_argument("--backtest", action="store_true", help="Run backtest mode")
    args = parser.parse_args()

    # Register signal handlers
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if args.backtest:
        run_backtest()
    else:
        run_live()


if __name__ == "__main__":
    main()

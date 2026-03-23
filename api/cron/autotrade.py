"""
Vercel Cron endpoint — runs one auto-trade scan cycle.

Vercel calls this endpoint on a schedule (see vercel.json crons config).
Each invocation:
    1. Restores trades from Redis into ephemeral /tmp SQLite
    2. Monitors open positions (SL/target/smart-exit/EOD)
    3. Scans for new signals and places trades
    4. All writes are synced to Redis automatically via store.py hooks

This replaces the background-thread approach which can't work on serverless.
"""

import json
import os
import shutil
from http.server import BaseHTTPRequestHandler
from pathlib import Path


def _init_vercel_db():
    """Copy deployed DB snapshot to /tmp (if present) then restore from Redis."""
    src = Path(__file__).resolve().parent.parent / "trading_bot" / "trading_bot.db"
    dst = Path("/tmp/trading_bot.db")
    if src.exists() and not dst.exists():
        shutil.copy2(str(src), str(dst))

    from trading_bot.data.store import init_db
    init_db()

    # Overlay live trades from Redis (survives cold starts)
    try:
        from trading_bot.redis_sync import restore_from_redis
        restored = restore_from_redis()
        if restored:
            print(f"[cron] Restored {restored} trades from Redis")
    except Exception as e:
        print(f"[cron] Redis restore error: {e}")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Single auto-trade scan invoked by Vercel Cron."""
        # Verify the request is from Vercel Cron (security)
        auth = self.headers.get("Authorization", "")
        cron_secret = os.getenv("CRON_SECRET", "")
        if cron_secret and auth != f"Bearer {cron_secret}":
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Unauthorized"}).encode())
            return

        try:
            _init_vercel_db()

            from trading_bot.autotrade import (
                _monitor_positions,
                _scan_and_trade,
                _is_in_trade_window,
                _is_past_force_exit,
            )
            from trading_bot.data.store import get_open_trades, get_today_pnl
            from trading_bot.utils.time_utils import now_ist

            now = now_ist()
            result = {
                "time": now.strftime("%H:%M:%S"),
                "date": now.strftime("%Y-%m-%d"),
            }

            # Monitor existing positions (always — for SL/target/EOD exit)
            _monitor_positions()
            result["monitored"] = True

            # Scan for new signals (only during trade window)
            if _is_in_trade_window():
                _scan_and_trade()
                result["scanned"] = True
            else:
                result["scanned"] = False
                result["reason"] = "outside_trade_window"

            # Gather summary
            open_trades = get_open_trades()
            auto_count = sum(
                1 for t in open_trades
                if (t.get("source") or t["trade_id"][:2]) in ("AUTO", "AT")
            )
            today_pnl = get_today_pnl(now.strftime("%Y-%m-%d"))

            result["open_positions"] = auto_count
            result["pnl_today"] = round(today_pnl, 2)
            result["status"] = "ok"

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        except Exception as e:
            print(f"[cron] Error: {e}")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

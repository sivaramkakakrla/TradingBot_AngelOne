"""Vercel serverless entrypoint — exposes the Flask app."""

import os
import shutil
from pathlib import Path

# On Vercel, the deployed db file is read-only (in /var/task/).
# Copy it to writable /tmp on first cold start, then overlay live
# trades from Redis so data persists across cold starts.
if os.getenv("VERCEL"):
    _src = Path(__file__).resolve().parent.parent / "trading_bot" / "trading_bot.db"
    _dst = Path("/tmp/trading_bot.db")
    if _src.exists() and not _dst.exists():
        shutil.copy2(str(_src), str(_dst))

from trading_bot.dashboard.server import app
from trading_bot.data.store import init_db

# Ensure DB schema exists (uses /tmp on Vercel)
init_db()

# Restore live trades from Redis (Vercel only — survives cold starts)
if os.getenv("VERCEL"):
    try:
        from trading_bot.redis_sync import restore_from_redis
        _restored = restore_from_redis()
        if _restored:
            print(f"[index] Restored {_restored} trades from Redis")
    except Exception as _e:
        print(f"[index] Redis restore error: {_e}")

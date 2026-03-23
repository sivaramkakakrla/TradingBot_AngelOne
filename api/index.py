"""Vercel serverless entrypoint — exposes the Flask app."""

import os
from pathlib import Path

from trading_bot.dashboard.server import app
from trading_bot.data.store import init_db

# On Vercel, /tmp is ephemeral — every cold start gets a fresh filesystem.
# Redis is the single source of truth. We create a fresh DB schema and
# restore all trades/orders/portfolio from Redis on every cold start.
init_db()

if os.getenv("VERCEL"):
    try:
        from trading_bot.redis_sync import restore_from_redis
        _restored = restore_from_redis()
        print(f"[index] Cold start: restored {_restored} trades from Redis")
    except Exception as _e:
        print(f"[index] Redis restore error: {_e}")

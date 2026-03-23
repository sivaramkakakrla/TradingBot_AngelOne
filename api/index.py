"""Vercel serverless entrypoint — exposes the Flask app."""

import os
import shutil
from pathlib import Path

# On Vercel, the deployed db file is read-only (in /var/task/).
# Copy it to writable /tmp on first cold start so trades persist
# across requests within the same function instance.
if os.getenv("VERCEL"):
    _src = Path(__file__).resolve().parent.parent / "trading_bot" / "trading_bot.db"
    _dst = Path("/tmp/trading_bot.db")
    if _src.exists() and not _dst.exists():
        shutil.copy2(str(_src), str(_dst))

from trading_bot.dashboard.server import app
from trading_bot.data.store import init_db

# Ensure DB schema exists (uses /tmp on Vercel)
init_db()

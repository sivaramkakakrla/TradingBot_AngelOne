"""Vercel serverless entrypoint — exposes the Flask app."""

from trading_bot.dashboard.server import app
from trading_bot.data.store import init_db

# Ensure DB schema exists (uses /tmp on Vercel)
init_db()

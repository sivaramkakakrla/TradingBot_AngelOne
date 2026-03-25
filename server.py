import os
from trading_bot.data.store import init_db
from trading_bot.dashboard.server import app

# Ensure DB schema exists (critical on Vercel where /tmp is ephemeral)
init_db()

# On Vercel, restore trades from Redis
if os.getenv("VERCEL"):
    try:
        from trading_bot.redis_sync import restore_from_redis
        _restored = restore_from_redis()
        print(f"[server] Cold start: restored {_restored} trades from Redis")
    except Exception as _e:
        print(f"[server] Redis restore error: {_e}")
else:
    # Non-Vercel (Railway / local): auto-start the background scanner at boot
    # This ensures scanning runs even if no browser is ever opened.
    try:
        from trading_bot.dashboard.server import start_background_scanner
        start_background_scanner()
        print("[server] Background scanner started (no browser needed)")
    except Exception as _e:
        print(f"[server] Background scanner start error: {_e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

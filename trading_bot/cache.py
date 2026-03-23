"""
cache.py — Upstash Redis cache helper for Vercel serverless deployments.

Provides get_cached() and set_cached() backed by Upstash Redis.
All operations are wrapped in try/except — if Redis is unavailable the app
continues to function without caching (just slower on Vercel cold starts).

Usage:
    from trading_bot.cache import get_cached, set_cached

    data = get_cached("nifty:live")
    if data is None:
        data = fetch_from_api()
        set_cached("nifty:live", data, ttl=30)
"""

import json
import os

try:
    from upstash_redis import Redis as _UpstashRedis
    _UPSTASH_AVAILABLE = True
except ImportError:
    _UPSTASH_AVAILABLE = False

# Module-level singleton — reused across invocations within the same warm
# Vercel container (not guaranteed, but saves re-init cost when it happens).
_redis = None


def _get_client():
    """Return the Upstash Redis client, initialising once if needed."""
    global _redis
    if _redis is not None:
        return _redis

    if not _UPSTASH_AVAILABLE:
        print("[cache] upstash-redis not installed — caching disabled")
        return None

    url = os.getenv("UPSTASH_REDIS_REST_URL", os.getenv("UPSTASH_REDIS_URL", "")).strip()
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN", os.getenv("UPSTASH_REDIS_TOKEN", "")).strip()

    if not url or not token:
        print("[cache] UPSTASH_REDIS_URL / UPSTASH_REDIS_TOKEN not set — caching disabled")
        return None

    try:
        _redis = _UpstashRedis(url=url, token=token)
        print("[cache] Redis client initialised")
        return _redis
    except Exception as exc:
        print(f"[cache] Redis init error: {exc}")
        return None


def get_cached(key: str):
    """
    Return the deserialised value stored at *key*, or None on cache miss / error.

    Prints a HIT or MISS line so cold-start behaviour is visible in Vercel logs.
    """
    client = _get_client()
    if client is None:
        print(f"[cache] MISS (no client): {key}")
        return None
    try:
        raw = client.get(key)
        if raw is None:
            print(f"[cache] MISS: {key}")
            return None
        print(f"[cache] HIT: {key}")
        # Upstash returns bytes or str depending on version
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        return json.loads(raw)
    except Exception as exc:
        print(f"[cache] get error ({key}): {exc}")
        return None


def set_cached(key: str, value, ttl: int = 60) -> bool:
    """
    Serialise *value* to JSON and store it in Redis with the given TTL (seconds).

    Returns True on success, False on any error.
    """
    client = _get_client()
    if client is None:
        return False
    try:
        client.set(key, json.dumps(value, default=str), ex=ttl)
        print(f"[cache] SET: {key} (ttl={ttl}s)")
        return True
    except Exception as exc:
        print(f"[cache] set error ({key}): {exc}")
        return False

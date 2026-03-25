"""
auth/login.py — AngelOne SmartAPI authentication with TOTP auto‑generation.

Provides:
    authenticate()  → SmartConnect object (logged‑in session)
    logout(obj)     → graceful logout
    get_auth_token() → current JWT for REST calls
    get_feed_token() → current feed token for WebSocket

Re‑authentication is idempotent — calling authenticate() when already
logged in returns the cached session. Call logout() first to force refresh.
"""

import os
import threading
from SmartApi import SmartConnect
import pyotp

from trading_bot import config
from trading_bot.utils.logger import get_logger

log = get_logger(__name__)

# ─── Module‑level session state (thread‑safe) ─────────────────────────────────
_lock = threading.Lock()
_session: SmartConnect | None = None
_auth_token: str = ""
_feed_token: str = ""
_refresh_token: str = ""


def _generate_totp() -> str:
    """Generate current TOTP from the base32 secret in config."""
    if not config.ANGEL_TOTP_KEY:
        raise ValueError("ANGEL_TOTP_KEY is not set in .env")
    totp = pyotp.TOTP(config.ANGEL_TOTP_KEY)
    return totp.now()


def authenticate() -> SmartConnect:
    """
    Log in to AngelOne SmartAPI and return the SmartConnect session.

    - Generates TOTP automatically.
    - Caches the session; subsequent calls return the same object.
    - Thread‑safe.

    Raises
    ------
    ValueError  if credentials are missing.
    Exception   if SmartAPI rejects the login.
    """
    global _session, _auth_token, _feed_token, _refresh_token

    with _lock:
        if _session is not None:
            log.debug("Returning cached SmartConnect session.")
            return _session

        # ── Try to restore session from Redis (avoids 2-4 s re-auth on cold start) ──
        try:
            from trading_bot.cache import get_cached
            cached_auth = get_cached("angelone:auth")
            if cached_auth and cached_auth.get("jwt"):
                log.info("Restoring AngelOne session from Redis cache (skipping generateSession)")
                obj = SmartConnect(api_key=config.ANGEL_API_KEY)
                obj.access_token  = cached_auth["jwt"]
                obj.refresh_token = cached_auth.get("refresh", "")

                # ── Validate restored token with a lightweight API call ──
                try:
                    profile = obj.getProfile(cached_auth.get("refresh", ""))
                    if profile and profile.get("success") is True and profile.get("data"):
                        log.info("Cached token is VALID — reusing session")
                        _auth_token    = cached_auth["jwt"]
                        _refresh_token = cached_auth.get("refresh", "")
                        _feed_token    = cached_auth.get("feed", "")
                        _session = obj
                        return _session
                    else:
                        log.warning("Cached token INVALID (profile: %s) — doing fresh login",
                                    profile.get("message", "unknown") if profile else "no response")
                except Exception as _validate_exc:
                    log.warning("Cached token EXPIRED (%s) — doing fresh login", _validate_exc)
        except Exception as _cache_exc:
            log.warning("Redis auth restore failed, doing fresh login: %s", _cache_exc)

        # Validate credentials present
        for name, val in [
            ("ANGEL_API_KEY", config.ANGEL_API_KEY),
            ("ANGEL_CLIENT_ID", config.ANGEL_CLIENT_ID),
            ("ANGEL_PASSWORD", config.ANGEL_PASSWORD),
            ("ANGEL_TOTP_KEY", config.ANGEL_TOTP_KEY),
        ]:
            if not val:
                raise ValueError(f"{name} is not configured. Update your .env")

        log.info("Authenticating with AngelOne SmartAPI …")

        # SmartConnect writes logs to cwd/logs/; on Vercel (read-only FS)
        # we temporarily switch to /tmp so the library can create its log dir.
        _on_vercel = bool(os.getenv("VERCEL"))
        if _on_vercel:
            _prev_cwd = os.getcwd()
            os.chdir("/tmp")
        try:
            obj = SmartConnect(api_key=config.ANGEL_API_KEY)
        finally:
            if _on_vercel:
                os.chdir(_prev_cwd)

        totp_value = _generate_totp()

        data = obj.generateSession(
            clientCode=config.ANGEL_CLIENT_ID,
            password=config.ANGEL_PASSWORD,
            totp=totp_value,
        )

        if data.get("status") is False:
            msg = data.get("message", "Unknown login error")
            log.error("Login FAILED: %s", msg)
            raise RuntimeError(f"AngelOne login failed: {msg}")

        _auth_token = data["data"]["jwtToken"]
        _refresh_token = data["data"]["refreshToken"]
        _feed_token = obj.getfeedToken()
        _session = obj

        log.info(
            "Login SUCCESS — client=%s  feed_token=%s…",
            config.ANGEL_CLIENT_ID,
            _feed_token[:12] if _feed_token else "N/A",
        )

        # ── Persist tokens to Redis so the next cold start can skip re-auth ──
        try:
            from trading_bot.cache import set_cached
            set_cached("angelone:auth", {
                "jwt":     _auth_token,
                "refresh": _refresh_token,
                "feed":    _feed_token,
            }, ttl=8 * 3600)   # 8 hours — well within AngelOne's 24 h token lifetime
        except Exception as _ce:
            log.warning("Could not cache auth tokens in Redis: %s", _ce)

        return _session


def logout() -> None:
    """Gracefully terminate the current session."""
    global _session, _auth_token, _feed_token, _refresh_token

    with _lock:
        if _session is None:
            log.debug("No active session to log out.")
            return
        try:
            _session.terminateSession(config.ANGEL_CLIENT_ID)
            log.info("Session terminated for %s", config.ANGEL_CLIENT_ID)
        except Exception as exc:
            log.warning("Logout error (non‑fatal): %s", exc)
        finally:
            _session = None
            _auth_token = ""
            _feed_token = ""
            _refresh_token = ""


def get_session() -> SmartConnect:
    """Return the active session, authenticating first if needed."""
    if _session is None:
        return authenticate()
    return _session


def get_auth_token() -> str:
    """Return the current JWT auth token."""
    if not _auth_token:
        authenticate()
    return _auth_token


def get_feed_token() -> str:
    """Return the current feed token for WebSocket."""
    if not _feed_token:
        authenticate()
    return _feed_token


def get_refresh_token() -> str:
    """Return the current refresh token."""
    if not _refresh_token:
        authenticate()
    return _refresh_token


def is_logged_in() -> bool:
    """Check whether we have an active session."""
    return _session is not None


def force_reauth() -> SmartConnect:
    """Force a fresh login by clearing the cached session and Redis token."""
    global _session, _auth_token, _feed_token, _refresh_token
    with _lock:
        _session = None
        _auth_token = ""
        _feed_token = ""
        _refresh_token = ""
    # Clear the Redis-cached token so authenticate() does a fresh generateSession
    try:
        from trading_bot.cache import _get_client
        r = _get_client()
        if r:
            r.delete("angelone:auth")
    except Exception:
        pass
    log.info("force_reauth: cleared session cache — re-authenticating")
    return authenticate()

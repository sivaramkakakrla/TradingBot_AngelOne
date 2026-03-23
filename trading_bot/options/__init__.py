"""
options/ — NIFTY Option Chain via AngelOne SmartAPI instrument master.

Downloads the full instrument master once per day, filters NIFTY NFO
options (OPTIDX), caches locally, and builds an option chain with
live premiums fetched via getMarketData().
"""

import datetime
import json
import os
from pathlib import Path
from threading import Lock

import requests

from trading_bot import config
from trading_bot.utils.logger import get_logger

log = get_logger(__name__)

_MASTER_URLS = [
    "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
]

if os.getenv("VERCEL"):
    _CACHE_FILE = Path("/tmp/nifty_options_cache.json")
else:
    _CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "nifty_options_cache.json"
_lock = Lock()
_nifty_options: list[dict] = []


# ═══════════════════════════════════════════════════════════════════════════════
#  INSTRUMENT MASTER — DOWNLOAD & CACHE
# ═══════════════════════════════════════════════════════════════════════════════

def _download_and_cache() -> list[dict]:
    """Download instrument master, filter NIFTY options, cache to disk."""
    master = None
    for url in _MASTER_URLS:
        try:
            log.info("Downloading instrument master from %s …", url[:60])
            resp = requests.get(url, timeout=120)
            resp.raise_for_status()
            master = resp.json()
            break
        except Exception as e:
            log.warning("Master download failed (%s): %s", url[:60], e)

    if master is None:
        raise RuntimeError("Could not download instrument master from any URL")

    # Filter: only NIFTY NFO options
    nifty_opts = []
    for r in master:
        if (
            r.get("name") == "NIFTY"
            and r.get("exch_seg") == "NFO"
            and r.get("instrumenttype") == "OPTIDX"
        ):
            raw_strike = float(r.get("strike", 0))
            # AngelOne stores strike * 100 in some versions
            strike = raw_strike / 100 if raw_strike > 50000 else raw_strike
            nifty_opts.append({
                "token":    r["token"],
                "symbol":   r["symbol"],
                "expiry":   r.get("expiry", ""),
                "strike":   strike,
                "lotsize":  int(r.get("lotsize", 65)),
            })

    log.info("Filtered %d NIFTY options from %d total instruments", len(nifty_opts), len(master))

    # Write cache
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_CACHE_FILE, "w") as f:
        json.dump({"date": datetime.date.today().isoformat(), "options": nifty_opts}, f)

    return nifty_opts


def load_options(force: bool = False) -> list[dict]:
    """Load NIFTY options list with daily disk cache."""
    global _nifty_options
    with _lock:
        if _nifty_options and not force:
            return _nifty_options

        today = datetime.date.today().isoformat()

        # Try disk cache
        if not force and _CACHE_FILE.exists():
            try:
                with open(_CACHE_FILE) as f:
                    cache = json.load(f)
                if cache.get("date") == today:
                    _nifty_options = cache["options"]
                    log.info("Loaded %d NIFTY options from cache", len(_nifty_options))
                    return _nifty_options
            except Exception:
                pass

        # Fresh download
        _nifty_options = _download_and_cache()
        return _nifty_options


# ═══════════════════════════════════════════════════════════════════════════════
#  EXPIRY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_expiry(exp_str: str) -> datetime.date | None:
    """Parse AngelOne expiry string (e.g. '19Mar2026', '2026-03-19')."""
    for fmt in ("%d%b%Y", "%d%B%Y", "%Y-%m-%d", "%d%b%y"):
        try:
            return datetime.datetime.strptime(exp_str, fmt).date()
        except ValueError:
            continue
    return None


def get_weekly_expiries() -> tuple[datetime.date, datetime.date]:
    """Return (nearest_expiry, second_nearest_expiry) from instrument master.
    Falls back to computed Thursdays if master isn't loaded yet."""
    today = datetime.date.today()
    try:
        expiries = get_available_expiries()  # sorted ISO strings
        future = [datetime.date.fromisoformat(e) for e in expiries if e >= today.isoformat()]
        if len(future) >= 2:
            return future[0], future[1]
        elif len(future) == 1:
            return future[0], future[0] + datetime.timedelta(days=7)
    except Exception:
        pass
    # Fallback: Thursday-based computation
    wd = today.weekday()
    if wd <= 3:
        this_thu = today + datetime.timedelta(days=3 - wd)
    else:
        this_thu = today + datetime.timedelta(days=10 - wd)
    return this_thu, this_thu + datetime.timedelta(days=7)


def get_available_expiries() -> list[str]:
    """Return sorted list of unique expiry dates found in instrument master."""
    options = load_options()
    seen = set()
    for opt in options:
        d = _parse_expiry(opt["expiry"])
        if d and d >= datetime.date.today():
            seen.add(d.isoformat())
    return sorted(seen)


# ═══════════════════════════════════════════════════════════════════════════════
#  OPTION CHAIN BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

def build_option_chain(
    session,
    nifty_spot: float,
    expiry_date: datetime.date | None = None,
    strikes_range: int = 500,
    strike_step: int = 50,
) -> dict:
    """
    Build and return option chain for a given expiry around ATM.

    Returns:
        {
            "expiry": "2026-03-19",
            "atm": 24500,
            "chain": [
                {
                    "strike": 24000,
                    "CE": {"token": "...", "symbol": "...", "ltp": 0, "lot_size": 65},
                    "PE": {"token": "...", "symbol": "...", "ltp": 0, "lot_size": 65},
                },
                ...
            ]
        }
    """
    options = load_options()
    if expiry_date is None:
        expiry_date, _ = get_weekly_expiries()

    atm = round(nifty_spot / strike_step) * strike_step
    lo = atm - strikes_range
    hi = atm + strikes_range

    chain_map: dict[float, dict] = {}
    token_map: dict[str, tuple[float, str]] = {}  # token → (strike, "CE"/"PE")

    for opt in options:
        exp = _parse_expiry(opt["expiry"])
        if exp != expiry_date:
            continue
        strike = opt["strike"]
        if strike < lo or strike > hi:
            continue

        sym = opt["symbol"]
        if sym.endswith("CE"):
            otype = "CE"
        elif sym.endswith("PE"):
            otype = "PE"
        else:
            continue

        chain_map.setdefault(strike, {})
        chain_map[strike][otype] = {
            "token":    opt["token"],
            "symbol":   opt["symbol"],
            "ltp":      0.0,
            "oi":       0,
            "volume":   0,
            "lot_size": opt["lotsize"],
        }
        token_map[opt["token"]] = (strike, otype)

    # ── Batch-fetch premiums via getMarketData (FULL mode for OI + LTP) ──
    all_tokens = list(token_map.keys())
    if all_tokens and session:
        try:
            resp = session.getMarketData(
                mode="FULL",
                exchangeTokens={"NFO": all_tokens},
            )
            if resp and resp.get("data"):
                for item in resp["data"].get("fetched", []):
                    tk = item.get("symbolToken") or item.get("symboltoken")
                    if tk and tk in token_map:
                        s, ot = token_map[tk]
                        chain_map[s][ot]["ltp"] = float(item.get("ltp", 0))
                        chain_map[s][ot]["oi"] = int(item.get("opnInterest", 0))
                        chain_map[s][ot]["volume"] = int(item.get("tradeVolume", 0) or item.get("exchTradeVal", 0) or 0)
        except Exception as e:
            log.error("getMarketData FULL failed: %s", e)

    # Sort by strike and package
    _empty = {"token": "", "symbol": "", "ltp": 0.0, "oi": 0, "volume": 0, "lot_size": 65}
    chain_list = []
    for s in sorted(chain_map.keys()):
        chain_list.append({
            "strike": s,
            "CE": chain_map[s].get("CE", {**_empty}),
            "PE": chain_map[s].get("PE", {**_empty}),
        })

    return {"expiry": expiry_date.isoformat(), "atm": atm, "chain": chain_list}


# ═══════════════════════════════════════════════════════════════════════════════
#  SINGLE / BATCH LTP FOR OPTION TOKENS
# ═══════════════════════════════════════════════════════════════════════════════

def get_option_ltp(session, tokens: list[str]) -> dict[str, float]:
    """Fetch LTP for a list of NFO tokens. Returns {token: ltp}."""
    if not tokens or not session:
        return {}
    result: dict[str, float] = {}
    # getMarketData LTP allows up to ~1000 tokens
    batch_size = 500
    for i in range(0, len(tokens), batch_size):
        batch = tokens[i : i + batch_size]
        try:
            resp = session.getMarketData(
                mode="LTP",
                exchangeTokens={"NFO": batch},
            )
            if resp and resp.get("data"):
                for item in resp["data"].get("fetched", []):
                    tk = item.get("symbolToken") or item.get("symboltoken")
                    if tk:
                        result[tk] = float(item.get("ltp", 0))
        except Exception as e:
            log.error("Option LTP batch fetch failed: %s", e)
    return result


def get_nearest_expiry() -> datetime.date | None:
    """Return the nearest future expiry date from the instrument master."""
    options = load_options()
    today = datetime.date.today()
    nearest = None
    for opt in options:
        d = _parse_expiry(opt["expiry"])
        if d and d >= today:
            if nearest is None or d < nearest:
                nearest = d
    return nearest


def find_atm_option(
    nifty_spot: float,
    option_type: str,
    expiry_date: datetime.date | None = None,
    strike_step: int = 50,
) -> dict | None:
    """
    Find the ATM option contract for a given option type (CE/PE).

    Returns:
        {"token": str, "symbol": str, "strike": float, "expiry": str,
         "option_type": str, "lotsize": int}
        or None if not found.
    """
    options = load_options()
    if not options:
        log.warning("No options loaded — cannot find ATM option")
        return None

    if expiry_date is None:
        expiry_date = get_nearest_expiry()
        if expiry_date is None:
            log.warning("No future expiry dates found")
            return None

    atm_strike = round(nifty_spot / strike_step) * strike_step
    otype = option_type.upper()

    # Search for exact ATM strike first, then nearest
    best = None
    best_diff = float("inf")

    for opt in options:
        exp = _parse_expiry(opt["expiry"])
        if exp != expiry_date:
            continue
        sym = opt["symbol"]
        if not sym.endswith(otype):
            continue
        diff = abs(opt["strike"] - atm_strike)
        if diff < best_diff:
            best_diff = diff
            best = opt

    if best is None:
        log.warning("No %s option found for expiry %s near strike %s",
                    otype, expiry_date, atm_strike)
        return None

    return {
        "token":       best["token"],
        "symbol":      best["symbol"],
        "strike":      best["strike"],
        "expiry":      expiry_date.strftime("%d %b").lstrip("0"),
        "expiry_date": expiry_date.isoformat(),
        "option_type": otype,
        "lotsize":     best["lotsize"],
    }


def format_option_name(strike: float, option_type: str, expiry_str: str) -> str:
    """Format as 'NIFTY 24 Mar 23650 PE'."""
    return f"NIFTY {expiry_str} {int(strike)} {option_type}"

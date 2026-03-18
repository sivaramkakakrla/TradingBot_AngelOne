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
                "lotsize":  int(r.get("lotsize", 25)),
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
    """Return (this_week_expiry, next_week_expiry) — both Thursdays."""
    today = datetime.date.today()
    wd = today.weekday()  # Mon=0 … Sun=6
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
                    "CE": {"token": "...", "symbol": "...", "ltp": 0, "lot_size": 25},
                    "PE": {"token": "...", "symbol": "...", "ltp": 0, "lot_size": 25},
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
            "lot_size": opt["lotsize"],
        }
        token_map[opt["token"]] = (strike, otype)

    # ── Batch-fetch premiums via getMarketData (LTP mode, up to 1000) ──
    all_tokens = list(token_map.keys())
    if all_tokens and session:
        try:
            resp = session.getMarketData(
                mode="LTP",
                exchangeTokens={"NFO": all_tokens},
            )
            if resp and resp.get("data"):
                for item in resp["data"].get("fetched", []):
                    tk = item.get("symbolToken") or item.get("symboltoken")
                    if tk and tk in token_map:
                        s, ot = token_map[tk]
                        chain_map[s][ot]["ltp"] = float(item.get("ltp", 0))
        except Exception as e:
            log.error("getMarketData LTP failed: %s", e)

    # Sort by strike and package
    _empty = {"token": "", "symbol": "", "ltp": 0.0, "lot_size": 25}
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

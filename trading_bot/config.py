"""
config.py — Central configuration for Project Candles trading system.

All tuneable parameters live here. Credentials are loaded from .env.
No hardcoded secrets.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ─── Load .env from project root ──────────────────────────────────────────────
_BASE_DIR = Path(__file__).resolve().parent
_ENV_PATH = _BASE_DIR / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)
else:
    # Also try parent directory
    _ENV_PARENT = _BASE_DIR.parent / ".env"
    if _ENV_PARENT.exists():
        load_dotenv(_ENV_PARENT)


# ═══════════════════════════════════════════════════════════════════════════════
#  BROKER CREDENTIALS  (loaded from environment — never commit real values)
# ═══════════════════════════════════════════════════════════════════════════════
ANGEL_API_KEY: str = os.getenv("ANGEL_API_KEY", "")
ANGEL_CLIENT_ID: str = os.getenv("ANGEL_CLIENT_ID", "")
ANGEL_PASSWORD: str = os.getenv("ANGEL_PASSWORD", "")
ANGEL_TOTP_KEY: str = os.getenv("ANGEL_TOTP_KEY", "")

# ═══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM (optional)
# ═══════════════════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ═══════════════════════════════════════════════════════════════════════════════
#  TRADING MODE
# ═══════════════════════════════════════════════════════════════════════════════
TRADING_MODE: str = os.getenv("TRADING_MODE", "paper")  # "paper" | "live"

# ═══════════════════════════════════════════════════════════════════════════════
#  INSTRUMENT SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════
UNDERLYING = "NIFTY"
EXCHANGE = "NSE"
NFO_EXCHANGE = "NFO"
NIFTY_TOKEN = "99926000"           # NIFTY 50 index token
SENSEX_TOKEN = "99919000"          # SENSEX index token (BSE)
LOT_SIZE = 25                      # NIFTY lot size (as of 2026)
TRADE_LOTS = 2                     # lots per trade
TRADE_QTY = LOT_SIZE * TRADE_LOTS  # 50

# ═══════════════════════════════════════════════════════════════════════════════
#  MULTI‑TIMEFRAME SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════
TIMEFRAMES = {
    "1m":  {"interval": "ONE_MINUTE",      "history_days": 30},
    "5m":  {"interval": "FIVE_MINUTE",     "history_days": 90},
    "15m": {"interval": "FIFTEEN_MINUTE",  "history_days": 180},
    "1D":  {"interval": "ONE_DAY",         "history_days": 730},  # 2 years
}

# ═══════════════════════════════════════════════════════════════════════════════
#  INDICATOR PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════════
# 15m trend
EMA_FAST = 20
EMA_SLOW = 50

# 5m confirmation
RSI_PERIOD = 14
RSI_BULL_THRESHOLD = 55
RSI_BEAR_THRESHOLD = 45

SUPERTREND_PERIOD = 10
SUPERTREND_MULTIPLIER = 3.0

# ═══════════════════════════════════════════════════════════════════════════════
#  CANDLE INTELLIGENCE (Project Candles Engine)
# ═══════════════════════════════════════════════════════════════════════════════
# Body strength
STRONG_BODY_RATIO = 0.6
WEAK_BODY_RATIO = 0.3

# Wick rejection
WICK_REJECTION_MULTIPLIER = 1.5

# Momentum burst
RANGE_BURST_MULTIPLIER = 1.8
VOLUME_BURST_MULTIPLIER = 1.5
AVG_LOOKBACK = 20  # bars for rolling average

# Breakout strength
BREAKOUT_CLOSE_PCT = 0.10  # close within 10% of range from high

# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY FILTERS
# ═══════════════════════════════════════════════════════════════════════════════
# VIX range
VIX_LOW = 12.0
VIX_HIGH = 20.0
VIX_SKIP_LOW = 10.0
VIX_SKIP_HIGH = 25.0

# Volume expansion
VOLUME_EXPANSION_MULT = 1.5

# OI wall distance (points from ATM)
OI_WALL_MIN_DISTANCE = 50

# PCR thresholds
PCR_BULLISH = 1.2
PCR_BEARISH = 0.8

# Time windows (IST, 24h format)
TRADE_WINDOWS = [
    ("09:30", "11:30"),
    ("13:30", "14:30"),
]
FORCE_EXIT_TIME = "15:00"

# Duplicate signal cooldown (seconds)
DUPLICATE_SIGNAL_COOLDOWN = 60

# ═══════════════════════════════════════════════════════════════════════════════
#  EXIT / RISK MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════
INITIAL_SL_POINTS = 20
TRAIL_START_POINTS = 30
TRAILING_SL_POINTS = 15
PARTIAL_EXIT_POINTS = 40
PARTIAL_EXIT_PCT = 0.50  # sell 50% at partial target

MAX_LOTS_PER_TRADE = 2
MAX_OPEN_TRADES = 3
MAX_DAILY_LOSS = 3000  # ₹3000

# ═══════════════════════════════════════════════════════════════════════════════
#  PAPER TRADING PORTFOLIO
# ═══════════════════════════════════════════════════════════════════════════════
PAPER_INITIAL_CAPITAL = 30_000  # ₹30,000 starting balance

# ═══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════════════════════════
if os.getenv("VERCEL"):
    DB_PATH = "/tmp/trading_bot.db"
else:
    DB_PATH = str(_BASE_DIR / "trading_bot.db")

# ═══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════════════════
LOG_DIR = str(_BASE_DIR / "logs")
LOG_LEVEL = "INFO"

# ═══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 5000

# ═══════════════════════════════════════════════════════════════════════════════
#  API RATE LIMITING
# ═══════════════════════════════════════════════════════════════════════════════
API_RATE_LIMIT = 3  # requests per second

# ═══════════════════════════════════════════════════════════════════════════════
#  SAFETY
# ═══════════════════════════════════════════════════════════════════════════════
AUTO_LOGIN_TIME = "08:30"          # IST
WS_NO_TICK_PAUSE_SECONDS = 15     # pause trading if no tick for this long
ORDER_CONFIRM_TIMEOUT = 3          # seconds to confirm COMPLETE status

# ═══════════════════════════════════════════════════════════════════════════════
#  TIMEZONE
# ═══════════════════════════════════════════════════════════════════════════════
TIMEZONE = "Asia/Kolkata"

# ═══════════════════════════════════════════════════════════════════════════════
#  EVENT CALENDAR — no trade within 30 min of these times
#  Format: list of (date_str, time_str) or just date_str for all‑day block
# ═══════════════════════════════════════════════════════════════════════════════
BLOCKED_EVENTS: list[dict] = [
    # Example entries — update each week / month:
    # {"date": "2026-04-01", "time": "10:00", "label": "RBI Policy"},
    # {"date": "2026-03-31", "time": "17:30", "label": "CPI Release"},
]
EVENT_BUFFER_MINUTES = 30

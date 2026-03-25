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
LOT_SIZE = 65                      # NIFTY lot size (as of Nov 2024, SEBI revised)
TRADE_LOTS = 1                     # lots per trade
TRADE_QTY = LOT_SIZE * TRADE_LOTS  # 65

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
# Full-day trading — quality enforced by per-session signal thresholds below
TRADE_WINDOWS = [
    ("09:15", "15:15"),   # full day — quality filtered by MIDDAY_MIN_STRENGTH
]
FORCE_EXIT_TIME = "15:15"

# ─── Midday quality gate (11:25–13:30: chop, low liquidity, fake breakouts) ──
# During this band the bot still trades, but requires a much stronger signal.
MIDDAY_START           = "11:25"   # start of elevated-threshold zone
MIDDAY_END             = "13:30"   # end of elevated-threshold zone
MIDDAY_MIN_STRENGTH    = 65        # vs MIN_SIGNAL_STRENGTH=45 at normal hours
MIDDAY_MIN_CONFIRMATIONS = 3       # vs MIN_CONFIRMATIONS=2 at normal hours
NO_TRADE_ZONE_START = "11:30"      # kept for reference / scoring module
NO_TRADE_ZONE_END   = "13:30"

# Duplicate signal cooldown (seconds) — also enforced via Redis on Vercel
DUPLICATE_SIGNAL_COOLDOWN = 120   # 2 minutes (short cooldown, allow frequent trades)

# Post-SL block: block same direction for this many seconds after a SL hit
SL_BLOCK_DURATION = 300           # 5 minutes (was 20 min — too restrictive)

# Per-period overtrading cap (max new auto-trades in a rolling window)
MAX_TRADES_PER_15MIN = 10         # effectively unlimited within 15 minutes

# Hard safety brakes
MAX_CONSECUTIVE_SL = 2            # pause trading after this many SL hits in a row
MAX_INTRADAY_DRAWDOWN = 5000      # stop new entries if peak-to-trough drawdown exceeds this (₹5000)

# Option premium quality band (avoid deep OTM junk and over-expensive contracts)
# Widened: ATM premiums range ₹50-₹400+ depending on DTE and volatility.
# Previous band ₹120-₹280 was too narrow and blocked most early-day trades.
MIN_ENTRY_PREMIUM = 50.0
MAX_ENTRY_PREMIUM = 500.0

# ═══════════════════════════════════════════════════════════════════════════════
#  EXIT / RISK MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════
INITIAL_SL_POINTS = 20        # fallback SL when ATR unavailable
SL_PCT_OF_PREMIUM = 0.10      # SL = 10% of option premium (overrides fixed points when using percentage mode)
SL_MODE = "percent"           # 'fixed' = INITIAL_SL_POINTS, 'percent' = SL_PCT_OF_PREMIUM * premium
SL_MIN_POINTS = 15            # percentage SL floor (never less than this)
SL_MAX_POINTS = 40            # percentage SL ceiling (never more than this)
TRAIL_START_POINTS = 25       # start trailing after 25 pts profit (62.5% of target)
TRAILING_SL_POINTS = 15       # trail SL distance once trailing activates
PARTIAL_EXIT_POINTS = 30      # take 50% off at 30 pts profit (75% of target)
PARTIAL_EXIT_PCT = 0.50       # sell 50% at partial target

MAX_LOTS_PER_TRADE = 2
MAX_OPEN_TRADES = 5           # allow multiple concurrent trades
MAX_DAILY_LOSS = 999999       # no daily loss limit
MAX_DAILY_TRADES = 999        # no daily trade cap

# ═══════════════════════════════════════════════════════════════════════════════
#  MARKET REGIME FILTERS  (sideways / trend detection)
# ═══════════════════════════════════════════════════════════════════════════════
# ADX-based chop filter — skip all signals when market is sideways
ADX_PERIOD = 14
ADX_SIDEWAYS_THRESHOLD = 14      # ADX < 14 → choppy/sideways → NO TRADE (was 18, too strict on 1m)
ADX_STRONG_TREND = 25            # ADX ≥ 25 → confirmed trend → prefer trading

# Higher-timeframe bias (15m EMA cross) — 1m signal must align
HTF_ENABLED = True               # enable 15m bias gate
HTF_EMA_FAST = 9                 # 15m fast EMA
HTF_EMA_SLOW = 21                # 15m slow EMA

# Entry signal quality floor
MIN_SIGNAL_STRENGTH = 45         # require strong signals only (was 25 — too permissive)

# Confirmation candle minimum body ratio (rejects dojis/spinning tops)
# Doji < 0.10, spinning top 0.10–0.20, normal candle > 0.25
CONFIRM_CANDLE_MIN_BODY_RATIO = 0.10  # was 0.18, too strict on 1m candles

# Opening range breakout quality filter (avoid random midday micro-breaks)
# DISABLED: on 1m NIFTY this blocks 09:15-09:30 entirely and requires breakout
# beyond OR high/low which is too strict for index candle trading.
OPENING_RANGE_FILTER_ENABLED = False
OPENING_RANGE_END = "09:30"      # build OR from 09:15 to 09:30 on 1m bars
OPENING_RANGE_BUFFER = 4.0        # points beyond OR high/low required (was 8, too wide)

# Candle quality: reject stretched bars that often mean late entry/chasing
MAX_ENTRY_BAR_ATR_MULT = 2.2      # if (bar range / ATR) > this, skip as over-extended

# ═══════════════════════════════════════════════════════════════════════════════
#  AI CONFIDENCE FILTER
# ═══════════════════════════════════════════════════════════════════════════════
# AI is used as a FILTER after rule-based ENTER is confirmed, not as a trigger
# If AI confidence score (0–100) is below this threshold, the trade is skipped
AI_FILTER_ENABLED = False         # disabled until rule engine generates enough trades to evaluate
AI_MIN_CONFIDENCE = 55           # skip trade if AI rates it below 55/100
AI_FILTER_TIMEOUT = 8            # seconds — if AI takes longer, fall through

# ═══════════════════════════════════════════════════════════════════════════════
#  20-DAY AVG + LINEAR REGRESSION STRATEGY
# ═══════════════════════════════════════════════════════════════════════════════
LINREG_PERIOD = 14                # LinReg period for daily & 1m
LINREG_FLAT_SLOPE_THRESH = 0.5    # abs(slope) below this → FLAT
LINREG_1M_FLAT_SLOPE_THRESH = 0.1 # 1m LinReg flat threshold (smaller scale)
THETA_MORNING_END = "11:30"       # full confidence zone ends
THETA_MIDDAY_END = "13:30"        # midday zone ends (need stronger signals)
THETA_STRONG_SIGNAL_HOUR = "13:30"  # after this, require both slopes strongly aligned
THETA_BLOCK_HOUR = "15:15"       # after this, no new entries (theta too punishing)

# ═══════════════════════════════════════════════════════════════════════════════
#  PAPER TRADING PORTFOLIO
# ═══════════════════════════════════════════════════════════════════════════════
PAPER_INITIAL_CAPITAL = 40_000  # ₹40,000 starting balance

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
MARKET_OPEN_TIME = "09:15"         # IST cash market open
MARKET_CLOSE_TIME = "15:30"        # IST cash market close
WS_NO_TICK_PAUSE_SECONDS = 15     # pause trading if no tick for this long
ORDER_CONFIRM_TIMEOUT = 3          # seconds to confirm COMPLETE status

# ═══════════════════════════════════════════════════════════════════════════════
#  OPENAI (LLM ANALYSIS)
# ═══════════════════════════════════════════════════════════════════════════════
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

# ═══════════════════════════════════════════════════════════════════════════════
#  GITHUB (AI AUTO-APPLY SUGGESTIONS)
# ═══════════════════════════════════════════════════════════════════════════════
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO: str = os.getenv("GITHUB_REPO", "sivaramkakakrla/TradingBot_AngelOne")

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

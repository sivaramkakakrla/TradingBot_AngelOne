from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(slots=True)
class ORBConfig:
    # ORB setup
    orb_start: str = "09:15"
    orb_end: str = "09:30"
    last_entry_time: str = "14:30"
    force_exit_time: str = "15:15"
    candle_interval: str = "ONE_MINUTE"

    # Breakout confirmation
    require_volume_spike: bool = True
    volume_spike_mult: float = 1.4
    volume_lookback: int = 20
    min_breakout_close_buffer: float = 1.0  # points beyond ORB high/low

    # Risk/target
    use_opposite_orb_sl: bool = True
    fixed_sl_points: float = 18.0
    rr_ratio: float = 1.5
    trail_mode: str = "orb_mid"  # orb_mid or prev_candle

    # Risk manager
    max_trades_per_day: int = 2
    max_reentry_per_direction: int = 1
    revenge_cooldown_minutes: int = 15

    # No-trade filters
    skip_low_vol_day: bool = True
    min_orb_range_points: float = 22.0
    gap_filter_enabled: bool = True
    max_gap_pct: float = 1.0
    news_event_block: bool = False

    # Execution
    paper_mode: bool = True
    live_mode: bool = False
    lots: int = 1
    poll_seconds: int = 3
    retry_attempts: int = 3
    retry_sleep_seconds: float = 1.2

    # Notifications
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @staticmethod
    def from_env() -> "ORBConfig":
        return ORBConfig(
            require_volume_spike=os.getenv("ORB_REQUIRE_VOLUME_SPIKE", "1") == "1",
            volume_spike_mult=float(os.getenv("ORB_VOLUME_SPIKE_MULT", "1.4")),
            min_breakout_close_buffer=float(os.getenv("ORB_BREAKOUT_BUFFER", "1.0")),
            use_opposite_orb_sl=os.getenv("ORB_USE_OPPOSITE_ORB_SL", "1") == "1",
            fixed_sl_points=float(os.getenv("ORB_FIXED_SL_POINTS", "18.0")),
            rr_ratio=float(os.getenv("ORB_RR_RATIO", "1.5")),
            trail_mode=os.getenv("ORB_TRAIL_MODE", "orb_mid"),
            max_trades_per_day=int(os.getenv("ORB_MAX_TRADES_PER_DAY", "2")),
            max_reentry_per_direction=int(os.getenv("ORB_MAX_REENTRY_PER_DIRECTION", "1")),
            revenge_cooldown_minutes=int(os.getenv("ORB_REVENGE_COOLDOWN_MIN", "15")),
            skip_low_vol_day=os.getenv("ORB_SKIP_LOW_VOL_DAY", "1") == "1",
            min_orb_range_points=float(os.getenv("ORB_MIN_ORB_RANGE", "22.0")),
            gap_filter_enabled=os.getenv("ORB_GAP_FILTER", "1") == "1",
            max_gap_pct=float(os.getenv("ORB_MAX_GAP_PCT", "1.0")),
            news_event_block=os.getenv("ORB_NEWS_EVENT_BLOCK", "0") == "1",
            paper_mode=os.getenv("ORB_PAPER_MODE", "1") == "1",
            live_mode=os.getenv("ORB_LIVE_MODE", "0") == "1",
            lots=int(os.getenv("ORB_LOTS", "1")),
            poll_seconds=int(os.getenv("ORB_POLL_SECONDS", "3")),
            retry_attempts=int(os.getenv("ORB_RETRY_ATTEMPTS", "3")),
            retry_sleep_seconds=float(os.getenv("ORB_RETRY_SLEEP", "1.2")),
            telegram_enabled=os.getenv("ORB_TELEGRAM_ENABLED", "0") == "1",
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        )

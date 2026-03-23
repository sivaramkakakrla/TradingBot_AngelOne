from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(slots=True)
class Reversal180Config:
    # Session windows
    orb_start: str = "09:15"
    orb_end: str = "09:30"
    signal_start: str = "09:30"
    last_entry_time: str = "14:30"
    force_exit_time: str = "15:10"

    # Entry quality filters
    min_reversal_body_ratio: float = 0.55
    require_volume_spike: bool = True
    volume_spike_mult: float = 1.4
    volume_lookback: int = 20

    # Risk settings
    sl_pct: float = 0.20
    rr_ratio: float = 2.0
    capital_per_trade: float = 30000.0
    quantity_lots: int = 1
    max_trades_per_day: int = 2
    max_one_trade_per_direction: bool = True

    # Regime filters
    min_orb_range_points: float = 22.0
    low_vol_skip_range_points: float = 16.0
    news_event_block: bool = False

    # Execution settings
    paper_mode: bool = True
    live_mode: bool = False
    poll_seconds: int = 5
    retry_attempts: int = 3
    retry_sleep_seconds: float = 1.5

    # Optional websocket switch
    use_websocket_if_available: bool = True

    @staticmethod
    def from_env() -> "Reversal180Config":
        return Reversal180Config(
            sl_pct=float(os.getenv("R180_SL_PCT", "0.20")),
            rr_ratio=float(os.getenv("R180_RR_RATIO", "2.0")),
            max_trades_per_day=int(os.getenv("R180_MAX_TRADES_PER_DAY", "2")),
            capital_per_trade=float(os.getenv("R180_CAPITAL_PER_TRADE", "30000")),
            quantity_lots=int(os.getenv("R180_QUANTITY_LOTS", "1")),
            paper_mode=os.getenv("R180_PAPER_MODE", "1") == "1",
            live_mode=os.getenv("R180_LIVE_MODE", "0") == "1",
            require_volume_spike=os.getenv("R180_REQUIRE_VOLUME_SPIKE", "1") == "1",
            min_orb_range_points=float(os.getenv("R180_MIN_ORB_RANGE_POINTS", "22.0")),
            low_vol_skip_range_points=float(os.getenv("R180_LOW_VOL_SKIP_RANGE_POINTS", "16.0")),
            news_event_block=os.getenv("R180_NEWS_EVENT_BLOCK", "0") == "1",
            poll_seconds=int(os.getenv("R180_POLL_SECONDS", "5")),
        )

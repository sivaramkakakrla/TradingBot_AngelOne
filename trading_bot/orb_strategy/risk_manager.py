from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from .config import ORBConfig


class ORBRiskManager:
    def __init__(self, cfg: ORBConfig):
        self.cfg = cfg
        self.daily_trades = defaultdict(int)
        self.reentries = defaultdict(lambda: {"BUY_CE": 0, "BUY_PE": 0})
        self.last_sl_time = defaultdict(lambda: {"BUY_CE": None, "BUY_PE": None})

    def _day(self, ts: str) -> str:
        try:
            return datetime.fromisoformat(ts).strftime("%Y-%m-%d")
        except Exception:
            return str(ts)[:10]

    def can_trade(self, ts: str, side: str) -> tuple[bool, str]:
        day = self._day(ts)
        if self.daily_trades[day] >= self.cfg.max_trades_per_day:
            return False, f"max trades reached ({self.cfg.max_trades_per_day})"

        if self.reentries[day][side] > self.cfg.max_reentry_per_direction:
            return False, f"re-entry limit reached for {side}"

        last_sl = self.last_sl_time[day][side]
        if last_sl is not None:
            now = datetime.fromisoformat(ts)
            if now - last_sl < timedelta(minutes=self.cfg.revenge_cooldown_minutes):
                return False, "revenge cooldown active"

        return True, ""

    def register_entry(self, ts: str, side: str) -> None:
        day = self._day(ts)
        self.daily_trades[day] += 1
        self.reentries[day][side] += 1

    def register_sl(self, ts: str, side: str) -> None:
        day = self._day(ts)
        self.last_sl_time[day][side] = datetime.fromisoformat(ts)

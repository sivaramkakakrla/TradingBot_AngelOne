from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .config import Reversal180Config


class RiskManager:
    def __init__(self, cfg: Reversal180Config):
        self.cfg = cfg
        self._daily_count = defaultdict(int)
        self._dir_traded = defaultdict(set)

    def _day(self, timestamp: str) -> str:
        try:
            return datetime.fromisoformat(timestamp).strftime("%Y-%m-%d")
        except Exception:
            return str(timestamp)[:10]

    def can_take(self, timestamp: str, side: str) -> tuple[bool, str]:
        day = self._day(timestamp)
        if self._daily_count[day] >= self.cfg.max_trades_per_day:
            return False, f"Daily trade cap reached ({self.cfg.max_trades_per_day})"

        if self.cfg.max_one_trade_per_direction:
            if side in self._dir_traded[day]:
                return False, f"Direction already traded today: {side}"

        hhmm = str(timestamp).replace("T", " ").split(" ")[-1][:5]
        if hhmm > self.cfg.last_entry_time:
            return False, f"No entries after {self.cfg.last_entry_time}"

        return True, ""

    def register(self, timestamp: str, side: str) -> None:
        day = self._day(timestamp)
        self._daily_count[day] += 1
        self._dir_traded[day].add(side)

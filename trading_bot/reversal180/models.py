from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class ORBRange:
    date: str
    high: float
    low: float

    @property
    def range_points(self) -> float:
        return self.high - self.low


@dataclass(slots=True)
class ReversalSignal:
    timestamp: str
    side: str            # BUY_CE or BUY_PE
    reason: str
    orb_high: float
    orb_low: float
    breakout_high: float
    breakout_low: float
    underlying_price: float


@dataclass(slots=True)
class OpenTrade:
    trade_id: str
    timestamp: str
    symbol: str
    broker_symbol: str
    token: str
    option_type: str
    side: str
    quantity: int
    entry_price: float
    sl_price: float
    target_price: float
    reason: str


@dataclass(slots=True)
class ClosedTrade:
    trade_id: str
    timestamp: str
    symbol: str
    option_type: str
    side: str
    quantity: int
    entry_price: float
    exit_price: float
    pnl: float
    exit_reason: str
    note: str

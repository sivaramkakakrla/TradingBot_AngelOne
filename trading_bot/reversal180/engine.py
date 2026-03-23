from __future__ import annotations

import time

from trading_bot.utils.time_utils import now_ist

from .config import Reversal180Config
from .data_feed import DataFeed
from .detector import BreakoutState, generate_failed_breakout_signal
from .models import OpenTrade
from .orb import calculate_orb
from .order_manager import OrderManager
from .risk import RiskManager
from .trade_logger import TradeLogger


class Reversal180Engine:
    def __init__(self, cfg: Reversal180Config | None = None):
        self.cfg = cfg or Reversal180Config.from_env()
        self.feed = DataFeed()
        self.orders = OrderManager(self.cfg)
        self.risk = RiskManager(self.cfg)
        self.logger = TradeLogger()
        self.state = BreakoutState()
        self._open_trade: OpenTrade | None = None

    def _within_live_window(self, hhmm: str) -> bool:
        if hhmm < self.cfg.signal_start:
            return False
        if hhmm > self.cfg.force_exit_time:
            return False
        return True

    def _skip_day(self, orb_range: float) -> bool:
        # Low-volatility skip filter
        return orb_range < self.cfg.low_vol_skip_range_points

    def run_cycle(self) -> None:
        now_hhmm = self.feed.now_hhmm()
        df = self.feed.get_5m(160)
        if df is None or len(df) < 40:
            return

        trade_date = self.feed.today_str()
        orb = calculate_orb(df, trade_date, self.cfg.orb_start, self.cfg.orb_end)
        if orb is None:
            return

        if orb.range_points < self.cfg.min_orb_range_points:
            self.logger.log_event(
                f"[{now_hhmm}] ORB range too narrow ({orb.range_points:.2f}) - skip"
            )
            return

        if self._skip_day(orb.range_points):
            self.logger.log_event(
                f"[{now_hhmm}] Low-volatility day (ORB={orb.range_points:.2f}) - no trade"
            )
            return

        # Manage existing trade first
        if self._open_trade is not None:
            ltp = self.feed.get_option_ltp(self._open_trade.token)
            closed = self.orders.maybe_exit(self._open_trade, ltp, now_hhmm, self.cfg.force_exit_time)
            if closed:
                self.logger.log_close(closed)
                self._open_trade = None
            return

        if not self._within_live_window(now_hhmm):
            return

        signal = generate_failed_breakout_signal(df, orb, self.state, self.cfg)
        if signal is None:
            return

        self.logger.log_event(f"[{now_hhmm}] Breakout detected / failed breakout confirmed")
        ok, reason = self.risk.can_take(signal.timestamp, signal.side)
        if not ok:
            self.logger.log_event(f"[{now_hhmm}] Signal blocked by risk manager: {reason}")
            return

        nifty = self.feed.get_nifty_ltp()
        if nifty <= 0:
            self.logger.log_event(f"[{now_hhmm}] No NIFTY LTP - skip")
            return

        trade = self.orders.enter(signal, nifty)
        self._open_trade = trade
        self.risk.register(signal.timestamp, signal.side)
        self.logger.log_open(trade)

    def run_live(self) -> None:
        self.logger.log_event("Starting 180 reversal engine")
        while True:
            try:
                self.run_cycle()
            except Exception as exc:
                self.logger.log_event(f"[{now_ist().strftime('%H:%M:%S')}] Engine error: {exc}")
            time.sleep(self.cfg.poll_seconds)

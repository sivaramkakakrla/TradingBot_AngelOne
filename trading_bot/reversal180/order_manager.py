from __future__ import annotations

import time
import uuid

from trading_bot import config
from trading_bot.auth.login import get_session
from trading_bot.options import find_atm_option, format_option_name, get_option_ltp

from .config import Reversal180Config
from .models import ReversalSignal, OpenTrade, ClosedTrade


class OrderManager:
    def __init__(self, cfg: Reversal180Config):
        self.cfg = cfg

    def _retry(self, fn, *args, **kwargs):
        last_exc = None
        for _ in range(self.cfg.retry_attempts):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                time.sleep(self.cfg.retry_sleep_seconds)
        if last_exc:
            raise last_exc
        raise RuntimeError("retry failed")

    def enter(self, signal: ReversalSignal, nifty_ltp: float) -> OpenTrade:
        option_type = "PE" if signal.side == "BUY_PE" else "CE"
        session = get_session()

        opt = self._retry(find_atm_option, nifty_ltp, option_type)
        if not opt:
            raise RuntimeError(f"No ATM {option_type} contract found")

        ltp_map = self._retry(get_option_ltp, session, [opt["token"]])
        entry = float(ltp_map.get(opt["token"], 0.0))
        if entry <= 0:
            raise RuntimeError("Option LTP not available")

        sl = round(entry * (1.0 - self.cfg.sl_pct), 2)
        tg = round(entry + (entry - sl) * self.cfg.rr_ratio, 2)

        symbol_name = format_option_name(opt["strike"], option_type, opt["expiry"])
        lot_size = int(opt["lotsize"])
        max_lots_by_budget = int(self.cfg.capital_per_trade // max(entry * lot_size, 1.0))
        tradable_lots = min(max_lots_by_budget, max(1, self.cfg.quantity_lots))
        if tradable_lots <= 0:
            raise RuntimeError(
                "Budget too low for entry "
                f"(capital={self.cfg.capital_per_trade:.0f}, entry={entry:.2f}, lot_size={lot_size})"
            )
        qty = lot_size * tradable_lots

        # Live mode order placement (optional)
        if self.cfg.live_mode and not self.cfg.paper_mode:
            orderparams = {
                "variety": "NORMAL",
                "tradingsymbol": opt["symbol"],
                "symboltoken": opt["token"],
                "transactiontype": "BUY",
                "exchange": config.NFO_EXCHANGE,
                "ordertype": "MARKET",
                "producttype": "INTRADAY",
                "duration": "DAY",
                "quantity": qty,
            }
            self._retry(session.placeOrder, orderparams)

        return OpenTrade(
            trade_id=f"R180-{uuid.uuid4().hex[:8].upper()}",
            timestamp=signal.timestamp,
            symbol=symbol_name,
            broker_symbol=opt["symbol"],
            token=opt["token"],
            option_type=option_type,
            side=signal.side,
            quantity=qty,
            entry_price=entry,
            sl_price=sl,
            target_price=tg,
            reason=signal.reason,
        )

    def maybe_exit(self, trade: OpenTrade, option_ltp: float, now_hhmm: str, force_exit_time: str) -> ClosedTrade | None:
        if option_ltp <= 0:
            return None

        reason = ""
        exit_px = option_ltp

        if option_ltp <= trade.sl_price:
            reason = "SL_HIT"
            exit_px = trade.sl_price
        elif option_ltp >= trade.target_price:
            reason = "TARGET_HIT"
            exit_px = trade.target_price
        elif now_hhmm >= force_exit_time:
            reason = "TIME_EXIT"
            exit_px = option_ltp

        if not reason:
            return None

        pnl = round((exit_px - trade.entry_price) * trade.quantity, 2)

        # Live mode exit
        if self.cfg.live_mode and not self.cfg.paper_mode:
            session = get_session()
            orderparams = {
                "variety": "NORMAL",
                "tradingsymbol": trade.broker_symbol,
                "symboltoken": trade.token,
                "transactiontype": "SELL",
                "exchange": config.NFO_EXCHANGE,
                "ordertype": "MARKET",
                "producttype": "INTRADAY",
                "duration": "DAY",
                "quantity": trade.quantity,
            }
            self._retry(session.placeOrder, orderparams)

        return ClosedTrade(
            trade_id=trade.trade_id,
            timestamp=str(now_hhmm),
            symbol=trade.symbol,
            option_type=trade.option_type,
            side=trade.side,
            quantity=trade.quantity,
            entry_price=trade.entry_price,
            exit_price=exit_px,
            pnl=pnl,
            exit_reason=reason,
            note=trade.reason,
        )

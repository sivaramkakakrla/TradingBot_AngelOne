from __future__ import annotations

import csv
import datetime
import os
import time
import uuid

import requests

from trading_bot import config as global_config
from trading_bot.auth.login import get_session
from trading_bot.options import find_atm_option, format_option_name
from trading_bot.utils.logger import get_logger

from .config import ORBConfig
from .data_handler import ORBDataHandler
from .risk_manager import ORBRiskManager
from .strategy_orb import ORBStrategy

log = get_logger(__name__)


class ORBExecutionEngine:
    def __init__(self, cfg: ORBConfig):
        self.cfg = cfg
        self.data = ORBDataHandler(cfg)
        self.strategy = ORBStrategy(cfg)
        self.risk = ORBRiskManager(cfg)
        self.open_trade = None
        self.csv_path = self._trade_csv_path()
        self._ensure_csv()

    def _trade_csv_path(self) -> str:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        p = os.path.join(root, "logs", "orb_strategy_trades.csv")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def _ensure_csv(self) -> None:
        if os.path.exists(self.csv_path):
            return
        with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "trade_id", "instrument", "side", "entry", "exit", "pnl", "reason", "note"])

    def _log(self, msg: str) -> None:
        log.info(msg)
        print(msg, flush=True)

    def _notify(self, text: str) -> None:
        if not self.cfg.telegram_enabled:
            return
        if not self.cfg.telegram_bot_token or not self.cfg.telegram_chat_id:
            return
        url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage"
        try:
            requests.post(url, json={"chat_id": self.cfg.telegram_chat_id, "text": text}, timeout=8)
        except Exception:
            pass

    def _append_trade(self, row: list) -> None:
        with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)

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

    def _now(self) -> tuple[str, str]:
        n = datetime.datetime.now().astimezone()
        return n.isoformat(timespec="seconds"), n.strftime("%H:%M")

    def _place_entry(self, side: str, spot_ltp: float, reason: str, stop_ref: float) -> None:
        option_type = "CE" if side == "BUY_CE" else "PE"
        opt = self._retry(find_atm_option, spot_ltp, option_type)
        if not opt:
            self._log("[WARN] No ATM option found")
            return

        entry_ltp = self.data.get_option_ltp(opt["token"])
        if entry_ltp <= 0:
            self._log("[WARN] Option LTP unavailable")
            return

        if self.cfg.use_opposite_orb_sl:
            sl = max(1.0, entry_ltp - abs(spot_ltp - stop_ref) * 0.35)
        else:
            sl = max(1.0, entry_ltp - self.cfg.fixed_sl_points)

        tg = entry_ltp + (entry_ltp - sl) * self.cfg.rr_ratio

        lot_size = int(opt["lotsize"])
        max_lots_by_budget = int(self.cfg.capital_per_trade // max(entry_ltp * lot_size, 1.0))
        tradable_lots = min(max_lots_by_budget, max(1, self.cfg.lots))
        if tradable_lots <= 0:
            self._log(
                f"[WARN] Budget too low for entry: capital={self.cfg.capital_per_trade:.0f}, "
                f"entry={entry_ltp:.2f}, lot_size={lot_size}"
            )
            return
        qty = lot_size * tradable_lots
        trade_id = f"ORB-{uuid.uuid4().hex[:8].upper()}"
        symbol = format_option_name(opt["strike"], option_type, opt["expiry"])

        if self.cfg.live_mode and not self.cfg.paper_mode:
            session = get_session()
            orderparams = {
                "variety": "NORMAL",
                "tradingsymbol": opt["symbol"],
                "symboltoken": opt["token"],
                "transactiontype": "BUY",
                "exchange": global_config.NFO_EXCHANGE,
                "ordertype": "MARKET",
                "producttype": "INTRADAY",
                "duration": "DAY",
                "quantity": qty,
            }
            self._retry(session.placeOrder, orderparams)

        ts, _ = self._now()
        self.open_trade = {
            "trade_id": trade_id,
            "timestamp": ts,
            "symbol": symbol,
            "broker_symbol": opt["symbol"],
            "token": opt["token"],
            "side": side,
            "qty": qty,
            "entry": float(entry_ltp),
            "sl": float(round(sl, 2)),
            "tg": float(round(tg, 2)),
            "trail": float(round(sl, 2)),
            "reason": reason,
        }

        msg = (
            f"[{ts}] {side} executed at Rs.{entry_ltp:.2f} | {symbol} | "
            f"lots={tradable_lots} qty={qty} cap={self.cfg.capital_per_trade:.0f} | "
            f"SL={sl:.2f} TG={tg:.2f}"
        )
        self._log(msg)
        self._notify(msg)

    def _trail_stop(self, latest_close: float, prev_high: float, prev_low: float, orb_mid: float) -> None:
        if self.open_trade is None:
            return
        if self.cfg.trail_mode == "prev_candle":
            if self.open_trade["side"] == "BUY_CE":
                new_trail = max(self.open_trade["trail"], prev_low)
            else:
                # for PE, trail based on opposite movement proxy via orb midpoint
                new_trail = max(self.open_trade["trail"], orb_mid)
        else:
            new_trail = max(self.open_trade["trail"], orb_mid)
        self.open_trade["trail"] = float(new_trail)

    def _try_exit(self, hhmm: str) -> None:
        t = self.open_trade
        if t is None:
            return

        ltp = self.data.get_option_ltp(t["token"])
        if ltp <= 0:
            return

        reason = ""
        exit_px = ltp

        if ltp <= t["trail"]:
            reason = "TRAIL_SL_HIT"
            exit_px = t["trail"]
        elif ltp <= t["sl"]:
            reason = "SL_HIT"
            exit_px = t["sl"]
        elif ltp >= t["tg"]:
            reason = "TARGET_HIT"
            exit_px = t["tg"]
        elif hhmm >= self.cfg.force_exit_time:
            reason = "TIME_EXIT"
            exit_px = ltp

        if not reason:
            return

        if self.cfg.live_mode and not self.cfg.paper_mode:
            session = get_session()
            orderparams = {
                "variety": "NORMAL",
                "tradingsymbol": t["broker_symbol"],
                "symboltoken": t["token"],
                "transactiontype": "SELL",
                "exchange": global_config.NFO_EXCHANGE,
                "ordertype": "MARKET",
                "producttype": "INTRADAY",
                "duration": "DAY",
                "quantity": t["qty"],
            }
            self._retry(session.placeOrder, orderparams)

        pnl = round((exit_px - t["entry"]) * t["qty"], 2)
        ts, _ = self._now()
        self._append_trade([
            ts,
            t["trade_id"],
            t["symbol"],
            t["side"],
            f"{t['entry']:.2f}",
            f"{exit_px:.2f}",
            f"{pnl:.2f}",
            reason,
            t["reason"],
        ])

        msg = f"[{ts}] {t['symbol']} exit at Rs.{exit_px:.2f} | PnL={pnl:.2f} | {reason}"
        self._log(msg)
        self._notify(msg)

        if reason in ("SL_HIT", "TRAIL_SL_HIT"):
            self.risk.register_sl(ts, t["side"])

        self.open_trade = None

    def run_once(self) -> None:
        ts, hhmm = self._now()

        df = self.data.get_1m_candles_today()
        if df is None or len(df) < 30:
            return

        orb = self.strategy.compute_orb(df)
        no_trade = self.strategy.no_trade_reason(df, orb)
        if no_trade:
            if hhmm in ("09:31", "09:35", "10:00"):
                self._log(f"[{ts}] No-trade filter active: {no_trade}")
            return

        # Manage open trade
        if self.open_trade is not None:
            if len(df) >= 2:
                prev = df.iloc[-2]
                self._trail_stop(
                    latest_close=float(df.iloc[-1]["close"]),
                    prev_high=float(prev["high"]),
                    prev_low=float(prev["low"]),
                    orb_mid=orb.midpoint,
                )
            self._try_exit(hhmm)
            return

        signal = self.strategy.generate_signal(df, orb)
        if signal is None:
            return

        ok, reason = self.risk.can_trade(signal.timestamp, signal.side)
        if not ok:
            self._log(f"[{ts}] Signal blocked: {reason}")
            return

        spot = self.data.get_spot_ltp()
        if spot <= 0:
            self._log(f"[{ts}] Spot LTP unavailable")
            return

        self.risk.register_entry(signal.timestamp, signal.side)
        self._place_entry(signal.side, spot, signal.reason, signal.stop_reference)

    def run_forever(self) -> None:
        self._log("Starting ORB strategy engine")
        while True:
            try:
                self.run_once()
            except Exception as exc:
                ts, _ = self._now()
                self._log(f"[{ts}] Engine error: {exc}")
            time.sleep(self.cfg.poll_seconds)

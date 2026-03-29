from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .config import ORBConfig


@dataclass(slots=True)
class ORBLevels:
    high: float
    low: float
    midpoint: float
    ready: bool


@dataclass(slots=True)
class ORBSignal:
    timestamp: str
    side: str  # BUY_CE | BUY_PE
    reason: str
    orb_high: float
    orb_low: float
    stop_reference: float


def _hhmm(ts: str) -> str:
    s = str(ts).replace("T", " ")
    t = s.split(" ")[-1]
    return t[:5]


def _calc_orb(df: pd.DataFrame, cfg: ORBConfig) -> ORBLevels:
    if df is None or df.empty:
        return ORBLevels(0.0, 0.0, 0.0, False)
    mask = df["timestamp"].astype(str).apply(lambda x: cfg.orb_start <= _hhmm(x) <= cfg.orb_end)
    r = df[mask]
    if r.empty:
        return ORBLevels(0.0, 0.0, 0.0, False)
    high = float(r["high"].max())
    low = float(r["low"].min())
    return ORBLevels(high, low, (high + low) / 2.0, True)


def _is_wick_only_breakout(row: pd.Series, orb_high: float, orb_low: float, side: str) -> bool:
    h = float(row["high"])
    l = float(row["low"])
    c = float(row["close"])
    if side == "BUY_CE":
        return h > orb_high and c <= orb_high
    return l < orb_low and c >= orb_low


def _volume_spike_ok(df: pd.DataFrame, idx: int, cfg: ORBConfig) -> bool:
    if not cfg.require_volume_spike:
        return True
    if "volume" not in df.columns or idx < cfg.volume_lookback:
        return False
    avg = float(df["volume"].iloc[idx - cfg.volume_lookback:idx].mean())
    if avg <= 0:
        return False
    cur = float(df["volume"].iloc[idx])
    return cur >= avg * cfg.volume_spike_mult


def _gap_pct(df: pd.DataFrame) -> float:
    if df is None or len(df) < 2:
        return 0.0
    first_open = float(df["open"].iloc[0])
    prev_close = float(df["close"].iloc[0])
    if prev_close <= 0:
        return 0.0
    return abs(first_open - prev_close) / prev_close * 100.0


class ORBStrategy:
    def __init__(self, cfg: ORBConfig):
        self.cfg = cfg
        self.last_signal_ts = ""

    def compute_orb(self, df_1m: pd.DataFrame) -> ORBLevels:
        return _calc_orb(df_1m, self.cfg)

    def no_trade_reason(self, df_1m: pd.DataFrame, orb: ORBLevels) -> str | None:
        if not orb.ready:
            return "ORB not ready"
        if orb.high - orb.low < self.cfg.min_orb_range_points and self.cfg.skip_low_vol_day:
            return "Low volatility day (narrow ORB)"
        if self.cfg.gap_filter_enabled:
            gp = _gap_pct(df_1m)
            if gp > self.cfg.max_gap_pct:
                return f"Gap too large ({gp:.2f}%)"
        return None

    def generate_signal(self, df_1m: pd.DataFrame, orb: ORBLevels) -> ORBSignal | None:
        if df_1m is None or len(df_1m) < 3 or not orb.ready:
            return None

        i = len(df_1m) - 1
        row = df_1m.iloc[i]
        ts = str(row["timestamp"])
        hhmm = _hhmm(ts)

        if hhmm <= self.cfg.orb_end:
            return None
        if hhmm > self.cfg.last_entry_time:
            return None
        if ts == self.last_signal_ts:
            return None

        close = float(row["close"])
        prev = df_1m.iloc[i - 1]

        # No-trade when still inside range
        if orb.low <= close <= orb.high:
            return None

        # BUY CE breakout above ORB high
        if close > (orb.high + self.cfg.min_breakout_close_buffer):
            if _is_wick_only_breakout(row, orb.high, orb.low, "BUY_CE"):
                return None
            if not _volume_spike_ok(df_1m, i, self.cfg):
                return None
            self.last_signal_ts = ts
            return ORBSignal(
                timestamp=ts,
                side="BUY_CE",
                reason="ORB upside breakout confirmed",
                orb_high=orb.high,
                orb_low=orb.low,
                stop_reference=orb.low,
            )

        # BUY PE breakout below ORB low
        if close < (orb.low - self.cfg.min_breakout_close_buffer):
            if _is_wick_only_breakout(row, orb.high, orb.low, "BUY_PE"):
                return None
            if not _volume_spike_ok(df_1m, i, self.cfg):
                return None
            self.last_signal_ts = ts
            return ORBSignal(
                timestamp=ts,
                side="BUY_PE",
                reason="ORB downside breakout confirmed",
                orb_high=orb.high,
                orb_low=orb.low,
                stop_reference=orb.high,
            )

        return None


def backtest_orb(df_1m: pd.DataFrame, cfg: ORBConfig) -> dict:
    strategy = ORBStrategy(cfg)
    orb = strategy.compute_orb(df_1m)
    trades: list[dict] = []

    if not orb.ready:
        return {"summary": {"total": 0, "pnl": 0.0}, "trades": []}

    # ── Quality gates ──
    _MAX_TRADES = cfg.max_trades_per_day          # default 2
    _COOLDOWN_BARS = cfg.revenge_cooldown_minutes  # ~15 bars (1-min candles)
    _ALLOWED_WINDOWS = [
        ("09:35", "11:30"),
        ("13:15", "14:30"),
    ]

    open_t = None
    trade_count = 0
    last_exit_bar = -_COOLDOWN_BARS - 1  # allow first trade immediately

    for i in range(30, len(df_1m)):
        cur = df_1m.iloc[: i + 1]
        row = cur.iloc[-1]
        hhmm = _hhmm(str(row["timestamp"]))

        # ── position open → track cumulative underlying movement ──
        if open_t is not None:
            under_now = float(row["close"])
            under_entry = open_t["under_entry"]
            sign = -1.0 if open_t["side"] == "BUY_PE" else 1.0
            under_move = (under_now - under_entry) * sign
            premium_now = open_t["entry"] + under_move * 0.50

            reason = None
            exit_px = premium_now
            if premium_now <= open_t["sl"]:
                reason = "SL_HIT"
                exit_px = open_t["sl"]
            elif premium_now >= open_t["tg"]:
                reason = "TARGET_HIT"
                exit_px = open_t["tg"]
            elif hhmm >= cfg.force_exit_time:
                reason = "TIME_EXIT"

            if reason:
                pnl = round(exit_px - open_t["entry"], 2)
                trades.append({**open_t, "exit": round(exit_px, 2), "pnl": pnl, "reason": reason})
                open_t = None
                last_exit_bar = i
            continue

        # ── gate: max trades per day ──
        if trade_count >= _MAX_TRADES:
            continue

        # ── gate: cooldown after previous exit ──
        if i - last_exit_bar < _COOLDOWN_BARS:
            continue

        # ── gate: time window ──
        in_window = any(ws <= hhmm <= we for ws, we in _ALLOWED_WINDOWS)
        if not in_window:
            continue

        sig = strategy.generate_signal(cur, orb)
        if sig is None:
            continue

        entry = 180.0
        sl = entry - cfg.fixed_sl_points
        tg = entry + cfg.fixed_sl_points * cfg.rr_ratio
        open_t = {
            "ts": sig.timestamp,
            "side": sig.side,
            "entry": entry,
            "sl": sl,
            "tg": tg,
            "under_entry": float(row["close"]),   # track entry underlying price
        }
        trade_count += 1

    # close any still-open position at last bar
    if open_t is not None:
        last = df_1m.iloc[-1]
        under_now = float(last["close"])
        sign = -1.0 if open_t["side"] == "BUY_PE" else 1.0
        under_move = (under_now - open_t["under_entry"]) * sign
        premium_now = open_t["entry"] + under_move * 0.50
        pnl = round(premium_now - open_t["entry"], 2)
        trades.append({**open_t, "exit": round(premium_now, 2), "pnl": pnl, "reason": "EOD_EXIT"})

    return {
        "summary": {
            "total": len(trades),
            "wins": sum(1 for t in trades if t["pnl"] > 0),
            "losses": sum(1 for t in trades if t["pnl"] <= 0),
            "pnl": round(sum(t["pnl"] for t in trades), 2),
        },
        "trades": trades,
    }

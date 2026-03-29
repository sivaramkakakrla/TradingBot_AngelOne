from __future__ import annotations

import pandas as pd

from .config import Reversal180Config
from .detector import BreakoutState, generate_failed_breakout_signal
from .orb import calculate_orb


def run_backtest(df_5m: pd.DataFrame, cfg: Reversal180Config | None = None) -> dict:
    """
    Backtest using the same failed-breakout signal logic.

    Option premium simulation:
      - synthetic entry premium: 180
      - stop: 20 percent (default)
      - target: RR based
      - premium move proxy from underlying move with fixed delta factor
    """
    cfg = cfg or Reversal180Config()
    if df_5m is None or len(df_5m) < 60:
        return {"trades": [], "summary": {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0}}

    trade_date = str(df_5m["timestamp"].iloc[0])[:10]
    orb = calculate_orb(df_5m, trade_date, cfg.orb_start, cfg.orb_end)
    if orb is None:
        return {"trades": [], "summary": {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0}}

    state = BreakoutState()
    trades: list[dict] = []
    open_t = None
    trade_count = 0

    # Start from bar 6 (right after ORB period: bars 0-3 cover 09:15-09:30)
    # Bar 35 ≈ 12:10 PM which missed all morning failed-breakout signals
    for i in range(6, len(df_5m)):
        cur = df_5m.iloc[: i + 1]
        row = cur.iloc[-1]
        ts = str(row["timestamp"])
        hhmm = ts.replace("T", " ").split(" ")[-1][:5]

        if open_t is not None:
            under_now = float(row["close"])
            under_entry = open_t["under_entry"]
            sign = -1.0 if open_t["side"] == "BUY_PE" else 1.0
            under_move = (under_now - under_entry) * sign
            premium_now = open_t["entry"] + under_move * 0.45

            if premium_now <= open_t["sl"]:
                pnl = open_t["sl"] - open_t["entry"]
                trades.append({**open_t, "exit_ts": ts, "exit": open_t["sl"], "pnl": round(pnl, 2), "reason": "SL_HIT"})
                open_t = None
            elif premium_now >= open_t["tg"]:
                pnl = open_t["tg"] - open_t["entry"]
                trades.append({**open_t, "exit_ts": ts, "exit": open_t["tg"], "pnl": round(pnl, 2), "reason": "TARGET_HIT"})
                open_t = None
            elif hhmm >= cfg.force_exit_time:
                pnl = premium_now - open_t["entry"]
                trades.append({**open_t, "exit_ts": ts, "exit": round(premium_now, 2), "pnl": round(pnl, 2), "reason": "TIME_EXIT"})
                open_t = None
            continue

        # max trades per day
        if trade_count >= cfg.max_trades_per_day:
            continue

        sig = generate_failed_breakout_signal(cur, orb, state, cfg)
        if not sig:
            continue

        if hhmm > cfg.last_entry_time:
            continue

        entry = 180.0
        sl = round(entry * (1 - cfg.sl_pct), 2)
        tg = round(entry + (entry - sl) * cfg.rr_ratio, 2)
        open_t = {
            "entry_ts": ts,
            "side": sig.side,
            "entry": entry,
            "sl": sl,
            "tg": tg,
            "under_entry": float(row["close"]),
            "reason": sig.reason,
        }
        trade_count += 1

    total = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = total - wins
    pnl = round(sum(t["pnl"] for t in trades), 2)

    return {
        "trades": trades,
        "summary": {
            "total": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round((wins / total) * 100, 2) if total else 0.0,
            "pnl": pnl,
        },
    }

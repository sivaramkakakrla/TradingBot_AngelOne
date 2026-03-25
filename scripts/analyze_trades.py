"""
scripts/analyze_trades.py
─────────────────────────
Reads closed AUTO trades from the SQLite database and detects:
  - Counter-trend losses  (e.g. BULLISH trend but PE trade → SL)
  - Overtrading / consecutive SL streak
  - Bad-time trades       (13:00–14:30 window)
  - Late-entry losses     (entered after large move, took SL)

Outputs analysis to:  scripts/analysis_output.json
Exports trade data to: data/trades.json (for audit / AI review)

Safety rule: analysis is IGNORED if < 5 closed trades available.

Usage:
    python scripts/analyze_trades.py          # analyze last 30 days
    python scripts/analyze_trades.py --days 7 # analyze last N days
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "trading_bot" / "trades.db"
OUTPUT_PATH = Path(__file__).parent / "analysis_output.json"
TRADES_JSON = ROOT / "data" / "trades.json"

MIN_SAMPLE = 5          # minimum trades to act on (safety gate)
BAD_TIME_START = 13     # hour (24h)
BAD_TIME_END   = 14     # inclusive — 13:00–14:59
LATE_MOVE_PTS  = 45     # points moved before entry = "late entry"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_closed_trades(days: int) -> list[dict]:
    """Return closed AUTO trades from the last `days` calendar days."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    sql = """
        SELECT trade_id, option_type, entry_price, exit_price, pnl,
               exit_reason, entry_time, exit_time, strategy, notes
        FROM   trades
        WHERE  status = 'CLOSED'
          AND  source = 'AUTO'
          AND  DATE(entry_time) >= ?
        ORDER  BY entry_time ASC
    """
    try:
        with _db() as conn:
            rows = conn.execute(sql, (since,)).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        print(f"[analyze] DB read error: {exc}", file=sys.stderr)
        return []


def _entry_hour(trade: dict) -> int | None:
    """Return the entry hour (0-23) from entry_time ISO string."""
    try:
        return datetime.fromisoformat(trade["entry_time"]).hour
    except Exception:
        return None


def _is_sl(trade: dict) -> bool:
    reason = (trade.get("exit_reason") or "").upper()
    return reason in {"SL_HIT", "SL HIT", "STOPLOSS", "STOP_LOSS", "SL"}


def _infer_trend_from_notes(trade: dict) -> str | None:
    """Best-effort: read trend tag stored in notes field by strategy."""
    notes = (trade.get("notes") or "").upper()
    if "BULLISH" in notes:
        return "BULLISH"
    if "BEARISH" in notes:
        return "BEARISH"
    if "SIDEWAYS" in notes:
        return "SIDEWAYS"
    return None


# ─────────────────────────────────────────────────────────
#  ANALYSERS
# ─────────────────────────────────────────────────────────

def detect_counter_trend_losses(trades: list[dict]) -> dict:
    """Flag: took PE during BULLISH trend or CE during BEARISH trend → SL."""
    count = 0
    total = 0
    for t in trades:
        if not _is_sl(t):
            continue
        trend = _infer_trend_from_notes(t)
        opt   = (t.get("option_type") or "").upper()
        if trend == "BULLISH" and opt == "PE":
            count += 1
        elif trend == "BEARISH" and opt == "CE":
            count += 1
        total += 1
    pct = round(count / total * 100, 1) if total else 0
    return {"counter_trend_sl_count": count, "sl_total": total, "counter_trend_pct": pct}


def detect_consecutive_sl(trades: list[dict]) -> dict:
    """Find max consecutive SL streak and flag if ≥ 2."""
    max_streak = 0
    streak = 0
    for t in trades:
        if _is_sl(t):
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return {"max_consecutive_sl": max_streak, "overtrading_detected": max_streak >= 2}


def detect_bad_time_trades(trades: list[dict]) -> dict:
    """Detect trades entered in 13:00–14:59 that resulted in SL."""
    count = 0
    for t in trades:
        h = _entry_hour(t)
        if h is not None and BAD_TIME_START <= h <= BAD_TIME_END and _is_sl(t):
            count += 1
    return {"bad_time_sl_count": count, "block_afternoon_suggested": count >= 2}


def detect_late_entries(trades: list[dict]) -> dict:
    """Flag SL trades where notes indicate a large move before entry."""
    count = 0
    for t in trades:
        if not _is_sl(t):
            continue
        notes = (t.get("notes") or "").upper()
        if "LATE" in notes or "OVEREXTENDED" in notes or "EXTENDED" in notes:
            count += 1
    return {"late_entry_sl_count": count, "tighten_late_entry_filter": count >= 2}


def compute_win_loss(trades: list[dict]) -> dict:
    wins   = [t for t in trades if (t.get("pnl") or 0) > 0]
    losses = [t for t in trades if (t.get("pnl") or 0) <= 0]
    total  = len(trades)
    win_rate = round(len(wins) / total * 100, 1) if total else 0
    avg_win  = round(sum(t["pnl"] for t in wins)   / len(wins),   2) if wins   else 0
    avg_loss = round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0
    return {
        "total": total, "wins": len(wins), "losses": len(losses),
        "win_rate_pct": win_rate, "avg_win": avg_win, "avg_loss": avg_loss,
    }


# ─────────────────────────────────────────────────────────
#  EXPORT helper
# ─────────────────────────────────────────────────────────

def export_trades_json(trades: list[dict]):
    """Write trades to data/trades.json for audit / optional AI analysis."""
    TRADES_JSON.parent.mkdir(parents=True, exist_ok=True)
    # Build structured format matching the spec
    structured = []
    for t in trades:
        try:
            entry_dt = datetime.fromisoformat(t.get("entry_time", ""))
            entry_time_str = entry_dt.strftime("%H:%M")
            date_str       = entry_dt.strftime("%Y-%m-%d")
        except Exception:
            entry_time_str = ""
            date_str       = ""

        side = (t.get("option_type") or "").upper()
        result = "SL" if _is_sl(t) else ("TARGET" if (t.get("pnl") or 0) > 0 else "MANUAL")

        # Infer trend from notes
        trend = _infer_trend_from_notes(t) or "UNKNOWN"

        structured.append({
            "date": date_str,
            "instrument": "NIFTY",
            "side": side,
            "entry": t.get("entry_price"),
            "exit":  t.get("exit_price"),
            "pnl":   t.get("pnl"),
            "result": result,
            "time": entry_time_str,
            "trend": trend,
            "exit_reason": t.get("exit_reason"),
        })

    TRADES_JSON.write_text(json.dumps(structured, indent=2), encoding="utf-8")
    print(f"[analyze] Exported {len(structured)} trades → {TRADES_JSON}")


# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Analyze daily trading patterns")
    parser.add_argument("--days", type=int, default=30, help="Look back N calendar days")
    args = parser.parse_args()

    trades = _fetch_closed_trades(args.days)
    print(f"[analyze] Found {len(trades)} closed AUTO trades in last {args.days} days")

    if len(trades) < MIN_SAMPLE:
        print(f"[analyze] Only {len(trades)} trades — below MIN_SAMPLE ({MIN_SAMPLE}). No rules updated.")
        output = {
            "sample_size": len(trades),
            "sufficient_sample": False,
            "notes": "Not enough trades to make decisions. Rules unchanged.",
        }
        OUTPUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")
        return

    # Run all analysers
    counter = detect_counter_trend_losses(trades)
    consec  = detect_consecutive_sl(trades)
    timing  = detect_bad_time_trades(trades)
    late    = detect_late_entries(trades)
    summary = compute_win_loss(trades)

    output = {
        "sample_size": len(trades),
        "sufficient_sample": True,
        "win_loss_summary": summary,
        "counter_trend": counter,
        "consecutive_sl": consec,
        "bad_time_trades": timing,
        "late_entries": late,
        # Rule recommendations (used by update_config.py)
        "recommendations": {
            "blockCounterTrend":  counter["counter_trend_pct"] >= 30,
            "stopAfterLosses":    consec["overtrading_detected"],
            "blockAfternoon":     timing["block_afternoon_suggested"],
            "tightenLateEntry":   late["tighten_late_entry_filter"],
            "suggestedMaxLosses": max(2, 3 - (1 if consec["max_consecutive_sl"] >= 3 else 0)),
        },
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    OUTPUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"[analyze] Analysis written → {OUTPUT_PATH}")
    print(f"[analyze] Win rate: {summary['win_rate_pct']}%  "
          f"Avg win: ₹{summary['avg_win']}  Avg loss: ₹{summary['avg_loss']}")
    print(f"[analyze] Recommendations: {output['recommendations']}")

    # Also export trades.json
    export_trades_json(trades)


if __name__ == "__main__":
    main()

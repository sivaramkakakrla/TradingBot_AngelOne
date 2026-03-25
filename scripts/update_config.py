"""
scripts/update_config.py
────────────────────────
Reads scripts/analysis_output.json and updates config/auto_config.json.

Safety rules (CRITICAL):
  - NEVER modifies trading_bot/config.py (core logic is immutable)
  - Max 1 rule change per run
  - Ignored if sample_size < 5
  - All changes appended to _audit_log inside auto_config.json

Usage:
    python scripts/update_config.py
    python scripts/update_config.py --dry-run   # preview changes only
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

ROOT          = Path(__file__).parent.parent
ANALYSIS_PATH = Path(__file__).parent / "analysis_output.json"
CONFIG_PATH   = ROOT / "config" / "auto_config.json"

MIN_SAMPLE      = 5
MAX_CHANGES_PER_RUN = 1   # apply at most 1 rule change per day (stability)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _audit(cfg: dict, field: str, old_val, new_val, reason: str):
    """Append one entry to the audit log inside auto_config.json."""
    entry = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "field": field,
        "from": old_val,
        "to":   new_val,
        "reason": reason,
    }
    cfg.setdefault("_audit_log", []).append(entry)
    print(f"[update_config] RULE CHANGE: {field}  {old_val!r} → {new_val!r}  ({reason})")


def main():
    parser = argparse.ArgumentParser(description="Update auto_config.json from analysis")
    parser.add_argument("--dry-run", action="store_true", help="Preview only — no file writes")
    args = parser.parse_args()

    if not ANALYSIS_PATH.exists():
        print("[update_config] No analysis_output.json found. Run analyze_trades.py first.")
        return

    analysis = _load(ANALYSIS_PATH)

    if not analysis.get("sufficient_sample", False):
        print(f"[update_config] Insufficient sample ({analysis.get('sample_size', 0)} trades). No changes.")
        return

    cfg = _load(CONFIG_PATH) if CONFIG_PATH.exists() else {}
    rec = analysis.get("recommendations", {})
    today = datetime.now().strftime("%Y-%m-%d")
    changes_applied = 0

    # ── Rule 1: Counter-trend filter ─────────────────────────────────────────
    if rec.get("blockCounterTrend") and cfg.get("allowCounterTrend", True):
        if changes_applied < MAX_CHANGES_PER_RUN:
            if not args.dry_run:
                _audit(cfg, "allowCounterTrend", True, False,
                       f"counter-trend SL rate {analysis['counter_trend']['counter_trend_pct']}% >= 30%")
                cfg["allowCounterTrend"] = False
            else:
                print("[dry-run] Would set allowCounterTrend = false")
            changes_applied += 1

    # ── Rule 2: After-hours trading ───────────────────────────────────────────
    if rec.get("blockAfternoon") and cfg.get("allowAfternoonTrading", True):
        if changes_applied < MAX_CHANGES_PER_RUN:
            if not args.dry_run:
                _audit(cfg, "allowAfternoonTrading", True, False,
                       f"afternoon SL count {analysis['bad_time_trades']['bad_time_sl_count']} >= 2")
                cfg["allowAfternoonTrading"] = False
            else:
                print("[dry-run] Would set allowAfternoonTrading = false")
            changes_applied += 1

    # ── Rule 3: Max consecutive losses threshold ──────────────────────────────
    new_max = rec.get("suggestedMaxLosses")
    old_max = cfg.get("maxConsecutiveLoss", 3)
    if new_max and new_max != old_max and new_max < old_max:
        if changes_applied < MAX_CHANGES_PER_RUN:
            if not args.dry_run:
                _audit(cfg, "maxConsecutiveLoss", old_max, new_max,
                       f"max consecutive SL streak was {analysis['consecutive_sl']['max_consecutive_sl']}")
                cfg["maxConsecutiveLoss"] = new_max
            else:
                print(f"[dry-run] Would set maxConsecutiveLoss = {new_max}")
            changes_applied += 1

    # ── Rule 4: Late-entry move threshold ─────────────────────────────────────
    if rec.get("tightenLateEntry"):
        current_thresh = cfg.get("maxMoveThreshold", 45)
        tighter = max(30, current_thresh - 5)   # tighten by 5 pts, floor at 30
        if tighter != current_thresh and changes_applied < MAX_CHANGES_PER_RUN:
            if not args.dry_run:
                _audit(cfg, "maxMoveThreshold", current_thresh, tighter,
                       f"late entry SL count {analysis['late_entries']['late_entry_sl_count']} >= 2")
                cfg["maxMoveThreshold"] = tighter
            else:
                print(f"[dry-run] Would set maxMoveThreshold = {tighter}")
            changes_applied += 1

    if changes_applied == 0:
        print("[update_config] No rule changes needed today.")
    elif not args.dry_run:
        cfg["_last_updated"] = today
        cfg["_sample_size"]  = analysis["sample_size"]
        _save(CONFIG_PATH, cfg)
        print(f"[update_config] Saved {changes_applied} change(s) → {CONFIG_PATH}")

    if args.dry_run:
        print(f"[dry-run] {changes_applied} change(s) would have been applied.")


if __name__ == "__main__":
    main()

"""
scripts/daily_pipeline.py
─────────────────────────
Daily automation pipeline:
  1. Analyze recent trades        (analyze_trades.py)
  2. Update dynamic config        (update_config.py)
  3. Git commit + push to GitHub  (auto-deploys on Vercel)

Run this once per day after market close (e.g. 15:45 IST).

Usage:
    python scripts/daily_pipeline.py
    python scripts/daily_pipeline.py --dry-run    # skip git push
    python scripts/daily_pipeline.py --days 7     # analyse last 7 days

Schedule with Windows Task Scheduler or cron:
    # Linux/Mac cron — 15:45 IST (10:15 UTC)
    15 10 * * 1-5  cd /path/to/project && python scripts/daily_pipeline.py

    # Windows Task Scheduler — Action:
    Program: python
    Arguments: scripts/daily_pipeline.py
    Start in: C:\\Users\\Sushma\\Desktop\\AngelOne
"""

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT    = Path(__file__).parent.parent
SCRIPTS = Path(__file__).parent
PYTHON  = sys.executable
LOG_DIR = ROOT / "logs" / "pipeline"


def _run(cmd: list[str], check=True) -> subprocess.CompletedProcess:
    print(f"\n>>> {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    if check and result.returncode != 0:
        print(f"[pipeline] STEP FAILED (exit {result.returncode}) — aborting.", file=sys.stderr)
        sys.exit(result.returncode)
    return result


def _git_push(dry_run: bool):
    """Stage config/auto_config.json + data/trades.json and push to GitHub."""
    status = _run(["git", "status", "--short"], check=False)
    if not status.stdout.strip():
        print("[pipeline] Nothing to commit — working tree clean.")
        return

    _run(["git", "add",
          "config/auto_config.json",
          "scripts/analysis_output.json",
          "data/trades.json"])

    today = datetime.now().strftime("%Y-%m-%d")
    msg   = f"Auto update config based on daily P&L [{today}]"

    if dry_run:
        print(f"[dry-run] Would commit: {msg}")
        print("[dry-run] Would push origin master")
        return

    _run(["git", "commit", "-m", msg])
    _run(["git", "push", "origin", "master"])
    print("[pipeline] Pushed to GitHub → Vercel auto-deploy triggered.")


def _write_log(text: str):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"{today}.log"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def main():
    parser = argparse.ArgumentParser(description="Daily trading analysis pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Analyse only — skip git push")
    parser.add_argument("--days",    type=int, default=30, help="Look back N days for analysis")
    args = parser.parse_args()

    start = datetime.now()
    header = f"\n{'='*60}\n[pipeline] START {start.strftime('%Y-%m-%d %H:%M:%S')}\n{'='*60}"
    print(header)
    _write_log(header)

    # ── Step 1: Analyse trades ─────────────────────────────────────────────
    print("\n[pipeline] STEP 1 — Analyse trades")
    _run([PYTHON, str(SCRIPTS / "analyze_trades.py"), "--days", str(args.days)])

    # ── Step 2: Update config ──────────────────────────────────────────────
    print("\n[pipeline] STEP 2 — Update auto_config.json")
    cmd = [PYTHON, str(SCRIPTS / "update_config.py")]
    if args.dry_run:
        cmd.append("--dry-run")
    _run(cmd)

    # ── Step 3: Git push → Vercel auto-deploy ─────────────────────────────
    print("\n[pipeline] STEP 3 — Git push")
    _git_push(dry_run=args.dry_run)

    elapsed = (datetime.now() - start).total_seconds()
    footer = f"[pipeline] DONE in {elapsed:.1f}s"
    print(f"\n{footer}")
    _write_log(footer)


if __name__ == "__main__":
    main()

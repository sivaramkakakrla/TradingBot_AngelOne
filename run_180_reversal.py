"""Run 180-degree failed-breakout reversal strategy (paper/live/backtest)."""

from __future__ import annotations

import argparse
import json

import pandas as pd

from trading_bot.auth.login import authenticate
from trading_bot.market import start_feed
from trading_bot.reversal180 import Reversal180Config, Reversal180Engine, run_backtest
from trading_bot.candle_cache import get_candles


def main() -> None:
    parser = argparse.ArgumentParser(description="NIFTY 180-degree reversal strategy")
    parser.add_argument("--backtest", action="store_true", help="run backtest and exit")
    parser.add_argument("--paper", action="store_true", help="force paper mode")
    parser.add_argument("--live", action="store_true", help="force live order mode")
    args = parser.parse_args()

    cfg = Reversal180Config.from_env()
    if args.paper:
        cfg.paper_mode = True
        cfg.live_mode = False
    if args.live:
        cfg.paper_mode = False
        cfg.live_mode = True

    authenticate()

    if args.backtest:
        df, _, _ = get_candles("5m", 500)
        if df is None or df.empty:
            print("No 5m candles available for backtest")
            return
        result = run_backtest(df, cfg)
        print(json.dumps(result["summary"], indent=2))
        return

    # Live/paper runtime
    start_feed(authenticate(), interval=3.0)
    engine = Reversal180Engine(cfg)
    print("Starting 180 reversal engine")
    print(f"Mode: {'LIVE' if cfg.live_mode and not cfg.paper_mode else 'PAPER'}")
    print(f"ORB: {cfg.orb_start}-{cfg.orb_end} | Last entry: {cfg.last_entry_time} | Exit: {cfg.force_exit_time}")
    engine.run_live()


if __name__ == "__main__":
    main()

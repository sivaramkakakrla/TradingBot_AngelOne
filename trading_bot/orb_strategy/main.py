from __future__ import annotations

import argparse
import json

from trading_bot.auth.login import authenticate
from trading_bot.market import start_feed
from trading_bot.utils.logger import get_logger

from .config import ORBConfig
from .data_handler import ORBDataHandler
from .execution_engine import ORBExecutionEngine
from .strategy_orb import backtest_orb

log = get_logger(__name__)


def run() -> None:
    parser = argparse.ArgumentParser(description="ORB strategy bot")
    parser.add_argument("--backtest", action="store_true", help="run backtest on current cached 1m data")
    parser.add_argument("--paper", action="store_true", help="force paper mode")
    parser.add_argument("--live", action="store_true", help="force live mode")
    args = parser.parse_args()

    cfg = ORBConfig.from_env()
    if args.paper:
        cfg.paper_mode = True
        cfg.live_mode = False
    if args.live:
        cfg.paper_mode = False
        cfg.live_mode = True

    session = authenticate()

    if args.backtest:
        dh = ORBDataHandler(cfg)
        df = dh.get_1m_candles_today()
        if df is None or df.empty:
            print("No 1m candles available")
            return
        result = backtest_orb(df, cfg)
        print(json.dumps(result["summary"], indent=2))
        return

    start_feed(session, interval=max(2.0, float(cfg.poll_seconds)))
    engine = ORBExecutionEngine(cfg)
    print("ORB strategy engine started")
    print(f"Mode: {'LIVE' if cfg.live_mode and not cfg.paper_mode else 'PAPER'}")
    print(f"ORB {cfg.orb_start}-{cfg.orb_end} | Last entry {cfg.last_entry_time} | Exit {cfg.force_exit_time}")
    engine.run_forever()


if __name__ == "__main__":
    run()

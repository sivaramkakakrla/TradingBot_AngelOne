"""Convenience runner for ORB strategy bot.

Usage:
  python run_orb_strategy.py --paper
  python run_orb_strategy.py --live
  python run_orb_strategy.py --backtest
"""

from trading_bot.orb_strategy.main import run


if __name__ == "__main__":
    run()

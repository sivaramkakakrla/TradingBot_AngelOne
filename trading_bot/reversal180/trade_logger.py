from __future__ import annotations

import csv
from pathlib import Path

from trading_bot.utils.logger import get_logger
from .models import OpenTrade, ClosedTrade


log = get_logger(__name__)


class TradeLogger:
    def __init__(self, csv_path: str = "logs/reversal180_trades.csv"):
        self.csv_path = Path(csv_path)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.csv_path.exists():
            with self.csv_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "trade_id", "instrument", "option_type", "side",
                    "qty", "entry", "exit", "pnl", "reason", "note",
                ])

    def log_event(self, message: str) -> None:
        log.info(message)

    def log_open(self, t: OpenTrade) -> None:
        self.log_event(
            f"[{t.timestamp}] {t.side} executed: {t.symbol} @ Rs.{t.entry_price:.2f} "
            f"SL={t.sl_price:.2f} TG={t.target_price:.2f}"
        )

    def log_close(self, c: ClosedTrade) -> None:
        self.log_event(
            f"[{c.timestamp}] {c.symbol} exit @ Rs.{c.exit_price:.2f} | PnL={c.pnl:.2f} | {c.exit_reason}"
        )
        with self.csv_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                c.timestamp,
                c.trade_id,
                c.symbol,
                c.option_type,
                c.side,
                c.quantity,
                f"{c.entry_price:.2f}",
                f"{c.exit_price:.2f}",
                f"{c.pnl:.2f}",
                c.exit_reason,
                c.note,
            ])

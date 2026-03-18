"""
utils/logger.py — Centralized logging configuration for Project Candles.

Usage:
    from utils.logger import get_logger
    log = get_logger(__name__)
    log.info("Signal generated", extra={"symbol": "NIFTY"})
"""

import logging
import os
from datetime import datetime
from pathlib import Path

from trading_bot import config

_INITIALIZED = False


def _ensure_log_dir() -> Path:
    """Create the log directory if it doesn't exist."""
    log_dir = Path(config.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _init_root_logger() -> None:
    """Configure root logger once: console + daily rotating file."""
    global _INITIALIZED
    if _INITIALIZED:
        return

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.DEBUG)

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    root.addHandler(console)

    # File handler — skip on read-only filesystems (e.g. Vercel)
    try:
        log_dir = _ensure_log_dir()
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = log_dir / f"candles_{today}.log"
        file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
        file_handler.setFormatter(fmt)
        file_handler.setLevel(logging.DEBUG)
        root.addHandler(file_handler)
    except OSError:
        pass

    _INITIALIZED = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Initializes root logger on first call."""
    _init_root_logger()
    return logging.getLogger(name)

"""
utils/time_utils.py — Timezone‑aware time helpers for IST trading hours.

All times are in Asia/Kolkata unless stated otherwise.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from trading_bot import config

IST = ZoneInfo(config.TIMEZONE)


def now_ist() -> datetime:
    """Current datetime in IST."""
    return datetime.now(tz=IST)


def today_ist() -> datetime:
    """Today's date at midnight IST."""
    return now_ist().replace(hour=0, minute=0, second=0, microsecond=0)


def parse_time(t_str: str) -> time:
    """Parse 'HH:MM' string to time object."""
    h, m = map(int, t_str.split(":"))
    return time(h, m)


def is_within_trade_window() -> bool:
    """Check if current IST time falls within any configured trade window."""
    current = now_ist().time()
    for start_str, end_str in config.TRADE_WINDOWS:
        start = parse_time(start_str)
        end = parse_time(end_str)
        if start <= current <= end:
            return True
    return False


def is_past_force_exit() -> bool:
    """Check if current time is at or past forced exit time."""
    current = now_ist().time()
    force_exit = parse_time(config.FORCE_EXIT_TIME)
    return current >= force_exit


def is_near_event() -> bool:
    """Return True if we are within EVENT_BUFFER_MINUTES of a blocked event."""
    current = now_ist()
    buffer = timedelta(minutes=config.EVENT_BUFFER_MINUTES)

    for event in config.BLOCKED_EVENTS:
        date_str = event.get("date", "")
        time_str = event.get("time", "")
        if not date_str:
            continue
        try:
            if time_str:
                event_dt = datetime.strptime(
                    f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=IST)
            else:
                # All‑day block — treat as 09:00 to 15:30
                event_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
                    hour=9, minute=0, tzinfo=IST
                )
        except ValueError:
            continue

        if abs(current - event_dt) <= buffer:
            return True
    return False


def seconds_until(target_time_str: str) -> float:
    """Seconds from now until target_time_str ('HH:MM') today in IST."""
    current = now_ist()
    target = parse_time(target_time_str)
    target_dt = current.replace(
        hour=target.hour, minute=target.minute, second=0, microsecond=0
    )
    return (target_dt - current).total_seconds()


def market_open_today() -> bool:
    """Rough check: market open Mon-Fri. Does not check exchange holidays."""
    return now_ist().weekday() < 5  # 0=Mon … 4=Fri

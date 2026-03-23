from __future__ import annotations

import pandas as pd

from .models import ORBRange


def _hhmm(ts: str) -> str:
    s = str(ts).replace("T", " ")
    t = s.split(" ")[-1]
    return t[:5]


def calculate_orb(df_5m: pd.DataFrame, trade_date: str, orb_start: str, orb_end: str) -> ORBRange | None:
    """Calculate opening range high/low from 5-minute candles."""
    if df_5m is None or df_5m.empty:
        return None
    if "timestamp" not in df_5m.columns:
        return None

    mask = df_5m["timestamp"].astype(str).apply(lambda x: orb_start <= _hhmm(x) <= orb_end)
    orb_df = df_5m[mask]
    if orb_df.empty:
        return None

    return ORBRange(
        date=trade_date,
        high=float(orb_df["high"].max()),
        low=float(orb_df["low"].min()),
    )

"""Timezone / DST helpers. Nothing here assumes 96 slots per day."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

SLOT = timedelta(minutes=15)


def local_midnight(day: date, tz: ZoneInfo) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(day, time(0, 0)), tz=tz)


def local_at(day: date, t: time, tz: ZoneInfo) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(day, t), tz=tz)


def day_bounds(day: date, tz: ZoneInfo) -> tuple[pd.Timestamp, pd.Timestamp]:
    """[start of local day, start of next local day). Spans 23/24/25 hours."""
    return local_midnight(day, tz), local_midnight(day + timedelta(days=1), tz)


def expected_slots(day: date, tz: ZoneInfo) -> int:
    """Number of 15-minute slots in a local calendar day: 96, or 92/100 on DST days."""
    start, end = day_bounds(day, tz)
    return int((end - start) / SLOT)


def expected_index(start: pd.Timestamp, end_exclusive: pd.Timestamp) -> pd.DatetimeIndex:
    """Every 15-minute slot start in [start, end). Built in UTC so DST gaps/overlaps are exact."""
    idx = pd.date_range(start.tz_convert("UTC"), end_exclusive.tz_convert("UTC"), freq="15min", inclusive="left")
    return idx.tz_convert(start.tz)

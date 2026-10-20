"""Validation of quarter-hourly series. Missing data is reported, never filled in."""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pandas as pd

from .models import PriceSeries
from .timeutil import SLOT, day_bounds, expected_index


class ValidationError(Exception):
    pass


def check_day_complete(prices: PriceSeries, day: date, tz: ZoneInfo) -> list[pd.Timestamp]:
    """Return the list of missing slot starts for a local calendar day (empty = complete)."""
    start, end = day_bounds(day, tz)
    expected = expected_index(start, end)
    present = prices.series.index
    return [ts for ts in expected if ts not in present]


def day_is_complete(prices: PriceSeries, day: date, tz: ZoneInfo) -> bool:
    return not check_day_complete(prices, day, tz)


def check_range_complete(prices: PriceSeries, start: pd.Timestamp, end_exclusive: pd.Timestamp) -> list[pd.Timestamp]:
    expected = expected_index(start, end_exclusive)
    present = prices.series.index
    return [ts for ts in expected if ts not in present]


def assert_well_formed(prices: PriceSeries) -> None:
    """Structural checks independent of which days are expected."""
    s = prices.series
    if s.empty:
        raise ValidationError(f"{prices.source}: empty series")
    if s.isna().any():
        raise ValidationError(f"{prices.source}: NaN values present")
    if s.index.has_duplicates:
        raise ValidationError(f"{prices.source}: duplicate timestamps")
    if not s.index.is_monotonic_increasing:
        raise ValidationError(f"{prices.source}: timestamps not increasing")
    # Every slot start must be on a 15-minute boundary (checked in UTC to be DST-proof).
    utc = s.index.tz_convert("UTC")
    if ((utc.minute % 15) != 0).any() or (utc.second != 0).any():
        raise ValidationError(f"{prices.source}: timestamps not aligned to 15-minute slots")
    # Spacing must be a positive multiple of one slot (gaps are allowed, overlaps are not).
    diffs = pd.Series(utc[1:] - utc[:-1])
    if (diffs <= pd.Timedelta(0)).any():
        raise ValidationError(f"{prices.source}: non-positive spacing between slots")
    if ((diffs % SLOT) != pd.Timedelta(0)).any():
        raise ValidationError(f"{prices.source}: slot spacing is not a multiple of 15 minutes")


def contiguous_segments(prices: PriceSeries) -> list[pd.Series]:
    """Split a series into runs with no gaps, so a sliding window never spans a hole."""
    s = prices.series
    if s.empty:
        return []
    utc = s.index.tz_convert("UTC")
    breaks = [0]
    for i in range(1, len(s)):
        if utc[i] - utc[i - 1] != SLOT:
            breaks.append(i)
    breaks.append(len(s))
    return [s.iloc[a:b] for a, b in zip(breaks[:-1], breaks[1:])]

"""Core data types.

Actual (published market) prices and forecast prices are *different classes*, not a flag.
Functions that compute statistics or recommendations accept only ``ActualPrices``; the
plotting function is the only consumer of ``ForecastPrices``. A forecast can therefore
never leak into a recommendation by accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

import pandas as pd

UNIT = "EUR/MWh"


class PriceSeries:
    """A tz-aware, 15-minute price series. Base class — do not instantiate directly."""

    kind: str = "abstract"

    def __init__(self, series: pd.Series, source: str, fetched_at: datetime) -> None:
        if not isinstance(series.index, pd.DatetimeIndex) or series.index.tz is None:
            raise TypeError("PriceSeries requires a tz-aware DatetimeIndex")
        if fetched_at.tzinfo is None:
            raise TypeError("fetched_at must be tz-aware")
        self.series = series.astype(float).sort_index()
        self.source = source
        self.fetched_at = fetched_at

    def __len__(self) -> int:
        return len(self.series)

    @property
    def empty(self) -> bool:
        return self.series.empty

    @property
    def start(self) -> pd.Timestamp:
        return self.series.index[0]

    @property
    def end(self) -> pd.Timestamp:
        """Start of the last slot."""
        return self.series.index[-1]

    def slice(self, start: datetime | pd.Timestamp, end: datetime | pd.Timestamp) -> "PriceSeries":
        """Return the same kind of series restricted to [start, end] (slot starts, inclusive)."""
        return type(self)(self.series.loc[start:end], self.source, self.fetched_at)

    def mean(self) -> float:
        return float(self.series.mean())

    def __repr__(self) -> str:
        if self.empty:
            return f"<{type(self).__name__} empty source={self.source}>"
        return f"<{type(self).__name__} {self.start:%Y-%m-%d %H:%M}..{self.end:%Y-%m-%d %H:%M} n={len(self)} source={self.source}>"


class ActualPrices(PriceSeries):
    """Published day-ahead market prices. The only input accepted by stats and window search."""

    kind = "actual"


class ForecastPrices(PriceSeries):
    """Model predictions. Displayed as context only; never used for recommendations."""

    kind = "forecast"


@dataclass
class Recommendation:
    start: pd.Timestamp          # local, slot start
    end: pd.Timestamp            # local, end of last slot (exclusive)
    avg_price: float             # EUR/MWh
    duration_hours: int
    basis: Literal["actual"] = "actual"

    def __str__(self) -> str:
        from . import nl  # local import: nl is presentation-only, models must stay dependency-free
        return (f"{nl.day_short(self.start)} {nl.clock(self.start)}–{nl.clock_end(self.start, self.end)} "
                f"(gem. {nl.num(self.avg_price)} {UNIT})")


@dataclass
class DailyBundle:
    run_date: date
    actual: ActualPrices                       # horizon start → end of tomorrow (or shorter)
    history: ActualPrices                      # previous N full days
    forecast: ForecastPrices | None            # day+2 only, or None
    tomorrow_published: bool
    forecast_known_until: pd.Timestamp | None = None
    warnings: list[str] = field(default_factory=list)
    creg: object | None = None                 # creg.CregComparison once computed (None = not configured)

    @property
    def period_average(self) -> float:
        return self.actual.mean()

    @property
    def history_average(self) -> float:
        return self.history.mean()

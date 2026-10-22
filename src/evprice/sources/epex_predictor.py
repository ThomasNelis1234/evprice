"""Day-ahead price *predictions* from epexpredictor.batzill.com (b3nn0/EpexPredictor).

Verified 2026-10-20 (Phase 0):
  GET /prices?region=BE&unit=EUR_PER_MWH&timezone=Europe/Brussels&hours=<n>
  -> {"prices": [{"startsAt": "2026-10-22T00:00:00+02:00", "total": 123.4}, ...],
      "knownUntil": "2026-10-21T23:45:00+02:00"}
  * `unit` defaults to CT_PER_KWH — must be passed explicitly.
  * No per-item actual/forecast flag. The series includes already-published slots up to `knownUntil`.
    We keep ONLY slots strictly after knownUntil and inside the requested forecast day. Published
    prices always come from energy-charts, never from here.
  * Public endpoint, fair use, no availability guarantee.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from ..http import JsonFetcher, SourceError
from ..models import ForecastPrices
from ..timeutil import day_bounds

log = logging.getLogger(__name__)

SOURCE_NAME = "epexpredictor.batzill.com"
ATTRIBUTION = "Voorspelling: EpexPredictor (b3nn0), statistisch model — schattingen, geen marktprijzen"


def fetch(fetcher: JsonFetcher, base_url: str, region: str, tz: ZoneInfo, hours: int = 96) -> dict:
    params = {"region": region, "unit": "EUR_PER_MWH", "timezone": tz.key, "hours": hours}
    return fetcher(f"{base_url}/prices", params)


def parse(raw: dict, tz: ZoneInfo, forecast_day: date,
          fetched_at: datetime | None = None) -> tuple[ForecastPrices, pd.Timestamp | None]:
    """Extract the forecast for ``forecast_day`` only. Returns (forecast, known_until).

    Slots at or before ``knownUntil`` are discarded even if they fall on the forecast day:
    those are published prices and must come from the market-data source.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("prices"), list):
        raise SourceError("epexpredictor: response lacks a 'prices' list")

    known_until: pd.Timestamp | None = None
    if raw.get("knownUntil"):
        known_until = pd.Timestamp(raw["knownUntil"])
        if known_until.tzinfo is None:
            raise SourceError("epexpredictor: knownUntil has no timezone")
        known_until = known_until.tz_convert(tz)

    rows = []
    for item in raw["prices"]:
        try:
            ts = pd.Timestamp(item["startsAt"])
            val = float(item["total"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"epexpredictor: malformed item {item!r}") from exc
        if ts.tzinfo is None:
            raise SourceError("epexpredictor: naive timestamp in response")
        rows.append((ts.tz_convert(tz), val))

    if not rows:
        raise SourceError("epexpredictor: empty prices list")

    series = pd.Series({ts: v for ts, v in rows}, dtype="float64", name="forecast").sort_index()
    if series.index.has_duplicates:
        raise SourceError("epexpredictor: duplicate timestamps")

    start, end = day_bounds(forecast_day, tz)
    mask = (series.index >= start) & (series.index < end)
    if known_until is not None:
        mask &= series.index > known_until
    series = series[mask]

    return ForecastPrices(series, SOURCE_NAME, fetched_at or datetime.now(timezone.utc)), known_until


def load(fetcher: JsonFetcher, base_url: str, region: str, tz: ZoneInfo,
         forecast_day: date, hours: int = 96) -> tuple[ForecastPrices, pd.Timestamp | None, dict]:
    raw = fetch(fetcher, base_url, region, tz, hours)
    forecast, known_until = parse(raw, tz, forecast_day)
    return forecast, known_until, raw

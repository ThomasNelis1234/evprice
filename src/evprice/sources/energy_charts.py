"""Published day-ahead prices from api.energy-charts.info (Fraunhofer ISE).

Verified 2026-10-20 (Phase 0):
  GET /price?bzn=BE&start=<ISO with offset>&end=<ISO with offset>
  -> {"license_info": str, "unix_seconds": [int], "price": [float], "unit": "EUR / MWh", "deprecated": bool}
  * A date-only `end` drops the last day; always send full timestamps with offset. `end` is then inclusive.
  * 15-minute spacing, timestamps are slot starts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from ..http import JsonFetcher, SourceError
from ..models import ActualPrices

log = logging.getLogger(__name__)

SOURCE_NAME = "energy-charts.info"
ATTRIBUTION = "Day-ahead prices: Bundesnetzagentur | SMARD.de via energy-charts.info (CC BY 4.0)"
EXPECTED_UNIT = "EUR / MWh"


def fetch(fetcher: JsonFetcher, base_url: str, bzn: str, start: pd.Timestamp, end_inclusive: pd.Timestamp) -> dict:
    params = {
        "bzn": bzn,
        "start": start.isoformat(timespec="minutes"),
        "end": end_inclusive.isoformat(timespec="minutes"),
    }
    return fetcher(f"{base_url}/price", params)


def parse(raw: dict, tz: ZoneInfo, fetched_at: datetime | None = None) -> ActualPrices:
    """Turn a raw response into ActualPrices. Raises SourceError on anything malformed."""
    if not isinstance(raw, dict):
        raise SourceError("energy-charts: response is not a JSON object")
    unit = raw.get("unit")
    if unit != EXPECTED_UNIT:
        raise SourceError(f"energy-charts: unexpected unit {unit!r}, expected {EXPECTED_UNIT!r}")
    ts = raw.get("unix_seconds")
    prices = raw.get("price")
    if not isinstance(ts, list) or not isinstance(prices, list):
        raise SourceError("energy-charts: missing unix_seconds/price arrays")
    if len(ts) != len(prices):
        raise SourceError(f"energy-charts: array length mismatch ({len(ts)} vs {len(prices)})")
    if raw.get("deprecated"):
        log.warning("energy-charts flags this endpoint as deprecated")

    idx = pd.to_datetime(pd.Index(ts, dtype="int64"), unit="s", utc=True).tz_convert(tz)
    series = pd.Series(prices, index=idx, dtype="float64", name="price")
    if series.isna().any():
        raise SourceError("energy-charts: null price values present")
    if series.index.has_duplicates:
        raise SourceError("energy-charts: duplicate timestamps")

    return ActualPrices(series, SOURCE_NAME, fetched_at or datetime.now(timezone.utc))


def load(fetcher: JsonFetcher, base_url: str, bzn: str, tz: ZoneInfo,
         start: pd.Timestamp, end_inclusive: pd.Timestamp) -> tuple[ActualPrices, dict]:
    """Fetch + parse. Returns (prices, raw) so the raw payload can be saved as a fixture."""
    raw = fetch(fetcher, base_url, bzn, start, end_inclusive)
    return parse(raw, tz), raw

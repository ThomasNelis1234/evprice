"""Synthetic API payloads matching the schemas verified in Phase 0.

Used by the tests and by the dry-run. Prices follow a plausible daily shape
(cheap night, morning peak, solar dip, evening peak) so graphs look realistic.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from evprice.timeutil import day_bounds, expected_index


def price_shape(ts: pd.Timestamp, day_offset: float = 0.0) -> float:
    h = ts.hour + ts.minute / 60
    base = 95 + 15 * math.sin(day_offset)
    night = -45 * math.exp(-((h - 3.5) ** 2) / 8)
    morning = 70 * math.exp(-((h - 8) ** 2) / 1.6)
    solar = -55 * math.exp(-((h - 13.5) ** 2) / 5)
    evening = 95 * math.exp(-((h - 18.5) ** 2) / 2.2)
    return round(base + night + morning + solar + evening, 2)


def energy_charts_payload(days: list[date], tz: ZoneInfo, drop: set[pd.Timestamp] | None = None,
                          unit: str = "EUR / MWh") -> dict:
    drop = drop or set()
    ts: list[int] = []
    prices: list[float] = []
    for n, day in enumerate(days):
        start, end = day_bounds(day, tz)
        for slot in expected_index(start, end):
            if slot in drop:
                continue
            ts.append(int(slot.timestamp()))
            prices.append(price_shape(slot, n * 0.7))
    return {
        "license_info": "CC BY 4.0 (creativecommons.org/licenses/by/4.0) from Bundesnetzagentur | SMARD.de",
        "unix_seconds": ts,
        "price": prices,
        "unit": unit,
        "deprecated": False,
    }


def predictor_payload(first_slot: pd.Timestamp, hours: int, known_until: pd.Timestamp | None,
                      tz: ZoneInfo) -> dict:
    idx = expected_index(first_slot, first_slot + timedelta(hours=hours))
    items = []
    for i, slot in enumerate(idx):
        val = price_shape(slot, 2.1) + 6 * math.sin(i / 7)
        # mimic the observed precision difference between published and predicted values
        val = round(val, 3) if known_until is not None and slot <= known_until else round(val, 4)
        items.append({"startsAt": slot.isoformat(), "total": val})
    payload = {"prices": items}
    if known_until is not None:
        payload["knownUntil"] = known_until.isoformat()
    return payload

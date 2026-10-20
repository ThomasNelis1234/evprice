"""Cheapest consecutive charging window(s).

Accepts ``ActualPrices`` only — passing a ``ForecastPrices`` is a TypeError by design.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .models import ActualPrices, Recommendation
from .timeutil import SLOT
from .validate import contiguous_segments

SLOTS_PER_HOUR = 4


def cheapest_windows(prices: ActualPrices, duration_hours: int,
                     max_windows: int = 2, alternative_within: float = 0.10) -> list[Recommendation]:
    """Return up to ``max_windows`` non-overlapping windows of ``duration_hours`` consecutive slots.

    The first is the global cheapest. Further windows are added only if their average is within
    ``alternative_within`` (fraction) of the best — or, when the best average is <= 0, within an
    absolute margin of 5 EUR/MWh — so the user sees a genuine alternative, not noise.
    Windows never span a gap in the data. Returns [] if no segment is long enough.
    """
    if not isinstance(prices, ActualPrices):
        raise TypeError("cheapest_windows accepts ActualPrices only — forecasts are never a basis")
    if duration_hours < 1:
        raise ValueError("duration_hours must be >= 1")
    n = duration_hours * SLOTS_PER_HOUR

    candidates: list[tuple[float, pd.Timestamp, pd.Timestamp]] = []
    for seg in contiguous_segments(prices):
        if len(seg) < n:
            continue
        values = seg.to_numpy(dtype=float)
        # rolling mean of length n via cumulative sums
        cs = np.concatenate([[0.0], np.cumsum(values)])
        means = (cs[n:] - cs[:-n]) / n
        for i, m in enumerate(means):
            start = seg.index[i]
            end = seg.index[i + n - 1] + SLOT
            candidates.append((float(m), start, end))

    if not candidates:
        return []

    candidates.sort(key=lambda c: (c[0], c[1]))
    best = candidates[0]
    chosen: list[tuple[float, pd.Timestamp, pd.Timestamp]] = [best]
    tolerance = max(abs(best[0]) * alternative_within, 5.0)

    for cand in candidates[1:]:
        if len(chosen) >= max_windows:
            break
        if cand[0] - best[0] > tolerance:
            break
        if all(cand[2] <= c[1] or cand[1] >= c[2] for c in chosen):  # no overlap
            chosen.append(cand)

    # best first; any further entries are alternatives
    return [Recommendation(start=s, end=e, avg_price=m, duration_hours=duration_hours) for m, s, e in chosen]

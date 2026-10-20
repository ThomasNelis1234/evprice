import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from evprice.models import ActualPrices, ForecastPrices
from evprice.timeutil import day_bounds, expected_index
from evprice.windows import cheapest_windows

TZ = ZoneInfo("Europe/Brussels")
NOW = datetime.now(timezone.utc)


def series_for(day: date, values=None, hours=None):
    start, end = day_bounds(day, TZ)
    if hours is not None:
        end = start + pd.Timedelta(hours=hours)
    idx = expected_index(start, end)
    if values is None:
        values = [100.0] * len(idx)
    return pd.Series(values, index=idx[: len(values)], dtype=float)


class TestCheapestWindows(unittest.TestCase):
    def test_finds_cheapest_block(self):
        s = series_for(date(2026, 10, 20))
        s.iloc[8:16] = 10.0      # 02:00–04:00 cheap
        prices = ActualPrices(s, "t", NOW)
        recs = cheapest_windows(prices, duration_hours=2, max_windows=1)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r.start, s.index[8])
        self.assertEqual(r.end, s.index[16])
        self.assertAlmostEqual(r.avg_price, 10.0)
        self.assertEqual(r.basis, "actual")

    def test_alternative_window_only_when_close(self):
        s = series_for(date(2026, 10, 20))
        s.iloc[8:16] = 10.0
        s.iloc[60:68] = 10.5     # within 10 % -> second window
        s.iloc[80:88] = 40.0     # not within -> excluded
        recs = cheapest_windows(ActualPrices(s, "t", NOW), 2, max_windows=3)
        self.assertEqual(len(recs), 2)
        self.assertEqual([r.start for r in recs], [s.index[8], s.index[60]])  # best first
        s.iloc[8:16] = 10.6  # now the later block is the best -> it must come first
        recs = cheapest_windows(ActualPrices(s, "t", NOW), 2, max_windows=3)
        self.assertEqual([r.start for r in recs], [s.index[60], s.index[8]])

    def test_windows_do_not_overlap(self):
        s = series_for(date(2026, 10, 20))
        s.iloc[8:20] = 10.0      # 3 cheap hours; two 2h windows cannot both fit without overlap
        recs = cheapest_windows(ActualPrices(s, "t", NOW), 2, max_windows=2)
        self.assertEqual(len(recs), 1)

    def test_rejects_forecast(self):
        s = series_for(date(2026, 10, 20))
        with self.assertRaises(TypeError):
            cheapest_windows(ForecastPrices(s, "f", NOW), 2)

    def test_window_never_spans_a_gap(self):
        s = series_for(date(2026, 10, 20))
        s.iloc[8:16] = 10.0
        s = s.drop(s.index[12])  # hole in the middle of the cheap block
        recs = cheapest_windows(ActualPrices(s, "t", NOW), 2, max_windows=1)
        self.assertEqual(len(recs), 1)
        self.assertNotEqual(recs[0].start, s.index[8])  # cannot use the cheap block any more

    def test_too_short_returns_empty(self):
        s = series_for(date(2026, 10, 20), hours=3)
        self.assertEqual(cheapest_windows(ActualPrices(s, "t", NOW), 4), [])

    def test_dst_fall_back_window_crosses_repeated_hour(self):
        # 2026-10-25: 02:00-03:00 occurs twice. A 2h window over that stretch must be 8 slots long.
        day = date(2026, 10, 25)
        s = series_for(day)
        self.assertEqual(len(s), 100)
        s.iloc[8:16] = 5.0  # local 02:00 CEST .. 02:45 CET (the repeated hour) is cheapest
        recs = cheapest_windows(ActualPrices(s, "t", NOW), 2, max_windows=1)
        r = recs[0]
        self.assertEqual((r.end - r.start), pd.Timedelta(hours=2))
        self.assertAlmostEqual(r.avg_price, 5.0)

    def test_negative_prices(self):
        s = series_for(date(2026, 10, 20))
        s.iloc[48:56] = -20.0
        recs = cheapest_windows(ActualPrices(s, "t", NOW), 2, max_windows=1)
        self.assertAlmostEqual(recs[0].avg_price, -20.0)


if __name__ == "__main__":
    unittest.main()

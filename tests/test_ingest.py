import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from evprice.http import SourceError
from evprice.models import ActualPrices, ForecastPrices
from evprice.sources import energy_charts, epex_predictor
from evprice.timeutil import expected_slots, local_midnight
from evprice.validate import ValidationError, assert_well_formed, check_day_complete, contiguous_segments

from .synth import energy_charts_payload, predictor_payload

TZ = ZoneInfo("Europe/Brussels")
NORMAL = date(2026, 10, 20)
DST_FALL = date(2026, 10, 25)    # 25-hour day
DST_SPRING = date(2026, 3, 29)   # 23-hour day


class TestTimeUtil(unittest.TestCase):
    def test_expected_slots(self):
        self.assertEqual(expected_slots(NORMAL, TZ), 96)
        self.assertEqual(expected_slots(DST_FALL, TZ), 100)
        self.assertEqual(expected_slots(DST_SPRING, TZ), 92)


class TestEnergyCharts(unittest.TestCase):
    def test_parse_normal_day(self):
        raw = energy_charts_payload([NORMAL], TZ)
        prices = energy_charts.parse(raw, TZ, datetime.now(timezone.utc))
        self.assertIsInstance(prices, ActualPrices)
        self.assertEqual(len(prices), 96)
        self.assertEqual(prices.start, local_midnight(NORMAL, TZ))
        assert_well_formed(prices)
        self.assertEqual(check_day_complete(prices, NORMAL, TZ), [])

    def test_parse_dst_days(self):
        for day, n in ((DST_FALL, 100), (DST_SPRING, 92)):
            with self.subTest(day=day):
                prices = energy_charts.parse(energy_charts_payload([day], TZ), TZ)
                self.assertEqual(len(prices), n)
                assert_well_formed(prices)
                self.assertEqual(check_day_complete(prices, day, TZ), [])

    def test_missing_slot_is_reported_not_filled(self):
        missing = local_midnight(NORMAL, TZ) + pd.Timedelta(hours=5)
        raw = energy_charts_payload([NORMAL], TZ, drop={missing})
        prices = energy_charts.parse(raw, TZ)
        self.assertEqual(len(prices), 95)
        self.assertEqual(check_day_complete(prices, NORMAL, TZ), [missing])
        self.assertEqual(len(contiguous_segments(prices)), 2)

    def test_wrong_unit_rejected(self):
        raw = energy_charts_payload([NORMAL], TZ, unit="ct/kWh")
        with self.assertRaises(SourceError):
            energy_charts.parse(raw, TZ)

    def test_malformed_rejected(self):
        with self.assertRaises(SourceError):
            energy_charts.parse({"unit": "EUR / MWh", "unix_seconds": [1, 2], "price": [1.0]}, TZ)
        raw = energy_charts_payload([NORMAL], TZ)
        raw["price"][3] = None
        with self.assertRaises(SourceError):
            energy_charts.parse(raw, TZ)

    def test_fetch_uses_full_iso_timestamps(self):
        seen = {}

        def fetcher(url, params):
            seen.update(params)
            return energy_charts_payload([NORMAL], TZ)

        start = local_midnight(NORMAL, TZ)
        energy_charts.fetch(fetcher, "https://x", "BE", start, start + pd.Timedelta(hours=23, minutes=45))
        self.assertEqual(seen["start"], "2026-10-20T00:00+02:00")
        self.assertEqual(seen["end"], "2026-10-20T23:45+02:00")


class TestPredictor(unittest.TestCase):
    def test_only_future_slots_of_forecast_day_kept(self):
        today = date(2026, 10, 20)
        tomorrow = date(2026, 10, 21)
        day2 = date(2026, 10, 22)
        known_until = local_midnight(tomorrow, TZ) + pd.Timedelta(hours=23, minutes=45)
        first = local_midnight(today, TZ) + pd.Timedelta(hours=21)
        raw = predictor_payload(first, 72, known_until, TZ)
        fc, ku = epex_predictor.parse(raw, TZ, day2)
        self.assertIsInstance(fc, ForecastPrices)
        self.assertEqual(ku, known_until)
        self.assertEqual(len(fc), 96)
        self.assertEqual(fc.start, local_midnight(day2, TZ))
        self.assertTrue((fc.series.index > known_until).all())

    def test_published_slots_on_forecast_day_are_discarded(self):
        # Tomorrow already published through 06:00 of day+2 (hypothetical) -> those slots excluded.
        day2 = date(2026, 10, 22)
        known_until = local_midnight(day2, TZ) + pd.Timedelta(hours=6)
        raw = predictor_payload(local_midnight(date(2026, 10, 21), TZ), 72, known_until, TZ)
        fc, _ = epex_predictor.parse(raw, TZ, day2)
        self.assertEqual(fc.start, known_until + pd.Timedelta(minutes=15))

    def test_forecast_on_dst_day(self):
        raw = predictor_payload(local_midnight(date(2026, 10, 24), TZ), 72,
                                local_midnight(DST_FALL, TZ) - pd.Timedelta(minutes=15), TZ)
        fc, _ = epex_predictor.parse(raw, TZ, DST_FALL)
        self.assertEqual(len(fc), 100)
        assert_well_formed(fc)

    def test_unit_is_forced(self):
        seen = {}

        def fetcher(url, params):
            seen.update(params)
            return predictor_payload(local_midnight(NORMAL, TZ), 24, None, TZ)

        epex_predictor.fetch(fetcher, "https://x", "BE", TZ)
        self.assertEqual(seen["unit"], "EUR_PER_MWH")
        self.assertEqual(seen["region"], "BE")
        self.assertEqual(seen["timezone"], "Europe/Brussels")

    def test_malformed(self):
        with self.assertRaises(SourceError):
            epex_predictor.parse({"prices": [{"startsAt": "2026-10-20T00:00:00", "total": 1}]}, TZ, NORMAL)
        with self.assertRaises(SourceError):
            epex_predictor.parse({"nope": []}, TZ, NORMAL)


class TestTypeSeparation(unittest.TestCase):
    def test_slice_preserves_kind(self):
        prices = energy_charts.parse(energy_charts_payload([NORMAL], TZ), TZ)
        sub = prices.slice(prices.start, prices.start + pd.Timedelta(hours=2))
        self.assertIsInstance(sub, ActualPrices)
        self.assertEqual(len(sub), 9)

    def test_well_formed_rejects_overlap(self):
        prices = energy_charts.parse(energy_charts_payload([NORMAL], TZ), TZ)
        bad = prices.series.copy()
        bad.index = bad.index.tz_convert("UTC").shift(1, freq="7min").tz_convert(TZ)
        with self.assertRaises(ValidationError):
            assert_well_formed(ActualPrices(bad, "x", datetime.now(timezone.utc)))


if __name__ == "__main__":
    unittest.main()

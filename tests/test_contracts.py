"""Other-contract comparison: tariff periods, EPEXDAM estimate, break-even arithmetic."""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evprice import contracts as cm  # noqa: E402
from evprice.config import load_config  # noqa: E402
from evprice.creg import CostStack  # noqa: E402
from evprice.models import ActualPrices, Recommendation  # noqa: E402
from evprice.timeutil import expected_index, local_midnight  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Europe/Brussels")
ENV = {"RECIPIENTS": "t@example.com"}


def series(start: date, days: int, value) -> ActualPrices:
    idx = expected_index(local_midnight(start, TZ), local_midnight(start + pd.Timedelta(days=days), TZ))
    vals = [value(ts) if callable(value) else value for ts in idx]
    return ActualPrices(pd.Series(vals, index=idx, dtype=float), "test", datetime.now(timezone.utc))


class TestPeriods(unittest.TestCase):
    def setUp(self):
        cfg = load_config(ROOT / "config.yaml", env=ENV).contracts
        self.basic = next(c for c in cfg.items if c.name == "Basic Online")
        self.empower = next(c for c in cfg.items if c.name == "Empower Flextime")

    def ts(self, s: str) -> pd.Timestamp:
        return pd.Timestamp(s, tz=TZ)

    def test_flat_is_always_one_period(self):
        for s in ("2026-11-09 03:00", "2026-11-09 18:00", "2026-11-08 12:00"):
            self.assertEqual(cm.period_at(self.basic, self.ts(s)), "enkel")

    def test_empower_weekday_schedule_from_the_sheet(self):
        monday = "2026-11-09 "
        self.assertEqual(cm.period_at(self.empower, self.ts(monday + "03:00")), "superdal")   # 1u–7u
        self.assertEqual(cm.period_at(self.empower, self.ts(monday + "07:00")), "piek")       # 7u–11u
        self.assertEqual(cm.period_at(self.empower, self.ts(monday + "10:59")), "piek")
        self.assertEqual(cm.period_at(self.empower, self.ts(monday + "11:00")), "dal")        # 11u–17u
        self.assertEqual(cm.period_at(self.empower, self.ts(monday + "17:00")), "piek")       # 17u–22u
        self.assertEqual(cm.period_at(self.empower, self.ts(monday + "22:00")), "dal")        # 22u–1u
        self.assertEqual(cm.period_at(self.empower, self.ts(monday + "00:30")), "dal")
        self.assertEqual(cm.period_at(self.empower, self.ts(monday + "01:00")), "superdal")

    def test_empower_weekend_schedule_from_the_sheet(self):
        sunday = "2026-11-08 "
        self.assertEqual(cm.period_at(self.empower, self.ts(sunday + "03:00")), "superdal")   # 1u–7u
        self.assertEqual(cm.period_at(self.empower, self.ts(sunday + "08:00")), "dal")        # 7u–11u
        self.assertEqual(cm.period_at(self.empower, self.ts(sunday + "13:00")), "superdal")   # 11u–17u
        self.assertEqual(cm.period_at(self.empower, self.ts(sunday + "19:00")), "dal")        # 17u–1u, no peak
        self.assertEqual(cm.period_at(self.empower, self.ts(sunday + "23:30")), "dal")


class TestEpexdam(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml", env=ENV).contracts

    def test_estimate_uses_only_the_running_month(self):
        # October at 100, November at 200 (3 days) → November estimate is 200, not a blend
        hist = series(date(2026, 10, 29), 3, 100.0)
        nov = series(date(2026, 11, 1), 3, 200.0)
        hist = ActualPrices(pd.concat([hist.series, nov.series]), "test", hist.fetched_at)
        actual = series(date(2026, 11, 4), 1, 260.0)
        e = cm.estimate_epexdam(self.cfg, hist, actual, date(2026, 11, 4))
        self.assertTrue(e.estimated)
        self.assertEqual(e.days_used, 4)
        self.assertAlmostEqual(e.value_eur_mwh, (3 * 200 + 260) / 4)

    def test_falls_back_to_reference_early_in_the_month(self):
        hist = series(date(2026, 10, 25), 7, 100.0)
        actual = series(date(2026, 11, 1), 2, 300.0)          # 2 days < min_days 3
        e = cm.estimate_epexdam(self.cfg, hist, actual, date(2026, 11, 1))
        self.assertFalse(e.estimated)
        self.assertEqual(e.value_eur_mwh, self.cfg.epexdam_reference_eur_mwh)
        self.assertIn("tariefkaart", e.describe())


class TestCompare(unittest.TestCase):
    def setUp(self):
        self.full = load_config(ROOT / "config.yaml", env=ENV)
        self.cfg, self.stack_cfg = self.full.contracts, self.full.cost_stack

    def test_break_even_matches_hand_calculation(self):
        epexdam = 174.40                                           # the sheet's reference month
        basic = next(c for c in self.cfg.items if c.name == "Basic Online")
        stack = CostStack(self.stack_cfg)
        idx = expected_index(local_midnight(date(2026, 11, 9), TZ), local_midnight(date(2026, 11, 10), TZ))
        be = cm.break_even_series(basic, stack, idx, epexdam)
        energy = 1.4600 + 0.1167 * epexdam                         # c€/kWh excl. btw, from the sheet
        self.assertAlmostEqual(float(be.iloc[0]), (energy - 0.7134) / 0.1039, places=6)
        self.assertEqual(be.nunique(), 1)
        # at the break-even price the dynamic energy formula gives the same c€/kWh
        self.assertAlmostEqual(0.7134 + 0.1039 * float(be.iloc[0]), energy, places=6)

    def test_sheet_prices_reproduced(self):
        # Tariff sheet shows Basic Online afname 23,121 c€/kWh incl. btw at EPEXDAM = 174,40
        basic = next(c for c in self.cfg.items if c.name == "Basic Online")
        self.assertAlmostEqual(basic.periods["enkel"](174.40) * 1.06, 23.121, places=2)
        # and Empower Flextime piek 29,082 / dal 21,706 / superdal 16,526
        emp = next(c for c in self.cfg.items if c.name == "Empower Flextime")
        self.assertAlmostEqual(emp.periods["piek"](174.40) * 1.06, 29.082, places=2)
        self.assertAlmostEqual(emp.periods["dal"](174.40) * 1.06, 21.706, places=2)
        self.assertAlmostEqual(emp.periods["superdal"](174.40) * 1.06, 16.526, places=2)

    def test_compare_costs_and_margins(self):
        run_date = date(2026, 11, 9)                                # Monday
        hist = series(date(2026, 11, 1), 8, 200.0)
        actual = series(run_date, 2, lambda ts: 100.0 if 1 <= ts.hour < 5 else 300.0)
        rec = Recommendation(start=pd.Timestamp("2026-11-09 01:00", tz=TZ), end=pd.Timestamp("2026-11-09 05:00", tz=TZ),
                             avg_price=100.0, duration_hours=4)
        cc = cm.compare(self.cfg, self.stack_cfg, actual, hist, [rec], run_date, 4,
                        horizon_end=local_midnight(date(2026, 11, 12), TZ))
        stack = CostStack(self.stack_cfg)
        self.assertAlmostEqual(cc.dynamic_best_c_per_kwh, stack.all_in(100.0))
        self.assertEqual(cc.charge_kwh, 4 * 4.6)
        emp = next(i for i in cc.items if i.contract.name == "Empower Flextime")
        self.assertEqual(emp.best_periods, ("superdal",))           # 1u–5u on a weekday
        expected = emp.contract.periods["superdal"](cc.epexdam.value_eur_mwh) * 1.06 + stack.fixed_incl_vat
        self.assertAlmostEqual(emp.best_all_in_c_per_kwh, expected)
        self.assertAlmostEqual(cc.margin_best(emp), expected - stack.all_in(100.0))
        # break-even profile covers the chart horizon (3 local days), costs only the published slots
        self.assertEqual(len(emp.break_even), 3 * 96)
        self.assertIsNone(emp.flat_break_even)
        basic = next(i for i in cc.items if i.contract.name == "Basic Online")
        self.assertIsNotNone(basic.flat_break_even)

    def test_no_recommendation_still_compares_the_period(self):
        run_date = date(2026, 11, 9)
        hist = series(date(2026, 11, 1), 8, 200.0)
        actual = series(run_date, 1, 250.0)
        cc = cm.compare(self.cfg, self.stack_cfg, actual, hist, [], run_date, 4)
        self.assertIsNone(cc.dynamic_best_c_per_kwh)
        for item in cc.items:
            self.assertIsNone(item.best_all_in_c_per_kwh)
            self.assertIsNone(cc.margin_best(item))
            self.assertGreater(item.period_all_in_c_per_kwh, 0)


if __name__ == "__main__":
    unittest.main()

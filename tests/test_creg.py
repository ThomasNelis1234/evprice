"""CREG comparison: cost model, break-even inversion, CSV reader, warnings."""

import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from evprice.config import ConfigError, CostStackConfig, CregConfig, load_config
from evprice.creg import (CostStack, check_against_csv, compare, implied_quarter_tariff, online_warnings,
                          parse_creg_csv, parse_monthly_csv)
from evprice.http import SourceError
from evprice.models import Recommendation

ROOT = Path(__file__).resolve().parents[1]

STACK = CostStackConfig(energy_a=0.7134, energy_b=0.1039, gsc_wkk_c_per_kwh=1.582, green_option_c_per_kwh=0.0,
                        federal_excise_c_per_kwh=4.4732, flemish_energy_fund_c_per_kwh=0.0,
                        distribution_operator="FLUVIUS ANTWERPEN", distribution_c_per_kwh=5.35329, vat=0.06)
CREG = CregConfig(region="Vlaanderen", tariff_c_per_kwh=32.25, valid_from=date(2026, 10, 1), valid_to=date(2026, 12, 31),
                  based_on="mei–juli 2026")


class TestCostStack(unittest.TestCase):
    def test_all_in_matches_the_tariff_sheet_example(self):
        # ENGIE sheet: estimated eSpot_15 173.23 EUR/MWh -> 19.835 c€/kWh energy incl. btw
        s = CostStack(STACK)
        self.assertAlmostEqual(s.energy_incl_vat(173.23), 19.835, places=2)
        self.assertAlmostEqual(s.fixed_incl_vat, 11.40849, places=4)

    def test_break_even_inverts_all_in(self):
        s = CostStack(STACK)
        be = s.break_even(32.25)
        self.assertAlmostEqual(s.all_in(be), 32.25, places=9)
        self.assertAlmostEqual(be, 182.4, delta=0.1)

    def test_green_option_and_capacity_surcharge_lower_the_break_even(self):
        s0 = CostStack(STACK).break_even(32.25)
        s1 = CostStack(CostStackConfig(**{**STACK.__dict__, "green_option_c_per_kwh": 1.06})).break_even(32.25)
        s2 = CostStack(CostStackConfig(**{**STACK.__dict__, "capacity_surcharge_c_per_kwh": 6.9})).break_even(32.25)
        self.assertLess(s1, s0)
        self.assertLess(s2, s1)
        self.assertAlmostEqual(s0 - s1, 1.06 / 1.06 / 0.1039, places=6)   # 1 c excl. btw ≈ 9.6 EUR/MWh

    def test_negative_spot_prices_reduce_but_do_not_remove_the_fixed_part(self):
        s = CostStack(STACK)
        self.assertAlmostEqual(s.all_in(-50), s.fixed_incl_vat + (0.7134 - 0.1039 * 50) * 1.06, places=9)
        self.assertGreater(s.all_in(-50), 0)


class TestCompare(unittest.TestCase):
    def rec(self, avg):
        tz = "Europe/Brussels"
        return Recommendation(start=pd.Timestamp("2026-10-23 11:45", tz=tz), end=pd.Timestamp("2026-10-23 15:45", tz=tz),
                              avg_price=avg, duration_hours=4)

    def test_windows_and_signs(self):
        c = compare(CREG, STACK, [self.rec(89.3), self.rec(200.0)], 147.8, date(2026, 10, 22))
        self.assertEqual([w.label for w in c.windows], ["beste venster", "alternatief", "gemiddelde getoonde periode"])
        self.assertGreater(c.windows[0].margin_c_per_kwh, 10)      # 89 EUR/MWh is well under break-even
        self.assertLess(c.windows[1].margin_c_per_kwh, 0)          # 200 EUR/MWh is above it
        self.assertAlmostEqual(c.windows[2].all_in_c_per_kwh, 28.44, delta=0.01)
        self.assertEqual(c.quarter_label(), "Q4 2026")
        self.assertEqual(c.warnings, [])

    def test_expired_config_warns(self):
        c = compare(CREG, STACK, [self.rec(89.3)], 147.8, date(2027, 1, 5))
        self.assertEqual(len(c.warnings), 1)
        self.assertIn("niet meer geldig", c.warnings[0])

    def test_no_recommendation_still_gives_period_row(self):
        c = compare(CREG, STACK, [], 147.8, date(2026, 10, 22))
        self.assertIsNone(c.best)
        self.assertEqual(len(c.windows), 1)


WIDE = "Date;Vlaanderen;Brussel;Wallonië\n2026-05;31,80;36,50;37,10\n2026-06;32,40;37,00;38,00\n2026-07;32,55;37,10;38,30\n"
EURO = "﻿Datum;VL;BRU;WAL\n01/05/2026;0,3180;0,3650;0,3710\n01/06/2026;0,3240;0,37;0,38\n01/07/2026;0,3255;0,371;0,383\n"


class TestCsv(unittest.TestCase):
    def test_layouts(self):
        for text in (WIDE, EURO):
            monthly = parse_monthly_csv(text, "Vlaanderen")
            self.assertEqual(sorted(monthly), [(2026, 5), (2026, 6), (2026, 7)])
            implied, names = implied_quarter_tariff(monthly, date(2026, 11, 2))
            self.assertAlmostEqual(implied, 32.25, places=6)
            self.assertEqual(names, ["mei 2026", "juni 2026", "juli 2026"])

    def test_live_creg_file(self):
        # Real file captured by the runner on 2026-11-02 (midday, data up to July 2026):
        # Year;Month;<region> End-user Price;<region> Average 3 months;...
        # Frozen copy outside tests/fixtures/live/, which the daily run overwrites with fresh downloads.
        text = (ROOT / "tests/fixtures/creg/creg_tariff_ev_until_2026-07.csv").read_text(encoding="utf-8")
        for region, monthly_jul, q4 in (("Vlaanderen", 33.65, 32.25), ("Brussel", 38.22, 36.88), ("Wallonië", 39.10, 37.79)):
            monthly, quarterly = parse_creg_csv(text, region)
            self.assertAlmostEqual(monthly[(2026, 7)], monthly_jul)
            self.assertAlmostEqual(quarterly[(2026, 7)], q4)                       # CREG's own quarterly figure
            self.assertAlmostEqual(implied_quarter_tariff(monthly, date(2026, 11, 2), quarterly)[0], q4)
            self.assertAlmostEqual(sum(monthly[(2026, m)] for m in (5, 6, 7)) / 3, q4, places=2)   # and our own mean agrees
        self.assertIsNone(implied_quarter_tariff(monthly, date(2027, 1, 15), quarterly)[0])      # Q1 2027 needs Aug–Oct
        chk = check_against_csv(CREG, lambda url: text, date(2026, 11, 2))
        self.assertTrue(chk.ok)
        self.assertEqual(online_warnings(CREG, chk), [])

    def test_live_creg_file_with_q1_2027(self):
        # Same CSV re-downloaded on 2026-11-02 at 14:00 UTC, after CREG added August–October 2026.
        text = (ROOT / "tests/fixtures/creg/creg_tariff_ev_until_2026-10.csv").read_text(encoding="utf-8")
        for region, q4, q1 in (("Vlaanderen", 32.25, 36.16), ("Brussel", 36.88, 41.36), ("Wallonië", 37.79, 41.88)):
            monthly, quarterly = parse_creg_csv(text, region)
            self.assertAlmostEqual(implied_quarter_tariff(monthly, date(2026, 11, 2), quarterly)[0], q4)
            self.assertAlmostEqual(implied_quarter_tariff(monthly, date(2027, 1, 15), quarterly)[0], q1)
            self.assertAlmostEqual(sum(monthly[(2026, m)] for m in (8, 9, 10)) / 3, q1, places=2)

    def test_quarter_rule_crosses_year(self):
        monthly = {(2025, 8): 30.0, (2025, 9): 31.0, (2025, 10): 32.0, (2025, 11): 1.0, (2025, 12): 2.0, (2026, 1): 3.0}
        self.assertAlmostEqual(implied_quarter_tariff(monthly, date(2026, 2, 1))[0], 31.0)
        self.assertAlmostEqual(implied_quarter_tariff(monthly, date(2026, 5, 1))[0], 2.0)
        self.assertIsNone(implied_quarter_tariff(monthly, date(2026, 8, 1))[0])

    def test_garbage_raises_source_error(self):
        with self.assertRaises(SourceError):
            parse_monthly_csv("<html>not a csv</html>", "Vlaanderen")
        with self.assertRaises(SourceError):
            parse_monthly_csv(WIDE, "Mars")

    def test_check_degrades_gracefully(self):
        def down(url):
            raise SourceError("timeout")
        chk = check_against_csv(CREG, down, date(2026, 11, 2))
        self.assertFalse(chk.ok)
        self.assertEqual(online_warnings(CREG, chk), [])
        saved = []
        chk = check_against_csv(CREG, lambda url: WIDE, date(2026, 11, 2), save_raw=saved.append)
        self.assertTrue(chk.ok)
        self.assertEqual(saved, [WIDE])
        self.assertEqual(online_warnings(CREG, chk), [])                       # 32.25 matches
        other = CregConfig(**{**CREG.__dict__, "tariff_c_per_kwh": 31.00})
        chk = check_against_csv(other, lambda url: WIDE, date(2026, 11, 2))
        self.assertEqual(len(online_warnings(other, chk)), 1)
        self.assertIn("32.25", online_warnings(other, chk)[0])


class TestConfig(unittest.TestCase):
    def test_repo_config_parses(self):
        cfg = load_config(ROOT / "config.yaml", env={})
        self.assertIsNotNone(cfg.creg)
        self.assertIsNotNone(cfg.cost_stack)
        self.assertEqual(cfg.cost_stack.distribution_operator, "FLUVIUS LIMBURG")
        self.assertAlmostEqual(cfg.cost_stack.distribution_c_per_kwh, 5.75257)
        # capacity share derived from the sheet: 51.9898 €/kW/j × 2.5 kW ÷ 4000 kWh = 3.249 c€/kWh
        self.assertAlmostEqual(cfg.cost_stack.capacity_surcharge_c_per_kwh, 51.9898 * 2.5 / 4000 * 100, places=9)
        self.assertEqual(cfg.cost_stack.charger_power_kw, 4.6)
        self.assertAlmostEqual(CostStack(cfg.cost_stack).break_even(32.25), 149.2, delta=0.1)

    def test_capacity_overrides_and_zero_peak(self):
        import tempfile
        import yaml
        raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        raw["cost_stack"]["capacity"]["extra_peak_kw"] = 0
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(raw, f)
        self.assertEqual(load_config(f.name, env={}).cost_stack.capacity_surcharge_c_per_kwh, 0.0)
        raw["cost_stack"]["capacity_surcharge_c_per_kwh"] = 1.5     # explicit value wins over the formula
        raw["cost_stack"]["capacity"]["extra_peak_kw"] = 2.5
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(raw, f)
        self.assertEqual(load_config(f.name, env={}).cost_stack.capacity_surcharge_c_per_kwh, 1.5)

    def test_unknown_operator_is_rejected(self):
        import tempfile
        import yaml
        raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        raw["cost_stack"]["distribution_operator"] = "FLUVIUS NERGENS"
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(raw, f)
        with self.assertRaises(ConfigError):
            load_config(f.name, env={})


if __name__ == "__main__":
    unittest.main()

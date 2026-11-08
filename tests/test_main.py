"""End-to-end orchestration tests with fake fetchers and a fake sender."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from evprice.config import load_config  # noqa: E402
from evprice.http import SourceError  # noqa: E402
from evprice.main import Outcome, run  # noqa: E402
from evprice.state import State  # noqa: E402
from demo import make_fake_fetcher, make_fake_text_fetcher  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = load_config(ROOT / "config.yaml", env={"GMAIL_USER": "u@example.com", "GMAIL_APP_PASSWORD": "x",
                                                        "RECIPIENTS": "one@example.com, two@example.com"})
        self.cfg = cfg.__class__(**{**cfg.__dict__, "state_file": Path(self.tmp.name) / "runs.json",
                                   "output_dir": Path(self.tmp.name) / "out"})
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def sender(self, cfg, msg):
        self.sent.append(msg)

    def at(self, s: str) -> datetime:
        return datetime.fromisoformat(s).replace(tzinfo=self.cfg.tz)

    def go(self, now: str, creg_ok=True, **fake):
        n = self.at(now)
        return run(self.cfg, make_fake_fetcher(self.cfg, n, **fake), n, sender=self.sender,
                   text_fetcher=make_fake_text_fetcher(creg_ok=creg_ok))


class TestRun(Harness):
    def test_sends_once_and_never_twice(self):
        self.assertEqual(self.go("2026-10-20T14:20"), Outcome.SENT)
        self.assertEqual(self.go("2026-10-20T14:50"), Outcome.SKIPPED)
        self.assertEqual(self.go("2026-10-20T16:50"), Outcome.SKIPPED)
        self.assertEqual(len(self.sent), 1)
        state = json.loads(self.cfg.state_file.read_text())
        self.assertEqual([r["status"] for r in state["runs"]], ["sent", "skipped", "skipped"])
        # next day is a fresh slate
        self.assertEqual(self.go("2026-10-21T14:20"), Outcome.SENT)
        self.assertEqual(len(self.sent), 2)

    def test_before_earliest_time_does_nothing(self):
        self.assertEqual(self.go("2026-10-20T11:30"), Outcome.SKIPPED)
        self.assertEqual(self.sent, [])

    def test_waits_for_tomorrow_until_cutoff_then_sends_with_warning(self):
        self.assertEqual(self.go("2026-10-20T14:20", tomorrow_ok=False), Outcome.SKIPPED)
        self.assertEqual(self.go("2026-10-20T17:50", tomorrow_ok=False), Outcome.SKIPPED)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.go("2026-10-20T18:05", tomorrow_ok=False), Outcome.SENT)
        msg = self.sent[0]
        self.assertIn("gegevens onvolledig", msg["Subject"])
        self.assertIn("nog niet gepubliceerd", msg.get_body(("plain",)).get_content())

    def test_forecast_outage_still_sends(self):
        self.assertEqual(self.go("2026-10-20T14:20", forecast_ok=False), Outcome.SENT)
        body = self.sent[0].get_body(("plain",)).get_content()
        self.assertIn("niet beschikbaar", body)
        self.assertIn("Beste:", body)

    def test_actual_source_outage_fails_without_sending(self):
        def broken(url, params):
            raise SourceError("boom")
        n = self.at("2026-10-20T14:20")
        self.assertEqual(run(self.cfg, broken, n, sender=self.sender), Outcome.FAILED)
        self.assertEqual(self.sent, [])
        self.assertFalse(State(self.cfg.state_file).already_sent(n.date()))

    def test_send_failure_is_retried_next_tick(self):
        def bad_sender(cfg, msg):
            raise RuntimeError("smtp down")
        n = self.at("2026-10-20T14:20")
        f = make_fake_fetcher(self.cfg, n)
        self.assertEqual(run(self.cfg, f, n, sender=bad_sender), Outcome.FAILED)
        self.assertEqual(run(self.cfg, f, n + timedelta(minutes=30), sender=self.sender), Outcome.SENT)
        self.assertEqual(len(self.sent), 1)

    def test_email_structure(self):
        self.go("2026-10-20T14:20")
        msg = self.sent[0]
        self.assertEqual(msg["To"], ", ".join(self.cfg.recipients))
        self.assertEqual(len(self.cfg.recipients), 2)
        parts = [p.get_content_type() for p in msg.walk()]
        self.assertIn("text/plain", parts)
        self.assertIn("text/html", parts)
        self.assertEqual(parts.count("image/png"), 2)  # inline + attachment

    def test_dry_run_writes_files_and_no_state(self):
        n = self.at("2026-10-20T14:20")
        self.assertEqual(run(self.cfg, make_fake_fetcher(self.cfg, n), n, dry_run=True, sender=self.sender), Outcome.DRY_RUN)
        self.assertTrue((self.cfg.output_dir / "ev-prices-2026-10-20.png").exists())
        html = (self.cfg.output_dir / "ev-prices-2026-10-20.html").read_text(encoding="utf-8")
        self.assertIn('src="ev-prices-2026-10-20.png"', html)   # browser-previewable, no cid: left
        self.assertNotIn("cid:", html)
        self.assertFalse(self.cfg.state_file.exists())
        self.assertEqual(self.sent, [])

    def test_dst_fall_back_day(self):
        # Run on Sat 24 Oct: tomorrow (25 Oct) has 100 slots. Everything must validate and send.
        self.assertEqual(self.go("2026-10-24T14:20"), Outcome.SENT)
        body = self.sent[0].get_body(("plain",)).get_content()
        self.assertNotIn("onvolledig", body)

    def test_dst_spring_forward_day(self):
        self.assertEqual(self.go("2026-03-28T14:20"), Outcome.SENT)
        self.assertNotIn("onvolledig", self.sent[0].get_body(("plain",)).get_content())

    def test_email_is_dutch_and_responsive(self):
        self.go("2026-10-20T14:20")
        msg = self.sent[0]
        subject = msg["Subject"]
        self.assertTrue(subject.startswith("⚡ EV laden:"), subject)
        self.assertRegex(subject, r"\d\du\d\d–\d\du\d\d")           # Belgian clock notation
        text = msg.get_body(("plain",)).get_content()
        self.assertIn("Goedkoopste laadvenster van 4 uur", text)
        self.assertIn("exclusief taksen en nettarieven", text)
        self.assertRegex(text, r"gem\. \d+,\d EUR/MWh")             # decimal comma
        html = msg.get_body(("html",)).get_content()
        self.assertIn('lang="nl-BE"', html)
        self.assertIn('name="viewport"', html)
        self.assertIn("max-width:600px", html)
        self.assertIn("@media only screen and (max-width:620px)", html)
        self.assertIn("<!--[if mso]>", html)                         # Outlook fixed-width fallback
        self.assertIn("cid:", html)
        for word in ("Goedkoopste laadvenster", "Alternatief", "Gemiddelde vorige 30 dagen",
                     "Overmorgen", "exclusief taksen en nettarieven"):
            self.assertIn(word, html)
        self.assertNotIn("Cheapest", html)

    def test_email_without_forecast_and_windows_still_renders(self):
        self.go("2026-10-20T14:20", forecast_ok=False)
        html = self.sent[0].get_body(("html",)).get_content()
        self.assertIn("niet beschikbaar", html)
        self.assertIn("Opgelet", html)

    def test_creg_comparison_in_email(self):
        self.go("2026-10-20T14:20")
        msg = self.sent[0]
        self.assertRegex(msg["Subject"], r"\d+ c (onder|boven) CREG")
        text = msg.get_body(("plain",)).get_content()
        self.assertIn("CREG-tarief Q4 2026 Vlaanderen: 32,25 c€/kWh", text)
        self.assertIn("Break-even day-aheadprijs", text)
        html = msg.get_body(("html",)).get_content()
        self.assertIn("CREG-tarief Q4 2026", html)
        self.assertIn("149 EUR/MWh", html)
        self.assertIn("capaciteitstarief", html)
        self.assertIn("4,6 kW", html)
        self.assertNotIn("goedkoper dan het gemiddelde", html)   # the pill now compares with CREG
        self.assertNotIn("controleer config.yaml", html)         # sample CSV agrees with config

    def test_creg_csv_outage_is_silent(self):
        self.go("2026-10-20T14:20", creg_ok=False)
        html = self.sent[0].get_body(("html",)).get_content()
        self.assertIn("CREG-tarief Q4 2026", html)
        self.assertNotIn("Opgelet", html)

    def test_creg_config_expired_warns(self):
        self.go("2027-01-05T14:20", creg_ok=False)
        text = self.sent[0].get_body(("plain",)).get_content()
        self.assertIn("niet meer geldig", text)
        self.assertIn("gegevens onvolledig", self.sent[0]["Subject"])

    def test_creg_csv_mismatch_warns(self):
        cfg = self.cfg.__class__(**{**self.cfg.__dict__,
                                   "creg": self.cfg.creg.__class__(**{**self.cfg.creg.__dict__, "tariff_c_per_kwh": 30.0})})
        n = self.at("2026-10-20T14:20")
        run(cfg, make_fake_fetcher(cfg, n), n, sender=self.sender, text_fetcher=make_fake_text_fetcher())
        text = self.sent[0].get_body(("plain",)).get_content()
        self.assertIn("controleer config.yaml", text)

    def test_without_creg_config_the_old_email_still_works(self):
        cfg = self.cfg.__class__(**{**self.cfg.__dict__, "creg": None, "cost_stack": None})
        n = self.at("2026-10-20T14:20")
        self.assertEqual(run(cfg, make_fake_fetcher(cfg, n), n, sender=self.sender), Outcome.SENT)
        html = self.sent[0].get_body(("html",)).get_content()
        self.assertNotIn("CREG", html)
        self.assertIn("goedkoper dan het gemiddelde", html)


class TestDutchFormatting(unittest.TestCase):
    def test_helpers(self):
        from evprice import nl
        from datetime import date
        tz = __import__("zoneinfo").ZoneInfo("Europe/Brussels")
        t = pd.Timestamp("2026-10-23 11:45", tz=tz)
        self.assertEqual(nl.day_long(t), "vrijdag 23 oktober")
        self.assertEqual(nl.day_long(t, year=True), "vrijdag 23 oktober 2026")
        self.assertEqual(nl.day_short(t), "vr 23 okt")
        self.assertEqual(nl.clock(t), "11u45")
        self.assertEqual(nl.num(89.25), "89,2")
        self.assertEqual(nl.num(89.0, 0), "89")
        self.assertEqual(nl.relative_day(t, date(2026, 10, 22)), "morgen")
        self.assertEqual(nl.relative_day(t, date(2026, 10, 23)), "vandaag")
        self.assertEqual(nl.relative_day(t, date(2026, 10, 21)), "overmorgen")
        self.assertIsNone(nl.relative_day(t, date(2026, 10, 19)))
        self.assertEqual(nl.when(t, date(2026, 10, 22)), "morgen, vrijdag 23 oktober")
        self.assertEqual(nl.when(t, date(2026, 10, 22), short=True), "morgen (vr 23 okt)")
        self.assertEqual(nl.when(t, date(2026, 10, 1)), "vrijdag 23 oktober")
        midnight = pd.Timestamp("2026-10-24 00:00", tz=tz)
        self.assertEqual(nl.clock_end(pd.Timestamp("2026-10-23 20:00", tz=tz), midnight), "24u00")
        self.assertEqual(nl.clock_end(pd.Timestamp("2026-10-24 00:00", tz=tz),
                                      pd.Timestamp("2026-10-24 04:00", tz=tz)), "04u00")


if __name__ == "__main__":
    unittest.main()

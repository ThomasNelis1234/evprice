"""Offline demo: run the whole pipeline on synthetic data and write the PNG/.eml to ./out.

    PYTHONPATH=src python scripts/demo.py [--now 2026-10-20T14:20] [--no-forecast] [--no-tomorrow]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evprice.config import load_config  # noqa: E402
from evprice.http import SourceError  # noqa: E402
from evprice.main import run  # noqa: E402
from evprice.timeutil import SLOT, day_bounds  # noqa: E402
from synth import energy_charts_payload, predictor_payload  # noqa: E402


def make_fake_fetcher(cfg, now_local, *, forecast_ok=True, tomorrow_ok=True):
    tz = cfg.tz
    today = now_local.date()
    days = [today - timedelta(days=n) for n in range(cfg.history_days, 0, -1)] + [today]
    if tomorrow_ok:
        days.append(today + timedelta(days=1))

    def fetcher(url, params):
        if "/price" in url and "prices" not in url:
            start = pd.Timestamp(params["start"]).tz_convert(tz)
            end = pd.Timestamp(params["end"]).tz_convert(tz)
            payload = energy_charts_payload(days, tz)
            keep = [(t, p) for t, p in zip(payload["unix_seconds"], payload["price"])
                    if start.timestamp() <= t <= end.timestamp()]
            payload["unix_seconds"] = [t for t, _ in keep]
            payload["price"] = [p for _, p in keep]
            return payload
        if "/prices" in url:
            if not forecast_ok:
                raise SourceError("simulated forecast outage")
            # last slot of the last published day — computed via day bounds so DST days (23h/25h) are right
            known_until = day_bounds(today + timedelta(days=1 if tomorrow_ok else 0), tz)[1] - SLOT
            first = pd.Timestamp(now_local).floor("15min")
            return predictor_payload(first, int(params["hours"]), known_until, tz)
        raise SourceError(f"unexpected url {url}")

    return fetcher


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", default="2026-10-20T14:20")
    ap.add_argument("--no-forecast", action="store_true")
    ap.add_argument("--no-tomorrow", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    now = datetime.fromisoformat(a.now).replace(tzinfo=cfg.tz)
    fetcher = make_fake_fetcher(cfg, now, forecast_ok=not a.no_forecast, tomorrow_ok=not a.no_tomorrow)
    outcome = run(cfg, fetcher, now, dry_run=True, force=a.force)
    print("outcome:", outcome, "->", cfg.output_dir.resolve())


if __name__ == "__main__":
    main()

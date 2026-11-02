"""One run of the daily job. Idempotent: safe to invoke every 30 minutes.

Exit codes: 0 = sent or deliberately skipped; 1 = failure (state records it; next tick retries).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .config import Config, load_config
from . import creg as cregmod
from .http import JsonFetcher, SourceError, TextFetcher, make_fetcher, make_text_fetcher
from .mailer import build_message, send
from .models import ActualPrices, DailyBundle, ForecastPrices, Recommendation
from .plot import render_png
from .sources import energy_charts, epex_predictor
from .state import RunRecord, State
from .timeutil import SLOT, day_bounds, local_at, local_midnight
from .validate import ValidationError, assert_well_formed, check_range_complete, day_is_complete
from .windows import cheapest_windows

log = logging.getLogger("evprice")


class Outcome:
    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"
    DRY_RUN = "dry-run"


def collect(cfg: Config, fetcher: JsonFetcher, now_local: datetime,
            fixture_dir: Path | None = None) -> DailyBundle:
    """Fetch and assemble everything for the run day. Raises SourceError/ValidationError on hard failure."""
    tz = cfg.tz
    today = now_local.date()
    tomorrow = today + timedelta(days=1)
    day2 = today + timedelta(days=2)
    warnings: list[str] = []

    horizon_start = local_at(today, cfg.horizon_start_local, tz)
    horizon_end = day_bounds(tomorrow, tz)[1]                       # start of day+2
    history_start = local_midnight(today - timedelta(days=cfg.history_days), tz)

    # One request covers history + today + tomorrow (end inclusive, full ISO timestamps — see source module).
    all_actual, raw_actual = energy_charts.load(fetcher, cfg.actual_base_url, cfg.bidding_zone, tz,
                                                history_start, horizon_end - SLOT)
    assert_well_formed(all_actual)
    if fixture_dir:
        _save_fixture(fixture_dir, f"energy_charts_{today}.json", raw_actual)

    history = all_actual.slice(history_start, local_midnight(today, tz) - SLOT)
    if history.empty:
        raise ValidationError("no history data returned")
    hist_missing = check_range_complete(history, history_start, local_midnight(today, tz))
    if hist_missing:
        warnings.append(f"de 30-daagse historiek is onvolledig ({len(hist_missing)} kwartieren ontbreken)")

    actual = all_actual.slice(horizon_start, horizon_end - SLOT)
    if actual.empty or actual.start > horizon_start:
        raise ValidationError(f"no published prices from {horizon_start:%H:%M} today — cannot build the graph")

    today_missing = check_range_complete(actual, horizon_start, local_midnight(tomorrow, tz))
    if today_missing:
        warnings.append(f"bij de prijzen van vandaag ontbreken {len(today_missing)} kwartieren")

    tomorrow_published = day_is_complete(all_actual, tomorrow, tz)
    if not tomorrow_published:
        n_tom = len(all_actual.slice(local_midnight(tomorrow, tz), horizon_end - SLOT))
        if n_tom:
            warnings.append(f"de prijzen van morgen zijn onvolledig (slechts {n_tom} kwartieren)")
        else:
            warnings.append("de prijzen van morgen zijn nog niet gepubliceerd — de aanbeveling geldt enkel voor vandaag")

    forecast: ForecastPrices | None = None
    known_until = None
    try:
        forecast, known_until, raw_fc = epex_predictor.load(fetcher, cfg.forecast_base_url, cfg.bidding_zone, tz, day2)
        if fixture_dir:
            _save_fixture(fixture_dir, f"epex_predictor_{today}.json", raw_fc)
        if forecast.empty:
            warnings.append("voorspelling voor overmorgen niet beschikbaar (geen waarden ontvangen)")
            forecast = None
        else:
            assert_well_formed(forecast)
            if not day_is_complete(forecast, day2, tz):
                warnings.append("de voorspelling voor overmorgen is onvolledig")
    except (SourceError, ValidationError) as exc:
        log.warning("forecast unavailable: %s", exc)
        warnings.append("voorspelling voor overmorgen niet beschikbaar")
        forecast = None

    return DailyBundle(run_date=today, actual=actual, history=history, forecast=forecast,
                       tomorrow_published=tomorrow_published, forecast_known_until=known_until,
                       warnings=warnings)


def _save_fixture(fixture_dir: Path, name: str, payload: dict) -> None:
    fixture_dir.mkdir(parents=True, exist_ok=True)
    (fixture_dir / name).write_text(json.dumps(payload), encoding="utf-8")


def _save_fixture_text(fixture_dir: Path, name: str, text: str) -> None:
    fixture_dir.mkdir(parents=True, exist_ok=True)
    (fixture_dir / name).write_text(text, encoding="utf-8")


def run(cfg: Config, fetcher: JsonFetcher, now: datetime | None = None, *,
        dry_run: bool = False, force: bool = False, sender=send, fixture_dir: Path | None = None,
        text_fetcher: TextFetcher | None = None) -> str:
    """Execute one tick. Returns an Outcome string.

    ``text_fetcher`` is used only for the optional CREG CSV check; None skips that check.
    """
    tz = cfg.tz
    now = now or datetime.now(timezone.utc)
    now_local = now.astimezone(tz)
    today = now_local.date()
    state = State(cfg.state_file)
    started = now.astimezone(timezone.utc).isoformat(timespec="seconds")

    def finish(status: str, reason: str = "", **extra) -> str:
        if not dry_run:
            state.record(RunRecord(run_date=today.isoformat(), started_at=started, status=status, reason=reason, **extra))
            state.save()
        log.info("outcome=%s %s", status, reason)
        return status

    if state.already_sent(today) and not force:
        return finish(Outcome.SKIPPED, "already sent today")
    if now_local.time() < cfg.earliest_send_local and not force:
        return finish(Outcome.SKIPPED, f"before earliest send time {cfg.earliest_send_local:%H:%M}")

    try:
        bundle = collect(cfg, fetcher, now_local, fixture_dir)
    except (SourceError, ValidationError) as exc:
        log.error("collection failed: %s", exc)
        return finish(Outcome.FAILED, str(exc))

    if not bundle.tomorrow_published and now_local.time() < cfg.publication_cutoff_local and not force:
        return finish(Outcome.SKIPPED, "tomorrow not yet published; will retry",
                      tomorrow_published=False, warnings=bundle.warnings)

    recs = cheapest_windows(bundle.actual, cfg.charging_duration_hours)
    if not recs:
        bundle.warnings.append("te weinig aaneengesloten gepubliceerde kwartieren voor de gevraagde laadduur")

    if cfg.creg and cfg.cost_stack:
        online: list[str] = []
        if cfg.creg.check_online and text_fetcher is not None:
            save = (lambda t: _save_fixture_text(fixture_dir, f"creg_tariff_ev_{today}.csv", t)) if fixture_dir else None
            try:   # a check, never a reason to lose the email
                check = cregmod.check_against_csv(cfg.creg, text_fetcher, today, save_raw=save)
                log.info("CREG check: %s", check.detail)
                online = cregmod.online_warnings(cfg.creg, check)
            except Exception as exc:  # noqa: BLE001
                log.warning("CREG online check crashed: %s", exc)
        bundle.creg = cregmod.compare(cfg.creg, cfg.cost_stack, recs, bundle.period_average, today, online)
        bundle.warnings.extend(bundle.creg.warnings)

    png = render_png(bundle, recs, tz, generated_at=now, horizon_end=day_bounds(today + timedelta(days=2), tz)[1])
    msg = build_message(cfg, bundle, recs, png)

    if dry_run:
        out = cfg.output_dir
        out.mkdir(parents=True, exist_ok=True)
        (out / f"ev-prices-{today}.png").write_bytes(png)
        (out / f"ev-prices-{today}.eml").write_bytes(bytes(msg))
        (out / f"ev-prices-{today}.txt").write_text(msg.get_body(("plain",)).get_content(), encoding="utf-8")
        # Browser-previewable copy of the HTML part: the inline chart is swapped for the PNG next to it.
        html_src = re.sub(r'src="cid:[^"]+"', f'src="ev-prices-{today}.png"', msg.get_body(("html",)).get_content())
        (out / f"ev-prices-{today}.html").write_text(html_src, encoding="utf-8")
        log.info("dry run: wrote %s", out)
        return Outcome.DRY_RUN

    try:
        sender(cfg, msg)
    except Exception as exc:  # noqa: BLE001
        log.error("send failed: %s", exc)
        return finish(Outcome.FAILED, f"send failed: {exc}", warnings=bundle.warnings)

    return finish(Outcome.SENT, sent_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  warnings=bundle.warnings, actual_slots=len(bundle.actual),
                  forecast_slots=len(bundle.forecast) if bundle.forecast else 0,
                  tomorrow_published=bundle.tomorrow_published)


def cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Daily EV charging price alert")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--dry-run", action="store_true", help="render PNG and .eml into the output dir, send nothing")
    p.add_argument("--force", action="store_true", help="ignore time gates and already-sent state")
    p.add_argument("--now", help="pretend the current time is this ISO timestamp (testing)")
    p.add_argument("--save-fixtures", metavar="DIR", help="save raw API responses here")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config(a.config)
    now = datetime.fromisoformat(a.now) if a.now else None
    if now is not None and now.tzinfo is None:
        now = now.replace(tzinfo=cfg.tz)
    fetcher = make_fetcher(cfg.http)
    outcome = run(cfg, fetcher, now, dry_run=a.dry_run, force=a.force,
                  fixture_dir=Path(a.save_fixtures) if a.save_fixtures else None,
                  text_fetcher=make_text_fetcher(cfg.http))
    return 0 if outcome != Outcome.FAILED else 1


if __name__ == "__main__":
    sys.exit(cli())

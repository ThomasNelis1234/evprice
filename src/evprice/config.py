"""Configuration: non-secrets from config.yaml, secrets from the environment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class HttpConfig:
    timeout_seconds: float = 30.0
    retries: int = 3
    backoff_seconds: float = 5.0


@dataclass(frozen=True)
class SmtpConfig:
    host: str = "smtp.gmail.com"
    port: int = 587


@dataclass(frozen=True)
class Secrets:
    gmail_user: str | None = None
    gmail_app_password: str | None = None

    @property
    def complete(self) -> bool:
        return bool(self.gmail_user and self.gmail_app_password)


@dataclass(frozen=True)
class CregConfig:
    """The quarterly CREG reimbursement tariff as configured (all-in, incl. btw, c€/kWh)."""
    region: str
    tariff_c_per_kwh: float
    valid_from: date
    valid_to: date
    based_on: str = ""
    csv_url: str = "https://www.creg.be/sites/default/files/assets/Prices/CREG_Tariff_EV.csv"
    check_online: bool = True


@dataclass(frozen=True)
class CostStackConfig:
    """Per-kWh costs on top of the day-ahead price. c€/kWh, incl. btw unless the name says otherwise."""
    energy_a: float                      # c€/kWh excl. btw, fixed part of the supplier formula
    energy_b: float                      # multiplier on the day-ahead price in EUR/MWh (0.1 = pass-through)
    gsc_wkk_c_per_kwh: float
    green_option_c_per_kwh: float
    federal_excise_c_per_kwh: float
    flemish_energy_fund_c_per_kwh: float
    distribution_operator: str
    distribution_c_per_kwh: float
    capacity_surcharge_c_per_kwh: float = 0.0   # derived from the capacity_* fields unless set explicitly
    capacity_tariff_eur_per_kw_year: float | None = None
    capacity_extra_peak_kw: float | None = None
    capacity_kwh_per_year: float | None = None
    charger_power_kw: float | None = None
    vat: float = 0.06
    source: str = ""


@dataclass(frozen=True)
class Config:
    bidding_zone: str
    timezone: str
    horizon_start_local: time
    earliest_send_local: time
    publication_cutoff_local: time
    charging_duration_hours: int
    history_days: int
    recipients: tuple[str, ...]
    actual_base_url: str
    forecast_base_url: str
    http: HttpConfig = field(default_factory=HttpConfig)
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    secrets: Secrets = field(default_factory=Secrets)
    state_file: Path = Path("state/runs.json")
    output_dir: Path = Path("out")
    creg: CregConfig | None = None
    cost_stack: CostStackConfig | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _parse_time(value: str, name: str) -> time:
    try:
        h, m = str(value).split(":")
        return time(int(h), int(m))
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"{name} must be HH:MM, got {value!r}") from exc


def _parse_recipients(raw: dict, env) -> tuple[str, ...]:
    """Recipients come from the RECIPIENTS environment variable (comma/semicolon/space separated),
    so that no personal address has to live in the repository. config.yaml 'recipients' is a fallback
    for local use only."""
    env_value = (env.get("RECIPIENTS") or "").strip()
    if env_value:
        value = [v for v in re.split(r"[,;\s]+", env_value) if v]
    else:
        value = raw.get("recipients", raw.get("recipient"))
        if isinstance(value, str):
            value = [value]
    recipients = tuple(str(v).strip() for v in (value or []) if str(v).strip())
    if not recipients:
        raise ConfigError("set RECIPIENTS in the environment (or 'recipients' in config.yaml) — at least one address")
    for r in recipients:
        if "@" not in r:
            raise ConfigError(f"invalid recipient address: {r!r}")
    return recipients


def _parse_date(value, name: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"{name} must be YYYY-MM-DD, got {value!r}") from exc


def _parse_creg(raw: dict | None) -> CregConfig | None:
    if not raw:
        return None
    try:
        tariff = float(raw["tariff_c_per_kwh"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError("creg.tariff_c_per_kwh must be a number (c€/kWh)") from exc
    if not 5 < tariff < 100:
        raise ConfigError(f"creg.tariff_c_per_kwh looks wrong: {tariff} (expected c€/kWh, e.g. 32.25)")
    valid_from = _parse_date(raw.get("valid_from"), "creg.valid_from")
    valid_to = _parse_date(raw.get("valid_to"), "creg.valid_to")
    if valid_to < valid_from:
        raise ConfigError("creg.valid_to is before creg.valid_from")
    return CregConfig(
        region=str(raw.get("region", "Vlaanderen")),
        tariff_c_per_kwh=tariff,
        valid_from=valid_from,
        valid_to=valid_to,
        based_on=str(raw.get("based_on", "")),
        csv_url=str(raw.get("csv_url", CregConfig.csv_url)),
        check_online=bool(raw.get("check_online", True)),
    )


def _parse_cost_stack(raw: dict | None) -> CostStackConfig | None:
    if not raw:
        return None

    def num(key: str, default: float | None = None) -> float:
        value = raw.get(key, default)
        if value is None:
            raise ConfigError(f"cost_stack.{key} is required")
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"cost_stack.{key} must be a number, got {value!r}") from exc

    table = raw.get("distribution_c_per_kwh")
    operator = str(raw.get("distribution_operator", "")).strip()
    if isinstance(table, dict):
        if operator not in table:
            raise ConfigError(f"cost_stack.distribution_operator {operator!r} is not in distribution_c_per_kwh "
                              f"(choose one of: {', '.join(table)})")
        distribution = float(table[operator])
    else:
        distribution = num("distribution_c_per_kwh")
    vat = num("vat", 0.06)
    if not 0 <= vat < 1:
        raise ConfigError("cost_stack.vat must be a fraction, e.g. 0.06")

    # Capacity tariff: explicit per-kWh figure wins; otherwise derive it from tariff × extra peak ÷ kWh.
    cap = raw.get("capacity") or {}
    cap_tariff = cap_peak = cap_kwh = charger = None
    if isinstance(cap, dict) and cap:
        table = cap.get("tariff_eur_per_kw_year")
        if isinstance(table, dict):
            if operator not in table:
                raise ConfigError(f"cost_stack.capacity.tariff_eur_per_kw_year has no entry for {operator!r}")
            cap_tariff = float(table[operator])
        elif table is not None:
            cap_tariff = float(table)
        cap_peak = float(cap.get("extra_peak_kw", 0.0))
        cap_kwh = float(cap.get("kwh_per_year", 0.0)) or None
        charger = float(cap["charger_power_kw"]) if cap.get("charger_power_kw") is not None else None
        if cap_peak < 0 or (cap_kwh is not None and cap_kwh <= 0):
            raise ConfigError("cost_stack.capacity: extra_peak_kw must be >= 0 and kwh_per_year > 0")
    if "capacity_surcharge_c_per_kwh" in raw:
        surcharge = num("capacity_surcharge_c_per_kwh", 0.0)
    elif cap_tariff is not None and cap_peak and cap_kwh:
        surcharge = cap_tariff * cap_peak / cap_kwh * 100
    else:
        surcharge = 0.0
    return CostStackConfig(
        energy_a=num("energy_a"),
        energy_b=num("energy_b"),
        gsc_wkk_c_per_kwh=num("gsc_wkk_c_per_kwh", 0.0),
        green_option_c_per_kwh=num("green_option_c_per_kwh", 0.0),
        federal_excise_c_per_kwh=num("federal_excise_c_per_kwh", 0.0),
        flemish_energy_fund_c_per_kwh=num("flemish_energy_fund_c_per_kwh", 0.0),
        distribution_operator=operator or "distributienet",
        distribution_c_per_kwh=distribution,
        capacity_surcharge_c_per_kwh=surcharge,
        capacity_tariff_eur_per_kw_year=cap_tariff,
        capacity_extra_peak_kw=cap_peak,
        capacity_kwh_per_year=cap_kwh,
        charger_power_kw=charger,
        vat=vat,
        source=str(raw.get("source", "")),
    )


def load_config(path: str | Path = "config.yaml", env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    duration = int(raw.get("charging_duration_hours", 4))
    if duration < 1 or duration > 24:
        raise ConfigError("charging_duration_hours must be between 1 and 24")

    sources = raw.get("sources") or {}
    http = raw.get("http") or {}
    smtp = raw.get("smtp") or {}

    return Config(
        bidding_zone=str(raw.get("bidding_zone", "BE")),
        timezone=str(raw.get("timezone", "Europe/Brussels")),
        horizon_start_local=_parse_time(raw.get("horizon_start_local", "14:00"), "horizon_start_local"),
        earliest_send_local=_parse_time(raw.get("earliest_send_local", "13:00"), "earliest_send_local"),
        publication_cutoff_local=_parse_time(raw.get("publication_cutoff_local", "18:00"), "publication_cutoff_local"),
        charging_duration_hours=duration,
        history_days=int(raw.get("history_days", 30)),
        recipients=_parse_recipients(raw, env),
        actual_base_url=str(sources.get("actual", "https://api.energy-charts.info")).rstrip("/"),
        forecast_base_url=str(sources.get("forecast", "https://epexpredictor.batzill.com")).rstrip("/"),
        http=HttpConfig(
            timeout_seconds=float(http.get("timeout_seconds", 30)),
            retries=int(http.get("retries", 3)),
            backoff_seconds=float(http.get("backoff_seconds", 5)),
        ),
        smtp=SmtpConfig(host=str(smtp.get("host", "smtp.gmail.com")), port=int(smtp.get("port", 587))),
        secrets=Secrets(
            gmail_user=env.get("GMAIL_USER") or None,
            # Google shows app passwords as "abcd efgh ijkl mnop"; the spaces are not part of the secret.
            gmail_app_password=(env.get("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip() or None,
        ),
        state_file=Path(raw.get("state_file", "state/runs.json")),
        output_dir=Path(raw.get("output_dir", "out")),
        creg=_parse_creg(raw.get("creg")),
        cost_stack=_parse_cost_stack(raw.get("cost_stack")),
    )

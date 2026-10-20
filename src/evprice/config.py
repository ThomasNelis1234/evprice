"""Configuration: non-secrets from config.yaml, secrets from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
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
class Config:
    bidding_zone: str
    timezone: str
    horizon_start_local: time
    earliest_send_local: time
    publication_cutoff_local: time
    charging_duration_hours: int
    history_days: int
    recipient: str
    actual_base_url: str
    forecast_base_url: str
    http: HttpConfig = field(default_factory=HttpConfig)
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    secrets: Secrets = field(default_factory=Secrets)
    state_file: Path = Path("state/runs.json")
    output_dir: Path = Path("out")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _parse_time(value: str, name: str) -> time:
    try:
        h, m = str(value).split(":")
        return time(int(h), int(m))
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"{name} must be HH:MM, got {value!r}") from exc


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
        recipient=str(raw["recipient"]),
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
    )

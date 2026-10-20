"""Compose and send the daily email (plain text + HTML with inline PNG + PNG attachment)."""

from __future__ import annotations

import html
import logging
import smtplib
import time
from email.message import EmailMessage
from email.utils import make_msgid

from .config import Config
from .models import UNIT, DailyBundle, Recommendation

log = logging.getLogger(__name__)


class MailError(Exception):
    pass


def subject_line(bundle: DailyBundle, recs: list[Recommendation]) -> str:
    if recs:
        r = recs[0]
        core = f"EV charging: cheapest {r.duration_hours}h {r.start:%a %H:%M}–{r.end:%H:%M} (avg {r.avg_price:.0f} {UNIT})"
    else:
        core = "EV charging: no window could be recommended"
    return core + (" — data incomplete" if bundle.warnings else "")


def body_text(bundle: DailyBundle, recs: list[Recommendation], tz) -> str:
    lines = []
    if recs:
        lines.append(f"Cheapest {recs[0].duration_hours}-hour charging window(s), based on published market prices:")
        for i, r in enumerate(recs):
            tag = "Best:       " if i == 0 else "Alternative:"
            lines.append(f"  {tag} {r.start:%A %d %b %H:%M} → {r.end:%H:%M}   avg {r.avg_price:.1f} {UNIT}")
    else:
        lines.append("No charging window could be recommended from the published prices.")
    lines.append("")
    lines.append(f"Average over the shown published period: {bundle.period_average:.1f} {UNIT}")
    lines.append(f"Average over the previous 30 days:        {bundle.history_average:.1f} {UNIT}")
    lines.append("")
    if bundle.forecast is not None and not bundle.forecast.empty:
        f = bundle.forecast
        lines.append(f"Day after tomorrow ({f.start:%A}): forecast only, average {f.mean():.1f} {UNIT} — "
                     "an estimate, not a market price. Not used for the recommendation.")
    else:
        lines.append("Day after tomorrow: forecast unavailable.")
    if bundle.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"  ! {w}" for w in bundle.warnings)
    lines.append("")
    lines.append(f"Prices are raw wholesale day-ahead prices (EUR/MWh, before taxes and grid costs), "
                 f"fetched {bundle.actual.fetched_at.astimezone(tz):%Y-%m-%d %H:%M %Z}.")
    return "\n".join(lines)


def build_message(cfg: Config, bundle: DailyBundle, recs: list[Recommendation], png: bytes) -> EmailMessage:
    tz = cfg.tz
    msg = EmailMessage()
    msg["Subject"] = subject_line(bundle, recs)
    msg["From"] = cfg.secrets.gmail_user or "evprice@localhost"
    msg["To"] = cfg.recipient
    text = body_text(bundle, recs, tz)
    msg.set_content(text)

    cid = make_msgid(domain="evprice")
    html_body = (
        "<html><body style='font-family:system-ui,sans-serif;color:#0b0b0b'>"
        f"<pre style='font-family:inherit;white-space:pre-wrap'>{html.escape(text)}</pre>"
        f"<img src='cid:{cid[1:-1]}' alt='Price chart' style='max-width:100%'>"
        "</body></html>"
    )
    msg.add_alternative(html_body, subtype="html")
    msg.get_payload()[1].add_related(png, maintype="image", subtype="png", cid=cid)
    msg.add_attachment(png, maintype="image", subtype="png",
                       filename=f"ev-prices-{bundle.run_date:%Y-%m-%d}.png")
    return msg


def _deliver(cfg: Config, msg: EmailMessage) -> None:
    """Try implicit TLS (465) first, then STARTTLS (587). Some runners close 587 connections."""
    errors = []
    for mode in ("ssl", "starttls"):
        try:
            if mode == "ssl":
                with smtplib.SMTP_SSL(cfg.smtp.host, 465, timeout=30) as s:
                    s.login(cfg.secrets.gmail_user, cfg.secrets.gmail_app_password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(cfg.smtp.host, cfg.smtp.port, timeout=30) as s:
                    s.ehlo()
                    s.starttls()
                    s.ehlo()
                    s.login(cfg.secrets.gmail_user, cfg.secrets.gmail_app_password)
                    s.send_message(msg)
            log.info("email sent via %s", mode)
            return
        except smtplib.SMTPAuthenticationError:
            raise
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("%s delivery failed: %s", mode, exc)
            errors.append(f"{mode}: {exc}")
    raise smtplib.SMTPException("; ".join(errors))


def send(cfg: Config, msg: EmailMessage, retries: int = 3, sleep=time.sleep) -> None:
    if not cfg.secrets.complete:
        raise MailError("GMAIL_USER / GMAIL_APP_PASSWORD not set")
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            _deliver(cfg, msg)
            log.info("email sent to %s", cfg.recipient)
            return
        except smtplib.SMTPAuthenticationError as exc:
            raise MailError(f"SMTP authentication failed: {exc}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            last = exc
            log.warning("send attempt %d/%d failed: %s", attempt, retries, exc)
            if attempt < retries:
                sleep(10 * attempt)
    raise MailError(f"email could not be sent after {retries} attempts: {last}")

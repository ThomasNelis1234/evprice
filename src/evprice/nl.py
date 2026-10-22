"""Dutch (Belgium) formatting helpers for everything the user sees.

Deliberately locale-independent: the GitHub Actions runner has no nl_BE locale, so day and
month names are spelled out here. Belgian conventions: lowercase day/month names, "11u45"
clock times, decimal comma, "u" for hours.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

WEEKDAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
WEEKDAYS_SHORT = ["ma", "di", "wo", "do", "vr", "za", "zo"]
MONTHS = ["januari", "februari", "maart", "april", "mei", "juni",
          "juli", "augustus", "september", "oktober", "november", "december"]
MONTHS_SHORT = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]


def weekday(d: date | datetime | pd.Timestamp, short: bool = False) -> str:
    return (WEEKDAYS_SHORT if short else WEEKDAYS)[d.weekday()]


def month(d: date | datetime | pd.Timestamp, short: bool = False) -> str:
    return (MONTHS_SHORT if short else MONTHS)[d.month - 1]


def day_long(d: date | datetime | pd.Timestamp, year: bool = False) -> str:
    """'vrijdag 23 oktober' (optionally with year)."""
    s = f"{weekday(d)} {d.day} {month(d)}"
    return f"{s} {d.year}" if year else s


def day_short(d: date | datetime | pd.Timestamp) -> str:
    """'vr 23 okt'."""
    return f"{weekday(d, short=True)} {d.day} {month(d, short=True)}"


def clock(t: datetime | pd.Timestamp) -> str:
    """'11u45' — Belgian clock notation."""
    return f"{t.hour:02d}u{t.minute:02d}"


def clock_end(start: pd.Timestamp, end: pd.Timestamp) -> str:
    """End of a window; an end exactly at the following midnight reads '24u00', not '00u00'."""
    if (end.hour, end.minute) == (0, 0) and end.date() != start.date():
        return "24u00"
    return clock(end)


def num(value: float, decimals: int = 1) -> str:
    """Decimal comma, no thousands separator (values are a few hundred at most)."""
    return f"{value:.{decimals}f}".replace(".", ",")


def relative_day(d: date | datetime | pd.Timestamp, today: date) -> str | None:
    """'vandaag', 'morgen', 'overmorgen' or None when further away."""
    day = d.date() if isinstance(d, datetime) else d   # pd.Timestamp is a datetime subclass
    delta = (day - today).days
    return {0: "vandaag", 1: "morgen", 2: "overmorgen"}.get(delta)


def when(start: pd.Timestamp, today: date, short: bool = False) -> str:
    """'morgen (vr 23 okt)' / 'vrijdag 23 oktober' depending on distance from today."""
    rel = relative_day(start, today)
    if rel:
        return f"{rel} ({day_short(start)})" if short else f"{rel}, {day_long(start)}"
    return day_short(start) if short else day_long(start)

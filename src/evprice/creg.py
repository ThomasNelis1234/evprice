"""Comparison with the CREG reimbursement tariff for home-charging a company car.

The CREG tariff is an *all-in* c€/kWh amount (energy + network + levies + btw) that an employer
may reimburse per kWh charged at home. Our prices are raw day-ahead EUR/MWh. To compare them we
model what one extra kWh really costs the household on a dynamic contract:

    all_in(P) = (energy_a + energy_b × P) × (1 + vat)             supplier formula, P in EUR/MWh
              + gsc_wkk + green_option + distribution + excise + energy fund + capacity surcharge

(the last line is already incl. btw on the tariff sheet). Inverting it gives the day-ahead price
at which charging costs exactly the reimbursement — the "break-even" line on the chart.

The configured tariff is checked for validity, and optionally against CREG's monthly CSV. The
CSV's exact layout was not verifiable when this was written, so the reader is deliberately
tolerant and every failure degrades to "could not check" — it never blocks the email.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import date

from .config import CostStackConfig, CregConfig
from .http import SourceError, TextFetcher
from .models import Recommendation

log = logging.getLogger(__name__)

# FOD Financiën rule: quarter → (year offset, months) of the CREG monthly values averaged.
QUARTER_MONTHS: dict[int, list[tuple[int, int]]] = {
    1: [(-1, 8), (-1, 9), (-1, 10)],
    2: [(-1, 11), (-1, 12), (0, 1)],
    3: [(0, 2), (0, 3), (0, 4)],
    4: [(0, 5), (0, 6), (0, 7)],
}
MONTH_NAMES_NL = ["januari", "februari", "maart", "april", "mei", "juni",
                  "juli", "augustus", "september", "oktober", "november", "december"]

REGION_ALIASES = {
    "vlaanderen": ("vlaanderen", "flanders", "flandre", "vlaams", "flemish", "vl"),
    "brussel": ("brussel", "brussels", "bruxelles", "bru", "bhg"),
    "wallonië": ("wallonië", "wallonie", "wallonia", "waals", "wal", "walloon"),
}


# --------------------------------------------------------------------------- cost model

@dataclass(frozen=True)
class CostStack:
    cfg: CostStackConfig

    @property
    def fixed_incl_vat(self) -> float:
        """Everything per kWh that does not move with the day-ahead price, incl. btw (c€/kWh)."""
        c = self.cfg
        return (c.gsc_wkk_c_per_kwh + c.green_option_c_per_kwh + c.distribution_c_per_kwh
                + c.federal_excise_c_per_kwh + c.flemish_energy_fund_c_per_kwh + c.capacity_surcharge_c_per_kwh)

    def energy_incl_vat(self, spot_eur_mwh: float) -> float:
        c = self.cfg
        return (c.energy_a + c.energy_b * spot_eur_mwh) * (1 + c.vat)

    def all_in(self, spot_eur_mwh: float) -> float:
        """Total cost of one kWh charged at the given day-ahead price, c€/kWh incl. btw."""
        return self.energy_incl_vat(spot_eur_mwh) + self.fixed_incl_vat

    def break_even(self, tariff_c_per_kwh: float) -> float:
        """Day-ahead price (EUR/MWh) at which all_in == tariff."""
        c = self.cfg
        return ((tariff_c_per_kwh - self.fixed_incl_vat) / (1 + c.vat) - c.energy_a) / c.energy_b


@dataclass(frozen=True)
class WindowCost:
    label: str                 # "beste", "alternatief", "gemiddelde periode"
    spot_eur_mwh: float
    all_in_c_per_kwh: float
    margin_c_per_kwh: float    # tariff − all_in; positive = charging is cheaper than the reimbursement


@dataclass(frozen=True)
class CregComparison:
    tariff: CregConfig
    stack: CostStack
    break_even_eur_mwh: float
    windows: list[WindowCost]
    warnings: list[str]

    @property
    def tariff_c(self) -> float:
        return self.tariff.tariff_c_per_kwh

    @property
    def best(self) -> WindowCost | None:
        """The best recommended window's cost — None when nothing could be recommended
        (the period-average row is context, never a recommendation)."""
        return next((w for w in self.windows if w.label == "beste venster"), None)

    def quarter_label(self) -> str:
        q = (self.tariff.valid_from.month - 1) // 3 + 1
        return f"Q{q} {self.tariff.valid_from.year}"


def compare(creg: CregConfig, stack_cfg: CostStackConfig, recs: list[Recommendation],
            period_average: float, today: date, extra_warnings: list[str] | None = None) -> CregComparison:
    stack = CostStack(stack_cfg)
    tariff = creg.tariff_c_per_kwh
    windows: list[WindowCost] = []
    for i, r in enumerate(recs):
        label = "beste venster" if i == 0 else "alternatief"
        cost = stack.all_in(r.avg_price)
        windows.append(WindowCost(label, r.avg_price, cost, tariff - cost))
    cost = stack.all_in(period_average)
    windows.append(WindowCost("gemiddelde getoonde periode", period_average, cost, tariff - cost))

    warnings = list(extra_warnings or [])
    if not creg.valid_from <= today <= creg.valid_to:
        warnings.append(f"het CREG-tarief in de configuratie ({tariff:.2f} c€/kWh, geldig "
                        f"{creg.valid_from:%d/%m/%Y}–{creg.valid_to:%d/%m/%Y}) is niet meer geldig — "
                        "werk config.yaml bij met het tarief van dit kwartaal")
    return CregComparison(creg, stack, stack.break_even(tariff), windows, warnings)


# --------------------------------------------------------------------------- CREG CSV check

@dataclass(frozen=True)
class CsvCheck:
    ok: bool                      # True = parsed and compared
    implied_tariff: float | None  # average of the three months the current quarter is based on
    detail: str                   # human-readable outcome (logged; becomes a warning on mismatch)


def _to_float(text: str) -> float | None:
    t = text.strip().replace(" ", "").replace(" ", "")
    if not t:
        return None
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")      # 1.234,56
    else:
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


_DATE_PATTERNS = [
    (re.compile(r"^(\d{4})[-/](\d{1,2})(?:[-/]\d{1,2})?$"), lambda m: (int(m.group(1)), int(m.group(2)))),
    (re.compile(r"^(\d{1,2})[-/](\d{4})$"), lambda m: (int(m.group(2)), int(m.group(1)))),
    (re.compile(r"^(?:\d{1,2}[-/])?(\d{1,2})[-/](\d{4})$"), lambda m: (int(m.group(2)), int(m.group(1)))),
    (re.compile(r"^(\d{4})(\d{2})$"), lambda m: (int(m.group(1)), int(m.group(2)))),
]
_MONTH_WORDS = {name[:3]: i + 1 for i, name in enumerate(MONTH_NAMES_NL)}
_MONTH_WORDS.update({"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
                     "sep": 9, "oct": 10, "nov": 11, "dec": 12, "mrt": 3, "mei": 5, "okt": 10})


def _year_month(text: str) -> tuple[int, int] | None:
    t = text.strip().strip('"')
    for pattern, conv in _DATE_PATTERNS:
        m = pattern.match(t)
        if m:
            y, mo = conv(m)
            if 2000 <= y <= 2100 and 1 <= mo <= 12:
                return y, mo
    m = re.match(r"^([A-Za-zéû]{3,9})\.?\s*[-/ ]?\s*(\d{4})$", t) or re.match(r"^(\d{4})\s*[-/ ]?\s*([A-Za-zéû]{3,9})$", t)
    if m:
        a, b = m.group(1), m.group(2)
        word, year = (a, b) if not a.isdigit() else (b, a)
        mo = _MONTH_WORDS.get(word[:3].lower())
        if mo:
            return int(year), mo
    return None


def parse_monthly_csv(text: str, region: str) -> dict[tuple[int, int], float]:
    """Return {(year, month): c€/kWh} for ``region`` from CREG's CSV, whatever its exact shape.

    Handles ';' or ',' separators, decimal commas, and both a wide layout (one column per region)
    and a long layout (a region column plus a value column). Raises SourceError when nothing usable
    is found, so the caller can report "could not check".
    """
    text = text.lstrip("﻿")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise SourceError("CREG CSV: too few rows")

    aliases = REGION_ALIASES.get(region.lower(), (region.lower(),))

    def is_region(cell: str) -> bool:
        c = cell.strip().lower()
        return any(c == a or c.startswith(a) or a in c for a in aliases)

    header = rows[0]
    date_col = next((i for i, c in enumerate(header) if re.search(r"date|datum|maand|month|period", c, re.I)), None)
    if date_col is None:   # header might not name it — take the first column that parses as a month in row 2
        date_col = next((i for i, c in enumerate(rows[1]) if _year_month(c)), 0)

    out: dict[tuple[int, int], float] = {}
    region_cols = [i for i, c in enumerate(header) if is_region(c)]
    if region_cols:                                            # wide layout
        col = region_cols[0]
        for r in rows[1:]:
            if len(r) <= max(col, date_col):
                continue
            ym = _year_month(r[date_col])
            val = _to_float(r[col])
            if ym and val is not None:
                out[ym] = val
    else:                                                      # long layout: find a region cell per row
        value_col = next((i for i, c in enumerate(header) if re.search(r"prijs|price|tarif|value|c€|cent", c, re.I)), None)
        for r in rows[1:]:
            if not any(is_region(c) for c in r):
                continue
            ym = _year_month(r[date_col]) if len(r) > date_col else None
            if not ym:
                continue
            candidates = [r[value_col]] if value_col is not None and len(r) > value_col else \
                [c for i, c in enumerate(r) if i != date_col and not is_region(c)]
            vals = [v for v in (_to_float(c) for c in candidates) if v is not None]
            if vals:
                out[ym] = vals[-1]
    if not out:
        raise SourceError(f"CREG CSV: no monthly values found for region {region!r}")
    # The series may be in €/kWh rather than c€/kWh; normalise to c€/kWh.
    if max(out.values()) < 2:
        out = {k: v * 100 for k, v in out.items()}
    return out


def implied_quarter_tariff(monthly: dict[tuple[int, int], float], today: date) -> tuple[float | None, list[str]]:
    """Average the three months the current quarter's tariff is based on. None if any is missing."""
    q = (today.month - 1) // 3 + 1
    months = [(today.year + dy, m) for dy, m in QUARTER_MONTHS[q]]
    names = [f"{MONTH_NAMES_NL[m - 1]} {y}" for y, m in months]
    if any(k not in monthly for k in months):
        return None, names
    return sum(monthly[k] for k in months) / 3, names


def check_against_csv(creg: CregConfig, fetch: TextFetcher, today: date,
                      save_raw=None) -> CsvCheck:
    """Download CREG's CSV and compare the implied current-quarter tariff with the configured one."""
    try:
        text = fetch(creg.csv_url)
    except SourceError as exc:
        return CsvCheck(False, None, f"CREG CSV niet opgehaald: {exc}")
    if save_raw:
        try:
            save_raw(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not save CREG CSV fixture: %s", exc)
    try:
        monthly = parse_monthly_csv(text, creg.region)
    except SourceError as exc:
        return CsvCheck(False, None, str(exc))
    implied, names = implied_quarter_tariff(monthly, today)
    if implied is None:
        have = ", ".join(f"{MONTH_NAMES_NL[m - 1]} {y}" for y, m in sorted(monthly)[-3:])
        return CsvCheck(False, None, f"CREG CSV gelezen, maar niet alle maanden ({', '.join(names)}) "
                                     f"zijn beschikbaar (laatste: {have})")
    return CsvCheck(True, implied, f"CREG CSV: gemiddelde {', '.join(names)} = {implied:.2f} c€/kWh "
                                   f"(configuratie: {creg.tariff_c_per_kwh:.2f})")


def online_warnings(creg: CregConfig, check: CsvCheck, tolerance: float = 0.05) -> list[str]:
    """Turn a CSV check into user-facing warnings (Dutch). Nothing when it matches or could not run."""
    if not check.ok or check.implied_tariff is None:
        log.info("CREG online check skipped: %s", check.detail)
        return []
    if abs(check.implied_tariff - creg.tariff_c_per_kwh) > tolerance:
        return [f"volgens de CREG-cijfers zou het tarief van dit kwartaal ongeveer {check.implied_tariff:.2f} c€/kWh "
                f"moeten zijn, de configuratie gebruikt {creg.tariff_c_per_kwh:.2f} — controleer config.yaml"]
    log.info("CREG online check OK: %s", check.detail)
    return []

"""Compose and send the daily email: Dutch (BE) plain text + responsive HTML with the chart inline
(CID) and attached as PNG.

The HTML is built the way email clients need it — nested tables, inline styles, fixed-width
Outlook fallback in conditional comments, fluid "stacking" columns that need no media queries —
so it renders on Gmail (web, Android, iOS), Outlook (desktop, web, mobile), Apple Mail and
phone-sized viewports alike. Icons are emoji: no remote images, nothing for a client to block.
"""

from __future__ import annotations

import html
import logging
import smtplib
import time
from datetime import date
from email.message import EmailMessage
from email.utils import make_msgid

import pandas as pd

from . import nl
from .config import Config
from .models import UNIT, DailyBundle, Recommendation
from .sources import energy_charts, epex_predictor

log = logging.getLogger(__name__)

# Palette — matches plot.py so the chart and the cards read as one piece.
C = {
    "bg": "#f1f1ee",
    "card": "#ffffff",
    "text": "#0b0b0b",
    "muted": "#52514e",
    "faint": "#8a8984",
    "line": "#e6e5e1",
    "navy": "#0f2742",
    "navy_text": "#c9d6e6",
    "blue": "#2a78d6",
    "green": "#1baf7a",
    "green_bg": "#e4f6ee",
    "green_dark": "#0f7a52",
    "orange": "#eb6834",
    "orange_bg": "#fdeee6",
    "amber": "#d9930d",
    "amber_bg": "#fdf3dc",
    "red": "#c8362b",
    "red_bg": "#fbe7e4",
    "purple": "#7a3fb8",
    "purple_bg": "#f1eaf9",
    "grey_bg": "#f6f6f3",
}
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


class MailError(Exception):
    pass


# --------------------------------------------------------------------------- text helpers

def span(r: Recommendation, sep: str = " → ") -> str:
    """'11u45 → 15u45' (an end at midnight reads '24u00')."""
    return f"{nl.clock(r.start)}{sep}{nl.clock_end(r.start, r.end)}"


def _savings_pct(best: float, reference: float) -> int | None:
    """Whole-percent saving of the best window versus a reference average, or None if meaningless."""
    if reference <= 0 or best >= reference:
        return None
    return round((1 - best / reference) * 100)


def _fetched_txt(bundle: DailyBundle, tz) -> str:
    f = bundle.actual.fetched_at.astimezone(tz)
    return f"{nl.day_long(f)} om {nl.clock(f)} ({f:%Z})"


def _margin_txt(margin: float, short: bool = False) -> str:
    """'10,3 c€/kWh onder het CREG-tarief' / short: '10 c onder CREG'."""
    side = "onder" if margin >= 0 else "boven"
    if short:
        return f"{abs(margin):.0f} c {side} CREG"
    return f"{nl.num(abs(margin))} c€/kWh {side} het CREG-tarief"


def headline(bundle: DailyBundle, recs: list[Recommendation]) -> str:
    """Subject without the leading icon; the key facts come first so phones show them uncut."""
    if recs:
        r = recs[0]
        rel = nl.relative_day(r.start, bundle.run_date) or nl.day_short(r.start)
        creg = f" · {_margin_txt(bundle.creg.best.margin_c_per_kwh, short=True)}" if bundle.creg and bundle.creg.best else ""
        core = (f"EV laden: {rel} {span(r, '–')} · gem. {r.avg_price:.0f} {UNIT}{creg} "
                f"({r.duration_hours} u, {nl.day_short(r.start)})")
    else:
        core = "EV laden: geen laadvenster kon aanbevolen worden"
    return core + (" — gegevens onvolledig" if bundle.warnings else "")


def subject_line(bundle: DailyBundle, recs: list[Recommendation]) -> str:
    icon = "⚡" if recs and not bundle.warnings else "⚠️"
    return f"{icon} {headline(bundle, recs)}"


def body_text(bundle: DailyBundle, recs: list[Recommendation], tz) -> str:
    today = bundle.run_date
    lines: list[str] = [f"EV-laadplanner — {nl.day_long(today, year=True)}", ""]
    if recs:
        best = recs[0]
        lines.append(f"Goedkoopste laadvenster van {best.duration_hours} uur, op basis van gepubliceerde marktprijzen:")
        lines.append(f"  Beste:       {nl.when(best.start, today)}, {span(best)}   gem. {nl.num(best.avg_price)} {UNIT}")
        if bundle.creg and bundle.creg.best:
            w = bundle.creg.best
            lines.append(f"               ≈ {nl.num(w.all_in_c_per_kwh)} c€/kWh all-in, "
                         f"{_margin_txt(w.margin_c_per_kwh)}")
        else:
            pct = _savings_pct(best.avg_price, bundle.period_average)
            if pct:
                lines.append(f"               ≈ {pct} % goedkoper dan het gemiddelde van de getoonde periode")
        for r in recs[1:]:
            lines.append(f"  Alternatief: {nl.when(r.start, today)}, {span(r)}   gem. {nl.num(r.avg_price)} {UNIT}")
    else:
        lines.append("Geen laadvenster kon aanbevolen worden op basis van de gepubliceerde prijzen.")
    lines.append("")
    lines.append(f"Gemiddelde van de getoonde periode:  {nl.num(bundle.period_average)} {UNIT}")
    lines.append(f"Gemiddelde van de vorige 30 dagen:   {nl.num(bundle.history_average)} {UNIT}")
    lines.append("")
    if bundle.creg:
        c = bundle.creg
        lines.append(f"CREG-tarief {c.quarter_label()} {c.tariff.region}: {nl.num(c.tariff_c, 2)} c€/kWh all-in"
                     + (f" (gebaseerd op {c.tariff.based_on})" if c.tariff.based_on else "") + ".")
        lines.append(f"Break-even day-aheadprijs met jouw kostenstructuur: {c.break_even_eur_mwh:.0f} {UNIT} — "
                     "daarboven kost laden meer dan je terugkrijgt.")
        for w in c.windows:
            lines.append(f"  {w.label:<28} {nl.num(w.spot_eur_mwh):>6} {UNIT} → "
                         f"{nl.num(w.all_in_c_per_kwh):>5} c€/kWh all-in, {_margin_txt(w.margin_c_per_kwh)}")
        st = c.stack.cfg
        if st.capacity_surcharge_c_per_kwh > 0:
            lines.append(f"Inclusief ≈ {nl.num(st.capacity_surcharge_c_per_kwh)} c€/kWh capaciteitstarief "
                         f"(+{nl.num(st.capacity_extra_peak_kw or 0)} kW maandpiek door het laden"
                         + (f", laden begrensd op {nl.num(st.charger_power_kw)} kW" if st.charger_power_kw else "") + ").")
        else:
            lines.append("Het capaciteitstarief (maandpiek) zit hier niet in; laad traag genoeg om je piek niet op te drijven.")
        lines.append("")
    if bundle.forecast is not None and not bundle.forecast.empty:
        f = bundle.forecast
        lines.append(f"Overmorgen ({nl.day_long(f.start)}): enkel een voorspelling, gemiddeld "
                     f"{nl.num(f.mean())} {UNIT} — een schatting, geen marktprijs. "
                     "Wordt niet gebruikt voor de aanbeveling.")
    else:
        lines.append("Overmorgen: voorspelling niet beschikbaar.")
    if bundle.warnings:
        lines.append("")
        lines.append("Opgelet:")
        lines.extend(f"  ! {w}" for w in bundle.warnings)
    lines.append("")
    lines.append(f"Ruwe groothandelsprijzen day-ahead ({UNIT}, exclusief taksen en nettarieven), "
                 f"Belgische biedzone, opgehaald op {_fetched_txt(bundle, tz)}.")
    lines.append(energy_charts.ATTRIBUTION)
    lines.append(epex_predictor.ATTRIBUTION)
    lines.append("")
    lines.append("De grafiek zit als PNG bij deze mail.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- HTML helpers

def _e(s: str) -> str:
    return html.escape(str(s), quote=True)


def _card(inner: str, *, bg: str, accent: str | None = None, outline: bool = False,
          pad: str = "18px 20px") -> str:
    """A rounded card as a full-width table (Outlook ignores the radius, keeps the colours)."""
    border = f"border:1px solid {C['line']};" if outline else ""
    if accent:
        border += f"border-left:5px solid {accent};"
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:separate;border-radius:12px;background-color:{bg};" bgcolor="{bg}">'
        f'<tr><td style="padding:{pad};{border}border-radius:12px;font-family:{FONT};color:{C["text"]};'
        f'font-size:15px;line-height:22px;">'
        f'{inner}</td></tr></table>'
    )


def _label(icon: str, text: str, color: str) -> str:
    return (f'<div style="font-family:{FONT};font-size:12px;line-height:18px;letter-spacing:0.6px;'
            f'text-transform:uppercase;font-weight:700;color:{color};">'
            f'<span style="font-size:15px;line-height:18px;">{icon}</span>&nbsp; {_e(text)}</div>')


def _spacer(h: int) -> str:
    return f'<tr><td style="height:{h}px;line-height:{h}px;font-size:0;">&nbsp;</td></tr>'


def _row(inner: str, columns: bool = False) -> str:
    # The column row is inset 4px less: each column pads 4px itself, so its cards line up.
    cls, pad = ("pad-cols", "0 20px") if columns else ("pad", "0 24px")
    return f'<tr><td class="{cls}" style="padding:{pad};">{inner}</td></tr>'


def _stat_col(icon: str, label: str, value: str, unit: str) -> str:
    """One fluid column: inline-block with a max-width, so two fit side by side on desktop and
    stack on narrow screens without media queries."""
    inner = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:separate;border-radius:12px;background-color:{C["grey_bg"]};" bgcolor="{C["grey_bg"]}">'
        f'<tr><td style="padding:14px 16px;font-family:{FONT};">'
        f'<div style="font-size:12px;line-height:18px;color:{C["muted"]};">'
        f'<span style="font-size:14px;">{icon}</span>&nbsp; {_e(label)}</div>'
        f'<div style="font-size:22px;line-height:28px;font-weight:700;color:{C["text"]};margin-top:2px;">'
        f'{_e(value)} <span style="font-size:13px;font-weight:400;color:{C["muted"]};">{_e(unit)}</span></div>'
        f'</td></tr></table>'
    )
    return (f'<div class="col" style="display:inline-block;width:100%;max-width:280px;vertical-align:top;">'
            f'<div style="padding:4px;">{inner}</div></div>')


def _hero(bundle: DailyBundle, recs: list[Recommendation]) -> str:
    today = bundle.run_date
    if not recs:
        inner = (_label("⚠️", "Geen aanbeveling", C["amber"]) +
                 f'<div style="font-size:17px;line-height:24px;font-weight:700;margin-top:6px;">'
                 f'Er kon geen laadvenster aanbevolen worden.</div>'
                 f'<div style="font-size:14px;line-height:21px;color:{C["muted"]};margin-top:4px;">'
                 f'De gepubliceerde prijzen volstaan niet om een aaneengesloten venster te kiezen. '
                 f'Bekijk de grafiek hieronder.</div>')
        return _card(inner, bg=C["amber_bg"], accent=C["amber"])

    best = recs[0]
    pill = ""
    if bundle.creg and bundle.creg.best:
        w = bundle.creg.best
        good = w.margin_c_per_kwh >= 0
        pill = (f'<span style="display:inline-block;margin-top:10px;padding:4px 10px;border-radius:999px;'
                f'background-color:{C["green"] if good else C["red"]};color:#ffffff;font-size:12px;line-height:16px;'
                f'font-weight:700;">{"↓" if good else "↑"} ≈ {_e(nl.num(w.all_in_c_per_kwh))}&nbsp;c€/kWh all-in · '
                f'{_e(_margin_txt(w.margin_c_per_kwh))}</span>')
    else:
        pct = _savings_pct(best.avg_price, bundle.period_average)
        if pct:
            pill = (f'<span style="display:inline-block;margin-top:10px;padding:4px 10px;border-radius:999px;'
                    f'background-color:{C["green"]};color:#ffffff;font-size:12px;line-height:16px;font-weight:700;">'
                    f'↓ ≈ {pct}&nbsp;% goedkoper dan het gemiddelde van de periode</span>')
    inner = (
        _label("⚡", f"Goedkoopste laadvenster · {best.duration_hours} u", C["green_dark"]) +
        f'<div style="font-size:16px;line-height:24px;color:{C["muted"]};margin-top:8px;">'
        f'{_e(nl.when(best.start, today).capitalize())}</div>'
        f'<div class="hero-time" style="font-size:34px;line-height:40px;font-weight:800;color:{C["text"]};'
        f'letter-spacing:-0.5px;">{_e(span(best))}</div>'
        f'<div style="font-size:15px;line-height:22px;color:{C["text"]};margin-top:2px;">'
        f'gemiddeld <strong>{_e(nl.num(best.avg_price))}&nbsp;{UNIT}</strong></div>'
        f'{pill}'
    )
    return _card(inner, bg=C["green_bg"], accent=C["green"])


def _alternative(bundle: DailyBundle, recs: list[Recommendation]) -> str:
    if len(recs) < 2:
        return ""
    r = recs[1]
    inner = (
        _label("🔁", "Alternatief", C["muted"]) +
        f'<div style="font-size:15px;line-height:22px;color:{C["muted"]};margin-top:6px;">'
        f'{_e(nl.when(r.start, bundle.run_date).capitalize())}</div>'
        f'<div style="font-size:19px;line-height:26px;font-weight:700;white-space:nowrap;">{_e(span(r))}</div>'
        f'<div style="font-size:15px;line-height:22px;color:{C["muted"]};">'
        f'gemiddeld {_e(nl.num(r.avg_price))}&nbsp;{UNIT}</div>'
    )
    return _card(inner, bg=C["card"], accent=C["blue"], outline=True)


def _stats(bundle: DailyBundle) -> str:
    """Two fluid columns. Outlook (Word engine) ignores inline-block, so it gets a real two-cell
    table through conditional comments; every other client stacks the columns when narrow."""
    cols = [
        _stat_col("📊", "Gemiddelde getoonde periode", nl.num(bundle.period_average), UNIT),
        _stat_col("📅", "Gemiddelde vorige 30 dagen", nl.num(bundle.history_average), UNIT),
    ]
    mso_open = ('<!--[if mso]><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
                '<tr><td width="50%" valign="top"><![endif]-->')
    mso_between = '<!--[if mso]></td><td width="50%" valign="top"><![endif]-->'
    mso_close = '<!--[if mso]></td></tr></table><![endif]-->'
    return ('<div style="font-size:0;text-align:left;">'
            + mso_open + mso_between.join(cols) + mso_close + '</div>')


def _creg(bundle: DailyBundle) -> str:
    c = bundle.creg
    if c is None:
        return ""
    t = c.tariff
    based = f" · op basis van {_e(t.based_on)}" if t.based_on else ""
    rows = ""
    for w in c.windows:
        good = w.margin_c_per_kwh >= 0
        color = C["green_dark"] if good else C["red"]
        rows += (
            f'<tr>'
            f'<td style="padding:6px 0;border-top:1px solid {C["line"]};font-size:14px;line-height:20px;color:{C["muted"]};">'
            f'{_e(w.label.capitalize())}<br><span style="font-size:12px;">{_e(nl.num(w.spot_eur_mwh))} {UNIT}</span></td>'
            f'<td align="right" style="padding:6px 0 6px 8px;border-top:1px solid {C["line"]};font-size:15px;line-height:20px;'
            f'white-space:nowrap;"><strong>{_e(nl.num(w.all_in_c_per_kwh))}</strong> <span style="font-size:12px;color:{C["muted"]};">c€/kWh</span></td>'
            f'<td align="right" style="padding:6px 0 6px 12px;border-top:1px solid {C["line"]};font-size:14px;line-height:20px;'
            f'font-weight:700;color:{color};white-space:nowrap;">{"−" if good else "+"}{_e(nl.num(abs(w.margin_c_per_kwh)))}</td>'
            f'</tr>'
        )
    stack = c.stack.cfg
    if stack.capacity_surcharge_c_per_kwh > 0:
        if stack.capacity_tariff_eur_per_kw_year and stack.capacity_extra_peak_kw and stack.capacity_kwh_per_year:
            kwh_year = f"{stack.capacity_kwh_per_year:,.0f}".replace(",", "\u202f")   # 4 000
            cap_txt = (f'Daarin zit ≈ {_e(nl.num(stack.capacity_surcharge_c_per_kwh))} c€/kWh capaciteitstarief: '
                       f'+{_e(nl.num(stack.capacity_extra_peak_kw))} kW maandpiek door het laden × '
                       f'{_e(nl.num(stack.capacity_tariff_eur_per_kw_year, 2))} €/kW/jaar, gespreid over '
                       f'{kwh_year} kWh/jaar')
        else:
            cap_txt = f'Daarin zit ≈ {_e(nl.num(stack.capacity_surcharge_c_per_kwh))} c€/kWh capaciteitstarief'
        if stack.charger_power_kw:
            cap_txt += f' (laden begrensd op {_e(nl.num(stack.charger_power_kw))} kW)'
        cap_txt += '. '
    else:
        cap_txt = 'Het capaciteitstarief (maandpiek) zit er niet in — laad traag genoeg om je piek niet op te drijven. '
    inner = (
        _label("🏷️", f"CREG-tarief {c.quarter_label()} · {t.region}", C["purple"]) +
        f'<div style="font-size:15px;line-height:22px;margin-top:6px;">'
        f'Je werkgever betaalt maximaal <strong>{_e(nl.num(c.tariff_c, 2))}&nbsp;c€/kWh</strong> terug (all-in{based}). '
        f'Met jouw dynamisch contract is dat gelijk aan een day-aheadprijs van '
        f'<strong style="color:{C["purple"]};white-space:nowrap;">≈ {c.break_even_eur_mwh:.0f} {UNIT}</strong> — '
        f'de paarse lijn op de grafiek. Daaronder hou je geld over, daarboven leg je bij.</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:10px;font-family:{FONT};">'
        f'<tr><td style="font-size:11px;line-height:16px;color:{C["faint"]};text-transform:uppercase;letter-spacing:0.5px;">Venster</td>'
        f'<td align="right" style="padding-left:8px;font-size:11px;line-height:16px;color:{C["faint"]};text-transform:uppercase;letter-spacing:0.5px;white-space:nowrap;">Jouw kost</td>'
        f'<td align="right" style="padding-left:12px;font-size:11px;line-height:16px;color:{C["faint"]};text-transform:uppercase;letter-spacing:0.5px;white-space:nowrap;">vs CREG</td></tr>'
        f'{rows}</table>'
        f'<div style="font-size:12px;line-height:18px;color:{C["muted"]};margin-top:10px;">'
        f'Kost per kWh = ENGIE-formule ({_e(nl.num(stack.energy_a, 4))} + {_e(nl.num(stack.energy_b, 4))} × spot, excl. btw) + '
        f'{_e(nl.num(c.stack.fixed_incl_vat, 2))} c€/kWh nettarief ({_e(stack.distribution_operator.title())}), accijns, '
        f'groenestroombijdragen en capaciteitstarief, incl. 6 % btw. {cap_txt}'
        f'Vaste jaarkosten tellen niet mee. Het CREG-tarief loopt 3 tot 8 maanden achter op de markt.</div>'
    )
    return _card(inner, bg=C["purple_bg"], accent=C["purple"])


def _forecast(bundle: DailyBundle) -> str:
    if bundle.forecast is not None and not bundle.forecast.empty:
        f = bundle.forecast
        body = (f'<div style="font-size:15px;line-height:22px;margin-top:6px;">'
                f'<strong>{_e(nl.day_long(f.start).capitalize())}</strong>: gemiddeld '
                f'<strong>{_e(nl.num(f.mean()))}&nbsp;{UNIT}</strong> volgens het voorspellingsmodel.</div>'
                f'<div style="font-size:13px;line-height:19px;color:{C["muted"]};margin-top:4px;">'
                f'Dit is een schatting, geen marktprijs, en wordt niet gebruikt voor de aanbeveling. '
                f'De echte prijzen voor die dag verschijnen morgenmiddag.</div>')
    else:
        body = (f'<div style="font-size:15px;line-height:22px;margin-top:6px;color:{C["muted"]};">'
                f'De voorspelling voor overmorgen is vandaag niet beschikbaar.</div>')
    return _card(_label("🔮", "Overmorgen · enkel voorspelling", C["orange"]) + body,
                 bg=C["orange_bg"], accent=C["orange"])


def _warnings(bundle: DailyBundle) -> str:
    if not bundle.warnings:
        return ""
    items = "".join(f'<li style="margin:4px 0;">{_e(w)}</li>' for w in bundle.warnings)
    inner = (_label("⚠️", "Opgelet", C["amber"]) +
             f'<ul style="margin:6px 0 0;padding-left:20px;font-size:14px;line-height:21px;">{items}</ul>')
    return _card(inner, bg=C["amber_bg"], accent=C["amber"])


def _chart(cid: str, alt: str, with_creg: bool) -> str:
    creg_txt = "paars = break-even t.o.v. het CREG-tarief, " if with_creg else ""
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:separate;border-radius:12px;background-color:{C["card"]};" bgcolor="{C["card"]}">'
        f'<tr><td style="padding:10px;">'
        f'<img src="cid:{cid}" alt="{_e(alt)}" width="532" '
        f'style="display:block;width:100%;max-width:532px;height:auto;border:0;border-radius:8px;">'
        f'</td></tr>'
        f'<tr><td style="padding:0 12px 12px;font-family:{FONT};font-size:12px;line-height:18px;color:{C["faint"]};">'
        f'Blauw = gepubliceerde prijzen, groen = aanbevolen venster(s), {creg_txt}oranje = voorspelling. '
        f'Tik op de grafiek om in te zoomen; ze zit ook als PNG-bijlage bij deze mail.</td></tr></table>'
    )


def _footer(bundle: DailyBundle, tz) -> str:
    return (
        f'<div style="font-family:{FONT};font-size:12px;line-height:18px;color:{C["faint"]};">'
        f'<span style="font-size:14px;">ℹ️</span>&nbsp; Ruwe groothandelsprijzen day-ahead ({UNIT}), '
        f'<strong>exclusief taksen en nettarieven</strong> · Belgische biedzone · '
        f'opgehaald op {_e(_fetched_txt(bundle, tz))}.<br>'
        f'{_e(energy_charts.ATTRIBUTION)}<br>{_e(epex_predictor.ATTRIBUTION)}</div>'
    )


def body_html(bundle: DailyBundle, recs: list[Recommendation], tz, cid: str) -> str:
    today: date = bundle.run_date
    title = _e(headline(bundle, recs))
    if recs:
        preheader = (f"Beste venster: {nl.when(recs[0].start, today, short=True)} {span(recs[0])} · "
                     f"gem. {nl.num(recs[0].avg_price)} {UNIT}")
        if bundle.creg and bundle.creg.best:
            preheader += f" · {_margin_txt(bundle.creg.best.margin_c_per_kwh)}"
    else:
        preheader = "Geen laadvenster kon aanbevolen worden — bekijk de grafiek."
    subtitle = f"{nl.day_long(today, year=True)} · prijzen van vandaag en morgen"
    if not bundle.tomorrow_published:
        subtitle = f"{nl.day_long(today, year=True)} · prijzen van vandaag (morgen nog niet gepubliceerd)"

    header = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:separate;border-radius:14px 14px 0 0;background-color:{C["navy"]};" bgcolor="{C["navy"]}">'
        f'<tr><td style="padding:22px 24px 20px;font-family:{FONT};">'
        f'<div style="font-size:20px;line-height:26px;font-weight:800;color:#ffffff;">'
        f'<span style="font-size:22px;">🔌</span>&nbsp; EV-laadplanner</div>'
        f'<div style="font-size:13px;line-height:19px;color:{C["navy_text"]};margin-top:4px;">{_e(subtitle.capitalize())}</div>'
        f'</td></tr></table>'
    )

    sections = [
        (_hero(bundle, recs), False),
        (_alternative(bundle, recs), False),
        (_stats(bundle), True),
        (_creg(bundle), False),
        (_forecast(bundle), False),
        (_warnings(bundle), False),
        (_chart(cid, f"Grafiek van de kwartierprijzen ({UNIT}) met het aanbevolen laadvenster", bundle.creg is not None), False),
        (_footer(bundle, tz), False),
    ]
    body_rows = "".join(_row(s, columns) + _spacer(12) for s, columns in sections if s)

    return f"""<!DOCTYPE html>
<html lang="nl-BE" xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<meta name="x-apple-disable-message-reformatting">
<meta name="format-detection" content="telephone=no,date=no,address=no,email=no">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{title}</title>
<!--[if mso]>
<xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml>
<style>table,td,div,span,p{{font-family:Arial,sans-serif !important;}}</style>
<![endif]-->
<style>
  html,body{{margin:0 !important;padding:0 !important;height:100% !important;width:100% !important;}}
  *{{-ms-text-size-adjust:100%;-webkit-text-size-adjust:100%;}}
  table,td{{mso-table-lspace:0pt !important;mso-table-rspace:0pt !important;}}
  img{{-ms-interpolation-mode:bicubic;border:0;line-height:100%;outline:none;text-decoration:none;}}
  a{{color:{C["blue"]};}}
  @media only screen and (max-width:620px){{
    .container{{width:100% !important;max-width:100% !important;}}
    .outer{{padding:12px 8px !important;}}
    .pad{{padding-left:12px !important;padding-right:12px !important;}}
    .pad-cols{{padding-left:8px !important;padding-right:8px !important;}}
    .hero-time{{font-size:30px !important;line-height:36px !important;}}
    .col{{max-width:100% !important;}}
  }}
</style>
</head>
<body style="margin:0;padding:0;background-color:{C["bg"]};" bgcolor="{C["bg"]}">
<div style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;color:{C["bg"]};">{_e(preheader)}&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{C["bg"]};" bgcolor="{C["bg"]}">
<tr><td class="outer" align="center" style="padding:24px 12px;">
<!--[if mso]><table role="presentation" width="600" align="center" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->
<table role="presentation" class="container" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;margin:0 auto;border-collapse:separate;border-radius:14px;background-color:{C["card"]};" bgcolor="{C["card"]}">
<tr><td style="padding:0;">{header}</td></tr>
{_spacer(20)}
{body_rows}
{_spacer(8)}
</table>
<!--[if mso]></td></tr></table><![endif]-->
<div style="font-family:{FONT};font-size:11px;line-height:16px;color:{C["faint"]};padding:14px 8px 0;text-align:center;">
Automatisch verstuurd door de EV-laadplanner · tijden in Europe/Brussels
</div>
</td></tr>
</table>
</body>
</html>"""


# --------------------------------------------------------------------------- message assembly

def build_message(cfg: Config, bundle: DailyBundle, recs: list[Recommendation], png: bytes) -> EmailMessage:
    tz = cfg.tz
    msg = EmailMessage()
    msg["Subject"] = subject_line(bundle, recs)
    msg["From"] = cfg.secrets.gmail_user or "evprice@localhost"
    msg["To"] = ", ".join(cfg.recipients)
    msg.set_content(body_text(bundle, recs, tz))

    cid = make_msgid(domain="evprice")
    msg.add_alternative(body_html(bundle, recs, tz, cid[1:-1]), subtype="html")
    msg.get_payload()[1].add_related(png, maintype="image", subtype="png", cid=cid)
    msg.add_attachment(png, maintype="image", subtype="png",
                       filename=f"ev-prijzen-{bundle.run_date:%Y-%m-%d}.png")
    return msg


# --------------------------------------------------------------------------- delivery

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
            log.info("email sent to %s", ", ".join(cfg.recipients))
            return
        except smtplib.SMTPAuthenticationError as exc:
            raise MailError(f"SMTP authentication failed: {exc}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            last = exc
            log.warning("send attempt %d/%d failed: %s", attempt, retries, exc)
            if attempt < retries:
                sleep(10 * attempt)
    raise MailError(f"email could not be sent after {retries} attempts: {last}")

"""Render the daily PNG. Actual prices solid, forecast dashed inside a shaded region,
recommended windows highlighted, two reference averages, attribution footer.

All text is Dutch (BE). The figure is sized for an email that is ~600px wide on desktop and
~360px on a phone: a 12x7 inch canvas with generous font sizes keeps labels legible after
downscaling, and the PNG is also attached at full size.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from . import nl  # noqa: E402
from .models import UNIT, DailyBundle, Recommendation  # noqa: E402
from .sources import energy_charts, epex_predictor  # noqa: E402
from .timeutil import SLOT, day_bounds  # noqa: E402

# Palette (validated light-mode categorical slots + neutrals) — shared with mailer.py
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#e6e5e1"
ACTUAL = "#2a78d6"       # slot 1 blue
FORECAST = "#eb6834"     # slot 2 orange
FORECAST_BAND = "#fbe3d8"
FORECAST_REGION = "#f4f1ea"
WINDOW = "#1baf7a"       # slot 3 aqua
WINDOW_FILL = "#d2f0e4"
AVG_PERIOD = "#52514e"
AVG_HIST = "#9a9893"

FORECAST_MAE = 20.0      # EUR/MWh ≈ 2 ct/kWh, author's reported 2-day MAE for BE

# Font sizes (points). The figure is downscaled ~3x in a desktop email, ~5x on a phone.
FS_TITLE, FS_LABEL, FS_TICK, FS_LEGEND, FS_NOTE, FS_FOOTER = 15, 12, 11, 11, 11, 9


def _step_xy(series: pd.Series):
    """Return x,y arrays for a step plot that extends the final slot to its end."""
    x = list(series.index) + [series.index[-1] + SLOT]
    y = list(series.values) + [series.values[-1]]
    return x, y


def _span(r: Recommendation) -> str:
    return f"{nl.clock(r.start)}–{nl.clock_end(r.start, r.end)}"


def _window_txt(r: Recommendation) -> str:
    return f"{nl.day_short(r.start)} {_span(r)} (gem. {nl.num(r.avg_price)} {UNIT})"


def render_png(bundle: DailyBundle, recs: list[Recommendation], tz, generated_at: datetime | None = None,
               horizon_end: pd.Timestamp | None = None, dpi: int = 150) -> bytes:
    generated_at = generated_at or datetime.now(timezone.utc)
    actual = bundle.actual.series
    forecast = bundle.forecast.series if bundle.forecast is not None and not bundle.forecast.empty else None

    fig, ax = plt.subplots(figsize=(12, 7), dpi=dpi)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    # --- forecast region (day+2), shaded whether or not we have data for it
    day2 = bundle.run_date + pd.Timedelta(days=2)
    d2_start, d2_end = day_bounds(day2.date() if hasattr(day2, "date") else day2, tz)
    x_end = horizon_end or d2_end
    ax.axvspan(d2_start, x_end, color=FORECAST_REGION, zorder=0, lw=0)

    # --- recommended windows
    for i, r in enumerate(recs):
        ax.axvspan(r.start, r.end, color=WINDOW_FILL, zorder=1, lw=0, alpha=1.0 if i == 0 else 0.6)
        ax.axvline(r.start, color=WINDOW, lw=1.8 if i == 0 else 1.2, zorder=2)
        ax.axvline(r.end, color=WINDOW, lw=1.8 if i == 0 else 1.2, zorder=2)
        tag = "BESTE" if i == 0 else "alternatief"
        ax.annotate(f"{tag}\n{_span(r)}\n{r.avg_price:.0f} {UNIT}",
                    xy=((r.start + (r.end - r.start) / 2), 1.0), xycoords=("data", "axes fraction"),
                    xytext=(0, -6), textcoords="offset points", ha="center", va="top",
                    fontsize=FS_NOTE, color=TEXT, fontweight="bold" if i == 0 else "normal", zorder=6)

    # --- reference lines
    ax.axhline(bundle.period_average, color=AVG_PERIOD, lw=1.2, ls="--", zorder=3,
               label=f"Gemiddelde getoonde periode: {nl.num(bundle.period_average)} {UNIT}")
    ax.axhline(bundle.history_average, color=AVG_HIST, lw=1.2, ls=":", zorder=3,
               label=f"Gemiddelde vorige 30 dagen: {nl.num(bundle.history_average)} {UNIT}")

    # --- published prices
    x, y = _step_xy(actual)
    ax.step(x, y, where="post", color=ACTUAL, lw=2.2, zorder=5,
            label="Gepubliceerde day-aheadprijs (vandaag + morgen)")

    # --- forecast
    if forecast is not None:
        fx, fy = _step_xy(forecast)
        ax.fill_between(fx, [v - FORECAST_MAE for v in fy], [v + FORECAST_MAE for v in fy],
                        step="post", color=FORECAST_BAND, zorder=3, lw=0)
        ax.step(fx, fy, where="post", color=FORECAST, lw=2.2, ls="--", zorder=5,
                label=f"Voorspelling {nl.day_short(d2_start)} — schatting, geen marktprijs "
                      f"(typische fout ±{FORECAST_MAE:.0f})")
    else:
        ax.text(d2_start + (x_end - d2_start) / 2, 0.5, "Voorspelling niet beschikbaar",
                transform=ax.get_xaxis_transform(), ha="center", va="center", fontsize=14, color=TEXT_2, zorder=6)

    # --- axes
    ax.set_xlim(actual.index[0], x_end)
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3), tz=tz))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))
    ax.tick_params(axis="x", labelsize=FS_TICK, colors=TEXT_2, length=0)
    ax.tick_params(axis="y", labelsize=FS_TICK, colors=TEXT_2, length=0)
    ax.set_ylabel(UNIT, color=TEXT_2, fontsize=FS_LABEL)
    ax.grid(True, axis="y", color=GRID, lw=0.8, zorder=0)
    ax.grid(False, axis="x")
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)

    # day boundaries + day labels
    first_day = actual.index[0].date()
    for n in range(0, 3):
        day = first_day + pd.Timedelta(days=n)
        s, e = day_bounds(day, tz)
        if n > 0:
            ax.axvline(s, color=TEXT_2, lw=0.8, zorder=2)
        label_x = max(s, actual.index[0]) + (min(e, x_end) - max(s, actual.index[0])) / 2
        rel = nl.relative_day(day, bundle.run_date)
        label = f"{rel} · {nl.day_long(s)}" if rel else nl.day_long(s)
        if n == 2:
            label += " (voorspelling)"
        ax.text(label_x, -0.11, label, transform=ax.get_xaxis_transform(),
                ha="center", va="top", fontsize=FS_LABEL, color=TEXT, fontweight="bold")

    ax.axhline(0, color=GRID, lw=1, zorder=1)

    # --- title & legend
    if not recs:
        ax.set_title("Geen laadvenster kon aanbevolen worden", loc="left", fontsize=FS_TITLE, color=TEXT,
                     pad=16, fontweight="bold")
    else:
        ax.set_title(f"Goedkoopste {recs[0].duration_hours} u om te laden: {_window_txt(recs[0])}",
                     loc="left", fontsize=FS_TITLE, color=TEXT, pad=30 if len(recs) > 1 else 16, fontweight="bold")
        if len(recs) > 1:   # second title line, lighter, so a long pair never runs off the canvas
            ax.text(0.0, 1.03, f"alternatief: {_window_txt(recs[1])}", transform=ax.transAxes,
                    ha="left", va="bottom", fontsize=FS_LABEL, color=TEXT_2)
    ax.legend(loc="upper left", bbox_to_anchor=(0.0, -0.19), ncol=2, frameon=False, fontsize=FS_LEGEND,
              labelcolor=TEXT_2)

    # --- footer
    fetched = bundle.actual.fetched_at.astimezone(tz)
    made = generated_at.astimezone(tz)
    lines = [
        f"Belgische biedzone · tijden in {tz.key} · aangemaakt {nl.day_short(made)} {nl.clock(made)} {made:%Z} · "
        f"gepubliceerde prijzen opgehaald om {nl.clock(fetched)} {fetched:%Z}",
        energy_charts.ATTRIBUTION + " · " + epex_predictor.ATTRIBUTION,
    ]
    if bundle.warnings:
        lines.append("⚠ " + " · ".join(bundle.warnings))
    fig.text(0.01, 0.01, "\n".join(lines), fontsize=FS_FOOTER, color=TEXT_2, va="bottom", ha="left")

    fig.subplots_adjust(left=0.065, right=0.99, top=0.9, bottom=0.31)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()

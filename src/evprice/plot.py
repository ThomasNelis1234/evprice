"""Render the daily PNG. Actual prices solid, forecast dashed inside a shaded region,
recommended windows highlighted, two reference averages, attribution footer."""

from __future__ import annotations

import io
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from .models import UNIT, DailyBundle, Recommendation  # noqa: E402
from .sources import energy_charts, epex_predictor  # noqa: E402
from .timeutil import SLOT, day_bounds  # noqa: E402

# Palette (validated light-mode categorical slots + neutrals)
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


def _step_xy(series: pd.Series):
    """Return x,y arrays for a step plot that extends the final slot to its end."""
    x = list(series.index) + [series.index[-1] + SLOT]
    y = list(series.values) + [series.values[-1]]
    return x, y


def render_png(bundle: DailyBundle, recs: list[Recommendation], tz, generated_at: datetime | None = None,
               horizon_end: pd.Timestamp | None = None, dpi: int = 110) -> bytes:
    generated_at = generated_at or datetime.now(timezone.utc)
    actual = bundle.actual.series
    forecast = bundle.forecast.series if bundle.forecast is not None and not bundle.forecast.empty else None

    fig, ax = plt.subplots(figsize=(15, 7.2), dpi=dpi)
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
        ax.axvline(r.start, color=WINDOW, lw=1.5 if i == 0 else 1, zorder=2)
        ax.axvline(r.end, color=WINDOW, lw=1.5 if i == 0 else 1, zorder=2)
        tag = "BEST" if i == 0 else "alternative"
        ax.annotate(f"{tag}\n{r.start:%H:%M}–{r.end:%H:%M}\n{r.avg_price:.0f} {UNIT}",
                    xy=((r.start + (r.end - r.start) / 2), 1.0), xycoords=("data", "axes fraction"),
                    xytext=(0, -6), textcoords="offset points", ha="center", va="top",
                    fontsize=9, color=TEXT, zorder=6)

    # --- reference lines
    ax.axhline(bundle.period_average, color=AVG_PERIOD, lw=1.2, ls="--", zorder=3,
               label=f"Avg of shown published period: {bundle.period_average:.1f} {UNIT}")
    ax.axhline(bundle.history_average, color=AVG_HIST, lw=1.2, ls=":", zorder=3,
               label=f"30-day average: {bundle.history_average:.1f} {UNIT}")

    # --- published prices
    x, y = _step_xy(actual)
    ax.step(x, y, where="post", color=ACTUAL, lw=2, zorder=5, label="Published day-ahead price (today + tomorrow)")

    # --- forecast
    if forecast is not None:
        fx, fy = _step_xy(forecast)
        ax.fill_between(fx, [v - FORECAST_MAE for v in fy], [v + FORECAST_MAE for v in fy],
                        step="post", color=FORECAST_BAND, zorder=3, lw=0)
        ax.step(fx, fy, where="post", color=FORECAST, lw=2, ls="--", zorder=5,
                label=f"Forecast for {d2_start:%a %d %b} — estimate, not a market price (±{FORECAST_MAE:.0f} typical error)")
    else:
        ax.text(d2_start + (x_end - d2_start) / 2, 0.5, "Forecast unavailable", transform=ax.get_xaxis_transform(),
                ha="center", va="center", fontsize=13, color=TEXT_2, zorder=6)

    # --- axes
    ax.set_xlim(actual.index[0], x_end)
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3), tz=tz))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))
    ax.tick_params(axis="x", labelsize=9, colors=TEXT_2, length=0)
    ax.tick_params(axis="y", labelsize=9, colors=TEXT_2, length=0)
    ax.set_ylabel(UNIT, color=TEXT_2, fontsize=10)
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
        tag = " (forecast)" if n == 2 else ""
        ax.text(label_x, -0.1, f"{s:%A %d %B}{tag}", transform=ax.get_xaxis_transform(),
                ha="center", va="top", fontsize=10, color=TEXT, fontweight="bold")

    ax.axhline(0, color=GRID, lw=1, zorder=1)

    # --- title & legend
    headline = "No published window could be recommended" if not recs else \
        f"Cheapest {recs[0].duration_hours}h to charge: {recs[0]}" + \
        (f"   ·   alternative: {recs[1]}" if len(recs) > 1 else "")
    ax.set_title(headline, loc="left", fontsize=13, color=TEXT, pad=14, fontweight="bold")
    ax.legend(loc="upper left", bbox_to_anchor=(0.0, -0.17), ncol=2, frameon=False, fontsize=9, labelcolor=TEXT_2)

    # --- footer
    fetched = bundle.actual.fetched_at.astimezone(tz)
    lines = [
        f"Belgian bidding zone · times in {tz.key} · generated {generated_at.astimezone(tz):%Y-%m-%d %H:%M %Z} · "
        f"published prices fetched {fetched:%H:%M %Z}",
        energy_charts.ATTRIBUTION + " · " + epex_predictor.ATTRIBUTION,
    ]
    if bundle.warnings:
        lines.append("⚠ " + " · ".join(bundle.warnings))
    fig.text(0.01, 0.01, "\n".join(lines), fontsize=8, color=TEXT_2, va="bottom", ha="left")

    fig.subplots_adjust(left=0.05, right=0.99, top=0.9, bottom=0.3)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()

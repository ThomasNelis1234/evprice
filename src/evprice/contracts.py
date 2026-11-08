"""Comparison with other supplier contracts (ENGIE Basic Online, Empower Flextime, …).

The user has a *dynamic* contract: every quarter-hour costs the day-ahead price through the formula in
``cost_stack``. A variable contract instead charges one price for the whole month, indexed on EPEXDAM —
the arithmetic mean of the daily day-ahead quotes of the delivery month. A time-of-use contract
(Empower Flextime) has a few such prices, one per period of the day.

Everything that is not energy (distribution, capacity tariff, levies, btw) is identical across these
contracts, so only the energy part is compared:

    dynamic energy(P)       = (a_dyn + b_dyn × P)        c€/kWh excl. btw, P = day-ahead price (EUR/MWh)
    contract energy(period) = a_c + b_c × EPEXDAM         c€/kWh excl. btw, constant within the period

Setting them equal gives the day-ahead price at which the dynamic contract costs exactly the same as
the other contract — the "break-even" lines on the chart. Below the line, dynamic is cheaper.

EPEXDAM for the running month is not known until the month ends; it is estimated from the published
daily means so far (falling back to the sheet's reference value when the month has barely started).
Fixed yearly fees are reported but not folded into the per-kWh figures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from .config import ContractConfig, ContractsConfig, CostStackConfig
from .creg import CostStack
from .models import ActualPrices, Recommendation
from .timeutil import expected_index

PERIOD_LABELS_NL = {"piek": "piekuren", "dal": "daluren", "superdal": "superdaluren", "enkel": "enkelvoudig"}


# --------------------------------------------------------------------------- EPEXDAM estimate

@dataclass(frozen=True)
class Epexdam:
    value_eur_mwh: float
    days_used: int                 # published days of the running month that went into the estimate
    month: date                    # first day of the month the value refers to
    estimated: bool                # True = from this month's prices; False = reference value from the sheet
    reference_month: str = ""

    def describe(self) -> str:
        from . import nl
        if self.estimated:
            return (f"geschat op {nl.num(self.value_eur_mwh)} EUR/MWh uit {self.days_used} gepubliceerde "
                    f"dag{'en' if self.days_used != 1 else ''} van {nl.month(self.month)}")
        ref = f" ({self.reference_month})" if self.reference_month else ""
        return f"referentiewaarde van de tariefkaart {nl.num(self.value_eur_mwh)} EUR/MWh{ref}"


def estimate_epexdam(cfg: ContractsConfig, history: ActualPrices, actual: ActualPrices, run_date: date) -> Epexdam:
    """Mean of the daily means of every published day in ``run_date``'s month (history + today/tomorrow)."""
    series = pd.concat([history.series, actual.series]).sort_index()
    series = series[~series.index.duplicated(keep="last")]
    local_days = series.index.tz_convert(history.series.index.tz).normalize()
    in_month = (local_days.year == run_date.year) & (local_days.month == run_date.month)
    daily = series[in_month].groupby(local_days[in_month]).mean()
    first = run_date.replace(day=1)
    if len(daily) >= cfg.epexdam_min_days:
        return Epexdam(float(daily.mean()), len(daily), first, True)
    return Epexdam(cfg.epexdam_reference_eur_mwh, len(daily), first, False, cfg.epexdam_reference_month)


# --------------------------------------------------------------------------- per-slot model

def period_at(contract: ContractConfig, ts: pd.Timestamp) -> str:
    """Which tariff period a local timestamp falls in."""
    if contract.kind == "flat" or not contract.schedule:
        return contract.default_period
    day_kind = "weekend" if ts.weekday() >= 5 else "weekday"
    hour = ts.hour + ts.minute / 60
    for period, windows in contract.schedule.get(day_kind, {}).items():
        for start, end in windows:
            if start <= hour < end:
                return period
    return contract.default_period


def energy_excl_vat_series(contract: ContractConfig, index: pd.DatetimeIndex, epexdam: float) -> pd.Series:
    """Contract energy price per slot, c€/kWh excl. btw."""
    values = [contract.periods[period_at(contract, ts)](epexdam) for ts in index]
    return pd.Series(values, index=index, dtype=float)


def break_even_series(contract: ContractConfig, stack: CostStack, index: pd.DatetimeIndex, epexdam: float) -> pd.Series:
    """Day-ahead price (EUR/MWh) per slot at which the dynamic contract costs the same as ``contract``."""
    c = stack.cfg
    return (energy_excl_vat_series(contract, index, epexdam) - c.energy_a) / c.energy_b


# --------------------------------------------------------------------------- comparison

@dataclass(frozen=True)
class ContractCost:
    contract: ContractConfig
    best_all_in_c_per_kwh: float | None     # what the best window would cost on this contract (None: no window)
    best_periods: tuple[str, ...]           # tariff periods the best window touches
    period_all_in_c_per_kwh: float          # average kWh over the whole shown period on this contract
    break_even: pd.Series                   # per slot of the shown horizon, EUR/MWh

    @property
    def flat_break_even(self) -> float | None:
        """One number when the break-even does not vary (flat contract), else None."""
        return float(self.break_even.iloc[0]) if self.break_even.nunique() == 1 else None


@dataclass(frozen=True)
class ContractsComparison:
    epexdam: Epexdam
    dynamic_best_c_per_kwh: float | None    # the user's own cost in the best window, all-in
    dynamic_period_c_per_kwh: float         # the user's own cost for an average kWh of the shown period
    items: list[ContractCost]
    charge_kwh: float | None                # kWh in one charging session (duration × charger power), if known

    def margin_best(self, item: ContractCost) -> float | None:
        """Contract minus dynamic in the best window (c€/kWh). Positive = dynamic is cheaper."""
        if item.best_all_in_c_per_kwh is None or self.dynamic_best_c_per_kwh is None:
            return None
        return item.best_all_in_c_per_kwh - self.dynamic_best_c_per_kwh

    def margin_period(self, item: ContractCost) -> float:
        return item.period_all_in_c_per_kwh - self.dynamic_period_c_per_kwh


def compare(cfg: ContractsConfig, stack_cfg: CostStackConfig, actual: ActualPrices, history: ActualPrices,
            recs: list[Recommendation], run_date: date, charging_duration_hours: int,
            horizon_end: pd.Timestamp | None = None) -> ContractsComparison:
    """``horizon_end`` (exclusive) extends the break-even profile past the published slots, over the
    forecast day on the chart; costs are always computed on the published slots only."""
    stack = CostStack(stack_cfg)
    epexdam = estimate_epexdam(cfg, history, actual, run_date)
    vat = 1 + stack_cfg.vat
    index = actual.series.index
    chart_index = expected_index(index[0], horizon_end) if horizon_end is not None and horizon_end > index[-1] else index
    best = recs[0] if recs else None

    dyn_best = stack.all_in(best.avg_price) if best else None
    dyn_period = stack.all_in(actual.mean())

    items: list[ContractCost] = []
    for contract in cfg.items:
        energy = energy_excl_vat_series(contract, index, epexdam.value_eur_mwh)
        all_in = energy * vat + stack.fixed_incl_vat
        if best is not None:
            window = all_in.loc[best.start:best.end - pd.Timedelta(minutes=1)]
            best_cost = float(window.mean()) if not window.empty else None
            touched = tuple(dict.fromkeys(period_at(contract, ts) for ts in window.index))
        else:
            best_cost, touched = None, ()
        items.append(ContractCost(
            contract=contract,
            best_all_in_c_per_kwh=best_cost,
            best_periods=touched,
            period_all_in_c_per_kwh=float(all_in.mean()),
            break_even=break_even_series(contract, stack, chart_index, epexdam.value_eur_mwh),
        ))

    kwh = charging_duration_hours * stack_cfg.charger_power_kw if stack_cfg.charger_power_kw else None
    return ContractsComparison(epexdam, dyn_best, dyn_period, items, kwh)

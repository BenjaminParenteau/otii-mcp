"""Battery life and brownout estimation from measured current.

A battery is modelled per cell by an open-circuit voltage curve over state of
charge (SoC), an internal resistance that may rise as it empties, a capacity,
a self-discharge rate and a recommended maximum pulse current. Cells combine
in series (voltage and resistance add) and in parallel (capacity adds,
resistance divides).

The device stops working when the battery's terminal voltage under load,
open-circuit voltage minus current x internal resistance, falls below the
device's minimum voltage. Checking this against the recorded peak current
shows the "dies early under radio bursts" case, not just running out of
charge.

The built-in models use typical values from public datasheets for
low-drain use. Real batteries vary with brand, temperature, age and load
pattern, so pass your own figures when accuracy matters.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, replace
from typing import Any

HOURS_PER_YEAR = 8766.0
SOC_STEP = 0.001
REPORT_SOCS = (1.0, 0.9, 0.75, 0.5, 0.25, 0.1, 0.05)


@dataclass(frozen=True)
class BatteryModel:
    name: str
    description: str
    chemistry: str
    capacity_mah: float  # per cell, at low drain
    # (soc, volts) per cell, soc from 0 (empty) to 1 (full); any order.
    ocv_curve: tuple[tuple[float, float], ...]
    # (soc, ohms) per cell.
    resistance_curve: tuple[tuple[float, float], ...]
    end_voltage_v: float  # per cell: the battery is empty at this open-circuit voltage
    self_discharge_per_year: float  # fraction of capacity lost per year at rest
    max_pulse_a: float  # per cell, recommended maximum pulse current
    series: int = 1
    parallel: int = 1

    def ocv(self, soc: float) -> float:
        return self.series * _interpolate(self.ocv_curve, soc)

    def resistance(self, soc: float) -> float:
        return self.series * _interpolate(self.resistance_curve, soc) / self.parallel

    @property
    def total_capacity_mah(self) -> float:
        return self.capacity_mah * self.parallel

    @property
    def pack_end_voltage_v(self) -> float:
        return self.end_voltage_v * self.series

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "chemistry": self.chemistry,
            "cells_series": self.series,
            "cells_parallel": self.parallel,
            "capacity_mah": self.total_capacity_mah,
            "nominal_voltage_v": self.ocv(0.5),
            "full_voltage_v": self.ocv(1.0),
            "internal_resistance_ohm": {"full": self.resistance(1.0), "half": self.resistance(0.5), "empty": self.resistance(0.0)},
            "self_discharge_per_year": self.self_discharge_per_year,
            "max_pulse_a": self.max_pulse_a * self.parallel,
        }


def _interpolate(curve: tuple[tuple[float, float], ...], soc: float) -> float:
    points = sorted(curve)
    socs = [p[0] for p in points]
    soc = min(max(soc, socs[0]), socs[-1])
    i = bisect.bisect_left(socs, soc)
    if i == 0:
        return points[0][1]
    (s0, v0), (s1, v1) = points[i - 1], points[i]
    if s1 == s0:
        return v1
    return v0 + (v1 - v0) * (soc - s0) / (s1 - s0)


# Typical low-drain figures from public datasheets (e.g. Energizer, Panasonic,
# Tadiran, Samsung SDI, A123). Starting points, not substitutes for your cell's datasheet.
MODELS: dict[str, BatteryModel] = {
    m.name: m
    for m in (
        BatteryModel(
            "cr2032",
            "Lithium coin cell, 3 V, ~225 mAh. High internal resistance: bursts above ~15 mA sag it hard.",
            "Li-MnO2",
            225,
            ((1.0, 3.20), (0.95, 3.02), (0.8, 2.97), (0.5, 2.93), (0.25, 2.85), (0.1, 2.70), (0.05, 2.50), (0.0, 2.00)),
            ((1.0, 15.0), (0.5, 20.0), (0.25, 28.0), (0.1, 40.0), (0.0, 60.0)),
            end_voltage_v=2.0,
            self_discharge_per_year=0.01,
            max_pulse_a=0.015,
        ),
        BatteryModel(
            "alkaline-aa",
            "Alkaline AA, 1.5 V, ~2500 mAh at low drain (less at high currents).",
            "Zn-MnO2",
            2500,
            ((1.0, 1.58), (0.9, 1.45), (0.75, 1.36), (0.5, 1.26), (0.25, 1.16), (0.1, 1.06), (0.0, 0.90)),
            ((1.0, 0.15), (0.5, 0.25), (0.25, 0.40), (0.0, 0.70)),
            end_voltage_v=0.9,
            self_discharge_per_year=0.02,
            max_pulse_a=2.0,
        ),
        BatteryModel(
            "alkaline-aaa",
            "Alkaline AAA, 1.5 V, ~1100 mAh at low drain.",
            "Zn-MnO2",
            1100,
            ((1.0, 1.58), (0.9, 1.45), (0.75, 1.36), (0.5, 1.26), (0.25, 1.16), (0.1, 1.06), (0.0, 0.90)),
            ((1.0, 0.25), (0.5, 0.35), (0.25, 0.55), (0.0, 0.90)),
            end_voltage_v=0.9,
            self_discharge_per_year=0.02,
            max_pulse_a=1.0,
        ),
        BatteryModel(
            "li-ion-18650",
            "Li-ion 18650, 3.7 V nominal, 4.2 V full, ~2600 mAh.",
            "Li-ion (NMC)",
            2600,
            (
                (1.0, 4.20), (0.9, 4.06), (0.8, 3.98), (0.7, 3.92), (0.6, 3.87), (0.5, 3.82),
                (0.4, 3.79), (0.3, 3.77), (0.2, 3.72), (0.1, 3.68), (0.05, 3.55), (0.0, 3.00),
            ),
            ((1.0, 0.05), (0.5, 0.05), (0.2, 0.06), (0.0, 0.10)),
            end_voltage_v=3.0,
            self_discharge_per_year=0.25,
            max_pulse_a=5.0,
        ),
        BatteryModel(
            "lipo",
            "LiPo pouch cell, 3.7 V nominal, 4.2 V full. Capacity varies widely: pass capacity_mah.",
            "Li-ion (LiCoO2)",
            1000,
            (
                (1.0, 4.20), (0.9, 4.06), (0.8, 3.98), (0.7, 3.92), (0.6, 3.87), (0.5, 3.82),
                (0.4, 3.79), (0.3, 3.77), (0.2, 3.72), (0.1, 3.68), (0.05, 3.55), (0.0, 3.00),
            ),
            ((1.0, 0.10), (0.5, 0.10), (0.2, 0.12), (0.0, 0.20)),
            end_voltage_v=3.0,
            self_discharge_per_year=0.25,
            max_pulse_a=2.0,
        ),
        BatteryModel(
            "lifepo4",
            "LiFePO4 18650, 3.2 V nominal, very flat curve, ~1500 mAh.",
            "LiFePO4",
            1500,
            ((1.0, 3.60), (0.95, 3.35), (0.8, 3.32), (0.5, 3.30), (0.2, 3.25), (0.1, 3.20), (0.05, 3.00), (0.0, 2.50)),
            ((1.0, 0.05), (0.5, 0.05), (0.1, 0.07), (0.0, 0.12)),
            end_voltage_v=2.5,
            self_discharge_per_year=0.30,
            max_pulse_a=4.0,
        ),
        BatteryModel(
            "li-socl2-aa",
            "Lithium thionyl chloride AA bobbin cell (e.g. ER14505), 3.6 V, ~2600 mAh, tiny self-discharge, "
            "weak at pulses (pair with a capacitor for radios).",
            "Li-SOCl2",
            2600,
            ((1.0, 3.67), (0.9, 3.60), (0.5, 3.58), (0.2, 3.55), (0.1, 3.45), (0.05, 3.30), (0.0, 2.00)),
            ((1.0, 10.0), (0.5, 12.0), (0.2, 15.0), (0.0, 30.0)),
            end_voltage_v=2.0,
            self_discharge_per_year=0.01,
            max_pulse_a=0.15,
        ),
    )
}


class BatteryError(ValueError):
    """Bad battery parameters."""


def build_model(
    name: str,
    *,
    capacity_mah: float | None = None,
    cells_series: int = 1,
    cells_parallel: int = 1,
    internal_resistance_ohm: float | None = None,
    ocv_curve: list[tuple[float, float]] | None = None,
) -> BatteryModel:
    """A built-in model with optional per-cell overrides."""
    try:
        model = MODELS[name]
    except KeyError:
        raise BatteryError(f"Unknown battery {name!r}; choose one of {', '.join(MODELS)}.") from None
    if cells_series < 1 or cells_parallel < 1:
        raise BatteryError("cells_series and cells_parallel must be at least 1.")
    changes: dict[str, Any] = {"series": cells_series, "parallel": cells_parallel}
    if capacity_mah is not None:
        if capacity_mah <= 0:
            raise BatteryError("capacity_mah must be positive.")
        changes["capacity_mah"] = capacity_mah
    if internal_resistance_ohm is not None:
        if internal_resistance_ohm < 0:
            raise BatteryError("internal_resistance_ohm can't be negative.")
        changes["resistance_curve"] = ((0.0, internal_resistance_ohm), (1.0, internal_resistance_ohm))
    if ocv_curve is not None:
        if len(ocv_curve) < 2 or any(not 0 <= soc <= 1 or volts <= 0 for soc, volts in ocv_curve):
            raise BatteryError("ocv_curve needs at least two [soc, volts] points with soc 0-1 and volts > 0.")
        changes["ocv_curve"] = tuple((float(s), float(v)) for s, v in ocv_curve)
        changes["end_voltage_v"] = min(v for _, v in ocv_curve)
    return replace(model, **changes)


def estimate(
    model: BatteryModel,
    avg_current_a: float,
    peak_current_a: float,
    *,
    worst_peak_a: float | None = None,
    device_min_voltage_v: float | None = None,
) -> dict[str, Any]:
    """Battery life for a load drawing `avg_current_a` with bursts up to `peak_current_a`.

    `peak_current_a` is the burst current to check brownout against (e.g. the
    highest 1 ms average); `worst_peak_a` (e.g. the highest single sample)
    gives a pessimistic second answer, since board capacitors usually ride
    through sub-millisecond spikes.
    """
    if avg_current_a <= 0:
        raise BatteryError("The average current must be positive to estimate a lifetime.")
    peak = max(peak_current_a, avg_current_a)
    cutoff = device_min_voltage_v if device_min_voltage_v is not None else model.pack_end_voltage_v
    cutoff_source = "device_min_voltage_v" if device_min_voltage_v is not None else "battery end voltage"

    def dead_soc(current_a: float) -> float:
        """Highest SoC at which the loaded voltage drops below the cutoff (0 if never)."""
        soc = 1.0
        while soc > 0:
            if model.ocv(soc) - current_a * model.resistance(soc) < cutoff:
                return soc
            soc = round(soc - SOC_STEP, 6)
        return 0.0

    soc_dead_avg = dead_soc(avg_current_a)
    soc_dead_peak = dead_soc(peak)
    soc_end = max(soc_dead_avg, soc_dead_peak)
    usable_fraction = 1.0 - soc_end
    if soc_dead_peak > soc_dead_avg and soc_dead_peak > 0:
        limited_by = "brownout under peak current"
    elif soc_dead_avg > 0:
        limited_by = "voltage under average current"
    else:
        limited_by = "capacity"

    capacity_mah = model.total_capacity_mah
    self_discharge_ma = capacity_mah * model.self_discharge_per_year / HOURS_PER_YEAR
    avg_ma = avg_current_a * 1000
    usable_mah = capacity_mah * usable_fraction
    hours = usable_mah / (avg_ma + self_discharge_ma)

    table = []
    for soc in REPORT_SOCS:
        ocv = model.ocv(soc)
        r = model.resistance(soc)
        table.append(
            {
                "soc": soc,
                "ocv_v": ocv,
                "internal_resistance_ohm": r,
                "under_avg_v": ocv - avg_current_a * r,
                "under_peak_v": ocv - peak * r,
            }
        )

    result: dict[str, Any] = {
        "battery": model.describe(),
        "load": {"avg_current_a": avg_current_a, "peak_current_a": peak},
        "cutoff_v": cutoff,
        "cutoff_source": cutoff_source,
        "limited_by": limited_by,
        "usable_fraction": usable_fraction,
        "usable_capacity_mah": usable_mah,
        "self_discharge_ma": self_discharge_ma,
        "lifetime_hours": hours,
        "lifetime_days": hours / 24,
        "lifetime_years": hours / HOURS_PER_YEAR,
        "voltage_by_soc": table,
    }
    if worst_peak_a is not None and worst_peak_a > peak:
        worst_end = max(soc_dead_avg, dead_soc(worst_peak_a))
        worst_usable = capacity_mah * (1.0 - worst_end)
        result["worst_case"] = {
            "peak_current_a": worst_peak_a,
            "usable_fraction": 1.0 - worst_end,
            "lifetime_hours": worst_usable / (avg_ma + self_discharge_ma),
            "note": "Using the single highest sample; board capacitors usually smooth spikes this short.",
        }

    warnings = []
    max_pulse = model.max_pulse_a * model.parallel
    if peak > max_pulse:
        warnings.append(
            f"Peak current {peak * 1000:.3g} mA exceeds this battery's recommended pulse current "
            f"({max_pulse * 1000:.3g} mA): expect more sag and less capacity than modelled. "
            "A capacitor across the battery or more cells in parallel helps."
        )
    if model.ocv(1.0) - peak * model.resistance(1.0) < cutoff:
        warnings.append("Even a full battery can't hold the device's minimum voltage at the peak current.")
    if avg_ma > 0.2 * capacity_mah and model.chemistry in ("Zn-MnO2", "Li-MnO2", "Li-SOCl2"):
        warnings.append(
            "The average current is high for this chemistry; real capacity will be noticeably lower than the "
            "low-drain rating used here."
        )
    if result["lifetime_years"] > 10:
        warnings.append("Over 10 years: shelf life and temperature will likely matter more than this estimate.")
    result["warnings"] = warnings
    result["summary"] = summary_text(result)
    return result


def _format_duration(hours: float) -> str:
    if hours >= 2 * HOURS_PER_YEAR:
        return f"{hours / HOURS_PER_YEAR:.3g} years"
    if hours >= 48:
        return f"{hours / 24:.3g} days"
    return f"{hours:.3g} hours"


def summary_text(result: dict[str, Any]) -> str:
    battery = result["battery"]
    cells = ""
    if battery["cells_series"] > 1 or battery["cells_parallel"] > 1:
        cells = f" ({battery['cells_series']}S{battery['cells_parallel']}P)"
    if result["usable_fraction"] <= 0:
        return (
            f"{battery['name']}{cells} can't power this load: at {result['load']['peak_current_a'] * 1000:.4g} mA "
            f"peak, even a full battery sags below the {result['cutoff_v']:.3g} V cutoff. "
            + " ".join(result["warnings"][:1])
        )
    text = (
        f"{battery['name']}{cells}: about {_format_duration(result['lifetime_hours'])} at "
        f"{result['load']['avg_current_a'] * 1000:.4g} mA average, using {result['usable_fraction']:.0%} of "
        f"{battery['capacity_mah']:.4g} mAh (limited by {result['limited_by']}, cutoff {result['cutoff_v']:.3g} V)"
    )
    if "worst_case" in result:
        text += f"; worst case {_format_duration(result['worst_case']['lifetime_hours'])}"
    if result["warnings"]:
        text += f". {len(result['warnings'])} warning(s)"
    return text

"""Analysis of a Recording's per-frame (1 ms) rows: time windows, intervals and bursts.

Each row holds the average, minimum and maximum current of one frame (50
samples), so short spikes are kept even though raw samples are not. Burst
timing therefore has 1 ms resolution, while charge and energy are exact
(frame averages integrate the raw samples).
"""

from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass
from typing import Any

from otii_mcp.ace import Recording

DEFAULT_INTERVALS = 10
MAX_INTERVALS = 60
MAX_BURSTS_LISTED = 20
# Auto threshold: the baseline (median) plus the largest of MIN_RISE_A,
# NOISE_SIGMAS x the baseline noise, and BASELINE_FRACTION x the baseline.
# Relative to the floor, not the peak, so a big burst doesn't hide smaller
# activity (e.g. 8 mA BLE events next to a 150 mA LTE burst).
MIN_RISE_A = 50e-6
NOISE_SIGMAS = 8.0
BASELINE_FRACTION = 0.05
# Bursts closer than this are one burst (radio events often have sub-steps).
DEFAULT_MERGE_GAP_S = 0.002
# The baseline median is estimated from at most this many rows.
MEDIAN_SAMPLE_ROWS = 200_000


class AnalysisError(ValueError):
    """The requested window or parameters don't fit the recording."""


@dataclass(frozen=True)
class Window:
    """Rows [first, stop) of a recording, covering [start_s, end_s)."""

    first: int
    stop: int
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def rows(self) -> int:
        return self.stop - self.first


def recording_length_s(recording: Recording) -> float:
    if not recording.frame_index:
        return 0.0
    return (recording.frame_index[-1] + 1) * recording.frame_s


def select(recording: Recording, start_s: float | None = None, end_s: float | None = None) -> Window:
    """The rows between start_s and end_s (seconds from the start of the recording)."""
    length = recording_length_s(recording)
    if length == 0:
        raise AnalysisError("The recording has no samples.")
    start = 0.0 if start_s is None else max(0.0, start_s)
    end = length if end_s is None else min(end_s, length)
    if end <= start:
        raise AnalysisError(f"Empty window: the recording is {length:.3f} s long; got start {start_s}, end {end_s}.")
    frame_s = recording.frame_s
    first = bisect.bisect_left(recording.frame_index, math.floor(start / frame_s + 1e-9))
    stop = bisect.bisect_left(recording.frame_index, math.ceil(end / frame_s - 1e-9))
    if stop <= first:
        raise AnalysisError(f"No samples arrived between {start:.3f} s and {end:.3f} s.")
    return Window(first, stop, start, end)


def summarize(recording: Recording, window: Window) -> dict[str, Any]:
    """Average/min/max current, voltage and power over a window, plus energy and charge."""
    lo, hi = window.first, window.stop
    current = recording.current_avg[lo:hi]
    voltage = recording.voltage_avg[lo:hi]
    power = recording.power_avg[lo:hi]
    rows = window.rows
    avg_current = math.fsum(current) / rows
    avg_power = math.fsum(power) / rows
    expected_rows = window.duration_s / recording.frame_s
    return {
        "start_s": window.start_s,
        "end_s": window.end_s,
        "duration_s": window.duration_s,
        "current_a": {
            "avg": avg_current,
            "min": min(recording.current_min[lo:hi]),
            "max": max(recording.current_max[lo:hi]),
        },
        "voltage_v": {"avg": math.fsum(voltage) / rows, "min": min(voltage), "max": max(voltage)},
        "power_w": {"avg": avg_power, "max": max(power)},
        # Average x duration, so frames missing from the window don't shrink it.
        "energy_j": avg_power * window.duration_s,
        "charge_c": avg_current * window.duration_s,
        "coverage": min(1.0, rows / expected_rows) if expected_rows else 0.0,
    }


def nice_interval(duration_s: float, target: int = DEFAULT_INTERVALS, minimum_s: float = 0.001) -> float:
    """A 1-2-5 step giving roughly `target` intervals."""
    raw = max(duration_s / target, minimum_s)
    exponent = math.floor(math.log10(raw))
    for step in (1, 2, 5, 10):
        candidate = step * 10**exponent
        if candidate >= raw * 0.999:
            return candidate
    return 10 ** (exponent + 1)


def intervals(recording: Recording, window: Window, interval_s: float | None = None) -> list[dict[str, Any]]:
    """Consecutive intervals across the window, each with its own averages and energy."""
    frame_s = recording.frame_s
    step = interval_s if interval_s is not None else nice_interval(window.duration_s, minimum_s=frame_s)
    if step < frame_s:
        raise AnalysisError(f"interval_s must be at least one frame ({frame_s * 1000:g} ms).")
    count = math.ceil(window.duration_s / step - 1e-9)
    if count > MAX_INTERVALS:
        raise AnalysisError(
            f"{count} intervals of {step:g} s is too many (max {MAX_INTERVALS}); use a longer interval_s or a shorter window."
        )
    rows = []
    for i in range(count):
        start = window.start_s + i * step
        end = min(window.end_s, start + step)
        try:
            part = select(recording, start, end)
        except AnalysisError:
            rows.append({"start_s": start, "end_s": end, "coverage": 0.0})
            continue
        stats = summarize(recording, part)
        rows.append(
            {
                "start_s": start,
                "end_s": end,
                "current_avg_a": stats["current_a"]["avg"],
                "current_max_a": stats["current_a"]["max"],
                "power_avg_w": stats["power_w"]["avg"],
                "energy_j": stats["energy_j"],
                "coverage": stats["coverage"],
            }
        )
    return rows


def auto_threshold(recording: Recording, window: Window) -> float:
    """Baseline (median frame current) plus a margin well above its noise."""
    values = recording.current_avg[window.first : window.stop]
    step = max(1, len(values) // MEDIAN_SAMPLE_ROWS)
    sample = values[::step]
    baseline = statistics.median(sample)
    # Robust noise estimate: median absolute deviation scaled to a standard deviation.
    sigma = 1.4826 * statistics.median(abs(v - baseline) for v in sample)
    return baseline + max(MIN_RISE_A, NOISE_SIGMAS * sigma, BASELINE_FRACTION * abs(baseline))


def bursts(
    recording: Recording,
    window: Window,
    threshold_a: float | None = None,
    merge_gap_s: float = DEFAULT_MERGE_GAP_S,
) -> dict[str, Any]:
    """Find activity bursts: runs of frames whose peak current exceeds the threshold.

    Without a threshold, one is derived from the window: the median current
    (the baseline, e.g. sleep) plus the largest of 50 uA, 8x its noise and 5%
    of it, so anything that wakes up from the floor counts as activity.
    """
    frame_s = recording.frame_s
    lo, hi = window.first, window.stop
    threshold = threshold_a if threshold_a is not None else auto_threshold(recording, window)
    index = recording.frame_index
    cur_avg = recording.current_avg
    cur_max = recording.current_max
    pow_avg = recording.power_avg
    merge_gap_frames = merge_gap_s / frame_s

    found: list[dict[str, Any]] = []
    burst: dict[str, Any] | None = None
    # Inactive frames after the last active frame of the open burst; folded
    # into the burst if another active frame arrives within the merge gap.
    pending_charge = pending_energy = 0.0
    pending_rows = 0
    quiet_charge = 0.0
    quiet_rows = 0

    def close() -> None:
        nonlocal burst, quiet_charge, quiet_rows
        if burst is not None:
            found.append(burst)
            burst = None
        quiet_charge += pending_charge
        quiet_rows += pending_rows

    for i in range(lo, hi):
        frame = index[i]
        if cur_max[i] > threshold:
            if burst is not None and frame - burst["last_frame"] - 1 <= merge_gap_frames:
                burst["charge"] += pending_charge
                burst["energy"] += pending_energy
            else:
                close()
                burst = {"first_frame": frame, "charge": 0.0, "energy": 0.0, "peak": float("-inf")}
            pending_charge = pending_energy = 0.0
            pending_rows = 0
            burst["last_frame"] = frame
            burst["charge"] += cur_avg[i] * frame_s
            burst["energy"] += pow_avg[i] * frame_s
            burst["peak"] = max(burst["peak"], cur_max[i])
        elif burst is not None:
            pending_charge += cur_avg[i] * frame_s
            pending_energy += pow_avg[i] * frame_s
            pending_rows += 1
        else:
            quiet_charge += cur_avg[i] * frame_s
            quiet_rows += 1
    close()

    listed = []
    durations = []
    peaks = []
    burst_charge = burst_energy = 0.0
    for b in found:
        duration = (b["last_frame"] - b["first_frame"] + 1) * frame_s
        durations.append(duration)
        peaks.append(b["peak"])
        burst_charge += b["charge"]
        burst_energy += b["energy"]
        if len(listed) < MAX_BURSTS_LISTED:
            listed.append(
                {
                    "start_s": b["first_frame"] * frame_s,
                    "duration_ms": duration * 1000,
                    "peak_a": b["peak"],
                    "avg_a": b["charge"] / duration,
                    "charge_c": b["charge"],
                    "energy_j": b["energy"],
                }
            )
    total_charge = math.fsum(cur_avg[lo:hi]) * frame_s
    total_energy = math.fsum(pow_avg[lo:hi]) * frame_s
    result: dict[str, Any] = {
        "threshold_a": threshold,
        "threshold_source": "given" if threshold_a is not None else "auto",
        "duration_resolution_ms": frame_s * 1000,
        "count": len(found),
        "per_second": len(found) / window.duration_s,
        # Average current outside bursts: the sleep / idle floor.
        "baseline_current_a": quiet_charge / (quiet_rows * frame_s) if quiet_rows else None,
        "duty_cycle": sum(durations) / window.duration_s,
        "charge_in_bursts_c": burst_charge,
        "energy_in_bursts_j": burst_energy,
        "share_of_charge": burst_charge / total_charge if total_charge > 0 else None,
        "share_of_energy": burst_energy / total_energy if total_energy > 0 else None,
    }
    if found:
        result["duration_ms"] = {
            "avg": 1000 * sum(durations) / len(durations),
            "min": 1000 * min(durations),
            "max": 1000 * max(durations),
        }
        result["peak_a"] = {"avg": sum(peaks) / len(peaks), "max": max(peaks)}
    result["listed"] = listed
    if len(found) > len(listed):
        result["listed_note"] = f"first {len(listed)} of {len(found)} bursts"
    return result

"""Render a window of a recording as a PNG: current (min-max band and average) above voltage."""

from __future__ import annotations

import io
import math
from typing import Any

from otii_mcp.ace import Recording
from otii_mcp.analysis import Window

MAX_BINS = 2000
# Log scale can't show zero or negative values (the Ace reads a few hundred
# nA negative at zero), so the band is clipped to this floor on log plots.
LOG_FLOOR_A = 1e-9
MAX_SHADED_BURSTS = 200


def _bins(recording: Recording, window: Window) -> dict[str, list[float]]:
    """Downsample rows into at most MAX_BINS time bins, keeping min and max."""
    frame_s = recording.frame_s
    first_frame = math.floor(window.start_s / frame_s + 1e-9)
    total_frames = max(1, math.ceil(window.end_s / frame_s - 1e-9) - first_frame)
    per_bin = max(1, math.ceil(total_frames / MAX_BINS))
    count = math.ceil(total_frames / per_bin)
    nan = float("nan")
    out = {k: [nan] * count for k in ("lo", "hi", "avg", "volt")}
    sums = [0.0] * count
    vsums = [0.0] * count
    hits = [0] * count
    index, cmin, cmax = recording.frame_index, recording.current_min, recording.current_max
    cavg, vavg = recording.current_avg, recording.voltage_avg
    for i in range(window.first, window.stop):
        b = (index[i] - first_frame) // per_bin
        if not 0 <= b < count:
            continue
        if hits[b]:
            out["lo"][b] = min(out["lo"][b], cmin[i])
            out["hi"][b] = max(out["hi"][b], cmax[i])
        else:
            out["lo"][b], out["hi"][b] = cmin[i], cmax[i]
        sums[b] += cavg[i]
        vsums[b] += vavg[i]
        hits[b] += 1
    for b in range(count):
        if hits[b]:
            out["avg"][b] = sums[b] / hits[b]
            out["volt"][b] = vsums[b] / hits[b]
    out["t"] = [(first_frame + (b + 0.5) * per_bin) * frame_s for b in range(count)]
    return out


def _current_unit(peak: float) -> tuple[str, float]:
    for unit, scale in (("A", 1.0), ("mA", 1e-3), ("uA", 1e-6)):
        if peak >= scale:
            return unit, scale
    return "nA", 1e-9


def render_png(
    recording: Recording,
    window: Window,
    *,
    title: str,
    log_scale: bool | None = None,
    threshold_a: float | None = None,
    bursts: list[dict[str, Any]] | None = None,
) -> bytes:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    data = _bins(recording, window)
    hi_values = [v for v in data["hi"] if not math.isnan(v)]
    avg_values = sorted(v for v in data["avg"] if not math.isnan(v))
    peak = max(hi_values) if hi_values else 0.0
    if log_scale is None:
        median = avg_values[len(avg_values) // 2] if avg_values else 0.0
        log_scale = median > 0 and peak / median > 100

    seconds = window.duration_s >= 2
    t_scale, t_unit = (1.0, "s") if seconds else (1e-3, "ms")
    t = [x / t_scale for x in data["t"]]

    if log_scale:
        unit, scale = "A", 1.0
        clip = lambda v: v if math.isnan(v) else max(v, LOG_FLOOR_A)  # noqa: E731
        lo = [clip(v) for v in data["lo"]]
        hi = [clip(v) for v in data["hi"]]
        avg = [clip(v) for v in data["avg"]]
    else:
        unit, scale = _current_unit(abs(peak) or 1e-9)
        lo = [v / scale for v in data["lo"]]
        hi = [v / scale for v in data["hi"]]
        avg = [v / scale for v in data["avg"]]

    fig = Figure(figsize=(11, 6), dpi=100)
    FigureCanvasAgg(fig)
    grid = fig.add_gridspec(2, 1, height_ratios=(3, 1), hspace=0.08)
    ax_i = fig.add_subplot(grid[0])
    ax_v = fig.add_subplot(grid[1], sharex=ax_i)

    # Steps, not lines: each point stands for a whole bin (>= 1 ms), and
    # interpolating would turn a flat 3 ms burst into a trapezoid.
    ax_i.fill_between(t, lo, hi, step="mid", color="#4c8bf5", alpha=0.35, linewidth=0, label="min-max per bin")
    ax_i.plot(t, avg, drawstyle="steps-mid", color="#1a4fb4", linewidth=0.9, label="average")
    if threshold_a is not None:
        level = max(threshold_a, LOG_FLOOR_A) if log_scale else threshold_a / scale
        ax_i.axhline(level, color="#d93025", linestyle="--", linewidth=0.8, label="burst threshold")
    for burst in (bursts or [])[:MAX_SHADED_BURSTS]:
        start = burst["start_s"] / t_scale
        ax_i.axvspan(start, start + burst["duration_ms"] / 1000 / t_scale, color="#f9ab00", alpha=0.15, linewidth=0)
    if log_scale:
        ax_i.set_yscale("log")
    ax_i.set_ylabel(f"Current ({unit})")
    ax_i.grid(True, which="both", alpha=0.3)
    ax_i.legend(loc="upper right", fontsize=8)
    ax_i.set_title(title, fontsize=10)
    ax_i.tick_params(labelbottom=False)

    ax_v.plot(t, data["volt"], drawstyle="steps-mid", color="#188038", linewidth=0.9)
    ax_v.set_ylabel("Voltage (V)")
    ax_v.set_xlabel(f"Time ({t_unit})")
    ax_v.grid(True, alpha=0.3)
    ax_v.set_xlim(window.start_s / t_scale, window.end_s / t_scale)

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight")
    return buffer.getvalue()

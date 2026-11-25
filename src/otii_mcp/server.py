"""MCP server for the Otii Ace: output control, readings, measurements, recordings, analysis, plots."""

from __future__ import annotations

import atexit
from typing import Annotated, Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from otii_mcp.bench import (
    DEFAULT_RECORD_S,
    MAX_CURRENT_LIMIT_A,
    MAX_MEASURE_S,
    MAX_RECORD_S,
    MAX_VOLTAGE_V,
    MIN_CURRENT_LIMIT_A,
    BenchError,
    Config,
    OtiiBench,
)

mcp = MCPServer(
    "otii",
    instructions=(
        "Controls a Qoitech Otii Ace power supply/analyzer that powers a device under test. "
        "Values are SI units (A, V, W, J). Use otii_read for a quick snapshot (25 Hz telemetry), "
        "otii_measure for a fixed-length 50 kHz measurement, or otii_record_start/otii_record_stop to "
        "record in the background while doing other things (e.g. flashing firmware or sending commands). "
        "After either, otii_plot shows the waveform, otii_analyze zooms into a window, otii_save writes "
        "CSV/PNG/JSON files. "
        "The current limit does not switch the output off: at the limit the Ace holds the current "
        "and the voltage sags, which otii_read reports as current_limited."
    ),
)

_bench: OtiiBench | None = None


def _get_bench() -> OtiiBench:
    global _bench
    if _bench is None:
        _bench = OtiiBench(Config.from_env())
        atexit.register(_bench.close)
    return _bench


def _run(action: str, op: Any) -> dict[str, Any]:
    try:
        return op(_get_bench())
    except BenchError as error:
        raise ToolError(f"{action} failed: {error}") from error


@mcp.tool(annotations=ToolAnnotations(title="Turn Otii output on", destructiveHint=False, idempotentHint=True))
def otii_power_on(
    voltage_v: Annotated[
        float | None,
        Field(
            gt=0,
            le=MAX_VOLTAGE_V,
            description="Output voltage in volts. Omit to keep the voltage already set on the Otii.",
        ),
    ] = None,
    current_limit_a: Annotated[
        float | None,
        Field(
            ge=MIN_CURRENT_LIMIT_A,
            le=MAX_CURRENT_LIMIT_A,
            description=(
                "Current limit in amps, applied before switching on. Above it the Ace limits the "
                "current (voltage sags) rather than switching off. Omit to keep the limit already set."
            ),
        ),
    ] = None,
) -> dict[str, Any]:
    """Turn on the Otii main power output, optionally setting the current limit and voltage first.

    Returns a reading taken right after switching on, including the current limit in force.
    """
    return _run("Power on", lambda bench: bench.power_on(voltage_v, current_limit_a))


@mcp.tool(annotations=ToolAnnotations(title="Turn Otii output off", destructiveHint=False, idempotentHint=True))
def otii_power_off() -> dict[str, Any]:
    """Turn off the Otii main power output. Returns a reading taken right after switching off."""
    return _run("Power off", lambda bench: bench.power_off())


@mcp.tool(annotations=ToolAnnotations(title="Read Otii now", readOnlyHint=True))
def otii_read() -> dict[str, Any]:
    """Latest output state, setpoint, current limit, current, voltage and power.

    A single 25 Hz telemetry reading: misleading for devices that sleep or transmit in bursts;
    use otii_measure for those.
    """
    return _run("Read", lambda bench: bench.read())


BurstThreshold = Annotated[
    float | None,
    Field(
        gt=0,
        description=(
            "Current above which a frame counts as part of a burst, in amps. Omit for automatic: the "
            "median current (the sleep/idle floor) plus the largest of 50 uA, 8x its noise and 5% of it. "
            "Raise it to ignore smaller activity and keep only big events."
        ),
    ),
]
IntervalS = Annotated[
    float | None,
    Field(gt=0, description="Length of each interval in the breakdown, in seconds. Omit for about 10 intervals."),
]
StartS = Annotated[
    float | None,
    Field(ge=0, description="Window start, in seconds from the start of the last capture. Omit for the start."),
]
EndS = Annotated[
    float | None,
    Field(gt=0, description="Window end, in seconds from the start of the last capture. Omit for the end."),
]

_REPORT_DOC = (
    "Returns exact statistics over every 50 kHz sample (avg/min/max current, voltage and power, "
    "energy, charge, coverage), a breakdown into `intervals`, and `bursts`: activity above a "
    "threshold with the baseline (e.g. sleep) current between bursts, count, duration (1 ms "
    "resolution), peak, charge and share of energy, listing the first 20. `coverage` is the share "
    "of due samples that arrived over USB (normally 100%). Use otii_plot to look at the waveform "
    "and otii_analyze to zoom into part of it."
)


@mcp.tool(
    annotations=ToolAnnotations(title="Measure Otii consumption", readOnlyHint=True),
    description=(
        "Record 50 kHz samples for a fixed time (blocks meanwhile; max 300 s). Does not switch the "
        "output. For longer captures, or to do other things while recording, use otii_record_start. "
        + _REPORT_DOC
    ),
)
def otii_measure(
    seconds: Annotated[
        float,
        Field(gt=0, le=MAX_MEASURE_S, description="How long to record, in seconds."),
    ] = 5.0,
    burst_threshold_a: BurstThreshold = None,
    interval_s: IntervalS = None,
) -> dict[str, Any]:
    return _run("Measure", lambda bench: bench.measure(seconds, burst_threshold_a, interval_s))



@mcp.tool(annotations=ToolAnnotations(title="Start Otii recording", readOnlyHint=True))
def otii_record_start(
    max_seconds: Annotated[
        float,
        Field(gt=0, le=MAX_RECORD_S, description="Stop automatically after this many seconds (max 3600)."),
    ] = DEFAULT_RECORD_S,
) -> dict[str, Any]:
    """Start a background 50 kHz recording and return immediately.

    Other tools (power on/off, reads) keep working while it runs, so you can start a recording,
    flash firmware or send commands to the device, then call otii_record_stop for the results.
    It keeps one CPU core busy while running and stops itself after max_seconds.
    """
    return _run("Start recording", lambda bench: bench.start_recording(max_seconds))


@mcp.tool(annotations=ToolAnnotations(title="Otii recording status", readOnlyHint=True))
def otii_record_status() -> dict[str, Any]:
    """Whether a recording is running, how long it has run, and the averages so far."""
    return _run("Recording status", lambda bench: bench.recording_status())


@mcp.tool(
    annotations=ToolAnnotations(title="Stop Otii recording", readOnlyHint=True),
    description=(
        "Stop the background recording (or collect one that stopped itself at max_seconds). " + _REPORT_DOC
    ),
)
def otii_record_stop(burst_threshold_a: BurstThreshold = None, interval_s: IntervalS = None) -> dict[str, Any]:
    return _run("Stop recording", lambda bench: bench.stop_recording(burst_threshold_a, interval_s))



@mcp.tool(annotations=ToolAnnotations(title="Analyze Otii capture", readOnlyHint=True))
def otii_analyze(
    start_s: StartS = None,
    end_s: EndS = None,
    burst_threshold_a: BurstThreshold = None,
    interval_s: IntervalS = None,
) -> dict[str, Any]:
    """Re-analyze the last measurement or recording, optionally only a time window of it.

    Same shape as otii_measure's result, computed from 1 ms rows (each keeps the average, min and
    max of its 50 samples). Use it to zoom into a burst or to try another burst threshold.
    """
    return _run("Analyze", lambda bench: bench.analyze(start_s, end_s, burst_threshold_a, interval_s))


@mcp.tool(annotations=ToolAnnotations(title="Plot Otii capture", readOnlyHint=True))
def otii_plot(
    start_s: StartS = None,
    end_s: EndS = None,
    log_scale: Annotated[
        bool | None,
        Field(description="Log current axis. Omit to choose automatically (log when peaks dwarf the baseline)."),
    ] = None,
    burst_threshold_a: BurstThreshold = None,
) -> list[TextContent | Image]:
    """Plot the last measurement or recording (or a time window of it) as an image.

    Current on top as a min-max band with the average line, the burst threshold dashed and bursts
    shaded; voltage below. Zoom in with start_s/end_s to see individual bursts (1 ms resolution).
    """
    try:
        caption, png = _get_bench().plot(start_s, end_s, log_scale, burst_threshold_a)
    except BenchError as error:
        raise ToolError(f"Plot failed: {error}") from error
    return [TextContent(type="text", text=caption), Image(data=png, format="png")]


@mcp.tool(annotations=ToolAnnotations(title="Save Otii capture", destructiveHint=False))
def otii_save(
    directory: Annotated[
        str | None,
        Field(description="Folder to write to. Omit for OTII_CAPTURE_DIR, or ~/otii-captures."),
    ] = None,
    start_s: StartS = None,
    end_s: EndS = None,
) -> dict[str, Any]:
    """Save the last measurement or recording (or a window of it): CSV at 1 ms resolution, a PNG plot and a JSON summary.

    CSV columns: time_s, current_avg_a, current_min_a, current_max_a, voltage_avg_v, power_avg_w.
    About 60 bytes per row, so an hour is roughly 200 MB.
    """
    return _run("Save", lambda bench: bench.save(directory, start_s, end_s))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

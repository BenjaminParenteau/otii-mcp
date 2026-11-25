"""MCP server exposing an Otii Ace as four tools: power on, power off, read, measure."""

from __future__ import annotations

import atexit
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from otii_mcp.bench import (
    MAX_CURRENT_LIMIT_A,
    MAX_MEASURE_S,
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
        "Values are SI units (A, V, W, J). Use otii_read for a quick snapshot (25 Hz telemetry) and "
        "otii_measure for real consumption of devices that sleep or burst (50 kHz samples). "
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


@mcp.tool(annotations=ToolAnnotations(title="Measure Otii consumption", readOnlyHint=True))
def otii_measure(
    seconds: Annotated[
        float,
        Field(gt=0, le=MAX_MEASURE_S, description="How long to record, in seconds."),
    ] = 5.0,
) -> dict[str, Any]:
    """Record 50 kHz samples; return average/min/max current, voltage and power, plus energy and charge.

    `coverage` is the share of due samples that arrived over USB (normally 100%); any missing
    frames are timing gaps unrelated to the load, so averages stay valid, but a spike inside a
    gap would not show in max. Blocks for the requested duration and keeps one CPU core busy
    meanwhile. Does not switch the output.
    """
    return _run("Measure", lambda bench: bench.measure(seconds))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

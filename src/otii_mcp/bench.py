"""Power on/off and power readings for one Otii Ace, on top of the direct driver."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from otii_mcp.ace import (
    CHANNEL_CURRENT_A,
    CHANNEL_POWER_W,
    CHANNEL_VOLTAGE_V,
    AceDevice,
    AceError,
    Recording,
    Sample,
    find_ace_port,
    open_transport,
)

# The Ace main output range is 0-25 V.
MAX_VOLTAGE_V = 25.0
# Current limit range accepted by the Otii software (1 mA - 5 A).
MIN_CURRENT_LIMIT_A = 0.001
MAX_CURRENT_LIMIT_A = 5.0
MAX_MEASURE_S = 300.0
# Below this share of frames received, a measurement is reported as an error
# rather than a result.
MIN_COVERAGE = 0.8
# After switching, skip readings for this long (two telemetry periods) so the
# reported values belong to the new state.
SETTLE_S = 0.08
# Limiting shows as the voltage sagging below the setpoint while current sits
# near the limit (measured: a 2 mA limit held 1.82-1.96 mA).
SAG_FRACTION = 0.95
NEAR_LIMIT_FRACTION = 0.8

T = TypeVar("T")


class BenchError(Exception):
    """A failure with a message meant for the person at the bench."""


@dataclass(frozen=True)
class Config:
    # COM port (or pyserial URL). None means find the Ace by its USB id.
    serial_port: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Config:
        return cls(serial_port=env.get("OTII_SERIAL_PORT") or None)


def open_device(config: Config) -> AceDevice:
    port = config.serial_port or find_ace_port()
    transport = open_transport(port)
    try:
        return AceDevice(transport)
    except AceError as error:
        raise AceError(f"The device on {port} did not answer like an Otii Ace: {error}") from error


def describe_reading(current_a: float, voltage_v: float, power_w: float) -> str:
    return f"{_si(current_a, 'A')} at {_si(voltage_v, 'V')} = {_si(power_w, 'W')}"


def _si(value: float, unit: str) -> str:
    magnitude = abs(value)
    for scale, prefix in ((1.0, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n")):
        if magnitude >= scale:
            return f"{value / scale:.4g} {prefix}{unit}"
    return f"{value / 1e-9:.4g} n{unit}"


class OtiiBench:
    """One Ace, opened on first use and reopened if it is unplugged and replugged."""

    def __init__(self, config: Config, open_device: Callable[[Config], AceDevice] = open_device) -> None:
        self._config = config
        self._open_device = open_device
        self._device: AceDevice | None = None
        # Serialises commands and (re)connecting.
        self._lock = threading.Lock()
        # One measurement at a time. Not held by commands, so power_off still
        # works in the middle of a long measurement.
        self._measure_lock = threading.Lock()

    def close(self) -> None:
        """Release the serial port. The output is left as it is."""
        with self._lock:
            self._drop_device()

    def power_on(self, voltage_v: float | None = None, current_limit_a: float | None = None) -> dict[str, Any]:
        if voltage_v is not None and not 0 < voltage_v <= MAX_VOLTAGE_V:
            raise BenchError(f"voltage_v must be above 0 and at most {MAX_VOLTAGE_V} V, got {voltage_v}")
        if current_limit_a is not None and not MIN_CURRENT_LIMIT_A <= current_limit_a <= MAX_CURRENT_LIMIT_A:
            raise BenchError(
                f"current_limit_a must be between {MIN_CURRENT_LIMIT_A} and {MAX_CURRENT_LIMIT_A} A, "
                f"got {current_limit_a}"
            )

        def op(device: AceDevice) -> None:
            if current_limit_a is not None:
                device.set_current_limit(current_limit_a)
            if voltage_v is not None:
                device.set_voltage(voltage_v)
            device.set_output(True)

        self._call(op)
        return self.read(after_switch=True)

    def power_off(self) -> dict[str, Any]:
        self._call(lambda device: device.set_output(False))
        return self.read(after_switch=True)

    def read(self, *, after_switch: bool = False) -> dict[str, Any]:
        """Latest telemetry. Fine for steady loads; use measure() for anything that sleeps or bursts.

        `after_switch` waits for readings taken after the output settled, so a
        reading right after power on/off reflects the new state.
        """

        def op(device: AceDevice) -> tuple[bool, float, float, Sample]:
            output_on, setpoint_v, limit_a = device.get_output(), device.get_voltage(), device.get_current_limit()
            newer_than = time.monotonic() + SETTLE_S if after_switch else None
            return output_on, setpoint_v, limit_a, device.latest(newer_than=newer_than)

        output_on, setpoint_v, limit_a, sample = self._call(op)
        # At the limit the Ace holds the current and lets the voltage sag.
        limited = (
            output_on
            and sample.voltage_v < setpoint_v * SAG_FRACTION
            and sample.current_a >= limit_a * NEAR_LIMIT_FRACTION
        )
        return {
            "output_on": output_on,
            "setpoint_v": setpoint_v,
            "current_limit_a": limit_a,
            "current_limited": limited,
            "current_a": sample.current_a,
            "voltage_v": sample.voltage_v,
            "power_w": sample.power_w,
            "summary": ("ON: " if output_on else "OFF: ")
            + describe_reading(sample.current_a, sample.voltage_v, sample.power_w)
            + (f" (CURRENT LIMITED at {_si(limit_a, 'A')}, voltage sagging)" if limited else ""),
        }

    def measure(self, seconds: float) -> dict[str, Any]:
        """Record full-rate (50 kHz) samples for `seconds`; return average/min/max and energy used."""
        if not 0 < seconds <= MAX_MEASURE_S:
            raise BenchError(f"seconds must be above 0 and at most {MAX_MEASURE_S:g}, got {seconds}")
        if not self._measure_lock.acquire(blocking=False):
            raise BenchError("A measurement is already running; wait for it to finish.")
        try:
            device = self._call(lambda device: device)
            try:
                recording = device.record(seconds)
            except AceError as error:
                if not device.alive:
                    with self._lock:
                        self._drop_device()
                    raise BenchError(f"{error} It will reconnect on the next call.") from error
                raise BenchError(str(error)) from error
        finally:
            self._measure_lock.release()
        return _statistics(recording, seconds)

    def _call(self, op: Callable[[AceDevice], T]) -> T:
        """Run `op` on the device, reconnecting once if the link had dropped (all ops are idempotent)."""
        with self._lock:
            for attempt in range(2):
                device = self._ensure_device()
                try:
                    return op(device)
                except AceError as error:
                    if device.alive:
                        raise BenchError(str(error)) from error
                    self._drop_device()
                    if attempt == 1:
                        raise BenchError(f"{error} It will reconnect on the next call.") from error
            raise AssertionError("unreachable")

    def _ensure_device(self) -> AceDevice:
        if self._device is not None and not self._device.alive:
            self._drop_device()
        if self._device is None:
            try:
                self._device = self._open_device(self._config)
            except AceError as error:
                raise BenchError(str(error)) from error
        return self._device

    def _drop_device(self) -> None:
        device, self._device = self._device, None
        if device is not None:
            try:
                device.close()
            except Exception:
                pass


def _statistics(recording: Recording, requested_s: float) -> dict[str, Any]:
    channels = recording.channels
    needed = (CHANNEL_CURRENT_A, CHANNEL_VOLTAGE_V, CHANNEL_POWER_W)
    if not all(c in channels and channels[c].count for c in needed):
        raise BenchError("No samples arrived from the Ace during the measurement.")
    current, voltage, power = (channels[c] for c in needed)
    coverage = recording.coverage(CHANNEL_CURRENT_A)
    if coverage < MIN_COVERAGE:
        raise BenchError(
            f"Only {coverage:.0%} of the Ace's sample frames arrived; the USB link is too slow for a "
            "trustworthy measurement. Try another USB port, directly on the PC."
        )
    result = {
        "duration_s": requested_s,
        "sample_rate_hz": current.sample_rate,
        "samples": current.count,
        # Samples received / samples due for the time streamed; normally 100%.
        # Any missing frames are USB timing gaps, unrelated to the load, so
        # averages stay unbiased, but a spike inside a gap would not show in max.
        "coverage": coverage,
        "current_a": {"avg": current.average, "min": current.minimum, "max": current.maximum},
        "voltage_v": {"avg": voltage.average, "min": voltage.minimum, "max": voltage.maximum},
        "power_w": {"avg": power.average, "min": power.minimum, "max": power.maximum},
        # Integral = average x duration, whatever the waveform looks like.
        "energy_j": power.average * requested_s,
        "charge_c": current.average * requested_s,
    }
    result["summary"] = (
        f"avg {describe_reading(current.average, voltage.average, power.average)} over {requested_s:g} s "
        f"at {current.sample_rate / 1000:g} kHz (peak {_si(current.maximum, 'A')}, "
        f"{coverage:.1%} of samples), energy {_si(result['energy_j'], 'J')}"
    )
    return result

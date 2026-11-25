"""Power on/off, readings, measurements and recordings for one Otii Ace, on top of the direct driver."""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from otii_mcp import analysis
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
# Background recordings stop on their own after max_seconds (default/limit).
DEFAULT_RECORD_S = 600.0
MAX_RECORD_S = 3600.0
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
    # Where otii_save writes files. None means ~/otii-captures.
    capture_dir: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Config:
        return cls(
            serial_port=env.get("OTII_SERIAL_PORT") or None,
            capture_dir=env.get("OTII_CAPTURE_DIR") or None,
        )


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


@dataclass
class Capture:
    """A finished measurement or recording, kept for analyze/plot/save."""

    recording: Recording
    started_at: datetime
    duration_s: float
    kind: str  # "measure" or "recording"
    auto_stopped: bool = False
    link_lost: bool = False
    # Whether otii_record_stop has returned this capture (for auto-stopped ones).
    collected: bool = False


@dataclass
class _Active:
    device: AceDevice
    recording: Recording
    started_at: datetime
    started_mono: float
    max_seconds: float
    timer: threading.Timer | None = field(default=None, repr=False)


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
        # Guards starting/stopping background recordings.
        self._record_state = threading.Lock()
        self._active: _Active | None = None
        self._capture: Capture | None = None

    def close(self) -> None:
        """Stop any background recording and release the serial port. The output is left as it is."""
        self._finish_active(auto=False)
        with self._lock:
            self._drop_device()

    # -- output ----------------------------------------------------------------

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

    # -- measurements and recordings ---------------------------------------------

    def measure(
        self, seconds: float, burst_threshold_a: float | None = None, interval_s: float | None = None
    ) -> dict[str, Any]:
        """Record full-rate (50 kHz) samples for `seconds`; return statistics, intervals and bursts."""
        if not 0 < seconds <= MAX_MEASURE_S:
            raise BenchError(f"seconds must be above 0 and at most {MAX_MEASURE_S:g}, got {seconds}")
        if self._active is not None:
            raise BenchError("A background recording is running; stop it with otii_record_stop first.")
        if not self._measure_lock.acquire(blocking=False):
            raise BenchError("A measurement is already running; wait for it to finish.")
        try:
            device = self._call(lambda device: device)
            started_at = datetime.now()
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
        stats = _statistics(recording, seconds, strict=True)
        capture = Capture(recording, started_at, seconds, kind="measure")
        self._capture = capture
        return self._report(capture, stats, burst_threshold_a, interval_s)

    def start_recording(self, max_seconds: float = DEFAULT_RECORD_S) -> dict[str, Any]:
        """Start a background recording; it runs until stop_recording() or max_seconds."""
        if not 0 < max_seconds <= MAX_RECORD_S:
            raise BenchError(f"max_seconds must be above 0 and at most {MAX_RECORD_S:g}, got {max_seconds}")
        with self._record_state:
            if self._active is not None:
                raise BenchError("A recording is already running; check it with otii_record_status or stop it.")
            if not self._measure_lock.acquire(blocking=False):
                raise BenchError("A measurement is running; wait for it to finish.")
            try:
                device = self._call(lambda device: device)
                try:
                    recording = device.start_recording()
                except AceError as error:
                    raise BenchError(str(error)) from error
            finally:
                self._measure_lock.release()
            active = _Active(device, recording, datetime.now(), time.monotonic(), max_seconds)
            active.timer = threading.Timer(max_seconds, self._finish_active, kwargs={"auto": True})
            active.timer.daemon = True
            active.timer.start()
            self._active = active
        return self.recording_status()

    def recording_status(self) -> dict[str, Any]:
        active = self._active
        if active is None:
            status: dict[str, Any] = {"recording": False}
            capture = self._capture
            if capture is not None:
                status["last_capture"] = {
                    "kind": capture.kind,
                    "started_at": capture.started_at.isoformat(timespec="seconds"),
                    "duration_s": capture.duration_s,
                    "auto_stopped": capture.auto_stopped,
                    "link_lost": capture.link_lost,
                }
                status["summary"] = (
                    f"Not recording. Last capture: {capture.kind}, {capture.duration_s:.3g} s from "
                    f"{capture.started_at:%H:%M:%S}" + (" (stopped at max_seconds)" if capture.auto_stopped else "")
                )
            else:
                status["summary"] = "Not recording, and nothing captured yet."
            return status
        elapsed = time.monotonic() - active.started_mono
        recording = active.recording
        current = recording.channels.get(CHANNEL_CURRENT_A)
        power = recording.channels.get(CHANNEL_POWER_W)
        status = {
            "recording": True,
            "started_at": active.started_at.isoformat(timespec="seconds"),
            "elapsed_s": elapsed,
            "max_seconds": active.max_seconds,
            "auto_stop_in_s": max(0.0, active.max_seconds - elapsed),
            "link_ok": active.device.alive,
            "samples": current.count if current else 0,
        }
        if current and current.count and power and power.count:
            status.update(
                current_avg_a=current.average,
                current_max_a=current.maximum,
                power_avg_w=power.average,
                energy_so_far_j=power.average * elapsed,
            )
            status["summary"] = (
                f"Recording for {elapsed:.1f} s (stops itself at {active.max_seconds:g} s): avg "
                f"{_si(current.average, 'A')}, peak {_si(current.maximum, 'A')}, "
                f"{_si(power.average * elapsed, 'J')} so far"
            )
        else:
            status["summary"] = f"Recording for {elapsed:.1f} s; no samples yet."
        if not active.device.alive:
            status["summary"] += ". The link to the Ace dropped; stop to keep what was captured."
        return status

    def stop_recording(
        self, burst_threshold_a: float | None = None, interval_s: float | None = None
    ) -> dict[str, Any]:
        """Stop the background recording and return statistics, intervals and bursts."""
        capture = self._finish_active(auto=False)
        if capture is None:
            last = self._capture
            if last is not None and last.kind == "recording" and last.auto_stopped and not last.collected:
                capture = last
            else:
                raise BenchError("No recording is running. Start one with otii_record_start.")
        capture.collected = True
        try:
            stats = _statistics(capture.recording, capture.duration_s, strict=False)
        except BenchError as error:
            raise BenchError(f"Recording stopped, but no samples arrived from the Ace ({error})") from error
        report = self._report(capture, stats, burst_threshold_a, interval_s)
        notes = []
        if capture.auto_stopped:
            notes.append(f"it stopped itself at max_seconds ({capture.duration_s:.0f} s)")
        if capture.link_lost:
            notes.append("the link to the Ace dropped, so it ends early")
        if notes:
            report["note"] = "The recording " + " and ".join(notes) + "."
            report["summary"] += f" Note: {report['note']}"
        return report

    def analyze(
        self,
        start_s: float | None = None,
        end_s: float | None = None,
        burst_threshold_a: float | None = None,
        interval_s: float | None = None,
    ) -> dict[str, Any]:
        """Statistics, intervals and bursts for a time window of the last capture."""
        capture = self._require_capture()
        recording = capture.recording
        try:
            window = analysis.select(recording, start_s, end_s)
            stats = analysis.summarize(recording, window)
        except analysis.AnalysisError as error:
            raise BenchError(str(error)) from error
        current, voltage, power = stats["current_a"], stats["voltage_v"], stats["power_w"]
        stats["summary"] = (
            f"{window.start_s:.4g}-{window.end_s:.4g} s: avg "
            f"{describe_reading(current['avg'], voltage['avg'], power['avg'])} "
            f"(peak {_si(current['max'], 'A')}), energy {_si(stats['energy_j'], 'J')}"
        )
        return self._report(capture, stats, burst_threshold_a, interval_s, window=window)

    def plot(
        self,
        start_s: float | None = None,
        end_s: float | None = None,
        log_scale: bool | None = None,
        burst_threshold_a: float | None = None,
    ) -> tuple[str, bytes]:
        """A PNG of a window of the last capture, and a one-line caption."""
        from otii_mcp import plot

        capture = self._require_capture()
        recording = capture.recording
        try:
            window = analysis.select(recording, start_s, end_s)
            stats = analysis.summarize(recording, window)
            found = analysis.bursts(recording, window, burst_threshold_a)
        except analysis.AnalysisError as error:
            raise BenchError(str(error)) from error
        caption = (
            f"{capture.started_at:%Y-%m-%d %H:%M:%S} {capture.kind}, {window.start_s:.4g}-{window.end_s:.4g} s: "
            f"avg {_si(stats['current_a']['avg'], 'A')}, peak {_si(stats['current_a']['max'], 'A')}, "
            f"{found['count']} bursts above {_si(found['threshold_a'], 'A')}"
        )
        png = plot.render_png(
            recording,
            window,
            title=caption,
            log_scale=log_scale,
            threshold_a=found["threshold_a"],
            bursts=found["listed"] if found["count"] <= analysis.MAX_BURSTS_LISTED else None,
        )
        return caption, png

    def save(
        self, directory: str | None = None, start_s: float | None = None, end_s: float | None = None
    ) -> dict[str, Any]:
        """Write the last capture (or a window of it) as CSV at 1 ms resolution, plus a PNG and a JSON summary."""
        capture = self._require_capture()
        recording = capture.recording
        try:
            window = analysis.select(recording, start_s, end_s)
        except analysis.AnalysisError as error:
            raise BenchError(str(error)) from error
        folder = Path(directory or self._config.capture_dir or Path.home() / "otii-captures").expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise BenchError(f"Cannot create {folder}: {error}") from error
        stem = f"otii_{capture.started_at:%Y%m%d_%H%M%S}"
        if start_s is not None or end_s is not None:
            stem += f"_{window.start_s:g}-{window.end_s:g}s"
        csv_path, png_path, json_path = (folder / f"{stem}{ext}" for ext in (".csv", ".png", ".json"))

        frame_s = recording.frame_s
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["time_s", "current_avg_a", "current_min_a", "current_max_a", "voltage_avg_v", "power_avg_w"])
            for i in range(window.first, window.stop):
                writer.writerow(
                    [
                        f"{recording.frame_index[i] * frame_s:.4f}",
                        f"{recording.current_avg[i]:.6e}",
                        f"{recording.current_min[i]:.6e}",
                        f"{recording.current_max[i]:.6e}",
                        f"{recording.voltage_avg[i]:.6f}",
                        f"{recording.power_avg[i]:.6e}",
                    ]
                )
        _, png = self.plot(start_s, end_s)
        png_path.write_bytes(png)
        summary = self.analyze(start_s, end_s)
        summary["capture"] = {"kind": capture.kind, "started_at": capture.started_at.isoformat(timespec="seconds")}
        json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return {
            "csv": str(csv_path),
            "png": str(png_path),
            "json": str(json_path),
            "rows": window.rows,
            "resolution_s": frame_s,
            "summary": f"Saved {window.rows} rows ({frame_s * 1000:g} ms each) to {csv_path}, plus .png and .json",
        }

    # -- internals ------------------------------------------------------------------

    def _finish_active(self, *, auto: bool) -> Capture | None:
        with self._record_state:
            active = self._active
            if active is None:
                return None
            self._active = None
            if active.timer is not None and not auto:
                active.timer.cancel()
            elapsed = time.monotonic() - active.started_mono
            try:
                active.device.stop_recording()
            except AceError:
                pass  # The link dropped; keep the samples received so far.
            link_lost = not active.device.alive
            if link_lost:
                with self._lock:
                    if self._device is active.device:
                        self._drop_device()
            recording = active.recording
            duration = recording.window_s or elapsed
            capture = Capture(recording, active.started_at, duration, "recording", auto_stopped=auto, link_lost=link_lost)
            self._capture = capture
            return capture

    def _require_capture(self) -> Capture:
        if self._capture is None:
            raise BenchError("Nothing captured yet. Run otii_measure, or otii_record_start then otii_record_stop.")
        return self._capture

    def _report(
        self,
        capture: Capture,
        stats: dict[str, Any],
        burst_threshold_a: float | None,
        interval_s: float | None,
        window: analysis.Window | None = None,
    ) -> dict[str, Any]:
        recording = capture.recording
        try:
            window = window or analysis.select(recording)
            found = analysis.bursts(recording, window, burst_threshold_a)
            parts = analysis.intervals(recording, window, interval_s)
        except analysis.AnalysisError as error:
            raise BenchError(str(error)) from error
        report = dict(stats)
        report["intervals"] = parts
        report["bursts"] = found
        report["summary"] = f"{stats['summary']}. {_describe_bursts(found)}"
        return report

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


def _describe_bursts(found: dict[str, Any]) -> str:
    if not found["count"]:
        return f"No bursts above {_si(found['threshold_a'], 'A')}"
    baseline = found["baseline_current_a"]
    text = (
        f"{found['count']} bursts above {_si(found['threshold_a'], 'A')} "
        f"(avg {found['duration_ms']['avg']:.3g} ms, peak up to {_si(found['peak_a']['max'], 'A')}, "
        f"{found['share_of_energy']:.0%} of the energy)"
    )
    if baseline is not None:
        text += f", baseline {_si(baseline, 'A')} between them"
    return text


def _statistics(recording: Recording, duration_s: float, *, strict: bool) -> dict[str, Any]:
    """Exact statistics over every 50 kHz sample. `strict` refuses low-coverage results."""
    channels = recording.channels
    needed = (CHANNEL_CURRENT_A, CHANNEL_VOLTAGE_V, CHANNEL_POWER_W)
    if not all(c in channels and channels[c].count for c in needed):
        raise BenchError("No samples arrived from the Ace during the measurement.")
    current, voltage, power = (channels[c] for c in needed)
    coverage = recording.coverage(CHANNEL_CURRENT_A)
    if strict and coverage < MIN_COVERAGE:
        raise BenchError(
            f"Only {coverage:.0%} of the Ace's sample frames arrived; the USB link is too slow for a "
            "trustworthy measurement. Try another USB port, directly on the PC."
        )
    result: dict[str, Any] = {
        "duration_s": duration_s,
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
        "energy_j": power.average * duration_s,
        "charge_c": current.average * duration_s,
    }
    result["summary"] = (
        f"avg {describe_reading(current.average, voltage.average, power.average)} over {duration_s:g} s "
        f"at {current.sample_rate / 1000:g} kHz (peak {_si(current.maximum, 'A')}, "
        f"{coverage:.1%} of samples), energy {_si(result['energy_j'], 'J')}"
    )
    if coverage < MIN_COVERAGE:
        result["warning"] = f"Only {coverage:.0%} of samples arrived; treat these figures with care."
        result["summary"] += f". WARNING: {result['warning']}"
    return result

"""Background recordings, burst/interval analysis, plots and saved files."""

from __future__ import annotations

import csv
import json
import struct
import threading
import time

import pytest

from otii_mcp import analysis
from otii_mcp.ace import SAMPLES_TAG, AceDevice, Recording
from otii_mcp.bench import BenchError, Config, OtiiBench
from otii_mcp.plot import render_png
from tests.fake_ace import FakeAce, FakeAceTransport

SLEEP_A = 5e-6
BURST_A = 20e-3
VOLTS = 3.3
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def frame(counter: int, current: float | list[float], volts: float = VOLTS) -> bytes:
    """A sample frame exactly as the Ace sends it (current, power, voltage blocks)."""
    samples = current if isinstance(current, list) else [current] * 50
    blocks = b""
    for channel, values in ((0, samples), (6, [volts * i for i in samples]), (1, [volts] * 50)):
        blocks += struct.pack("<HHI", channel, 50, 50_000) + struct.pack("<50f", *values)
    return SAMPLES_TAG + struct.pack("<I", counter) + blocks


def periodic_bursts(t: float, period_s: float = 0.1, width_s: float = 0.004) -> float:
    # Whole frames, not float modulo (0.3 % 0.1 is 0.0999...).
    frame_no, period, width = round(t * 1000), round(period_s * 1000), round(width_s * 1000)
    return BURST_A if frame_no % period < width else SLEEP_A


def recording_from(currents: list[float | list[float]], skip: set[int] = frozenset()) -> Recording:
    rec = Recording()
    for i, current in enumerate(currents):
        if i not in skip:
            rec.add_frame(frame(i, current), arrival=i * 0.001)
    return rec


# -- analysis on synthetic frames ------------------------------------------------


def test_frames_keep_average_min_and_max_per_ms():
    spike = [SLEEP_A] * 49 + [10e-3]
    rec = recording_from([SLEEP_A, spike])

    assert list(rec.frame_index) == [0, 1]
    assert rec.current_max[1] == pytest.approx(10e-3)
    assert rec.current_min[1] == pytest.approx(SLEEP_A)
    assert rec.current_avg[1] == pytest.approx((49 * SLEEP_A + 10e-3) / 50)
    assert rec.voltage_avg[0] == pytest.approx(VOLTS)
    assert rec.frame_s == pytest.approx(0.001)


def test_select_window_and_summarize_energy():
    rec = recording_from([1e-3] * 500 + [2e-3] * 500)

    second_half = analysis.select(rec, 0.5, 1.0)
    stats = analysis.summarize(rec, second_half)

    assert second_half.rows == 500
    assert stats["current_a"]["avg"] == pytest.approx(2e-3)
    assert stats["energy_j"] == pytest.approx(2e-3 * VOLTS * 0.5, rel=1e-5)
    assert stats["coverage"] == pytest.approx(1.0)


def test_select_rejects_empty_or_out_of_range_windows():
    rec = recording_from([1e-3] * 100)
    with pytest.raises(analysis.AnalysisError, match="Empty window"):
        analysis.select(rec, 0.5, 0.6)


def test_missing_frames_lower_window_coverage_but_not_the_timeline():
    rec = recording_from([1e-3] * 100, skip=set(range(40, 60)))

    stats = analysis.summarize(rec, analysis.select(rec))

    assert analysis.recording_length_s(rec) == pytest.approx(0.1)
    assert stats["coverage"] == pytest.approx(0.8)


@pytest.mark.parametrize(("duration", "expected"), [(1.0, 0.1), (3.0, 0.5), (60.0, 10.0), (0.02, 0.002)])
def test_nice_interval_gives_about_ten_steps(duration, expected):
    assert analysis.nice_interval(duration) == pytest.approx(expected)


def test_intervals_break_down_the_window():
    rec = recording_from([1e-3] * 500 + [3e-3] * 500)

    rows = analysis.intervals(rec, analysis.select(rec), interval_s=0.25)

    assert [round(r["current_avg_a"] * 1e3, 6) for r in rows] == [1.0, 1.0, 3.0, 3.0]
    with pytest.raises(analysis.AnalysisError, match="too many"):
        analysis.intervals(rec, analysis.select(rec), interval_s=0.001)


def test_bursts_found_with_auto_threshold():
    rec = recording_from([periodic_bursts(i / 1000) for i in range(1000)])

    found = analysis.bursts(rec, analysis.select(rec))

    assert found["threshold_source"] == "auto"
    assert SLEEP_A < found["threshold_a"] < BURST_A
    assert found["count"] == 10
    assert found["per_second"] == pytest.approx(10)
    assert found["duration_ms"]["avg"] == pytest.approx(4)
    assert found["peak_a"]["max"] == pytest.approx(BURST_A)
    assert found["baseline_current_a"] == pytest.approx(SLEEP_A, rel=1e-3)
    assert found["duty_cycle"] == pytest.approx(0.04)
    assert found["share_of_charge"] > 0.99
    assert found["listed"][0] == pytest.approx(
        {"start_s": 0.0, "duration_ms": 4.0, "peak_a": BURST_A, "avg_a": BURST_A,
         "charge_c": BURST_A * 0.004, "energy_j": BURST_A * VOLTS * 0.004}, rel=1e-5
    )


def test_steady_load_has_no_bursts():
    rec = recording_from([3.3e-3 + (i % 3) * 1e-6 for i in range(500)])

    found = analysis.bursts(rec, analysis.select(rec))

    assert found["count"] == 0
    assert found["baseline_current_a"] == pytest.approx(3.301e-3, rel=1e-3)


def test_close_bursts_merge_and_distant_ones_do_not():
    pattern = [SLEEP_A] * 10 + [BURST_A] * 3 + [SLEEP_A] * 1 + [BURST_A] * 3 + [SLEEP_A] * 10 + [BURST_A] * 3 + [SLEEP_A] * 10
    rec = recording_from(pattern)

    found = analysis.bursts(rec, analysis.select(rec), merge_gap_s=0.002)

    assert found["count"] == 2
    assert found["listed"][0]["duration_ms"] == pytest.approx(7)
    merged_charge = 6 * BURST_A * 0.001 + SLEEP_A * 0.001
    assert found["listed"][0]["charge_c"] == pytest.approx(merged_charge, rel=1e-5)


def test_sub_millisecond_spike_is_caught_by_frame_maximum():
    spike = [SLEEP_A] * 49 + [10e-3]
    rec = recording_from([SLEEP_A] * 50 + [spike] + [SLEEP_A] * 50)

    found = analysis.bursts(rec, analysis.select(rec))

    assert found["count"] == 1
    assert found["listed"][0]["peak_a"] == pytest.approx(10e-3)
    assert found["listed"][0]["duration_ms"] == pytest.approx(1)  # 1 ms resolution
    assert found["listed"][0]["charge_c"] == pytest.approx((49 * SLEEP_A + 10e-3) / 50 * 0.001, rel=1e-5)


def test_given_threshold_is_used_and_listing_is_capped():
    rec = recording_from([periodic_bursts(i / 1000, period_s=0.01, width_s=0.002) for i in range(1000)])

    found = analysis.bursts(rec, analysis.select(rec), threshold_a=1e-3)

    assert found["threshold_source"] == "given"
    assert found["count"] == 100
    assert len(found["listed"]) == analysis.MAX_BURSTS_LISTED
    assert found["listed_note"] == "first 20 of 100 bursts"


# -- plots ---------------------------------------------------------------------------


@pytest.mark.parametrize("log_scale", [None, True, False])
def test_plot_renders_png(log_scale):
    rec = recording_from([periodic_bursts(i / 1000) for i in range(3000)])
    window = analysis.select(rec)
    found = analysis.bursts(rec, window)

    png = render_png(rec, window, title="test", log_scale=log_scale, threshold_a=found["threshold_a"], bursts=found["listed"])

    assert png[:8] == PNG_MAGIC
    assert len(png) > 10_000


def test_plot_handles_long_recordings_and_gaps():
    rec = recording_from([periodic_bursts(i / 1000) for i in range(20_000)], skip=set(range(5000, 6000)))

    started = time.monotonic()
    png = render_png(rec, analysis.select(rec, 2.0, 18.0), title="long")

    assert png[:8] == PNG_MAGIC
    assert time.monotonic() - started < 10


# -- bench: background recordings against the simulated Ace ----------------------


class Rig:
    def __init__(self, profile=None, capture_dir=None) -> None:
        self.ace = FakeAce(load_ohm=1000.0, profile=profile)
        self.transports: list[FakeAceTransport] = []
        self.bench = OtiiBench(Config(capture_dir=capture_dir), open_device=self._open)

    def _open(self, _config: Config) -> AceDevice:
        transport = FakeAceTransport(self.ace)
        self.transports.append(transport)
        return AceDevice(transport)


@pytest.fixture
def rig(tmp_path):
    harness = Rig(profile=periodic_bursts, capture_dir=str(tmp_path))
    harness.bench.power_on(VOLTS)
    yield harness
    harness.bench.close()
    harness.ace.stop()


def test_background_recording_start_status_stop(rig):
    started = rig.bench.start_recording(max_seconds=30)
    assert started["recording"] is True
    time.sleep(1.05)
    status = rig.bench.recording_status()

    report = rig.bench.stop_recording()

    assert status["recording"] is True and status["elapsed_s"] >= 1.0
    assert status["auto_stop_in_s"] < 30
    assert report["sample_rate_hz"] == 50_000
    assert report["duration_s"] == pytest.approx(1.05, abs=0.1)
    assert report["bursts"]["count"] in (10, 11, 12)
    assert report["bursts"]["duration_ms"]["avg"] == pytest.approx(4, abs=1)
    assert report["bursts"]["baseline_current_a"] == pytest.approx(SLEEP_A, rel=0.05)
    assert len(report["intervals"]) >= 5
    assert "bursts above" in report["summary"]
    assert rig.ace.streaming is False
    assert rig.bench.recording_status()["recording"] is False


def test_output_control_keeps_working_while_recording(rig):
    rig.bench.start_recording(max_seconds=30)
    time.sleep(0.3)

    off = rig.bench.power_off()
    reading = rig.bench.read()
    rig.bench.stop_recording()

    assert off["output_on"] is False
    assert reading["output_on"] is False


def test_measure_and_second_start_are_refused_while_recording(rig):
    rig.bench.start_recording(max_seconds=30)
    time.sleep(0.2)
    try:
        with pytest.raises(BenchError, match="background recording is running"):
            rig.bench.measure(0.2)
        with pytest.raises(BenchError, match="already running"):
            rig.bench.start_recording()
    finally:
        rig.bench.stop_recording()


def test_recording_stops_itself_at_max_seconds(rig):
    rig.bench.start_recording(max_seconds=0.5)
    time.sleep(1.0)

    status = rig.bench.recording_status()
    report = rig.bench.stop_recording()

    assert status["recording"] is False
    assert status["last_capture"]["auto_stopped"] is True
    assert rig.ace.streaming is False
    assert report["duration_s"] == pytest.approx(0.5, abs=0.1)
    assert "stopped itself at max_seconds" in report["note"]
    with pytest.raises(BenchError, match="No recording is running"):
        rig.bench.stop_recording()


def test_unplug_during_recording_keeps_partial_data(rig):
    rig.bench.start_recording(max_seconds=30)
    time.sleep(0.5)
    rig.transports[-1].unplugged = True
    time.sleep(0.2)

    status = rig.bench.recording_status()
    report = rig.bench.stop_recording()

    assert status["link_ok"] is False
    assert report["samples"] > 10_000
    assert "link to the Ace dropped" in report["note"]
    assert rig.bench.read()["output_on"] is True  # reconnected


def test_analyze_plot_and_save_use_the_last_capture(rig, tmp_path):
    rig.bench.start_recording(max_seconds=30)
    time.sleep(1.0)
    rig.bench.stop_recording()

    zoom = rig.bench.analyze(0.0, 0.2, interval_s=0.05)
    caption, png = rig.bench.plot(0.0, 0.25)
    saved = rig.bench.save()

    assert zoom["duration_s"] == pytest.approx(0.2)
    assert zoom["bursts"]["count"] == 2
    assert len(zoom["intervals"]) == 4
    assert png[:8] == PNG_MAGIC and "bursts above" in caption
    with open(saved["csv"], newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["time_s", "current_avg_a", "current_min_a", "current_max_a", "voltage_avg_v", "power_avg_w"]
    assert len(rows) - 1 == saved["rows"] >= 900
    assert open(saved["png"], "rb").read(8) == PNG_MAGIC
    assert json.loads(open(saved["json"], encoding="utf-8").read())["bursts"]["count"] >= 9
    assert str(tmp_path) in saved["csv"]


def test_measure_report_includes_bursts_and_intervals(rig):
    report = rig.bench.measure(0.5)

    assert report["bursts"]["count"] in (5, 6)
    assert report["intervals"]
    assert rig.bench.analyze()["bursts"]["count"] == report["bursts"]["count"]


def test_analysis_tools_need_a_capture_first():
    harness = Rig()
    try:
        for call in (harness.bench.analyze, harness.bench.plot, harness.bench.save):
            with pytest.raises(BenchError, match="Nothing captured yet"):
                call()
    finally:
        harness.bench.close()
        harness.ace.stop()


def test_close_stops_a_running_recording():
    harness = Rig()
    harness.bench.start_recording(max_seconds=30)
    time.sleep(0.2)

    harness.bench.close()

    assert harness.ace.streaming is False
    harness.ace.stop()


def test_concurrent_stop_and_auto_stop_do_not_clash(rig):
    rig.bench.start_recording(max_seconds=0.3)
    time.sleep(0.29)
    results: list[object] = []

    def stop() -> None:
        try:
            results.append(rig.bench.stop_recording())
        except BenchError as error:
            results.append(error)

    threads = [threading.Thread(target=stop) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    reports = [r for r in results if isinstance(r, dict)]
    assert len(reports) == 1
    assert rig.ace.streaming is False


def test_stopping_before_any_samples_says_so(rig):
    rig.bench.start_recording(max_seconds=30)

    with pytest.raises(BenchError, match="Recording stopped, but no samples arrived"):
        rig.bench.stop_recording()

    assert rig.bench.recording_status()["recording"] is False


def test_auto_threshold_keeps_small_activity_next_to_a_big_burst():
    def trace(frame_no: int) -> float:
        if 2000 <= frame_no < 2200:
            return 0.15  # cellular transmit
        if frame_no % 500 < 3:
            return 0.008  # BLE advertising, 3 ms
        return SLEEP_A

    rec = recording_from([trace(i) for i in range(4000)])

    found = analysis.bursts(rec, analysis.select(rec))

    assert found["threshold_a"] < 1e-3
    assert found["count"] == 8  # 7 advertisements + the transmit burst (which swallows the one at 2000 ms)
    assert found["baseline_current_a"] == pytest.approx(SLEEP_A, rel=1e-3)
    assert found["peak_a"]["max"] == pytest.approx(0.15)

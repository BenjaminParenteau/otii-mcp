"""OtiiBench on the real driver, against a simulated Ace."""

from __future__ import annotations

import threading
import time

import pytest

from otii_mcp.ace import CMD_SET, CMD_SYNC, PROP_CURRENT_LIMIT_A, PROP_OUTPUT, PROP_VOLTAGE_UV, AceDevice
from otii_mcp.bench import BenchError, Config, OtiiBench
from tests.fake_ace import FakeAce, FakeAceTransport


class Harness:
    """A bench whose device factory hands out fresh connections to one simulated Ace."""

    def __init__(self) -> None:
        self.ace = FakeAce()
        self.transports: list[FakeAceTransport] = []
        self.bench = OtiiBench(Config(), open_device=self._open)

    def _open(self, _config: Config) -> AceDevice:
        transport = FakeAceTransport(self.ace)
        self.transports.append(transport)
        return AceDevice(transport)

    def sets(self) -> list[tuple[int, ...]]:
        return [args for command, args in self.ace.commands if command == CMD_SET]


@pytest.fixture
def rig():
    harness = Harness()
    yield harness
    harness.bench.close()
    harness.ace.stop()


def test_read_reports_output_off(rig):
    reading = rig.bench.read()

    assert reading["output_on"] is False
    assert reading["setpoint_v"] == 3.3
    assert reading["summary"].startswith("OFF: ")


def test_power_on_sets_voltage_then_switches_on_and_reports_the_new_state(rig):
    reading = rig.bench.power_on(5.0)

    assert reading["output_on"] is True
    assert reading["voltage_v"] == pytest.approx(5.0)
    assert reading["current_a"] == pytest.approx(0.05)
    assert reading["summary"] == "ON: 50 mA at 5 V = 250 mW"
    commands = [(c, a[:1]) for c, a in rig.ace.commands if c in (CMD_SET, CMD_SYNC)]
    assert commands == [(CMD_SET, (PROP_VOLTAGE_UV,)), (CMD_SYNC, ()), (CMD_SET, (PROP_OUTPUT,))]
    assert rig.sets()[0] == (PROP_VOLTAGE_UV, 5_000_000)


def test_power_on_without_voltage_keeps_the_setpoint(rig):
    reading = rig.bench.power_on()

    assert reading["voltage_v"] == pytest.approx(3.3)
    assert rig.sets() == [(PROP_OUTPUT, 1)]


@pytest.mark.parametrize("voltage", [0, -1, 25.01])
def test_out_of_range_voltage_is_rejected_before_anything_is_sent(rig, voltage):
    with pytest.raises(BenchError, match="voltage_v"):
        rig.bench.power_on(voltage)

    assert rig.ace.commands == []


def test_power_off(rig):
    rig.bench.power_on(3.3)

    reading = rig.bench.power_off()

    assert reading["output_on"] is False
    assert reading["voltage_v"] < 0.01
    assert rig.ace.output_on is False


def test_measure_uses_full_rate_samples(rig):
    rig.bench.power_on(5.0)

    result = rig.bench.measure(1.0)

    assert result["sample_rate_hz"] == 50_000
    assert 40_000 <= result["samples"] <= 55_000
    assert result["coverage"] >= 0.97
    assert result["current_a"]["avg"] == pytest.approx(0.05)
    assert result["voltage_v"]["avg"] == pytest.approx(5.0)
    assert result["power_w"]["avg"] == pytest.approx(0.25)
    assert result["energy_j"] == pytest.approx(0.25)
    assert result["charge_c"] == pytest.approx(0.05)
    assert result["summary"].startswith("avg 50 mA at 5 V = 250 mW over 1 s at 50 kHz")
    assert "% of samples" in result["summary"]


def test_measure_reports_partial_coverage(rig):
    rig.ace.drop_every = 25  # 4 % of frames missing, like the real USB link
    rig.bench.power_on(5.0)

    result = rig.bench.measure(0.5)

    assert 0.90 <= result["coverage"] <= 0.97
    assert result["current_a"]["avg"] == pytest.approx(0.05)
    assert result["energy_j"] == pytest.approx(0.125)


def test_measure_refuses_when_too_few_frames_arrive(rig):
    rig.ace.drop_every = 2
    with pytest.raises(BenchError, match=r"Only \d+% of the Ace.s sample frames arrived"):
        rig.bench.measure(0.3)


def test_power_on_sets_current_limit_before_voltage_and_output(rig):
    reading = rig.bench.power_on(5.0, current_limit_a=0.2)

    sets = [args[0] for args in rig.sets()]
    assert sets == [PROP_CURRENT_LIMIT_A, PROP_VOLTAGE_UV, PROP_OUTPUT]
    assert reading["current_limit_a"] == pytest.approx(0.2)
    assert reading["current_limited"] is False


def test_read_flags_current_limiting(rig):
    reading = rig.bench.power_on(5.0, current_limit_a=0.01)  # 100 ohm load wants 50 mA

    assert reading["current_limited"] is True
    assert reading["voltage_v"] < 1.5
    assert "CURRENT LIMITED at 10 mA" in reading["summary"]


@pytest.mark.parametrize("limit", [0.0005, 5.1])
def test_out_of_range_current_limit_is_rejected_before_anything_is_sent(rig, limit):
    with pytest.raises(BenchError, match="current_limit_a"):
        rig.bench.power_on(3.3, current_limit_a=limit)

    assert rig.ace.commands == []


@pytest.mark.parametrize("seconds", [0, 301])
def test_measure_rejects_out_of_range_duration(rig, seconds):
    with pytest.raises(BenchError, match="seconds"):
        rig.bench.measure(seconds)


def test_power_off_works_during_a_measurement(rig):
    rig.bench.power_on(5.0)
    measuring = threading.Thread(target=rig.bench.measure, args=(1.0,))
    measuring.start()
    time.sleep(0.3)

    started = time.monotonic()
    rig.bench.power_off()
    elapsed = time.monotonic() - started
    measuring.join()

    assert elapsed < 0.5
    assert rig.ace.output_on is False


def test_second_concurrent_measurement_is_refused(rig):
    measuring = threading.Thread(target=rig.bench.measure, args=(0.5,))
    measuring.start()
    time.sleep(0.1)
    try:
        with pytest.raises(BenchError, match="already running"):
            rig.bench.measure(0.1)
    finally:
        measuring.join()


def test_reconnects_after_unplug_and_replug(rig):
    rig.bench.read()
    rig.transports[0].unplugged = True
    time.sleep(0.1)

    reading = rig.bench.power_on(3.3)

    assert reading["output_on"] is True
    assert len(rig.transports) == 2


def test_unplug_during_measurement_is_reported(rig):
    measuring_error: list[Exception] = []

    def run() -> None:
        try:
            rig.bench.measure(1.0)
        except BenchError as error:
            measuring_error.append(error)

    rig.bench.read()
    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.2)
    rig.transports[0].unplugged = True
    thread.join()

    assert measuring_error and "Lost the Ace" in str(measuring_error[0])
    assert rig.bench.read()["output_on"] is False


def test_open_failure_is_a_bench_error():
    from otii_mcp.ace import AceError

    def refuse(_config: Config) -> AceDevice:
        raise AceError("COM10 is busy. Close the Otii desktop app (only one program can use the Ace).")

    bench = OtiiBench(Config(), open_device=refuse)

    with pytest.raises(BenchError, match="Close the Otii desktop app"):
        bench.read()


def test_config_from_env():
    assert Config.from_env({"OTII_SERIAL_PORT": "COM10"}) == Config(serial_port="COM10")
    assert Config.from_env({}) == Config(serial_port=None)

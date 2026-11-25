"""Battery life and brownout estimation."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from otii_mcp.ace import AceDevice, AceError
from otii_mcp.battery import HOURS_PER_YEAR, MODELS, BatteryError, build_model, estimate
from otii_mcp.bench import BenchError, Config, OtiiBench
from otii_mcp.plot import render_battery_png
from tests.fake_ace import FakeAce, FakeAceTransport
from tests.test_recording import BURST_A, PNG_MAGIC, periodic_bursts

FLAT = [(0.0, 3.0), (1.0, 3.0)]
LINEAR = [(0.0, 2.0), (1.0, 3.2)]


def test_all_builtin_models_are_sane():
    for model in MODELS.values():
        assert model.ocv(1.0) > model.ocv(0.5) > model.ocv(0.0) >= model.end_voltage_v * 0.99
        assert model.resistance(0.0) >= model.resistance(1.0) > 0
        assert model.capacity_mah > 0 and model.max_pulse_a > 0


def test_series_and_parallel_scale_voltage_resistance_and_capacity():
    single = build_model("alkaline-aa")
    pack = build_model("alkaline-aa", cells_series=2, cells_parallel=3)

    assert pack.ocv(0.5) == pytest.approx(2 * single.ocv(0.5))
    assert pack.resistance(0.5) == pytest.approx(single.resistance(0.5) * 2 / 3)
    assert pack.total_capacity_mah == pytest.approx(3 * single.capacity_mah)
    assert pack.pack_end_voltage_v == pytest.approx(1.8)


def test_capacity_limited_lifetime_includes_self_discharge():
    model = build_model("cr2032", capacity_mah=100, ocv_curve=FLAT, internal_resistance_ohm=10)

    result = estimate(model, 1e-3, 10e-3, device_min_voltage_v=2.85)

    self_discharge_ma = 100 * 0.01 / HOURS_PER_YEAR
    assert result["limited_by"] == "capacity"
    assert result["usable_fraction"] == pytest.approx(1.0)
    assert result["lifetime_hours"] == pytest.approx(100 / (1 + self_discharge_ma))


def test_brownout_under_peak_ends_life_early():
    model = build_model("cr2032", capacity_mah=100, ocv_curve=LINEAR, internal_resistance_ohm=10)

    # 20 mA x 10 ohm = 0.2 V sag; the 2.6 V cutoff is hit when OCV < 2.8 V, at soc 2/3.
    result = estimate(model, 1e-3, 20e-3, device_min_voltage_v=2.6)

    assert result["limited_by"] == "brownout under peak current"
    assert result["usable_fraction"] == pytest.approx(1 / 3, abs=0.002)
    assert result["usable_capacity_mah"] == pytest.approx(100 / 3, abs=0.2)


def test_voltage_table_reports_sag_at_each_state_of_charge():
    model = build_model("cr2032", ocv_curve=LINEAR, internal_resistance_ohm=10)

    result = estimate(model, 1e-3, 20e-3, device_min_voltage_v=2.0)
    half = next(row for row in result["voltage_by_soc"] if row["soc"] == 0.5)

    assert half["ocv_v"] == pytest.approx(2.6)
    assert half["under_avg_v"] == pytest.approx(2.59)
    assert half["under_peak_v"] == pytest.approx(2.4)


def test_load_that_no_battery_state_can_carry():
    result = estimate(build_model("cr2032"), 200e-6, 0.15, device_min_voltage_v=1.8)

    assert result["usable_fraction"] == 0
    assert result["lifetime_hours"] == 0
    assert result["summary"].startswith("cr2032 can't power this load")
    assert any("recommended pulse current" in w for w in result["warnings"])
    assert any("Even a full battery" in w for w in result["warnings"])


def test_worst_case_uses_the_single_highest_sample():
    model = build_model("cr2032", ocv_curve=LINEAR, internal_resistance_ohm=10)

    result = estimate(model, 1e-3, 10e-3, worst_peak_a=30e-3, device_min_voltage_v=2.6)

    assert result["worst_case"]["usable_fraction"] < result["usable_fraction"]
    assert "worst case" in result["summary"]


def test_default_cutoff_is_the_pack_end_voltage():
    result = estimate(build_model("alkaline-aa", cells_series=2), 1e-3, 1e-3)

    assert result["cutoff_v"] == pytest.approx(1.8)
    assert result["cutoff_source"] == "battery end voltage"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"name": "nope"}, "Unknown battery"),
        ({"name": "cr2032", "cells_series": 0}, "at least 1"),
        ({"name": "cr2032", "capacity_mah": -1}, "positive"),
        ({"name": "cr2032", "ocv_curve": [(0.5, 3.0)]}, "at least two"),
        ({"name": "cr2032", "ocv_curve": [(0.0, 3.0), (1.5, 3.1)]}, "soc 0-1"),
    ],
)
def test_bad_battery_parameters_are_rejected(kwargs, message):
    with pytest.raises(BatteryError, match=message):
        build_model(**kwargs)


def test_zero_average_current_is_rejected():
    with pytest.raises(BatteryError, match="positive"):
        estimate(build_model("cr2032"), 0.0, 0.0)


def test_battery_plot_renders():
    model = build_model("cr2032")
    result = estimate(model, 20e-6, 12e-3, device_min_voltage_v=2.5)

    assert render_battery_png(model, result)[:8] == PNG_MAGIC


# -- through the bench -----------------------------------------------------------------


def _no_device(_config: Config) -> AceDevice:
    raise AceError("no Ace connected")


def test_given_currents_need_no_ace():
    bench = OtiiBench(Config(), open_device=_no_device)

    result = bench.battery_estimate({"name": "alkaline-aa", "cells_series": 2}, {"avg_current_a": 1e-3, "peak_current_a": 0.1})

    assert result["load"]["source"] == {"kind": "given"}
    assert result["lifetime_days"] > 90
    caption, png = bench.battery_plot({"name": "alkaline-aa", "cells_series": 2}, {"avg_current_a": 1e-3})
    assert png[:8] == PNG_MAGIC and "alkaline-aa" in caption


def test_peak_without_average_is_refused():
    bench = OtiiBench(Config(), open_device=_no_device)
    with pytest.raises(BenchError, match="avg_current_a together with peak_current_a"):
        bench.battery_estimate({"name": "cr2032"}, {"peak_current_a": 0.01})


def test_capture_is_needed_when_no_currents_are_given():
    bench = OtiiBench(Config(), open_device=_no_device)
    with pytest.raises(BenchError, match="Nothing captured yet"):
        bench.battery_estimate({"name": "cr2032"}, {})


def test_estimate_from_a_measured_capture():
    ace = FakeAce(load_ohm=1000.0, profile=periodic_bursts)
    bench = OtiiBench(Config(), open_device=lambda _c: AceDevice(FakeAceTransport(ace)))
    try:
        bench.power_on(3.3)
        bench.measure(1.0)

        result = bench.battery_estimate({"name": "cr2032"}, {"device_min_voltage_v": 2.0})
    finally:
        bench.close()
        ace.stop()

    avg = result["load"]["avg_current_a"]
    assert avg == pytest.approx(0.04 * BURST_A, rel=0.1)  # 4 ms of 20 mA every 100 ms
    assert result["load"]["peak_current_a"] == pytest.approx(BURST_A)
    assert result["load"]["source"]["kind"] == "measure"
    assert any(w.startswith("The capture is only") for w in result["warnings"])
    assert any("recommended pulse current" in w for w in result["warnings"])


# -- over MCP, with no Ace attached --------------------------------------------------


def test_battery_tools_over_stdio_without_hardware():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "otii_mcp.server"],
        env={"OTII_SERIAL_PORT": "COM250"},
        cwd=str(Path(__file__).resolve().parent.parent),
    )

    async def scenario():
        async with Client(params) as client:
            estimate_result = await client.call_tool(
                "otii_battery_estimate",
                {"battery": "cr2032", "avg_current_a": 20e-6, "peak_current_a": 8e-3, "device_min_voltage_v": 1.8},
            )
            plot_result = await client.call_tool(
                "otii_battery_plot", {"battery": "li-ion-18650", "avg_current_a": 0.01, "peak_current_a": 0.3}
            )
            bad = await client.call_tool("otii_battery_estimate", {"battery": "unobtainium", "avg_current_a": 1e-3})
            return estimate_result, plot_result, bad

    estimate_result, plot_result, bad = asyncio.run(scenario())

    assert not estimate_result.is_error
    assert 400 < estimate_result.structured_content["lifetime_days"] < 500
    assert [block.type for block in plot_result.content] == ["text", "image"]
    assert bad.is_error

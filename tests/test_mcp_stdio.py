"""End to end: launch the MCP server over stdio, as Claude Code does, against a simulated Ace."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from tests.fake_ace import FakeAce, FakeAceSocketServer

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def ace_server():
    server = FakeAceSocketServer(FakeAce())
    yield server
    server.stop()


def _params(serial_port: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "otii_mcp.server"],
        env={"OTII_SERIAL_PORT": serial_port},
        cwd=str(PROJECT_ROOT),
    )


def test_tools_over_stdio(ace_server):
    async def scenario():
        async with Client(_params(ace_server.url)) as client:
            tools = await client.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            on = await client.call_tool("otii_power_on", {"voltage_v": 3.3})
            read = await client.call_tool("otii_read", {})
            measured = await client.call_tool("otii_measure", {"seconds": 0.5})
            off = await client.call_tool("otii_power_off", {})
            bad = await client.call_tool("otii_power_on", {"voltage_v": 30})
            return names, on, read, measured, off, bad

    names, on, read, measured, off, bad = asyncio.run(scenario())

    assert names == ["otii_measure", "otii_power_off", "otii_power_on", "otii_read"]
    assert not on.is_error and on.structured_content["output_on"] is True
    assert read.structured_content["current_a"] == pytest.approx(0.033)
    assert measured.structured_content["power_w"]["avg"] == pytest.approx(3.3 * 3.3 / 100)
    assert measured.structured_content["sample_rate_hz"] == 50_000
    assert off.structured_content["output_on"] is False
    assert bad.is_error
    assert ace_server.ace.output_on is False


def test_missing_ace_is_a_tool_error_not_a_crash():
    async def scenario():
        async with Client(_params("COM250")) as client:
            return await client.call_tool("otii_read", {})

    result = asyncio.run(scenario())

    assert result.is_error
    assert "Cannot open COM250" in result.content[0].text

# otii-mcp

An unofficial MCP server for the Otii Ace power analyzer. It switches the main
output on and off, sets the current limit, measures and records power at
50 kHz, finds activity bursts, and plots the waveform as an image the AI can
see. It talks to the Ace directly over USB, so it works without the Otii
software running.

> **Unofficial.** This project is not affiliated with, endorsed by or supported
> by Qoitech AB. "Otii" and "Qoitech" are trademarks of Qoitech AB and are used
> here only to say which hardware this works with. For supported automation,
> use Qoitech's own Otii software and toolboxes.
>
> **Use at your own risk.** The software controls a power supply connected to
> your hardware. A bug, a firmware change or a misused tool call can put the
> wrong voltage or current on a device. It comes with no warranty (see
> [LICENSE](LICENSE)). Set a current limit and check readings before connecting
> anything valuable.

| Tool | What it does |
| --- | --- |
| `otii_power_on(voltage_v?, current_limit_a?)` | Optionally sets the current limit (1 mA-5 A) and voltage (0-25 V), then turns the output on |
| `otii_power_off()` | Turns the output off |
| `otii_read()` | Latest 25 Hz reading: on/off, setpoint, current limit and whether it is limiting, current, voltage, power |
| `otii_measure(seconds=5, ...)` | Records 50 kHz samples for N seconds (max 300) and returns a report (below) |
| `otii_record_start(max_seconds=600)` | Starts a background recording and returns at once; other tools keep working. Stops itself at `max_seconds` (max 3600) |
| `otii_record_status()` | Elapsed time and averages so far |
| `otii_record_stop(...)` | Stops the recording and returns a report |
| `otii_analyze(start_s?, end_s?, ...)` | Re-analyzes the last capture, or a time window of it |
| `otii_plot(start_s?, end_s?, log_scale?)` | Returns a PNG of the last capture or a window: current band (min-max) and average, burst threshold and bursts, voltage below |
| `otii_save(directory?, start_s?, end_s?)` | Writes CSV (1 ms rows), PNG and JSON summary. Default folder: `OTII_CAPTURE_DIR` or `~/otii-captures` |

A **report** has exact statistics over every 50 kHz sample (average, minimum and
maximum current, voltage and power, plus energy, charge and coverage), an
`intervals` breakdown (about 10 by default, or every `interval_s`), and
`bursts`:

- the sleep/idle baseline current between bursts
- burst count and rate
- duration (1 ms resolution)
- peak current, charge and energy, and the share of energy spent in bursts
- the first 20 bursts listed individually

The burst threshold is automatic unless you pass `burst_threshold_a`: the
median current plus the largest of 50 µA, 8× its noise and 5% of it. Bursts
less than 2 ms apart are merged.

A typical agent loop: `otii_record_start`, flash firmware or send a command
to the device, `otii_record_stop`, then `otii_plot` and
`otii_analyze(start_s, end_s)` to zoom into whatever looks odd.

## Limits

- `otii_measure` uses the same 50 kHz sample stream the Otii app records,
  normally with 100% of samples (verified up to 30 s, 1.5 M samples).
- **It keeps one CPU core busy while recording**, and only then. The Ace
  holds samples only briefly. If the PC is idle, the CPU drops into deep
  sleep states, its response to each USB transfer slows, and the Ace starts
  skipping frames (5-10% lost, measured). Sub-millisecond sleeps,
  efficiency-mode opt-out, a 1 ms timer and extra overlapped reads didn't
  fix it, but a busy core did, every time. The Otii app gets the same effect
  by being a busy foreground app.
  That includes background recordings, for as long as they run, which is
  why they stop themselves at `max_seconds`.
- Each result reports `coverage`: samples received compared with samples due
  for the time streamed. Below 80% a measurement is refused; a background
  recording returns its result with a warning. Any missing frames are timing
  gaps unrelated to the load, so averages stay valid, but a spike inside a gap
  wouldn't show in max.
- Raw 50 kHz samples aren't kept. Each 1 ms frame is stored as current
  average, minimum and maximum, plus average voltage and power. That's about
  24 KB per second (86 MB for an hour). Overall statistics are exact over
  every sample; windowed analysis, plots and CSV files work at 1 ms, so
  spike heights survive but sub-millisecond timing doesn't.
- The recording and analysis tools (`otii_record_*`, `otii_analyze`,
  `otii_plot`, `otii_save`) and burst detection are tested against a
  simulated Ace and synthetic traces, not yet on hardware.
- `otii_read` is a single 25 Hz telemetry value, fine for steady loads only.
- The current limit doesn't switch the output off. At the limit the Ace
  holds the current (just under the set value) and lets the voltage sag, like a
  bench supply. `otii_read` reports this as `current_limited`.
- Current was checked on a nominal 1 kΩ resistor at 1, 2, 3.3 and 5 V
  (2026-11-25): current was linear to within 0.1 µA over 1-5 mA and implied
  988.5 Ω, which is within a typical resistor tolerance. The resistor wasn't
  measured independently, so absolute gain is confirmed only to about ±1-5%.
  Zero offset is a few hundred nA negative (about -0.1 µA with the output off,
  -0.3 µA from the fit), so treat sub-µA sleep currents with care.
- Only one program can use the Ace at a time. Close the Otii app while the MCP
  server is talking to it.
- The Ace has no host watchdog: the output stays as it is when the MCP server
  exits or the cable's host side goes away. Turn it off explicitly.

## Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `OTII_SERIAL_PORT` | auto | COM port of the Ace. By default the Ace is found by its USB id (`0FCE:D1E6`) |
| `OTII_CAPTURE_DIR` | `~/otii-captures` | Where `otii_save` writes files when no directory is given |

## Install

Needs Python 3.11+ and the Ace on USB.

**To just use it**, install straight from GitHub with pipx. This puts an
`otii-mcp` command on your PATH (open a new terminal after `ensurepath`):

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
py -m pipx install git+https://github.com/BenjaminParenteau/otii-mcp.git
```

To update later, run `py -m pipx upgrade otii-mcp`, then restart the server in
your MCP client.

**To work on it**, clone the repo and install it editable. In the client setup
below, use `<repo>\.venv\Scripts\python.exe` with the arguments
`-m otii_mcp.server` in place of `otii-mcp`:

```powershell
git clone https://github.com/BenjaminParenteau/otii-mcp.git
cd otii-mcp
py -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Only tested on Windows. pyserial and the USB-id lookup should also work on
Linux and macOS (on Linux the user needs serial-port access, e.g. the
`dialout` group).

## Connect to Claude Code

```powershell
claude mcp add --scope user otii '--' otii-mcp
```

Restart the server from `/mcp` after an update.

## Connect to GitHub Copilot

These steps follow GitHub's and VS Code's documentation as of November 2026.
They haven't been tested with this server yet; the Claude Code setup has.

**Copilot Business or Enterprise:** an organization admin must enable the
"MCP servers in Copilot" policy first. It's off by default. Free, Pro and Pro+
plans aren't affected.

**VS Code (Copilot Chat) and Copilot CLI** share one user-level file,
`~/.copilot/mcp-config.json` (on Windows,
`C:\Users\<you>\.copilot\mcp-config.json`). Create it, or add `otii` to the
existing `mcpServers`:

```json
{
  "mcpServers": {
    "otii": {
      "type": "stdio",
      "command": "otii-mcp",
      "args": [],
      "tools": ["*"]
    }
  }
}
```

To pin the Ace to a port instead of auto-detecting it, add
`"env": { "OTII_SERIAL_PORT": "COM10" }`.

Instead of editing the file, you can use the guided flows:

- **VS Code:** run **MCP: Add Server** from the Command Palette, choose
  **Command (stdio)**, enter `otii-mcp`, use `otii` as the server ID, and save it
  to **Global** (Copilot Global). Then open Copilot Chat, switch to **Agent**
  mode, approve the server's startup when asked, and check that the `otii_*`
  tools are ticked in the tools picker.
- **Copilot CLI:** run `copilot mcp add otii -- otii-mcp` in a terminal, or
  `/mcp add` inside a session (Server Type **STDIO**, Command `otii-mcp`, then
  **Ctrl+S**). `copilot mcp list` shows it.

**Visual Studio 2022 (17.14+) and JetBrains IDEs** still use their own
`mcp.json` with a `servers` key. Open it from Copilot Chat in Agent mode via the
tools icon (Visual Studio: **+** > Configure MCP server; JetBrains: **Add MCP
Tools**), and add:

```json
{
  "servers": {
    "otii": {
      "type": "stdio",
      "command": "otii-mcp",
      "args": []
    }
  }
}
```

VS Code also still reads the older `.vscode/mcp.json` in this `servers` format,
but it's deprecated in favour of the `mcpServers` file above.

Only one program can use the Ace at a time. If both Claude Code and Copilot
have the server configured, the first one to call a tool holds the port until
its server restarts.

## Checks

```powershell
.venv\Scripts\python.exe -m pytest
```

Tests need no hardware: `tests/fake_ace.py` simulates an Ace speaking the same
protocol, and `tests/test_ace_protocol.py` checks the encoder and parser against
bytes captured from a real Ace.

With the Ace connected, a ~1 kΩ resistor (or nothing) on its output, and the
Otii app closed (it briefly sets a 2 mA limit to show limiting, then restores
the previous limit):

```powershell
.venv\Scripts\python.exe tools\hw_check.py 3.3
```

## Protocol

Qoitech doesn't publish the Ace's USB protocol. The details below were
determined by observing the USB traffic between an Ace (firmware 3.2.5) and
the Otii 3 desktop app (3.7.6) during normal use, so the instrument can be
used from other software. The Otii software itself was not decompiled or
disassembled, and this repository contains none of its code. Other firmware
versions may differ; `tools/hw_check.py` is a quick way to check.

The Ace is a USB CDC serial device. Open it with DTR and RTS low; the baud
rate doesn't matter. Everything is little-endian.

```
frame:   a3 2c b5 7f | u16 length | u16 checksum (sum of payload bytes) | payload
command: u32 seq | u32 cmd | u32 args...
reply:   0e 03 99 ff | u32 seq | u32 status (0 ok, 0xfffffffd not supported) | data
stream:  ce f2 2f ff | u32 0 | records (u16 type | u16 len | body)
```

| Command | Meaning |
| --- | --- |
| `0x14` | Hello, sent first by the app |
| `0x64 id 0` | Get property |
| `0x66 id value` | Set property |
| `0x68` | Barrier; the app sends it after a voltage or limit change (answers in ~70 ms) |
| `0x78 ch 1/0` | Enable/disable a sample channel (0 current, 1 voltage, 6 power) |
| `0x7c` / `0x7e` | Start / stop the sample stream |
| `0x0a lo hi` | Host clock in microseconds, once a second (not needed to keep the output on) |

| Property | Meaning |
| --- | --- |
| `0x09` | Main output: 1 on, 0 off. A change triggers a type-1 stream record |
| `0x0b` | Main voltage setpoint, microvolts |
| `0x100c` | Current limit, f32 amps (the Ace also reports it as `0x0c`, whole mA) |

Stream records of type 2 are telemetry, `u32 id | f32 value`, about 25 per
second: `0x00` main current (A), `0x01` main voltage (V), `0x06` main power (W).
Telemetry starts as soon as the port is opened.

While streaming, sample frames arrive about 1000 times a second, each followed
by an empty status frame:

```
samples: 69 83 2a ff | u32 frame counter | blocks: u16 channel | u16 count (50) | u32 rate (50000) | f32[count]
```

The app also enables channel `0x17` at connect, which adds an empty block
(count 0) at the end of each frame. It isn't needed.

`tools/decode_pcap.py` decodes tshark exports of USBPcap captures, for
working out more of the protocol.

## License

MIT, see [LICENSE](LICENSE).

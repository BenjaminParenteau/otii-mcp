# otii-mcp

An unofficial MCP server for the Otii Ace power analyzer. It switches the main
output on and off, sets the current limit, and measures power at 50 kHz. It
talks to the Ace directly over USB, so it works without the Otii software
running.

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
| `otii_measure(seconds=5)` | Records 50 kHz samples for N seconds (max 300): avg/min/max current, voltage, power, energy (J), charge (C), and coverage |

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
- Each result reports `coverage`: samples received compared with samples due
  for the time streamed. Below 80% the result is refused. Any missing frames
  are timing gaps unrelated to the load, so averages stay valid, but a spike
  inside a gap wouldn't show in max.
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

## Install and register

Needs Python 3.11+ and the Ace on USB.

**To just use it**, install straight from GitHub with pipx:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
py -m pipx install git+https://github.com/BenjaminParenteau/otii-mcp.git
claude mcp add --scope user otii '--' otii-mcp
```

To update later, run `py -m pipx upgrade otii-mcp`, then restart the `otii`
server in Claude Code (`/mcp`).

**To work on it**, clone the repo and install it editable:

```powershell
git clone https://github.com/BenjaminParenteau/otii-mcp.git
cd otii-mcp
py -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
claude mcp add --scope user otii '--' "$PWD\.venv\Scripts\python.exe" -m otii_mcp.server
```

Only tested on Windows. pyserial and the USB-id lookup should also work on
Linux and macOS (on Linux the user needs serial-port access, e.g. the
`dialout` group).

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

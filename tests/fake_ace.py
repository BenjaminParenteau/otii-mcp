"""A simulated Otii Ace speaking the same wire protocol as the real one.

Replies, events, telemetry and sample frames use the exact layouts seen in
captures of a real Ace (see tests/data). The load on the output is a plain
resistor, and the current limit behaves like the real one: current is held at
the limit and the voltage sags.
"""

from __future__ import annotations

import queue
import socket
import struct
import threading
import time

from otii_mcp.ace import (
    CMD_CHANNEL,
    CMD_GET,
    CMD_HELLO,
    CMD_SET,
    CMD_STREAM_START,
    CMD_STREAM_STOP,
    CMD_SYNC,
    CMD_TIME_SYNC,
    PROP_CURRENT_LIMIT_A,
    PROP_OUTPUT,
    PROP_VOLTAGE_UV,
    REPLY_TAG,
    SAMPLES_TAG,
    STATUS_NOT_SUPPORTED,
    STREAM_TAG,
    FrameParser,
    encode_frame,
)

# Real hello reply body, from a capture.
HELLO_DATA = bytes.fromhex("0300000020800000206563413f000000")
PROP_CURRENT_LIMIT_MA = 0x0C
SAMPLES_PER_BLOCK = 50
SAMPLE_RATE = 50_000


def _f32_bits(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


def _f32(bits: int) -> float:
    return struct.unpack("<f", struct.pack("<I", bits))[0]


class FakeAce:
    def __init__(self, *, load_ohm: float = 100.0, telemetry_hz: float = 25.0) -> None:
        self.output_on = False
        self.voltage_uv = 3_300_000
        self.current_limit_a = 0.5
        self.load_ohm = load_ohm
        self.commands: list[tuple[int, tuple[int, ...]]] = []
        self.silent = False  # stop answering commands (telemetry continues)
        self.drop_every = 0  # when > 0, skip every Nth sample frame (counter still advances)
        self.enabled_channels: list[int] = []
        self.streaming = False
        self._frame_counter = 0
        self._parser = FrameParser()
        self._outbox: queue.SimpleQueue[bytes] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._period = 1.0 / telemetry_hz
        threading.Thread(target=self._telemetry_loop, daemon=True).start()
        threading.Thread(target=self._sample_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def output(self) -> tuple[float, float]:
        """(volts, amps) on the output, honouring the current limit."""
        if not self.output_on:
            return 0.0047, -1.2e-7
        volts = self.voltage_uv / 1e6
        amps = volts / self.load_ohm
        if amps > self.current_limit_a:
            amps = self.current_limit_a * 0.98  # the real Ace regulates just under the limit
            volts = amps * self.load_ohm
        return volts, amps

    # Byte pipe, host side.
    def host_write(self, data: bytes) -> None:
        for payload in self._parser.feed(data):
            seq, command = struct.unpack_from("<II", payload)
            args = struct.unpack_from(f"<{(len(payload) - 8) // 4}I", payload, 8)
            with self._lock:
                self.commands.append((command, args))
                if not self.silent:
                    self._handle(seq, command, args)

    def host_read(self, timeout: float) -> bytes:
        try:
            chunk = self._outbox.get(timeout=timeout) if timeout > 0 else self._outbox.get_nowait()
        except queue.Empty:
            return b""
        while not self._outbox.empty():
            chunk += self._outbox.get()
        return chunk

    def _handle(self, seq: int, command: int, args: tuple[int, ...]) -> None:
        if command == CMD_HELLO:
            self._reply(seq, 0, HELLO_DATA)
        elif command in (CMD_TIME_SYNC, CMD_SYNC):
            self._reply(seq, 0, b"")
        elif command == CMD_CHANNEL:
            channel, enable = args[0], args[1]
            if enable and channel not in self.enabled_channels:
                self.enabled_channels.append(channel)
            elif not enable and channel in self.enabled_channels:
                self.enabled_channels.remove(channel)
            self._reply(seq, 0, b"")
        elif command == CMD_STREAM_START:
            self.streaming, self._frame_counter = True, 0
            self._reply(seq, 0, b"")
        elif command == CMD_STREAM_STOP:
            self.streaming = False
            self._reply(seq, 0, b"")
        elif command == CMD_GET:
            value = {
                PROP_OUTPUT: int(self.output_on),
                PROP_VOLTAGE_UV: self.voltage_uv,
                PROP_CURRENT_LIMIT_A: _f32_bits(self.current_limit_a),
                PROP_CURRENT_LIMIT_MA: round(self.current_limit_a * 1000),
            }.get(args[0])
            if value is None:
                self._reply(seq, STATUS_NOT_SUPPORTED, b"")
            else:
                self._reply(seq, 0, struct.pack("<I", value))
        elif command == CMD_SET:
            prop, value = args[0], args[1]
            if prop == PROP_OUTPUT:
                self.output_on = bool(value)
                self._event((prop, value))
            elif prop == PROP_VOLTAGE_UV:
                self.voltage_uv = value
            elif prop == PROP_CURRENT_LIMIT_A:
                self.current_limit_a = _f32(value)
                self._event((PROP_CURRENT_LIMIT_MA, round(self.current_limit_a * 1000)), (prop, value))
            else:
                self._reply(seq, STATUS_NOT_SUPPORTED, b"")
                return
            self._reply(seq, 0, struct.pack("<I", value))
        else:
            self._reply(seq, STATUS_NOT_SUPPORTED, b"")

    def _reply(self, seq: int, status: int, data: bytes) -> None:
        self._outbox.put(encode_frame(REPLY_TAG + struct.pack("<II", seq, status) + data))

    def _event(self, *changes: tuple[int, int]) -> None:
        records = b"".join(struct.pack("<HHII", 1, 8, prop, value) for prop, value in changes)
        self._outbox.put(encode_frame(STREAM_TAG + b"\0\0\0\0" + records))

    def _telemetry_loop(self) -> None:
        while not self._stop.wait(self._period):
            with self._lock:
                volts, amps = self.output()
            records = struct.pack("<HHIII", 2, 12, 0, 1, 0)  # leading record seen in real packets
            for telemetry_id, value in ((0, amps), (1, volts), (6, amps * volts), (0x14, 30.0)):
                records += struct.pack("<HHIf", 2, 8, telemetry_id, value)
            self._outbox.put(encode_frame(STREAM_TAG + b"\0\0\0\0" + records))

    def _sample_loop(self) -> None:
        # Real Ace: 1000 frames/s, each followed by an empty status frame.
        # Emitted in batches paced by elapsed time (sleep granularity on
        # Windows is ~15 ms), so the rate holds at 1000/s.
        started: float | None = None
        while not self._stop.wait(0.005):
            with self._lock:
                if not self.streaming:
                    started = None
                    continue
                now = time.monotonic()
                if started is None:
                    started = now
                due = int((now - started) * 1000) - self._frame_counter
                volts, amps = self.output()
                values = {0: amps, 1: volts, 6: amps * volts}
                chunk = b""
                for _ in range(max(0, due)):
                    counter = self._frame_counter
                    self._frame_counter += 1
                    if self.drop_every and counter % self.drop_every == self.drop_every - 1:
                        continue
                    body = b"".join(
                        struct.pack("<HHI", channel, SAMPLES_PER_BLOCK, SAMPLE_RATE)
                        + struct.pack(f"<{SAMPLES_PER_BLOCK}f", *([values.get(channel, 0.0)] * SAMPLES_PER_BLOCK))
                        for channel in self.enabled_channels
                    )
                    chunk += encode_frame(SAMPLES_TAG + struct.pack("<I", counter) + body)
                    chunk += encode_frame(STREAM_TAG + b"\0\0\0\0")
            if chunk:
                self._outbox.put(chunk)


class FakeAceTransport:
    """In-process Transport around a FakeAce, with a switch to simulate unplugging."""

    def __init__(self, ace: FakeAce) -> None:
        self.ace = ace
        self.unplugged = False
        self.closed = False

    def read(self, size: int) -> bytes:
        if self.unplugged or self.closed:
            raise OSError("device disconnected")
        return self.ace.host_read(0.05)

    def write(self, data: bytes) -> int:
        if self.unplugged or self.closed:
            raise OSError("device disconnected")
        self.ace.host_write(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


class FakeAcePollingTransport(FakeAceTransport):
    """Like pyserial: has a `timeout`, so the driver switches to non-blocking reads while streaming."""

    def __init__(self, ace: FakeAce) -> None:
        super().__init__(ace)
        self.timeout: float | None = 0.05
        self.polls = 0  # non-blocking reads

    def read(self, size: int) -> bytes:
        if self.unplugged or self.closed:
            raise OSError("device disconnected")
        if self.timeout == 0:
            self.polls += 1
            return self.ace.host_read(0)
        return self.ace.host_read(self.timeout or 0.05)


class FakeAceSocketServer:
    """Serves a FakeAce on TCP so a subprocess can reach it via OTII_SERIAL_PORT=socket://..."""

    def __init__(self, ace: FakeAce) -> None:
        self.ace = ace
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"socket://127.0.0.1:{self._listener.getsockname()[1]}"
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self) -> None:
        self._listener.close()
        self.ace.stop()

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._pump_out, args=(conn,), daemon=True).start()
            threading.Thread(target=self._pump_in, args=(conn,), daemon=True).start()

    def _pump_in(self, conn: socket.socket) -> None:
        try:
            while data := conn.recv(4096):
                self.ace.host_write(data)
        except OSError:
            pass

    def _pump_out(self, conn: socket.socket) -> None:
        try:
            while True:
                data = self.ace.host_read(0.1)
                if data:
                    conn.sendall(data)
        except OSError:
            pass

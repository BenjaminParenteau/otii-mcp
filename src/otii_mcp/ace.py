"""Direct USB-serial driver for the Otii Ace power analyzer.

Unofficial and not affiliated with Qoitech. The message format was determined
by observing the USB traffic between an Ace (firmware 3.2.5) and the Otii 3
desktop app (3.7.6) during normal use, so that the instrument can be used
from other software. Only what on/off, current limit and power measurement
need is implemented.

Wire format, both directions, little-endian:

    a3 2c b5 7f | u16 length | u16 checksum | payload[length]

where the checksum is the 16-bit sum of the payload bytes.

Host -> Ace payload:  u32 seq | u32 command | u32 args...
Ace -> host reply:    0e 03 99 ff | u32 seq | u32 status (0 = ok) | data...
Ace -> host status:   ce f2 2f ff | u32 0 | records: u16 type | u16 len | body
Ace -> host samples:  69 83 2a ff | u32 frame counter | blocks:
                      u16 channel | u16 count | u32 sample rate | f32 values[count]

Status records of type 2 are telemetry (u32 id, f32 value), about 25 per
second; type 1 are change notifications (u32 id, u32 value). Sample frames
only flow between the stream start and stop commands, about 1000 frames per
second with 50 samples per enabled channel (50 kHz).
"""

from __future__ import annotations

import struct
import threading
import time
from array import array
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Protocol

MAGIC = bytes.fromhex("a32cb57f")
REPLY_TAG = bytes.fromhex("0e0399ff")
STREAM_TAG = bytes.fromhex("cef22fff")
SAMPLES_TAG = bytes.fromhex("69832aff")
HEADER_LEN = 8
MAX_PAYLOAD = 8192

USB_VID = 0x0FCE
USB_PID = 0xD1E6

CMD_TIME_SYNC = 0x0A
CMD_HELLO = 0x14
CMD_GET = 0x64
CMD_SET = 0x66
CMD_SYNC = 0x68
CMD_CHANNEL = 0x78  # args: channel, 1 = enable / 0 = disable
CMD_STREAM_START = 0x7C
CMD_STREAM_STOP = 0x7E

PROP_OUTPUT = 0x09
PROP_VOLTAGE_UV = 0x0B
PROP_CURRENT_LIMIT_A = 0x100C  # f32 amps; the Ace echoes it as 0x0C in whole mA

RECORD_EVENT = 1
RECORD_TELEMETRY = 2

# Ids shared by telemetry records and sample channels.
CHANNEL_CURRENT_A = 0x00
CHANNEL_VOLTAGE_V = 0x01
CHANNEL_POWER_W = 0x06
MAIN_CHANNELS = (CHANNEL_CURRENT_A, CHANNEL_POWER_W, CHANNEL_VOLTAGE_V)  # order the app uses

STATUS_NOT_SUPPORTED = 0xFFFFFFFD
REPLY_TIMEOUT_S = 2.0
TIME_SYNC_PERIOD_S = 1.0
READ_CHUNK = 1 << 16
RX_BUFFER = 1 << 22


class AceError(Exception):
    """The Ace refused a command, stopped answering, or the link dropped."""


class Transport(Protocol):
    """A byte pipe to the Ace: pyserial's Serial fits, and so do test doubles."""

    def read(self, size: int) -> bytes: ...
    def write(self, data: bytes) -> int | None: ...
    def close(self) -> None: ...


def encode_frame(payload: bytes) -> bytes:
    return MAGIC + struct.pack("<HH", len(payload), sum(payload) & 0xFFFF) + payload


class FrameParser:
    """Turns a byte stream into payloads, resyncing on garbage or bad checksums.

    Scans with an index and trims the buffer once per feed, so a 64 KB chunk
    holding a hundred frames costs one copy, not a hundred.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.bad_frames = 0

    def feed(self, data: bytes) -> list[bytes]:
        buffer = self._buffer
        buffer += data
        out: list[bytes] = []
        pos = 0
        end = len(buffer)
        while True:
            start = buffer.find(MAGIC, pos)
            if start < 0:
                # Keep a possible partial magic at the end.
                pos = max(pos, end - (len(MAGIC) - 1))
                break
            if start + HEADER_LEN > end:
                pos = start
                break
            length, checksum = struct.unpack_from("<HH", buffer, start + 4)
            if length > MAX_PAYLOAD:
                self.bad_frames += 1
                pos = start + 1
                continue
            stop = start + HEADER_LEN + length
            if stop > end:
                pos = start
                break
            payload = bytes(buffer[start + HEADER_LEN : stop])
            if sum(payload) & 0xFFFF != checksum:
                self.bad_frames += 1
                pos = start + 1
                continue
            out.append(payload)
            pos = stop
        del buffer[:pos]
        return out


def stream_records(payload: bytes) -> Iterator[tuple[int, bytes]]:
    offset = 8
    while offset + 4 <= len(payload):
        record_type, length = struct.unpack_from("<HH", payload, offset)
        body = payload[offset + 4 : offset + 4 + length]
        if len(body) < length:
            return
        yield record_type, body
        offset += 4 + length


_BLOCK_HEADER = struct.Struct("<HHI")
_FLOATS: dict[int, struct.Struct] = {}


def sample_blocks(payload: bytes) -> Iterator[tuple[int, int, tuple[float, ...]]]:
    """(channel, sample_rate, values) for each block of a sample frame."""
    offset = 8
    size = len(payload)
    while offset + 8 <= size:
        channel, count, rate = _BLOCK_HEADER.unpack_from(payload, offset)
        if count == 0:
            return
        unpacker = _FLOATS.get(count)
        if unpacker is None:
            unpacker = _FLOATS[count] = struct.Struct(f"<{count}f")
        if offset + 8 + unpacker.size > size:
            return
        yield channel, rate, unpacker.unpack_from(payload, offset + 8)
        offset += 8 + unpacker.size


@dataclass(frozen=True)
class Sample:
    """One telemetry update: main current, voltage and power."""

    time: float  # time.monotonic() when received
    current_a: float
    voltage_v: float
    power_w: float


@dataclass
class ChannelStats:
    sample_rate: int
    count: int = 0
    total: float = 0.0
    minimum: float = float("inf")
    maximum: float = float("-inf")

    @property
    def average(self) -> float:
        return self.total / self.count


_NAN = float("nan")


@dataclass
class Recording:
    """Statistics over full-rate samples, plus one row per frame (1 ms) for analysis and plots.

    Raw 50 kHz samples are not kept. Each frame keeps current average/min/max
    (so spikes survive downsampling) and average voltage and power: about
    24 KB per second, 86 MB for an hour.
    """

    channels: dict[int, ChannelStats] = field(default_factory=dict)
    frames: int = 0
    # Gaps in the Ace's frame counter. This undercounts loss: when starved,
    # the Ace also produces fewer frames, so coverage() is time-based instead.
    dropped_frames: int = 0
    first_arrival: float | None = None
    last_arrival: float | None = None
    # Host-side streaming window: stream-start reply to stop command.
    window_s: float | None = None
    # Per-frame rows. frame_index is the Ace's frame counter relative to the
    # first frame, so missing frames leave gaps in time instead of shifting it.
    frame_index: array = field(default_factory=lambda: array("I"))
    current_avg: array = field(default_factory=lambda: array("f"))
    current_min: array = field(default_factory=lambda: array("f"))
    current_max: array = field(default_factory=lambda: array("f"))
    voltage_avg: array = field(default_factory=lambda: array("f"))
    power_avg: array = field(default_factory=lambda: array("f"))
    _first_counter: int | None = None
    _last_counter: int | None = None

    def add_frame(self, payload: bytes, arrival: float | None = None) -> None:
        counter = struct.unpack_from("<I", payload, 4)[0]
        if self._last_counter is not None:
            self.dropped_frames += (counter - self._last_counter - 1) & 0xFFFFFFFF
        else:
            self._first_counter = counter
        self._last_counter = counter
        self.frames += 1
        if arrival is not None:
            if self.first_arrival is None:
                self.first_arrival = arrival
            self.last_arrival = arrival
        per_frame: dict[int, tuple[float, float, float]] = {}
        for channel, rate, values in sample_blocks(payload):
            stats = self.channels.get(channel)
            if stats is None:
                stats = self.channels[channel] = ChannelStats(rate)
            total, low, high = sum(values), min(values), max(values)
            stats.count += len(values)
            stats.total += total
            if low < stats.minimum:
                stats.minimum = low
            if high > stats.maximum:
                stats.maximum = high
            per_frame[channel] = (total / len(values), low, high)
        current = per_frame.get(CHANNEL_CURRENT_A, (_NAN, _NAN, _NAN))
        self.frame_index.append((counter - self._first_counter) & 0xFFFFFFFF)
        self.current_avg.append(current[0])
        self.current_min.append(current[1])
        self.current_max.append(current[2])
        self.voltage_avg.append(per_frame.get(CHANNEL_VOLTAGE_V, (_NAN,))[0])
        self.power_avg.append(per_frame.get(CHANNEL_POWER_W, (_NAN,))[0])

    @property
    def frame_s(self) -> float:
        """Duration of one frame (1 ms on the Ace: 50 samples at 50 kHz)."""
        stats = self.channels.get(CHANNEL_CURRENT_A)
        if stats is None or not self.frames:
            return 0.001
        return stats.count / self.frames / stats.sample_rate

    def coverage(self, channel: int) -> float:
        """Samples received on `channel` / samples the Ace should have produced while streaming.

        The streaming time is the longer of the host-side window (start reply to
        stop command) and the first-to-last frame arrival span, so batching at
        either end can't hide losses. Capped at 1.0 to absorb timing jitter.
        """
        stats = self.channels.get(channel)
        if stats is None or self.first_arrival is None or self.last_arrival is None or self.frames < 2:
            return 0.0
        per_frame_s = stats.count / self.frames / stats.sample_rate
        streamed_s = max(self.last_arrival - self.first_arrival + per_frame_s, self.window_s or 0.0)
        return min(1.0, stats.count / (streamed_s * stats.sample_rate))


class AceDevice:
    """A connected Ace. Thread-safe; a background thread reads the stream."""

    def __init__(self, transport: Transport, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._transport = transport
        self._clock = clock
        self._opened_at = clock()
        self._parser = FrameParser()
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._record_lock = threading.Lock()
        self._seq = 0
        self._pending: dict[int, Future[bytes]] = {}
        self._latest: Sample | None = None
        self._partial: dict[int, float] = {}
        self._recording: Recording | None = None
        self._recording_channels: tuple[int, ...] = ()
        self._recording_started = 0.0
        self._failure: BaseException | None = None
        self._closed = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name="ace-reader", daemon=True)
        self._ticker = threading.Thread(target=self._time_sync_loop, name="ace-time-sync", daemon=True)
        self._reader.start()
        try:
            self._command(CMD_HELLO)
            # A previous session may have died mid-recording; make sure no
            # sample stream is running.
            self._stop_stream(MAIN_CHANNELS)
        except Exception:
            self.close()
            raise
        self._ticker.start()

    # -- public API --------------------------------------------------------

    def close(self) -> None:
        """Stop talking to the Ace. Leaves the output as it is."""
        self._closed.set()
        try:
            self._transport.close()
        finally:
            self._fail_pending(AceError("Connection closed"))

    @property
    def alive(self) -> bool:
        return self._failure is None and not self._closed.is_set()

    def get_output(self) -> bool:
        return bool(self.get_property(PROP_OUTPUT))

    def set_output(self, on: bool) -> None:
        self.set_property(PROP_OUTPUT, 1 if on else 0)

    def get_voltage(self) -> float:
        return self.get_property(PROP_VOLTAGE_UV) / 1e6

    def set_voltage(self, volts: float) -> None:
        self.set_property(PROP_VOLTAGE_UV, round(volts * 1e6))
        # The Otii app always follows a setpoint change with this barrier; it
        # answers once the new value has been applied (~70 ms).
        self._command(CMD_SYNC)

    def get_current_limit(self) -> float:
        return _u32_to_f32(self.get_property(PROP_CURRENT_LIMIT_A))

    def set_current_limit(self, amps: float) -> None:
        """Above this the Ace limits current (the voltage sags); it does not switch off."""
        self.set_property(PROP_CURRENT_LIMIT_A, _f32_to_u32(amps))
        self._command(CMD_SYNC)

    def get_property(self, prop: int) -> int:
        data = self._command(CMD_GET, prop, 0)
        return struct.unpack_from("<I", data)[0]

    def set_property(self, prop: int, value: int) -> None:
        self._command(CMD_SET, prop, value)

    def latest(self, max_age_s: float = 1.0, *, newer_than: float | None = None) -> Sample:
        """The most recent telemetry, waiting up to `max_age_s` for one.

        With `newer_than` (a time.monotonic() value), only a reading received
        after that moment counts, e.g. to see the effect of switching.
        """
        self._raise_if_failed()
        deadline = self._clock() + max_age_s
        while True:
            with self._state_lock:
                sample = self._latest
            fresh = sample is not None and self._clock() - sample.time <= max_age_s
            if fresh and (newer_than is None or sample.time > newer_than):
                return sample
            if self._clock() > deadline:
                raise AceError("The Ace stopped sending readings.")
            time.sleep(0.01)
            self._raise_if_failed()

    @property
    def recording(self) -> Recording | None:
        """The recording in progress, if any (its statistics update live)."""
        return self._recording

    def start_recording(self, channels: Iterable[int] = MAIN_CHANNELS) -> Recording:
        """Start streaming full-rate samples; they accumulate until stop_recording()."""
        channels = tuple(channels)
        if not self._record_lock.acquire(blocking=False):
            raise AceError("A recording is already running.")
        recording = Recording()
        try:
            for channel in channels:
                self._command(CMD_CHANNEL, channel, 1)
            with self._state_lock:
                self._recording = recording
            self._command(CMD_STREAM_START)
        except BaseException:
            with self._state_lock:
                self._recording = None
            if self.alive:
                try:
                    self._stop_stream(channels)
                except AceError:
                    pass
            self._record_lock.release()
            raise
        self._recording_channels = channels
        self._recording_started = self._clock()
        return recording

    def stop_recording(self) -> Recording:
        """Stop the stream and return the finished recording.

        Works after the link has dropped too: the samples received so far are kept.
        """
        recording = self._recording
        if recording is None:
            raise AceError("No recording is running.")
        recording.window_s = self._clock() - self._recording_started
        try:
            if self.alive:
                self._stop_stream(self._recording_channels)
        finally:
            with self._state_lock:
                self._recording = None
            self._record_lock.release()
        return recording

    def record(self, seconds: float, channels: Iterable[int] = MAIN_CHANNELS) -> Recording:
        """Stream full-rate samples for `seconds` and return them."""
        recording = self.start_recording(channels)
        try:
            end = self._clock() + seconds
            while (remaining := end - self._clock()) > 0:
                self._closed.wait(min(0.1, remaining))
                self._raise_if_failed()
        finally:
            self.stop_recording()
        return recording

    # -- protocol ------------------------------------------------------------

    def _stop_stream(self, channels: Iterable[int]) -> None:
        self._command(CMD_STREAM_STOP)
        for channel in channels:
            self._command(CMD_CHANNEL, channel, 0)

    def _command(self, command: int, *args: int, timeout: float = REPLY_TIMEOUT_S) -> bytes:
        self._raise_if_failed()
        future: Future[bytes] = Future()
        with self._write_lock:
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            seq = self._seq
            self._pending[seq] = future
            payload = struct.pack(f"<II{len(args)}I", seq, command, *args)
            try:
                self._transport.write(encode_frame(payload))
            except Exception as error:
                self._pending.pop(seq, None)
                self._mark_failed(error)
                raise AceError(f"Writing to the Ace failed: {error}") from error
        try:
            return future.result(timeout)
        except FutureTimeout:
            raise AceError(f"The Ace did not answer command {command:#04x}.") from None
        finally:
            self._pending.pop(seq, None)

    def _read_loop(self) -> None:
        # While streaming, read without blocking (timeout 0) and spin. Keeping
        # this core busy stops the CPU from entering deep idle states, whose
        # wake-up latency slows the PC's response to each USB transfer enough
        # that the Ace drops 5-10% of its frames (measured on a Windows laptop;
        # sub-ms sleeps did not help, a busy core gave 100%). Costs one core
        # only while recording. Transports without a `timeout` just block.
        can_poll = hasattr(self._transport, "timeout")
        idle_timeout = getattr(self._transport, "timeout", None)
        polling = False
        try:
            while not self._closed.is_set():
                want_poll = can_poll and self._recording is not None
                if want_poll != polling:
                    self._transport.timeout = 0 if want_poll else idle_timeout  # type: ignore[attr-defined]
                    polling = want_poll
                data = self._transport.read(READ_CHUNK)
                if data:
                    for payload in self._parser.feed(data):
                        self._dispatch(payload)
                elif polling:
                    time.sleep(0)
        except Exception as error:
            if not self._closed.is_set():
                self._mark_failed(error)

    def _dispatch(self, payload: bytes) -> None:
        tag = payload[:4]
        if tag == SAMPLES_TAG:
            recording = self._recording
            if recording is not None:
                recording.add_frame(payload, self._clock())
        elif tag == REPLY_TAG and len(payload) >= 12:
            seq, status = struct.unpack_from("<II", payload, 4)
            future = self._pending.get(seq)
            if future is None or future.done():
                return
            if status == 0:
                future.set_result(payload[12:])
            else:
                reason = "not supported" if status == STATUS_NOT_SUPPORTED else f"status {status:#x}"
                future.set_exception(AceError(f"The Ace rejected the command ({reason})."))
        elif tag == STREAM_TAG:
            had_telemetry = False
            for record_type, body in stream_records(payload):
                if record_type == RECORD_TELEMETRY and len(body) == 8:
                    telemetry_id, value = struct.unpack("<If", body)
                    self._partial[telemetry_id] = value
                    had_telemetry = True
            if had_telemetry:
                self._publish_sample()

    def _publish_sample(self) -> None:
        values = self._partial
        if not {CHANNEL_CURRENT_A, CHANNEL_VOLTAGE_V, CHANNEL_POWER_W} <= values.keys():
            return
        sample = Sample(
            time=self._clock(),
            current_a=values[CHANNEL_CURRENT_A],
            voltage_v=values[CHANNEL_VOLTAGE_V],
            power_w=values[CHANNEL_POWER_W],
        )
        with self._state_lock:
            self._latest = sample

    def _time_sync_loop(self) -> None:
        # The Otii app sends its clock (microseconds) once a second. The Ace
        # doesn't need it to stay on, but we mirror the app's behaviour.
        while not self._closed.wait(TIME_SYNC_PERIOD_S):
            if self._failure is not None:
                return
            micros = int((self._clock() - self._opened_at) * 1e6)
            try:
                self._command(CMD_TIME_SYNC, micros & 0xFFFFFFFF, micros >> 32)
            except AceError:
                return

    # -- failure handling ------------------------------------------------------

    def _mark_failed(self, error: BaseException) -> None:
        if self._failure is None:
            self._failure = error
        self._fail_pending(AceError(f"Lost the Ace: {error}"))

    def _fail_pending(self, error: AceError) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    def _raise_if_failed(self) -> None:
        if self._closed.is_set():
            raise AceError("Connection closed")
        if self._failure is not None:
            raise AceError(f"Lost the Ace: {self._failure}")


def _f32_to_u32(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


def _u32_to_f32(value: int) -> float:
    return struct.unpack("<f", struct.pack("<I", value))[0]


def find_ace_port() -> str:
    """The COM port of the single connected Ace, found by its USB id."""
    from serial.tools import list_ports

    ports = [p.device for p in list_ports.comports() if p.vid == USB_VID and p.pid == USB_PID]
    if not ports:
        raise AceError("No Otii Ace found on USB. Check the cable and that it is powered.")
    if len(ports) > 1:
        raise AceError(f"Several Otii devices found ({', '.join(ports)}); set OTII_SERIAL_PORT.")
    return ports[0]


def open_transport(port: str) -> Transport:
    """Open the Ace's serial port the way the Otii app does: DTR and RTS low.

    `port` may also be a pyserial URL such as socket://host:port (used by tests).
    """
    import serial

    link = serial.serial_for_url(port, do_not_open=True, timeout=0.05, write_timeout=2)
    link.dtr = False
    link.rts = False
    try:
        link.open()
    except serial.SerialException as error:
        if "denied" in str(error).lower() or "busy" in str(error).lower():
            raise AceError(
                f"{port} is busy: only one program can use the Ace. Close the Otii desktop app, or any "
                "other MCP client (e.g. Claude Code or Copilot) running this server."
            ) from error
        raise AceError(f"Cannot open {port}: {error}") from error
    # Full-rate streaming is ~640 KB/s; a large driver buffer rides out
    # scheduling hiccups in the reader thread (Windows only).
    set_buffer_size = getattr(link, "set_buffer_size", None)
    if set_buffer_size is not None:
        set_buffer_size(rx_size=RX_BUFFER, tx_size=1 << 14)
    return link

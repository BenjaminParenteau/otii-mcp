"""The wire protocol, checked against bytes captured from a real Ace and the Otii 3.7.6 app."""

from __future__ import annotations

import struct
import threading
import time
from pathlib import Path

import pytest

from otii_mcp.ace import (
    CMD_CHANNEL,
    CMD_SET,
    CMD_STREAM_START,
    MAGIC,
    PROP_VOLTAGE_UV,
    REPLY_TAG,
    SAMPLES_TAG,
    STREAM_TAG,
    AceDevice,
    AceError,
    FrameParser,
    Recording,
    encode_frame,
    open_transport,
    sample_blocks,
    stream_records,
)
from tests.fake_ace import FakeAce, FakeAcePollingTransport, FakeAceTransport

DATA = Path(__file__).parent / "data"
# Seconds 79.2-80.5 of a capture where the app changed the setpoint from 1.234 V to 2.5 V
# with the output on. Columns: time, USB endpoint (0x01 = to Ace, 0x82 = from Ace), hex data.
CAPTURE = DATA / "ace_capture_s3_excerpt.txt"
# The first ~40 ms of a full-rate recording by the app: 3.3 V on a 1 kOhm resistor.
STREAM_CAPTURE = DATA / "ace_capture_s5_stream_start.txt"


def captured(endpoint: str, path: Path = CAPTURE) -> list[bytes]:
    rows = (line.split() for line in path.read_text().splitlines() if line.strip())
    return [bytes.fromhex(data) for _, ep, data, *_ in rows if ep == endpoint]


def captured_sample_frames() -> list[bytes]:
    parser = FrameParser()
    return [p for raw in captured("0x82", STREAM_CAPTURE) for p in parser.feed(raw) if p[:4] == SAMPLES_TAG]


def test_captured_stream_start_sequence_is_decoded():
    # The app sends all four commands in one USB transfer.
    commands = FrameParser().feed(captured("0x01", STREAM_CAPTURE)[0])
    decoded = [struct.unpack_from(f"<{len(p) // 4}I", p)[1:] for p in commands]

    assert decoded == [
        (CMD_CHANNEL, 0, 1),
        (CMD_CHANNEL, 6, 1),
        (CMD_CHANNEL, 1, 1),
        (CMD_STREAM_START,),
    ]


def test_captured_sample_frames_decode_to_the_resistor_load():
    frames = captured_sample_frames()
    assert len(frames) >= 20

    blocks = {channel: (rate, values) for channel, rate, values in sample_blocks(frames[0])}

    assert set(blocks) == {0, 1, 6}
    assert all(rate == 50_000 and len(values) == 50 for rate, values in blocks.values())
    assert blocks[0][1][0] == pytest.approx(3.3e-3, rel=0.02)  # 3.3 V across ~1 kOhm
    assert blocks[1][1][0] == pytest.approx(3.3, abs=0.01)
    assert blocks[6][1][0] == pytest.approx(11e-3, rel=0.02)


def test_recording_statistics_over_captured_frames():
    recording = Recording()
    for frame in captured_sample_frames():
        recording.add_frame(frame)

    current = recording.channels[0]
    assert recording.dropped_frames == 0
    assert current.count == 50 * recording.frames
    assert current.minimum <= current.average <= current.maximum
    assert current.average == pytest.approx(3.337e-3, rel=0.01)


def test_recording_counts_missing_frames_from_counter_gaps():
    frames = captured_sample_frames()[:10]
    recording = Recording()
    for frame in frames[:3] + frames[6:]:
        recording.add_frame(frame)

    assert recording.frames == 7
    assert recording.dropped_frames == 3


def test_encoder_reproduces_the_apps_bytes_exactly():
    sent = captured("0x01")
    assert len(sent) == 6
    for raw in sent:
        (payload,) = FrameParser().feed(raw)
        assert encode_frame(payload) == raw


def test_captured_voltage_command_is_set_property_in_microvolts():
    commands = [FrameParser().feed(raw)[0] for raw in captured("0x01")]
    voltage_sets = [p for p in commands if len(p) >= 16 and struct.unpack_from("<II", p, 4) == (CMD_SET, PROP_VOLTAGE_UV)]

    assert voltage_sets
    assert all(struct.unpack_from("<I", p, 12)[0] == 2_500_000 for p in voltage_sets)


def test_parser_reads_every_captured_frame_with_valid_checksums():
    parser = FrameParser()
    payloads = [p for raw in captured("0x82") for p in parser.feed(raw)]

    assert len(payloads) == 39
    assert parser.bad_frames == 0
    assert {p[:4] for p in payloads} == {REPLY_TAG, STREAM_TAG}


def test_captured_telemetry_shows_the_new_setpoint():
    parser = FrameParser()
    voltages = []
    for raw in captured("0x82"):
        for payload in parser.feed(raw):
            if payload[:4] == STREAM_TAG:
                for record_type, body in stream_records(payload):
                    if record_type == 2 and struct.unpack_from("<I", body)[0] == 1:
                        voltages.append(struct.unpack_from("<f", body, 4)[0])

    assert voltages[0] == pytest.approx(1.234, abs=0.01)
    assert voltages[-1] == pytest.approx(2.5, abs=0.01)


def test_parser_handles_split_frames_garbage_and_bad_checksums():
    good = encode_frame(b"\x01\x02\x03\x04")
    corrupt = bytearray(encode_frame(b"\x09\x09\x09\x09"))
    corrupt[-1] ^= 0xFF
    stream = b"noise" + MAGIC[:2] + bytes(corrupt) + good + good

    parser = FrameParser()
    out = [p for byte in stream for p in parser.feed(bytes([byte]))]

    assert out == [b"\x01\x02\x03\x04"] * 2
    assert parser.bad_frames == 1


def test_device_reports_unsupported_properties():
    ace = FakeAce()
    device = AceDevice(FakeAceTransport(ace))
    try:
        with pytest.raises(AceError, match="not supported"):
            device.get_property(0x60)
    finally:
        device.close()
        ace.stop()


def test_device_that_never_answers_is_reported():
    ace = FakeAce()
    ace.silent = True
    try:
        with pytest.raises(AceError, match="did not answer"):
            AceDevice(FakeAceTransport(ace))
    finally:
        ace.stop()


def test_time_sync_is_sent_once_a_second():
    ace = FakeAce()
    device = AceDevice(FakeAceTransport(ace))
    try:
        time.sleep(1.3)
        assert [c for c, _ in ace.commands].count(0x0A) == 1
    finally:
        device.close()
        ace.stop()


def test_missing_port_is_reported():
    with pytest.raises(AceError, match="Cannot open COM250"):
        open_transport("COM250")


@pytest.fixture
def device():
    ace = FakeAce(load_ohm=1000.0)
    dev = AceDevice(FakeAceTransport(ace))
    yield dev, ace
    dev.close()
    ace.stop()


def test_record_streams_enabled_channels_then_stops(device):
    dev, ace = device
    dev.set_voltage(3.3)
    dev.set_output(True)

    recording = dev.record(0.3)

    assert set(recording.channels) == {0, 1, 6}
    assert recording.channels[0].sample_rate == 50_000
    assert recording.channels[0].average == pytest.approx(3.3e-3, rel=1e-3)
    assert recording.dropped_frames == 0
    assert ace.streaming is False
    assert ace.enabled_channels == []
    channel_cmds = [args[:2] for cmd, args in ace.commands if cmd == CMD_CHANNEL]
    assert channel_cmds[-6:] == [(0, 1), (6, 1), (1, 1), (0, 0), (6, 0), (1, 0)]


def test_record_reports_frames_the_ace_skipped(device):
    dev, ace = device
    ace.drop_every = 10

    recording = dev.record(0.3)

    assert recording.dropped_frames > 0
    assert recording.dropped_frames == pytest.approx(recording.frames / 9, abs=2)


def test_only_one_recording_at_a_time(device):
    dev, _ = device
    first = threading.Thread(target=dev.record, args=(0.4,))
    first.start()
    time.sleep(0.1)
    try:
        with pytest.raises(AceError, match="already running"):
            dev.record(0.1)
    finally:
        first.join()


def test_current_limit_round_trips_as_float_amps(device):
    dev, ace = device

    dev.set_current_limit(0.123)

    assert ace.current_limit_a == pytest.approx(0.123, rel=1e-6)
    assert dev.get_current_limit() == pytest.approx(0.123, rel=1e-6)


def test_connect_stops_a_stream_left_running(device):
    _, ace = device
    stops = [cmd for cmd, _ in ace.commands if cmd == 0x7E]
    assert stops  # sent during connect


def test_coverage_is_time_based_and_sees_frames_the_ace_never_produced():
    frame = captured_sample_frames()[0]
    recording = Recording()
    # 100 ms of streaming, but only 80 frames arrived (some never produced, so
    # there are no counter gaps to see).
    for i in range(80):
        recording.add_frame(frame[:4] + struct.pack("<I", i) + frame[8:], arrival=i * 0.00125)

    assert recording.dropped_frames == 0
    assert recording.coverage(0) == pytest.approx(0.8, abs=0.01)


def test_coverage_is_full_when_every_frame_arrives():
    frame = captured_sample_frames()[0]
    recording = Recording()
    for i in range(100):
        recording.add_frame(frame[:4] + struct.pack("<I", i) + frame[8:], arrival=i * 0.001)

    assert recording.coverage(0) == pytest.approx(1.0)


def test_streaming_polls_when_the_transport_supports_it():
    ace = FakeAce(load_ohm=1000.0)
    transport = FakeAcePollingTransport(ace)
    dev = AceDevice(transport)
    try:
        dev.set_output(True)
        polls_before = transport.polls
        recording = dev.record(1.0)  # the simulator starts streaming 5-15 ms late
        polls_during = transport.polls - polls_before

        assert polls_during > 100
        assert recording.coverage(0) > 0.95
        assert recording.channels[0].average == pytest.approx(3.3e-3, rel=1e-3)
        time.sleep(0.2)
        idle_polls = transport.polls
        time.sleep(0.2)
        assert transport.polls == idle_polls  # back to blocking reads after the recording
    finally:
        dev.close()
        ace.stop()

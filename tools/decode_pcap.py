"""Decode Otii Ace USB traffic exported by tshark (time, endpoint, hex payload per line).

Usage: tshark -r X.pcap -Y "usb.data_len>0" -T fields -e frame.time_relative -e usb.endpoint_address -e usb.capdata | python tools/decode_pcap.py [mode]
"""
import struct
import sys
from collections import defaultdict

MAGIC = bytes.fromhex("a32cb57f")


def frames(buf):
    """Split a byte stream into (payload) frames; verifies the additive checksum."""
    i = 0
    while True:
        j = buf.find(MAGIC, i)
        if j < 0 or j + 8 > len(buf):
            return
        length, csum = struct.unpack_from("<HH", buf, j + 4)
        payload = buf[j + 8 : j + 8 + length]
        if len(payload) < length:
            return
        ok = (sum(payload) & 0xFFFF) == csum
        yield payload, ok
        i = j + 8 + length


def records(payload):
    """Telemetry payload: 8-byte header then (u16 type, u16 len, body) records."""
    off = 8
    while off + 4 <= len(payload):
        rtype, rlen = struct.unpack_from("<HH", payload, off)
        body = payload[off + 4 : off + 4 + rlen]
        yield rtype, body
        off += 4 + rlen


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "telemetry"
    stats = defaultdict(list)
    for line in sys.stdin:
        parts = line.split()
        if len(parts) < 3:
            continue
        t, ep, data = float(parts[0]), parts[1], bytes.fromhex(parts[2])
        for payload, ok in frames(data):
            head = payload[:8].hex()
            if mode == "frames":
                print(f"{t:10.4f} {ep} ok={ok} len={len(payload):4d} {payload[:64].hex()}")
            elif mode == "telemetry" and ep == "0x82" and len(payload) > 100:
                for rtype, body in records(payload):
                    if rtype == 2 and len(body) == 8:
                        rid, raw = struct.unpack_from("<I4s", body)
                        stats[rid].append((t, struct.unpack("<f", raw)[0], struct.unpack("<I", raw)[0]))
    if mode == "telemetry":
        for rid in sorted(stats):
            vals = [v for _, v, _ in stats[rid]]
            ints = sorted({i for _, _, i in stats[rid]})
            print(f"id 0x{rid:04x} n={len(vals):5d} f32 min={min(vals):12.6g} max={max(vals):12.6g} last={vals[-1]:12.6g} distinct_raw={len(ints)}"
                  + (f" raw={[hex(i) for i in ints[:4]]}" if len(ints) <= 4 else ""))


if __name__ == "__main__":
    main()

"""Exercise the real Ace end to end: read, on, current limit, full-rate measure, off.

Usage: .venv\\Scripts\\python.exe tools\\hw_check.py [volts]
Run with a resistor of roughly 1 kOhm (or nothing) on the Ace output and the Otii app closed.
The script briefly sets a 2 mA current limit to show limiting, then restores the previous limit.
"""

import json
import sys
import time

from otii_mcp.bench import Config, OtiiBench


def show(label: str, result: dict) -> None:
    print(f"{label:>18}: {result['summary']}")


def main() -> None:
    volts = float(sys.argv[1]) if len(sys.argv) > 1 else 3.3
    bench = OtiiBench(Config.from_env())
    try:
        initial = bench.read()
        show("initial", initial)
        previous_limit = initial["current_limit_a"]
        print(f"{'current limit':>18}: {previous_limit * 1e3:.1f} mA")
        show(f"on @ {volts} V", bench.power_on(volts))
        time.sleep(0.3)
        measured = bench.measure(3)
        show("measure 3 s", measured)
        print(json.dumps({k: v for k, v in measured.items() if k != "summary"}, indent=1))
        limited = bench.power_on(volts, current_limit_a=0.002)
        show("limit 2 mA", limited)
        print(f"{'limited flag':>18}: {limited['current_limited']}")
        show("restore limit", bench.power_on(volts, current_limit_a=previous_limit))
    finally:
        show("off", bench.power_off())
        bench.close()


if __name__ == "__main__":
    main()

"""Exercise the real Ace end to end: read, on, full-rate measure, background recording,
save and plot, current limit, off. The plot is copied to ./hw_check_plot.png.

Usage: .venv\\Scripts\\python.exe tools\\hw_check.py [volts]
Run with a resistor of roughly 1 kOhm (or nothing) on the Ace output and the Otii app closed.
The script briefly sets a 2 mA current limit to show limiting, then restores the previous limit.
"""

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

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
        brief = {k: v for k, v in measured.items() if k not in ("summary", "intervals", "bursts")}
        print(json.dumps(brief, indent=1))

        # Background recording with the output switched off and on mid-way,
        # so the capture has an edge to find and plot.
        show("record start", bench.start_recording(max_seconds=30))
        time.sleep(1.0)
        bench.power_off()
        time.sleep(0.5)
        bench.power_on(volts)
        time.sleep(1.0)
        show("record status", bench.recording_status())
        show("record stop", bench.stop_recording())
        with tempfile.TemporaryDirectory() as folder:
            saved = bench.save(folder)
            show("save", saved)
            plot_copy = Path.cwd() / "hw_check_plot.png"
            shutil.copy(saved["png"], plot_copy)
            print(f"{'plot':>18}: {plot_copy}")

        limited = bench.power_on(volts, current_limit_a=0.002)
        show("limit 2 mA", limited)
        print(f"{'limited flag':>18}: {limited['current_limited']}")
        show("restore limit", bench.power_on(volts, current_limit_a=previous_limit))
    finally:
        show("off", bench.power_off())
        bench.close()


if __name__ == "__main__":
    main()

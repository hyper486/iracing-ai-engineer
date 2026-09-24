"""Accelerated numerical soak, not an SDK/audio/VR or wall-clock soak test."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from iracing_ai_engineer.synthetic_runtime import run_synthetic_runtime


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--laps", type=int, default=500)
    parser.add_argument("--output", type=Path, required=True,
                        help="New private/local report file; never overwrite a previous receipt")
    args = parser.parse_args(argv)
    if not 8 <= args.laps <= 3000:
        parser.error("laps must be between 8 and 3000")
    # Own an exclusive output before expensive work. A killed run has no final
    # PASS receipt. Console progress is aggregate and explicitly synthetic.
    with args.output.open("x", encoding="utf-8") as handle:
        result = run_synthetic_runtime(laps=args.laps, progress=lambda row: print(
            json.dumps({"progress": row}), flush=True))
        json.dump(result, handle, sort_keys=True, allow_nan=False)
        handle.write("\n")
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

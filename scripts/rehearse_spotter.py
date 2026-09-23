"""No-game, no-audio deterministic proximity rehearsal using invented samples.

This exercises the actual frame-level detector, not a simulator connection.
The console report is always SYNTHETIC, non-audible and unaccepted for racing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def rehearse() -> dict:
    source = Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(source))
    from iracing_ai_engineer.sdk_probe import RawSdkFrame
    from iracing_ai_engineer.spotter import ProximitySpotter

    model = ProximitySpotter()
    for tick in range(1, 301):
        code = 1 if tick < 60 or tick >= 240 else 2 if tick < 120 else 4 if tick < 180 else 3
        at = 100 + tick / 60
        model.feed(RawSdkFrame(
            buffer_tick=tick, session_info_update=1, sim_mode_raw="full",
            captured_monotonic_s=at,
            values={
                "SessionNum": 0, "SessionTick": tick, "SessionTime": tick / 60,
                "PlayerCarIdx": 0, "CarLeftRight": code, "IsOnTrack": True,
                "IsOnTrackCar": True, "IsReplayPlaying": False, "OnPitRoad": False,
                "PlayerCarInPitStall": False,
            },
        ), now=at)
    rows = model.audit()
    transitions = [row["kind"] for row in rows if row["decision"] == "CANDIDATE"]
    expected = ["CAR_LEFT", "CARS_BOTH_SIDES", "CAR_RIGHT", "ALL_CLEAR"]
    if transitions != expected:
        raise RuntimeError("SYNTHETIC_PROXIMITY_REHEARSAL_FAILED")
    final = model.snapshot(now=105.0)
    return {
        "evidence_kind": "SYNTHETIC", "live_acceptance": False, "audible": False,
        "result": "PASS", "transitions": transitions,
        "audit_sha256": final["audit_sha256"], "counts": final["counts"],
        "limitation": "Detector-only rehearsal; no SDK, audio device, LLM, or VR was exercised.",
    }


if __name__ == "__main__":
    print(json.dumps(rehearse(), ensure_ascii=False, indent=2))

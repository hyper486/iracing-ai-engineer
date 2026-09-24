"""Private, historical-only recomputation using the native numerical owners.

An internally valid collector receipt is not source authentication. No raw
envelope, driver metadata, live state, SDK, provider or audio leaves this owner.
Results are withheld until the terminal receipt and file identity both pass.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter, OrderedDict
from pathlib import Path

from .adapters import TelemetryAdapterError, _load_record, _new_collector_validator
from .live_app import AppState, _LiveAnalysis, bound_session_type
from .live_fuel import LiveFuelConfig
from .live_monitor import LIVE_MONITOR_FIELDS
from .live_traffic import bound_track_length_mm
from .llm_evidence import build_live_context
from .sdk_probe import RawSdkFrame
from .spotter import SPOTTER_FIELDS
from .tire_capture_replay import ERRORS as TIRE_ERRORS
from .tire_capture_replay import TireReplayError, TireReplayJoin
from .trial_replay import TrialReplayError, _plain_file

MAX_CAPTURE_BYTES = 4 * 1024**3
MAX_LINE_BYTES = 1024**2
MAX_SCHEMAS = 128
MAX_CARDS = 128
_FIELDS = frozenset((*LIVE_MONITOR_FIELDS, *SPOTTER_FIELDS))
_RESET_EVENTS = frozenset(("session_reset", "schema_changed", "source_stale",
                           "source_resumed", "duplicate_tick_conflict",
                           "capture_clock_regression", "session_info_changed_without_update"))
_GROUPS = {
    "fuel": ("fuel.current", "fuel.burn_per_lap", "fuel.sample_laps",
             "fuel.learning_progress", "fuel.observed_burn_range"),
    "driving": ("driving.location", "driving.loss", "driving.pattern", "driving.practice",
                "driving.learning_progress"),
    "stint": ("stint.observed", "tire.observed_context", "tire.pace"),
    "tire": ("tire.driver_confirmed_age",),
    "pit": ("pit_observation.elapsed", "pit_observation.baseline",
            "pit_observation.fuel_change", "pit_observation.limits"),
}
_ERRORS = frozenset(("INVALID", "FILE_UNSAFE", "FILE_CHANGED", "TOO_LARGE", "IO_FAILED",
                     "CANCELLED", "CLOCK_REQUIRED", "SOURCE_UNSUPPORTED", "RUNTIME_FAULT"))
_ERRORS |= TIRE_ERRORS


class CaptureReplayError(ValueError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in _ERRORS else "INVALID"
        super().__init__(self.code)


class _Replay:
    def __init__(self, cancelled):
        self.cancelled = cancelled
        self.now = 0.
        self.model = self.state = None
        self.metadata, self.metadata_update = None, None
        self.segment = self.frames = self.publications = self.evicted = 0
        self.segment_start = 0.
        self.previous_clock = None
        self.last_publication = None
        self.events, self.available = Counter(), Counter()
        self.cards = OrderedDict()
        self.latest = {}
        self.last_pit = None
        self.tires = None

    def check_cancelled(self):
        if self.cancelled():
            raise CaptureReplayError("CANCELLED")

    def reset(self):
        if self.tires is not None and self.model is not None:
            self.tires.boundary(self.frames, self.segment)
        if self.model is not None:
            self.model.close()
        self.model = self.state = None
        self.previous_clock = self.last_publication = self.last_pit = None

    def consume(self, record, validator):
        kind = record["record_type"]
        if kind == "run" and record["sim_mode"] != "full":
            raise CaptureReplayError("SOURCE_UNSUPPORTED")
        if kind == "schema":
            schema = validator.schemas[validator.current_schema_epoch]
            if (len(validator.schemas) > MAX_SCHEMAS or len(schema.names) > 4096
                    or (schema.expected_car_count or 0) > 128 or schema.tick_rate_hz > 360):
                raise CaptureReplayError("TOO_LARGE")
        if kind == "event":
            event = record["event_kind"]
            self.events[event] += 1
            if event in _RESET_EVENTS:
                self.reset()
            if event == "session_reset":
                self.metadata, self.metadata_update = None, None
        elif kind == "session_info":
            self.metadata, self.metadata_update = record["payload"], record["session_info_update"]
        elif kind == "frame":
            self.frame(record, validator.schemas[record["schema_epoch"]])

    def frame(self, record, schema):
        captured = record["capture_monotonic_us"]
        if type(captured) is not int or not 0 <= captured <= 2**53:
            raise CaptureReplayError("CLOCK_REQUIRED")
        observed = captured / 1_000_000
        if self.previous_clock is not None and not 0 < observed - self.previous_clock <= .5:
            self.events["replay_clock_boundary"] += 1
            self.reset()
        self.now = observed
        if self.model is None:
            self.segment += 1
            self.segment_start = observed
            self.state = AppState(clock=lambda: self.now)
            self.state.connection("CONNECTED")
            self.model = _LiveAnalysis(
                self.state, LiveFuelConfig(), identifier="offline-capture-recomputation",
                tick_rate=schema.tick_rate_hz, car_count=schema.expected_car_count,
                generation=self.state.generation, allowed=lambda: True,
            )
        frame = RawSdkFrame(
            buffer_tick=record["buffer_tick"], session_info_update=record["session_info_update"],
            values={key: value for key, value in record["values"].items() if key in _FIELDS},
            read_errors=tuple(key for key in record["read_errors"] if key in _FIELDS),
            sim_mode_raw=record["sim_mode_raw"], captured_monotonic_s=observed,
        )
        self.model.process((frame, bound_session_type(self.metadata, self.metadata_update, frame),
                            observed, bound_track_length_mm(
                                self.metadata, self.metadata_update, frame)))
        if self.tires is not None:
            self.tires.after_frame(frame, captured, self.model, self.frames + 1, self.segment)
        # Pace accelerated input only at actual lap-worker boundaries. This is
        # not a replay of original thread scheduling or an audio latency test.
        driving = self.model._driving
        if driving is not None:
            worker = driving._worker
            deadline = time.monotonic() + 30
            while not worker.wait_idle(.05):
                self.check_cancelled()
                if time.monotonic() >= deadline:
                    raise CaptureReplayError("RUNTIME_FAULT")
            if worker.snapshot()["failed"]:
                raise CaptureReplayError("RUNTIME_FAULT")
        self.previous_clock = observed
        self.frames += 1
        if self.model._next_snapshot != self.last_publication:
            self.last_publication = self.model._next_snapshot
            self.summarize()

    def summarize(self):
        value = self.state.snapshot()
        if self.tires is not None and self.model._tire_age is not None:
            # A matched assertion is applied after this frame's normal feed.
            # Refresh only the separate offline copy at publication boundaries.
            value["tire_age"] = self.model._tire_age.snapshot(value.get("monitor") or {})
        self.publications += 1
        facts = {item["id"]: item["text"] for item in build_live_context(value)["facts"]}
        telemetry = (value.get("monitor") or {}).get("telemetry") or {}
        lap = telemetry.get("laps_completed")
        if type(lap) is not int or not 0 <= lap <= 2**53:
            lap = None
        for group, identifiers in _GROUPS.items():
            selected = [{"id": key, "text": facts[key]} for key in identifiers if key in facts]
            if not selected:
                continue
            self.available[group] += 1
            # A completed pit observation persists through subsequent laps. Keep
            # one card per observation revision, not one "visit" per later lap.
            if group == "pit":
                pit = value["pit_observation"]["observation"]
                if pit == self.last_pit:
                    continue
                self.last_pit = pit
                key = (self.segment, group, self.frames)
            else:
                key = (self.segment, group, lap)
            card = {"segment": self.segment, "lap_completed": lap, "group": group,
                    "segment_elapsed_s": round(self.now - self.segment_start, 3),
                    "facts": selected}
            self.cards[key] = card
            self.cards.move_to_end(key)
            self.latest[group] = card
            if len(self.cards) > MAX_CARDS:
                self.cards.popitem(last=False)
                self.evicted += 1

    def report(self, evidence, size):
        return {
            "contract_version": "native-capture-replay-v1", "status": "RECOMPUTED",
            "source_kind": "OFFLINE_REPLAY", "source_authenticity": "UNVERIFIED",
            "live_acceptance": False, "heard": False, "audio_played": False,
            "sdk_accessed": False, "provider_called": False, "advisor_only": True,
            "bytes": size, "frames": self.frames, "segments": self.segment,
            "publications": self.publications, "events": dict(self.events),
            "available_publications": dict(self.available), "cards": list(self.cards.values()),
            "latest": dict(self.latest), "evicted_cards": self.evicted,
            "completion_status": evidence.completion_status,
            "strategy_parameters": "NOT_RECORDED_NOT_APPLIED",
            "tire_replay": self.tires.finish() if self.tires is not None else None,
        }


def replay_capture(path: Path, *, journal: Path | None = None, cancelled=lambda: False) -> dict:
    """Read one sealed private clip; never publish partial or live-shaped data."""
    replay = _Replay(cancelled)
    validator = _new_collector_validator(stale_after_s=.5, opponent_error_policy="degrade",
                                         require_receipt=True)
    try:
        replay.check_cancelled()
        if journal is not None:
            replay.tires = TireReplayJoin(journal, cancelled)
        with _plain_file(path, MAX_CAPTURE_BYTES) as (handle, size):
            expected_digest = None
            if replay.tires is not None:
                digest = hashlib.sha256()
                remaining = size
                while remaining:
                    replay.check_cancelled()
                    chunk = handle.read(min(MAX_LINE_BYTES, remaining))
                    if not chunk:
                        raise CaptureReplayError("FILE_CHANGED")
                    digest.update(chunk)
                    remaining -= len(chunk)
                if handle.read(1):
                    raise CaptureReplayError("FILE_CHANGED")
                expected_digest = digest.hexdigest()
                replay.tires.select(expected_digest, size)
                handle.seek(0)
            consumed_digest = hashlib.sha256()
            line_number, consumed = 0, 0
            while line := handle.readline(MAX_LINE_BYTES + 1):
                replay.check_cancelled()
                line_number += 1
                consumed += len(line)
                consumed_digest.update(line)
                if len(line) > MAX_LINE_BYTES or consumed > size:
                    raise CaptureReplayError("TOO_LARGE")
                record = _load_record(line.decode("utf-8"), line_number)
                validator.process(record, line_number=line_number)
                replay.consume(record, validator)
            evidence = validator.finish()
            replay.check_cancelled()
            if expected_digest is not None and consumed_digest.hexdigest() != expected_digest:
                raise CaptureReplayError("FILE_CHANGED")
            report = replay.report(evidence, size)
        return report  # Identity check above must complete before exposing facts.
    except TireReplayError as error:
        raise CaptureReplayError(error.code) from None
    except CaptureReplayError:
        raise
    except TrialReplayError as error:
        raise CaptureReplayError(error.code.removeprefix("TRIAL_")) from None
    except (TelemetryAdapterError, UnicodeError, ValueError, TypeError, RecursionError):
        raise CaptureReplayError("INVALID") from None
    except OSError:
        raise CaptureReplayError("IO_FAILED") from None
    except Exception:
        raise CaptureReplayError("RUNTIME_FAULT") from None
    finally:
        replay.reset()

"""Silent, bounded replay of a private native trial journal.

Recomputes proximity decisions, correlates recorded software audio calls, and
optionally checks companion capture bytes. Hash agreement is not simulator
authentication, playback reproduction, human hearing or live acceptance.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import Counter, OrderedDict, deque
from contextlib import contextmanager
from pathlib import Path

from .desktop_settings import SettingsStore, _identity, _reparse
from .live_worker import payload_size
from .sdk_probe import RawSdkFrame
from .spotter import SPOTTER_FIELDS, ProximitySpotter, SpotterConfig
from .trial_audit import (
    _BOOL_FIELDS,
    _INTEGER_FIELDS,
    AUDIO_KINDS,
    AUDIO_OUTCOMES,
    AUDIO_OUTPUTS,
    AUDIO_REASONS,
    AUDIO_STATES,
    MAX_ENTRY_BYTES,
    MAX_TRIAL_BYTES,
    TRIAL_CONTRACT,
    _number,
    validate_projection,
)

_ERRORS = frozenset(("TRIAL_INVALID", "TRIAL_FILE_UNSAFE", "TRIAL_FILE_CHANGED",
                     "TRIAL_TOO_LARGE", "TRIAL_HASH_MISMATCH", "TRIAL_DETECTOR_MISMATCH",
                     "TRIAL_RUNTIME_FAULT", "TRIAL_IO_FAILED", "TRIAL_CANCELLED"))
_LINE_LIMIT = 32 * 1024
_PENDING_LIMIT = 2048


class TrialReplayError(ValueError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in _ERRORS else "TRIAL_INVALID"
        super().__init__(self.code)


def _require(condition, code="TRIAL_INVALID"):
    if not condition:
        raise TrialReplayError(code)


def _keys(value, keys):
    _require(type(value) is dict and set(value) == set(keys.split()))


def _integer(value, low=0, high=2**53):
    return type(value) is int and low <= value <= high


def _hex(value, length):
    return type(value) is str and re.fullmatch("[0-9a-f]{" + str(length) + "}", value) is not None


def _enum(value, choices):
    return type(value) is str and value in choices


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _constant(_value):
    raise TrialReplayError("TRIAL_INVALID")


@contextmanager
def _plain_file(path, maximum):
    """Bounded descriptor read with reparse/hardlink and before/after identity checks."""
    path = Path(os.path.abspath(path))
    store = SettingsStore(path.parent)
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode) and not _reparse(before) and before.st_nlink == 1,
             "TRIAL_FILE_UNSAFE")
    _require(0 < before.st_size <= maximum, "TRIAL_TOO_LARGE")
    flags = os.O_RDONLY
    for name in ("O_BINARY", "O_CLOEXEC", "O_NOFOLLOW"):
        flags |= getattr(os, name, 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        _require(_identity(opened) == _identity(before) and opened.st_nlink == 1
                 and stat.S_ISREG(opened.st_mode), "TRIAL_FILE_CHANGED")
        yield handle, opened.st_size
        after, named = os.fstat(handle.fileno()), path.lstat()
        store._check_root()
        _require(_identity(before) == _identity(after) == _identity(named)
                 and before.st_ctime_ns == named.st_ctime_ns
                 and opened.st_ctime_ns == after.st_ctime_ns
                 and named.st_nlink == after.st_nlink == 1 and not _reparse(named),
                 "TRIAL_FILE_CHANGED")


def _frame(value):
    _keys(value, "values buffer_tick captured_monotonic_s sim_mode read_errors")
    _require(type(value["values"]) is list and len(value["values"]) == len(SPOTTER_FIELDS))
    for name, item in zip(SPOTTER_FIELDS, value["values"], strict=True):
        _require(item is None or (type(item) is bool if name in _BOOL_FIELDS else
                                 _integer(item, -2**53) if name in _INTEGER_FIELDS else
                                 _number(item)))
    _require(value["buffer_tick"] is None or _integer(value["buffer_tick"], -2**53))
    _require(value["captured_monotonic_s"] is None or _number(value["captured_monotonic_s"]))
    _require(_enum(value["sim_mode"], ("full", "other")))
    errors = value["read_errors"]
    _require(type(errors) is list and len(errors) <= len(SPOTTER_FIELDS)
             and all(_enum(item, SPOTTER_FIELDS) for item in errors)
             and len(set(errors)) == len(errors))
    return RawSdkFrame(buffer_tick=value["buffer_tick"], session_info_update=0,
                       values=dict(zip(SPOTTER_FIELDS, value["values"], strict=True)),
                       read_errors=tuple(errors), sim_mode_raw=value["sim_mode"],
                       captured_monotonic_s=value["captured_monotonic_s"])


class _Replay:
    def __init__(self, capture_directory, cancelled):
        self.capture_directory = capture_directory
        self.cancelled = cancelled
        self.model = None
        self.generation = -1
        self.segments = self.frames = self.operations = 0
        self.audio_sequence = None
        self.audio_health = None
        self.current = None
        self.pending = OrderedDict()
        self.tail = deque(maxlen=128)
        self.decisions, self.reasons, self.outcomes = Counter(), Counter(), Counter()
        self.audio_reasons = Counter()
        self.results, self.unbound, self.notices = Counter(), 0, 0
        self.captures, self.capture_checks = Counter(), Counter()
        self.capture_open = None
        self.capture_generation = -1
        self.capture_tail = deque(maxlen=128)
        self.tire_assertions = 0
        self.tire_tail = deque(maxlen=32)
        self.last_tire_assertion = None
        self.evicted = 0

    def _finalize(self, entry):
        outcomes = entry["audio_outcomes"]
        if "PLAYBACK_ERROR" in outcomes:
            result = "PLAYBACK_ERROR"
        elif "CANCELLED" in outcomes:
            result = "CANCELLED"
        elif "PLAYBACK_COMPLETED" in outcomes:
            result = "SOFTWARE_COMPLETED_NOT_HEARING_CONFIRMED"
        elif "PLAYBACK_STARTED" in outcomes:
            result = "STARTED_WITHOUT_TERMINAL_RECEIPT"
        elif "ATTEMPTED" in outcomes:
            result = "ATTEMPTED_NOT_STARTED"
        else:
            result = "DETECTED_NO_RECORDED_ATTEMPT"
        self.results[result] += 1
        self.tail.append({**entry, "result": result})

    def detector(self, item):
        _require(type(item) is dict)
        operation = item.get("operation")
        if operation == "RESET":
            _keys(item, "operation generation tick_rate_hz config")
            _require(_integer(item["generation"]) and item["generation"] > self.generation)
            _keys(item["config"],
                  "max_age_s occupied_confirm_s clear_confirm_s repeat_s intent_ttl_s")
            self.model = ProximitySpotter(tick_rate_hz=item["tick_rate_hz"],
                                          config=SpotterConfig(**item["config"]))
            self.generation = item["generation"]
            self.current = None
            self.segments += 1
            return
        _keys(item, "operation generation now prior_clock raised frame unavailable_status "
                    "decisions event_count")
        _require(self.model is not None and _integer(item["generation"])
                 and item["generation"] == self.generation)
        _require(_enum(operation, ("FEED", "TIME", "UNAVAILABLE")))
        _require(_number(item["now"]) and item["now"] >= 0)
        _require(type(item["raised"]) is bool and _integer(item["event_count"]))
        _require(type(item["decisions"]) is list and len(item["decisions"]) <= 8)
        _require(not item["raised"], "TRIAL_RUNTIME_FAULT")
        cursor, previous, serial = self.model.trace_cursor()
        prior = item["prior_clock"]
        if prior is None:
            _require(previous is None, "TRIAL_DETECTOR_MISMATCH")
        else:
            _require(_number(prior) and prior >= 0)
            _require(previous is not None and prior >= previous, "TRIAL_DETECTOR_MISMATCH")
            self.model.advance_time(prior)
            _require(not self.model.audit_since(cursor), "TRIAL_DETECTOR_MISMATCH")
        if operation == "FEED":
            _require(item["unavailable_status"] is None)
            self.model.feed(_frame(item["frame"]), now=item["now"])
            self.frames += 1
        elif operation == "TIME":
            _require(item["frame"] is None and item["unavailable_status"] is None)
            self.model.advance_time(item["now"])
        else:
            _require(item["frame"] is None and _enum(
                item["unavailable_status"], ("WAIT_SIM", "DISCONNECTED", "STOPPED", "ERROR")))
            self.model.unavailable(item["unavailable_status"], now=item["now"])
        actual = self.model.audit_since(cursor)
        _require(_canonical(actual) == _canonical(item["decisions"])
                 and self.model.trace_cursor()[2] == item["event_count"], "TRIAL_DETECTOR_MISMATCH")
        self.operations += 1
        for row in actual:
            decision = row["decision"]
            self.decisions[decision] += 1
            if decision == "HEALTH":
                self.reasons[row["reason"]] += 1
            elif decision == "CANDIDATE":
                serial += 1
                self.current = (self.generation, row["epoch"], serial)
                self.pending[self.current] = {
                    "event_id": list(self.current), "kind": row["kind"], "tick": row["tick"],
                    "audio_health_at_detection": self.audio_health,
                    "audio_outcomes": [], "withdrawal": None, "start_delay_ms": None,
                    "last_audio_outcome": None,
                }
                if len(self.pending) > _PENDING_LIMIT:
                    self._finalize(self.pending.popitem(last=False)[1])
                    self.evicted += 1
            elif decision in ("EXPIRED", "SUPERSEDED", "INVALIDATED"):
                if self.current in self.pending:
                    self.pending[self.current]["withdrawal"] = decision
                self.current = None

    def audio(self, item):
        _keys(item, "sequence revision now outcome status reason output_status kind event_id "
                    "start_delay_ms enabled muted output_selection heard live_acceptance")
        _require(_integer(item["sequence"], 1) and _integer(item["revision"])
                 and _number(item["now"]) and item["now"] >= 0)
        _require(self.audio_sequence is None or item["sequence"] == self.audio_sequence + 1)
        _require(_enum(item["outcome"], AUDIO_OUTCOMES) and _enum(item["status"], AUDIO_STATES)
                 and _enum(item["reason"], AUDIO_REASONS | AUDIO_KINDS)
                 and _enum(item["output_status"], AUDIO_OUTPUTS)
                 and _enum(item["output_selection"], ("default", "selected", "unset")))
        _require(type(item["enabled"]) is bool and type(item["muted"]) is bool
                 and item["heard"] is False and item["live_acceptance"] is False)
        delay = item["start_delay_ms"]
        _require(delay is None or (_number(delay) and delay >= 0))
        identity = item["event_id"]
        if identity is None:
            _require(item["kind"] is None and item["outcome"] in ("HEALTH", "FAILED"))
        else:
            _require(type(identity) is list and len(identity) == 3
                     and all(_integer(value, -1) for value in identity)
                     and _enum(item["kind"], AUDIO_KINDS) and item["outcome"] != "HEALTH")
        self.audio_sequence = item["sequence"]
        self.audio_health = {key: item[key] for key in ("status", "reason", "enabled", "muted",
                                                       "output_selection", "output_status")}
        self.outcomes[item["outcome"]] += 1
        if item["outcome"] in ("HEALTH", "FAILED", "PLAYBACK_ERROR"):
            self.audio_reasons[item["reason"]] += 1
        if identity is None:
            return
        if item["kind"] in ("PTT_CANCELLED", "DATA_LOST"):
            _require(identity == [-1, -1, 0] if item["kind"] == "DATA_LOST" else
                     identity[0] >= 0 and identity[1] >= 0 and identity[2] == 0)
            self.notices += 1
            return
        entry = self.pending.get(tuple(identity))
        if entry is None or entry["kind"] != item["kind"]:
            self.unbound += 1
            return
        outcome, previous = item["outcome"], entry["last_audio_outcome"]
        allowed = {
            "ATTEMPTED": (None, "PLAYBACK_COMPLETED", "CANCELLED", "DROPPED_BEFORE_START",
                          "START_DEADLINE_MISSED", "NOTICE_PREEMPTED", "PLAYBACK_ERROR"),
            "PLAYBACK_STARTED": ("ATTEMPTED",),
            "PLAYBACK_COMPLETED": ("PLAYBACK_STARTED",), "CANCELLED": ("PLAYBACK_STARTED",),
            "DROPPED_BEFORE_START": ("ATTEMPTED",),
            "START_DEADLINE_MISSED": ("DROPPED_BEFORE_START",),
            "NOTICE_PREEMPTED": ("ATTEMPTED", "PLAYBACK_STARTED"),
            "PLAYBACK_ERROR": ("ATTEMPTED", "PLAYBACK_STARTED"),
        }
        _require(previous in allowed.get(outcome, ()))
        entry["last_audio_outcome"] = outcome
        if item["outcome"] not in entry["audio_outcomes"]:
            entry["audio_outcomes"].append(item["outcome"])
        if delay is not None:
            entry["start_delay_ms"] = delay

    def capture(self, item):
        _keys(item, "status generation capture_id bytes sha256")
        _require(_enum(item["status"], ("OPEN", "COMPLETE", "EMPTY", "INCOMPLETE"))
                 and _integer(item["generation"], 0, self.generation)
                 and _hex(item["capture_id"], 32) and _hex(item["sha256"], 64)
                 and _integer(item["bytes"], 0, 4 * 1024**3))
        key = item["generation"], item["capture_id"]
        if item["status"] == "OPEN":
            _require(self.capture_open is None and item["generation"] > self.capture_generation)
            self.capture_open, self.capture_generation = key, item["generation"]
        else:
            _require(self.capture_open == key)
            self.capture_open = None
            check = "NOT_CHECKED"
            if self.capture_directory is not None and item["status"] == "COMPLETE":
                check = self._check_capture(item)
            self.capture_checks[check] += 1
            self.capture_tail.append({**item, "byte_link_check": check})
        self.captures[item["status"]] += 1

    def tire_confirmation(self, item):
        # Fixed driver assertion only. The spotter trace does not contain the
        # fields required to re-evaluate tire age or verify a service action.
        _require(item["generation"] == self.generation)
        row = item["assertion"]
        key = (self.generation, row["decision_tick"], row["revision"])
        if self.last_tire_assertion is not None:
            previous = self.last_tire_assertion
            # Session ticks may restart inside one transport connection; the
            # owner revision continues increasing across those resets.
            _require(key[0] > previous[0] or (key[0] == previous[0] and key[2] > previous[2]))
        self.last_tire_assertion = key
        self.tire_assertions += 1
        self.tire_tail.append(item)

    def _check_capture(self, item):
        path = Path(self.capture_directory) / f"capture-{item['capture_id']}.jsonl"
        try:
            digest = hashlib.sha256()
            with _plain_file(path, 4 * 1024**3) as (handle, size):
                if size != item["bytes"]:
                    return "MISMATCH"
                remaining = size
                while remaining:
                    _require(not self.cancelled(), "TRIAL_CANCELLED")
                    chunk = handle.read(min(1024**2, remaining))
                    _require(bool(chunk), "TRIAL_FILE_CHANGED")
                    digest.update(chunk)
                    remaining -= len(chunk)
            return "MATCH" if digest.hexdigest() == item["sha256"] else "MISMATCH"
        except TrialReplayError as error:
            if error.code == "TRIAL_CANCELLED":
                raise
            return "UNAVAILABLE_OR_UNSAFE"
        except Exception:
            return "UNAVAILABLE_OR_UNSAFE"

    def result(self, complete, entries):
        for entry in self.pending.values():
            self._finalize(entry)
        self.pending.clear()
        return {
            "contract_version": TRIAL_CONTRACT,
            "status": ("INCOMPLETE_PREFIX" if not complete else "NO_DETECTOR_DATA"
                       if not self.frames else "REPLAY_MATCH"),
            "source_kind": "OFFLINE_REPLAY", "source_authenticity": "UNVERIFIED",
            "advisor_only": True, "audio_played": False, "heard": False, "live_acceptance": False,
            "complete": complete, "entries": entries, "detector_segments": self.segments,
            "frames": self.frames, "operations": self.operations,
            "decisions": dict(self.decisions), "health_transitions": dict(self.reasons),
            "audio_outcomes": dict(self.outcomes), "event_results": dict(self.results),
            "audio_health_records": dict(self.audio_reasons),
            "unbound_audio_records": self.unbound, "notice_audio_records": self.notices,
            "correlation_evictions": self.evicted, "correlation_limit": _PENDING_LIMIT,
            "events_tail": list(self.tail), "captures": dict(self.captures),
            "capture_left_open": self.capture_open is not None,
            "capture_byte_checks": dict(self.capture_checks),
            "captures_tail": list(self.capture_tail),
            "tire_assertions": self.tire_assertions,
            "tire_assertions_tail": list(self.tire_tail),
            "tire_age_recomputed": False, "tire_service_verified": False,
        }


def _replay(path, capture_directory, cancelled):
    replay = _Replay(capture_directory, cancelled)
    digest = hashlib.sha256()
    entries, header, complete = 0, False, False
    with _plain_file(path, MAX_TRIAL_BYTES) as (handle, remaining):
        while remaining:
            _require(not cancelled(), "TRIAL_CANCELLED")
            raw = handle.readline(min(_LINE_LIMIT + 1, remaining))
            _require(bool(raw), "TRIAL_FILE_CHANGED")
            remaining -= len(raw)
            _require(len(raw) <= _LINE_LIMIT, "TRIAL_TOO_LARGE")
            _require(not complete)
            if not raw.endswith(b"\n"):
                _require(header and remaining == 0)
                break  # An interrupted final write is an incomplete prefix, never a seal.
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                               parse_constant=_constant)
            payload_size(value, limit=MAX_ENTRY_BYTES + 4096)
            _require(type(value) is dict)
            if not header:
                _keys(value, "record contract_version run_id advisor_only executable "
                             "live_acceptance heard source_authenticity")
                _require(value["record"] == "header"
                         and value["contract_version"] in (TRIAL_CONTRACT, "private-trial-audit-v1")
                         and _hex(value["run_id"], 32) and value["advisor_only"] is True
                         and value["executable"] is False and value["live_acceptance"] is False
                         and value["heard"] is False
                         and value["source_authenticity"] == "UNVERIFIED")
                header = True
                input_contract = value["contract_version"]
            elif value.get("record") == "footer":
                _keys(value, "record entries stream_sha256 completion live_acceptance heard")
                _require(_integer(value["entries"]) and value["entries"] == entries
                         and value["completion"] == "COMPLETE"
                         and value["live_acceptance"] is False and value["heard"] is False)
                _require(value["stream_sha256"] == digest.hexdigest(), "TRIAL_HASH_MISMATCH")
                complete = True
            else:
                _keys(value, "record sequence lane payload")
                _require(value["record"] == "entry" and _integer(value["sequence"], 1)
                         and value["sequence"] == entries + 1
                         and _enum(value["lane"], ("detector", "audio", "capture",
                                                  "tire_confirmation")))
                _require(value["lane"] != "tire_confirmation" or input_contract == TRIAL_CONTRACT)
                validate_projection(value["lane"], value["payload"])
                getattr(replay, value["lane"])(value["payload"])
                entries += 1
            digest.update(raw)
    _require(header)
    return replay.result(complete, entries)


def replay_trial(path: Path, *, capture_directory: Path | None = None, cancelled=lambda: False):
    """Read only. Errors expose fixed codes, never paths, data or exception chains."""
    code = "TRIAL_INVALID"
    try:
        return _replay(path, capture_directory, cancelled)
    except TrialReplayError as error:
        code = error.code
    except OSError:
        code = "TRIAL_IO_FAILED"
    except Exception:
        pass
    raise TrialReplayError(code)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Silent private proximity/audio journal replay")
    parser.add_argument("journal", type=Path)
    parser.add_argument("--capture-directory", type=Path)
    args = parser.parse_args(argv)
    try:
        report = replay_trial(args.journal, capture_directory=args.capture_directory)
    except TrialReplayError as error:
        print(json.dumps({"status": "REJECTED", "reason": error.code,
                          "heard": False, "live_acceptance": False}))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["status"] == "REPLAY_MATCH" else 1


if __name__ == "__main__":
    raise SystemExit(main())

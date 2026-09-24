"""Explicit post-session service review, never unattended label approval.

Only a revalidated capture/journal pair supplies exit coordinates and lineage.
The human supplies every disposition and retained evidence file. Public hashes
bind this self-attested review; they authenticate neither reviewer nor service.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .desktop_settings import SettingsStore
from .engineer_session import canonical_sha256
from .live_tire_age import KINDS
from .retrieved_live_analysis import (
    _bound_event_identity_from_context,
    _new_tire_stint_tracker,
    _track_tire_stint_sample,
)
from .tire_service_history import HISTORY_VERSION, validate_service_history
from .trial_replay import _plain_file

PLAN_VERSION = "tire-service-review-plan-v1"
MAX_EXITS = 256
MAX_EVIDENCE_BYTES = 4 * 1024**3
SKIP = "UNREVIEWED"
ATTESTATION = "HUMAN_REVIEWED_ACTUAL_SERVICE_NOT_COUNTER_OR_REQUEST"


class TireReviewError(ValueError):
    pass


class TireReviewDraft:
    """Private bounded projection using the exact offline tire-exit rules."""

    def __init__(self):
        self.tracker = _new_tire_stint_tracker()
        self.rows, self.hints = [], {}
        self.visit_tick = None
        self.unavailable_frames = 0

    def feed(self, sample):
        previous = self.tracker["previous_point"]
        _track_tire_stint_sample(self.tracker, sample)
        point = self.tracker["previous_point"]
        if point is None:
            self.visit_tick = None
            self.unavailable_frames += 1
            return
        if previous and not previous["on_pit_road"] and point["on_pit_road"]:
            self.visit_tick = point["decision_tick"]
        if previous and previous["on_pit_road"] and not point["on_pit_road"]:
            if len(self.rows) >= MAX_EXITS:
                raise TireReviewError("TIRE_REVIEW_LIMIT")
            self.rows.append({"ordinal": len(self.rows) + 1, "visit_tick": self.visit_tick,
                **{key: point[key] for key in (
                    "decision_tick", "laps_completed", "tire_compound", "tire_sets_used")}})
            self.visit_tick = None

    def assertion(self, row, accepted):
        key = row["visit_tick"]
        if key not in self.hints and len(self.hints) >= 1024:
            raise TireReviewError("TIRE_REVIEW_LIMIT")
        self.hints[key] = {"driver_assertion": row["kind"],
                           "assertion_recomputed": bool(accepted)}

    def finish(self, evidence, validator, capture_sha256):
        # The existing history format identifies exits by tick, not session.
        # Do not collapse multiple sessions or contradictory sequences into it.
        if (evidence.session_reset_count or self.tracker["invalid_reasons"]
                or not self.rows):
            raise TireReviewError("TIRE_REVIEW_UNAVAILABLE")
        identity = _bound_event_identity_from_context(
            validator.event_identity_context_evidence(evidence).to_dict())
        rows = [{**row, **self.hints.get(row["visit_tick"], {
            "driver_assertion": None, "assertion_recomputed": False})} for row in self.rows]
        material = {"contract_version": PLAN_VERSION, "capture_sha256": capture_sha256,
            "identity_sha256": canonical_sha256(identity),
            "source_receipt_sha256": canonical_sha256(evidence.to_dict()),
            "source_authenticity": "UNVERIFIED", "status": "PENDING_HUMAN_REVIEW",
            "live_acceptance": False, "unavailable_frames": self.unavailable_frames,
            "exits": rows}
        return {**material, "plan_sha256": canonical_sha256(material)}


def _check_cancelled(cancelled):
    if cancelled():
        raise TireReviewError("TIRE_REVIEW_CANCELLED")


def _evidence_digest(path, cancelled, maximum=MAX_EVIDENCE_BYTES):
    if not isinstance(path, Path):
        raise TireReviewError("TIRE_REVIEW_EVIDENCE_REQUIRED")
    with _plain_file(path, maximum) as (handle, size):
        digest, remaining = hashlib.sha256(), size
        while remaining:
            _check_cancelled(cancelled)
            data = handle.read(min(1024**2, remaining))
            if not data:
                raise TireReviewError("TIRE_REVIEW_EVIDENCE_CHANGED")
            digest.update(data)
            remaining -= len(data)
        if handle.read(1):
            raise TireReviewError("TIRE_REVIEW_EVIDENCE_CHANGED")
        result = digest.hexdigest(), size
    return result


def export_reviewed_tires(capture, journal, *, expected_plan_sha256, decisions,
                          attested, directory, cancelled=lambda: False):
    """Re-open exact inputs, then create a new private review/history bundle.

    No implicit kind, evidence, attestation or overwrite. Evidence bytes remain
    in their original private files; the bundle retains hashes and locators.
    """
    from .capture_replay import replay_capture_for_tire_review

    if attested is not True or type(decisions) is not list or not 1 <= len(decisions) <= MAX_EXITS:
        raise TireReviewError("TIRE_REVIEW_EXPLICIT_DECISIONS_REQUIRED")
    _check_cancelled(cancelled)
    _, plan = replay_capture_for_tire_review(capture, journal=journal, cancelled=cancelled)
    if plan["plan_sha256"] != expected_plan_sha256:
        raise TireReviewError("TIRE_REVIEW_PLAN_CHANGED")
    if len(decisions) != len(plan["exits"]):
        raise TireReviewError("TIRE_REVIEW_EXPLICIT_DECISIONS_REQUIRED")
    journal_sha, _ = _evidence_digest(journal, cancelled)
    cache, evidence_bytes = {}, 0
    reviews, labels = [], []
    for row, choice in zip(plan["exits"], decisions, strict=True):
        _check_cancelled(cancelled)
        if (type(choice) is not dict or set(choice) != {
                "ordinal", "kind", "evidence_path", "evidence_offset_s"}
                or type(choice["ordinal"]) is not int or choice["ordinal"] != row["ordinal"]
                or type(choice["kind"]) is not str or choice["kind"] not in (*KINDS, SKIP)):
            raise TireReviewError("TIRE_REVIEW_EXPLICIT_DECISIONS_REQUIRED")
        offset = choice["evidence_offset_s"]
        if offset is not None and (type(offset) is not int or not 0 <= offset <= 7 * 86400):
            raise TireReviewError("TIRE_REVIEW_EVIDENCE_REQUIRED")
        evidence = None
        if choice["kind"] == SKIP:
            if choice["evidence_path"] is not None or offset is not None:
                raise TireReviewError("TIRE_REVIEW_EXPLICIT_DECISIONS_REQUIRED")
        else:
            path = choice["evidence_path"]
            if (not isinstance(path, Path)
                    or path.resolve() in (capture.resolve(), journal.resolve())):
                raise TireReviewError("TIRE_REVIEW_INDEPENDENT_EVIDENCE_REQUIRED")
            key = path.resolve()
            if key not in cache:
                cache[key] = _evidence_digest(path, cancelled, MAX_EVIDENCE_BYTES - evidence_bytes)
                evidence_bytes += cache[key][1]
            sha, size = cache[key]
            if sha in (plan["capture_sha256"], journal_sha):
                raise TireReviewError("TIRE_REVIEW_INDEPENDENT_EVIDENCE_REQUIRED")
            evidence = {"sha256": sha, "bytes": size, "offset_s": offset}
        review = {"contract_version": "tire-service-human-review-v1",
            "plan_sha256": plan["plan_sha256"], "observed_exit": row,
            "kind": choice["kind"], "evidence": evidence, "attestation": ATTESTATION,
            "reviewer_authentication": "SELF_ATTESTED_NOT_AUTHENTICATED",
            "source_authenticity": "UNVERIFIED", "live_acceptance": False}
        review_sha = canonical_sha256(review)
        reviews.append({**review, "review_sha256": review_sha})
        if choice["kind"] != SKIP:
            labels.append({"kind": choice["kind"], "label_receipt_sha256": review_sha,
                **{key: row[key] for key in (
                    "decision_tick", "laps_completed", "tire_compound", "tire_sets_used")}})
    if not labels:
        raise TireReviewError("TIRE_REVIEW_NO_REVIEWED_EVENTS")
    material = {"contract_version": HISTORY_VERSION,
        "identity_sha256": plan["identity_sha256"],
        "source_receipt_sha256": plan["source_receipt_sha256"],
        "provenance": "REVIEWED_LABELS_NOT_SDK_SERVICE_CONTENTS", "events": labels}
    history = {**material, "history_sha256": canonical_sha256(material)}
    validate_service_history(history, expected_history_sha256=history["history_sha256"],
        expected_identity_sha256=plan["identity_sha256"],
        expected_source_receipt_sha256=plan["source_receipt_sha256"])
    receipt = {"contract_version": "tire-service-review-bundle-v1", "plan": plan,
        "reviews": reviews, "history_sha256": history["history_sha256"],
        "source_authenticity": "UNVERIFIED", "live_acceptance": False}
    # CreateNew files in a new private child; never replace existing records.
    _check_cancelled(cancelled)
    store = SettingsStore(directory)
    store._check_root(create=True)
    child = store.root / ("tire-review-" + uuid4().hex)
    child.mkdir(mode=0o700)
    target = SettingsStore(child)
    for name, value in (("review.json", receipt), ("service-history.json", history)):
        _check_cancelled(cancelled)
        target._check_root()
        payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                              allow_nan=False) + "\n").encode("utf-8")
        with (child / name).open("x+b") as handle:
            if handle.write(payload) != len(payload):
                raise TireReviewError("TIRE_REVIEW_WRITE_FAILED")
            handle.flush()
            os.fsync(handle.fileno())
            handle.seek(0)
            if handle.read() != payload:
                raise TireReviewError("TIRE_REVIEW_WRITE_FAILED")
        target._check_root()
    return {"status": "EXPORTED_SELF_ATTESTED", "directory_name": child.name,
        "history_sha256": history["history_sha256"], "reviewed_events": len(labels),
        "unreviewed_events": len(reviews) - len(labels), "live_acceptance": False}

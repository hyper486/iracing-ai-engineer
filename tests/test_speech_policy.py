from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace

import pytest

from iracing_ai_engineer.speech_policy import (
    MESSAGE_CLASS_PRIORITY,
    MESSAGE_PARAM_SCHEMA,
    MESSAGE_TEMPLATE_ID,
    ActiveEnvelopeSnapshot,
    BoundaryKind,
    BoundarySignal,
    CanonicalPolicyInputRecord,
    DecisionKind,
    LifecycleKind,
    MessageClass,
    MuteKind,
    MuteSignal,
    PolicyBinding,
    Priority,
    ShadowSpeechPolicy,
    SpeechEnvelope,
    SpeechPolicyConfig,
    SpeechPolicyError,
    SpeechRefresh,
    SpeechRevocation,
    TimingEvidence,
    TriState,
    process_speech_policy,
    process_speech_policy_run,
    replay_speech_policy,
)

BINDING = PolicyBinding("aeis-rig", "race-42", 3, 7)
EVIDENCE_SHA = "a" * 64
OTHER_EVIDENCE_SHA = "b" * 64

type Param = tuple[str, bool | int | float | str]

DEFAULT_PARAMS: dict[MessageClass, tuple[Param, ...]] = {
    MessageClass.FUEL_SHORTAGE: (("shortfall_ml", 1),),
    MessageClass.PIT_CLOSED: (),
    MessageClass.RULES_RISK: (),
    MessageClass.INTEGRITY_ALERT: (),
    MessageClass.BOX_THIS_LAP: (),
    MessageClass.SERVICE_CONTENT: (
        ("fuel_add_ml", 0),
        ("service_code", "FUEL_ONLY"),
    ),
    MessageClass.CRITICAL_STRATEGY_CHANGE: (),
    MessageClass.WINDOW_OPENING_SOON: (("laps", 1),),
    MessageClass.FUEL_SAVE_TARGET: (("milliliters_per_lap", 1),),
    MessageClass.DRIVING_PRACTICE: (("corner", 1),),
    MessageClass.OVERLAY_INFO: (("state", "READY"),),
}


def envelope(
    message_class: MessageClass,
    *,
    at: int,
    conflict: str,
    params: tuple[Param, ...] | None = None,
    deadline: int | None = None,
    binding: PolicyBinding = BINDING,
    evidence_sha256: str = EVIDENCE_SHA,
    supersedes: str | None = None,
    executable: bool = False,
) -> SpeechEnvelope:
    return SpeechEnvelope(
        binding=binding,
        message_class=message_class,
        template_id=MESSAGE_TEMPLATE_ID[message_class],
        scalar_params=DEFAULT_PARAMS[message_class] if params is None else params,
        conflict_key=conflict,
        evidence_sha256=evidence_sha256,
        issued_session_time_us=at,
        valid_until_session_time_us=deadline if deadline is not None else at + 200_000_000,
        supersedes_content_revision_sha256=supersedes,
        executable=executable,
    )


def timing(
    at: int,
    *,
    safe: TriState = TriState.TRUE,
    quality: TriState = TriState.TRUE,
    binding: PolicyBinding = BINDING,
    evidence_sha256: str = EVIDENCE_SHA,
) -> TimingEvidence:
    return TimingEvidence(
        binding=binding,
        session_time_us=at,
        straight=safe,
        brake_clear=safe,
        steering_centered=safe,
        side_by_side_clear=safe,
        quality_stable=quality,
        evidence_sha256=evidence_sha256,
    )


def refresh(
    active: SpeechEnvelope,
    *,
    at: int,
    deadline: int,
    evidence_sha256: str = OTHER_EVIDENCE_SHA,
    expected_revision: str | None = None,
    previous_envelope_sha256: str | None = None,
    executable: bool = False,
) -> SpeechRefresh:
    return SpeechRefresh(
        binding=active.binding,
        conflict_key=active.conflict_key,
        expected_content_revision_sha256=(
            active.content_revision_sha256
            if expected_revision is None
            else expected_revision
        ),
        previous_envelope_sha256=(
            active.envelope_sha256
            if previous_envelope_sha256 is None
            else previous_envelope_sha256
        ),
        evidence_sha256=evidence_sha256,
        session_time_us=at,
        valid_until_session_time_us=deadline,
        executable=executable,
    )


def boundary(kind: BoundaryKind, at: int) -> BoundarySignal:
    return BoundarySignal(BINDING, kind, at, OTHER_EVIDENCE_SHA)


def mute(kind: MuteKind, at: int) -> MuteSignal:
    return MuteSignal(BINDING, kind, at, OTHER_EVIDENCE_SHA)


def unmuted(**overrides: object) -> SpeechPolicyConfig:
    return replace(SpeechPolicyConfig(muted=False), **overrides)


def kinds(items):
    return [item.kind for item in items]


def test_message_allowlist_priorities_templates_and_schemas_are_closed():
    assert MESSAGE_CLASS_PRIORITY == {
        MessageClass.FUEL_SHORTAGE: Priority.P0,
        MessageClass.PIT_CLOSED: Priority.P0,
        MessageClass.RULES_RISK: Priority.P0,
        MessageClass.INTEGRITY_ALERT: Priority.P0,
        MessageClass.BOX_THIS_LAP: Priority.P1,
        MessageClass.SERVICE_CONTENT: Priority.P1,
        MessageClass.CRITICAL_STRATEGY_CHANGE: Priority.P1,
        MessageClass.WINDOW_OPENING_SOON: Priority.P2,
        MessageClass.FUEL_SAVE_TARGET: Priority.P2,
        MessageClass.DRIVING_PRACTICE: Priority.P3,
        MessageClass.OVERLAY_INFO: Priority.P3,
    }
    with pytest.raises(TypeError):
        MESSAGE_CLASS_PRIORITY[MessageClass.FUEL_SHORTAGE] = Priority.P3  # type: ignore[index]
    with pytest.raises(TypeError):
        MESSAGE_PARAM_SCHEMA[MessageClass.PIT_CLOSED] = ()  # type: ignore[index]
    assert set(MESSAGE_TEMPLATE_ID) == set(MessageClass)
    assert set(MESSAGE_PARAM_SCHEMA) == set(MessageClass)
    for message_class in MessageClass:
        candidate = envelope(message_class, at=0, conflict=f"valid.{message_class.value}")
        assert candidate.priority is MESSAGE_CLASS_PRIORITY[message_class]


def test_default_mute_discards_candidate_and_outputs_no_audio_or_execution():
    candidate = envelope(MessageClass.BOX_THIS_LAP, at=500_000, conflict="box")
    policy = ShadowSpeechPolicy(BINDING)
    policy.feed([timing(0), timing(250_000), timing(500_000), candidate])
    receipt = policy.finish()

    assert policy.config.muted is True
    assert policy.muted is True
    assert kinds(policy.events) == [LifecycleKind.ISSUE, LifecycleKind.REVOKE]
    assert kinds(policy.decisions) == [DecisionKind.SUPPRESS_MUTED]
    payload = policy.decisions[0].to_dict()
    assert payload["mode"] == "SHADOW_ONLY"
    assert payload["audible"] is False
    assert payload["executable"] is False
    serialized = json.dumps(payload, sort_keys=True)
    assert all(term not in serialized for term in ("rendered_text", "audio", "tts"))
    assert receipt.status == "PASS_SHADOW_ONLY"


def test_receipt_bound_mute_on_clears_and_mute_off_never_replays_old_candidate():
    config = SpeechPolicyConfig(
        muted=True,
        stable_consecutive_samples=1,
        stable_duration_us=0,
    )
    policy = ShadowSpeechPolicy(BINDING, config)
    candidate = envelope(MessageClass.PIT_CLOSED, at=0, conflict="pits")
    policy.feed([mute(MuteKind.MUTE_OFF, 0), timing(0), candidate])
    policy.feed(mute(MuteKind.MUTE_ON, 1))
    policy.feed([mute(MuteKind.MUTE_OFF, 2), timing(2)])
    receipt = policy.finish()

    assert kinds(policy.decisions) == [
        DecisionKind.SHADOW_WOULD_SPEAK,
        DecisionKind.SUPPRESS_MUTED,
    ]
    assert policy.muted is False
    assert receipt.final_muted is False
    assert receipt.to_dict()["input_kind_counts"]["MuteSignal"] == 3
    assert kinds(policy.events) == [LifecycleKind.ISSUE, LifecycleKind.REVOKE]


def test_candidate_received_while_muted_requires_fresh_issue_after_unmute():
    config = SpeechPolicyConfig(stable_consecutive_samples=1, stable_duration_us=0)
    policy = ShadowSpeechPolicy(BINDING, config)
    old = envelope(MessageClass.FUEL_SHORTAGE, at=0, conflict="fuel")
    policy.feed([timing(0, safe=TriState.FALSE), old])
    policy.feed([mute(MuteKind.MUTE_OFF, 1), timing(1, safe=TriState.FALSE)])
    fresh = replace(old, issued_session_time_us=2, valid_until_session_time_us=100)
    policy.feed([fresh, timing(2, safe=TriState.FALSE)])
    policy.finish()

    assert kinds(policy.decisions) == [
        DecisionKind.SUPPRESS_MUTED,
        DecisionKind.SHADOW_WOULD_SPEAK,
    ]
    assert kinds(policy.events) == [
        LifecycleKind.ISSUE,
        LifecycleKind.REVOKE,
        LifecycleKind.ISSUE,
    ]


@pytest.mark.parametrize(
    ("message_class", "params", "error"),
    [
        (MessageClass.FUEL_SHORTAGE, (("shortfall_ml", -1),), "PARAM_OUT_OF_RANGE"),
        (MessageClass.FUEL_SHORTAGE, (("shortfall_ml", True),), "PARAM_TYPE_MISMATCH"),
        (MessageClass.FUEL_SHORTAGE, (("arbitrary", 1),), "PARAM_SCHEMA_MISMATCH"),
        (MessageClass.FUEL_SAVE_TARGET, (("milliliters_per_lap", 0),), "PARAM_OUT_OF_RANGE"),
        (MessageClass.WINDOW_OPENING_SOON, (("laps", 101),), "PARAM_OUT_OF_RANGE"),
        (
            MessageClass.SERVICE_CONTENT,
            (("fuel_add_ml", 0), ("service_code", "EXECUTE")),
            "PARAM_TOKEN_NOT_ALLOWLISTED",
        ),
        (MessageClass.OVERLAY_INFO, (("state", "SAY_THIS"),), "PARAM_TOKEN_NOT_ALLOWLISTED"),
        (MessageClass.PIT_CLOSED, (("unexpected", 1),), "PARAM_SCHEMA_MISMATCH"),
    ],
)
def test_per_class_parameter_schema_rejects_wrong_types_ranges_and_tokens(
    message_class: MessageClass,
    params: tuple[Param, ...],
    error: str,
):
    with pytest.raises(SpeechPolicyError, match=error):
        envelope(message_class, at=0, conflict="invalid", params=params)


def test_envelope_rejects_execution_free_text_nested_values_and_unknown_template():
    with pytest.raises(SpeechPolicyError, match="EXECUTABLE_RECOMMENDATION_FORBIDDEN"):
        envelope(MessageClass.BOX_THIS_LAP, at=0, conflict="box", executable=True)
    with pytest.raises(SpeechPolicyError, match="TEMPLATE_NOT_ALLOWLISTED"):
        replace(
            envelope(MessageClass.PIT_CLOSED, at=0, conflict="pits"),
            template_id="shadow.unreviewed.v1",
        )
    with pytest.raises(SpeechPolicyError, match="FREE_TEXT_PARAM_FORBIDDEN"):
        envelope(
            MessageClass.SERVICE_CONTENT,
            at=0,
            conflict="service",
            params=(("fuel_add_ml", 0), ("service_code", "say no tires")),
        )
    with pytest.raises(SpeechPolicyError, match="NONSCALAR_PARAM_FORBIDDEN"):
        envelope(
            MessageClass.SERVICE_CONTENT,
            at=0,
            conflict="service",
            params=(("fuel_add_ml", 0), ("service_code", ["FUEL_ONLY"])),  # type: ignore[arg-type]
        )
    with pytest.raises(SpeechPolicyError, match="INTEGRITY_ALERT_MUST_BE_NONTACTICAL"):
        envelope(
            MessageClass.INTEGRITY_ALERT,
            at=0,
            conflict="integrity",
            params=(("amount_ml", 2000),),
        )


def test_contract_is_immutable_and_revision_hash_covers_content_not_lineage_edges():
    first = envelope(
        MessageClass.FUEL_SAVE_TARGET,
        at=1,
        conflict="fuel.target",
        params=(("milliliters_per_lap", 125),),
    )
    refreshed = envelope(
        MessageClass.FUEL_SAVE_TARGET,
        at=2,
        conflict="fuel.target",
        params=(("milliliters_per_lap", 125),),
        evidence_sha256=OTHER_EVIDENCE_SHA,
        supersedes="c" * 64,
    )
    assert first.content_revision_sha256 == refreshed.content_revision_sha256
    assert first.envelope_sha256 != refreshed.envelope_sha256
    with pytest.raises(FrozenInstanceError):
        first.conflict_key = "changed"  # type: ignore[misc]


def test_refresh_updates_only_evidence_and_deadline_without_reissuing_content():
    config = unmuted(stable_consecutive_samples=1, stable_duration_us=0)
    first = envelope(
        MessageClass.BOX_THIS_LAP,
        at=0,
        conflict="box",
        deadline=10,
    )
    heartbeat = refresh(first, at=1, deadline=100)
    policy = ShadowSpeechPolicy(BINDING, config)
    policy.feed(first)
    policy.feed([timing(1), heartbeat])
    receipt = policy.finish()

    assert kinds(policy.events) == [LifecycleKind.ISSUE, LifecycleKind.NO_CHANGE]
    assert policy.events[-1].reason_codes == ("EVIDENCE_DEADLINE_REFRESHED",)
    assert policy.events[-1].previous_revision_sha256 == first.content_revision_sha256
    assert policy.events[-1].current_revision_sha256 == first.content_revision_sha256
    assert kinds(policy.decisions) == [
        DecisionKind.HOLD_UNSAFE,
        DecisionKind.SHADOW_WOULD_SPEAK,
    ]
    assert policy.decisions[-1].message_evidence_sha256 == OTHER_EVIDENCE_SHA
    assert policy.decisions[-1].audible is False
    assert policy.decisions[-1].executable is False

    snapshot = policy.final_active_envelopes[0]
    refreshed = snapshot.envelope
    assert refreshed.content_revision_sha256 == first.content_revision_sha256
    assert refreshed.message_class is first.message_class
    assert refreshed.template_id == first.template_id
    assert refreshed.scalar_params == first.scalar_params
    assert refreshed.issued_session_time_us == first.issued_session_time_us
    assert refreshed.evidence_sha256 == OTHER_EVIDENCE_SHA
    assert refreshed.valid_until_session_time_us == 100
    assert refreshed.envelope_sha256 != first.envelope_sha256
    assert receipt.final_active_envelope_count == 1
    assert receipt.to_dict()["input_kind_counts"]["SpeechRefresh"] == 1


def test_refresh_contract_forbids_execution_and_nonfuture_deadline():
    active = envelope(MessageClass.PIT_CLOSED, at=0, conflict="pits")
    with pytest.raises(SpeechPolicyError, match="EXECUTABLE_RECOMMENDATION_FORBIDDEN"):
        refresh(active, at=1, deadline=2, executable=True)
    with pytest.raises(SpeechPolicyError, match="INVALID_REFRESH_DEADLINE"):
        refresh(active, at=1, deadline=1)


@pytest.mark.parametrize(
    ("expected_revision", "previous_hash", "error"),
    [
        ("c" * 64, None, "REFRESH_CONTENT_REVISION_PRECONDITION_FAILED"),
        (None, "c" * 64, "REFRESH_ENVELOPE_PRECONDITION_FAILED"),
    ],
)
def test_refresh_requires_exact_active_revision_and_previous_envelope_hash(
    expected_revision: str | None,
    previous_hash: str | None,
    error: str,
):
    active = envelope(MessageClass.BOX_THIS_LAP, at=0, conflict="box")
    heartbeat = refresh(
        active,
        at=1,
        deadline=100,
        expected_revision=expected_revision,
        previous_envelope_sha256=previous_hash,
    )
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    policy.feed([active, heartbeat])
    with pytest.raises(SpeechPolicyError, match=error):
        policy.finish()
    receipt = policy.finish()
    assert receipt.status == "REJECTED"
    assert receipt.failure_code == error
    assert receipt.final_active_envelope_count == 0


def test_replayed_refresh_is_stale_after_first_compare_and_swap():
    active = envelope(MessageClass.OVERLAY_INFO, at=0, conflict="overlay")
    first_refresh = refresh(active, at=1, deadline=100)
    replay = replace(
        first_refresh,
        session_time_us=2,
        valid_until_session_time_us=200,
    )
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    policy.feed([active, first_refresh, replay])
    with pytest.raises(
        SpeechPolicyError, match="REFRESH_ENVELOPE_PRECONDITION_FAILED"
    ):
        policy.finish()
    assert policy.finish().failure_code == "REFRESH_ENVELOPE_PRECONDITION_FAILED"
    assert kinds(policy.events) == [LifecycleKind.ISSUE, LifecycleKind.NO_CHANGE]


def test_refresh_at_exclusive_deadline_cannot_resurrect_expired_envelope():
    active = envelope(
        MessageClass.BOX_THIS_LAP,
        at=0,
        conflict="box",
        deadline=10,
    )
    heartbeat = refresh(active, at=10, deadline=100)
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    policy.feed([active, heartbeat])
    with pytest.raises(SpeechPolicyError, match="REFRESH_WITHOUT_ACTIVE_ENVELOPE"):
        policy.finish()
    receipt = policy.finish()

    assert kinds(policy.events) == [LifecycleKind.ISSUE, LifecycleKind.REVOKE]
    assert kinds(policy.decisions)[-1] is DecisionKind.DROP_EXPIRED
    assert receipt.failure_code == "REFRESH_WITHOUT_ACTIVE_ENVELOPE"
    assert receipt.final_active_envelope_count == 0


@pytest.mark.parametrize("attack", ["revoke", "mute"])
def test_same_time_revoke_or_mute_wins_before_refresh_for_every_arrival_chunk(
    attack: str,
):
    active = envelope(MessageClass.BOX_THIS_LAP, at=0, conflict="box")
    heartbeat = refresh(active, at=1, deadline=100)
    guard: SpeechRevocation | MuteSignal
    if attack == "revoke":
        guard = SpeechRevocation(
            BINDING,
            active.conflict_key,
            active.content_revision_sha256,
            1,
            EVIDENCE_SHA,
        )
    else:
        guard = mute(MuteKind.MUTE_ON, 1)

    def rejected(order: list[SpeechRefresh | SpeechRevocation | MuteSignal], split: bool):
        policy = ShadowSpeechPolicy(BINDING, unmuted())
        policy.feed(active)
        if split:
            for item in order:
                policy.feed(item)
        else:
            policy.feed(order)
        with pytest.raises(SpeechPolicyError, match="REFRESH_WITHOUT_ACTIVE_ENVELOPE"):
            policy.finish()
        receipt = policy.finish()
        return (
            policy.input_records,
            policy.events,
            policy.decisions,
            policy.final_active_envelopes,
            receipt,
        )

    artifacts = [
        rejected([guard, heartbeat], False),
        rejected([heartbeat, guard], False),
        rejected([guard, heartbeat], True),
        rejected([heartbeat, guard], True),
    ]
    assert all(artifact == artifacts[0] for artifact in artifacts[1:])
    assert artifacts[0][-1].final_active_envelope_count == 0


def test_default_mute_and_duplicate_same_time_refresh_fail_closed():
    active = envelope(MessageClass.BOX_THIS_LAP, at=0, conflict="box")
    heartbeat = refresh(active, at=1, deadline=100)
    muted_policy = ShadowSpeechPolicy(BINDING)
    muted_policy.feed([active, heartbeat])
    with pytest.raises(SpeechPolicyError, match="REFRESH_WITHOUT_ACTIVE_ENVELOPE"):
        muted_policy.finish()
    muted_receipt = muted_policy.finish()
    assert muted_receipt.final_muted is True
    assert all(item.audible is False for item in muted_policy.decisions)
    assert all(item.executable is False for item in muted_policy.decisions)

    duplicate = ShadowSpeechPolicy(BINDING, unmuted())
    duplicate.feed([active, heartbeat, heartbeat])
    with pytest.raises(SpeechPolicyError, match="MULTIPLE_REFRESHES_SAME_CONFLICT"):
        duplicate.finish()
    assert duplicate.finish().status == "REJECTED"


def test_refresh_order_chunk_and_canonical_record_replay_are_identical():
    config = unmuted(stable_consecutive_samples=1, stable_duration_us=0)
    active = envelope(MessageClass.BOX_THIS_LAP, at=0, conflict="box")
    heartbeat = refresh(active, at=1, deadline=100)
    current_timing = timing(1)
    batch_a = process_speech_policy_run(
        BINDING, [active, heartbeat, current_timing], config=config
    )
    batch_b = process_speech_policy_run(
        BINDING, [active, current_timing, heartbeat], config=config
    )
    chunked_policy = ShadowSpeechPolicy(BINDING, config)
    chunked_policy.feed(active)
    chunked_policy.feed(current_timing)
    chunked_policy.feed(heartbeat)
    chunked_receipt = chunked_policy.finish()
    chunked = (
        chunked_policy.input_records,
        chunked_policy.events,
        chunked_policy.decisions,
        chunked_policy.final_active_envelopes,
        chunked_receipt,
    )

    assert batch_a == batch_b
    assert chunked == (
        batch_a.input_records,
        batch_a.events,
        batch_a.decisions,
        batch_a.final_active_envelopes,
        batch_a.receipt,
    )

    persisted = json.loads(
        json.dumps([item.to_dict() for item in batch_a.input_records])
    )
    restored = tuple(CanonicalPolicyInputRecord.from_dict(item) for item in persisted)
    replayed = replay_speech_policy(BINDING, restored, config=config)
    assert replayed == batch_a
    with pytest.raises(FrozenInstanceError):
        restored[0].sequence = 99  # type: ignore[misc]

    snapshot_payload = batch_a.final_active_envelopes[0].to_dict()
    assert ActiveEnvelopeSnapshot.from_dict(snapshot_payload) == (
        batch_a.final_active_envelopes[0]
    )
    with pytest.raises(FrozenInstanceError):
        batch_a.final_active_envelopes[0].conflict_key = "changed"  # type: ignore[misc]
    canonical_snapshots = json.dumps(
        [item.to_dict() for item in batch_a.final_active_envelopes],
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert batch_a.receipt.final_active_envelopes_sha256 == hashlib.sha256(
        canonical_snapshots
    ).hexdigest()

    tampered = dict(persisted[0])
    tampered_payload = dict(tampered["payload"])
    tampered_payload["evidence_sha256"] = "f" * 64
    tampered["payload"] = tampered_payload
    with pytest.raises(
        SpeechPolicyError, match="CANONICAL_INPUT_RECORD_HASH_MISMATCH"
    ):
        CanonicalPolicyInputRecord.from_dict(tampered)


def test_p0_bypasses_road_window_and_cooldown_but_not_quality_stability():
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    candidate = envelope(
        MessageClass.FUEL_SHORTAGE,
        at=250_000,
        conflict="fuel.shortage",
        params=(("shortfall_ml", 2200),),
    )
    policy.feed(
        [
            timing(0, safe=TriState.FALSE),
            timing(250_000, safe=TriState.UNKNOWN),
            candidate,
            timing(500_000, safe=TriState.FALSE),
        ]
    )
    policy.finish()

    assert kinds(policy.decisions) == [
        DecisionKind.HOLD_UNSAFE,
        DecisionKind.SHADOW_WOULD_SPEAK,
    ]
    decision = policy.decisions[-1]
    assert decision.priority is Priority.P0
    assert decision.timing_evidence is not None
    assert decision.timing_evidence.straight is TriState.FALSE


def test_p1_and_p2_require_all_true_continuous_safe_window():
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    candidate = envelope(
        MessageClass.WINDOW_OPENING_SOON,
        at=250_000,
        conflict="pit.window",
        params=(("laps", 2),),
    )
    policy.feed(
        [
            timing(0),
            timing(250_000),
            candidate,
            timing(500_000, safe=TriState.UNKNOWN),
            timing(750_000),
            timing(1_000_000),
            timing(1_250_000),
        ]
    )
    policy.finish()

    assert kinds(policy.decisions) == [
        DecisionKind.HOLD_UNSAFE,
        DecisionKind.SHADOW_WOULD_SPEAK,
    ]
    assert policy.decisions[-1].priority is Priority.P2


def test_large_timing_gap_resets_consecutive_windows_instead_of_faking_continuity():
    config = unmuted(max_timing_gap_us=1_000_000)
    candidate = envelope(MessageClass.BOX_THIS_LAP, at=250_000, conflict="box")
    insufficient = ShadowSpeechPolicy(BINDING, config)
    insufficient.feed([timing(0), timing(250_000), candidate, timing(100_000_000)])
    insufficient.finish()
    assert kinds(insufficient.decisions) == [DecisionKind.HOLD_UNSAFE]

    sufficient = ShadowSpeechPolicy(BINDING, config)
    sufficient.feed(
        [
            timing(0),
            timing(250_000),
            candidate,
            timing(100_000_000),
            timing(100_250_000),
            timing(100_500_000),
        ]
    )
    sufficient.finish()
    assert kinds(sufficient.decisions)[-1] is DecisionKind.SHADOW_WOULD_SPEAK
    assert sufficient.decisions[-1].quality_stable_since_session_time_us == 100_000_000


def test_quality_unknown_at_same_timestamp_revokes_new_tactical_candidate():
    config = unmuted(stable_consecutive_samples=1, stable_duration_us=0)
    policy = ShadowSpeechPolicy(BINDING, config)
    candidate = envelope(MessageClass.BOX_THIS_LAP, at=0, conflict="box")
    policy.feed([candidate, timing(0, quality=TriState.UNKNOWN)])
    policy.feed([replace(candidate, issued_session_time_us=1), timing(1)])
    policy.finish()

    assert kinds(policy.events) == [
        LifecycleKind.ISSUE,
        LifecycleKind.REVOKE,
        LifecycleKind.ISSUE,
    ]
    assert kinds(policy.decisions) == [
        DecisionKind.SUPPRESS_BOUNDARY,
        DecisionKind.SHADOW_WOULD_SPEAK,
    ]


def test_p3_is_lifecycle_log_only_when_explicitly_superseded_and_revoked():
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    first = envelope(
        MessageClass.DRIVING_PRACTICE,
        at=0,
        conflict="practice.corner",
        params=(("corner", 1),),
    )
    changed = envelope(
        MessageClass.DRIVING_PRACTICE,
        at=1,
        conflict="practice.corner",
        params=(("corner", 2),),
        supersedes=first.content_revision_sha256,
    )
    revoke = SpeechRevocation(
        BINDING,
        changed.conflict_key,
        changed.content_revision_sha256,
        2,
        EVIDENCE_SHA,
    )
    policy.feed([first, changed, revoke])
    policy.finish()

    assert kinds(policy.events) == [
        LifecycleKind.ISSUE,
        LifecycleKind.REVOKE,
        LifecycleKind.ISSUE,
        LifecycleKind.REVOKE,
    ]
    assert policy.decisions == ()


def test_supersession_is_exact_revoke_issue_and_same_content_is_no_change():
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    first = envelope(MessageClass.SERVICE_CONTENT, at=0, conflict="service")
    changed = envelope(
        MessageClass.SERVICE_CONTENT,
        at=1,
        conflict="service",
        params=(("fuel_add_ml", 20_000), ("service_code", "FUEL_AND_TIRES")),
        supersedes=first.content_revision_sha256,
    )
    repeated = replace(
        changed,
        issued_session_time_us=2,
        valid_until_session_time_us=200,
        evidence_sha256=OTHER_EVIDENCE_SHA,
        supersedes_content_revision_sha256=None,
    )
    policy.feed([first, changed, repeated])
    policy.finish()

    assert kinds(policy.events) == [
        LifecycleKind.ISSUE,
        LifecycleKind.REVOKE,
        LifecycleKind.ISSUE,
        LifecycleKind.NO_CHANGE,
    ]
    revoke_event, issue_event = policy.events[1:3]
    assert revoke_event.previous_revision_sha256 == first.content_revision_sha256
    assert revoke_event.current_revision_sha256 == changed.content_revision_sha256
    assert issue_event.previous_revision_sha256 == first.content_revision_sha256
    assert issue_event.current_revision_sha256 == changed.content_revision_sha256
    assert kinds(policy.decisions).count(DecisionKind.DROP_REVOKED) == 1


@pytest.mark.parametrize("supersedes", [None, "c" * 64])
def test_changed_content_requires_exact_active_revision(supersedes: str | None):
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    first = envelope(MessageClass.FUEL_SAVE_TARGET, at=0, conflict="fuel")
    changed = envelope(
        MessageClass.FUEL_SAVE_TARGET,
        at=1,
        conflict="fuel",
        params=(("milliliters_per_lap", 2),),
        supersedes=supersedes,
    )
    policy.feed([first, changed])
    with pytest.raises(SpeechPolicyError, match="SUPERSEDES_PRECONDITION_FAILED"):
        policy.finish()
    assert policy.finish().status == "REJECTED"


def test_initial_candidate_rejects_non_none_supersedes():
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    policy.feed(
        envelope(
            MessageClass.FUEL_SAVE_TARGET,
            at=0,
            conflict="fuel",
            supersedes="c" * 64,
        )
    )
    with pytest.raises(SpeechPolicyError, match="UNEXPECTED_SUPERSEDES_WITHOUT_ACTIVE"):
        policy.finish()
    assert policy.finish().failure_code == "UNEXPECTED_SUPERSEDES_WITHOUT_ACTIVE"


def test_unchanged_content_requires_none_supersedes_by_documented_rule():
    first = envelope(MessageClass.FUEL_SAVE_TARGET, at=0, conflict="fuel")
    duplicate = replace(
        first,
        issued_session_time_us=1,
        valid_until_session_time_us=100,
        supersedes_content_revision_sha256=first.content_revision_sha256,
    )
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    policy.feed([first, duplicate])
    with pytest.raises(SpeechPolicyError, match="NO_CHANGE_MUST_NOT_SUPERSEDE"):
        policy.finish()
    assert policy.finish().status == "REJECTED"


def test_delayed_a_after_b_is_rejected_unless_it_explicitly_supersedes_b():
    first = envelope(
        MessageClass.FUEL_SAVE_TARGET,
        at=0,
        conflict="fuel",
        params=(("milliliters_per_lap", 100),),
    )
    second = envelope(
        MessageClass.FUEL_SAVE_TARGET,
        at=1,
        conflict="fuel",
        params=(("milliliters_per_lap", 200),),
        supersedes=first.content_revision_sha256,
    )
    delayed = replace(first, issued_session_time_us=2, valid_until_session_time_us=200)
    rejected = ShadowSpeechPolicy(BINDING, unmuted())
    rejected.feed([first, second, delayed])
    with pytest.raises(SpeechPolicyError, match="SUPERSEDES_PRECONDITION_FAILED"):
        rejected.finish()

    explicit = replace(
        delayed,
        supersedes_content_revision_sha256=second.content_revision_sha256,
    )
    accepted = ShadowSpeechPolicy(BINDING, unmuted())
    accepted.feed([first, second, explicit])
    assert accepted.finish().status == "PASS_SHADOW_ONLY"
    assert kinds(accepted.events).count(LifecycleKind.ISSUE) == 3


def test_explicit_revoke_is_revision_guarded_and_idempotent():
    candidate = envelope(MessageClass.PIT_CLOSED, at=0, conflict="pits")
    stale = SpeechRevocation(BINDING, candidate.conflict_key, "c" * 64, 1, EVIDENCE_SHA)
    exact = replace(
        stale,
        expected_content_revision_sha256=candidate.content_revision_sha256,
        session_time_us=2,
    )
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    policy.feed([candidate, stale, exact, replace(exact, session_time_us=3)])
    policy.finish()

    assert kinds(policy.events) == [
        LifecycleKind.ISSUE,
        LifecycleKind.NO_CHANGE,
        LifecycleKind.REVOKE,
        LifecycleKind.NO_CHANGE,
    ]
    assert policy.events[1].reason_codes == ("STALE_REVOKE_IGNORED",)
    assert policy.events[-1].reason_codes == ("ALREADY_INACTIVE",)


@pytest.mark.parametrize("kind", list(BoundaryKind))
def test_each_boundary_revokes_tactical_state_and_clears_pending(kind: BoundaryKind):
    candidate = envelope(MessageClass.CRITICAL_STRATEGY_CHANGE, at=0, conflict="strategy")
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    policy.feed([candidate, boundary(kind, 1)])
    policy.finish()

    assert kinds(policy.events)[-1] is LifecycleKind.REVOKE
    assert kinds(policy.decisions)[-1] is DecisionKind.SUPPRESS_BOUNDARY
    assert kind.value in policy.decisions[-1].reason_codes


@pytest.mark.parametrize(
    "kind",
    [
        BoundaryKind.SOURCE_STALE,
        BoundaryKind.DROPPED_TICKS,
        BoundaryKind.QUALITY_REJECTED,
    ],
)
def test_boundary_clears_candidate_even_when_both_share_timestamp(kind: BoundaryKind):
    config = unmuted(stable_consecutive_samples=1, stable_duration_us=0)
    policy = ShadowSpeechPolicy(BINDING, config)
    policy.feed(
        [
            timing(0),
            envelope(MessageClass.BOX_THIS_LAP, at=0, conflict="box"),
            boundary(kind, 0),
        ]
    )
    policy.finish()

    assert DecisionKind.SHADOW_WOULD_SPEAK not in kinds(policy.decisions)
    assert kinds(policy.decisions) == [DecisionKind.SUPPRESS_BOUNDARY]


@pytest.mark.parametrize("kind", [BoundaryKind.SOURCE_RESET, BoundaryKind.SESSION_RESET])
def test_reset_invalidates_epoch_and_new_candidate_cannot_reopen_it(kind: BoundaryKind):
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    policy.feed(
        [boundary(kind, 0), envelope(MessageClass.FUEL_SHORTAGE, at=1, conflict="fuel")]
    )
    policy.finish()

    assert policy.events == ()
    assert kinds(policy.decisions) == [DecisionKind.SUPPRESS_BOUNDARY]
    assert "IDENTITY_EPOCH_INVALIDATED" in policy.decisions[0].reason_codes


def test_same_timestamp_priority_is_order_and_chunk_invariant():
    config = unmuted(
        stable_consecutive_samples=1,
        stable_duration_us=0,
        global_cooldown_us=100,
        per_conflict_cooldown_us=100,
    )
    p1 = envelope(MessageClass.BOX_THIS_LAP, at=0, conflict="box")
    p2 = envelope(MessageClass.FUEL_SAVE_TARGET, at=0, conflict="fuel")
    orders = ([p2, timing(0), p1], [p1, p2, timing(0)])
    results = [process_speech_policy(BINDING, inputs, config=config) for inputs in orders]

    assert results[0] == results[1]
    events, decisions, receipt = results[0]
    assert [event.conflict_key for event in events] == ["box", "fuel"]
    assert kinds(decisions) == [
        DecisionKind.SHADOW_WOULD_SPEAK,
        DecisionKind.HOLD_COOLDOWN,
    ]
    assert decisions[0].priority is Priority.P1
    assert receipt.input_count == 3


def test_global_and_per_conflict_cooldowns_open_at_exact_boundaries():
    config = unmuted(
        stable_consecutive_samples=1,
        stable_duration_us=0,
        global_cooldown_us=100,
        per_conflict_cooldown_us=200,
    )
    policy = ShadowSpeechPolicy(BINDING, config)
    first = envelope(
        MessageClass.FUEL_SAVE_TARGET,
        at=0,
        conflict="fuel",
        params=(("milliliters_per_lap", 100),),
    )
    changed = envelope(
        MessageClass.FUEL_SAVE_TARGET,
        at=100,
        conflict="fuel",
        params=(("milliliters_per_lap", 200),),
        supersedes=first.content_revision_sha256,
    )
    policy.feed([first, timing(0), changed, timing(100), timing(199), timing(200)])
    policy.finish()

    assert kinds(policy.decisions) == [
        DecisionKind.SHADOW_WOULD_SPEAK,
        DecisionKind.DROP_REVOKED,
        DecisionKind.HOLD_COOLDOWN,
        DecisionKind.SHADOW_WOULD_SPEAK,
    ]
    assert "PER_CONFLICT_COOLDOWN" in policy.decisions[2].reason_codes
    assert policy.decisions[-1].session_time_us == 200


def test_p0_bypasses_cooldown_but_starts_it_for_lower_priority():
    config = unmuted(
        stable_consecutive_samples=1,
        stable_duration_us=0,
        global_cooldown_us=100,
        per_conflict_cooldown_us=100,
    )
    policy = ShadowSpeechPolicy(BINDING, config)
    policy.feed(
        [
            envelope(MessageClass.FUEL_SHORTAGE, at=0, conflict="fuel"),
            envelope(MessageClass.PIT_CLOSED, at=0, conflict="pits"),
            envelope(MessageClass.BOX_THIS_LAP, at=0, conflict="box"),
            timing(0),
        ]
    )
    policy.finish()

    assert kinds(policy.decisions) == [
        DecisionKind.SHADOW_WOULD_SPEAK,
        DecisionKind.SHADOW_WOULD_SPEAK,
        DecisionKind.HOLD_COOLDOWN,
    ]


def test_deadline_is_exclusive_and_precedes_timing_at_same_timestamp():
    config = unmuted(stable_consecutive_samples=1, stable_duration_us=0)
    candidate = envelope(
        MessageClass.FUEL_SHORTAGE,
        at=0,
        conflict="fuel",
        deadline=100,
    )
    policy = ShadowSpeechPolicy(BINDING, config)
    policy.feed([candidate, timing(100, safe=TriState.FALSE)])
    policy.finish()

    assert kinds(policy.events) == [LifecycleKind.ISSUE, LifecycleKind.REVOKE]
    assert kinds(policy.decisions) == [
        DecisionKind.HOLD_UNSAFE,
        DecisionKind.DROP_EXPIRED,
    ]
    assert policy.decisions[-1].session_time_us == 100


def test_identity_mismatch_and_time_regression_fail_closed_with_receipt():
    other = replace(BINDING, session_epoch=8)
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    policy.feed(envelope(MessageClass.BOX_THIS_LAP, at=10, conflict="box"))
    policy.feed(timing(11))
    with pytest.raises(SpeechPolicyError, match="IDENTITY_MISMATCH"):
        policy.feed(timing(12, binding=other))
    receipt = policy.finish()
    assert receipt.status == "REJECTED"
    assert receipt.failure_code == "IDENTITY_MISMATCH"
    assert kinds(policy.decisions)[-1] is DecisionKind.SUPPRESS_BOUNDARY

    regressed = ShadowSpeechPolicy(BINDING, unmuted())
    regressed.feed(timing(10))
    with pytest.raises(SpeechPolicyError, match="SESSION_TIME_REGRESSION"):
        regressed.feed(timing(9))
    assert regressed.finish().failure_code == "SESSION_TIME_REGRESSION"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_id", "other-rig"),
        ("session_id", "other-session"),
        ("source_epoch", 4),
        ("session_epoch", 8),
    ],
)
def test_every_identity_component_is_bound(field: str, value: str | int):
    foreign = replace(BINDING, **{field: value})
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    with pytest.raises(SpeechPolicyError, match="IDENTITY_MISMATCH"):
        policy.feed(
            envelope(
                MessageClass.FUEL_SHORTAGE,
                at=0,
                conflict="foreign",
                binding=foreign,
            )
        )
    assert policy.finish().status == "REJECTED"


def test_duplicate_timing_cannot_fake_samples_and_conflicting_duplicate_rejects():
    first = timing(0)
    candidate = envelope(MessageClass.BOX_THIS_LAP, at=0, conflict="box")
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    policy.feed([first, first, first, candidate])
    policy.finish()
    assert kinds(policy.decisions) == [DecisionKind.HOLD_UNSAFE]

    conflicting = ShadowSpeechPolicy(BINDING, unmuted())
    conflicting.feed([first, replace(first, evidence_sha256=OTHER_EVIDENCE_SHA)])
    with pytest.raises(SpeechPolicyError, match="CONFLICTING_TIMING_EVIDENCE"):
        conflicting.finish()
    assert conflicting.finish().status == "REJECTED"


def test_conflicting_same_timestamp_inputs_reject_instead_of_using_arrival_order():
    policy = ShadowSpeechPolicy(BINDING, unmuted())
    policy.feed([mute(MuteKind.MUTE_ON, 0), mute(MuteKind.MUTE_OFF, 0)])
    with pytest.raises(SpeechPolicyError, match="CONFLICTING_MUTE_SIGNALS"):
        policy.finish()

    first = envelope(MessageClass.FUEL_SAVE_TARGET, at=0, conflict="fuel")
    second = replace(
        first,
        scalar_params=(("milliliters_per_lap", 2),),
        supersedes_content_revision_sha256=first.content_revision_sha256,
    )
    duplicate = ShadowSpeechPolicy(BINDING, unmuted())
    duplicate.feed([first, second])
    with pytest.raises(SpeechPolicyError, match="MULTIPLE_CANDIDATES"):
        duplicate.finish()


def test_chunking_does_not_change_events_decisions_or_receipt():
    inputs = [
        envelope(MessageClass.FUEL_SHORTAGE, at=0, conflict="fuel"),
        timing(0, safe=TriState.FALSE),
        timing(250_000, safe=TriState.UNKNOWN),
        envelope(MessageClass.OVERLAY_INFO, at=500_000, conflict="overlay"),
        timing(500_000, safe=TriState.FALSE),
    ]
    config = unmuted()
    batch = process_speech_policy(BINDING, inputs, config=config)

    chunked = ShadowSpeechPolicy(BINDING, config)
    chunked.feed(inputs[0])
    chunked.feed_chunk(iter(inputs[1:3]))
    chunked.feed(inputs[3:])
    receipt = chunked.finish()
    assert (chunked.events, chunked.decisions, receipt) == batch
    assert chunked.finish() is receipt
    with pytest.raises(RuntimeError, match="already finished"):
        chunked.feed(inputs[0])

    payload = receipt.to_dict()
    claimed = payload.pop("receipt_sha256")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert claimed == hashlib.sha256(canonical).hexdigest()


def test_receipt_is_identical_across_python_hash_seeds():
    script = r'''
import json
from iracing_ai_engineer.speech_policy import *
b = PolicyBinding("rig", "session", 1, 2)
c = SpeechPolicyConfig(muted=False, stable_consecutive_samples=1, stable_duration_us=0)
e = SpeechEnvelope(b, MessageClass.OVERLAY_INFO,
                   MESSAGE_TEMPLATE_ID[MessageClass.OVERLAY_INFO],
                   (("state", "READY"),), "overlay", "b" * 64, 0, 10)
r = SpeechRefresh(b, "overlay", e.content_revision_sha256,
                  e.envelope_sha256, "c" * 64, 1, 20)
run = process_speech_policy_run(b, [e, r], config=c)
print(json.dumps(run.to_dict(), sort_keys=True, separators=(",", ":")))
'''
    outputs = []
    for seed in ("1", "987654"):
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": seed,
                "PYTHONPATH": os.path.abspath("src"),
            }
        )
        outputs.append(
            subprocess.run(
                [sys.executable, "-c", script],
                check=True,
                capture_output=True,
                text=True,
                cwd=os.getcwd(),
                env=environment,
            ).stdout
        )
    assert outputs[0] == outputs[1]
    artifact = json.loads(outputs[0])
    receipt = artifact["receipt"]
    assert receipt["status"] == "PASS_SHADOW_ONLY"
    assert len(receipt["receipt_sha256"]) == 64
    assert receipt["final_active_envelope_count"] == 1
    assert len(artifact["input_records"]) == 2
    assert len(artifact["final_active_envelopes"]) == 1

"""Deterministic, shadow-only policy for future race-engineer speech.

The policy deliberately has no renderer, TTS, audio, network, simulator, or
control integration.  Its strongest positive result is an immutable
``SHADOW_WOULD_SPEAK`` audit record whose ``audible`` and ``executable`` fields
are always false.

Inputs are bound to one source/session epoch.  Timing is based only on the
caller's monotonic ``session_time_us``.  Reset, stale, dropped-tick, rejected
quality, identity mismatch, or time regression evidence fails closed and
clears tactical speech state.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum, StrEnum
from types import MappingProxyType

SPEECH_POLICY_CONTRACT_VERSION = "shadow-speech-policy-v2"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
_PARAMETER_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ENUM_VALUE = re.compile(r"[A-Z0-9][A-Z0-9_.:/-]{0,63}")


class SpeechPolicyError(ValueError):
    """A fail-closed speech-policy contract rejection."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(code if message is None else f"{code}: {message}")


class Priority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class MessageClass(StrEnum):
    FUEL_SHORTAGE = "fuel_shortage"
    PIT_CLOSED = "pit_closed"
    RULES_RISK = "rules_risk"
    INTEGRITY_ALERT = "integrity_alert"
    BOX_THIS_LAP = "box_this_lap"
    SERVICE_CONTENT = "service_content"
    CRITICAL_STRATEGY_CHANGE = "critical_strategy_change"
    WINDOW_OPENING_SOON = "window_opening_soon"
    FUEL_SAVE_TARGET = "fuel_save_target"
    DRIVING_PRACTICE = "driving_practice"
    OVERLAY_INFO = "overlay_info"


MESSAGE_CLASS_PRIORITY: Mapping[MessageClass, Priority] = MappingProxyType(
    {
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
)

# A template identifier is data, not free-form speech.  The policy accepts one
# fixed, versioned template per message class and never renders it.
MESSAGE_TEMPLATE_ID: Mapping[MessageClass, str] = MappingProxyType(
    {message_class: f"shadow.{message_class.value}.v1" for message_class in MessageClass}
)


class TriState(StrEnum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


class LifecycleKind(StrEnum):
    ISSUE = "ISSUE"
    REVOKE = "REVOKE"
    NO_CHANGE = "NO_CHANGE"


class DecisionKind(StrEnum):
    SHADOW_WOULD_SPEAK = "SHADOW_WOULD_SPEAK"
    SUPPRESS_MUTED = "SUPPRESS_MUTED"
    SUPPRESS_BOUNDARY = "SUPPRESS_BOUNDARY"
    HOLD_UNSAFE = "HOLD_UNSAFE"
    HOLD_COOLDOWN = "HOLD_COOLDOWN"
    DROP_EXPIRED = "DROP_EXPIRED"
    DROP_REVOKED = "DROP_REVOKED"


class BoundaryKind(StrEnum):
    SOURCE_RESET = "SOURCE_RESET"
    SESSION_RESET = "SESSION_RESET"
    SOURCE_STALE = "SOURCE_STALE"
    DROPPED_TICKS = "DROPPED_TICKS"
    QUALITY_REJECTED = "QUALITY_REJECTED"


class MuteKind(StrEnum):
    MUTE_ON = "MUTE_ON"
    MUTE_OFF = "MUTE_OFF"


def _require_non_negative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SpeechPolicyError("INVALID_INTEGER", f"{name} must be a non-negative integer")
    return value


def _require_identifier(name: str, value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise SpeechPolicyError("INVALID_IDENTIFIER", f"{name} is invalid")
    return value


def _require_sha256(name: str, value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise SpeechPolicyError("INVALID_SHA256", f"{name} must be lowercase SHA-256")
    return value


def _jsonable(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SpeechPolicyError("NONFINITE_VALUE")
        return 0.0 if value == 0.0 else value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


type Scalar = bool | int | float | str
type ScalarParams = tuple[tuple[str, Scalar], ...]


@dataclass(frozen=True, slots=True)
class _ScalarRule:
    name: str
    scalar_type: str
    minimum: int | None = None
    maximum: int | None = None
    allowed_tokens: tuple[str, ...] = ()


_MESSAGE_PARAM_RULES: Mapping[MessageClass, tuple[_ScalarRule, ...]] = MappingProxyType(
    {
        MessageClass.FUEL_SHORTAGE: (
            _ScalarRule("shortfall_ml", "int", minimum=1, maximum=500_000),
        ),
        MessageClass.PIT_CLOSED: (),
        MessageClass.RULES_RISK: (),
        MessageClass.INTEGRITY_ALERT: (),
        MessageClass.BOX_THIS_LAP: (),
        MessageClass.SERVICE_CONTENT: (
            _ScalarRule("fuel_add_ml", "int", minimum=0, maximum=500_000),
            _ScalarRule(
                "service_code",
                "token",
                allowed_tokens=(
                    "FUEL_AND_TIRES",
                    "FUEL_ONLY",
                    "NO_SERVICE",
                    "TIRES_ONLY",
                ),
            ),
        ),
        MessageClass.CRITICAL_STRATEGY_CHANGE: (),
        MessageClass.WINDOW_OPENING_SOON: (
            _ScalarRule("laps", "int", minimum=1, maximum=100),
        ),
        MessageClass.FUEL_SAVE_TARGET: (
            _ScalarRule(
                "milliliters_per_lap",
                "int",
                minimum=1,
                maximum=10_000,
            ),
        ),
        MessageClass.DRIVING_PRACTICE: (
            _ScalarRule("corner", "int", minimum=1, maximum=1_000),
        ),
        MessageClass.OVERLAY_INFO: (
            _ScalarRule(
                "state",
                "token",
                allowed_tokens=("BLOCKED", "READY", "STALE", "WAIT_DATA"),
            ),
        ),
    }
)

MESSAGE_PARAM_SCHEMA: Mapping[
    MessageClass,
    tuple[tuple[str, str, int | None, int | None, tuple[str, ...]], ...],
] = MappingProxyType(
    {
        message_class: tuple(
            (
                rule.name,
                rule.scalar_type,
                rule.minimum,
                rule.maximum,
                rule.allowed_tokens,
            )
            for rule in rules
        )
        for message_class, rules in _MESSAGE_PARAM_RULES.items()
    }
)


def _validate_scalar_params(value: object) -> ScalarParams:
    if not isinstance(value, tuple):
        raise SpeechPolicyError("INVALID_SCALAR_PARAMS", "scalar_params must be a tuple")
    validated: list[tuple[str, Scalar]] = []
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise SpeechPolicyError("INVALID_SCALAR_PARAMS")
        name, scalar = item
        if not isinstance(name, str) or _PARAMETER_NAME.fullmatch(name) is None:
            raise SpeechPolicyError("INVALID_SCALAR_PARAM_NAME")
        if isinstance(scalar, bool):
            pass
        elif isinstance(scalar, int):
            if abs(scalar) > 10**12:
                raise SpeechPolicyError("SCALAR_PARAM_OUT_OF_RANGE")
        elif isinstance(scalar, float):
            if not math.isfinite(scalar) or abs(scalar) > 10**12:
                raise SpeechPolicyError("SCALAR_PARAM_OUT_OF_RANGE")
            scalar = 0.0 if scalar == 0.0 else scalar
        elif isinstance(scalar, str):
            # Strings are closed enum-like tokens, never prose to be spoken.
            if _ENUM_VALUE.fullmatch(scalar) is None:
                raise SpeechPolicyError("FREE_TEXT_PARAM_FORBIDDEN")
        else:
            raise SpeechPolicyError("NONSCALAR_PARAM_FORBIDDEN")
        validated.append((name, scalar))
    names = tuple(item[0] for item in validated)
    if names != tuple(sorted(names)) or len(names) != len(set(names)):
        raise SpeechPolicyError("UNSORTED_OR_DUPLICATE_SCALAR_PARAMS")
    return tuple(validated)


def _validate_message_params(
    message_class: MessageClass, value: object
) -> ScalarParams:
    params = _validate_scalar_params(value)
    rules = _MESSAGE_PARAM_RULES[message_class]
    if tuple(name for name, _ in params) != tuple(rule.name for rule in rules):
        raise SpeechPolicyError("PARAM_SCHEMA_MISMATCH", message_class.value)
    for (_, scalar), rule in zip(params, rules, strict=True):
        if rule.scalar_type == "int":
            if type(scalar) is not int:
                raise SpeechPolicyError("PARAM_TYPE_MISMATCH", rule.name)
            assert rule.minimum is not None and rule.maximum is not None
            if not rule.minimum <= scalar <= rule.maximum:
                raise SpeechPolicyError("PARAM_OUT_OF_RANGE", rule.name)
        elif rule.scalar_type == "token":
            if not isinstance(scalar, str):
                raise SpeechPolicyError("PARAM_TYPE_MISMATCH", rule.name)
            if scalar not in rule.allowed_tokens:
                raise SpeechPolicyError("PARAM_TOKEN_NOT_ALLOWLISTED", rule.name)
        else:  # pragma: no cover - closed internal schema construction
            raise AssertionError(f"unsupported scalar rule: {rule.scalar_type}")
    return params


@dataclass(frozen=True, slots=True)
class PolicyBinding:
    source_id: str
    session_id: str
    source_epoch: int
    session_epoch: int

    def __post_init__(self) -> None:
        _require_identifier("source_id", self.source_id)
        _require_identifier("session_id", self.session_id)
        _require_non_negative_int("source_epoch", self.source_epoch)
        _require_non_negative_int("session_epoch", self.session_epoch)

    def to_dict(self) -> dict[str, object]:
        return {
            "session_epoch": self.session_epoch,
            "session_id": self.session_id,
            "source_epoch": self.source_epoch,
            "source_id": self.source_id,
        }


@dataclass(frozen=True, slots=True)
class SpeechPolicyConfig:
    """Explicit deterministic thresholds; shadow output is muted by default."""

    muted: bool = True
    stable_consecutive_samples: int = 3
    stable_duration_us: int = 500_000
    max_timing_gap_us: int = 1_000_000
    global_cooldown_us: int = 15_000_000
    per_conflict_cooldown_us: int = 30_000_000

    def __post_init__(self) -> None:
        if not isinstance(self.muted, bool):
            raise SpeechPolicyError("INVALID_MUTED")
        if (
            isinstance(self.stable_consecutive_samples, bool)
            or not isinstance(self.stable_consecutive_samples, int)
            or self.stable_consecutive_samples < 1
        ):
            raise SpeechPolicyError("INVALID_STABLE_SAMPLE_COUNT")
        _require_non_negative_int("stable_duration_us", self.stable_duration_us)
        _require_non_negative_int("max_timing_gap_us", self.max_timing_gap_us)
        _require_non_negative_int("global_cooldown_us", self.global_cooldown_us)
        _require_non_negative_int(
            "per_conflict_cooldown_us", self.per_conflict_cooldown_us
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "global_cooldown_us": self.global_cooldown_us,
            "max_timing_gap_us": self.max_timing_gap_us,
            "muted": self.muted,
            "per_conflict_cooldown_us": self.per_conflict_cooldown_us,
            "stable_consecutive_samples": self.stable_consecutive_samples,
            "stable_duration_us": self.stable_duration_us,
        }


@dataclass(frozen=True, slots=True)
class SpeechEnvelope:
    """One structured candidate; it contains no rendered or renderable prose.

    ``supersedes_content_revision_sha256`` is an optimistic-concurrency edge:
    it must be ``None`` for an initial issue and for an unchanged revision, and
    must equal the currently active revision for changed content.
    """

    binding: PolicyBinding
    message_class: MessageClass
    template_id: str
    scalar_params: ScalarParams
    conflict_key: str
    evidence_sha256: str
    issued_session_time_us: int
    valid_until_session_time_us: int
    supersedes_content_revision_sha256: str | None = None
    executable: bool = False
    contract_version: str = SPEECH_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.binding, PolicyBinding):
            raise SpeechPolicyError("INVALID_BINDING")
        if not isinstance(self.message_class, MessageClass):
            raise SpeechPolicyError("MESSAGE_CLASS_NOT_ALLOWLISTED")
        if self.template_id != MESSAGE_TEMPLATE_ID[self.message_class]:
            raise SpeechPolicyError("TEMPLATE_NOT_ALLOWLISTED")
        if self.message_class is MessageClass.INTEGRITY_ALERT and self.scalar_params:
            raise SpeechPolicyError("INTEGRITY_ALERT_MUST_BE_NONTACTICAL")
        _validate_message_params(self.message_class, self.scalar_params)
        _require_identifier("conflict_key", self.conflict_key)
        _require_sha256("evidence_sha256", self.evidence_sha256)
        issued = _require_non_negative_int(
            "issued_session_time_us", self.issued_session_time_us
        )
        deadline = _require_non_negative_int(
            "valid_until_session_time_us", self.valid_until_session_time_us
        )
        if deadline <= issued:
            raise SpeechPolicyError("INVALID_DEADLINE", "deadline is exclusive")
        if self.supersedes_content_revision_sha256 is not None:
            _require_sha256(
                "supersedes_content_revision_sha256",
                self.supersedes_content_revision_sha256,
            )
        if not isinstance(self.executable, bool) or self.executable:
            raise SpeechPolicyError("EXECUTABLE_RECOMMENDATION_FORBIDDEN")
        if self.contract_version != SPEECH_POLICY_CONTRACT_VERSION:
            raise SpeechPolicyError("CONTRACT_VERSION_MISMATCH")

    @property
    def priority(self) -> Priority:
        return MESSAGE_CLASS_PRIORITY[self.message_class]

    @property
    def content_revision_sha256(self) -> str:
        return _sha256(
            {
                "contract_version": self.contract_version,
                "message_class": self.message_class.value,
                "scalar_params": dict(self.scalar_params),
                "template_id": self.template_id,
            }
        )

    @property
    def envelope_sha256(self) -> str:
        return _sha256(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "binding": self.binding.to_dict(),
            "conflict_key": self.conflict_key,
            "content_revision_sha256": self.content_revision_sha256,
            "contract_version": self.contract_version,
            "evidence_sha256": self.evidence_sha256,
            "executable": False,
            "issued_session_time_us": self.issued_session_time_us,
            "message_class": self.message_class.value,
            "priority": self.priority.value,
            "scalar_params": dict(self.scalar_params),
            "supersedes_content_revision_sha256": (
                self.supersedes_content_revision_sha256
            ),
            "template_id": self.template_id,
            "valid_until_session_time_us": self.valid_until_session_time_us,
        }


@dataclass(frozen=True, slots=True)
class TimingEvidence:
    binding: PolicyBinding
    session_time_us: int
    straight: TriState
    brake_clear: TriState
    steering_centered: TriState
    side_by_side_clear: TriState
    quality_stable: TriState
    evidence_sha256: str
    contract_version: str = SPEECH_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.binding, PolicyBinding):
            raise SpeechPolicyError("INVALID_BINDING")
        _require_non_negative_int("session_time_us", self.session_time_us)
        for name in (
            "straight",
            "brake_clear",
            "steering_centered",
            "side_by_side_clear",
            "quality_stable",
        ):
            if not isinstance(getattr(self, name), TriState):
                raise SpeechPolicyError("INVALID_TRISTATE", name)
        _require_sha256("evidence_sha256", self.evidence_sha256)
        if self.contract_version != SPEECH_POLICY_CONTRACT_VERSION:
            raise SpeechPolicyError("CONTRACT_VERSION_MISMATCH")

    @property
    def all_safe(self) -> bool:
        return all(
            value is TriState.TRUE
            for value in (
                self.straight,
                self.brake_clear,
                self.steering_centered,
                self.side_by_side_clear,
                self.quality_stable,
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "binding": self.binding.to_dict(),
            "brake_clear": self.brake_clear.value,
            "contract_version": self.contract_version,
            "evidence_sha256": self.evidence_sha256,
            "quality_stable": self.quality_stable.value,
            "session_time_us": self.session_time_us,
            "side_by_side_clear": self.side_by_side_clear.value,
            "steering_centered": self.steering_centered.value,
            "straight": self.straight.value,
        }


@dataclass(frozen=True, slots=True)
class SpeechRevocation:
    binding: PolicyBinding
    conflict_key: str
    expected_content_revision_sha256: str
    session_time_us: int
    evidence_sha256: str
    contract_version: str = SPEECH_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.binding, PolicyBinding):
            raise SpeechPolicyError("INVALID_BINDING")
        _require_identifier("conflict_key", self.conflict_key)
        _require_sha256(
            "expected_content_revision_sha256",
            self.expected_content_revision_sha256,
        )
        _require_non_negative_int("session_time_us", self.session_time_us)
        _require_sha256("evidence_sha256", self.evidence_sha256)
        if self.contract_version != SPEECH_POLICY_CONTRACT_VERSION:
            raise SpeechPolicyError("CONTRACT_VERSION_MISMATCH")

    def to_dict(self) -> dict[str, object]:
        return {
            "binding": self.binding.to_dict(),
            "conflict_key": self.conflict_key,
            "contract_version": self.contract_version,
            "evidence_sha256": self.evidence_sha256,
            "expected_content_revision_sha256": self.expected_content_revision_sha256,
            "session_time_us": self.session_time_us,
        }


@dataclass(frozen=True, slots=True)
class SpeechRefresh:
    """Receipt-bound heartbeat for one exact active envelope.

    A refresh has no message content fields.  It can therefore update only the
    evidence and exclusive deadline of the active envelope selected by both
    its content revision and its full-envelope hash.  The two hashes form a
    compare-and-swap edge: a delayed or replayed refresh cannot refresh a newer
    envelope accidentally.
    """

    binding: PolicyBinding
    conflict_key: str
    expected_content_revision_sha256: str
    previous_envelope_sha256: str
    evidence_sha256: str
    session_time_us: int
    valid_until_session_time_us: int
    executable: bool = False
    contract_version: str = SPEECH_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.binding, PolicyBinding):
            raise SpeechPolicyError("INVALID_BINDING")
        _require_identifier("conflict_key", self.conflict_key)
        _require_sha256(
            "expected_content_revision_sha256",
            self.expected_content_revision_sha256,
        )
        _require_sha256("previous_envelope_sha256", self.previous_envelope_sha256)
        _require_sha256("evidence_sha256", self.evidence_sha256)
        now = _require_non_negative_int("session_time_us", self.session_time_us)
        deadline = _require_non_negative_int(
            "valid_until_session_time_us", self.valid_until_session_time_us
        )
        if deadline <= now:
            raise SpeechPolicyError("INVALID_REFRESH_DEADLINE", "deadline is exclusive")
        if not isinstance(self.executable, bool) or self.executable:
            raise SpeechPolicyError("EXECUTABLE_RECOMMENDATION_FORBIDDEN")
        if self.contract_version != SPEECH_POLICY_CONTRACT_VERSION:
            raise SpeechPolicyError("CONTRACT_VERSION_MISMATCH")

    def to_dict(self) -> dict[str, object]:
        return {
            "binding": self.binding.to_dict(),
            "conflict_key": self.conflict_key,
            "contract_version": self.contract_version,
            "evidence_sha256": self.evidence_sha256,
            "executable": False,
            "expected_content_revision_sha256": (
                self.expected_content_revision_sha256
            ),
            "previous_envelope_sha256": self.previous_envelope_sha256,
            "session_time_us": self.session_time_us,
            "valid_until_session_time_us": self.valid_until_session_time_us,
        }


@dataclass(frozen=True, slots=True)
class BoundarySignal:
    binding: PolicyBinding
    kind: BoundaryKind
    session_time_us: int
    evidence_sha256: str
    contract_version: str = SPEECH_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.binding, PolicyBinding):
            raise SpeechPolicyError("INVALID_BINDING")
        if not isinstance(self.kind, BoundaryKind):
            raise SpeechPolicyError("INVALID_BOUNDARY_KIND")
        _require_non_negative_int("session_time_us", self.session_time_us)
        _require_sha256("evidence_sha256", self.evidence_sha256)
        if self.contract_version != SPEECH_POLICY_CONTRACT_VERSION:
            raise SpeechPolicyError("CONTRACT_VERSION_MISMATCH")

    def to_dict(self) -> dict[str, object]:
        return {
            "binding": self.binding.to_dict(),
            "contract_version": self.contract_version,
            "evidence_sha256": self.evidence_sha256,
            "kind": self.kind.value,
            "session_time_us": self.session_time_us,
        }


@dataclass(frozen=True, slots=True)
class MuteSignal:
    """Receipt-bound one-click mute state transition."""

    binding: PolicyBinding
    kind: MuteKind
    session_time_us: int
    evidence_sha256: str
    contract_version: str = SPEECH_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.binding, PolicyBinding):
            raise SpeechPolicyError("INVALID_BINDING")
        if not isinstance(self.kind, MuteKind):
            raise SpeechPolicyError("INVALID_MUTE_KIND")
        _require_non_negative_int("session_time_us", self.session_time_us)
        _require_sha256("evidence_sha256", self.evidence_sha256)
        if self.contract_version != SPEECH_POLICY_CONTRACT_VERSION:
            raise SpeechPolicyError("CONTRACT_VERSION_MISMATCH")

    def to_dict(self) -> dict[str, object]:
        return {
            "binding": self.binding.to_dict(),
            "contract_version": self.contract_version,
            "evidence_sha256": self.evidence_sha256,
            "kind": self.kind.value,
            "session_time_us": self.session_time_us,
        }


type PolicyInput = (
    SpeechEnvelope
    | SpeechRefresh
    | TimingEvidence
    | SpeechRevocation
    | BoundarySignal
    | MuteSignal
)


_POLICY_INPUT_KIND_NAMES = frozenset(
    {
        "BoundarySignal",
        "MuteSignal",
        "SpeechEnvelope",
        "SpeechRefresh",
        "SpeechRevocation",
        "TimingEvidence",
    }
)


@dataclass(frozen=True, slots=True)
class CanonicalPolicyInputRecord:
    """Immutable, persistence-safe record of one canonically ordered input."""

    sequence: int
    input_kind: str
    canonical_payload_json: str
    contract_version: str = SPEECH_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _require_non_negative_int("sequence", self.sequence)
        if self.input_kind not in _POLICY_INPUT_KIND_NAMES:
            raise SpeechPolicyError("INVALID_CANONICAL_INPUT_KIND")
        if not isinstance(self.canonical_payload_json, str):
            raise SpeechPolicyError("INVALID_CANONICAL_INPUT_PAYLOAD")
        try:
            payload = json.loads(self.canonical_payload_json)
        except (TypeError, ValueError) as error:
            raise SpeechPolicyError("INVALID_CANONICAL_INPUT_PAYLOAD") from error
        if not isinstance(payload, dict):
            raise SpeechPolicyError("INVALID_CANONICAL_INPUT_PAYLOAD")
        if _canonical_json(payload).decode("utf-8") != self.canonical_payload_json:
            raise SpeechPolicyError("NONCANONICAL_INPUT_PAYLOAD")
        if payload.get("contract_version") != self.contract_version:
            raise SpeechPolicyError("CONTRACT_VERSION_MISMATCH")
        if self.contract_version != SPEECH_POLICY_CONTRACT_VERSION:
            raise SpeechPolicyError("CONTRACT_VERSION_MISMATCH")

    @classmethod
    def from_input(
        cls, sequence: int, item: PolicyInput
    ) -> CanonicalPolicyInputRecord:
        return cls(
            sequence=sequence,
            input_kind=type(item).__name__,
            canonical_payload_json=_canonical_json(item.to_dict()).decode("utf-8"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CanonicalPolicyInputRecord:
        expected = {
            "contract_version",
            "input_kind",
            "payload",
            "record_sha256",
            "sequence",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise SpeechPolicyError("INVALID_CANONICAL_INPUT_RECORD")
        payload = value["payload"]
        if not isinstance(payload, Mapping):
            raise SpeechPolicyError("INVALID_CANONICAL_INPUT_PAYLOAD")
        record = cls(
            sequence=value["sequence"],  # type: ignore[arg-type]
            input_kind=value["input_kind"],  # type: ignore[arg-type]
            canonical_payload_json=_canonical_json(payload).decode("utf-8"),
            contract_version=value["contract_version"],  # type: ignore[arg-type]
        )
        if value["record_sha256"] != record.record_sha256:
            raise SpeechPolicyError("CANONICAL_INPUT_RECORD_HASH_MISMATCH")
        return record

    @property
    def payload(self) -> dict[str, object]:
        payload = json.loads(self.canonical_payload_json)
        assert isinstance(payload, dict)
        return payload

    @property
    def record_sha256(self) -> str:
        return _sha256(
            {
                "contract_version": self.contract_version,
                "input_kind": self.input_kind,
                "payload": self.payload,
                "sequence": self.sequence,
            }
        )

    def to_policy_input(self) -> PolicyInput:
        return _policy_input_from_payload(self.input_kind, self.payload)

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "input_kind": self.input_kind,
            "payload": self.payload,
            "record_sha256": self.record_sha256,
            "sequence": self.sequence,
        }


@dataclass(frozen=True, slots=True)
class ActiveEnvelopeSnapshot:
    """Immutable final active-envelope artifact with independently verifiable hash."""

    conflict_key: str
    content_revision_sha256: str
    envelope_sha256: str
    canonical_envelope_json: str
    contract_version: str = SPEECH_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _require_identifier("conflict_key", self.conflict_key)
        _require_sha256("content_revision_sha256", self.content_revision_sha256)
        _require_sha256("envelope_sha256", self.envelope_sha256)
        if not isinstance(self.canonical_envelope_json, str):
            raise SpeechPolicyError("INVALID_ACTIVE_ENVELOPE_SNAPSHOT")
        try:
            payload = json.loads(self.canonical_envelope_json)
        except (TypeError, ValueError) as error:
            raise SpeechPolicyError("INVALID_ACTIVE_ENVELOPE_SNAPSHOT") from error
        if not isinstance(payload, dict):
            raise SpeechPolicyError("INVALID_ACTIVE_ENVELOPE_SNAPSHOT")
        if _canonical_json(payload).decode("utf-8") != self.canonical_envelope_json:
            raise SpeechPolicyError("NONCANONICAL_ACTIVE_ENVELOPE_SNAPSHOT")
        envelope = _speech_envelope_from_payload(payload)
        if (
            envelope.conflict_key != self.conflict_key
            or envelope.content_revision_sha256 != self.content_revision_sha256
            or envelope.envelope_sha256 != self.envelope_sha256
        ):
            raise SpeechPolicyError("ACTIVE_ENVELOPE_SNAPSHOT_HASH_MISMATCH")
        if self.contract_version != SPEECH_POLICY_CONTRACT_VERSION:
            raise SpeechPolicyError("CONTRACT_VERSION_MISMATCH")

    @classmethod
    def from_envelope(cls, envelope: SpeechEnvelope) -> ActiveEnvelopeSnapshot:
        return cls(
            conflict_key=envelope.conflict_key,
            content_revision_sha256=envelope.content_revision_sha256,
            envelope_sha256=envelope.envelope_sha256,
            canonical_envelope_json=_canonical_json(envelope.to_dict()).decode("utf-8"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ActiveEnvelopeSnapshot:
        expected = {
            "contract_version",
            "conflict_key",
            "content_revision_sha256",
            "envelope",
            "envelope_sha256",
            "snapshot_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise SpeechPolicyError("INVALID_ACTIVE_ENVELOPE_SNAPSHOT")
        envelope = value["envelope"]
        if not isinstance(envelope, Mapping):
            raise SpeechPolicyError("INVALID_ACTIVE_ENVELOPE_SNAPSHOT")
        snapshot = cls(
            conflict_key=value["conflict_key"],  # type: ignore[arg-type]
            content_revision_sha256=value[  # type: ignore[arg-type]
                "content_revision_sha256"
            ],
            envelope_sha256=value["envelope_sha256"],  # type: ignore[arg-type]
            canonical_envelope_json=_canonical_json(envelope).decode("utf-8"),
            contract_version=value["contract_version"],  # type: ignore[arg-type]
        )
        if value["snapshot_sha256"] != snapshot.snapshot_sha256:
            raise SpeechPolicyError("ACTIVE_ENVELOPE_SNAPSHOT_HASH_MISMATCH")
        return snapshot

    @property
    def envelope(self) -> SpeechEnvelope:
        return _speech_envelope_from_payload(self.envelope_payload)

    @property
    def envelope_payload(self) -> dict[str, object]:
        payload = json.loads(self.canonical_envelope_json)
        assert isinstance(payload, dict)
        return payload

    @property
    def snapshot_sha256(self) -> str:
        return _sha256(self._base_dict())

    def _base_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "conflict_key": self.conflict_key,
            "content_revision_sha256": self.content_revision_sha256,
            "envelope": self.envelope_payload,
            "envelope_sha256": self.envelope_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        payload = self._base_dict()
        payload["snapshot_sha256"] = self.snapshot_sha256
        return payload


def _expect_payload_keys(
    payload: Mapping[str, object], expected: frozenset[str]
) -> None:
    if set(payload) != expected:
        raise SpeechPolicyError("INVALID_CANONICAL_INPUT_PAYLOAD")


def _binding_from_payload(value: object) -> PolicyBinding:
    if not isinstance(value, Mapping):
        raise SpeechPolicyError("INVALID_CANONICAL_INPUT_PAYLOAD")
    _expect_payload_keys(
        value,
        frozenset({"session_epoch", "session_id", "source_epoch", "source_id"}),
    )
    return PolicyBinding(
        source_id=value["source_id"],  # type: ignore[arg-type]
        session_id=value["session_id"],  # type: ignore[arg-type]
        source_epoch=value["source_epoch"],  # type: ignore[arg-type]
        session_epoch=value["session_epoch"],  # type: ignore[arg-type]
    )


def _speech_envelope_from_payload(
    payload: Mapping[str, object],
) -> SpeechEnvelope:
    _expect_payload_keys(
        payload,
        frozenset(
            {
                "binding",
                "conflict_key",
                "content_revision_sha256",
                "contract_version",
                "evidence_sha256",
                "executable",
                "issued_session_time_us",
                "message_class",
                "priority",
                "scalar_params",
                "supersedes_content_revision_sha256",
                "template_id",
                "valid_until_session_time_us",
            }
        ),
    )
    params = payload["scalar_params"]
    if not isinstance(params, Mapping) or not all(
        isinstance(name, str) for name in params
    ):
        raise SpeechPolicyError("INVALID_CANONICAL_INPUT_PAYLOAD")
    try:
        envelope = SpeechEnvelope(
            binding=_binding_from_payload(payload["binding"]),
            message_class=MessageClass(payload["message_class"]),  # type: ignore[arg-type]
            template_id=payload["template_id"],  # type: ignore[arg-type]
            scalar_params=tuple(sorted(params.items())),  # type: ignore[arg-type]
            conflict_key=payload["conflict_key"],  # type: ignore[arg-type]
            evidence_sha256=payload["evidence_sha256"],  # type: ignore[arg-type]
            issued_session_time_us=payload["issued_session_time_us"],  # type: ignore[arg-type]
            valid_until_session_time_us=payload[  # type: ignore[arg-type]
                "valid_until_session_time_us"
            ],
            supersedes_content_revision_sha256=payload[  # type: ignore[arg-type]
                "supersedes_content_revision_sha256"
            ],
            executable=payload["executable"],  # type: ignore[arg-type]
            contract_version=payload["contract_version"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as error:
        if isinstance(error, SpeechPolicyError):
            raise
        raise SpeechPolicyError("INVALID_CANONICAL_INPUT_PAYLOAD") from error
    if envelope.to_dict() != dict(payload):
        raise SpeechPolicyError("CANONICAL_INPUT_PAYLOAD_MISMATCH")
    return envelope


def _policy_input_from_payload(
    input_kind: str, payload: Mapping[str, object]
) -> PolicyInput:
    try:
        if input_kind == "SpeechEnvelope":
            return _speech_envelope_from_payload(payload)
        if input_kind == "SpeechRefresh":
            _expect_payload_keys(
                payload,
                frozenset(
                    {
                        "binding",
                        "conflict_key",
                        "contract_version",
                        "evidence_sha256",
                        "executable",
                        "expected_content_revision_sha256",
                        "previous_envelope_sha256",
                        "session_time_us",
                        "valid_until_session_time_us",
                    }
                ),
            )
            item: PolicyInput = SpeechRefresh(
                binding=_binding_from_payload(payload["binding"]),
                conflict_key=payload["conflict_key"],  # type: ignore[arg-type]
                expected_content_revision_sha256=payload[  # type: ignore[arg-type]
                    "expected_content_revision_sha256"
                ],
                previous_envelope_sha256=payload[  # type: ignore[arg-type]
                    "previous_envelope_sha256"
                ],
                evidence_sha256=payload["evidence_sha256"],  # type: ignore[arg-type]
                session_time_us=payload["session_time_us"],  # type: ignore[arg-type]
                valid_until_session_time_us=payload[  # type: ignore[arg-type]
                    "valid_until_session_time_us"
                ],
                executable=payload["executable"],  # type: ignore[arg-type]
                contract_version=payload["contract_version"],  # type: ignore[arg-type]
            )
        elif input_kind == "TimingEvidence":
            _expect_payload_keys(
                payload,
                frozenset(
                    {
                        "binding",
                        "brake_clear",
                        "contract_version",
                        "evidence_sha256",
                        "quality_stable",
                        "session_time_us",
                        "side_by_side_clear",
                        "steering_centered",
                        "straight",
                    }
                ),
            )
            item = TimingEvidence(
                binding=_binding_from_payload(payload["binding"]),
                session_time_us=payload["session_time_us"],  # type: ignore[arg-type]
                straight=TriState(payload["straight"]),  # type: ignore[arg-type]
                brake_clear=TriState(payload["brake_clear"]),  # type: ignore[arg-type]
                steering_centered=TriState(  # type: ignore[arg-type]
                    payload["steering_centered"]
                ),
                side_by_side_clear=TriState(  # type: ignore[arg-type]
                    payload["side_by_side_clear"]
                ),
                quality_stable=TriState(payload["quality_stable"]),  # type: ignore[arg-type]
                evidence_sha256=payload["evidence_sha256"],  # type: ignore[arg-type]
                contract_version=payload["contract_version"],  # type: ignore[arg-type]
            )
        elif input_kind == "SpeechRevocation":
            _expect_payload_keys(
                payload,
                frozenset(
                    {
                        "binding",
                        "conflict_key",
                        "contract_version",
                        "evidence_sha256",
                        "expected_content_revision_sha256",
                        "session_time_us",
                    }
                ),
            )
            item = SpeechRevocation(
                binding=_binding_from_payload(payload["binding"]),
                conflict_key=payload["conflict_key"],  # type: ignore[arg-type]
                expected_content_revision_sha256=payload[  # type: ignore[arg-type]
                    "expected_content_revision_sha256"
                ],
                session_time_us=payload["session_time_us"],  # type: ignore[arg-type]
                evidence_sha256=payload["evidence_sha256"],  # type: ignore[arg-type]
                contract_version=payload["contract_version"],  # type: ignore[arg-type]
            )
        elif input_kind == "BoundarySignal":
            _expect_payload_keys(
                payload,
                frozenset(
                    {
                        "binding",
                        "contract_version",
                        "evidence_sha256",
                        "kind",
                        "session_time_us",
                    }
                ),
            )
            item = BoundarySignal(
                binding=_binding_from_payload(payload["binding"]),
                kind=BoundaryKind(payload["kind"]),  # type: ignore[arg-type]
                session_time_us=payload["session_time_us"],  # type: ignore[arg-type]
                evidence_sha256=payload["evidence_sha256"],  # type: ignore[arg-type]
                contract_version=payload["contract_version"],  # type: ignore[arg-type]
            )
        elif input_kind == "MuteSignal":
            _expect_payload_keys(
                payload,
                frozenset(
                    {
                        "binding",
                        "contract_version",
                        "evidence_sha256",
                        "kind",
                        "session_time_us",
                    }
                ),
            )
            item = MuteSignal(
                binding=_binding_from_payload(payload["binding"]),
                kind=MuteKind(payload["kind"]),  # type: ignore[arg-type]
                session_time_us=payload["session_time_us"],  # type: ignore[arg-type]
                evidence_sha256=payload["evidence_sha256"],  # type: ignore[arg-type]
                contract_version=payload["contract_version"],  # type: ignore[arg-type]
            )
        else:
            raise SpeechPolicyError("INVALID_CANONICAL_INPUT_KIND")
    except (TypeError, ValueError) as error:
        if isinstance(error, SpeechPolicyError):
            raise
        raise SpeechPolicyError("INVALID_CANONICAL_INPUT_PAYLOAD") from error
    if item.to_dict() != dict(payload):
        raise SpeechPolicyError("CANONICAL_INPUT_PAYLOAD_MISMATCH")
    return item


@dataclass(frozen=True, slots=True)
class SpeechLifecycleEvent:
    sequence: int
    kind: LifecycleKind
    binding: PolicyBinding
    session_time_us: int
    conflict_key: str
    previous_revision_sha256: str | None
    current_revision_sha256: str | None
    reason_codes: tuple[str, ...]
    audible: bool = False
    executable: bool = False
    mode: str = "SHADOW_ONLY"
    contract_version: str = SPEECH_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _require_non_negative_int("sequence", self.sequence)
        _require_non_negative_int("session_time_us", self.session_time_us)
        if not isinstance(self.kind, LifecycleKind):
            raise SpeechPolicyError("INVALID_LIFECYCLE_KIND")
        if not isinstance(self.binding, PolicyBinding):
            raise SpeechPolicyError("INVALID_BINDING")
        _require_identifier("conflict_key", self.conflict_key)
        for name, value in (
            ("previous_revision_sha256", self.previous_revision_sha256),
            ("current_revision_sha256", self.current_revision_sha256),
        ):
            if value is not None:
                _require_sha256(name, value)
        if not isinstance(self.reason_codes, tuple) or not all(
            isinstance(item, str) and item for item in self.reason_codes
        ):
            raise SpeechPolicyError("INVALID_REASON_CODES")
        if self.audible is not False or self.executable is not False:
            raise SpeechPolicyError("NON_SHADOW_OUTPUT_FORBIDDEN")
        if self.mode != "SHADOW_ONLY":
            raise SpeechPolicyError("NON_SHADOW_MODE_FORBIDDEN")
        if self.contract_version != SPEECH_POLICY_CONTRACT_VERSION:
            raise SpeechPolicyError("CONTRACT_VERSION_MISMATCH")

    def to_dict(self) -> dict[str, object]:
        return {
            "audible": False,
            "binding": self.binding.to_dict(),
            "conflict_key": self.conflict_key,
            "contract_version": self.contract_version,
            "current_revision_sha256": self.current_revision_sha256,
            "executable": False,
            "kind": self.kind.value,
            "mode": self.mode,
            "previous_revision_sha256": self.previous_revision_sha256,
            "reason_codes": list(self.reason_codes),
            "sequence": self.sequence,
            "session_time_us": self.session_time_us,
        }


@dataclass(frozen=True, slots=True)
class SpeechDecision:
    sequence: int
    kind: DecisionKind
    binding: PolicyBinding
    session_time_us: int
    conflict_key: str
    content_revision_sha256: str
    message_class: MessageClass
    priority: Priority
    template_id: str
    scalar_params: ScalarParams
    message_evidence_sha256: str
    timing_evidence: TimingEvidence | None
    quality_stable_since_session_time_us: int | None
    safe_since_session_time_us: int | None
    quality_consecutive_samples: int
    safe_consecutive_samples: int
    reason_codes: tuple[str, ...]
    audible: bool = False
    executable: bool = False
    mode: str = "SHADOW_ONLY"
    contract_version: str = SPEECH_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _require_non_negative_int("sequence", self.sequence)
        _require_non_negative_int("session_time_us", self.session_time_us)
        if not isinstance(self.kind, DecisionKind):
            raise SpeechPolicyError("INVALID_DECISION_KIND")
        if not isinstance(self.binding, PolicyBinding):
            raise SpeechPolicyError("INVALID_BINDING")
        _require_identifier("conflict_key", self.conflict_key)
        _require_sha256("content_revision_sha256", self.content_revision_sha256)
        if not isinstance(self.message_class, MessageClass):
            raise SpeechPolicyError("MESSAGE_CLASS_NOT_ALLOWLISTED")
        if self.priority is not MESSAGE_CLASS_PRIORITY[self.message_class]:
            raise SpeechPolicyError("PRIORITY_CLASS_MISMATCH")
        if self.template_id != MESSAGE_TEMPLATE_ID[self.message_class]:
            raise SpeechPolicyError("TEMPLATE_NOT_ALLOWLISTED")
        _validate_message_params(self.message_class, self.scalar_params)
        _require_sha256("message_evidence_sha256", self.message_evidence_sha256)
        if self.timing_evidence is not None:
            if not isinstance(self.timing_evidence, TimingEvidence):
                raise SpeechPolicyError("INVALID_TIMING_EVIDENCE")
            if self.timing_evidence.binding != self.binding:
                raise SpeechPolicyError("TIMING_IDENTITY_MISMATCH")
            if self.timing_evidence.session_time_us != self.session_time_us:
                raise SpeechPolicyError("TIMING_TIME_MISMATCH")
        for name, value in (
            (
                "quality_stable_since_session_time_us",
                self.quality_stable_since_session_time_us,
            ),
            ("safe_since_session_time_us", self.safe_since_session_time_us),
        ):
            if value is not None:
                _require_non_negative_int(name, value)
                if value > self.session_time_us:
                    raise SpeechPolicyError("STABILITY_TIME_AFTER_DECISION", name)
        _require_non_negative_int(
            "quality_consecutive_samples", self.quality_consecutive_samples
        )
        _require_non_negative_int("safe_consecutive_samples", self.safe_consecutive_samples)
        if self.kind is DecisionKind.SHADOW_WOULD_SPEAK:
            if (
                self.timing_evidence is None
                or self.timing_evidence.quality_stable is not TriState.TRUE
                or self.quality_stable_since_session_time_us is None
            ):
                raise SpeechPolicyError("SPEECH_INTENT_WITHOUT_STABLE_QUALITY")
            if self.priority is not Priority.P0 and (
                not self.timing_evidence.all_safe
                or self.safe_since_session_time_us is None
            ):
                raise SpeechPolicyError("ORDINARY_INTENT_WITHOUT_SAFE_WINDOW")
        if not isinstance(self.reason_codes, tuple) or not all(
            isinstance(item, str) and item for item in self.reason_codes
        ):
            raise SpeechPolicyError("INVALID_REASON_CODES")
        if self.audible is not False or self.executable is not False:
            raise SpeechPolicyError("NON_SHADOW_OUTPUT_FORBIDDEN")
        if self.mode != "SHADOW_ONLY":
            raise SpeechPolicyError("NON_SHADOW_MODE_FORBIDDEN")
        if self.contract_version != SPEECH_POLICY_CONTRACT_VERSION:
            raise SpeechPolicyError("CONTRACT_VERSION_MISMATCH")

    def to_dict(self) -> dict[str, object]:
        return {
            "audible": False,
            "binding": self.binding.to_dict(),
            "conflict_key": self.conflict_key,
            "content_revision_sha256": self.content_revision_sha256,
            "contract_version": self.contract_version,
            "executable": False,
            "kind": self.kind.value,
            "message_class": self.message_class.value,
            "message_evidence_sha256": self.message_evidence_sha256,
            "mode": self.mode,
            "priority": self.priority.value,
            "quality_consecutive_samples": self.quality_consecutive_samples,
            "quality_stable_since_session_time_us": (
                self.quality_stable_since_session_time_us
            ),
            "reason_codes": list(self.reason_codes),
            "safe_consecutive_samples": self.safe_consecutive_samples,
            "safe_since_session_time_us": self.safe_since_session_time_us,
            "scalar_params": dict(self.scalar_params),
            "sequence": self.sequence,
            "session_time_us": self.session_time_us,
            "template_id": self.template_id,
            "timing_evidence": (
                self.timing_evidence.to_dict()
                if self.timing_evidence is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class SpeechPolicyReceipt:
    binding_sha256: str
    config_sha256: str
    inputs_sha256: str
    final_active_envelopes_sha256: str
    lifecycle_events_sha256: str
    decisions_sha256: str
    transcript_sha256: str
    receipt_sha256: str
    input_count: int
    final_active_envelope_count: int
    lifecycle_event_count: int
    decision_count: int
    input_kind_counts: tuple[tuple[str, int], ...]
    lifecycle_kind_counts: tuple[tuple[str, int], ...]
    decision_kind_counts: tuple[tuple[str, int], ...]
    final_muted: bool
    status: str
    failure_code: str | None
    contract_version: str = SPEECH_POLICY_CONTRACT_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "binding_sha256": self.binding_sha256,
            "config_sha256": self.config_sha256,
            "contract_version": self.contract_version,
            "decision_count": self.decision_count,
            "decision_kind_counts": dict(self.decision_kind_counts),
            "decisions_sha256": self.decisions_sha256,
            "failure_code": self.failure_code,
            "final_active_envelope_count": self.final_active_envelope_count,
            "final_active_envelopes_sha256": self.final_active_envelopes_sha256,
            "final_muted": self.final_muted,
            "input_count": self.input_count,
            "input_kind_counts": dict(self.input_kind_counts),
            "inputs_sha256": self.inputs_sha256,
            "lifecycle_event_count": self.lifecycle_event_count,
            "lifecycle_events_sha256": self.lifecycle_events_sha256,
            "lifecycle_kind_counts": dict(self.lifecycle_kind_counts),
            "receipt_sha256": self.receipt_sha256,
            "status": self.status,
            "transcript_sha256": self.transcript_sha256,
        }


@dataclass(frozen=True, slots=True)
class SpeechPolicyRun:
    """Complete immutable artifact for persistence and independent replay."""

    binding: PolicyBinding
    config: SpeechPolicyConfig
    input_records: tuple[CanonicalPolicyInputRecord, ...]
    events: tuple[SpeechLifecycleEvent, ...]
    decisions: tuple[SpeechDecision, ...]
    final_active_envelopes: tuple[ActiveEnvelopeSnapshot, ...]
    receipt: SpeechPolicyReceipt

    def to_dict(self) -> dict[str, object]:
        return {
            "binding": self.binding.to_dict(),
            "config": self.config.to_dict(),
            "contract_version": SPEECH_POLICY_CONTRACT_VERSION,
            "decisions": [item.to_dict() for item in self.decisions],
            "events": [item.to_dict() for item in self.events],
            "final_active_envelopes": [
                item.to_dict() for item in self.final_active_envelopes
            ],
            "input_records": [item.to_dict() for item in self.input_records],
            "receipt": self.receipt.to_dict(),
        }


@dataclass(slots=True)
class _Pending:
    envelope: SpeechEnvelope
    issue_sequence: int
    last_hold: DecisionKind | None = None


class ShadowSpeechPolicy:
    """Stateful, source-bound shadow evaluator with deterministic receipts.

    Inputs sharing a ``session_time_us`` are staged as one atomic batch.  The
    batch is canonically ordered and evaluated only when time advances or
    :meth:`finish` closes the stream, so arrival order and chunk size cannot
    select a different prompt.
    """

    def __init__(
        self,
        binding: PolicyBinding,
        config: SpeechPolicyConfig | None = None,
    ) -> None:
        if not isinstance(binding, PolicyBinding):
            raise SpeechPolicyError("INVALID_BINDING")
        self._binding = binding
        self._config = config if config is not None else SpeechPolicyConfig()
        if not isinstance(self._config, SpeechPolicyConfig):
            raise SpeechPolicyError("INVALID_CONFIG")
        self._inputs: list[CanonicalPolicyInputRecord] = []
        self._events: list[SpeechLifecycleEvent] = []
        self._decisions: list[SpeechDecision] = []
        self._transcript: list[dict[str, object]] = []
        self._active: dict[str, SpeechEnvelope] = {}
        self._pending: dict[str, _Pending] = {}
        self._muted = self._config.muted
        self._staged_time_us: int | None = None
        self._staged_inputs: list[PolicyInput] = []
        self._last_time_us: int | None = None
        self._last_timing: TimingEvidence | None = None
        self._quality_since_us: int | None = None
        self._quality_count = 0
        self._safe_since_us: int | None = None
        self._safe_count = 0
        self._last_shadow_speech_us: int | None = None
        self._last_conflict_speech_us: dict[str, int] = {}
        self._identity_invalidated = False
        self._failure_code: str | None = None
        self._finished = False
        self._receipt: SpeechPolicyReceipt | None = None
        self._final_active_envelopes: tuple[ActiveEnvelopeSnapshot, ...] | None = None

    @property
    def events(self) -> tuple[SpeechLifecycleEvent, ...]:
        return tuple(self._events)

    @property
    def decisions(self) -> tuple[SpeechDecision, ...]:
        return tuple(self._decisions)

    @property
    def input_records(self) -> tuple[CanonicalPolicyInputRecord, ...]:
        return tuple(self._inputs)

    @property
    def canonical_input_records(self) -> tuple[CanonicalPolicyInputRecord, ...]:
        return self.input_records

    @property
    def final_active_envelopes(self) -> tuple[ActiveEnvelopeSnapshot, ...]:
        if self._final_active_envelopes is None:
            raise RuntimeError("speech policy is not finished")
        return self._final_active_envelopes

    @property
    def final_active_snapshot(self) -> tuple[ActiveEnvelopeSnapshot, ...]:
        return self.final_active_envelopes

    @property
    def binding(self) -> PolicyBinding:
        return self._binding

    @property
    def config(self) -> SpeechPolicyConfig:
        return self._config

    @property
    def muted(self) -> bool:
        return self._muted

    def feed(self, item: PolicyInput | Iterable[PolicyInput]) -> None:
        if isinstance(
            item,
            (
                SpeechEnvelope,
                SpeechRefresh,
                TimingEvidence,
                SpeechRevocation,
                BoundarySignal,
                MuteSignal,
            ),
        ):
            self._stage_one(item)
            return
        self.feed_chunk(item)

    def feed_chunk(self, items: Iterable[PolicyInput]) -> None:
        if self._finished:
            raise RuntimeError("speech policy is already finished")
        for item in items:
            self._stage_one(item)

    def _stage_one(self, item: PolicyInput) -> None:
        if self._finished:
            raise RuntimeError("speech policy is already finished")
        if self._failure_code is not None:
            raise SpeechPolicyError("POLICY_ALREADY_REJECTED", self._failure_code)
        if not isinstance(
            item,
            (
                SpeechEnvelope,
                SpeechRefresh,
                TimingEvidence,
                SpeechRevocation,
                BoundarySignal,
                MuteSignal,
            ),
        ):
            self._fail_closed("UNSUPPORTED_INPUT", self._last_time_us or 0)
        if item.binding != self._binding:
            self._append_input_record(item)
            self._fail_closed("IDENTITY_MISMATCH", self._last_time_us or 0)
        item_time = self._input_time(item)
        if self._last_time_us is not None and item_time < self._last_time_us:
            self._append_input_record(item)
            self._fail_closed("SESSION_TIME_REGRESSION", self._last_time_us)
        if self._staged_time_us is None:
            self._staged_time_us = item_time
        elif item_time > self._staged_time_us:
            self._flush_staged()
            self._staged_time_us = item_time
        elif item_time < self._staged_time_us:  # defensive; covered by last_time
            self._append_input_record(item)
            self._fail_closed("SESSION_TIME_REGRESSION", self._staged_time_us)
        self._staged_inputs.append(item)
        self._last_time_us = item_time

    @staticmethod
    def _input_time(item: PolicyInput) -> int:
        return (
            item.issued_session_time_us
            if isinstance(item, SpeechEnvelope)
            else item.session_time_us
        )

    @staticmethod
    def _input_sort_key(item: PolicyInput) -> tuple[object, ...]:
        if isinstance(item, MuteSignal):
            rank = 0
            discriminator = item.kind.value
        elif isinstance(item, SpeechRevocation):
            rank = 1
            discriminator = item.conflict_key
        elif isinstance(item, SpeechRefresh):
            rank = 2
            discriminator = (
                item.conflict_key,
                item.expected_content_revision_sha256,
                item.previous_envelope_sha256,
            )
        elif isinstance(item, SpeechEnvelope):
            rank = 3
            discriminator = (
                int(item.priority.value[1]),
                item.conflict_key,
                item.content_revision_sha256,
            )
        elif isinstance(item, TimingEvidence):
            rank = 4
            discriminator = item.evidence_sha256
        else:
            rank = 5
            discriminator = item.kind.value
        return (rank, discriminator, _canonical_json(item.to_dict()))

    def _append_input_record(self, item: PolicyInput) -> None:
        self._inputs.append(CanonicalPolicyInputRecord.from_input(len(self._inputs), item))

    def _flush_staged(self) -> None:
        if self._staged_time_us is None:
            return
        now = self._staged_time_us
        staged = sorted(self._staged_inputs, key=self._input_sort_key)
        self._staged_time_us = None
        self._staged_inputs = []
        for item in staged:
            self._append_input_record(item)

        envelope_conflicts = [
            item.conflict_key for item in staged if isinstance(item, SpeechEnvelope)
        ]
        if len(envelope_conflicts) != len(set(envelope_conflicts)):
            self._fail_closed("MULTIPLE_CANDIDATES_SAME_CONFLICT_AND_TIME", now)
        refresh_conflicts = [
            item.conflict_key for item in staged if isinstance(item, SpeechRefresh)
        ]
        if len(refresh_conflicts) != len(set(refresh_conflicts)):
            self._fail_closed("MULTIPLE_REFRESHES_SAME_CONFLICT_AND_TIME", now)
        mute_kinds = {item.kind for item in staged if isinstance(item, MuteSignal)}
        if len(mute_kinds) > 1:
            self._fail_closed("CONFLICTING_MUTE_SIGNALS", now)

        self._expire_due(now)
        for item in staged:
            self._process_one(item)
        self._evaluate_pending(now)

    def _process_one(self, item: PolicyInput) -> None:
        if isinstance(item, TimingEvidence):
            self._feed_timing(item)
        elif isinstance(item, SpeechEnvelope):
            self._feed_envelope(item)
        elif isinstance(item, SpeechRevocation):
            self._feed_revocation(item)
        elif isinstance(item, SpeechRefresh):
            self._feed_refresh(item)
        elif isinstance(item, BoundarySignal):
            self._feed_boundary(item)
        else:
            self._feed_mute(item)

    def _feed_timing(self, evidence: TimingEvidence) -> None:
        if self._identity_invalidated:
            self._clear_tactical(
                evidence.session_time_us,
                DecisionKind.SUPPRESS_BOUNDARY,
                ("IDENTITY_EPOCH_INVALIDATED",),
            )
            return
        if (
            self._last_timing is not None
            and evidence.session_time_us == self._last_timing.session_time_us
        ):
            if evidence != self._last_timing:
                self._fail_closed("CONFLICTING_TIMING_EVIDENCE", evidence.session_time_us)
            return
        if (
            self._last_timing is not None
            and evidence.session_time_us - self._last_timing.session_time_us
            > self._config.max_timing_gap_us
        ):
            self._reset_stability()
        self._last_timing = evidence
        if evidence.quality_stable is TriState.TRUE:
            if self._quality_since_us is None:
                self._quality_since_us = evidence.session_time_us
                self._quality_count = 1
            else:
                self._quality_count += 1
        else:
            self._quality_since_us = None
            self._quality_count = 0
            self._safe_since_us = None
            self._safe_count = 0
            self._clear_tactical(
                evidence.session_time_us,
                DecisionKind.SUPPRESS_BOUNDARY,
                ("QUALITY_NOT_STABLE", evidence.quality_stable.value),
            )
            return
        if evidence.all_safe:
            if self._safe_since_us is None:
                self._safe_since_us = evidence.session_time_us
                self._safe_count = 1
            else:
                self._safe_count += 1
        else:
            self._safe_since_us = None
            self._safe_count = 0

    def _feed_envelope(self, envelope: SpeechEnvelope) -> None:
        now = envelope.issued_session_time_us
        if self._identity_invalidated:
            self._decision(
                DecisionKind.SUPPRESS_BOUNDARY,
                envelope,
                now,
                ("IDENTITY_EPOCH_INVALIDATED",),
            )
            return
        previous = self._active.get(envelope.conflict_key)
        revision = envelope.content_revision_sha256
        expected_previous = envelope.supersedes_content_revision_sha256
        if previous is None and expected_previous is not None:
            self._fail_closed("UNEXPECTED_SUPERSEDES_WITHOUT_ACTIVE", now)
        if previous is not None and previous.content_revision_sha256 == revision:
            if expected_previous is not None:
                self._fail_closed("NO_CHANGE_MUST_NOT_SUPERSEDE", now)
            self._event(
                LifecycleKind.NO_CHANGE,
                now,
                envelope.conflict_key,
                revision,
                revision,
                ("CONTENT_REVISION_UNCHANGED",),
            )
            return
        if previous is not None:
            if expected_previous != previous.content_revision_sha256:
                self._fail_closed("SUPERSEDES_PRECONDITION_FAILED", now)
            self._event(
                LifecycleKind.REVOKE,
                now,
                envelope.conflict_key,
                previous.content_revision_sha256,
                revision,
                ("CONTENT_REVISION_CHANGED",),
            )
            if previous.priority is not Priority.P3:
                self._decision(
                    DecisionKind.DROP_REVOKED,
                    previous,
                    now,
                    ("SUPERSEDED", revision),
                )
            self._pending.pop(envelope.conflict_key, None)
        self._active[envelope.conflict_key] = envelope
        self._event(
            LifecycleKind.ISSUE,
            now,
            envelope.conflict_key,
            previous.content_revision_sha256 if previous is not None else None,
            revision,
            ("CONTENT_REVISION_CHANGED",) if previous is not None else ("INITIAL_ISSUE",),
        )
        if envelope.priority is Priority.P3:
            # P3 is represented by the immutable lifecycle log only.  It can
            # never become a speech decision.
            return
        self._pending[envelope.conflict_key] = _Pending(
            envelope=envelope,
            issue_sequence=len(self._events) - 1,
        )

    def _feed_refresh(self, refresh: SpeechRefresh) -> None:
        now = refresh.session_time_us
        if self._identity_invalidated:
            self._fail_closed("REFRESH_IDENTITY_EPOCH_INVALIDATED", now)
        active = self._active.get(refresh.conflict_key)
        if active is None:
            self._fail_closed("REFRESH_WITHOUT_ACTIVE_ENVELOPE", now)
        assert active is not None
        if (
            active.content_revision_sha256
            != refresh.expected_content_revision_sha256
        ):
            self._fail_closed("REFRESH_CONTENT_REVISION_PRECONDITION_FAILED", now)
        if active.envelope_sha256 != refresh.previous_envelope_sha256:
            self._fail_closed("REFRESH_ENVELOPE_PRECONDITION_FAILED", now)

        refreshed = replace(
            active,
            evidence_sha256=refresh.evidence_sha256,
            valid_until_session_time_us=refresh.valid_until_session_time_us,
        )
        if refreshed.content_revision_sha256 != active.content_revision_sha256:
            self._fail_closed("REFRESH_CHANGED_CONTENT_REVISION", now)
        if refreshed.envelope_sha256 == active.envelope_sha256:
            self._fail_closed("REFRESH_MUST_CHANGE_EVIDENCE_OR_DEADLINE", now)

        self._active[refresh.conflict_key] = refreshed
        pending = self._pending.get(refresh.conflict_key)
        if pending is not None:
            pending.envelope = refreshed
        self._event(
            LifecycleKind.NO_CHANGE,
            now,
            refresh.conflict_key,
            active.content_revision_sha256,
            refreshed.content_revision_sha256,
            ("EVIDENCE_DEADLINE_REFRESHED",),
        )

    def _feed_revocation(self, revocation: SpeechRevocation) -> None:
        active = self._active.get(revocation.conflict_key)
        now = revocation.session_time_us
        expected = revocation.expected_content_revision_sha256
        if active is None:
            self._event(
                LifecycleKind.NO_CHANGE,
                now,
                revocation.conflict_key,
                None,
                None,
                ("ALREADY_INACTIVE",),
            )
            return
        if active.content_revision_sha256 != expected:
            self._event(
                LifecycleKind.NO_CHANGE,
                now,
                revocation.conflict_key,
                active.content_revision_sha256,
                active.content_revision_sha256,
                ("STALE_REVOKE_IGNORED",),
            )
            return
        self._event(
            LifecycleKind.REVOKE,
            now,
            revocation.conflict_key,
            active.content_revision_sha256,
            None,
            ("EXPLICIT_REVOKE",),
        )
        if active.priority is not Priority.P3:
            self._decision(
                DecisionKind.DROP_REVOKED,
                active,
                now,
                ("EXPLICIT_REVOKE", revocation.evidence_sha256),
            )
        self._active.pop(revocation.conflict_key, None)
        self._pending.pop(revocation.conflict_key, None)

    def _feed_boundary(self, boundary: BoundarySignal) -> None:
        self._clear_tactical(
            boundary.session_time_us,
            DecisionKind.SUPPRESS_BOUNDARY,
            (boundary.kind.value, boundary.evidence_sha256),
        )
        self._reset_stability()
        if boundary.kind in {BoundaryKind.SOURCE_RESET, BoundaryKind.SESSION_RESET}:
            self._identity_invalidated = True

    def _feed_mute(self, signal: MuteSignal) -> None:
        if signal.kind is MuteKind.MUTE_ON:
            self._muted = True
            self._clear_tactical(
                signal.session_time_us,
                DecisionKind.SUPPRESS_MUTED,
                (MuteKind.MUTE_ON.value, signal.evidence_sha256),
            )
        else:
            # Turning the shadow audit gate back on never revives an old
            # candidate.  A new, lineage-bound ISSUE input is required.
            self._muted = False

    def _evaluate_pending(self, now: int) -> None:
        pending = sorted(
            self._pending.values(),
            key=lambda item: (
                int(item.envelope.priority.value[1]),
                item.issue_sequence,
                item.envelope.conflict_key,
            ),
        )
        for state in pending:
            envelope = state.envelope
            if self._pending.get(envelope.conflict_key) is not state:
                continue
            if now >= envelope.valid_until_session_time_us:
                self._event(
                    LifecycleKind.REVOKE,
                    now,
                    envelope.conflict_key,
                    envelope.content_revision_sha256,
                    None,
                    ("DEADLINE_EXPIRED",),
                )
                self._decision(
                    DecisionKind.DROP_EXPIRED,
                    envelope,
                    now,
                    ("EXCLUSIVE_DEADLINE_REACHED",),
                )
                self._pending.pop(envelope.conflict_key, None)
                self._active.pop(envelope.conflict_key, None)
                continue
            if self._muted:
                self._event(
                    LifecycleKind.REVOKE,
                    now,
                    envelope.conflict_key,
                    envelope.content_revision_sha256,
                    None,
                    ("MUTED",),
                )
                self._decision(
                    DecisionKind.SUPPRESS_MUTED,
                    envelope,
                    now,
                    ("MUTED",),
                )
                self._pending.pop(envelope.conflict_key, None)
                self._active.pop(envelope.conflict_key, None)
                continue
            current_timing = (
                self._last_timing is not None
                and self._last_timing.session_time_us == now
            )
            if not self._quality_ready(now) or not current_timing:
                self._decision_once(
                    state,
                    DecisionKind.HOLD_UNSAFE,
                    now,
                    ("QUALITY_STABILITY_WINDOW_NOT_MET",),
                )
                continue
            if envelope.priority is not Priority.P0 and not self._safe_ready(now):
                self._decision_once(
                    state,
                    DecisionKind.HOLD_UNSAFE,
                    now,
                    ("ALL_SAFE_WINDOW_NOT_MET",),
                )
                continue
            if envelope.priority is not Priority.P0:
                cooldown_reasons = self._cooldown_reasons(envelope, now)
                if cooldown_reasons:
                    self._decision_once(
                        state,
                        DecisionKind.HOLD_COOLDOWN,
                        now,
                        cooldown_reasons,
                    )
                    continue
            self._decision(
                DecisionKind.SHADOW_WOULD_SPEAK,
                envelope,
                now,
                ("AUDIT_INTENT_ONLY",),
            )
            self._last_shadow_speech_us = now
            self._last_conflict_speech_us[envelope.conflict_key] = now
            self._pending.pop(envelope.conflict_key, None)

    def _expire_due(self, now: int) -> None:
        expired = sorted(
            (
                envelope
                for envelope in self._active.values()
                if now >= envelope.valid_until_session_time_us
            ),
            key=lambda envelope: (
                int(envelope.priority.value[1]),
                envelope.conflict_key,
                envelope.content_revision_sha256,
            ),
        )
        for envelope in expired:
            if self._active.get(envelope.conflict_key) is not envelope:
                continue
            self._event(
                LifecycleKind.REVOKE,
                now,
                envelope.conflict_key,
                envelope.content_revision_sha256,
                None,
                ("DEADLINE_EXPIRED",),
            )
            if envelope.priority is not Priority.P3:
                self._decision(
                    DecisionKind.DROP_EXPIRED,
                    envelope,
                    now,
                    ("EXCLUSIVE_DEADLINE_REACHED",),
                )
            self._pending.pop(envelope.conflict_key, None)
            self._active.pop(envelope.conflict_key, None)

    def _quality_ready(self, now: int) -> bool:
        return (
            self._quality_since_us is not None
            and self._quality_count >= self._config.stable_consecutive_samples
            and now - self._quality_since_us >= self._config.stable_duration_us
        )

    def _safe_ready(self, now: int) -> bool:
        return (
            self._safe_since_us is not None
            and self._safe_count >= self._config.stable_consecutive_samples
            and now - self._safe_since_us >= self._config.stable_duration_us
        )

    def _cooldown_reasons(
        self, envelope: SpeechEnvelope, now: int
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if (
            self._last_shadow_speech_us is not None
            and now - self._last_shadow_speech_us < self._config.global_cooldown_us
        ):
            reasons.append("GLOBAL_COOLDOWN")
        conflict_time = self._last_conflict_speech_us.get(envelope.conflict_key)
        if (
            conflict_time is not None
            and now - conflict_time < self._config.per_conflict_cooldown_us
        ):
            reasons.append("PER_CONFLICT_COOLDOWN")
        return tuple(reasons)

    def _clear_tactical(
        self,
        now: int,
        decision_kind: DecisionKind,
        reason_codes: tuple[str, ...],
    ) -> None:
        tactical = sorted(
            (
                envelope
                for envelope in self._active.values()
                if envelope.priority is not Priority.P3
            ),
            key=lambda envelope: (
                int(envelope.priority.value[1]),
                envelope.conflict_key,
                envelope.content_revision_sha256,
            ),
        )
        for envelope in tactical:
            self._event(
                LifecycleKind.REVOKE,
                now,
                envelope.conflict_key,
                envelope.content_revision_sha256,
                None,
                reason_codes,
            )
            self._decision(decision_kind, envelope, now, reason_codes)
            self._active.pop(envelope.conflict_key, None)
            self._pending.pop(envelope.conflict_key, None)

    def _reset_stability(self) -> None:
        self._last_timing = None
        self._quality_since_us = None
        self._quality_count = 0
        self._safe_since_us = None
        self._safe_count = 0

    def _fail_closed(self, code: str, now: int) -> None:
        if self._staged_inputs:
            for item in sorted(self._staged_inputs, key=self._input_sort_key):
                self._append_input_record(item)
            self._staged_inputs = []
            self._staged_time_us = None
        self._clear_tactical(
            now,
            DecisionKind.SUPPRESS_BOUNDARY,
            (code,),
        )
        self._reset_stability()
        self._identity_invalidated = True
        self._failure_code = code
        raise SpeechPolicyError(code)

    def _event(
        self,
        kind: LifecycleKind,
        now: int,
        conflict_key: str,
        previous_revision_sha256: str | None,
        current_revision_sha256: str | None,
        reason_codes: tuple[str, ...],
    ) -> None:
        event = SpeechLifecycleEvent(
            sequence=len(self._events),
            kind=kind,
            binding=self._binding,
            session_time_us=now,
            conflict_key=conflict_key,
            previous_revision_sha256=previous_revision_sha256,
            current_revision_sha256=current_revision_sha256,
            reason_codes=reason_codes,
        )
        self._events.append(event)
        self._transcript.append({"event": event.to_dict()})

    def _decision(
        self,
        kind: DecisionKind,
        envelope: SpeechEnvelope,
        now: int,
        reason_codes: tuple[str, ...],
    ) -> None:
        decision = SpeechDecision(
            sequence=len(self._decisions),
            kind=kind,
            binding=self._binding,
            session_time_us=now,
            conflict_key=envelope.conflict_key,
            content_revision_sha256=envelope.content_revision_sha256,
            message_class=envelope.message_class,
            priority=envelope.priority,
            template_id=envelope.template_id,
            scalar_params=envelope.scalar_params,
            message_evidence_sha256=envelope.evidence_sha256,
            timing_evidence=(
                self._last_timing
                if self._last_timing is not None
                and self._last_timing.session_time_us == now
                else None
            ),
            quality_stable_since_session_time_us=self._quality_since_us,
            safe_since_session_time_us=self._safe_since_us,
            quality_consecutive_samples=self._quality_count,
            safe_consecutive_samples=self._safe_count,
            reason_codes=reason_codes,
        )
        self._decisions.append(decision)
        self._transcript.append({"decision": decision.to_dict()})

    def _decision_once(
        self,
        pending: _Pending,
        kind: DecisionKind,
        now: int,
        reason_codes: tuple[str, ...],
    ) -> None:
        if pending.last_hold is kind:
            return
        self._decision(kind, pending.envelope, now, reason_codes)
        pending.last_hold = kind

    def finish(self) -> SpeechPolicyReceipt:
        if self._receipt is not None:
            return self._receipt
        if self._failure_code is None:
            self._flush_staged()
        self._finished = True
        event_payload = [event.to_dict() for event in self._events]
        decision_payload = [decision.to_dict() for decision in self._decisions]
        input_payload = [record.to_dict() for record in self._inputs]
        self._final_active_envelopes = tuple(
            ActiveEnvelopeSnapshot.from_envelope(envelope)
            for envelope in sorted(
                self._active.values(),
                key=lambda item: (
                    item.conflict_key,
                    item.content_revision_sha256,
                    item.envelope_sha256,
                ),
            )
        )
        final_active_payload = [
            snapshot.to_dict() for snapshot in self._final_active_envelopes
        ]
        base: dict[str, object] = {
            "binding_sha256": _sha256(self._binding.to_dict()),
            "config_sha256": _sha256(self._config.to_dict()),
            "contract_version": SPEECH_POLICY_CONTRACT_VERSION,
            "decision_count": len(self._decisions),
            "decision_kind_counts": dict(
                sorted(Counter(item.kind.value for item in self._decisions).items())
            ),
            "decisions_sha256": _sha256(decision_payload),
            "failure_code": self._failure_code,
            "final_active_envelope_count": len(self._final_active_envelopes),
            "final_active_envelopes_sha256": _sha256(final_active_payload),
            "final_muted": self._muted,
            "input_count": len(self._inputs),
            "input_kind_counts": dict(
                sorted(Counter(item.input_kind for item in self._inputs).items())
            ),
            "inputs_sha256": _sha256(input_payload),
            "lifecycle_event_count": len(self._events),
            "lifecycle_events_sha256": _sha256(event_payload),
            "lifecycle_kind_counts": dict(
                sorted(Counter(item.kind.value for item in self._events).items())
            ),
            "status": "REJECTED" if self._failure_code is not None else "PASS_SHADOW_ONLY",
            "transcript_sha256": _sha256(self._transcript),
        }
        receipt_sha256 = _sha256(base)
        self._receipt = SpeechPolicyReceipt(
            binding_sha256=str(base["binding_sha256"]),
            config_sha256=str(base["config_sha256"]),
            inputs_sha256=str(base["inputs_sha256"]),
            final_active_envelopes_sha256=str(
                base["final_active_envelopes_sha256"]
            ),
            lifecycle_events_sha256=str(base["lifecycle_events_sha256"]),
            decisions_sha256=str(base["decisions_sha256"]),
            transcript_sha256=str(base["transcript_sha256"]),
            receipt_sha256=receipt_sha256,
            input_count=len(self._inputs),
            final_active_envelope_count=len(self._final_active_envelopes),
            lifecycle_event_count=len(self._events),
            decision_count=len(self._decisions),
            input_kind_counts=tuple(
                sorted(Counter(item.input_kind for item in self._inputs).items())
            ),
            lifecycle_kind_counts=tuple(
                sorted(Counter(item.kind.value for item in self._events).items())
            ),
            decision_kind_counts=tuple(
                sorted(Counter(item.kind.value for item in self._decisions).items())
            ),
            final_muted=self._muted,
            status=str(base["status"]),
            failure_code=self._failure_code,
        )
        return self._receipt


def process_speech_policy_run(
    binding: PolicyBinding,
    inputs: Iterable[PolicyInput],
    *,
    config: SpeechPolicyConfig | None = None,
) -> SpeechPolicyRun:
    """Evaluate a finite stream and return its complete immutable artifact."""

    policy = ShadowSpeechPolicy(binding, config)
    policy.feed_chunk(inputs)
    receipt = policy.finish()
    return SpeechPolicyRun(
        binding=policy.binding,
        config=policy.config,
        input_records=policy.input_records,
        events=policy.events,
        decisions=policy.decisions,
        final_active_envelopes=policy.final_active_envelopes,
        receipt=receipt,
    )


def replay_speech_policy(
    binding: PolicyBinding,
    records: Iterable[CanonicalPolicyInputRecord | Mapping[str, object]],
    *,
    config: SpeechPolicyConfig | None = None,
) -> SpeechPolicyRun:
    """Strictly reconstruct and replay persisted canonical input records."""

    normalized: list[CanonicalPolicyInputRecord] = []
    for sequence, value in enumerate(records):
        record = (
            value
            if isinstance(value, CanonicalPolicyInputRecord)
            else CanonicalPolicyInputRecord.from_dict(value)
        )
        if record.sequence != sequence:
            raise SpeechPolicyError("NONCONTIGUOUS_CANONICAL_INPUT_SEQUENCE")
        normalized.append(record)
    run = process_speech_policy_run(
        binding,
        (record.to_policy_input() for record in normalized),
        config=config,
    )
    if run.input_records != tuple(normalized):
        raise SpeechPolicyError("CANONICAL_REPLAY_ORDER_MISMATCH")
    return run


def process_speech_policy(
    binding: PolicyBinding,
    inputs: Iterable[PolicyInput],
    *,
    config: SpeechPolicyConfig | None = None,
) -> tuple[
    tuple[SpeechLifecycleEvent, ...],
    tuple[SpeechDecision, ...],
    SpeechPolicyReceipt,
]:
    """Evaluate a finite input stream using the v1-compatible tuple API."""

    run = process_speech_policy_run(binding, inputs, config=config)
    return run.events, run.decisions, run.receipt

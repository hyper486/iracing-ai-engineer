"""Installable, local-only orchestration for one shadow engineer session.

The public session builder is completed by joining these source and derived
component helpers to the admitted advisor-timeline clock boundary.  This
module deliberately owns no CLI, renderer, audio, network, or vehicle-control
surface.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

from .adapters import (
    ValidatedCollectorRun,
    ValidatedIbtRun,
    open_collector_jsonl,
    open_collector_jsonl_snapshot,
    open_ibt_telemetry,
)
from .advisor_timeline import (
    AdvisorTimelineError,
    build_advisor_timeline,
    validate_advisor_timeline,
)
from .corner_cards import build_corner_cards
from .driving_diagnosis import build_diagnosis_evidence
from .driving_model_replay import build_driving_model_replay
from .fuel import FuelScenario
from .m2_strategy import build_m2_strategy_receipt
from .model_replay import build_fuel_model_replay
from .pit_stint import (
    PitStintReceiptError,
    build_pit_stint_receipt,
    validate_pit_stint_receipt,
)
from .speech_policy import SpeechPolicyConfig

ENGINEER_SESSION_CONTRACT_VERSION = "engineer-session-v1"
DEFAULT_ADVISOR_LEASE_DURATION_US = 2_000_000

_SHA256_CHARS = frozenset("0123456789abcdef")

_COMPONENT_KEYS = frozenset(
    {
        "advisor_timeline",
        "corner_cards",
        "driving_diagnosis",
        "driving_replay",
        "fuel_replay",
        "m1_pit_stint",
        "m2_strategy",
    }
)
_ORCHESTRATION_KEYS = frozenset(
    {
        "expected_previous_m2_sha256",
        "expected_previous_revision",
        "expected_rules_profile_sha256",
        "expected_rules_source_sha256",
        "previous_m2_receipt",
        "rules_profile",
        "strategy_context_sha256",
    }
)
_INPUT_LINEAGE_KEYS = frozenset(
    {
        "event_receipt_sha256",
        "input_evidence_sha256",
        "input_kind",
        "input_lineage_sha256",
        "normalized_samples_sha256",
        "sample_count",
        "session_id",
        "source_content_sha256",
        "source_id",
        "source_kind",
    }
)
_SESSION_KEYS = frozenset(
    {
        "admission_receipt",
        "advisor_only",
        "attestation_status",
        "component_hashes",
        "components",
        "contract_version",
        "derivation_status",
        "engineer_session_sha256",
        "execution_mode",
        "input_lineage",
        "orchestration_inputs",
        "safety",
        "semantic_hashes",
        "status",
    }
)
_SAFETY = {
    "audio_emitted": False,
    "executable": False,
    "html_rendered": False,
    "network_accessed": False,
    "vehicle_control_enabled": False,
}


class EngineerSessionError(ValueError):
    """Fail-closed error raised by engineer-session orchestration."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise EngineerSessionError(code, message)


def _canonical_json(value: object, *, newline: bool = False) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise EngineerSessionError(
            "CANONICAL_JSON_FAILED", "value is not canonical-JSON-safe"
        ) from exc
    return encoded + (b"\n" if newline else b"")


def canonical_sha256(value: object) -> str:
    """Return the canonical digest used by the session contract."""

    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _persisted_json(value: object) -> bytes:
    """Match the package CLI's stable, indented component serialization."""

    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise EngineerSessionError(
            "COMPONENT_SERIALIZATION_FAILED",
            "component is not stable JSON",
        ) from exc


def _json_object_copy(value: object, name: str) -> dict[str, object]:
    try:
        copied = json.loads(_canonical_json(value))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover
        raise EngineerSessionError(
            "CANONICAL_JSON_FAILED", f"{name} cannot be copied as JSON"
        ) from exc
    if type(copied) is not dict:
        _fail("SCHEMA_INVALID", f"{name} must be a plain object")
    return copied


def _mapping(value: object, name: str) -> dict[str, object]:
    if type(value) is not dict:
        _fail("SCHEMA_INVALID", f"{name} must be a plain object")
    return value


def _exact_mapping(
    value: object, keys: frozenset[str], name: str
) -> dict[str, object]:
    result = _mapping(value, name)
    if set(result) != keys:
        _fail("SCHEMA_INVALID", f"{name} keys are invalid")
    return result


def _sha256(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _SHA256_CHARS for character in value)
    ):
        _fail("SCHEMA_INVALID", f"{name} must be a lowercase SHA-256 digest")
    return value


def _plain_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail("SCHEMA_INVALID", f"{name} must be a plain integer >= {minimum}")
    return value


def _identifier(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 256
        or any(ord(character) < 32 for character in value)
    ):
        _fail("SCHEMA_INVALID", f"{name} must be a valid bound identifier")
    return value


def _normalization_value(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("SCHEMA_INVALID", f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        _fail("SCHEMA_INVALID", f"{name} must be finite and positive")
    return number


@contextmanager
def _open_input(
    input_path: str | Path,
    *,
    input_kind: Literal["ibt", "collector"],
    source_id: str | None,
    session_id: str | None,
    stale_after_s: float,
    opponent_error_policy: Literal["degrade", "reject"],
) -> Iterator[ValidatedIbtRun | ValidatedCollectorRun]:
    """Open one fresh adapter admission for exactly one component pass."""

    if input_kind == "ibt":
        if source_id is None or session_id is None:
            _fail(
                "IBT_IDENTITY_REQUIRED",
                "IBT input requires explicit source_id and session_id",
            )
        with open_ibt_telemetry(
            input_path,
            source_id=source_id,
            session_id=session_id,
            stale_after_s=stale_after_s,
            opponent_error_policy=opponent_error_policy,
        ) as run:
            yield run
        return
    if input_kind != "collector":
        _fail("INPUT_KIND_INVALID", "input_kind must be ibt or collector")
    if source_id is not None or session_id is not None:
        _fail(
            "COLLECTOR_IDENTITY_EMBEDDED",
            "collector source and session identities must come from its receipt",
        )
    with open_collector_jsonl(
        input_path,
        stale_after_s=stale_after_s,
        opponent_error_policy=opponent_error_policy,
        require_receipt=True,
    ) as run:
        yield run


_RunOpener = Callable[[], AbstractContextManager[ValidatedIbtRun | ValidatedCollectorRun]]
_CollectorRunOpener = Callable[[], AbstractContextManager[ValidatedCollectorRun]]


def _descriptor_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_size,
        metadata.st_ctime_ns,
        metadata.st_mtime_ns,
    )


def _hash_bound_descriptor(
    descriptor: int,
    bound_identity: tuple[int, int, int, int, int, int],
) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    byte_count = 0
    while True:
        chunk = os.read(descriptor, 1_048_576)
        if not chunk:
            break
        digest.update(chunk)
        byte_count += len(chunk)
    if (
        byte_count != bound_identity[3]
        or _descriptor_identity(os.fstat(descriptor)) != bound_identity
    ):
        _fail(
            "COLLECTOR_SNAPSHOT_CHANGED",
            "collector snapshot changed during full descriptor hash",
        )
    return digest.hexdigest()


def _collector_snapshot_opener(
    snapshot_handle: object,
    *,
    stale_after_s: float,
    opponent_error_policy: Literal["degrade", "reject"],
) -> tuple[
    _CollectorRunOpener,
    tuple[int, int, int, int, int, int],
    str,
]:
    """Bind fresh admissions to one caller-owned regular-file descriptor.

    Each admission owns a new ``dup`` and text decoder, explicitly seeks that
    duplicate to byte zero, and delegates parsing to the public snapshot
    adapter.  No pathname is accepted or reopened.  Admissions are sequential:
    ``dup`` shares a kernel file position on platforms where that is the native
    descriptor semantic, so concurrent use of ``snapshot_handle`` is forbidden.
    The caller retains ownership of the original handle.
    """

    if type(snapshot_handle) is not io.FileIO:
        raise TypeError("collector snapshot must be an unbuffered binary io.FileIO")
    if (
        snapshot_handle.closed
        or not snapshot_handle.readable()
        or not snapshot_handle.seekable()
    ):
        _fail(
            "COLLECTOR_SNAPSHOT_INVALID",
            "collector snapshot must be open, readable, and seekable",
        )
    fileno = snapshot_handle.fileno
    try:
        descriptor = fileno()
    except (OSError, ValueError) as exc:
        raise EngineerSessionError(
            "COLLECTOR_SNAPSHOT_INVALID",
            "collector snapshot descriptor is unavailable",
        ) from exc
    if type(descriptor) is not int or descriptor < 0:
        _fail(
            "COLLECTOR_SNAPSHOT_INVALID",
            "collector snapshot descriptor must be a non-negative integer",
        )
    try:
        initial = os.fstat(descriptor)
    except OSError as exc:
        raise EngineerSessionError(
            "COLLECTOR_SNAPSHOT_INVALID",
            "cannot inspect collector snapshot descriptor",
        ) from exc
    if not stat.S_ISREG(initial.st_mode) or initial.st_size < 1:
        _fail(
            "COLLECTOR_SNAPSHOT_INVALID",
            "collector snapshot must be one non-empty regular file",
        )
    bound_identity = _descriptor_identity(initial)

    bound_sha256 = _hash_bound_descriptor(descriptor, bound_identity)

    @contextmanager
    def open_admission() -> Iterator[ValidatedCollectorRun]:
        duplicate: int | None = None
        try:
            duplicate = os.dup(descriptor)
            os.lseek(duplicate, 0, os.SEEK_SET)
            if _descriptor_identity(os.fstat(duplicate)) != bound_identity:
                _fail(
                    "COLLECTOR_SNAPSHOT_CHANGED",
                    "collector snapshot identity changed before admission",
                )
            with os.fdopen(
                duplicate,
                "r",
                encoding="utf-8",
                errors="strict",
                newline="",
                closefd=True,
            ) as text_handle:
                duplicate = None
                with open_collector_jsonl_snapshot(
                    text_handle,
                    stale_after_s=stale_after_s,
                    opponent_error_policy=opponent_error_policy,
                    require_receipt=True,
                ) as run:
                    yield run
                if _descriptor_identity(os.fstat(text_handle.fileno())) != bound_identity:
                    _fail(
                        "COLLECTOR_SNAPSHOT_CHANGED",
                        "collector snapshot identity changed during admission",
                    )
        except EngineerSessionError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise EngineerSessionError(
                "COLLECTOR_SNAPSHOT_INVALID",
                f"cannot read collector snapshot descriptor: {exc}",
            ) from exc
        finally:
            if duplicate is not None:
                with suppress(OSError):
                    os.close(duplicate)

    return open_admission, bound_identity, bound_sha256


def _component_error(label: str, exc: Exception) -> EngineerSessionError:
    return EngineerSessionError(
        "COMPONENT_BUILD_FAILED", f"{label} component refused input: {exc}"
    )


def _source_content_sha256(
    evidence: Mapping[str, object], *, input_kind: Literal["ibt", "collector"]
) -> str:
    key = "source_sha256" if input_kind == "ibt" else "records_sha256"
    forbidden = "records_sha256" if input_kind == "ibt" else "source_sha256"
    if forbidden in evidence:
        _fail(
            "INPUT_LINEAGE_MISMATCH",
            f"{input_kind} evidence contains the other source digest field",
        )
    return _sha256(evidence.get(key), f"input evidence {key}")


@dataclass(frozen=True, slots=True)
class _SourceComponents:
    fuel_replay: dict[str, object]
    driving_replay: dict[str, object]
    pit_stint: dict[str, object]
    input_lineage: dict[str, object]


def _lineage_from_components(
    fuel: Mapping[str, object],
    driving: Mapping[str, object],
    pit_stint: Mapping[str, object],
    *,
    input_kind: Literal["ibt", "collector"],
) -> dict[str, object]:
    try:
        validated_pit_stint = validate_pit_stint_receipt(
            pit_stint,
            expected_pit_stint_receipt_sha256=_sha256(
                pit_stint.get("pit_stint_receipt_sha256"),
                "M1 pit/stint receipt SHA-256",
            ),
        )
    except PitStintReceiptError as exc:
        raise EngineerSessionError(
            "M1_RECEIPT_INVALID", f"M1 pit/stint receipt failed validation: {exc}"
        ) from exc
    if validated_pit_stint != pit_stint:
        _fail("M1_RECEIPT_INVALID", "M1 pit/stint receipt did not round-trip exactly")

    fuel_evidence = _mapping(fuel.get("input_evidence"), "fuel input evidence")
    driving_evidence = _mapping(
        driving.get("input_evidence"), "driving input evidence"
    )
    pit_evidence = _mapping(
        validated_pit_stint.get("input_evidence"), "pit/stint input evidence"
    )
    if fuel_evidence != driving_evidence or fuel_evidence != pit_evidence:
        _fail(
            "INPUT_LINEAGE_MISMATCH",
            "fresh component admissions do not share exact input evidence",
        )
    if fuel.get("input_kind") != input_kind or driving.get("input_kind") != input_kind:
        _fail("INPUT_LINEAGE_MISMATCH", "component input kinds do not close")

    fuel_normalized = _mapping(
        fuel.get("normalized_input_receipt"), "fuel normalized receipt"
    )
    driving_normalized = _mapping(
        driving.get("normalized_input_receipt"), "driving normalized receipt"
    )
    pit_normalized = _mapping(
        validated_pit_stint.get("normalized_input_receipt"),
        "pit/stint normalized receipt",
    )
    if fuel_normalized != driving_normalized or fuel_normalized != pit_normalized:
        _fail(
            "INPUT_LINEAGE_MISMATCH",
            "fresh component admissions do not share one normalized stream",
        )

    fuel_event = _mapping(fuel.get("event_receipt"), "fuel event receipt")
    driving_event = _mapping(driving.get("event_receipt"), "driving event receipt")
    pit_event = _mapping(
        validated_pit_stint.get("upstream_event_receipt"),
        "pit/stint event receipt",
    )
    if fuel_event != driving_event or fuel_event != pit_event:
        _fail(
            "INPUT_LINEAGE_MISMATCH",
            "fresh component admissions do not share one event receipt",
        )

    fuel_profile = _mapping(
        _mapping(fuel.get("pipeline"), "fuel pipeline").get("normalization"),
        "fuel normalization profile",
    )
    driving_profile = _mapping(
        _mapping(driving.get("pipeline"), "driving pipeline").get(
            "normalization"
        ),
        "driving normalization profile",
    )
    pit_profile = _mapping(
        _mapping(
            validated_pit_stint.get("input_binding"), "pit/stint input binding"
        ).get("normalization_profile"),
        "pit/stint normalization profile",
    )
    if fuel_profile != driving_profile or fuel_profile != pit_profile:
        _fail(
            "INPUT_LINEAGE_MISMATCH",
            "component admissions do not share one normalization profile",
        )

    sample_count = _plain_int(
        fuel_normalized.get("sample_count"), "normalized sample_count", minimum=1
    )
    if fuel_event.get("sample_count") != sample_count:
        _fail("INPUT_LINEAGE_MISMATCH", "event and normalized counts do not close")
    source_kind = _identifier(fuel_evidence.get("source_kind"), "source_kind")
    expected_source_kinds = (
        {"IBT_OFFLINE"}
        if input_kind == "ibt"
        else {"SDK_LIVE", "REPLAY_SDK_PROXY"}
    )
    if source_kind not in expected_source_kinds:
        _fail("INPUT_LINEAGE_MISMATCH", "input kind and source kind do not agree")

    source_digest = _source_content_sha256(fuel_evidence, input_kind=input_kind)
    base = {
        "event_receipt_sha256": _sha256(
            fuel_event.get("receipt_sha256"), "event receipt SHA-256"
        ),
        "input_evidence_sha256": canonical_sha256(fuel_evidence),
        "input_kind": input_kind,
        "normalized_samples_sha256": _sha256(
            fuel_normalized.get("samples_sha256"), "normalized samples SHA-256"
        ),
        "sample_count": sample_count,
        "session_id": _identifier(fuel_evidence.get("session_id"), "session_id"),
        "source_content_sha256": source_digest,
        "source_id": _identifier(fuel_evidence.get("source_id"), "source_id"),
        "source_kind": source_kind,
    }
    return {**base, "input_lineage_sha256": canonical_sha256(base)}


def _build_source_components_from_opener(
    open_input: _RunOpener,
    *,
    input_kind: Literal["ibt", "collector"],
    scenario: FuelScenario,
) -> _SourceComponents:
    """Build fuel, driving, and M1 from three independent admissions."""

    try:
        with open_input() as run:
            fuel = _json_object_copy(
                build_fuel_model_replay(run, scenario=scenario), "fuel replay"
            )
    except EngineerSessionError:
        raise
    except Exception as exc:
        raise _component_error("fuel", exc) from exc

    try:
        with open_input() as run:
            driving = _json_object_copy(
                build_driving_model_replay(run), "driving replay"
            )
    except EngineerSessionError:
        raise
    except Exception as exc:
        raise _component_error("driving", exc) from exc

    evidence = _mapping(fuel.get("input_evidence"), "fuel input evidence")
    normalized = _mapping(
        fuel.get("normalized_input_receipt"), "fuel normalized receipt"
    )
    event = _mapping(fuel.get("event_receipt"), "fuel event receipt")
    try:
        with open_input() as run:
            pit_stint = _json_object_copy(
                build_pit_stint_receipt(
                    run,
                    expected_source_sha256=_source_content_sha256(
                        evidence, input_kind=input_kind
                    ),
                    expected_normalized_samples_sha256=_sha256(
                        normalized.get("samples_sha256"),
                        "normalized samples SHA-256",
                    ),
                    expected_event_receipt_sha256=_sha256(
                        event.get("receipt_sha256"), "event receipt SHA-256"
                    ),
                ),
                "pit/stint receipt",
            )
    except EngineerSessionError:
        raise
    except Exception as exc:
        raise _component_error("pit/stint", exc) from exc

    lineage = _lineage_from_components(
        fuel,
        driving,
        pit_stint,
        input_kind=input_kind,
    )
    return _SourceComponents(fuel, driving, pit_stint, lineage)


def _build_source_components(
    input_path: str | Path,
    *,
    input_kind: Literal["ibt", "collector"],
    source_id: str | None,
    session_id: str | None,
    scenario: FuelScenario,
    stale_after_s: float,
    opponent_error_policy: Literal["degrade", "reject"],
) -> _SourceComponents:
    def open_input() -> AbstractContextManager[ValidatedIbtRun | ValidatedCollectorRun]:
        return _open_input(
            input_path,
            input_kind=input_kind,
            source_id=source_id,
            session_id=session_id,
            stale_after_s=stale_after_s,
            opponent_error_policy=opponent_error_policy,
        )

    return _build_source_components_from_opener(
        open_input,
        input_kind=input_kind,
        scenario=scenario,
    )


@dataclass(frozen=True, slots=True)
class _DerivedComponents:
    m2_strategy: dict[str, object]
    corner_cards: dict[str, object]
    driving_diagnosis: dict[str, object]
    orchestration_inputs: dict[str, object]


def _build_derived_components(
    source: _SourceComponents,
    *,
    strategy_context: Mapping[str, object],
    rules_profile: Mapping[str, object] | None,
    expected_rules_profile_sha256: str | None,
    expected_rules_source_sha256: str | None,
    previous_m2_receipt: Mapping[str, object] | None,
    expected_previous_m2_sha256: str | None,
    expected_previous_revision: int | None,
) -> _DerivedComponents:
    """Build M2, corner cards, and diagnosis from admitted source outputs."""

    context = _json_object_copy(strategy_context, "strategy context")
    context_sha = _sha256(context.get("context_sha256"), "strategy context SHA-256")
    rules = (
        _json_object_copy(rules_profile, "rules profile")
        if rules_profile is not None
        else None
    )
    previous = (
        _json_object_copy(previous_m2_receipt, "previous M2 receipt")
        if previous_m2_receipt is not None
        else None
    )
    if rules is None:
        if (
            expected_rules_profile_sha256 is not None
            or expected_rules_source_sha256 is not None
        ):
            _fail(
                "OPTIONAL_INPUT_INVALID",
                "rules digests require an exact rules profile",
            )
    else:
        _sha256(expected_rules_profile_sha256, "expected rules profile SHA-256")
        _sha256(expected_rules_source_sha256, "expected rules source SHA-256")
    if previous is None:
        if (
            expected_previous_m2_sha256 is not None
            or expected_previous_revision is not None
        ):
            _fail(
                "OPTIONAL_INPUT_INVALID",
                "previous M2 expectations require an exact previous receipt",
            )
    else:
        _sha256(expected_previous_m2_sha256, "expected previous M2 SHA-256")
        _plain_int(
            expected_previous_revision,
            "expected previous M2 revision",
            minimum=1,
        )

    try:
        m2 = _json_object_copy(
            build_m2_strategy_receipt(
                source.fuel_replay,
                source.pit_stint,
                context,
                expected_fuel_replay_sha256=_sha256(
                    source.fuel_replay.get("fuel_replay_sha256"),
                    "fuel replay SHA-256",
                ),
                expected_m1_receipt_sha256=_sha256(
                    source.pit_stint.get("pit_stint_receipt_sha256"),
                    "M1 receipt SHA-256",
                ),
                expected_strategy_context_sha256=context_sha,
                rules_profile_value=rules,
                expected_rules_profile_sha256=expected_rules_profile_sha256,
                expected_rules_source_sha256=expected_rules_source_sha256,
                previous_receipt_value=previous,
                expected_previous_receipt_sha256=expected_previous_m2_sha256,
                expected_previous_revision=expected_previous_revision,
            ),
            "M2 strategy receipt",
        )
    except EngineerSessionError:
        raise
    except Exception as exc:
        raise _component_error("M2 strategy", exc) from exc

    try:
        cards = _json_object_copy(
            build_corner_cards(source.driving_replay, top=3), "corner cards"
        )
        diagnosis = _json_object_copy(
            build_diagnosis_evidence(_persisted_json(source.driving_replay)),
            "driving diagnosis",
        )
    except EngineerSessionError:
        raise
    except Exception as exc:
        raise _component_error("driving evidence", exc) from exc

    orchestration = {
        "expected_previous_m2_sha256": expected_previous_m2_sha256,
        "expected_previous_revision": expected_previous_revision,
        "expected_rules_profile_sha256": expected_rules_profile_sha256,
        "expected_rules_source_sha256": expected_rules_source_sha256,
        "previous_m2_receipt": previous,
        "rules_profile": rules,
        "strategy_context_sha256": context_sha,
    }
    return _DerivedComponents(m2, cards, diagnosis, orchestration)


def _component_hashes(components: Mapping[str, object]) -> dict[str, str]:
    fields = {
        "advisor_timeline": "advisor_timeline_sha256",
        "corner_cards": "corner_cards_sha256",
        "driving_diagnosis": "diagnosis_evidence_sha256",
        "driving_replay": "driving_replay_sha256",
        "fuel_replay": "fuel_replay_sha256",
        "m1_pit_stint": "pit_stint_receipt_sha256",
        "m2_strategy": "m2_strategy_receipt_sha256",
    }
    return {
        name: _sha256(
            _mapping(components.get(name), f"{name} component").get(field),
            f"{name} component SHA-256",
        )
        for name, field in fields.items()
    }


def _semantic_hashes(components: Mapping[str, object]) -> dict[str, str]:
    material = {
        "driving_model_semantic_sha256": _sha256(
            _mapping(
                components.get("driving_replay"), "driving replay component"
            ).get("model_semantic_sha256"),
            "driving model semantic SHA-256",
        ),
        "fuel_model_semantic_sha256": _sha256(
            _mapping(components.get("fuel_replay"), "fuel replay component").get(
                "model_semantic_sha256"
            ),
            "fuel model semantic SHA-256",
        ),
        "m1_pit_stint_semantic_sha256": canonical_sha256(
            _m1_semantic_projection(
                components.get("m1_pit_stint"),
            )
        ),
    }
    return {**material, "source_neutral_sha256": canonical_sha256(material)}


def _m1_semantic_projection(value: object) -> dict[str, object]:
    """Project validated M1 meaning while removing adapter-specific leaves.

    All result, safety, quality, capability, and normalization fields remain.
    Content/receipt digests and adapter identity are provenance, while record
    count and tick rate are mapped onto their common source-neutral meaning.
    """

    try:
        receipt = validate_pit_stint_receipt(
            value,
            expected_pit_stint_receipt_sha256=_sha256(
                _mapping(value, "M1 pit/stint receipt").get(
                    "pit_stint_receipt_sha256"
                ),
                "M1 pit/stint receipt SHA-256",
            ),
        )
    except PitStintReceiptError as exc:
        raise EngineerSessionError(
            "M1_RECEIPT_INVALID", f"M1 semantic projection refused input: {exc}"
        ) from exc

    evidence = _mapping(receipt["input_evidence"], "M1 input evidence")
    if evidence.get("source_kind") == "IBT_OFFLINE":
        sample_count = _plain_int(
            evidence.get("record_count"), "M1 IBT record_count", minimum=1
        )
        tick_rate_hz = _plain_int(
            evidence.get("tick_rate_hz"), "M1 IBT tick_rate_hz", minimum=1
        )
    else:
        sample_count = _plain_int(
            evidence.get("frame_record_count"),
            "M1 collector frame_record_count",
            minimum=1,
        )
        rates = evidence.get("tick_rate_hz_values")
        if type(rates) is not list or len(rates) != 1:
            _fail("M1_RECEIPT_INVALID", "M1 collector tick rate is not singular")
        tick_rate_hz = _plain_int(rates[0], "M1 collector tick_rate_hz", minimum=1)

    binding = _mapping(receipt["input_binding"], "M1 input binding")
    normalized = _mapping(
        receipt["normalized_input_receipt"], "M1 normalized receipt"
    )
    event = _mapping(receipt["upstream_event_receipt"], "M1 event receipt")
    projection = {
        key: item
        for key, item in receipt.items()
        if key
        not in {
            "input_binding",
            "input_evidence",
            "normalized_input_receipt",
            "pit_stint_receipt_sha256",
            "upstream_event_receipt",
        }
    }
    projection["input_binding"] = {
        "normalization_profile": binding["normalization_profile"],
    }
    projection["input_evidence"] = {
        "completion_status": evidence["completion_status"],
        "sample_count": sample_count,
        "tick_rate_hz": tick_rate_hz,
    }
    projection["normalized_input_receipt"] = {
        key: item for key, item in normalized.items() if key != "samples_sha256"
    }
    projection["upstream_event_receipt"] = {
        key: item
        for key, item in event.items()
        if key not in {"events_sha256", "receipt_sha256"}
    }
    return projection


def _validate_timeline_lineage(
    timeline: Mapping[str, object],
    m2: Mapping[str, object],
    diagnosis: Mapping[str, object],
    driving: Mapping[str, object],
    lineage: Mapping[str, object],
) -> str:
    clock = _mapping(timeline.get("clock_receipt"), "advisor clock receipt")
    clock_source = _mapping(
        clock.get("source_binding"), "advisor clock source binding"
    )
    expected_source = {
        "event_receipt_sha256": lineage["event_receipt_sha256"],
        "normalized_samples_sha256": lineage["normalized_samples_sha256"],
        "sample_count": lineage["sample_count"],
        "session_id": lineage["session_id"],
        "source_id": lineage["source_id"],
        "source_kind": lineage["source_kind"],
        "source_sha256": lineage["source_content_sha256"],
    }
    if clock_source != expected_source:
        _fail(
            "INPUT_LINEAGE_MISMATCH",
            "advisor clock source does not close to component admissions",
        )
    if (
        clock.get("input_kind") != lineage["input_kind"]
        or clock.get("input_evidence_sha256")
        != lineage["input_evidence_sha256"]
    ):
        _fail(
            "INPUT_LINEAGE_MISMATCH",
            "advisor clock evidence does not close to component admissions",
        )

    timeline_binding = _mapping(
        timeline.get("input_binding"), "advisor timeline input binding"
    )
    serialized_sha = hashlib.sha256(_persisted_json(driving)).hexdigest()
    expected_timeline_binding = {
        "diagnosis_evidence_sha256": diagnosis["diagnosis_evidence_sha256"],
        "driving_replay_serialized_sha256": serialized_sha,
        "event_receipt_sha256": lineage["event_receipt_sha256"],
        "m2_receipt_sha256": [
            _sha256(
                m2.get("m2_strategy_receipt_sha256"),
                "M2 strategy receipt SHA-256",
            )
        ],
        "normalized_samples_sha256": lineage["normalized_samples_sha256"],
        "sample_count": lineage["sample_count"],
        "session_id": lineage["session_id"],
        "source_id": lineage["source_id"],
        "source_kind": lineage["source_kind"],
        "source_sha256": lineage["source_content_sha256"],
    }
    if timeline_binding != expected_timeline_binding:
        _fail(
            "INPUT_LINEAGE_MISMATCH",
            "advisor timeline does not close to source and diagnosis receipts",
        )
    return _sha256(
        clock.get("clock_receipt_sha256"), "advisor clock receipt SHA-256"
    )


def _admission_receipt(
    components: Mapping[str, object], lineage: Mapping[str, object]
) -> dict[str, object]:
    timeline = _mapping(
        components.get("advisor_timeline"), "advisor timeline component"
    )
    diagnosis = _mapping(
        components.get("driving_diagnosis"), "driving diagnosis component"
    )
    m2 = _mapping(components.get("m2_strategy"), "M2 strategy component")
    driving = _mapping(
        components.get("driving_replay"), "driving replay component"
    )
    clock_sha = _validate_timeline_lineage(
        timeline, m2, diagnosis, driving, lineage
    )
    hashes = _component_hashes(components)
    shared = {
        "event_receipt_sha256": lineage["event_receipt_sha256"],
        "input_evidence_sha256": lineage["input_evidence_sha256"],
        "normalized_samples_sha256": lineage["normalized_samples_sha256"],
        "source_content_sha256": lineage["source_content_sha256"],
    }
    passes = [
        {
            **shared,
            "component_sha256": hashes[component],
            "consumer": consumer,
            "ordinal": ordinal,
        }
        for ordinal, (consumer, component) in enumerate(
            (
                ("fuel_model_replay", "fuel_replay"),
                ("driving_model_replay", "driving_replay"),
                ("m1_pit_stint", "m1_pit_stint"),
                ("advisor_timeline_clock", "advisor_timeline"),
            ),
            start=1,
        )
    ]
    base = {
        "advisor_clock_receipt_sha256": clock_sha,
        "fresh_admission_count": 4,
        "passes": passes,
    }
    return {**base, "admission_receipt_sha256": canonical_sha256(base)}


def _assemble_session(
    source: _SourceComponents,
    derived: _DerivedComponents,
    timeline: Mapping[str, object],
) -> dict[str, object]:
    components: dict[str, object] = {
        "advisor_timeline": _json_object_copy(timeline, "advisor timeline"),
        "corner_cards": derived.corner_cards,
        "driving_diagnosis": derived.driving_diagnosis,
        "driving_replay": source.driving_replay,
        "fuel_replay": source.fuel_replay,
        "m1_pit_stint": source.pit_stint,
        "m2_strategy": derived.m2_strategy,
    }
    lineage = _json_object_copy(source.input_lineage, "input lineage")
    timeline_value = _mapping(
        components["advisor_timeline"], "advisor timeline component"
    )
    base: dict[str, object] = {
        "admission_receipt": _admission_receipt(components, lineage),
        "advisor_only": True,
        "attestation_status": "NOT_R7_ATTESTED",
        "component_hashes": _component_hashes(components),
        "components": components,
        "contract_version": ENGINEER_SESSION_CONTRACT_VERSION,
        "derivation_status": "POST_ADMISSION_PACKAGE_EXTERNAL",
        "execution_mode": "SHADOW_ONLY",
        "input_lineage": lineage,
        "orchestration_inputs": derived.orchestration_inputs,
        "safety": dict(_SAFETY),
        "semantic_hashes": _semantic_hashes(components),
        "status": timeline_value.get("status"),
    }
    return {**base, "engineer_session_sha256": canonical_sha256(base)}


def _build_engineer_session_from_opener(
    open_input: _RunOpener,
    *,
    input_kind: Literal["ibt", "collector"],
    scenario: FuelScenario,
    strategy_context: Mapping[str, object] | None,
    strategy_context_builder: (
        Callable[[Mapping[str, object]], Mapping[str, object]] | None
    ),
    rules_profile: Mapping[str, object] | None,
    expected_rules_profile_sha256: str | None,
    expected_rules_source_sha256: str | None,
    previous_m2_receipt: Mapping[str, object] | None,
    expected_previous_m2_sha256: str | None,
    expected_previous_revision: int | None,
    advisor_config: SpeechPolicyConfig | None,
    advisor_lease_duration_us: int,
) -> dict[str, object]:
    if (strategy_context is None) == (strategy_context_builder is None):
        _fail(
            "STRATEGY_CONTEXT_INVALID",
            "supply exactly one strategy context or source-bound context builder",
        )
    source = _build_source_components_from_opener(
        open_input,
        input_kind=input_kind,
        scenario=scenario,
    )
    if strategy_context_builder is not None:
        try:
            context_value = strategy_context_builder(
                _json_object_copy(source.input_lineage, "input lineage")
            )
        except EngineerSessionError:
            raise
        except Exception as exc:
            raise EngineerSessionError(
                "STRATEGY_CONTEXT_BUILD_FAILED",
                f"source-bound strategy context builder failed: {exc}",
            ) from exc
    else:
        assert strategy_context is not None
        context_value = strategy_context
    derived = _build_derived_components(
        source,
        strategy_context=context_value,
        rules_profile=rules_profile,
        expected_rules_profile_sha256=expected_rules_profile_sha256,
        expected_rules_source_sha256=expected_rules_source_sha256,
        previous_m2_receipt=previous_m2_receipt,
        expected_previous_m2_sha256=expected_previous_m2_sha256,
        expected_previous_revision=expected_previous_revision,
    )
    serialized_driving = _persisted_json(source.driving_replay)
    serialized_sha = hashlib.sha256(serialized_driving).hexdigest()
    m2_sha = _sha256(
        derived.m2_strategy.get("m2_strategy_receipt_sha256"),
        "M2 strategy receipt SHA-256",
    )
    try:
        with open_input() as run:
            timeline = _json_object_copy(
                build_advisor_timeline(
                    run,
                    [derived.m2_strategy],
                    serialized_driving,
                    expected_m2_receipt_sha256s=[m2_sha],
                    expected_driving_replay_serialized_sha256=serialized_sha,
                    config=advisor_config,
                    lease_duration_us=advisor_lease_duration_us,
                ),
                "advisor timeline",
            )
    except EngineerSessionError:
        raise
    except Exception as exc:
        raise _component_error("advisor timeline", exc) from exc

    receipt = _assemble_session(source, derived, timeline)
    return validate_engineer_session(
        receipt,
        expected_engineer_session_sha256=_sha256(
            receipt.get("engineer_session_sha256"), "engineer session SHA-256"
        ),
    )


def build_engineer_session(
    input_path: str | Path,
    *,
    input_kind: Literal["ibt", "collector"],
    scenario: FuelScenario,
    strategy_context: Mapping[str, object],
    source_id: str | None = None,
    session_id: str | None = None,
    rules_profile: Mapping[str, object] | None = None,
    expected_rules_profile_sha256: str | None = None,
    expected_rules_source_sha256: str | None = None,
    previous_m2_receipt: Mapping[str, object] | None = None,
    expected_previous_m2_sha256: str | None = None,
    expected_previous_revision: int | None = None,
    stale_after_s: float = 0.5,
    opponent_error_policy: Literal["degrade", "reject"] = "degrade",
    advisor_config: SpeechPolicyConfig | None = None,
    advisor_lease_duration_us: int = DEFAULT_ADVISOR_LEASE_DURATION_US,
) -> dict[str, object]:
    """Build one local shadow session from four fresh source admissions.

    Fuel, driving, M1, and the advisor clock each consume a different opaque
    adapter run.  The fourth run is the sole authority for mapping the M2
    decision tick to SessionTime; caller-supplied clock values are not accepted.
    """

    def open_input() -> AbstractContextManager[ValidatedIbtRun | ValidatedCollectorRun]:
        return _open_input(
            input_path,
            input_kind=input_kind,
            source_id=source_id,
            session_id=session_id,
            stale_after_s=stale_after_s,
            opponent_error_policy=opponent_error_policy,
        )

    return _build_engineer_session_from_opener(
        open_input,
        input_kind=input_kind,
        scenario=scenario,
        strategy_context=strategy_context,
        strategy_context_builder=None,
        rules_profile=rules_profile,
        expected_rules_profile_sha256=expected_rules_profile_sha256,
        expected_rules_source_sha256=expected_rules_source_sha256,
        previous_m2_receipt=previous_m2_receipt,
        expected_previous_m2_sha256=expected_previous_m2_sha256,
        expected_previous_revision=expected_previous_revision,
        advisor_config=advisor_config,
        advisor_lease_duration_us=advisor_lease_duration_us,
    )


def build_engineer_session_from_collector_snapshot(
    snapshot_handle: object,
    *,
    scenario: FuelScenario,
    strategy_context: Mapping[str, object] | None = None,
    strategy_context_builder: (
        Callable[[Mapping[str, object]], Mapping[str, object]] | None
    ) = None,
    expected_snapshot_sha256: str | None = None,
    expected_snapshot_byte_size: int | None = None,
    rules_profile: Mapping[str, object] | None = None,
    expected_rules_profile_sha256: str | None = None,
    expected_rules_source_sha256: str | None = None,
    previous_m2_receipt: Mapping[str, object] | None = None,
    expected_previous_m2_sha256: str | None = None,
    expected_previous_revision: int | None = None,
    stale_after_s: float = 0.5,
    opponent_error_policy: Literal["degrade", "reject"] = "degrade",
    advisor_config: SpeechPolicyConfig | None = None,
    advisor_lease_duration_us: int = DEFAULT_ADVISOR_LEASE_DURATION_US,
) -> dict[str, object]:
    """Build from four fresh admissions of one caller-owned capture handle.

    The original descriptor is never closed and no pathname is consulted.
    Every pass owns a sequential ``dup`` positioned at byte zero.  Callers must
    keep the completed capture immutable and must not use or seek its handle
    concurrently until this function returns.
    """

    open_input, bound_identity, bound_sha256 = _collector_snapshot_opener(
        snapshot_handle,
        stale_after_s=stale_after_s,
        opponent_error_policy=opponent_error_policy,
    )
    if expected_snapshot_sha256 is not None and bound_sha256 != _sha256(
        expected_snapshot_sha256, "expected collector snapshot SHA-256"
    ):
        _fail(
            "COLLECTOR_SNAPSHOT_DIGEST_MISMATCH",
            "collector snapshot differs from its independent digest",
        )
    if expected_snapshot_byte_size is not None and bound_identity[3] != _plain_int(
        expected_snapshot_byte_size,
        "expected collector snapshot byte size",
        minimum=1,
    ):
        _fail(
            "COLLECTOR_SNAPSHOT_DIGEST_MISMATCH",
            "collector snapshot differs from its independent byte size",
        )
    receipt = _build_engineer_session_from_opener(
        open_input,
        input_kind="collector",
        scenario=scenario,
        strategy_context=strategy_context,
        strategy_context_builder=strategy_context_builder,
        rules_profile=rules_profile,
        expected_rules_profile_sha256=expected_rules_profile_sha256,
        expected_rules_source_sha256=expected_rules_source_sha256,
        previous_m2_receipt=previous_m2_receipt,
        expected_previous_m2_sha256=expected_previous_m2_sha256,
        expected_previous_revision=expected_previous_revision,
        advisor_config=advisor_config,
        advisor_lease_duration_us=advisor_lease_duration_us,
    )
    descriptor = snapshot_handle.fileno()
    try:
        final_identity = _descriptor_identity(os.fstat(descriptor))
    except OSError as exc:
        raise EngineerSessionError(
            "COLLECTOR_SNAPSHOT_CHANGED",
            "collector snapshot descriptor closed during analysis",
        ) from exc
    if final_identity != bound_identity:
        _fail(
            "COLLECTOR_SNAPSHOT_CHANGED",
            "collector snapshot identity changed across fresh admissions",
        )
    if _hash_bound_descriptor(descriptor, bound_identity) != bound_sha256:
        _fail(
            "COLLECTOR_SNAPSHOT_CHANGED",
            "collector snapshot bytes changed across fresh admissions",
        )
    return receipt


def validate_engineer_session(
    value: object,
    *,
    expected_engineer_session_sha256: str | None = None,
) -> dict[str, object]:
    """Validate and replay one persisted session without reopening its source.

    This proves schema, hashes, cross-receipt lineage, and deterministic derived
    outputs.  It does not independently re-prove source authenticity; a full
    rebuild from ``input_path`` must call :func:`build_engineer_session` again.
    Supplying an independently retained outer digest also detects total rehashes.
    """

    payload = _exact_mapping(
        _json_object_copy(value, "engineer session"),
        _SESSION_KEYS,
        "engineer session",
    )
    if payload.get("contract_version") != ENGINEER_SESSION_CONTRACT_VERSION:
        _fail("CONTRACT_VERSION_MISMATCH", "unsupported engineer session contract")
    stored = _sha256(
        payload.get("engineer_session_sha256"), "engineer session SHA-256"
    )
    if expected_engineer_session_sha256 is not None and stored != _sha256(
        expected_engineer_session_sha256,
        "expected engineer session SHA-256",
    ):
        _fail(
            "ENGINEER_SESSION_SHA256_MISMATCH",
            "engineer session failed independent digest binding",
        )
    material = {
        key: item
        for key, item in payload.items()
        if key != "engineer_session_sha256"
    }
    if canonical_sha256(material) != stored:
        _fail(
            "ENGINEER_SESSION_SHA256_MISMATCH",
            "engineer session self hash mismatch",
        )
    if (
        payload.get("advisor_only") is not True
        or payload.get("attestation_status") != "NOT_R7_ATTESTED"
        or payload.get("derivation_status")
        != "POST_ADMISSION_PACKAGE_EXTERNAL"
        or payload.get("execution_mode") != "SHADOW_ONLY"
        or payload.get("safety") != _SAFETY
    ):
        _fail("SAFETY_BOUNDARY_INVALID", "engineer session safety boundary is invalid")

    components = _exact_mapping(
        payload.get("components"), _COMPONENT_KEYS, "engineer session components"
    )
    fuel = _mapping(components.get("fuel_replay"), "fuel replay component")
    driving = _mapping(
        components.get("driving_replay"), "driving replay component"
    )
    pit_stint = _mapping(
        components.get("m1_pit_stint"), "M1 pit/stint component"
    )
    m2 = _mapping(components.get("m2_strategy"), "M2 strategy component")
    diagnosis = _mapping(
        components.get("driving_diagnosis"), "driving diagnosis component"
    )
    timeline = _mapping(
        components.get("advisor_timeline"), "advisor timeline component"
    )

    lineage = _exact_mapping(
        payload.get("input_lineage"), _INPUT_LINEAGE_KEYS, "input lineage"
    )
    input_kind = lineage.get("input_kind")
    if input_kind not in {"ibt", "collector"}:
        _fail("INPUT_LINEAGE_MISMATCH", "input lineage kind is invalid")
    rebuilt_lineage = _lineage_from_components(
        fuel,
        driving,
        pit_stint,
        input_kind=input_kind,
    )
    if rebuilt_lineage != lineage:
        _fail(
            "INPUT_LINEAGE_MISMATCH",
            "persisted input lineage does not reproduce exactly",
        )

    orchestration = _exact_mapping(
        payload.get("orchestration_inputs"),
        _ORCHESTRATION_KEYS,
        "orchestration inputs",
    )
    context = _mapping(m2.get("strategy_context"), "M2 strategy context")
    source = _SourceComponents(fuel, driving, pit_stint, rebuilt_lineage)
    replayed = _build_derived_components(
        source,
        strategy_context=context,
        rules_profile=orchestration["rules_profile"],
        expected_rules_profile_sha256=orchestration[
            "expected_rules_profile_sha256"
        ],
        expected_rules_source_sha256=orchestration[
            "expected_rules_source_sha256"
        ],
        previous_m2_receipt=orchestration["previous_m2_receipt"],
        expected_previous_m2_sha256=orchestration[
            "expected_previous_m2_sha256"
        ],
        expected_previous_revision=orchestration["expected_previous_revision"],
    )
    if (
        replayed.m2_strategy != m2
        or replayed.corner_cards != components["corner_cards"]
        or replayed.driving_diagnosis != diagnosis
        or replayed.orchestration_inputs != orchestration
    ):
        _fail(
            "COMPONENT_REPLAY_MISMATCH",
            "derived components do not reproduce exactly",
        )

    expected_hashes = _component_hashes(components)
    if payload.get("component_hashes") != expected_hashes:
        _fail("COMPONENT_HASH_MISMATCH", "component hash table does not close")
    expected_semantics = _semantic_hashes(components)
    if payload.get("semantic_hashes") != expected_semantics:
        _fail("SEMANTIC_HASH_MISMATCH", "semantic hash table does not close")

    expected_admission = _admission_receipt(components, lineage)
    if payload.get("admission_receipt") != expected_admission:
        _fail("ADMISSION_RECEIPT_MISMATCH", "admission receipt does not close")
    clock_sha = _sha256(
        expected_admission.get("advisor_clock_receipt_sha256"),
        "advisor clock receipt SHA-256",
    )
    serialized_driving = _persisted_json(driving)
    serialized_driving_sha = hashlib.sha256(serialized_driving).hexdigest()
    try:
        validated_timeline = validate_advisor_timeline(
            timeline,
            [m2],
            serialized_driving,
            expected_m2_receipt_sha256s=[expected_hashes["m2_strategy"]],
            expected_driving_replay_serialized_sha256=serialized_driving_sha,
            expected_clock_receipt_sha256=clock_sha,
        )
    except AdvisorTimelineError as exc:
        raise EngineerSessionError(
            "ADVISOR_TIMELINE_INVALID",
            f"advisor timeline replay failed: {exc}",
        ) from exc
    if validated_timeline != timeline:
        _fail(
            "ADVISOR_TIMELINE_INVALID",
            "advisor timeline does not reproduce exactly",
        )
    if payload.get("status") != timeline.get("status"):
        _fail("STATUS_MISMATCH", "session status promoted the advisor timeline")
    return payload


def write_engineer_session_exclusive(
    path: str | Path, receipt: Mapping[str, object]
) -> None:
    """Validate and CreateNew-write one deterministic JSON session artifact.

    A failed write deliberately leaves every directory entry untouched: an
    operator may need to inspect or remove a partial artifact, but this process
    must never unlink a pathname that another actor could have replaced.  A
    successful return proves the final platform-specific observation made below;
    it cannot make the pathname immutable after that observation.
    """

    validated = validate_engineer_session(receipt)
    payload = _persisted_json(validated)
    output = Path(path)
    name = output.name
    if name in {"", ".", ".."}:
        _fail("OUTPUT_PATH_INVALID", "output must name one file in a parent directory")
    parent = output.parent
    parent_descriptor: int | None = None
    if os.name == "nt":
        try:
            absolute_parent = parent.absolute()
            resolved_parent = parent.resolve(strict=True)
            parent_identity = os.lstat(absolute_parent)
        except OSError as exc:
            raise EngineerSessionError(
                "OUTPUT_PARENT_OPEN_FAILED",
                f"cannot inspect output parent safely: {exc}",
            ) from exc
        if (
            os.path.normcase(str(absolute_parent))
            != os.path.normcase(str(resolved_parent))
            or not stat.S_ISDIR(parent_identity.st_mode)
            or stat.S_ISLNK(parent_identity.st_mode)
            or int(getattr(parent_identity, "st_file_attributes", 0)) & 0x400
        ):
            _fail(
                "OUTPUT_PARENT_OPEN_FAILED",
                "Windows output parent must be one real non-reparse directory",
            )
        output = absolute_parent / name
        parent = absolute_parent
    else:
        parent_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            parent_descriptor = os.open(parent, parent_flags)
        except OSError as exc:
            raise EngineerSessionError(
                "OUTPUT_PARENT_OPEN_FAILED", f"cannot open output parent safely: {exc}"
            ) from exc
        parent_identity = os.fstat(parent_descriptor)
        if not stat.S_ISDIR(parent_identity.st_mode):
            os.close(parent_descriptor)
            _fail("OUTPUT_PARENT_OPEN_FAILED", "output parent is not a directory")

    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    verification_descriptor: int | None = None
    opened_identity: os.stat_result | None = None
    payload_sha256 = hashlib.sha256(payload).digest()

    def same_identity(left: os.stat_result, right: os.stat_result) -> bool:
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)

    def is_plain_file(metadata: os.stat_result) -> bool:
        return (
            stat.S_ISREG(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and not (int(getattr(metadata, "st_file_attributes", 0)) & 0x400)
        )

    def mode_is_admitted(metadata: os.stat_result) -> bool:
        # Windows does not expose the protected installation ACL through POSIX
        # permission bits.  The enclosing R8 security-tree admission owns that
        # proof; this writer still proves regular/non-reparse identity and bytes.
        return os.name == "nt" or stat.S_IMODE(metadata.st_mode) == 0o600

    def path_still_names_opened_file() -> bool:
        if opened_identity is None:
            return False
        try:
            current_parent = os.stat(parent, follow_symlinks=False)
            if os.name == "nt":
                current_file = os.stat(output, follow_symlinks=False)
            else:
                current_file = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
        except OSError:
            return False
        return (
            same_identity(current_parent, parent_identity)
            and same_identity(current_file, opened_identity)
            and is_plain_file(current_file)
        )

    def final_path_matches(verified: os.stat_result) -> bool:
        if opened_identity is None:
            return False
        try:
            current_parent = os.stat(parent, follow_symlinks=False)
            current_file = os.stat(output, follow_symlinks=False)
        except OSError:
            return False
        return (
            same_identity(current_parent, parent_identity)
            and same_identity(current_file, opened_identity)
            and is_plain_file(current_file)
            and mode_is_admitted(current_file)
            and current_file.st_nlink == 1
            and current_file.st_size == len(payload)
            and (
                os.name == "nt"
                or (
                    current_file.st_ctime_ns == verified.st_ctime_ns
                    and current_file.st_mtime_ns == verified.st_mtime_ns
                )
            )
        )

    def descriptor_matches_payload(candidate: int | None) -> bool:
        if candidate is None or opened_identity is None:
            return False
        before = os.fstat(candidate)
        initial_checks = {
            "identity": same_identity(before, opened_identity),
            "mode": mode_is_admitted(before),
            "nlink": before.st_nlink == 1,
            "plain_file": is_plain_file(before),
            "size": before.st_size == len(payload),
        }
        initial_failures = [name for name, passed in initial_checks.items() if not passed]
        if initial_failures:
            if os.name == "nt":
                raise EngineerSessionError(
                    "OUTPUT_CONTENT_CHANGED",
                    "Windows output descriptor metadata differs: "
                    + ",".join(initial_failures),
                )
            return False
        os.lseek(candidate, 0, os.SEEK_SET)
        readback = bytearray()
        while len(readback) < len(payload):
            chunk = os.read(
                candidate,
                min(1_048_576, len(payload) - len(readback)),
            )
            if not chunk:
                break
            readback.extend(chunk)
        after = os.fstat(candidate)
        final_checks = {
            "bytes": bytes(readback) == payload,
            "hash": hashlib.sha256(readback).digest() == payload_sha256,
            "identity": same_identity(after, opened_identity),
            "mode": mode_is_admitted(after),
            "nlink": after.st_nlink == 1,
            "plain_file": is_plain_file(after),
            "size": after.st_size == len(payload),
            "timestamps": (
                os.name == "nt"
                or (
                    before.st_ctime_ns == after.st_ctime_ns
                    and before.st_mtime_ns == after.st_mtime_ns
                )
            ),
        }
        final_failures = [name for name, passed in final_checks.items() if not passed]
        if final_failures and os.name == "nt":
            raise EngineerSessionError(
                "OUTPUT_CONTENT_CHANGED",
                "Windows output descriptor readback differs: "
                + ",".join(final_failures),
            )
        return not final_failures

    try:
        try:
            if os.name == "nt":
                descriptor = os.open(output, flags, 0o600)
            else:
                descriptor = os.open(
                    name,
                    flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
        except OSError as exc:
            raise EngineerSessionError(
                "OUTPUT_CREATE_FAILED", f"cannot create output exclusively: {exc}"
            ) from exc
        opened_identity = os.fstat(descriptor)
        if not is_plain_file(opened_identity):
            _fail("OUTPUT_CREATE_FAILED", "new output is not a regular file")
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write while persisting engineer session")
            remaining = remaining[written:]
        os.fsync(descriptor)
        if not path_still_names_opened_file():
            _fail("OUTPUT_PATH_CHANGED", "output pathname changed during write")
        if parent_descriptor is not None:
            os.fsync(parent_descriptor)
            if not path_still_names_opened_file():
                _fail("OUTPUT_PATH_CHANGED", "output pathname changed before commit")
        if not descriptor_matches_payload(descriptor):
            _fail("OUTPUT_CONTENT_CHANGED", "output content changed before commit")
        if not path_still_names_opened_file():
            _fail("OUTPUT_PATH_CHANGED", "output pathname changed during verification")
        if os.name == "nt":
            # Keep the exact CreateNew object open across the writer close.  A
            # duplicated Windows handle inherits the original no-delete sharing
            # contract, so there is no pathname-replacement gap before readback.
            verification_descriptor = os.dup(descriptor)
        closing_descriptor = descriptor
        descriptor = None
        os.close(closing_descriptor)

        if os.name != "nt":
            verification_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                verification_descriptor = os.open(
                    name,
                    verification_flags,
                    dir_fd=parent_descriptor,
                )
            except OSError as exc:
                raise EngineerSessionError(
                    "OUTPUT_PATH_CHANGED",
                    f"cannot reopen committed output safely: {exc}",
                ) from exc
        if not path_still_names_opened_file():
            _fail("OUTPUT_PATH_CHANGED", "output pathname changed after writer close")
        if not descriptor_matches_payload(verification_descriptor):
            _fail("OUTPUT_CONTENT_CHANGED", "reopened output does not match payload")
        if not path_still_names_opened_file():
            _fail("OUTPUT_PATH_CHANGED", "output pathname changed after final readback")
        verified_metadata = os.fstat(verification_descriptor)
        closing_descriptor = verification_descriptor
        verification_descriptor = None
        os.close(closing_descriptor)
        if parent_descriptor is not None:
            closing_descriptor = parent_descriptor
            parent_descriptor = None
            os.close(closing_descriptor)
        if not final_path_matches(verified_metadata):
            _fail("OUTPUT_PATH_CHANGED", "output pathname changed during descriptor close")
    except BaseException:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
            descriptor = None
        if verification_descriptor is not None:
            with suppress(OSError):
                os.close(verification_descriptor)
            verification_descriptor = None
        raise
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if verification_descriptor is not None:
            with suppress(OSError):
                os.close(verification_descriptor)
        if parent_descriptor is not None:
            with suppress(OSError):
                os.close(parent_descriptor)


__all__ = [
    "DEFAULT_ADVISOR_LEASE_DURATION_US",
    "ENGINEER_SESSION_CONTRACT_VERSION",
    "EngineerSessionError",
    "build_engineer_session",
    "build_engineer_session_from_collector_snapshot",
    "canonical_sha256",
    "validate_engineer_session",
    "write_engineer_session_exclusive",
]

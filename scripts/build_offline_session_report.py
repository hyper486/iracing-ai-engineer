# ruff: noqa: E501
"""Build a deterministic canonical post-session report artifact.

This package-external builder accepts four content-addressed receipts: a complete
driving replay, its derived corner cards, a complete fuel replay, and its derived
pit plan.  It validates the two complete replay contracts with the frozen sibling
builders, closes the derived-receipt lineage, executes every report projection in
stdlib SQLite, and emits only the canonical Data Analytics ``artifact.json``.

The artifact is advisor-only.  Historical fuel burn is recording evidence, the
pit plan remains DEVELOPMENT_SMOKE, and all driving findings remain SHADOW_ONLY.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import sqlite3
import stat
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, NoReturn

SESSION_REPORT_CONTRACT_VERSION = "offline-session-report-artifact-v1"
DRIVING_REPLAY_CONTRACT_VERSION = "driving-model-replay-v1"
FUEL_REPLAY_CONTRACT_VERSION = "fuel-model-replay-v2"
CORNER_CARDS_CONTRACT_VERSION = "offline-corner-cards-v1"
PIT_PLAN_CONTRACT_VERSION = "offline-pit-plan-v1"

MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_DATASETS = 8
MAX_SNAPSHOT_ROWS = 128

_SHA256_CHARS = frozenset("0123456789abcdef")
_SAFE_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}\Z")
_SAFE_SQL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_TOP_LEVEL_KEYS = frozenset({"manifest", "package_info", "snapshot", "sources", "surface"})
_CORNER_GATES = {
    "condition_data": "WAIT_CONDITION_DATA",
    "corner_identity": "CANDIDATE_NOT_GOLDEN",
    "human_labels": "WAIT_HUMAN_LABELS",
    "source_driving_replay": "PASS",
}
_PIT_CAPABILITY_KEYS = frozenset(
    {
        "current_tire_wear",
        "event_rules",
        "fuel_feasibility",
        "opponent_fuel",
        "race_recommendation",
        "rejoin_prediction",
        "traffic_model",
    }
)
_SOURCE_DATASET = {
    "session_scope_sql": "session_scope",
    "fuel_burn_sql": "fuel_burn",
    "corner_losses_sql": "corner_losses",
    "corner_per_lap_sql": "corner_per_lap",
    "capability_gates_sql": "capability_gates",
}
_SOURCE_ROLES = {
    "session_scope_sql": ("driving_replay", "fuel_replay"),
    "fuel_burn_sql": ("fuel_replay",),
    "corner_losses_sql": ("driving_replay", "corner_cards"),
    "corner_per_lap_sql": ("driving_replay", "corner_cards"),
    "capability_gates_sql": (
        "driving_replay",
        "corner_cards",
        "fuel_replay",
        "pit_plan",
    ),
}
_SNAPSHOT_DATASETS = frozenset(_SOURCE_DATASET.values())
_REQUIRED_GATE_STATUSES = frozenset(
    {
        "WAIT_CONDITION_DATA",
        "WAIT_HUMAN_LABELS",
        "CANDIDATE_NOT_GOLDEN",
        "SKIP",
        "BLOCKED",
        "DEVELOPMENT_SMOKE",
        "PASS_DEVELOPMENT_SMOKE",
    }
)
_FORBIDDEN_VISIBLE_PIT_ACTION_TOKENS = (
    "+4",
    "20.986486",
    "change_tires",
    "development_smoke_selected",
    "fuel_add_l",
    "fuel_mode",
    "fuel_service_time_s",
    "no_tires",
    "selection_state",
    "service_alternative",
    "stationary_service_time_s",
    "tire_service",
    "total_pit_loss_s",
)


class SessionReportError(ValueError):
    """Raised when inputs cannot support one honest bounded report."""


@dataclass(frozen=True)
class LoadedReceipt:
    value: dict[str, Any]
    file_name: str
    serialized_sha256: str


def _canonical_json(value: object, *, newline: bool = False) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise SessionReportError("value is not canonical-JSON-safe") from exc
    return payload + (b"\n" if newline else b"")


def canonical_sha256(value: object) -> str:
    """Return the SHA-256 of canonical JSON without a trailing newline."""

    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _mapping(value: object, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise SessionReportError(f"{name} must be a plain object")
    return value


def _array(value: object, name: str) -> list[Any]:
    if type(value) is not list:
        raise SessionReportError(f"{name} must be an array")
    return value


def _text(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 32 for character in value)
    ):
        raise SessionReportError(f"{name} must be a bounded non-empty string")
    return value


def _sha256(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _SHA256_CHARS for character in value)
    ):
        raise SessionReportError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise SessionReportError(f"{name} must be a plain integer >= {minimum}")
    return value


def _number(value: object, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SessionReportError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise SessionReportError(f"{name} is outside its finite range")
    return result


def _reject_constant(value: str) -> NoReturn:
    raise SessionReportError(f"non-finite JSON constant is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SessionReportError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _read_receipt(path: Path, *, expected_sha256: str, name: str) -> LoadedReceipt:
    expected = _sha256(expected_sha256, f"expected {name} file SHA-256")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SessionReportError(f"cannot open {name} safely: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SessionReportError(f"{name} must be a regular file")
        if before.st_size <= 0 or before.st_size > MAX_INPUT_BYTES:
            raise SessionReportError(f"{name} size is outside the bounded input range")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise SessionReportError(f"{name} changed or ended while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise SessionReportError(f"{name} grew while being read")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise SessionReportError(f"{name} changed while being read")
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise SessionReportError(f"{name} serialized SHA-256 mismatch")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionReportError(f"{name} is not strict UTF-8 JSON") from exc
    file_name = path.name
    if not _SAFE_FILE_NAME.fullmatch(file_name):
        raise SessionReportError(f"{name} basename is not safe for logical provenance")
    return LoadedReceipt(_mapping(value, name), file_name, actual)


_SIBLING_MODULES: dict[str, ModuleType] = {}


def _load_sibling(file_name: str, module_name: str) -> ModuleType:
    cached = _SIBLING_MODULES.get(module_name)
    if cached is not None:
        return cached
    path = Path(__file__).with_name(file_name)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SessionReportError(f"cannot load frozen validator {file_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    _SIBLING_MODULES[module_name] = module
    return module


def _validate_full_driving(value: object, *, expected_sha256: str) -> dict[str, Any]:
    expected = _sha256(expected_sha256, "expected driving replay SHA-256")
    module = _load_sibling(
        "build_offline_corner_cards.py", "_aeis_frozen_offline_corner_cards"
    )
    try:
        validated = module.validate_driving_replay(value)
    except module.CornerCardError as exc:
        raise SessionReportError(f"driving replay refused: {exc}") from exc
    if validated.get("replay_sha256") != expected:
        raise SessionReportError("driving replay does not match the independent expected digest")
    return _mapping(validated, "validated driving replay")


def _validate_full_fuel(value: object, *, expected_sha256: str) -> dict[str, Any]:
    expected = _sha256(expected_sha256, "expected fuel replay SHA-256")
    module = _load_sibling("build_offline_pit_plan.py", "_aeis_frozen_offline_pit_plan")
    try:
        validated = module._validate_fuel_replay(  # noqa: SLF001 - frozen contract validator
            value,
            expected_fuel_replay_sha256=expected,
        )
    except module.PitPlanError as exc:
        raise SessionReportError(f"fuel replay refused: {exc}") from exc
    return _mapping(validated, "validated fuel replay")


def _validate_corner_cards(value: object, driving_replay: object) -> dict[str, Any]:
    report = _mapping(value, "corner cards")
    module = _load_sibling(
        "build_offline_corner_cards.py", "_aeis_frozen_offline_corner_cards"
    )
    try:
        expected = module.build_corner_cards(driving_replay, top=3)
    except module.CornerCardError as exc:
        raise SessionReportError(f"corner card reconstruction refused: {exc}") from exc
    if report != expected:
        raise SessionReportError("corner cards do not equal the deterministic Top-3 reconstruction")
    if (
        report.get("contract_version") != CORNER_CARDS_CONTRACT_VERSION
        or report.get("advisor_only") is not True
        or report.get("status") != "SHADOW_ONLY"
        or report.get("execution_status") != "COMPLETE"
        or report.get("gates") != _CORNER_GATES
    ):
        raise SessionReportError("corner cards safety boundary or WAIT gates are invalid")
    cards = _array(report.get("cards"), "corner cards.cards")
    if len(cards) != 3:
        raise SessionReportError("corner cards must retain the complete Top-3")
    for index, raw in enumerate(cards, start=1):
        card = _mapping(raw, f"corner card {index}")
        if (
            card.get("rank") != index
            or card.get("status") != "SHADOW_ONLY"
            or card.get("executable") is not False
        ):
            raise SessionReportError(f"corner card {index} is executable or promoted")
    return {
        "cards": cards,
        "input_receipt": _mapping(report.get("input_receipt"), "corner input_receipt"),
        "reference": _mapping(report.get("reference"), "corner reference"),
        "report": report,
    }


def _validate_pit_plan(
    value: object,
    fuel_replay: Mapping[str, Any],
    *,
    expected_fuel_replay_sha256: str,
) -> dict[str, Any]:
    plan = _mapping(value, "pit plan")
    module = _load_sibling("build_offline_pit_plan.py", "_aeis_frozen_offline_pit_plan")
    current_input = module._current_input_binding(  # noqa: SLF001 - frozen lineage builder
        fuel_replay,
        expected_fuel_replay_sha256=expected_fuel_replay_sha256,
    )
    try:
        validated = module._validate_previous_plan(  # noqa: SLF001 - frozen receipt validator
            plan,
            current_input_binding=current_input,
        )
    except module.PitPlanError as exc:
        raise SessionReportError(f"pit plan refused: {exc}") from exc
    if plan.get("input_binding") != current_input:
        raise SessionReportError("pit plan input binding does not exactly match the full fuel replay")
    if (
        plan.get("contract_version") != PIT_PLAN_CONTRACT_VERSION
        or plan.get("advisor_only") is not True
        or plan.get("attestation_status") != "NOT_R7_ATTESTED"
        or plan.get("derivation_status") != "POST_ADMISSION_DERIVED"
        or plan.get("execution_mode") != "SHADOW"
        or plan.get("plan_scope") != "FUEL_FEASIBILITY_ONLY"
        or plan.get("quality_gate")
        != {
            "reasons": ["NON_OFFICIAL_DEVELOPMENT_SMOKE_ONLY"],
            "status": "PASS_DEVELOPMENT_SMOKE",
        }
    ):
        raise SessionReportError("pit plan is not an explicit development-smoke shadow receipt")
    rules = _mapping(plan.get("rules_binding"), "pit rules_binding")
    if (
        rules.get("official_event_rules") is not False
        or rules.get("profile_status") != "DEVELOPMENT_SMOKE"
    ):
        raise SessionReportError("pit rules must remain non-official DEVELOPMENT_SMOKE")
    capabilities = _mapping(plan.get("capabilities"), "pit capabilities")
    if set(capabilities) != _PIT_CAPABILITY_KEYS:
        raise SessionReportError("pit capability set is incomplete")
    expected_statuses = {
        "current_tire_wear": "SKIP",
        "event_rules": "DEVELOPMENT_SMOKE",
        "fuel_feasibility": "PASS_DEVELOPMENT_SMOKE",
        "opponent_fuel": "SKIP",
        "race_recommendation": "BLOCKED",
        "rejoin_prediction": "SKIP",
        "traffic_model": "SKIP",
    }
    for capability, expected_status in expected_statuses.items():
        item = _mapping(capabilities.get(capability), f"pit capability {capability}")
        if item.get("status") != expected_status:
            raise SessionReportError(f"pit capability {capability} lost {expected_status}")
        if expected_status == "SKIP" and item.get("estimate_available") is not False:
            raise SessionReportError(f"pit capability {capability} fabricated an estimate")
    recommendations = _array(plan.get("recommendations"), "pit recommendations")
    if len(recommendations) != 1:
        raise SessionReportError("development-smoke pit plan must retain one shadow candidate")
    recommendation = _mapping(recommendations[0], "pit recommendation")
    if (
        recommendation.get("status") != "SHADOW_ONLY"
        or recommendation.get("executable") is not False
        or recommendation.get("expected_gain_range_s") is not None
        or recommendation.get("claim_scope") != "FUEL_FEASIBILITY_ONLY"
    ):
        raise SessionReportError("pit recommendation is executable or overclaims its scope")
    recommendation_id = _text(
        recommendation.get("recommendation_id"), "pit recommendation_id"
    )
    lifecycle = _array(plan.get("lifecycle_events"), "pit lifecycle_events")
    if lifecycle != [{"event": "ISSUE", "recommendation_id": recommendation_id}]:
        raise SessionReportError("pit lifecycle must exactly issue the active shadow candidate")
    return {
        "alternatives": _array(plan.get("service_alternatives"), "service alternatives"),
        "capabilities": capabilities,
        "plan": validated,
        "recommendation": recommendation,
    }


def _validate_cross_lineage(
    driving: Mapping[str, Any],
    corners: Mapping[str, Any],
    fuel: Mapping[str, Any],
    pit: Mapping[str, Any],
) -> None:
    driving_replay = _mapping(driving.get("replay"), "validated driving replay payload")
    corner_input = _mapping(corners.get("input_receipt"), "validated corner input")
    pit_input = _mapping(pit["plan"].get("input_binding"), "validated pit input")
    shared_pairs = {
        "input kind": (driving_replay.get("input_kind"), fuel.get("input_kind")),
        "input evidence": (driving_replay.get("input_evidence"), fuel.get("input_evidence")),
        "normalized receipt": (
            driving_replay.get("normalized_input_receipt"),
            fuel.get("normalized_input_receipt"),
        ),
        "event receipt": (driving_replay.get("event_receipt"), fuel.get("event_receipt")),
    }
    corner_pairs = {
        "corner driving replay": (
            corner_input.get("driving_replay_sha256"),
            driving.get("replay_sha256"),
        ),
        "corner input provenance": (
            corner_input.get("input_provenance_sha256"),
            driving.get("input_provenance_sha256"),
        ),
        "corner model output": (
            corner_input.get("model_output_sha256"),
            driving_replay.get("model_output_sha256"),
        ),
        "corner model semantic": (
            corner_input.get("model_semantic_sha256"),
            driving_replay.get("model_semantic_sha256"),
        ),
    }
    fuel_evidence = _mapping(fuel.get("input_evidence"), "validated fuel evidence")
    fuel_normalized = _mapping(
        fuel.get("normalized_input_receipt"), "validated fuel normalized receipt"
    )
    fuel_event = _mapping(fuel.get("event_receipt"), "validated fuel event receipt")
    pit_pairs = {
        "pit replay": (pit_input.get("fuel_replay_sha256"), fuel.get("fuel_replay_sha256")),
        "pit expected replay": (
            pit_input.get("expected_fuel_replay_sha256"),
            fuel.get("fuel_replay_sha256"),
        ),
        "pit evidence": (
            pit_input.get("input_evidence_sha256"),
            canonical_sha256(fuel_evidence),
        ),
        "pit normalized SHA": (
            pit_input.get("normalized_samples_sha256"),
            fuel_normalized.get("samples_sha256"),
        ),
        "pit normalized count": (
            pit_input.get("normalized_sample_count"),
            fuel_normalized.get("sample_count"),
        ),
        "pit event receipt": (
            pit_input.get("event_receipt_sha256"),
            fuel_event.get("receipt_sha256"),
        ),
        "pit model output": (
            pit_input.get("model_output_sha256"),
            fuel.get("model_output_sha256"),
        ),
        "pit model semantic": (
            pit_input.get("model_semantic_sha256"),
            fuel.get("model_semantic_sha256"),
        ),
        "pit scenario": (pit_input.get("scenario_sha256"), fuel.get("scenario_sha256")),
        "pit source": (pit_input.get("source_id"), fuel_evidence.get("source_id")),
        "pit session": (pit_input.get("session_id"), fuel_evidence.get("session_id")),
        "pit source kind": (pit_input.get("source_kind"), fuel_evidence.get("source_kind")),
    }
    mismatches = [
        name
        for name, (left, right) in {**shared_pairs, **corner_pairs, **pit_pairs}.items()
        if left != right
    ]
    if mismatches:
        raise SessionReportError(f"cross-receipt lineage mismatch: {', '.join(sorted(mismatches))}")


def _safe_logical_path(value: object, name: str) -> str:
    path = _text(value, name)
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or "." in pure.parts
        or "\\" in path
        or path.startswith("~")
        or len(pure.parts) < 2
    ):
        raise SessionReportError(f"{name} must be a safe repo-relative logical path")
    return path


def _logical_receipt_path(role: str, receipt: LoadedReceipt) -> str:
    return _safe_logical_path(
        f"logical-receipts/{role}/{receipt.serialized_sha256}.json",
        f"{role} logical path",
    )


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if type(value) is str:
        return "'" + value.replace("'", "''") + "'"
    if type(value) is int:
        return str(value)
    if type(value) is float and math.isfinite(value):
        return repr(value)
    raise SessionReportError("SQLite projection rows must contain only plain scalar values")


def _projection_sql(rows: Sequence[Mapping[str, object]]) -> tuple[str, tuple[str, ...]]:
    if not rows:
        raise SessionReportError("SQLite projection cannot be empty")
    columns = tuple(rows[0])
    if not columns or any(not _SAFE_SQL_IDENTIFIER.fullmatch(column) for column in columns):
        raise SessionReportError("SQLite projection has an unsafe column name")
    if any(tuple(row) != columns for row in rows):
        raise SessionReportError("SQLite projection rows do not share one ordered schema")
    quoted = ", ".join(f'"{column}"' for column in columns)
    values = ",\n    ".join(
        "(" + ", ".join(_sql_literal(row[column]) for column in columns) + ")" for row in rows
    )
    sql = (
        f"WITH reviewed({quoted}) AS (\n"
        f"  VALUES\n    {values}\n"
        f")\nSELECT {quoted} FROM reviewed ORDER BY \"row_order\" ASC"
    )
    return sql, columns


def _execute_projection(sql: str) -> list[dict[str, object]]:
    connection = sqlite3.connect(":memory:")
    try:
        cursor = connection.execute(sql)
        columns = [description[0] for description in cursor.description or ()]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    except sqlite3.Error as exc:
        raise SessionReportError(f"SQLite source query failed: {exc}") from exc
    finally:
        connection.close()


def _receipt_canonical(receipt: LoadedReceipt, field: str) -> str:
    return _sha256(receipt.value.get(field), f"{receipt.file_name}.{field}")


def _build_source(
    *,
    source_id: str,
    dataset: str,
    rows: list[dict[str, object]],
    receipts: Sequence[tuple[str, LoadedReceipt, str]],
    description: str,
    filters: Sequence[str],
    metric_definitions: Sequence[str],
) -> dict[str, object]:
    if _SOURCE_DATASET.get(source_id) != dataset:
        raise SessionReportError("source/dataset mapping is not canonical")
    sql, _ = _projection_sql(rows)
    projected = _execute_projection(sql)
    if _canonical_json(projected) != _canonical_json(rows):
        raise SessionReportError(f"SQLite projection differs from snapshot dataset {dataset}")
    logical_paths = [_logical_receipt_path(role, receipt) for role, receipt, _ in receipts]
    receipt_filters = []
    for role, receipt, canonical_field in receipts:
        canonical_digest = _receipt_canonical(receipt, canonical_field)
        receipt_filters.extend(
            (
                f"{role} serialized_sha256 = {receipt.serialized_sha256}",
                f"{role} {canonical_field} = {canonical_digest}",
            )
        )
    query_digest = canonical_sha256({"dataset": dataset, "rows": rows, "sql": sql})
    return {
        "id": source_id,
        "label": f"Non-resolvable content-addressed audit binding — {dataset}",
        "path": logical_paths[0],
        "query": {
            "description": (
                f"{description} This non-resolvable logical source does not query receipt "
                "files: the runnable SQLite SQL only replays already-audited rows embedded "
                "in this bounded snapshot through an inline VALUES CTE."
            ),
            "engine": "sqlite",
            "filters": [*receipt_filters, *filters, f"snapshot row count = {len(rows)}"],
            "id": f"{source_id}:{query_digest}",
            "language": "sql",
            "metric_definitions": list(metric_definitions),
            "sql": sql,
            "tables_used": ["reviewed"],
        },
    }


def _reason_text(value: object) -> str:
    if type(value) is not list:
        return "none recorded"
    reasons = [str(item) for item in value]
    return ", ".join(reasons) if reasons else "none recorded"


def _candidate_label(corner_id: object) -> str:
    identifier = _text(corner_id, "corner_id")
    return f"Candidate window {identifier} (not authenticated name)"


def _lap_ordinals(ids: Sequence[object], ordinal_by_id: Mapping[str, int]) -> str:
    ordinals: list[int] = []
    for value in ids:
        identifier = _text(value, "evidence lap id")
        if identifier not in ordinal_by_id:
            raise SessionReportError("corner evidence references an unknown comparison lap")
        ordinals.append(ordinal_by_id[identifier])
    return ", ".join(str(ordinal) for ordinal in ordinals) if ordinals else "none"


def _corner_datasets(corners: Mapping[str, Any]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    cards = _array(corners.get("cards"), "validated corner cards")
    summary_rows: list[dict[str, object]] = []
    per_lap_rows: list[dict[str, object]] = []
    row_order = 0
    for card_raw in cards:
        card = _mapping(card_raw, "validated corner card")
        rank = _integer(card.get("rank"), "corner rank", minimum=1)
        label = _candidate_label(card.get("corner_id"))
        per_lap = _array(card.get("per_lap_evidence"), "corner per-lap evidence")
        ordinal_by_id: dict[str, int] = {}
        for raw in per_lap:
            lap = _mapping(raw, "corner per-lap row")
            lap_id = _text(lap.get("lap_id"), "corner lap_id")
            ordinal_by_id[lap_id] = _integer(lap.get("lap_ordinal"), "corner lap ordinal", minimum=1)
        loss_support = [_text(item, "loss evidence id") for item in _array(card.get("loss_evidence_lap_ids"), "loss evidence")]
        loss_counter = [_text(item, "loss counterexample id") for item in _array(card.get("loss_counterexample_lap_ids"), "loss counterexamples")]
        action_support = [_text(item, "action evidence id") for item in _array(card.get("action_evidence_lap_ids"), "action evidence")]
        action_counter = [_text(item, "action counterexample id") for item in _array(card.get("action_counterexample_lap_ids"), "action counterexamples")]
        lap_ids = set(ordinal_by_id)
        if set(loss_support).isdisjoint(loss_counter) is False or set(loss_support) | set(loss_counter) != lap_ids:
            raise SessionReportError("loss evidence/counterexample partition is incomplete")
        action = card.get("action")
        if action is None:
            if action_support or action_counter or card.get("diagnosis") is not None:
                raise SessionReportError("no-action card carries action evidence or diagnosis")
            action_status = "NO_SUPPORTED_ACTION"
            action_text = "No supported action"
            diagnosis = "No supported diagnosis"
        else:
            action_text = _text(action, "corner action")
            diagnosis = _text(card.get("diagnosis"), "corner diagnosis")
            if set(action_support).isdisjoint(action_counter) is False or set(action_support) | set(action_counter) != lap_ids:
                raise SessionReportError("action evidence/counterexample partition is incomplete")
            action_status = "PRACTICE_HYPOTHESIS_ONLY"
        summary = _mapping(card.get("loss_summary"), "corner loss_summary")
        comparison_n = _integer(summary.get("comparison_lap_count"), "comparison_lap_count", minimum=1)
        support_n = _integer(summary.get("supporting_lap_count"), "supporting_lap_count")
        if comparison_n != len(per_lap) or support_n != len(loss_support):
            raise SessionReportError("corner support counts do not close")
        summary_rows.append(
            {
                "row_order": rank,
                "rank": rank,
                "candidate_window": label,
                "median_accounted_loss_s": _number(
                    summary.get("median_accounted_window_delta_s"), "median accounted loss"
                ),
                "loss_support_n": support_n,
                "comparison_n": comparison_n,
                "loss_support_fraction": f"{support_n}/{comparison_n}",
                "loss_counterexample_n": len(loss_counter),
                "loss_counterexample_laps": _lap_ordinals(loss_counter, ordinal_by_id),
                "action_status": action_status,
                "action_evidence_n": len(action_support),
                "action_evidence_laps": _lap_ordinals(action_support, ordinal_by_id),
                "action_counterexample_n": len(action_counter),
                "action_counterexample_laps": _lap_ordinals(action_counter, ordinal_by_id),
                "diagnosis": diagnosis,
                "practice_action": action_text,
                "confidence": _text(card.get("confidence"), "corner confidence"),
                "claim_status": _text(card.get("status"), "corner status"),
            }
        )
        for raw in per_lap:
            lap = _mapping(raw, "corner per-lap row")
            row_order += 1
            lap_id = _text(lap.get("lap_id"), "corner lap_id")
            loss_role = "SUPPORT" if lap_id in loss_support else "COUNTEREXAMPLE"
            if action is None:
                action_role = "NOT_EVALUATED_NO_ACTION"
            else:
                action_role = "SUPPORT" if lap_id in action_support else "COUNTEREXAMPLE"
            if lap.get("phase_closed") is not True:
                raise SessionReportError("per-lap phase partition is not closed")
            lap_ordinal = _integer(lap.get("lap_ordinal"), "lap ordinal", minimum=1)
            accounted_delta = _number(
                lap.get("accounted_window_delta_s"), "accounted window delta"
            )
            per_lap_rows.append(
                {
                    "row_order": row_order,
                    "corner_rank": rank,
                    "candidate_window": label,
                    "point_label": f"{card['corner_id']} · lap {lap_ordinal}",
                    "lap_ordinal": lap_ordinal,
                    "evidence_lap_id": lap_id,
                    "accounted_window_delta_s": accounted_delta,
                    "delta_sign": "POSITIVE_LOSS" if accounted_delta > 0 else "NEGATIVE_COUNTEREXAMPLE",
                    "approach_delta_s": _number(lap.get("approach_delta_s"), "approach delta"),
                    "local_delta_s": _number(lap.get("local_delta_s"), "local delta"),
                    "carry_delta_s": _number(lap.get("carry_delta_s"), "carry delta"),
                    "loss_role": loss_role,
                    "action_role": action_role,
                    "phase_status": "CLOSED",
                    "phase_residual_s": _number(lap.get("phase_residual_s"), "phase residual"),
                }
            )
    if len(summary_rows) != 3 or len(per_lap_rows) != 18:
        raise SessionReportError("report requires 3 corner summaries and all 18 per-lap rows")
    if [row["candidate_window"] for row in summary_rows] != [
        "Candidate window C08 (not authenticated name)",
        "Candidate window C02 (not authenticated name)",
        "Candidate window C01 (not authenticated name)",
    ]:
        raise SessionReportError("corner ranking or fixed candidate labels changed")
    if [row["action_status"] for row in summary_rows[:2]] != [
        "NO_SUPPORTED_ACTION",
        "NO_SUPPORTED_ACTION",
    ]:
        raise SessionReportError("C08/C02 must remain no-action findings")
    return summary_rows, per_lap_rows


def _capability_rows(
    driving_replay: Mapping[str, Any],
    corners: Mapping[str, Any],
    fuel: Mapping[str, Any],
    pit: Mapping[str, Any],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []

    def add(layer: str, capability: str, value: object) -> None:
        item = _mapping(value, f"{layer}.{capability}") if type(value) is dict else None
        status = _text(item.get("status"), f"{layer}.{capability}.status") if item else _text(value, f"{layer}.{capability}")
        estimate = "NO" if item and item.get("estimate_available") is False else "N/A"
        reasons = _reason_text(item.get("reasons")) if item else "upstream fixed gate"
        rows.append(
            {
                "row_order": len(rows) + 1,
                "layer": layer,
                "capability": capability,
                "status": status,
                "estimate_available": estimate,
                "reasons": reasons,
            }
        )

    for capability, status in _CORNER_GATES.items():
        add("corner_cards", capability, status)
    for capability, value in _mapping(
        driving_replay.get("capabilities"), "driving capabilities"
    ).items():
        add("driving_replay", capability, value)
    for capability, value in _mapping(fuel.get("capabilities"), "fuel capabilities").items():
        add("fuel_replay", capability, value)
    for capability, value in _mapping(pit.get("capabilities"), "pit capabilities").items():
        add("pit_plan", capability, value)
    statuses = {str(row["status"]) for row in rows}
    if not _REQUIRED_GATE_STATUSES.issubset(statuses):
        missing = sorted(_REQUIRED_GATE_STATUSES - statuses)
        raise SessionReportError(f"capability rows lost required fail-closed states: {missing}")
    return rows


def _artifact_without_hash(artifact: Mapping[str, Any]) -> dict[str, Any]:
    package = _mapping(artifact.get("package_info"), "package_info")
    package_without_hash = {key: value for key, value in package.items() if key != "artifact_sha256"}
    return {**artifact, "package_info": package_without_hash}


def _bind_artifact_hash(base: dict[str, object]) -> dict[str, object]:
    package = _mapping(base.get("package_info"), "package_info")
    if "artifact_sha256" in package:
        raise SessionReportError("artifact hash must be absent before binding")
    digest = canonical_sha256(base)
    return {**base, "package_info": {**package, "artifact_sha256": digest}}


def _build_artifact_from_validated(
    driving_receipt: LoadedReceipt,
    corner_receipt: LoadedReceipt,
    fuel_receipt: LoadedReceipt,
    pit_receipt: LoadedReceipt,
    *,
    driving: Mapping[str, Any],
    corners: Mapping[str, Any],
    fuel: Mapping[str, Any],
    pit: Mapping[str, Any],
) -> dict[str, object]:
    driving_replay = _mapping(driving.get("replay"), "validated driving replay")
    evidence = _mapping(driving_replay.get("input_evidence"), "driving input_evidence")
    normalized = _mapping(
        driving_replay.get("normalized_input_receipt"), "driving normalized receipt"
    )
    event = _mapping(driving_replay.get("event_receipt"), "driving event receipt")
    context = _mapping(driving_replay.get("driving_context"), "driving context")
    driving_model = _mapping(driving_replay.get("model_output"), "driving model_output")
    driving_reference = _mapping(driving_model.get("reference"), "driving reference")
    reference_lap = _integer(
        driving_reference.get("lap_ordinal"), "driving reference lap", minimum=1
    )
    eligible_laps = [
        _integer(value, "eligible lap ordinal", minimum=1)
        for value in _array(driving_model.get("eligible_lap_ordinals"), "eligible laps")
    ]
    grid_step_m = _number(driving_model.get("grid_step_m"), "grid step", minimum=0.0)
    if grid_step_m <= 0.0 or reference_lap not in eligible_laps:
        raise SessionReportError("driving reference/grid evidence is invalid")
    source_name = _text(evidence.get("source_id"), "source_id")
    session_name = _text(evidence.get("session_id"), "session_id")

    model = _mapping(fuel.get("model_output"), "fuel model_output")
    burn = _mapping(model.get("burn"), "fuel burn")
    admitted_laps = _integer(burn.get("accepted_laps"), "accepted fuel laps", minimum=1)
    rejected_laps = _integer(burn.get("rejected_laps"), "rejected fuel laps")
    mean_burn = _number(burn.get("mean_l_per_lap"), "mean fuel burn", minimum=0.0)
    conservative_burn = _number(
        burn.get("conservative_l_per_lap"), "conservative fuel burn", minimum=0.0
    )
    quantile = _number(burn.get("conservative_quantile"), "conservative quantile", minimum=0.0)
    if quantile > 1.0:
        raise SessionReportError("conservative fuel quantile must not exceed 1")
    percentile_label = f"P{round(quantile * 100):d}"

    scope_rows = [
        {
            "row_order": 1,
            "bound_source": source_name,
            "bound_session": session_name,
            "source_kind": _text(evidence.get("source_kind"), "source_kind"),
            "raw_source_sha256": _sha256(evidence.get("source_sha256"), "raw source SHA"),
            "normalized_sample_count": _integer(
                normalized.get("sample_count"), "normalized sample count", minimum=1
            ),
            "normalized_samples_sha256": _sha256(
                normalized.get("samples_sha256"), "normalized samples SHA"
            ),
            "event_count": _integer(event.get("event_count"), "event count"),
            "event_receipt_sha256": _sha256(event.get("receipt_sha256"), "event receipt SHA"),
            "track_length_mm": _integer(
                context.get("track_length_mm"), "track length mm", minimum=1
            ),
            "reference_lap_ordinal": reference_lap,
            "clean_lap_count": len(eligible_laps),
            "comparison_lap_count": len(eligible_laps) - 1,
            "grid_step_m": grid_step_m,
        }
    ]
    fuel_rows = [
        {
            "row_order": 1,
            "admitted_laps": admitted_laps,
            "rejected_laps": rejected_laps,
            "mean_l_per_lap": mean_burn,
            "conservative_percentile": percentile_label,
            "conservative_l_per_lap": conservative_burn,
            "conservative_quantile": quantile,
            "confidence": _text(burn.get("confidence"), "fuel burn confidence"),
            "provenance": _text(burn.get("source_label"), "fuel burn provenance"),
            "claim_scope": "RECORDING_ONLY",
        }
    ]
    corner_rows, per_lap_rows = _corner_datasets(corners)
    comparison_counts = {int(row["comparison_n"]) for row in corner_rows}
    if comparison_counts != {len(eligible_laps) - 1}:
        raise SessionReportError("corner comparisons do not match eligible clean laps")
    corner_reference = _integer(
        _mapping(corners.get("reference"), "corner reference").get("reference_lap_ordinal"),
        "corner reference lap",
        minimum=1,
    )
    if corner_reference != reference_lap:
        raise SessionReportError("corner/driving reference lap mismatch")
    gate_rows = _capability_rows(driving_replay, corners, fuel, pit)

    receipts = {
        "driving_replay": (driving_receipt, "driving_replay_sha256"),
        "corner_cards": (corner_receipt, "corner_cards_sha256"),
        "fuel_replay": (fuel_receipt, "fuel_replay_sha256"),
        "pit_plan": (pit_receipt, "pit_plan_sha256"),
    }
    all_receipts = [(role, receipt, field) for role, (receipt, field) in receipts.items()]
    sources = [
        _build_source(
            source_id="session_scope_sql",
            dataset="session_scope",
            rows=scope_rows,
            receipts=[all_receipts[0], all_receipts[2]],
            description="Executes the reviewed one-row source/session/normalized/event lineage projection shared by both complete replay receipts.",
            filters=["driving and fuel input evidence are byte-for-byte equal", "complete IBT_OFFLINE source only"],
            metric_definitions=[
                "normalized_sample_count is the admitted normalized replay row count, not a live refresh.",
                "track_length_mm is the verified driving-context track length in millimetres.",
                "reference_lap_ordinal is a real eligible lap, not a theoretical composite.",
                "comparison_lap_count excludes the reference from the clean eligible cohort.",
                "grid_step_m is the spatial resampling interval used by the driving model.",
            ],
        ),
        _build_source(
            source_id="fuel_burn_sql",
            dataset="fuel_burn",
            rows=fuel_rows,
            receipts=[all_receipts[2]],
            description="Executes the reviewed historical fuel-burn projection from the fully validated fuel replay.",
            filters=["accepted laps only for burn statistics", "recording-only historical evidence", "no live fuel state"],
            metric_definitions=[
                "mean_l_per_lap is arithmetic mean fuel used across admitted laps.",
                f"conservative_l_per_lap is the observed {percentile_label} quantile across admitted laps.",
                "admitted_laps excludes laps rejected by the upstream deterministic fuel model.",
            ],
        ),
        _build_source(
            source_id="corner_losses_sql",
            dataset="corner_losses",
            rows=corner_rows,
            receipts=[all_receipts[0], all_receipts[1]],
            description="Executes the reviewed Top-3 corner summary projection reconstructed from the full driving replay.",
            filters=["rank <= 3", "descriptive SHADOW_ONLY cards", "candidate corner identities are not authenticated"],
            metric_definitions=[
                "median_accounted_loss_s is the median closed approach+local+carry window delta versus the bound reference lap.",
                "loss_support_n/comparison_n includes all supporting and counterexample laps.",
                "action evidence is separate from loss evidence and never interpreted as expected gain.",
            ],
        ),
        _build_source(
            source_id="corner_per_lap_sql",
            dataset="corner_per_lap",
            rows=per_lap_rows,
            receipts=[all_receipts[0], all_receipts[1]],
            description="Executes all 18 reviewed per-lap evidence rows for the three corner cards, including negative counterexamples.",
            filters=["3 candidate windows", "6 comparison laps per window", "phase partition closed"],
            metric_definitions=[
                "accounted_window_delta_s equals approach_delta_s + local_delta_s + carry_delta_s within the recorded residual.",
                "loss_role and action_role preserve support and counterexample partitions independently.",
            ],
        ),
        _build_source(
            source_id="capability_gates_sql",
            dataset="capability_gates",
            rows=gate_rows,
            receipts=all_receipts,
            description="Executes the reviewed capability-gate projection across all four exact-validated receipts.",
            filters=["WAIT/SKIP/BLOCKED states retained", "no unavailable estimate imputation", "advisor-only boundary"],
            metric_definitions=[
                "status is copied without promotion from the source receipt layer.",
                "estimate_available = NO only when the upstream capability explicitly carries false.",
            ],
        ),
    ]

    cards = [
        {
            "id": "admitted_fuel_laps",
            "dataset": "fuel_burn",
            "description": "Historical recording laps admitted by the deterministic burn model.",
            "sourceId": "fuel_burn_sql",
            "metrics": [{"label": "Admitted burn laps", "field": "admitted_laps", "format": "number"}],
        },
        {
            "id": "mean_fuel_burn",
            "dataset": "fuel_burn",
            "description": "Recording-only arithmetic mean; not a live forecast.",
            "sourceId": "fuel_burn_sql",
            "metrics": [{"label": "Mean burn (L/lap)", "field": "mean_l_per_lap", "format": "number"}],
        },
        {
            "id": "conservative_fuel_burn",
            "dataset": "fuel_burn",
            "description": f"Recording-only observed {percentile_label}; not a live forecast.",
            "sourceId": "fuel_burn_sql",
            "metrics": [{"label": f"Conservative {percentile_label} (L/lap)", "field": "conservative_l_per_lap", "format": "number"}],
        },
        {
            "id": "top_corner_loss",
            "dataset": "corner_losses",
            "filter": {"rank": 1},
            "description": "Largest median accounted loss among the three descriptive candidate windows.",
            "sourceId": "corner_losses_sql",
            "metrics": [{"label": "Top median loss (s)", "field": "median_accounted_loss_s", "format": "number"}],
        },
    ]
    chart = {
        "id": "corner_loss_distribution",
        "title": "Per-lap accounted loss by candidate window",
        "subtitle": (
            "All 18 comparison-lap observations; y lanes 1=C08, 2=C02, 3=C01. "
            "Candidate IDs are not authenticated Spa corner names."
        ),
        "type": "scatter",
        "dataset": "corner_per_lap",
        "sourceId": "corner_per_lap_sql",
        "intent": "comparison",
        "question": (
            "Across all six comparison laps, where are candidate-window deltas repeatedly "
            "positive or contradicted by negative observations?"
        ),
        "rationale": (
            "A scatter plot preserves all 18 observations and the zero baseline, so medians "
            "cannot hide counterexamples."
        ),
        "comparisonContext": {
            "baseline": f"reference lap {reference_lap}",
            "denominator": "all six bound comparison laps per candidate window",
            "grain": "one comparison lap × one braking-derived candidate window",
            "unit": "seconds",
        },
        "encodings": {
            "x": {
                "field": "accounted_window_delta_s",
                "type": "quantitative",
                "label": "Accounted-window delta vs lap 11",
                "unit": "s",
                "format": "number",
            },
            "y": {
                "field": "corner_rank",
                "type": "quantitative",
                "label": "Candidate lane (1=C08, 2=C02, 3=C01)",
                "format": "number",
            },
            "color": {"field": "loss_role", "type": "nominal", "label": "Loss evidence role"},
            "tooltip": [
                {"field": "candidate_window", "label": "Candidate window"},
                {"field": "lap_ordinal", "label": "Lap"},
                {"field": "accounted_window_delta_s", "label": "Accounted delta", "unit": "s"},
                {"field": "loss_role", "label": "Loss evidence role"},
            ],
        },
        "xAxisTitle": "Accounted-window delta versus reference lap 11 (s)",
        "yAxisTitle": "Candidate lane: 1=C08 · 2=C02 · 3=C01",
        "valueFormat": "number",
        "layout": "full",
        "referenceLines": [
            {
                "axis": "x",
                "value": 0,
                "label": "Equal to reference lap 11",
                "color": "neutral",
                "lineStyle": "dashed",
            }
        ],
        "legend": {"position": "bottom", "title": "Loss evidence role"},
        "palette": {"kind": "categorical", "name": "blue-neutral"},
    }
    tables = [
        {
            "id": "corner_summary_table",
            "title": "Candidate-window support and counterexamples",
            "subtitle": "Loss and action evidence are separate; no expected-gain claim is shown.",
            "dataset": "corner_losses",
            "sourceId": "corner_losses_sql",
            "layout": "full",
            "density": "spacious",
            "defaultSort": {"field": "rank", "direction": "asc"},
            "columns": [
                {"field": "rank", "label": "Rank", "format": "number"},
                {"field": "candidate_window", "label": "Candidate window", "type": "text"},
                {"field": "median_accounted_loss_s", "label": "Median accounted loss", "format": "number", "unit": "s"},
                {"field": "loss_support_n", "label": "Support n", "format": "number"},
                {"field": "comparison_n", "label": "Comparison N", "format": "number"},
                {"field": "loss_counterexample_laps", "label": "Loss counterexample laps", "type": "text"},
                {"field": "action_status", "label": "Action status", "type": "text"},
                {"field": "action_evidence_n", "label": "Action support n", "format": "number"},
                {"field": "action_counterexample_laps", "label": "Action counterexample laps", "type": "text"},
                {"field": "practice_action", "label": "Practice action", "type": "text"},
                {"field": "confidence", "label": "Overall confidence", "type": "text"},
            ],
        },
        {
            "id": "corner_per_lap_table",
            "title": "All per-lap corner evidence",
            "subtitle": "All 18 rows are retained, including negative counterexamples.",
            "dataset": "corner_per_lap",
            "sourceId": "corner_per_lap_sql",
            "layout": "full",
            "density": "comfortable",
            "defaultSort": {"field": "row_order", "direction": "asc"},
            "columns": [
                {"field": "row_order", "label": "Order", "format": "number"},
                {"field": "candidate_window", "label": "Candidate window", "type": "text"},
                {"field": "lap_ordinal", "label": "Lap", "format": "number"},
                {"field": "accounted_window_delta_s", "label": "Accounted delta", "format": "number", "unit": "s"},
                {"field": "approach_delta_s", "label": "Approach delta", "format": "number", "unit": "s"},
                {"field": "local_delta_s", "label": "Local delta", "format": "number", "unit": "s"},
                {"field": "carry_delta_s", "label": "Carry delta", "format": "number", "unit": "s"},
                {"field": "loss_role", "label": "Loss evidence role", "type": "text"},
                {"field": "action_role", "label": "Action evidence role", "type": "text"},
                {"field": "phase_status", "label": "Phase partition", "type": "text"},
            ],
        },
        {
            "id": "capability_gates_table",
            "title": "Capability gates retained from source receipts",
            "subtitle": "WAIT, SKIP, BLOCKED, and development-only states are not promoted.",
            "dataset": "capability_gates",
            "sourceId": "capability_gates_sql",
            "layout": "full",
            "density": "comfortable",
            "defaultSort": {"field": "row_order", "direction": "asc"},
            "columns": [
                {"field": "row_order", "label": "Order", "format": "number"},
                {"field": "layer", "label": "Receipt layer", "type": "text"},
                {"field": "capability", "label": "Capability", "type": "text"},
                {"field": "status", "label": "Status", "type": "text"},
                {"field": "estimate_available", "label": "Estimate available", "type": "text"},
                {"field": "reasons", "label": "Reasons", "type": "text"},
            ],
        },
    ]

    top = corner_rows[0]
    c01 = corner_rows[2]
    blocks = [
        {"id": "title", "type": "markdown", "body": "# iRacing AI 工程师离线赛后报告"},
        {
            "id": "executive_summary",
            "type": "markdown",
            "body": (
                "## Executive Summary\n\n"
                "**当前边界：`SHADOW_ONLY · LOW · WAIT_CONDITION_DATA / WAIT_HUMAN_LABELS · "
                "race_recommendation=BLOCKED`。**\n\n"
                "- 历史燃油消耗基线可用于回放复盘，但仍不是实时比赛状态。\n"
                f"- C08 为 **{float(corner_rows[0]['median_accounted_loss_s']):.3f} s ({corner_rows[0]['loss_support_n']}/{corner_rows[0]['comparison_n']})**，"
                f"C02 为 **{float(corner_rows[1]['median_accounted_loss_s']):.3f} s ({corner_rows[1]['loss_support_n']}/{corner_rows[1]['comparison_n']})**，"
                f"C01 为 **{float(corner_rows[2]['median_accounted_loss_s']):.3f} s ({corner_rows[2]['loss_support_n']}/{corner_rows[2]['comparison_n']})**；"
                "这些是相对参考圈的描述性窗口中位数，不能相加。\n"
                "- C08/C02 没有支持的动作诊断；C01 仅保留 practice-only 假设。真实进站策略继续 `BLOCKED`，"
                "非官方 development smoke 只证明计算链闭合，不显示也不授权任何比赛服务方案。"
            ),
        },
        {
            "id": "scope_definitions",
            "type": "markdown",
            "sourceId": "session_scope_sql",
            "body": (
                "## 定义与范围\n\n"
                f"报告绑定 Audi R8 LMS EVO II GT3 × Spa 的完整离线 `IBT_OFFLINE` 源 `{source_name}` 与会话 `{session_name}`；"
                "driving 与 fuel replay 的原始、normalized 和 event receipts 完全一致。"
                f"参考为真实 eligible **lap {reference_lap}**，不是 PB 或理论拼接圈；共有 **{len(eligible_laps)} 个 clean laps**、"
                f"每个候选窗口 **{len(eligible_laps) - 1} 个 comparison laps**，空间网格 **{grid_step_m:g} m**。"
                "`accounted loss` 是每个比较圈相对参考圈的 approach、local 与 carry 闭合窗口差值，不是因果收益；"
                "各阶段中位数不能相加。\n\n"
                "**边界：`SHADOW_ONLY · NOT_R7_ATTESTED · advisor_only`。** 本报告不会发出车辆控制或比赛执行命令。"
            ),
        },
        {
            "id": "headline_metrics",
            "type": "metric-strip",
            "cardIds": ["admitted_fuel_laps", "mean_fuel_burn", "conservative_fuel_burn", "top_corner_loss"],
        },
        {
            "id": "fuel_history",
            "type": "markdown",
            "sourceId": "fuel_burn_sql",
            "body": (
                "## 燃油：先建立 recording-only 基线\n\n"
                f"完整 fuel replay 接纳 **{admitted_laps} 圈**、拒绝 **{rejected_laps} 圈**；接纳圈平均消耗 **{mean_burn:.3f} L/lap**，"
                f"观察到的保守 **{percentile_label} 为 {conservative_burn:.3f} L/lap**。这些是历史录制证据，只能用于开发和回放规划；"
                "任何实时剩余圈、当前油量或规则变化都必须重新采集和计算。"
            ),
        },
        {
            "id": "pit_strategy_gate",
            "type": "markdown",
            "sourceId": "capability_gates_sql",
            "body": (
                "## 进站：服务计算已验证，比赛建议仍阻断\n\n"
                "完整 fuel replay 与 pit-plan lineage 已闭合，但规则仍为非官方、未绑定赛事的 `DEVELOPMENT_SMOKE`。"
                "它只验证燃油可行性和计算闭合；本报告刻意不显示加油量、进站圈、换胎选择、服务时间或 pit-loss 数字。"
                "对手燃油、交通、重返位置和当前轮胎磨损均不可用，因此 race recommendation 保持 `BLOCKED`，"
                "且不存在可执行服务建议。"
            ),
        },
        {
            "id": "corner_findings",
            "type": "markdown",
            "sourceId": "corner_losses_sql",
            "body": (
                "## 分弯：三项候选窗口按 accounted loss 排序\n\n"
                f"**{top['candidate_window']}** 的中位 accounted loss 为 **{float(top['median_accounted_loss_s']):.3f} s**，支持 **{top['loss_support_n']}/{top['comparison_n']}**，"
                f"有 **{top['loss_counterexample_n']}** 个反例；C08 与 C02 均为 `NO_SUPPORTED_ACTION`。"
                f"**{c01['candidate_window']}** 的损失支持为 **{c01['loss_support_n']}/{c01['comparison_n']}**，但动作证据只有 **{c01['action_evidence_n']}/{c01['comparison_n']}**；"
                "因此 C01 的 loss 证据和练习动作证据严格分开。下图显示全部 18 个逐圈观察、包含负向反例和零基线；"
                "中位数只留在摘要表中，不堆叠阶段中位数，也不展示预期收益。"
            ),
        },
        {"id": "corner_loss_chart", "type": "chart", "chartId": "corner_loss_distribution", "layout": "full"},
        {"id": "corner_summary", "type": "table", "tableId": "corner_summary_table", "layout": "full"},
        {
            "id": "per_lap_evidence_note",
            "type": "markdown",
            "sourceId": "corner_per_lap_sql",
            "body": (
                "## 每圈证据：负向反例完整保留\n\n"
                "下表保留三个候选窗口各六个比较圈，共 **18 行**。`loss_role` 与 `action_role` 独立编码，"
                "避免由中位数排名隐藏负向反例；phase 列只确认窗口分解闭合。"
            ),
        },
        {"id": "corner_per_lap", "type": "table", "tableId": "corner_per_lap_table", "layout": "full"},
        {
            "id": "capability_gate_findings",
            "type": "markdown",
            "sourceId": "capability_gates_sql",
            "body": (
                "## Capability Gates\n\n"
                "条件数据与人工标签继续 `WAIT`，轮胎、对手燃油、交通和重返预测继续 `SKIP`，"
                "个性化 coaching 与真实比赛建议继续 `BLOCKED`。没有为不可观测量填入默认估计，也没有把开发校验提升为 R7 认证。"
            ),
        },
        {"id": "capability_gates", "type": "table", "tableId": "capability_gates_table", "layout": "full"},
        {
            "id": "next_steps",
            "type": "markdown",
            "body": (
                "## Next Steps\n\n"
                "1. **仅在练习中**对 C01 做受控 A/B 或 ABAB：以小步幅后移刹车点，同时保持一次连续的 release-to-throttle；"
                "先看同条件圈速、1x、second lift 和转向修正，不把当前假设用于正式比赛。\n"
                "2. 为 C08/C02 补同条件证据与人工复核；在新的诊断证据出现前不生成刹车、油门、循迹刹车、路线或路肩动作。\n"
                "3. 绑定目标 endurance 赛事的官方油箱、加油、换胎和 pit-lane 规则，再重算策略。\n"
                "4. 采集真实 SDK 会话并复用同一 normalized/event/model 管线；交通、重返和轮胎输入通过各自质量门槛后再扩展能力。"
            ),
        },
        {
            "id": "further_questions",
            "type": "markdown",
            "body": (
                "## Further Questions\n\n"
                "- C08、C02、C01 分别对应哪个经人工确认的 Spa 弯角/组合弯？\n"
                "- C01 在同条件 A/B 或 ABAB 中是否变快，且不增加 1x、second lift 或转向修正？\n"
                "- C08/C02 能否积累支持某个具体动作诊断的证据？\n"
                "- 目标 endurance series 的官方油箱、加油、换胎、pit-lane 和重返规则是什么？"
            ),
        },
        {
            "id": "caveats",
            "type": "markdown",
            "body": (
                "## Caveats\n\n"
                "- 这是内容寻址、snapshot-bounded 的离线报告，不会自动刷新。\n"
                f"- 数据只含 {len(eligible_laps)} 个 clean laps、每窗口 {len(eligible_laps) - 1} 个 comparison laps；"
                f"参考为 lap {reference_lap}，网格为 {grid_step_m:g} m。条件匹配与人工标签仍在 WAIT。\n"
                "- 候选窗口 ID 不是已认证弯名；驾驶关系是描述性的，不是个性化、因果或高置信结论。\n"
                "- 非官方 development-smoke 规则不能视为 Audi、Spa、iRacing 或具体赛事事实；真实比赛策略仍 BLOCKED。\n"
                "- 当前轮胎磨损、对手燃油、交通和重返位置均为 **Unavailable — not estimated**，而不是低置信数值。\n"
                "- 燃油统计只在本 recording 内有效；所有建议均为 advisor-only、non-executable、未通过 R7 attestation，"
                "且本报告不构成 live SDK 证据。"
            ),
        },
    ]

    snapshot = {
        "version": 1,
        "status": "ready",
        "datasets": {
            "session_scope": scope_rows,
            "fuel_burn": fuel_rows,
            "corner_losses": corner_rows,
            "corner_per_lap": per_lap_rows,
            "capability_gates": gate_rows,
        },
    }
    manifest = {
        "version": 1,
        "surface": "report",
        "title": "iRacing AI 工程师离线赛后报告",
        "description": "Answer-first, source-backed, advisor-only post-session evidence review.",
        "blocks": blocks,
        "cards": cards,
        "charts": [chart],
        "tables": tables,
        "sources": sources,
    }
    input_artifacts: dict[str, object] = {}
    for role, (receipt, canonical_field) in receipts.items():
        input_artifacts[role] = {
            "canonical_field": canonical_field,
            "canonical_sha256": _receipt_canonical(receipt, canonical_field),
            "logical_path": _logical_receipt_path(role, receipt),
            "serialized_sha256": receipt.serialized_sha256,
        }
    package_info = {
        "artifact_hash_contract": "canonical-json-without-package_info.artifact_sha256-v1",
        "attestation_status": "NOT_R7_ATTESTED",
        "audience": "product_stakeholders",
        "contract_version": SESSION_REPORT_CONTRACT_VERSION,
        "execution_boundary": "SHADOW_ONLY_ADVISOR_ONLY",
        "input_artifacts": input_artifacts,
        "session_id": session_name,
        "snapshot_boundary": "four_complete_exact_validated_receipts_no_live_refresh",
        "source_id": source_name,
    }
    artifact = _bind_artifact_hash(
        {
            "surface": "report",
            "manifest": manifest,
            "snapshot": snapshot,
            "sources": sources,
            "package_info": package_info,
        }
    )
    validate_session_report_artifact(artifact)
    return artifact


def build_session_report(
    driving_receipt: LoadedReceipt,
    corner_receipt: LoadedReceipt,
    fuel_receipt: LoadedReceipt,
    pit_receipt: LoadedReceipt,
    *,
    expected_driving_replay_sha256: str,
    expected_fuel_replay_sha256: str,
) -> dict[str, object]:
    """Validate the four receipt inputs and build one canonical report artifact."""

    driving = _validate_full_driving(
        driving_receipt.value,
        expected_sha256=expected_driving_replay_sha256,
    )
    fuel = _validate_full_fuel(
        fuel_receipt.value,
        expected_sha256=expected_fuel_replay_sha256,
    )
    corners = _validate_corner_cards(corner_receipt.value, driving_receipt.value)
    pit = _validate_pit_plan(
        pit_receipt.value,
        fuel,
        expected_fuel_replay_sha256=expected_fuel_replay_sha256,
    )
    _validate_cross_lineage(driving, corners, fuel, pit)
    return _build_artifact_from_validated(
        driving_receipt,
        corner_receipt,
        fuel_receipt,
        pit_receipt,
        driving=driving,
        corners=corners,
        fuel=fuel,
        pit=pit,
    )


def validate_session_report_artifact(value: object) -> dict[str, Any]:
    """Validate this builder's self-bound report, sources, and SQLite projections."""

    artifact = _mapping(value, "session report artifact")
    if set(artifact) != _TOP_LEVEL_KEYS or artifact.get("surface") != "report":
        raise SessionReportError("session report top-level shape or surface is invalid")
    package = _mapping(artifact.get("package_info"), "package_info")
    if (
        package.get("contract_version") != SESSION_REPORT_CONTRACT_VERSION
        or package.get("attestation_status") != "NOT_R7_ATTESTED"
        or package.get("execution_boundary") != "SHADOW_ONLY_ADVISOR_ONLY"
    ):
        raise SessionReportError("session report package boundary is invalid")
    digest = _sha256(package.get("artifact_sha256"), "package_info.artifact_sha256")
    if canonical_sha256(_artifact_without_hash(artifact)) != digest:
        raise SessionReportError("session report artifact_sha256 mismatch")

    manifest = _mapping(artifact.get("manifest"), "manifest")
    if (
        manifest.get("version") != 1
        or manifest.get("surface") != "report"
        or manifest.get("title") != "iRacing AI 工程师离线赛后报告"
    ):
        raise SessionReportError("session report manifest identity is invalid")
    blocks = _array(manifest.get("blocks"), "manifest.blocks")
    if len(blocks) < 2 or blocks[0] != {
        "id": "title",
        "type": "markdown",
        "body": "# iRacing AI 工程师离线赛后报告",
    }:
        raise SessionReportError("first block must be the matching visible title")
    executive = _mapping(blocks[1], "Executive Summary block")
    if executive.get("id") != "executive_summary" or not str(executive.get("body", "")).startswith(
        "## Executive Summary\n"
    ):
        raise SessionReportError("Executive Summary must immediately follow the title")
    block_by_id = {
        _text(_mapping(block, "manifest block").get("id"), "block id"): block for block in blocks
    }
    executive_body = str(executive.get("body", ""))
    for required in ("SHADOW_ONLY", "LOW", "WAIT_CONDITION_DATA", "WAIT_HUMAN_LABELS", "BLOCKED"):
        if required not in executive_body:
            raise SessionReportError("Executive Summary lost a mandatory evidence boundary")
    scope_body = str(_mapping(block_by_id.get("scope_definitions"), "scope_definitions").get("body", ""))
    for required in ("lap 11", "7 个 clean laps", "6 个 comparison laps", "1 m"):
        if required not in scope_body:
            raise SessionReportError("reader-visible reference/cohort/grid scope is incomplete")
    next_steps_body = str(_mapping(block_by_id.get("next_steps"), "next_steps").get("body", ""))
    for required in ("C01", "A/B", "练习", "release-to-throttle", "不把当前假设用于正式比赛"):
        if required not in next_steps_body:
            raise SessionReportError("C01 practice-only experiment boundary is incomplete")
    if any(_mapping(block, "manifest block").get("type") == "html" for block in blocks):
        raise SessionReportError("bespoke HTML blocks are forbidden")
    visible_payload = {
        "blocks": blocks,
        "cards": _array(manifest.get("cards"), "manifest.cards"),
        "charts": _array(manifest.get("charts"), "manifest.charts"),
        "tables": _array(manifest.get("tables"), "manifest.tables"),
    }
    visible_text = _canonical_json(visible_payload).decode("utf-8").lower()
    if any(token in visible_text for token in _FORBIDDEN_VISIBLE_PIT_ACTION_TOKENS):
        raise SessionReportError("reader-visible report exposes development-smoke action details")

    snapshot = _mapping(artifact.get("snapshot"), "snapshot")
    if snapshot.get("version") != 1 or snapshot.get("status") != "ready":
        raise SessionReportError("snapshot must be ready v1")
    datasets = _mapping(snapshot.get("datasets"), "snapshot.datasets")
    if set(datasets) != set(_SOURCE_DATASET.values()) or len(datasets) > MAX_DATASETS:
        raise SessionReportError("snapshot dataset set is invalid")
    total_rows = 0
    for dataset_id, raw_rows in datasets.items():
        rows = _array(raw_rows, f"snapshot.datasets.{dataset_id}")
        if not rows or any(type(row) is not dict for row in rows):
            raise SessionReportError(f"snapshot.datasets.{dataset_id} is not reviewed row data")
        total_rows += len(rows)
    if total_rows > MAX_SNAPSHOT_ROWS:
        raise SessionReportError("snapshot row count exceeds its bound")
    corner_rows = _array(datasets.get("corner_losses"), "corner_losses")
    per_lap_rows = _array(datasets.get("corner_per_lap"), "corner_per_lap")
    if len(corner_rows) != 3 or len(per_lap_rows) != 18:
        raise SessionReportError("corner snapshot must retain 3 summaries and all 18 evidence rows")
    labels = [row.get("candidate_window") for row in corner_rows]
    if labels != [
        "Candidate window C08 (not authenticated name)",
        "Candidate window C02 (not authenticated name)",
        "Candidate window C01 (not authenticated name)",
    ]:
        raise SessionReportError("fixed candidate labels or ordering changed")
    if any(row.get("action_status") != "NO_SUPPORTED_ACTION" for row in corner_rows[:2]):
        raise SessionReportError("C08/C02 no-action boundary changed")
    if corner_rows[2].get("action_evidence_n") == corner_rows[2].get("loss_support_n"):
        raise SessionReportError("C01 action evidence was conflated with loss evidence")
    gate_statuses = {
        str(row.get("status"))
        for row in _array(datasets.get("capability_gates"), "capability_gates")
    }
    if not _REQUIRED_GATE_STATUSES.issubset(gate_statuses):
        raise SessionReportError("WAIT/SKIP/BLOCKED capability states were lost")

    charts = _array(manifest.get("charts"), "manifest.charts")
    if len(charts) != 1:
        raise SessionReportError("report must contain exactly one chart")
    chart = _mapping(charts[0], "manifest.charts[0]")
    encodings = _mapping(chart.get("encodings"), "chart encodings")
    reference_lines = _array(chart.get("referenceLines"), "chart referenceLines")
    if (
        chart.get("type") != "scatter"
        or chart.get("dataset") != "corner_per_lap"
        or chart.get("sourceId") != "corner_per_lap_sql"
        or _mapping(encodings.get("x"), "chart x encoding").get("field")
        != "accounted_window_delta_s"
        or _mapping(encodings.get("y"), "chart y encoding").get("field") != "corner_rank"
        or _mapping(encodings.get("color"), "chart color encoding").get("field") != "loss_role"
        or len(reference_lines) != 1
        or _mapping(reference_lines[0], "zero reference line").get("axis") != "x"
        or _mapping(reference_lines[0], "zero reference line").get("value") != 0
    ):
        raise SessionReportError("the sole chart must preserve all 18 per-lap deltas and zero")
    if "action" in _canonical_json(chart).decode("utf-8").lower():
        raise SessionReportError("the loss chart must not encode action evidence")
    if "expected_gain" in _canonical_json({"manifest": manifest, "snapshot": snapshot}).decode(
        "utf-8"
    ):
        raise SessionReportError("report must not display expected gain")

    sources = _array(artifact.get("sources"), "sources")
    if manifest.get("sources") != sources or len(sources) != len(_SOURCE_DATASET):
        raise SessionReportError("manifest/top-level canonical sources differ")
    source_by_id: dict[str, dict[str, Any]] = {}
    for raw_source in sources:
        source = _mapping(raw_source, "canonical source")
        if set(source) != {"id", "label", "path", "query"}:
            raise SessionReportError("canonical source contains unsupported fields")
        source_id = _text(source.get("id"), "source id")
        if source_id in source_by_id or source_id not in _SOURCE_DATASET:
            raise SessionReportError("canonical source id is duplicate or unknown")
        source_path = _safe_logical_path(source.get("path"), f"source {source_id}.path")
        if source.get("label") != f"Non-resolvable content-addressed audit binding — {_SOURCE_DATASET[source_id]}":
            raise SessionReportError("canonical source label hides its non-resolvable boundary")
        query = _mapping(source.get("query"), f"source {source_id}.query")
        expected_query_keys = {
            "description",
            "engine",
            "filters",
            "id",
            "language",
            "metric_definitions",
            "sql",
            "tables_used",
        }
        if set(query) != expected_query_keys or query.get("engine") != "sqlite" or query.get(
            "language"
        ) != "sql":
            raise SessionReportError("canonical source query metadata is invalid")
        if _array(query.get("tables_used"), "tables_used") != ["reviewed"]:
            raise SessionReportError("source SQL must identify only its embedded reviewed CTE")
        description = _text(query.get("description"), f"source {source_id}.description")
        if "non-resolvable logical source" not in description or "inline VALUES CTE" not in description:
            raise SessionReportError("source description overstates logical receipt path resolution")
        sql_value = query.get("sql")
        if (
            type(sql_value) is not str
            or not sql_value.strip()
            or len(sql_value.encode("utf-8")) > 200_000
            or "\x00" in sql_value
        ):
            raise SessionReportError(f"source {source_id}.query.sql is not bounded SQL text")
        sql = sql_value
        if not sql.lstrip().startswith("WITH reviewed("):
            raise SessionReportError(f"source {source_id}.query.sql is not a reviewed projection")
        projected = _execute_projection(sql)
        dataset_id = _SOURCE_DATASET[source_id]
        if _canonical_json(projected) != _canonical_json(datasets[dataset_id]):
            raise SessionReportError(f"source {source_id} SQL does not reproduce {dataset_id}")
        expected_query_id = (
            f"{source_id}:"
            f"{canonical_sha256({'dataset': dataset_id, 'rows': datasets[dataset_id], 'sql': sql})}"
        )
        if query.get("id") != expected_query_id:
            raise SessionReportError(f"source {source_id} query id does not bind SQL and rows")
        if not _array(query.get("metric_definitions"), "metric_definitions"):
            raise SessionReportError(f"source {source_id} lacks metric definitions")
        source["path"] = source_path
        source_by_id[source_id] = source

    for collection_name in ("cards", "charts", "tables"):
        for raw_item in _array(manifest.get(collection_name), f"manifest.{collection_name}"):
            item = _mapping(raw_item, f"manifest.{collection_name} item")
            source_id = item.get("sourceId")
            dataset_id = item.get("dataset")
            if source_id not in source_by_id or _SOURCE_DATASET.get(str(source_id)) != dataset_id:
                raise SessionReportError(
                    f"manifest.{collection_name} item source/dataset provenance is not exact"
                )
            if collection_name == "tables" and item.get("defaultSort") is not None:
                sort_field = _mapping(item.get("defaultSort"), "table defaultSort").get("field")
                column_fields = {
                    _mapping(column, "table column").get("field")
                    for column in _array(item.get("columns"), "table columns")
                }
                if sort_field not in column_fields:
                    raise SessionReportError("table defaultSort field is not a declared column")
    for raw_block in blocks:
        block = _mapping(raw_block, "manifest block")
        if block.get("sourceId") is not None and block.get("sourceId") not in source_by_id:
            raise SessionReportError("markdown sourceId does not resolve")

    inputs = _mapping(package.get("input_artifacts"), "package input_artifacts")
    if set(inputs) != {"driving_replay", "corner_cards", "fuel_replay", "pit_plan"}:
        raise SessionReportError("package input artifact roles are incomplete")
    expected_canonical_fields = {
        "driving_replay": "driving_replay_sha256",
        "corner_cards": "corner_cards_sha256",
        "fuel_replay": "fuel_replay_sha256",
        "pit_plan": "pit_plan_sha256",
    }
    input_by_path: dict[str, tuple[str, dict[str, Any]]] = {}
    for role, raw_entry in inputs.items():
        entry = _mapping(raw_entry, f"package input {role}")
        if set(entry) != {
            "canonical_field",
            "canonical_sha256",
            "logical_path",
            "serialized_sha256",
        }:
            raise SessionReportError("package input entry contains unsupported fields")
        if entry.get("canonical_field") != expected_canonical_fields[role]:
            raise SessionReportError("package input canonical field does not match its role")
        logical_path = _safe_logical_path(
            entry.get("logical_path"), f"package input {role}.logical_path"
        )
        serialized_digest = _sha256(
            entry.get("serialized_sha256"), f"package input {role}.serialized_sha256"
        )
        expected_logical_path = f"logical-receipts/{role}/{serialized_digest}.json"
        if logical_path != expected_logical_path:
            raise SessionReportError("logical input path is not content-addressed")
        _sha256(entry.get("canonical_sha256"), f"package input {role}.canonical_sha256")
        if logical_path in input_by_path:
            raise SessionReportError("package input logical paths are not unique")
        input_by_path[logical_path] = (role, entry)
    scope_row = _mapping(
        _array(datasets.get("session_scope"), "session_scope")[0], "session_scope row"
    )
    if (
        package.get("source_id") != scope_row.get("bound_source")
        or package.get("session_id") != scope_row.get("bound_session")
    ):
        raise SessionReportError("package source/session do not bind the scope dataset")
    for source_id, source in source_by_id.items():
        query = _mapping(source.get("query"), f"source {source_id}.query")
        filters = _array(query.get("filters"), f"source {source_id}.filters")
        expected_roles = _SOURCE_ROLES[source_id]
        for role in expected_roles:
            matching = [entry for bound_role, entry in input_by_path.values() if bound_role == role]
            if len(matching) != 1:
                raise SessionReportError(f"source {source_id} lacks an exact input role binding")
            entry = matching[0]
            required_filters = {
                f"{role} serialized_sha256 = {entry['serialized_sha256']}",
                f"{role} {entry['canonical_field']} = {entry['canonical_sha256']}",
            }
            if not required_filters.issubset(set(filters)):
                raise SessionReportError(f"source {source_id} receipt provenance is incomplete")
        first_role = expected_roles[0]
        first_path = next(
            path for path, (bound_role, _) in input_by_path.items() if bound_role == first_role
        )
        if source.get("path") != first_path:
            raise SessionReportError(f"source {source_id}.path does not bind its primary input")
    return artifact


def _exclusive_write(path: Path, payload: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short output write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
    except OSError as exc:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if created:
            with suppress(OSError):
                os.unlink(path)
        raise SessionReportError(f"cannot exclusively create output: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("driving_replay", type=Path)
    parser.add_argument("corner_cards", type=Path)
    parser.add_argument("fuel_replay", type=Path)
    parser.add_argument("pit_plan", type=Path)
    parser.add_argument("--expected-driving-replay-file-sha256", required=True)
    parser.add_argument("--expected-driving-replay-sha256", required=True)
    parser.add_argument("--expected-corner-cards-file-sha256", required=True)
    parser.add_argument("--expected-fuel-replay-file-sha256", required=True)
    parser.add_argument("--expected-fuel-replay-sha256", required=True)
    parser.add_argument("--expected-pit-plan-file-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        driving = _read_receipt(
            args.driving_replay,
            expected_sha256=args.expected_driving_replay_file_sha256,
            name="driving replay",
        )
        corners = _read_receipt(
            args.corner_cards,
            expected_sha256=args.expected_corner_cards_file_sha256,
            name="corner cards",
        )
        fuel = _read_receipt(
            args.fuel_replay,
            expected_sha256=args.expected_fuel_replay_file_sha256,
            name="fuel replay",
        )
        pit = _read_receipt(
            args.pit_plan,
            expected_sha256=args.expected_pit_plan_file_sha256,
            name="pit plan",
        )
        artifact = build_session_report(
            driving,
            corners,
            fuel,
            pit,
            expected_driving_replay_sha256=args.expected_driving_replay_sha256,
            expected_fuel_replay_sha256=args.expected_fuel_replay_sha256,
        )
        payload = _canonical_json(artifact, newline=True)
        _exclusive_write(args.output, payload)
    except (OSError, SessionReportError) as exc:
        error = {
            "contract_version": SESSION_REPORT_CONTRACT_VERSION,
            "error": str(exc),
            "status": "REFUSED",
        }
        sys.stderr.buffer.write(_canonical_json(error, newline=True))
        return 3
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# ruff: noqa: E501
"""Deterministic, advisor-only reports for validated engineer sessions.

The report is a projection of one fully validated ``engineer-session-v1``
artifact.  It does not reopen telemetry, run a model, access a network, or
control iRacing.  Race-strategy advice is sourced only from the M2 strategy
component; development-smoke fuel candidates are deliberately excluded.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import stat
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import NoReturn

from .engineer_session import validate_engineer_session

ENGINEER_SESSION_REPORT_CONTRACT_VERSION = "engineer-session-report-v1"

_SHA256_CHARS = frozenset("0123456789abcdef")
_REPORT_KEYS = frozenset(
    {
        "advisor_only",
        "answer_first",
        "blockers",
        "contract_version",
        "engineer_session_binding",
        "readiness",
        "report_sha256",
        "safety",
        "sections",
        "status",
    }
)
_SAFETY = {
    "advisor_only": True,
    "development_smoke_fuel_values_exposed": False,
    "html_self_contained": True,
    "network_accessed": False,
    "pit_black_box_control_enabled": False,
    "recommendations_executable": False,
    "script_execution_enabled": False,
    "source_recommendation_policy": "M2_STRATEGY_ONLY",
    "telemetry_read_only": True,
    "vehicle_control_enabled": False,
}
_PASS_STATUSES = frozenset(
    {
        "COMPLETE",
        "PASS",
        "PASS_SHADOW_CONTRACT",
        "PRACTICE_READY",
        "READY",
    }
)


class EngineerSessionReportError(ValueError):
    """Fail-closed error raised by report projection or persistence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise EngineerSessionReportError(code, message)


def _canonical_json(value: object, *, newline: bool = False) -> bytes:
    try:
        payload = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise EngineerSessionReportError(
            "CANONICAL_JSON_FAILED",
            "report value is not canonical-JSON-safe",
        ) from exc
    return payload + (b"\n" if newline else b"")


def _persisted_json(value: object) -> bytes:
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
        raise EngineerSessionReportError(
            "REPORT_SERIALIZATION_FAILED",
            "report is not stable JSON",
        ) from exc


def _copy_json(value: object, name: str) -> object:
    try:
        return json.loads(_canonical_json(value))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover
        raise EngineerSessionReportError(
            "CANONICAL_JSON_FAILED",
            f"{name} cannot be copied as JSON",
        ) from exc


def _mapping(value: object, name: str) -> dict[str, object]:
    if type(value) is not dict:
        _fail("SCHEMA_INVALID", f"{name} must be a plain object")
    return value


def _list(value: object, name: str) -> list[object]:
    if type(value) is not list:
        _fail("SCHEMA_INVALID", f"{name} must be a plain list")
    return value


def _sha256(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _SHA256_CHARS for character in value)
    ):
        _fail("SCHEMA_INVALID", f"{name} must be a lowercase SHA-256 digest")
    return value


def _status(value: object, fallback: str = "UNKNOWN") -> str:
    if type(value) is str and value:
        return value
    return fallback


def _reason_codes(value: Mapping[str, object]) -> list[str]:
    candidates: list[object] = []
    for key in ("reason_codes", "reasons"):
        item = value.get(key)
        if type(item) is list:
            candidates.extend(item)
        elif type(item) is str:
            candidates.append(item)
    reason = value.get("reason")
    if type(reason) is str:
        candidates.append(reason)
    return sorted({item for item in candidates if type(item) is str and item})


def _capability_rows(value: object, domain: str) -> list[dict[str, object]]:
    capabilities = _mapping(value, f"{domain} capabilities")
    rows: list[dict[str, object]] = []
    for name in sorted(capabilities):
        capability = _mapping(capabilities[name], f"{domain} capability {name}")
        rows.append(
            {
                "domain": domain,
                "name": name,
                "reason_codes": _reason_codes(capability),
                "status": _status(capability.get("status")),
            }
        )
    return rows


def _ensure_non_executable(
    values: Sequence[object],
    *,
    name: str,
) -> list[object]:
    copied: list[object] = []
    for index, value in enumerate(values):
        item = _mapping(value, f"{name}[{index}]")
        if item.get("executable") is not False:
            _fail(
                "SAFETY_BOUNDARY_INVALID",
                f"{name}[{index}] is not explicitly non-executable",
            )
        copied.append(_copy_json(item, f"{name}[{index}]"))
    return copied


def _gate_blockers(
    rows: list[dict[str, object]],
    *,
    domain: str,
    gate: object,
) -> None:
    value = _mapping(gate, f"{domain} quality gate")
    status = _status(value.get("status"))
    if status in _PASS_STATUSES:
        return
    reasons = _reason_codes(value) or [status]
    rows.extend(
        {"code": reason, "domain": domain, "status": status}
        for reason in reasons
    )


def _capability_blockers(
    rows: list[dict[str, object]], capability_rows: Sequence[Mapping[str, object]]
) -> None:
    for capability in capability_rows:
        status = _status(capability.get("status"))
        if status in _PASS_STATUSES:
            continue
        reasons = capability.get("reason_codes")
        codes = (
            [item for item in reasons if type(item) is str and item]
            if type(reasons) is list
            else []
        )
        if not codes:
            name = capability.get("name")
            codes = [str(name) if type(name) is str else status]
        rows.extend(
            {
                "code": code,
                "domain": _status(capability.get("domain"), "unknown"),
                "status": status,
            }
            for code in codes
        )


def _promotion_blockers(
    rows: list[dict[str, object]],
    *,
    cards: Sequence[object],
    diagnosis: Mapping[str, object],
) -> None:
    for card in cards:
        card_value = _mapping(card, "corner card")
        corner_id = _status(card_value.get("corner_id"), "UNKNOWN_CORNER")
        raw = card_value.get("promotion_blockers")
        if type(raw) is list:
            for code in raw:
                if type(code) is str and code:
                    rows.append(
                        {
                            "code": code,
                            "domain": f"driving:{corner_id}",
                            "status": "BLOCKED",
                        }
                    )
    gates = _mapping(diagnosis.get("promotion_gates"), "diagnosis promotion gates")
    for name in sorted(gates):
        raw_gate = gates[name]
        if type(raw_gate) is str:
            status = raw_gate
            reasons = [raw_gate]
        else:
            gate = _mapping(raw_gate, f"diagnosis promotion gate {name}")
            status = _status(gate.get("status"))
            reasons = _reason_codes(gate) or [name]
        if status in _PASS_STATUSES:
            continue
        rows.extend(
            {"code": reason, "domain": "driving_diagnosis", "status": status}
            for reason in reasons
        )


def _deduplicate_blockers(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    unique = {
        (
            _status(row.get("domain"), "unknown"),
            _status(row.get("code"), "UNKNOWN"),
            _status(row.get("status")),
        )
        for row in rows
    }
    return [
        {"code": code, "domain": domain, "status": status}
        for domain, code, status in sorted(unique)
    ]


def _first_text(value: Mapping[str, object], keys: Sequence[str]) -> str | None:
    for key in keys:
        item = value.get(key)
        if type(item) is str and item.strip():
            return item.strip()
    return None


def _build_report_from_validated_session(
    session: Mapping[str, object],
) -> dict[str, object]:
    components = _mapping(session.get("components"), "engineer session components")
    lineage = _mapping(session.get("input_lineage"), "engineer session input lineage")
    fuel = _mapping(components.get("fuel_replay"), "fuel replay component")
    driving_replay = _mapping(
        components.get("driving_replay"), "driving replay component"
    )
    pit_stint = _mapping(components.get("m1_pit_stint"), "M1 pit/stint component")
    strategy = _mapping(components.get("m2_strategy"), "M2 strategy component")
    corner_cards = _mapping(components.get("corner_cards"), "corner-card component")
    diagnosis = _mapping(
        components.get("driving_diagnosis"), "driving diagnosis component"
    )
    timeline = _mapping(
        components.get("advisor_timeline"), "advisor timeline component"
    )

    if (
        strategy.get("advisor_only") is not True
        or pit_stint.get("advisor_only") is not True
        or corner_cards.get("advisor_only") is not True
        or diagnosis.get("advisor_only") is not True
    ):
        _fail(
            "SAFETY_BOUNDARY_INVALID",
            "every report-facing component must be advisor-only",
        )

    strategy_recommendations = _ensure_non_executable(
        _list(strategy.get("recommendations"), "M2 strategy recommendations"),
        name="M2 strategy recommendations",
    )
    cards = _ensure_non_executable(
        _list(corner_cards.get("cards"), "corner cards"),
        name="corner cards",
    )
    diagnosis_recommendations = _ensure_non_executable(
        _list(diagnosis.get("recommendations"), "diagnosis recommendations"),
        name="diagnosis recommendations",
    )
    practice_cards = [
        card
        for card in cards
        if isinstance(card, dict)
        and card.get("practice_only") is True
        and type(card.get("action")) is str
        and bool(str(card["action"]).strip())
    ]

    fuel_capabilities = _capability_rows(fuel.get("capabilities"), "fuel")
    pit_capabilities = _capability_rows(pit_stint.get("capabilities"), "pit_stint")
    strategy_capabilities = _capability_rows(
        strategy.get("capabilities"), "strategy"
    )
    driving_capabilities = _capability_rows(
        driving_replay.get("capabilities"), "driving"
    )

    blockers: list[dict[str, object]] = []
    _gate_blockers(blockers, domain="fuel", gate=fuel.get("quality_gate"))
    _gate_blockers(blockers, domain="pit_stint", gate=pit_stint.get("quality_gate"))
    _gate_blockers(blockers, domain="strategy", gate=strategy.get("quality_gate"))
    for rows in (
        fuel_capabilities,
        pit_capabilities,
        strategy_capabilities,
        driving_capabilities,
    ):
        _capability_blockers(blockers, rows)
    _promotion_blockers(blockers, cards=cards, diagnosis=diagnosis)
    blocker_rows = _deduplicate_blockers(blockers)

    strategy_status = (
        "ADVICE_READY" if strategy_recommendations else _status(strategy.get("status"))
    )
    if practice_cards:
        driving_status = "PRACTICE_READY"
    elif cards:
        driving_status = "EVIDENCE_ONLY"
    else:
        driving_status = _status(driving_replay.get("readiness_status"))

    if strategy_recommendations:
        first_strategy = _mapping(
            strategy_recommendations[0], "first M2 strategy recommendation"
        )
        detail = _first_text(
            first_strategy,
            ("message", "action", "summary", "recommendation", "kind"),
        ) or "Review the gated M2 strategy recommendation."
        answer_first = {
            "category": "STRATEGY",
            "detail": detail,
            "headline": "Race strategy advice is available.",
            "practice_action_count": len(practice_cards),
            "strategy_advice_count": len(strategy_recommendations),
        }
        report_status = "ADVICE_AVAILABLE"
    elif practice_cards:
        first_card = _mapping(practice_cards[0], "first practice card")
        corner_id = _status(first_card.get("corner_id"), "top-loss corner")
        answer_first = {
            "category": "PRACTICE",
            "detail": _status(first_card.get("action")),
            "headline": f"Practice priority: {corner_id}.",
            "practice_action_count": len(practice_cards),
            "strategy_advice_count": 0,
        }
        report_status = "PRACTICE_AVAILABLE"
    elif cards:
        answer_first = {
            "category": "EVIDENCE_ONLY",
            "detail": (
                "Corner-loss evidence exists, but its promotion gates do not yet "
                "support a practice action."
            ),
            "headline": "Driving evidence is available; advice is still gated.",
            "practice_action_count": 0,
            "strategy_advice_count": 0,
        }
        report_status = "EVIDENCE_ONLY"
    else:
        first_blocker = blocker_rows[0]["code"] if blocker_rows else "WAIT_DATA"
        answer_first = {
            "category": "WAIT_DATA",
            "detail": f"No advice was promoted; first explicit blocker: {first_blocker}.",
            "headline": "The session is valid, but advice remains gated.",
            "practice_action_count": 0,
            "strategy_advice_count": 0,
        }
        report_status = "WAIT_DATA"

    speech_policy = _mapping(
        timeline.get("speech_policy_run"), "advisor timeline speech policy run"
    )
    decisions = _list(speech_policy.get("decisions"), "advisor speech decisions")
    driving_context = _mapping(
        driving_replay.get("driving_context"), "driving context"
    )
    horizon = _mapping(strategy.get("horizon"), "strategy horizon")
    rules_binding = _mapping(strategy.get("rules_binding"), "strategy rules binding")
    traffic_rejoin = _mapping(
        strategy.get("traffic_rejoin"), "strategy traffic/rejoin"
    )

    sections = {
        "advisor_timeline": {
            "decision_count": len(decisions),
            "decisions": _copy_json(decisions, "advisor speech decisions"),
            "status": _status(timeline.get("status")),
            "summary": _copy_json(timeline.get("summary"), "advisor timeline summary"),
        },
        "driving": {
            "capabilities": driving_capabilities,
            "card_count": len(cards),
            "cards": cards,
            "diagnosis": {
                "claim_scope": diagnosis.get("claim_scope"),
                "executable": False,
                "promotion_gates": _copy_json(
                    diagnosis.get("promotion_gates"), "diagnosis promotion gates"
                ),
                "recommendations": diagnosis_recommendations,
                "status": _status(diagnosis.get("status")),
                "summary": _copy_json(
                    diagnosis.get("summary"), "driving diagnosis summary"
                ),
            },
            "practice_action_count": len(practice_cards),
            "reference": _copy_json(
                corner_cards.get("reference"), "corner-card reference"
            ),
            "status": driving_status,
            "track_context": _copy_json(driving_context, "driving context"),
        },
        "fuel": {
            "capabilities": fuel_capabilities,
            "lap_receipt": _copy_json(fuel.get("lap_receipt"), "fuel lap receipt"),
            "model_readiness_status": _status(fuel.get("readiness_status")),
            "quality_gate": _copy_json(fuel.get("quality_gate"), "fuel quality gate"),
            "recommendation_source": "M2_STRATEGY_ONLY",
            "strategy_numbers_exposed": False,
        },
        "pit_stint": {
            "capabilities": pit_capabilities,
            "pit_cycles": _copy_json(pit_stint.get("pit_cycles"), "pit cycles"),
            "quality_gate": _copy_json(
                pit_stint.get("quality_gate"), "pit/stint quality gate"
            ),
            "service_contents": _copy_json(
                pit_stint.get("service_contents"), "service contents"
            ),
            "status": _status(pit_stint.get("status")),
            "stints": _copy_json(pit_stint.get("stints"), "stints"),
            "summary": _copy_json(pit_stint.get("summary"), "pit/stint summary"),
        },
        "strategy": {
            "authoritative_component": "m2_strategy",
            "capabilities": strategy_capabilities,
            "horizon": {
                "kind": horizon.get("kind"),
                "one_more_lap_status": horizon.get("one_more_lap_status"),
                "reason_codes": _reason_codes(horizon),
                "status": _status(horizon.get("status")),
            },
            "quality_gate": _copy_json(
                strategy.get("quality_gate"), "strategy quality gate"
            ),
            "recommendation_count": len(strategy_recommendations),
            "recommendations": strategy_recommendations,
            "rules": {
                "official_event_rules": rules_binding.get("official_event_rules"),
                "profile_id": rules_binding.get("profile_id"),
                "reason_codes": _reason_codes(rules_binding),
                "status": _status(rules_binding.get("status")),
            },
            "status": strategy_status,
            "traffic_rejoin_status": _status(traffic_rejoin.get("status")),
        },
    }
    readiness = {
        "advisor_timeline": _status(timeline.get("status")),
        "driving": driving_status,
        "fuel_model": _status(fuel.get("readiness_status")),
        "pit_stint": _status(pit_stint.get("status")),
        "strategy": strategy_status,
    }
    binding = {
        "engineer_session_contract_version": session.get("contract_version"),
        "engineer_session_sha256": _sha256(
            session.get("engineer_session_sha256"), "engineer session SHA-256"
        ),
        "input_evidence_sha256": _sha256(
            lineage.get("input_evidence_sha256"), "input evidence SHA-256"
        ),
        "input_kind": lineage.get("input_kind"),
        "sample_count": lineage.get("sample_count"),
        "session_id": lineage.get("session_id"),
        "source_content_sha256": _sha256(
            lineage.get("source_content_sha256"), "source content SHA-256"
        ),
        "source_id": lineage.get("source_id"),
        "source_kind": lineage.get("source_kind"),
    }
    base = {
        "advisor_only": True,
        "answer_first": answer_first,
        "blockers": blocker_rows,
        "contract_version": ENGINEER_SESSION_REPORT_CONTRACT_VERSION,
        "engineer_session_binding": binding,
        "readiness": readiness,
        "safety": dict(_SAFETY),
        "sections": sections,
        "status": report_status,
    }
    return {**base, "report_sha256": hashlib.sha256(_canonical_json(base)).hexdigest()}


def build_engineer_session_report(
    engineer_session: object,
    *,
    expected_engineer_session_sha256: str | None = None,
) -> dict[str, object]:
    """Build one deterministic report from a fully validated engineer session."""

    validated = validate_engineer_session(
        engineer_session,
        expected_engineer_session_sha256=expected_engineer_session_sha256,
    )
    return _build_report_from_validated_session(validated)


def validate_engineer_session_report(
    report: object,
    engineer_session: object,
    *,
    expected_report_sha256: str | None = None,
    expected_engineer_session_sha256: str | None = None,
) -> dict[str, object]:
    """Rebuild and exact-compare one report against its bound source session."""

    validated_session = validate_engineer_session(
        engineer_session,
        expected_engineer_session_sha256=expected_engineer_session_sha256,
    )
    copied = _copy_json(report, "engineer session report")
    payload = _mapping(copied, "engineer session report")
    if set(payload) != _REPORT_KEYS:
        _fail("SCHEMA_INVALID", "engineer session report keys are invalid")
    if payload.get("contract_version") != ENGINEER_SESSION_REPORT_CONTRACT_VERSION:
        _fail("CONTRACT_VERSION_MISMATCH", "unsupported report contract")
    stored = _sha256(payload.get("report_sha256"), "report SHA-256")
    if expected_report_sha256 is not None and stored != _sha256(
        expected_report_sha256, "expected report SHA-256"
    ):
        _fail("REPORT_SHA256_MISMATCH", "report failed independent digest binding")
    material = {key: value for key, value in payload.items() if key != "report_sha256"}
    if hashlib.sha256(_canonical_json(material)).hexdigest() != stored:
        _fail("REPORT_SHA256_MISMATCH", "report self hash mismatch")
    expected = _build_report_from_validated_session(validated_session)
    if payload != expected:
        _fail("REPORT_REPLAY_MISMATCH", "report does not reproduce from its session")
    return payload


def _display(value: object) -> str:
    if value is None:
        return "—"
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if type(value) is float:
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if type(value) is list:
        return ", ".join(_display(item) for item in value) if value else "—"
    if type(value) is dict:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _e(value: object) -> str:
    return html.escape(_display(value), quote=True)


def _capability_table(rows: object) -> str:
    values = _list(rows, "report capability rows")
    if not values:
        return '<p class="muted">No capability rows.</p>'
    body = "".join(
        "<tr>"
        f"<td>{_e(_mapping(row, 'capability row').get('name'))}</td>"
        f"<td><span class=\"pill\">{_e(_mapping(row, 'capability row').get('status'))}</span></td>"
        f"<td>{_e(_mapping(row, 'capability row').get('reason_codes'))}</td>"
        "</tr>"
        for row in values
    )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Capability</th>'
        f"<th>Status</th><th>Reasons</th></tr></thead><tbody>{body}</tbody></table></div>"
    )


def _recommendation_cards(values: object, *, empty: str) -> str:
    recommendations = _list(values, "report recommendations")
    if not recommendations:
        return f'<p class="muted">{_e(empty)}</p>'
    rendered: list[str] = []
    for index, raw in enumerate(recommendations, start=1):
        item = _mapping(raw, "report recommendation")
        title = _first_text(
            item,
            ("corner_id", "recommendation_id", "kind", "status"),
        ) or f"Recommendation {index}"
        action = _first_text(
            item,
            ("message", "action", "summary", "recommendation", "diagnosis"),
        ) or "Review the bound evidence in the audit payload."
        rendered.append(
            '<article class="recommendation">'
            f"<h4>{_e(title)}</h4><p>{_e(action)}</p>"
            f"<p class=\"meta\">Status: {_e(item.get('status'))} · "
            f"Executable: {_e(item.get('executable'))}</p></article>"
        )
    return "".join(rendered)


def _pit_stint_table(values: object) -> str:
    stints = _list(values, "report stints")
    if not stints:
        return '<p class="muted">No stint intervals were recognized.</p>'
    body = "".join(
        "<tr>"
        f"<td>{_e(_mapping(raw, 'stint').get('stint_id'))}</td>"
        f"<td>{_e(_mapping(raw, 'stint').get('status'))}</td>"
        f"<td>{_e(_mapping(raw, 'stint').get('duration_s'))}</td>"
        f"<td>{_e(_mapping(raw, 'stint').get('observed_laps_completed_delta'))}</td>"
        f"<td>{_e(_mapping(raw, 'stint').get('observed_start_tank_level_l'))}</td>"
        f"<td>{_e(_mapping(raw, 'stint').get('observed_end_tank_level_l'))}</td>"
        "</tr>"
        for raw in stints
    )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Stint</th><th>Status</th>'
        "<th>Duration s</th><th>Laps observed</th><th>Fuel start L</th>"
        f"<th>Fuel end L</th></tr></thead><tbody>{body}</tbody></table></div>"
    )


def render_engineer_session_report_html(
    report: object,
    engineer_session: object,
    *,
    expected_report_sha256: str | None = None,
    expected_engineer_session_sha256: str | None = None,
) -> bytes:
    """Render deterministic, self-contained, script-free report HTML."""

    validated = validate_engineer_session_report(
        report,
        engineer_session,
        expected_report_sha256=expected_report_sha256,
        expected_engineer_session_sha256=expected_engineer_session_sha256,
    )
    answer = _mapping(validated["answer_first"], "report answer-first")
    binding = _mapping(validated["engineer_session_binding"], "report binding")
    readiness = _mapping(validated["readiness"], "report readiness")
    sections = _mapping(validated["sections"], "report sections")
    strategy = _mapping(sections["strategy"], "report strategy section")
    fuel = _mapping(sections["fuel"], "report fuel section")
    pit_stint = _mapping(sections["pit_stint"], "report pit/stint section")
    driving = _mapping(sections["driving"], "report driving section")
    timeline = _mapping(sections["advisor_timeline"], "report timeline section")
    blockers = _list(validated["blockers"], "report blockers")

    readiness_cards = "".join(
        '<div class="metric">'
        f"<span>{_e(name.replace('_', ' ').title())}</span>"
        f"<strong>{_e(readiness[name])}</strong></div>"
        for name in sorted(readiness)
    )
    blocker_items = (
        "".join(
            f"<li><code>{_e(_mapping(row, 'blocker').get('domain'))}</code> · "
            f"{_e(_mapping(row, 'blocker').get('code'))} "
            f"<span class=\"muted\">({_e(_mapping(row, 'blocker').get('status'))})</span></li>"
            for row in blockers
        )
        or "<li>No explicit blocker was projected.</li>"
    )
    audit_json = html.escape(
        _persisted_json(validated).decode("utf-8"), quote=False
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
  <title>iRacing AI Engineer Session Report</title>
  <style>
    :root {{ color-scheme: dark; --bg:#09111f; --panel:#121d30; --line:#29405f;
      --text:#edf4ff; --muted:#a8b7ca; --cyan:#65d8ff; --amber:#ffc76a; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:linear-gradient(145deg,#07101d,#0d1930); color:var(--text);
      font:15px/1.55 Inter,Segoe UI,system-ui,sans-serif; }}
    main {{ width:min(1180px,calc(100% - 28px)); margin:24px auto 64px; }}
    header,section,details {{ background:rgba(18,29,48,.96); border:1px solid var(--line);
      border-radius:16px; padding:20px; margin:14px 0; box-shadow:0 12px 35px #0005; }}
    h1,h2,h3,h4 {{ margin:.2em 0 .55em; line-height:1.2; }} h1 {{ font-size:clamp(1.8rem,4vw,3rem); }}
    h2 {{ color:var(--cyan); }} .eyebrow {{ color:var(--cyan); text-transform:uppercase;
      letter-spacing:.12em; font-weight:700; }} .answer {{ font-size:1.18rem; max-width:78ch; }}
    .safety {{ border-left:4px solid var(--amber); padding-left:12px; }} .muted,.meta {{ color:var(--muted); }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:10px; }}
    .metric,.recommendation {{ background:#0b1628; border:1px solid #263d5c; border-radius:12px; padding:13px; }}
    .metric span {{ display:block; color:var(--muted); font-size:.82rem; }} .metric strong {{ overflow-wrap:anywhere; }}
    .pill {{ display:inline-block; border:1px solid #3d668d; border-radius:999px; padding:2px 8px; }}
    .table-wrap {{ overflow-x:auto; }} table {{ width:100%; border-collapse:collapse; min-width:620px; }}
    th,td {{ text-align:left; vertical-align:top; border-bottom:1px solid #263d5c; padding:9px; }}
    th {{ color:var(--cyan); }} code,pre {{ font-family:Cascadia Code,Consolas,monospace; }}
    code {{ overflow-wrap:anywhere; }} pre {{ white-space:pre-wrap; overflow-wrap:anywhere; font-size:.78rem; }}
    summary {{ cursor:pointer; font-weight:700; color:var(--cyan); }} ul {{ padding-left:22px; }}
    @media (max-width:560px) {{ main {{ width:min(100% - 16px,1180px); margin-top:8px; }}
      header,section,details {{ border-radius:11px; padding:15px; }} }}
  </style>
</head>
<body data-report-sha256="{_e(validated['report_sha256'])}">
<main>
  <header>
    <p class="eyebrow">Advisor-only · {_e(validated['status'])}</p>
    <h1>{_e(answer.get('headline'))}</h1>
    <p class="answer">{_e(answer.get('detail'))}</p>
    <p class="safety"><strong>Safety boundary:</strong> telemetry is read-only; this report cannot steer,
      brake, accelerate, change the iRacing pit black box, or execute a strategy.</p>
  </header>
  <section><h2>Session identity</h2><div class="grid">
    <div class="metric"><span>Source kind</span><strong>{_e(binding.get('source_kind'))}</strong></div>
    <div class="metric"><span>Input kind</span><strong>{_e(binding.get('input_kind'))}</strong></div>
    <div class="metric"><span>Source</span><strong>{_e(binding.get('source_id'))}</strong></div>
    <div class="metric"><span>Session</span><strong>{_e(binding.get('session_id'))}</strong></div>
    <div class="metric"><span>Samples</span><strong>{_e(binding.get('sample_count'))}</strong></div>
    <div class="metric"><span>Session SHA-256</span><strong>{_e(binding.get('engineer_session_sha256'))}</strong></div>
  </div></section>
  <section><h2>Readiness</h2><div class="grid">{readiness_cards}</div></section>
  <section><h2>Race strategy</h2>
    <p class="meta">Only authoritative M2 recommendations are shown. Fuel-model smoke candidates and
      scenario numbers are intentionally excluded.</p>
    {_recommendation_cards(strategy.get('recommendations'), empty='No M2 strategy advice passed every gate.')}
    <h3>Strategy capabilities</h3>{_capability_table(strategy.get('capabilities'))}
  </section>
  <section><h2>Fuel model</h2>
    <p>Model readiness: <span class="pill">{_e(fuel.get('model_readiness_status'))}</span>. This section
      reports evidence quality only; reader-facing strategy numbers come exclusively from M2.</p>
    {_capability_table(fuel.get('capabilities'))}
  </section>
  <section><h2>Pit and stint evidence</h2>
    <p>Status: <span class="pill">{_e(pit_stint.get('status'))}</span></p>
    {_pit_stint_table(pit_stint.get('stints'))}
    <h3>Capabilities</h3>{_capability_table(pit_stint.get('capabilities'))}
  </section>
  <section><h2>Driving and corner work</h2>
    {_recommendation_cards(driving.get('cards'), empty='No corner card is available for this session.')}
    <h3>Driving capabilities</h3>{_capability_table(driving.get('capabilities'))}
  </section>
  <section><h2>Advisor timeline</h2>
    <p>Status: <span class="pill">{_e(timeline.get('status'))}</span> · decisions:
      {_e(timeline.get('decision_count'))}</p>
  </section>
  <section><h2>Explicit blockers</h2><ul>{blocker_items}</ul></section>
  <details><summary>Audit payload</summary><p class="muted">Canonical report projection for independent review.</p>
    <pre>{audit_json}</pre></details>
</main>
</body>
</html>
"""
    return document.replace("\r\n", "\n").encode("utf-8")


def _validate_output_path(path: Path, label: str) -> Path:
    if path.name in {"", ".", ".."}:
        _fail("OUTPUT_PATH_INVALID", f"{label} must name one file")
    try:
        absolute_parent = path.parent.absolute()
        resolved_parent = path.parent.resolve(strict=True)
        parent_metadata = os.lstat(absolute_parent)
    except OSError as exc:
        raise EngineerSessionReportError(
            "OUTPUT_PARENT_OPEN_FAILED",
            f"cannot inspect {label} parent safely: {exc}",
        ) from exc
    if (
        os.path.normcase(str(absolute_parent)) != os.path.normcase(str(resolved_parent))
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or int(getattr(parent_metadata, "st_file_attributes", 0)) & 0x400
    ):
        _fail(
            "OUTPUT_PARENT_OPEN_FAILED",
            f"{label} parent must be one real non-reparse directory",
        )
    return absolute_parent / path.name


def _write_bytes_exclusive(path: Path, payload: bytes, label: str) -> None:
    output = _validate_output_path(path, label)
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(output, flags, 0o600)
        except OSError as exc:
            raise EngineerSessionReportError(
                "OUTPUT_CREATE_FAILED",
                f"cannot CreateNew {label}: {exc}",
            ) from exc
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or int(getattr(metadata, "st_file_attributes", 0)) & 0x400
        ):
            _fail("OUTPUT_CREATE_FAILED", f"new {label} is not a regular file")
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError(f"short write while persisting {label}")
            remaining = remaining[written:]
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if after.st_size != len(payload) or after.st_nlink != 1:
            _fail("OUTPUT_CONTENT_CHANGED", f"{label} metadata changed during write")
        os.lseek(descriptor, 0, os.SEEK_SET)
        readback = bytearray()
        while len(readback) < len(payload):
            chunk = os.read(descriptor, min(1_048_576, len(payload) - len(readback)))
            if not chunk:
                break
            readback.extend(chunk)
        if bytes(readback) != payload:
            _fail("OUTPUT_CONTENT_CHANGED", f"{label} failed same-handle readback")
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def write_engineer_session_report_bundle_exclusive(
    artifact_path: str | Path,
    html_path: str | Path,
    report: object,
    engineer_session: object,
    *,
    expected_report_sha256: str | None = None,
    expected_engineer_session_sha256: str | None = None,
) -> None:
    """CreateNew-write a validated JSON artifact and self-contained HTML report.

    Existing paths are never overwritten or removed.  If an external actor
    causes the second CreateNew operation to fail, the first forensic artifact
    is intentionally left in place rather than unlinked through a raced path.
    """

    validated = validate_engineer_session_report(
        report,
        engineer_session,
        expected_report_sha256=expected_report_sha256,
        expected_engineer_session_sha256=expected_engineer_session_sha256,
    )
    artifact = Path(artifact_path)
    rendered = Path(html_path)
    artifact_absolute = _validate_output_path(artifact, "report artifact")
    rendered_absolute = _validate_output_path(rendered, "report HTML")
    if os.path.normcase(str(artifact_absolute)) == os.path.normcase(
        str(rendered_absolute)
    ):
        _fail("OUTPUT_PATH_INVALID", "report artifact and HTML paths must differ")
    if artifact_absolute.exists() or rendered_absolute.exists():
        _fail("OUTPUT_CREATE_FAILED", "report output path already exists")
    html_payload = render_engineer_session_report_html(
        validated,
        engineer_session,
        expected_report_sha256=validated["report_sha256"],
        expected_engineer_session_sha256=expected_engineer_session_sha256,
    )
    _write_bytes_exclusive(artifact_absolute, _persisted_json(validated), "report artifact")
    _write_bytes_exclusive(rendered_absolute, html_payload, "report HTML")


__all__ = [
    "ENGINEER_SESSION_REPORT_CONTRACT_VERSION",
    "EngineerSessionReportError",
    "build_engineer_session_report",
    "render_engineer_session_report_html",
    "validate_engineer_session_report",
    "write_engineer_session_report_bundle_exclusive",
]

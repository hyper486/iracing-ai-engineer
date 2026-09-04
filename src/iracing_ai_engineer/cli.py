"""Command-line entry points for offline analysis and read-only live collection."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

from .collector import (
    COLLECTOR_CONTRACT_VERSION,
    CollectorConsistencyError,
    collect_transport_to_jsonl,
)
from .contracts import NORMALIZATION_PROFILE_VERSION, SDK_PROBE_CONTRACT_VERSION
from .live_monitor import (
    LIVE_MONITOR_CONTRACT_VERSION,
    LiveMonitorError,
    monitor_live_transport,
)
from .sdk_probe import (
    SdkProbeConsistencyError,
    SdkProbeUnavailable,
    WindowsPyirsdkTransport,
    probe_live_sdk,
)
from .telemetry import TELEMETRY_CONTRACT_VERSION, SourceKind

EVENT_REPLAY_CONTRACT_VERSION = "event-replay-v1"

_PUBLIC_AUDI_SPA_PRESET = "public-audi-spa"
_PUBLIC_AUDI_SPA_ASSET_ID = "public-audi-r8-evo2-spa"
_PUBLIC_AUDI_SPA_SESSION_ID = "public-fixture-2023-12-race"
_PUBLIC_AUDI_SPA_SOURCE_SHA256 = "754d14e6e2870eb00d42368e36c7a15495cb68bfd5f829ab08665d0f95fc7f36"
_PUBLIC_AUDI_SPA_BYTE_SIZE = 162_304_117
_PUBLIC_AUDI_SPA_LABEL_PATH = Path("data/labels/candidates/audi-spa-v1.candidate.json")
_PUBLIC_AUDI_SPA_LABEL_ARTIFACT_SHA256 = (
    "7cbf488e74220df163b23c6e79544ac28654adf91158d035ee412faced14d8dc"
)
_PUBLIC_AUDI_SPA_LABEL_CANDIDATE_SHA256 = (
    "f30ea24e0b52400b704e91c1ae385f8d903d9d2ff6ec67e6c2039baa1690cdfa"
)
_PUBLIC_AUDI_SPA_TARGET_LAP = 11
_PUBLIC_AUDI_SPA_GRID_STEP_M = 1.0
_PUBLIC_AUDI_SPA_TOP = 3
_PUBLIC_AUDI_SPA_SCENARIO_SUMMARY = {
    "current_fuel_l": 20.0,
    "purpose": "DETERMINISTIC_SMOKE_ONLY_NOT_EVENT_TRUTH",
    "refuel_rate_l_per_s": 2.0,
    "remaining_laps": 10,
    "reserve_l": 1.0,
    "tank_capacity_l": 120.0,
}
_PUBLIC_AUDI_SPA_COMPONENT_HASHES = {
    "condition_cohort_sha256": ("83a20c37b40630e2295630ab54f08d7cbabc63ae8dc6ca7ef4245c3773d4d337"),
    "condition_config_sha256": ("8c89e01fab4db83c4111662169b4525933106db08de318b617d558383a8e1a5f"),
    "condition_provenance_sha256": (
        "074a8f8c34f8eb557ee970950d2d727515628ca9e84280ed7dc818ee862b85bb"
    ),
    "condition_semantic_sha256": (
        "c2f0f9e14445e73862036d6701fba4f7e80b409581e2805205edd2f4ec8d50cb"
    ),
    "driving_model_output_sha256": (
        "f7a7165b19dfa08f1576b3f2e495cfbedb2011aa32317b91d3eb725967af3195"
    ),
    "driving_model_semantic_sha256": (
        "74f6f52d5743260cbdcedaa59a0e0620afb1d8c8987195009e31f7cb86399df6"
    ),
    "driving_replay_sha256": ("c5a8f19f156c57c3951e112df24ad3e3f07956961b78c68fe972a534955ebb82"),
    "fuel_model_output_sha256": (
        "5e483f4f3987a542ca296553fc710bbd04038fd3dee2bf3d7a578f5ae1c76c15"
    ),
    "fuel_model_semantic_sha256": (
        "d68fe9387c7d83db9f6425503f06be98c116d2cab18b63216963a6fa9ec76fe5"
    ),
    "fuel_replay_sha256": ("1f3b642c43dd6b7cd16e433dee3f26335f9aecd0c83950e02b706a2f79c3a65c"),
    "label_artifact_sha256": _PUBLIC_AUDI_SPA_LABEL_ARTIFACT_SHA256,
    "label_candidate_payload_sha256": _PUBLIC_AUDI_SPA_LABEL_CANDIDATE_SHA256,
    "shadow_analysis_sha256": ("5d521bdf7443fe5be9e8ab27cd29254bc0ea56ee0dedf2ddd16eda89224554b6"),
}


def _finite_nonnegative(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than or equal to zero")
    return value


def _finite_positive(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return value


def _nonnegative_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be an integer greater than or equal to zero")
    return value


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _probability(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or not 0.5 <= value <= 1.0:
        raise argparse.ArgumentTypeError("must be a finite number between 0.5 and 1.0")
    return value


def _run_identifier(raw: str) -> str:
    if (
        not raw
        or raw != raw.strip()
        or len(raw.encode("utf-8")) > 256
        or any(ord(character) < 32 for character in raw)
    ):
        raise argparse.ArgumentTypeError(
            "must be a non-empty identifier without outer whitespace or controls"
        )
    return raw


def _sha256_digest(raw: str) -> str:
    value = raw.casefold()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise argparse.ArgumentTypeError("must be a 64-character SHA-256 digest")
    return value


def _add_fuel_scenario_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--current-fuel-l", type=_finite_nonnegative)
    parser.add_argument("--tank-capacity-l", type=_finite_positive)
    parser.add_argument("--refuel-rate-lps", type=_finite_positive)
    remaining = parser.add_mutually_exclusive_group()
    remaining.add_argument("--remaining-laps", type=_nonnegative_int)
    remaining.add_argument("--remaining-time-s", type=_finite_nonnegative)
    parser.add_argument("--reference-lap-time-s", type=_finite_positive)
    parser.add_argument("--reserve-l", type=_finite_nonnegative, default=1.0)
    parser.add_argument("--fuel-quantile", type=_probability, default=0.90)
    parser.add_argument("--minimum-fuel-laps", type=_positive_int, default=5)
    parser.add_argument("--timed-race-extra-laps", type=_nonnegative_int, default=1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iracing-aie")
    subcommands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subcommands.add_parser(
        "inspect", help="profile one or more immutable .ibt files"
    )
    inspect_parser.add_argument("paths", type=Path, nargs="+")
    inspect_parser.add_argument(
        "--full", action="store_true", help="include field, lap, and gate detail"
    )

    replay_parser = subcommands.add_parser("replay", help="produce a deterministic replay receipt")
    replay_parser.add_argument("path", type=Path)
    replay_parser.add_argument("--frame-hash-chunk-size", type=int, default=4096)

    events_parser = subcommands.add_parser(
        "events",
        help="validate and replay .ibt or collector JSONL through the shared event pipeline",
        description=(
            "Fully validate an immutable IBT or collector JSONL input, normalize it, "
            "and run the same deterministic event state machine used by live consumers."
        ),
    )
    events_parser.add_argument("path", type=Path)
    events_parser.add_argument(
        "--input-kind",
        choices=("auto", "ibt", "collector"),
        default="auto",
    )
    events_parser.add_argument(
        "--source-id",
        type=_run_identifier,
        help="required for IBT; collector identity is bound by its run record",
    )
    events_parser.add_argument(
        "--session-id",
        type=_run_identifier,
        help="required for IBT; collector identity is bound by its run record",
    )
    events_parser.add_argument("--stale-after-seconds", type=_finite_positive, default=0.5)
    events_parser.add_argument(
        "--allow-incomplete-collector",
        action="store_true",
        help="recovery mode: accept a fully valid newline-complete crash prefix",
    )
    events_parser.add_argument(
        "--include-events",
        action="store_true",
        help="include every normalized event in addition to the deterministic receipt",
    )

    shadow_parser = subcommands.add_parser(
        "shadow", help="run non-executable fuel and driving analysis on an .ibt"
    )
    shadow_parser.add_argument("path", type=Path)
    shadow_parser.add_argument("--analysis", choices=("fuel", "driving", "all"), default="all")
    _add_fuel_scenario_arguments(shadow_parser)
    shadow_parser.add_argument("--grid-m", type=_finite_positive, default=1.0)
    shadow_parser.add_argument("--top", type=_positive_int, default=3)
    shadow_parser.add_argument("--receipt-only", action="store_true")
    shadow_parser.add_argument(
        "--require-capability",
        action="append",
        choices=(
            "fuel_model_smoke",
            "driving_analysis_smoke",
            "personalized_coaching",
            "opponent_fuel",
            "traffic_model",
            "current_tire_wear",
            "race_recommendation",
        ),
        default=[],
    )

    offline_demo_parser = subcommands.add_parser(
        "offline-demo",
        help="run the provenance-bound Audi/Spa offline engineer demonstration",
        description=(
            "Verify the frozen public Audi/Spa asset and pending label candidate, "
            "then run all advisor-only offline components through one cross-bound receipt."
        ),
    )
    offline_demo_parser.add_argument(
        "--preset",
        choices=(_PUBLIC_AUDI_SPA_PRESET,),
        required=True,
    )
    offline_demo_parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/public_sources.json"),
        help="strict public-source manifest (default: data/public_sources.json)",
    )
    offline_demo_parser.add_argument(
        "--output",
        type=Path,
        help="optional new JSON artifact path; an existing path is never overwritten",
    )
    offline_demo_parser.add_argument(
        "--require-trusted",
        action="store_true",
        help="exit 5 unless offline, condition-trust, and label-trust gates all pass",
    )

    fuel_replay_parser = subcommands.add_parser(
        "fuel-replay",
        help="run IBT or collector telemetry through one shared shadow fuel model",
        description=(
            "Fully validate and normalize an IBT or collector JSONL source, then run "
            "the shared event, lap-feature, and fuel-strategy pipeline. Output is "
            "shadow-only and never controls iRacing."
        ),
    )
    fuel_replay_parser.add_argument("path", type=Path)
    fuel_replay_parser.add_argument(
        "--input-kind", choices=("auto", "ibt", "collector"), default="auto"
    )
    fuel_replay_parser.add_argument(
        "--source-id",
        type=_run_identifier,
        help="required for IBT; collector identity is bound by its run record",
    )
    fuel_replay_parser.add_argument(
        "--session-id",
        type=_run_identifier,
        help="required for IBT; collector identity is bound by its run record",
    )
    fuel_replay_parser.add_argument("--stale-after-seconds", type=_finite_positive, default=0.5)
    _add_fuel_scenario_arguments(fuel_replay_parser)
    fuel_replay_parser.add_argument("--receipt-only", action="store_true")
    fuel_replay_parser.add_argument(
        "--require-ready",
        action="store_true",
        help="exit 5 unless the shared fuel model capability passes",
    )

    driving_replay_parser = subcommands.add_parser(
        "driving-replay",
        help="run IBT or collector telemetry through one shared shadow driving model",
        description=(
            "Fully validate and normalize an IBT or collector JSONL source, bind "
            "track length from validated SessionInfo, then run the shared event, "
            "lap-feature, and distance-domain driving pipeline. Output is descriptive "
            "practice evidence only and never controls iRacing."
        ),
    )
    driving_replay_parser.add_argument("path", type=Path)
    driving_replay_parser.add_argument(
        "--input-kind", choices=("auto", "ibt", "collector"), default="auto"
    )
    driving_replay_parser.add_argument(
        "--source-id",
        type=_run_identifier,
        help="required for IBT; collector identity is bound by its run record",
    )
    driving_replay_parser.add_argument(
        "--session-id",
        type=_run_identifier,
        help="required for IBT; collector identity is bound by its run record",
    )
    driving_replay_parser.add_argument("--stale-after-seconds", type=_finite_positive, default=0.5)
    driving_replay_parser.add_argument("--grid-m", type=_finite_positive, default=1.0)
    driving_replay_parser.add_argument("--top", type=_positive_int, default=3)
    driving_replay_parser.add_argument("--receipt-only", action="store_true")
    driving_replay_parser.add_argument(
        "--require-ready",
        action="store_true",
        help="exit 5 unless the shared driving model capability passes",
    )

    condition_cohort_parser = subcommands.add_parser(
        "condition-cohort",
        help="match clean laps across five directly observed condition dimensions",
        description=(
            "Build a provenance-bound condition cohort from one fully validated IBT "
            "or collector input. The matcher keeps the fixed minimum of eight laps, "
            "does not approve labels, and emits no driving recommendation."
        ),
    )
    condition_cohort_parser.add_argument("path", type=Path)
    condition_cohort_parser.add_argument(
        "--input-kind", choices=("auto", "ibt", "collector"), default="auto"
    )
    condition_cohort_parser.add_argument(
        "--source-id",
        type=_run_identifier,
        help="required for IBT; collector identity is bound by its run record",
    )
    condition_cohort_parser.add_argument(
        "--session-id",
        type=_run_identifier,
        help="required for IBT; collector identity is bound by its run record",
    )
    condition_cohort_parser.add_argument(
        "--stale-after-seconds", type=_finite_positive, default=0.5
    )
    condition_cohort_parser.add_argument(
        "--target-lap-ordinal", type=_nonnegative_int, required=True
    )
    condition_cohort_parser.add_argument(
        "--track-state-labels",
        type=Path,
        help="optional strict source-bound ApprovedTrackStateLabelSet JSON",
    )
    condition_cohort_parser.add_argument("--receipt-only", action="store_true")
    condition_cohort_parser.add_argument(
        "--require-ready",
        action="store_true",
        help="exit 5 unless trusted_readiness_status is PASS",
    )

    driving_labels_parser = subcommands.add_parser(
        "driving-labels",
        help="propose, validate, or regress human-reviewed driving labels",
        description=(
            "Keep model-generated corner proposals separate from independently "
            "reviewed labels. This command never approves labels or manufactures "
            "human-review evidence."
        ),
    )
    driving_labels_actions = driving_labels_parser.add_subparsers(
        dest="driving_labels_action", required=True
    )
    labels_propose_parser = driving_labels_actions.add_parser(
        "propose",
        help="freeze a PENDING candidate from one complete driving replay JSON",
    )
    labels_propose_parser.add_argument("replay", type=Path)
    labels_propose_parser.add_argument("--output", type=Path, required=True)
    labels_propose_parser.add_argument("--label-set-id", required=True)
    labels_propose_parser.add_argument("--car-key", required=True)
    labels_propose_parser.add_argument("--track-key", required=True)
    labels_propose_parser.add_argument("--layout-key", required=True)
    labels_propose_parser.add_argument("--condition-scope", default="DRY_UNMATCHED_SMOKE_ONLY")
    labels_validate_parser = driving_labels_actions.add_parser(
        "validate", help="strictly validate a driving-label artifact and its hashes"
    )
    labels_validate_parser.add_argument("labels", type=Path)
    labels_regress_parser = driving_labels_actions.add_parser(
        "regress",
        help="compare an approved label artifact with a driving replay JSON",
    )
    labels_regress_parser.add_argument("labels", type=Path)
    labels_regress_parser.add_argument("replay", type=Path)

    probe_parser = subcommands.add_parser(
        "sdk-probe", help="read-only probe of iRacing's Windows shared-memory SDK"
    )
    probe_parser.add_argument("--wait-seconds", type=_finite_nonnegative, default=20.0)
    probe_parser.add_argument("--sample-seconds", type=_finite_positive, default=3.0)
    probe_parser.add_argument("--poll-seconds", type=_finite_positive, default=0.05)
    probe_parser.add_argument("--full-schema", action="store_true")
    probe_parser.add_argument(
        "--require-capability",
        choices=(
            "sdk_connection",
            "session_clock",
            "replay_control_only",
            "lap_position",
            "driving_controls",
            "fuel_direct",
            "race_strategy_core",
            "opponent_tracking",
        ),
    )

    live_monitor_parser = subcommands.add_parser(
        "monitor-live",
        help="stream privacy-safe advisor state from the read-only live SDK",
        description=(
            "Normalize every distinct SDK tick, then emit bounded JSONL status "
            "snapshots for a future overlay or speech process. No raw telemetry is "
            "persisted and no simulator, vehicle, or pit control is available."
        ),
    )
    live_monitor_parser.add_argument("--source-id", required=True, type=_run_identifier)
    live_monitor_parser.add_argument("--session-id", required=True, type=_run_identifier)
    live_monitor_parser.add_argument(
        "--expected-source-kind",
        choices=("auto", "live", "replay"),
        default="auto",
        help="fail closed if the observed simulator mode differs (default: auto)",
    )
    live_monitor_parser.add_argument(
        "--wait-seconds", type=_finite_nonnegative, default=20.0
    )
    live_monitor_parser.add_argument(
        "--duration-seconds", type=_finite_positive, default=30.0
    )
    live_monitor_parser.add_argument(
        "--poll-seconds", type=_finite_positive, default=0.01
    )
    live_monitor_parser.add_argument(
        "--snapshot-seconds", type=_finite_positive, default=0.5
    )
    live_monitor_parser.add_argument(
        "--stale-after-seconds", type=_finite_positive, default=0.5
    )
    live_monitor_parser.add_argument(
        "--require-in-car",
        action="store_true",
        help="exit 5 after the terminal receipt unless an in-car snapshot was observed",
    )

    live_preflight_parser = subcommands.add_parser(
        "live-preflight",
        help="run a noncanonical real-SDK canary before a long live capture",
        description=(
            "Exercise the exact Windows SDK transport, collector, adapter, event, "
            "and driving pipelines into a preflight-*.jsonl file. The resulting "
            "PRECHECK_ONLY receipt can gate capture start but is never live "
            "acceptance evidence. Synthetic provenance is not exposed by this CLI."
        ),
    )
    live_preflight_parser.add_argument(
        "output",
        type=Path,
        help="new noncanonical preflight-*.jsonl path; never a live-*.jsonl path",
    )
    live_preflight_parser.add_argument("--source-id", required=True, type=_run_identifier)
    live_preflight_parser.add_argument("--session-id", required=True, type=_run_identifier)
    live_preflight_parser.add_argument(
        "--expected-sim-process-id", required=True, type=_positive_int
    )
    live_preflight_parser.add_argument(
        "--expected-sim-start-time-utc-ticks", required=True, type=_positive_int
    )
    live_preflight_parser.add_argument(
        "--expected-windows-session-id", required=True, type=_nonnegative_int
    )
    live_preflight_parser.add_argument(
        "--wait-seconds", type=_finite_nonnegative, default=120.0
    )
    live_preflight_parser.add_argument(
        "--duration-seconds", type=_finite_positive, default=30.0
    )
    live_preflight_parser.add_argument(
        "--poll-seconds", type=_finite_positive, default=0.01
    )
    live_preflight_parser.add_argument(
        "--stale-after-seconds", type=_finite_positive, default=0.5
    )

    collect_parser = subcommands.add_parser(
        "collect-live",
        help="collect read-only live/replay SDK telemetry to a new JSONL file",
        description=(
            "Collect read-only iRacing SDK telemetry into an exclusively created JSONL "
            "file. This command must run in the same logged-in Windows console session "
            "as iRacing; SSH and service sessions cannot access that session-local SDK map."
        ),
    )
    collect_parser.add_argument(
        "output",
        type=Path,
        help="new JSONL output path; an existing path is never overwritten",
    )
    collect_parser.add_argument("--source-id", required=True, type=_run_identifier)
    collect_parser.add_argument("--session-id", required=True, type=_run_identifier)
    collect_parser.add_argument(
        "--expected-source-kind",
        choices=("auto", "live", "replay"),
        default="auto",
        help="fail closed if the observed simulator mode differs (default: auto)",
    )
    collect_parser.add_argument("--wait-seconds", type=_finite_nonnegative, default=20.0)
    collect_parser.add_argument("--duration-seconds", type=_finite_positive, default=60.0)
    collect_parser.add_argument("--poll-seconds", type=_finite_positive, default=0.01)
    collect_parser.add_argument("--stale-after-seconds", type=_finite_positive, default=0.5)

    subcommands.add_parser(
        "supervise-r8",
        help="run the protected zero-argument R8 single-process live supervisor",
        description=(
            "Production-only entrypoint for the administrator-protected embedded "
            "Windows Python runtime. It accepts no path, hash, timing, transport, "
            "or other caller-controlled security parameter."
        ),
    )

    session_report_parser = subcommands.add_parser(
        "session-report",
        help="render a validated engineer session as an advisor-only local report",
        description=(
            "Validate and independently bind one engineer-session-v1 artifact, then "
            "CreateNew-write a deterministic JSON report and self-contained HTML. "
            "Only M2 strategy recommendations are reader-facing; fuel smoke candidates "
            "are excluded and no vehicle or pit-box control is available."
        ),
    )
    session_report_parser.add_argument("session", type=Path)
    session_report_parser.add_argument(
        "--expected-engineer-session-sha256",
        type=_sha256_digest,
        required=True,
        help="independently retained engineer-session SHA-256",
    )
    session_report_parser.add_argument(
        "--artifact-output",
        type=Path,
        required=True,
        help="new deterministic JSON report path; never overwritten",
    )
    session_report_parser.add_argument(
        "--html-output",
        type=Path,
        required=True,
        help="new self-contained HTML report path; never overwritten",
    )
    session_report_parser.add_argument(
        "--require-advice",
        action="store_true",
        help="exit 5 unless strategy or practice advice is available",
    )

    live_session_report_parser = subcommands.add_parser(
        "live-session-report",
        help="replay a retrieved SDK_LIVE capture into a safe local report",
        description=(
            "Object-exact replay a sealed capture against its independently bound "
            "live-engineer-session proof. The complete engineer session remains "
            "in memory; only the advisor-only JSON and script-free HTML projection "
            "is persisted."
        ),
    )
    live_session_report_parser.add_argument("capture", type=Path)
    live_session_report_parser.add_argument(
        "--live-session",
        type=Path,
        required=True,
        help="retrieved live-engineer-session-v1 JSON proof",
    )
    live_session_report_parser.add_argument(
        "--expected-live-engineer-session-sha256",
        type=_sha256_digest,
        required=True,
        help="independently retained live proof SHA-256",
    )
    live_session_report_parser.add_argument(
        "--expected-remote-capture-sha256",
        type=_sha256_digest,
        required=True,
        help="producer-side capture SHA-256 retained outside the downloaded file",
    )
    live_session_report_parser.add_argument(
        "--expected-remote-capture-byte-size",
        type=_positive_int,
        required=True,
        help="producer-side capture byte size",
    )
    live_session_report_parser.add_argument(
        "--artifact-output",
        type=Path,
        required=True,
        help="new deterministic JSON report path; never overwritten",
    )
    live_session_report_parser.add_argument(
        "--html-output",
        type=Path,
        required=True,
        help="new self-contained HTML report path; never overwritten",
    )
    live_session_report_parser.add_argument(
        "--stale-after-seconds",
        type=_finite_positive,
        default=0.5,
    )
    live_session_report_parser.add_argument(
        "--require-advice",
        action="store_true",
        help="exit 5 unless strategy or practice advice is available",
    )

    profile_parser = subcommands.add_parser(
        "make-live-analysis-profile",
        help="CreateNew-write a hashed user/event profile for live finalization",
        description=(
            "Build the exact user/event configuration accepted by "
            "finalize-live-analysis. Current fuel and the decision clock are never "
            "accepted here; they must come from the SDK_LIVE capture."
        ),
    )
    profile_parser.add_argument("output", type=Path)
    profile_parser.add_argument("--profile-id", type=_run_identifier, required=True)
    profile_parser.add_argument("--profile-version", type=_positive_int, default=1)
    profile_parser.add_argument("--tank-capacity-l", type=_finite_positive, required=True)
    profile_parser.add_argument(
        "--refuel-rate-lps", type=_finite_positive, required=True
    )
    profile_parser.add_argument("--reserve-l", type=_finite_nonnegative, default=1.0)
    profile_parser.add_argument("--fuel-quantile", type=_probability, default=0.9)
    profile_parser.add_argument(
        "--minimum-fuel-laps", type=_positive_int, default=5
    )
    profile_parser.add_argument(
        "--timed-race-extra-laps", type=_nonnegative_int, default=1
    )
    profile_horizon = profile_parser.add_mutually_exclusive_group()
    profile_horizon.add_argument("--remaining-laps", type=_nonnegative_int)
    profile_horizon.add_argument("--remaining-time-s", type=_finite_nonnegative)
    profile_parser.add_argument("--reference-lap-time-s", type=_finite_positive)

    calibration_parser = subcommands.add_parser(
        "build-pit-calibration",
        help="CreateNew-write an identity-bound matched pit/service model",
        description=(
            "Validate at least three independently labelled matched pit samples, "
            "then derive the exact calibration-model object consumed by M2."
        ),
    )
    calibration_parser.add_argument("dataset", type=Path)
    calibration_parser.add_argument(
        "--expected-dataset-sha256", type=_sha256_digest, required=True
    )
    calibration_parser.add_argument("--output", type=Path, required=True)

    tire_performance_parser = subcommands.add_parser(
        "build-tire-performance-model",
        help="CreateNew-write a matched, fuel-adjusted tire-performance model",
        description=(
            "Validate at least three disjoint condition-matched stint pairs and a "
            "self-hashed fuel-load correction, then derive a shadow performance-age "
            "envelope. This never promotes the envelope to current physical wear."
        ),
    )
    tire_performance_parser.add_argument("dataset", type=Path)
    tire_performance_parser.add_argument(
        "--expected-dataset-sha256", type=_sha256_digest, required=True
    )
    tire_performance_parser.add_argument("--output", type=Path, required=True)

    finalize_live_parser = subcommands.add_parser(
        "finalize-live-analysis",
        help="build a complete advisor-only analysis bundle from a sealed SDK_LIVE run",
        description=(
            "Object-exactly replay a retrieved R8 live proof and its producer-bound "
            "capture, combine SDK-direct fuel/horizon evidence with one independently "
            "hashed user/event profile, and CreateNew-write an engineer session, JSON "
            "report, script-free HTML, and cross-artifact bundle receipt. Missing rules, "
            "calibration, traffic, penalty, or driving evidence remains an explicit WAIT."
        ),
    )
    finalize_live_parser.add_argument("capture", type=Path)
    finalize_live_parser.add_argument("--live-session", type=Path, required=True)
    finalize_live_parser.add_argument("--analysis-profile", type=Path, required=True)
    finalize_live_parser.add_argument(
        "--expected-live-engineer-session-sha256",
        type=_sha256_digest,
        required=True,
    )
    finalize_live_parser.add_argument(
        "--expected-remote-capture-sha256",
        type=_sha256_digest,
        required=True,
    )
    finalize_live_parser.add_argument(
        "--expected-remote-capture-byte-size",
        type=_positive_int,
        required=True,
    )
    finalize_live_parser.add_argument(
        "--expected-analysis-profile-sha256",
        type=_sha256_digest,
        required=True,
    )
    finalize_live_parser.add_argument("--calibration-model", type=Path)
    finalize_live_parser.add_argument(
        "--expected-calibration-model-sha256", type=_sha256_digest
    )
    finalize_live_parser.add_argument(
        "--expected-calibration-source-receipt-sha256", type=_sha256_digest
    )
    finalize_live_parser.add_argument("--tire-performance-model", type=Path)
    finalize_live_parser.add_argument(
        "--expected-tire-performance-model-sha256", type=_sha256_digest
    )
    finalize_live_parser.add_argument(
        "--expected-tire-performance-source-receipt-sha256",
        type=_sha256_digest,
    )
    finalize_live_parser.add_argument("--rules-profile", type=Path)
    finalize_live_parser.add_argument(
        "--expected-rules-profile-sha256", type=_sha256_digest
    )
    finalize_live_parser.add_argument(
        "--expected-rules-source-sha256", type=_sha256_digest
    )
    finalize_live_parser.add_argument("--previous-m2-receipt", type=Path)
    finalize_live_parser.add_argument(
        "--expected-previous-m2-sha256", type=_sha256_digest
    )
    finalize_live_parser.add_argument(
        "--expected-previous-revision", type=_positive_int
    )
    finalize_live_parser.add_argument(
        "--session-output", type=Path, required=True
    )
    finalize_live_parser.add_argument(
        "--artifact-output", type=Path, required=True
    )
    finalize_live_parser.add_argument("--html-output", type=Path, required=True)
    finalize_live_parser.add_argument(
        "--receipt-output", type=Path, required=True
    )
    finalize_live_parser.add_argument(
        "--stale-after-seconds", type=_finite_positive, default=0.5
    )
    finalize_live_parser.add_argument(
        "--require-strategy-advice",
        action="store_true",
        help="exit 5 after writing evidence unless at least one M2 strategy advice exists",
    )
    finalize_live_parser.add_argument(
        "--require-driving-advice",
        action="store_true",
        help="exit 5 after writing evidence unless at least one practice action exists",
    )

    verify_live_parser = subcommands.add_parser(
        "verify-live-analysis",
        help="object-exactly replay a complete retrieved SDK_LIVE analysis bundle",
        description=(
            "Read-only verification of the capture, R8 live proof, analysis profile, "
            "engineer session, report JSON, HTML bytes, and final bundle receipt."
        ),
    )
    verify_live_parser.add_argument("capture", type=Path)
    verify_live_parser.add_argument("--live-session", type=Path, required=True)
    verify_live_parser.add_argument("--analysis-profile", type=Path, required=True)
    verify_live_parser.add_argument("--engineer-session", type=Path, required=True)
    verify_live_parser.add_argument("--report-artifact", type=Path, required=True)
    verify_live_parser.add_argument("--report-html", type=Path, required=True)
    verify_live_parser.add_argument("--bundle-receipt", type=Path, required=True)
    verify_live_parser.add_argument(
        "--expected-live-engineer-session-sha256",
        type=_sha256_digest,
        required=True,
    )
    verify_live_parser.add_argument(
        "--expected-remote-capture-sha256",
        type=_sha256_digest,
        required=True,
    )
    verify_live_parser.add_argument(
        "--expected-remote-capture-byte-size",
        type=_positive_int,
        required=True,
    )
    verify_live_parser.add_argument(
        "--expected-analysis-profile-sha256",
        type=_sha256_digest,
        required=True,
    )
    verify_live_parser.add_argument(
        "--expected-bundle-receipt-sha256",
        type=_sha256_digest,
        required=True,
    )
    verify_live_parser.add_argument("--calibration-model", type=Path)
    verify_live_parser.add_argument(
        "--expected-calibration-model-sha256", type=_sha256_digest
    )
    verify_live_parser.add_argument(
        "--expected-calibration-source-receipt-sha256", type=_sha256_digest
    )
    verify_live_parser.add_argument("--tire-performance-model", type=Path)
    verify_live_parser.add_argument(
        "--expected-tire-performance-model-sha256", type=_sha256_digest
    )
    verify_live_parser.add_argument(
        "--expected-tire-performance-source-receipt-sha256",
        type=_sha256_digest,
    )
    verify_live_parser.add_argument("--rules-profile", type=Path)
    verify_live_parser.add_argument(
        "--expected-rules-profile-sha256", type=_sha256_digest
    )
    verify_live_parser.add_argument(
        "--expected-rules-source-sha256", type=_sha256_digest
    )
    verify_live_parser.add_argument("--previous-m2-receipt", type=Path)
    verify_live_parser.add_argument(
        "--expected-previous-m2-sha256", type=_sha256_digest
    )
    verify_live_parser.add_argument(
        "--expected-previous-revision", type=_positive_int
    )
    verify_live_parser.add_argument(
        "--stale-after-seconds", type=_finite_positive, default=0.5
    )
    verify_live_parser.add_argument(
        "--require-strategy-advice", action="store_true"
    )
    verify_live_parser.add_argument(
        "--require-driving-advice", action="store_true"
    )

    m0_parser = subcommands.add_parser(
        "m0-accept",
        help="validate the first Windows live capture and its provenance receipts",
        description=(
            "Snapshot and validate one COMPLETE live collector capture, verify its "
            "Windows install/launch receipts, and replay it twice in fresh processes "
            "through the shared event and shadow fuel pipelines."
        ),
    )
    m0_parser.add_argument("capture", type=Path)
    m0_parser.add_argument("--launch-receipt", type=Path, required=True)
    m0_parser.add_argument("--install-manifest", type=Path, required=True)
    m0_parser.add_argument("--performance-receipt", type=Path)
    m0_parser.add_argument("--expected-installer-sha256", type=_sha256_digest, required=True)
    m0_parser.add_argument("--expected-launcher-sha256", type=_sha256_digest, required=True)
    m0_parser.add_argument("--expected-requirements-sha256", type=_sha256_digest, required=True)
    m0_parser.add_argument("--expected-wheel-sha256", type=_sha256_digest, required=True)
    m0_parser.add_argument(
        "--expected-wheelhouse-manifest-sha256",
        type=_sha256_digest,
        required=True,
    )
    m0_parser.add_argument("--minimum-capture-seconds", type=_finite_positive, default=30.0)
    m0_parser.add_argument("--subprocess-timeout-seconds", type=_finite_positive, default=600.0)
    _add_fuel_scenario_arguments(m0_parser)
    m0_parser.add_argument(
        "--require-pass",
        action="store_true",
        help="exit 5 unless core validation and an external performance A/B both pass",
    )
    return parser


def _collector_source_kind(value: str) -> SourceKind | None:
    return {
        "auto": None,
        "live": SourceKind.SDK_LIVE,
        "replay": SourceKind.REPLAY_SDK_PROXY,
    }[value]


def _event_input_kind(path: Path, requested: str) -> str:
    if requested != "auto":
        return requested
    suffix = path.suffix.casefold()
    if suffix == ".ibt":
        return "ibt"
    if suffix in {".jsonl", ".ndjson"}:
        return "collector"
    raise ValueError(
        "cannot infer event input kind; use --input-kind ibt or --input-kind collector"
    )


def _condition_input_kind(path: Path, requested: str) -> str:
    if requested != "auto":
        return requested
    suffix = path.suffix.casefold()
    if suffix == ".ibt":
        return "ibt"
    if suffix in {".jsonl", ".ndjson"}:
        return "collector"
    raise ValueError(
        "cannot infer condition-cohort input kind; use --input-kind ibt or --input-kind collector"
    )


def _print_error(contract_version: str, error: str, exc: BaseException) -> None:
    print(str(exc), file=sys.stderr)
    print(
        json.dumps(
            {
                "contract_version": contract_version,
                "error": error,
                "message": str(exc),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _strict_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON value is forbidden: {value}")


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(path)
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_json_pairs,
        parse_constant=_reject_json_constant,
    )
    if type(value) is not dict:
        raise ValueError(f"{label} must contain one JSON object")
    return value


def _write_new_json(path: Path, value: object) -> None:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    )
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)
        handle.write("\n")


def _plain_object(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be a plain JSON object")
    return value


def _regular_file_sha256(path: Path, label: str) -> tuple[str, int]:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link: {path}")
    digest = hashlib.sha256()
    byte_size = 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or int(getattr(before, "st_file_attributes", 0)) & 0x400
        ):
            raise ValueError(f"{label} must be one real singly-linked file: {path}")
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            byte_size += len(chunk)
        after = os.fstat(handle.fileno())
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity or byte_size != before.st_size:
            raise ValueError(f"{label} changed while it was hashed: {path}")
    return digest.hexdigest(), byte_size


def _read_regular_file_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb", buffering=0) as handle:
        before = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or int(getattr(before, "st_file_attributes", 0)) & 0x400
        ):
            raise ValueError(f"{label} must be one real singly-linked file: {path}")
        payload = bytearray()
        while chunk := handle.read(1024 * 1024):
            payload.extend(chunk)
        after = os.fstat(handle.fileno())
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity or len(payload) != before.st_size:
            raise ValueError(f"{label} changed while it was read: {path}")
    return bytes(payload)


def _read_regular_json_object(path: Path, label: str) -> dict[str, object]:
    payload = _read_regular_file_bytes(path, label)
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{label} must be strict UTF-8 JSON: {path}") from exc
    if type(value) is not dict:
        raise ValueError(f"{label} must contain one JSON object")
    return value


def _manifest_digest(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _offline_demo_preset(manifest_path: Path) -> dict[str, object]:
    """Resolve and validate the one frozen local-only Audi/Spa demo preset."""

    from .fuel import FuelScenario

    if manifest_path.is_symlink():
        raise ValueError(f"offline-demo manifest must not be a symbolic link: {manifest_path}")
    manifest = _read_json_object(manifest_path, "offline-demo manifest")
    data_directory = manifest_path.parent
    if data_directory.name != "data" or data_directory.is_symlink():
        raise ValueError("offline-demo manifest must be a direct file in a real data directory")
    project_root = data_directory.parent

    assets = manifest.get("assets")
    if type(assets) is not list:
        raise ValueError("offline-demo manifest assets must be a JSON array")
    if any(type(item) is not dict for item in assets):
        raise ValueError("offline-demo manifest assets must contain only plain objects")
    matches = [item for item in assets if item.get("asset_id") == _PUBLIC_AUDI_SPA_ASSET_ID]
    if len(matches) != 1:
        raise ValueError(
            f"offline-demo manifest must contain exactly one {_PUBLIC_AUDI_SPA_ASSET_ID!r} asset"
        )
    asset = matches[0]

    local_path = asset.get("local_path")
    if type(local_path) is not str:
        raise ValueError("offline-demo asset local_path must be a string")
    relative_path = Path(local_path)
    if (
        relative_path.is_absolute()
        or relative_path.parent != Path("data/raw")
        or relative_path.suffix.casefold() != ".ibt"
    ):
        raise ValueError("offline-demo asset must name one direct data/raw .ibt file")
    expected_size = asset.get("byte_size")
    if type(expected_size) is not int or expected_size <= 0:
        raise ValueError("offline-demo asset byte_size must be a positive integer")
    expected_source_sha256 = _manifest_digest(asset.get("sha256"), "offline-demo asset sha256")
    if (
        expected_source_sha256 != _PUBLIC_AUDI_SPA_SOURCE_SHA256
        or expected_size != _PUBLIC_AUDI_SPA_BYTE_SIZE
    ):
        raise ValueError(
            "offline-demo manifest asset differs from the package-frozen public-audi-spa trust root"
        )
    raw_path = project_root / relative_path
    actual_source_sha256, actual_size = _regular_file_sha256(raw_path, "offline-demo asset")
    if (actual_source_sha256, actual_size) != (
        expected_source_sha256,
        expected_size,
    ):
        raise ValueError("offline-demo asset does not match its manifest size and SHA-256")

    scenario = FuelScenario(
        current_fuel_l=20.0,
        tank_capacity_l=120.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=10,
        reserve_l=1.0,
    )
    scenario_sha256 = _canonical_sha256(scenario.to_dict())
    fuel_receipt = _plain_object(
        asset.get("provisional_shared_fuel_model_receipt"),
        "offline-demo frozen fuel receipt",
    )
    if fuel_receipt.get("source_id") != _PUBLIC_AUDI_SPA_ASSET_ID:
        raise ValueError("offline-demo frozen fuel source_id mismatch")
    if fuel_receipt.get("session_id") != _PUBLIC_AUDI_SPA_SESSION_ID:
        raise ValueError("offline-demo frozen fuel session_id mismatch")
    if fuel_receipt.get("scenario") != _PUBLIC_AUDI_SPA_SCENARIO_SUMMARY:
        raise ValueError("offline-demo frozen fuel scenario mismatch")
    if fuel_receipt.get("scenario_sha256") != scenario_sha256:
        raise ValueError("offline-demo frozen fuel scenario hash mismatch")

    driving_receipt = _plain_object(
        asset.get("provisional_shared_driving_model_receipt"),
        "offline-demo frozen driving receipt",
    )
    if driving_receipt.get("source_id") != _PUBLIC_AUDI_SPA_ASSET_ID:
        raise ValueError("offline-demo frozen driving source_id mismatch")
    if driving_receipt.get("session_id") != _PUBLIC_AUDI_SPA_SESSION_ID:
        raise ValueError("offline-demo frozen driving session_id mismatch")
    driving_pipeline = _plain_object(
        driving_receipt.get("pipeline"), "offline-demo frozen driving pipeline"
    )
    driving_config = _plain_object(
        driving_pipeline.get("driving_config"),
        "offline-demo frozen driving config",
    )
    if driving_config.get("grid_step_m") != _PUBLIC_AUDI_SPA_GRID_STEP_M:
        raise ValueError("offline-demo frozen grid step mismatch")
    driving_summary = _plain_object(
        driving_receipt.get("model_summary"),
        "offline-demo frozen driving summary",
    )
    if driving_summary.get("reference_lap_ordinal") != _PUBLIC_AUDI_SPA_TARGET_LAP:
        raise ValueError("offline-demo frozen driving reference-lap mismatch")

    condition_receipt = _plain_object(
        asset.get("provisional_condition_cohort_receipt"),
        "offline-demo frozen condition receipt",
    )
    if condition_receipt.get("target_lap_ordinal") != _PUBLIC_AUDI_SPA_TARGET_LAP:
        raise ValueError("offline-demo frozen condition target-lap mismatch")

    label_receipt = _plain_object(
        asset.get("provisional_driving_label_candidate"),
        "offline-demo frozen label receipt",
    )
    if label_receipt.get("labeled_lap_ordinal") != _PUBLIC_AUDI_SPA_TARGET_LAP:
        raise ValueError("offline-demo frozen label target-lap mismatch")
    expected_label_artifact = _manifest_digest(
        label_receipt.get("artifact_sha256"),
        "offline-demo frozen label artifact_sha256",
    )
    expected_label_candidate = _manifest_digest(
        label_receipt.get("candidate_payload_sha256"),
        "offline-demo frozen label candidate_payload_sha256",
    )
    if (
        expected_label_artifact != _PUBLIC_AUDI_SPA_LABEL_ARTIFACT_SHA256
        or expected_label_candidate != _PUBLIC_AUDI_SPA_LABEL_CANDIDATE_SHA256
    ):
        raise ValueError(
            "offline-demo manifest label hashes differ from the package-frozen "
            "public-audi-spa trust root"
        )
    label_path = project_root / _PUBLIC_AUDI_SPA_LABEL_PATH
    _regular_file_sha256(label_path, "offline-demo pending label candidate")
    label_payload = _read_json_object(label_path, "offline-demo pending label candidate")
    if label_payload.get("artifact_sha256") != expected_label_artifact:
        raise ValueError("offline-demo pending label artifact hash mismatch")
    if label_payload.get("candidate_payload_sha256") != expected_label_candidate:
        raise ValueError("offline-demo pending label candidate hash mismatch")
    candidate_basis = _plain_object(
        label_payload.get("candidate_basis"), "offline-demo pending label candidate basis"
    )
    expected_candidate_basis = {
        "grid_step_mm": round(_PUBLIC_AUDI_SPA_GRID_STEP_M * 1_000),
        "labeled_lap_ordinal": _PUBLIC_AUDI_SPA_TARGET_LAP,
        "session_id": _PUBLIC_AUDI_SPA_SESSION_ID,
        "source_data_sha256": expected_source_sha256,
        "source_id": _PUBLIC_AUDI_SPA_ASSET_ID,
    }
    for key, expected in expected_candidate_basis.items():
        if candidate_basis.get(key) != expected:
            raise ValueError(f"offline-demo pending label {key} mismatch")

    shadow_receipt = _plain_object(
        asset.get("provisional_shadow_receipt"),
        "offline-demo frozen shadow receipt",
    )
    shadow_hashes = _plain_object(
        shadow_receipt.get("receipt"), "offline-demo frozen shadow hashes"
    )
    expected_component_hashes = {
        "condition_cohort_sha256": _manifest_digest(
            condition_receipt.get("condition_cohort_sha256"),
            "frozen condition_cohort_sha256",
        ),
        "condition_config_sha256": _manifest_digest(
            condition_receipt.get("condition_config_sha256"),
            "frozen condition_config_sha256",
        ),
        "condition_provenance_sha256": _manifest_digest(
            condition_receipt.get("condition_provenance_sha256"),
            "frozen condition_provenance_sha256",
        ),
        "condition_semantic_sha256": _manifest_digest(
            condition_receipt.get("condition_semantic_sha256"),
            "frozen condition_semantic_sha256",
        ),
        "driving_model_output_sha256": _manifest_digest(
            driving_receipt.get("model_output_sha256"),
            "frozen driving model_output_sha256",
        ),
        "driving_model_semantic_sha256": _manifest_digest(
            driving_receipt.get("model_semantic_sha256"),
            "frozen driving model_semantic_sha256",
        ),
        "driving_replay_sha256": _manifest_digest(
            driving_receipt.get("driving_replay_sha256"),
            "frozen driving_replay_sha256",
        ),
        "fuel_model_output_sha256": _manifest_digest(
            fuel_receipt.get("model_output_sha256"),
            "frozen fuel model_output_sha256",
        ),
        "fuel_model_semantic_sha256": _manifest_digest(
            fuel_receipt.get("model_semantic_sha256"),
            "frozen fuel model_semantic_sha256",
        ),
        "fuel_replay_sha256": _manifest_digest(
            fuel_receipt.get("fuel_replay_sha256"),
            "frozen fuel_replay_sha256",
        ),
        "label_artifact_sha256": expected_label_artifact,
        "label_candidate_payload_sha256": expected_label_candidate,
        "shadow_analysis_sha256": _manifest_digest(
            shadow_hashes.get("analysis_sha256"),
            "frozen shadow analysis_sha256",
        ),
    }
    if expected_component_hashes != _PUBLIC_AUDI_SPA_COMPONENT_HASHES:
        raise ValueError(
            "offline-demo component receipts differ from the package-frozen "
            "public-audi-spa trust root"
        )
    return {
        "expected_component_hashes": dict(_PUBLIC_AUDI_SPA_COMPONENT_HASHES),
        "expected_source_sha256": _PUBLIC_AUDI_SPA_SOURCE_SHA256,
        "expected_size": _PUBLIC_AUDI_SPA_BYTE_SIZE,
        "fuel_scenario": scenario,
        "label_payload": label_payload,
        "path": raw_path,
        "session_id": _PUBLIC_AUDI_SPA_SESSION_ID,
        "source_id": _PUBLIC_AUDI_SPA_ASSET_ID,
        "target_lap_ordinal": _PUBLIC_AUDI_SPA_TARGET_LAP,
    }


def _validate_offline_demo_output(
    payload: object,
    *,
    expected_component_hashes: object,
    expected_source_sha256: object,
    expected_size: object,
) -> bool:
    """Validate launcher-level preset bindings and return its trusted-gate result."""

    from .offline_demo import OFFLINE_ENGINEER_DEMO_CONTRACT_VERSION

    demo = _plain_object(payload, "offline-demo output")
    if demo.get("contract_version") != OFFLINE_ENGINEER_DEMO_CONTRACT_VERSION:
        raise ValueError("offline-demo output contract version mismatch")
    if demo.get("advisor_only") is not True or demo.get("execution_mode") != "SHADOW":
        raise ValueError("offline-demo output must remain advisor-only SHADOW")
    if demo.get("execution_status") != "COMPLETE":
        raise ValueError("offline-demo output is not COMPLETE")
    if demo.get("component_hashes") != expected_component_hashes:
        raise ValueError("offline-demo component hashes do not match the frozen preset")

    input_binding = _plain_object(demo.get("input_binding"), "offline-demo input binding")
    input_evidence = _plain_object(
        input_binding.get("input_evidence"), "offline-demo input evidence"
    )
    if input_evidence.get("source_sha256") != expected_source_sha256:
        raise ValueError("offline-demo output source SHA-256 mismatch")
    if input_evidence.get("byte_size") != expected_size:
        raise ValueError("offline-demo output source size mismatch")

    recommendations = _plain_object(demo.get("recommendations"), "offline-demo recommendations")
    if set(recommendations) != {"shared_driving", "shared_fuel", "shadow"}:
        raise ValueError("offline-demo recommendation groups are invalid")
    for group_name, raw_items in recommendations.items():
        if type(raw_items) is not list:
            raise ValueError(f"offline-demo {group_name} recommendations must be an array")
        for index, item in enumerate(raw_items):
            if type(item) is not dict or item.get("executable") is not False:
                raise ValueError(f"offline-demo {group_name} recommendation {index} is executable")

    demo_sha256 = demo.get("demo_sha256")
    if type(demo_sha256) is not str:
        raise ValueError("offline-demo output has no demo SHA-256")
    binding = {key: value for key, value in demo.items() if key != "demo_sha256"}
    if demo_sha256 != _canonical_sha256(binding):
        raise ValueError("offline-demo output demo SHA-256 mismatch")

    gates = _plain_object(demo.get("gates"), "offline-demo gates")
    trusted_gate_names = ("offline_demo", "condition_trust", "label_trust")
    statuses: list[object] = []
    for name in trusted_gate_names:
        gate = _plain_object(gates.get(name), f"offline-demo {name} gate")
        statuses.append(gate.get("status"))
    return all(status == "PASS" for status in statuses)


def _event_replay_payload(
    *,
    input_kind: str,
    input_evidence: dict[str, object],
    stale_after_s: float,
    event_receipt: dict[str, object],
    events: list[dict[str, object]] | None,
) -> dict[str, object]:
    normalization = {
        "normalized_telemetry_contract_version": TELEMETRY_CONTRACT_VERSION,
        "opponent_error_policy": "degrade",
        "profile_version": NORMALIZATION_PROFILE_VERSION,
        "stale_after_us": round(stale_after_s * 1_000_000),
    }
    normalization["config_sha256"] = _canonical_sha256(normalization)
    quality_reasons: list[str] = []
    if input_evidence["completion_status"] != "COMPLETE":
        quality_reasons.append("INCOMPLETE_RECOVERY")
    if input_kind == "collector":
        for field, reason in (
            ("duplicate_conflict_count", "DUPLICATE_CONFLICTS"),
            ("dropped_tick_count", "DROPPED_TICKS"),
            ("stale_event_count", "SOURCE_STALE_EVENTS"),
            ("schema_change_count", "SCHEMA_CHANGED"),
            ("session_reset_count", "SESSION_RESET"),
            ("capture_clock_regression_count", "CAPTURE_CLOCK_REGRESSION"),
            ("read_error_frame_count", "SDK_READ_ERRORS"),
            ("driver_info_key_count", "DRIVER_INFO_PERSISTED"),
        ):
            if input_evidence.get(field, 0):
                quality_reasons.append(reason)
    event_kind_counts = event_receipt.get("event_kind_counts", {})
    if isinstance(event_kind_counts, dict) and event_kind_counts.get("dropped_ticks", 0):
        quality_reasons.append("DROPPED_TICKS")
    sample_count = event_receipt.get("sample_count", 0)
    accepted_count = event_receipt.get("accepted_sample_count", 0)
    rejected_count = event_receipt.get("rejected_sample_count", 0)
    if sample_count == 0:
        quality_reasons.append("NO_NORMALIZED_SAMPLES")
    elif accepted_count == 0:
        quality_reasons.append("NO_ACCEPTED_SAMPLES")
    if rejected_count:
        quality_reasons.append("NORMALIZED_REJECTED_SAMPLES")
    quality_reasons = list(dict.fromkeys(quality_reasons))
    quality_gate = {
        "status": "PASS" if not quality_reasons else "DEGRADED",
        "reasons": quality_reasons,
    }
    binding = {
        "contract_version": EVENT_REPLAY_CONTRACT_VERSION,
        "event_receipt": event_receipt,
        "input_evidence": input_evidence,
        "input_kind": input_kind,
        "normalization": normalization,
        "quality_gate": quality_gate,
    }
    payload: dict[str, object] = {
        **binding,
        "event_replay_sha256": _canonical_sha256(binding),
    }
    if events is not None:
        payload["events"] = events
    return payload


def _fuel_scenario(parser: argparse.ArgumentParser, args: argparse.Namespace):
    from .fuel import FuelScenario

    required = {
        "--current-fuel-l": args.current_fuel_l,
        "--tank-capacity-l": args.tank_capacity_l,
        "--refuel-rate-lps": args.refuel_rate_lps,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        parser.error("shadow fuel analysis requires " + ", ".join(missing))
    if args.remaining_laps is None and args.remaining_time_s is None:
        parser.error("shadow fuel analysis requires --remaining-laps or --remaining-time-s")
    if args.current_fuel_l > args.tank_capacity_l:
        parser.error("--current-fuel-l cannot exceed --tank-capacity-l")
    if args.reserve_l >= args.tank_capacity_l:
        parser.error("--reserve-l must be below --tank-capacity-l")
    if args.minimum_fuel_laps < 2:
        parser.error("--minimum-fuel-laps must be at least 2")
    return FuelScenario(
        current_fuel_l=args.current_fuel_l,
        tank_capacity_l=args.tank_capacity_l,
        refuel_rate_l_per_s=args.refuel_rate_lps,
        remaining_laps=args.remaining_laps,
        remaining_time_s=args.remaining_time_s,
        reference_lap_time_s=args.reference_lap_time_s,
        reserve_l=args.reserve_l,
        conservative_quantile=args.fuel_quantile,
        minimum_valid_laps=args.minimum_fuel_laps,
        timed_race_extra_laps=args.timed_race_extra_laps,
    )


def _shadow_fuel_scenario(parser: argparse.ArgumentParser, args: argparse.Namespace):
    if args.grid_m > 10.0:
        parser.error("--grid-m must be at most 10")
    if args.analysis == "driving":
        return None
    return _fuel_scenario(parser, args)


def _retrieved_live_optional_inputs(
    args: argparse.Namespace,
) -> tuple[
    dict[str, object] | None,
    dict[str, object] | None,
    dict[str, object] | None,
    dict[str, object] | None,
]:
    calibration_values = (
        args.calibration_model,
        args.expected_calibration_model_sha256,
        args.expected_calibration_source_receipt_sha256,
    )
    if any(value is not None for value in calibration_values) and not all(
        value is not None for value in calibration_values
    ):
        raise ValueError(
            "calibration model path, model SHA-256, and source-receipt SHA-256 "
            "must be supplied together"
        )
    tire_performance_values = (
        args.tire_performance_model,
        args.expected_tire_performance_model_sha256,
        args.expected_tire_performance_source_receipt_sha256,
    )
    if any(value is not None for value in tire_performance_values) and not all(
        value is not None for value in tire_performance_values
    ):
        raise ValueError(
            "tire-performance model path, model SHA-256, and source-receipt "
            "SHA-256 must be supplied together"
        )
    rule_values = (
        args.rules_profile,
        args.expected_rules_profile_sha256,
        args.expected_rules_source_sha256,
    )
    if any(value is not None for value in rule_values) and not all(
        value is not None for value in rule_values
    ):
        raise ValueError(
            "rules profile path, profile SHA-256, and source-document SHA-256 "
            "must be supplied together"
        )
    previous_values = (
        args.previous_m2_receipt,
        args.expected_previous_m2_sha256,
        args.expected_previous_revision,
    )
    if any(value is not None for value in previous_values) and not all(
        value is not None for value in previous_values
    ):
        raise ValueError(
            "previous M2 receipt, receipt SHA-256, and revision must be supplied together"
        )
    calibration = (
        _read_regular_json_object(args.calibration_model, "calibration model")
        if args.calibration_model is not None
        else None
    )
    tire_performance = (
        _read_regular_json_object(
            args.tire_performance_model,
            "tire-performance model",
        )
        if args.tire_performance_model is not None
        else None
    )
    rules = (
        _read_regular_json_object(args.rules_profile, "rules profile")
        if args.rules_profile is not None
        else None
    )
    previous = (
        _read_regular_json_object(args.previous_m2_receipt, "previous M2 receipt")
        if args.previous_m2_receipt is not None
        else None
    )
    return calibration, tire_performance, rules, previous


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "build-tire-performance-model":
        from .retrieved_live_analysis import (
            MATCHED_TIRE_PERFORMANCE_DATASET_CONTRACT_VERSION,
            TirePerformanceError,
            write_tire_performance_model_exclusive,
        )

        try:
            model = write_tire_performance_model_exclusive(
                args.dataset,
                args.output,
                expected_dataset_sha256=args.expected_dataset_sha256,
            )
        except TirePerformanceError as exc:
            print(
                json.dumps(
                    {
                        "code": exc.code,
                        "contract_version": (
                            MATCHED_TIRE_PERFORMANCE_DATASET_CONTRACT_VERSION
                        ),
                        "detail": str(exc),
                        "status": "WAIT_TIRE_PERFORMANCE_MODEL",
                    },
                    sort_keys=True,
                )
            )
            return 2
        print(
            json.dumps(
                {
                    "estimate_available": model["estimate_available"],
                    "model_sha256": model["model_sha256"],
                    "pair_count": model["pair_count"],
                    "status": model["status"],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "build-pit-calibration":
        from .retrieved_live_analysis import (
            MATCHED_PIT_CALIBRATION_DATASET_CONTRACT_VERSION,
            PitCalibrationError,
            write_matched_pit_calibration_model_exclusive,
        )

        try:
            model = write_matched_pit_calibration_model_exclusive(
                args.dataset,
                args.output,
                expected_dataset_sha256=args.expected_dataset_sha256,
            )
        except PitCalibrationError as exc:
            print(
                json.dumps(
                    {
                        "code": exc.code,
                        "contract_version": (
                            MATCHED_PIT_CALIBRATION_DATASET_CONTRACT_VERSION
                        ),
                        "detail": str(exc),
                        "status": "WAIT_CALIBRATION",
                    },
                    sort_keys=True,
                )
            )
            return 2
        print(
            json.dumps(
                {
                    "model_sha256": model["model_sha256"],
                    "sample_count": model["sample_count"],
                    "status": model["status"],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "supervise-r8":
        from .live_supervisor import (
            SUPERVISOR_CONTRACT_VERSION,
            LiveSupervisorError,
            run_live_supervisor,
        )

        try:
            payload = run_live_supervisor()
            if type(payload) is not dict or payload.get("status") not in {
                "WAIT",
                "READY",
            }:
                raise LiveSupervisorError(
                    "SUPERVISOR_RESULT_INVALID",
                    "supervisor returned neither WAIT nor READY",
                )
            encoded_payload = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except LiveSupervisorError as exc:
            print(
                json.dumps(
                    {
                        "contract_version": SUPERVISOR_CONTRACT_VERSION,
                        "error": exc.code,
                        "message": str(exc),
                        "status": "FAILED",
                    },
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 3
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "contract_version": SUPERVISOR_CONTRACT_VERSION,
                        "error": "UNEXPECTED_SUPERVISOR_FATAL",
                        "message": str(exc),
                        "status": "FAILED",
                    },
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 3
        print(encoded_payload)
        return 0
    if args.command == "make-live-analysis-profile":
        from .retrieved_live_analysis import (
            RETRIEVED_LIVE_ANALYSIS_PROFILE_CONTRACT_VERSION,
            RetrievedLiveAnalysisError,
            build_retrieved_live_analysis_profile,
            write_retrieved_live_analysis_profile_exclusive,
        )

        try:
            profile = build_retrieved_live_analysis_profile(
                profile_id=args.profile_id,
                profile_version=args.profile_version,
                tank_capacity_l=args.tank_capacity_l,
                refuel_rate_l_per_s=args.refuel_rate_lps,
                reserve_l=args.reserve_l,
                conservative_quantile=args.fuel_quantile,
                minimum_valid_laps=args.minimum_fuel_laps,
                timed_race_extra_laps=args.timed_race_extra_laps,
                remaining_laps=args.remaining_laps,
                remaining_time_s=args.remaining_time_s,
                reference_lap_time_s=args.reference_lap_time_s,
            )
            write_retrieved_live_analysis_profile_exclusive(
                args.output,
                profile,
                expected_analysis_profile_sha256=profile[
                    "analysis_profile_sha256"
                ],
            )
            file_sha256, byte_size = _regular_file_sha256(
                args.output, "analysis profile"
            )
            payload = {
                "analysis_profile_sha256": profile["analysis_profile_sha256"],
                "byte_size": byte_size,
                "contract_version": RETRIEVED_LIVE_ANALYSIS_PROFILE_CONTRACT_VERSION,
                "file_sha256": file_sha256,
                "output": str(args.output),
                "profile_id": profile["profile_id"],
                "profile_version": profile["profile_version"],
                "status": "PASS_PROFILE_CREATED",
            }
        except (
            RetrievedLiveAnalysisError,
            OSError,
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
        ) as exc:
            _print_error(
                RETRIEVED_LIVE_ANALYSIS_PROFILE_CONTRACT_VERSION,
                "MAKE_LIVE_ANALYSIS_PROFILE_ERROR",
                exc,
            )
            return 3
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "finalize-live-analysis":
        from .retrieved_live_analysis import (
            RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION,
            RetrievedLiveAnalysisError,
            write_retrieved_live_analysis_bundle_exclusive,
        )

        try:
            live_session = _read_regular_json_object(
                args.live_session, "live engineer session"
            )
            profile = _read_regular_json_object(
                args.analysis_profile, "analysis profile"
            )
            calibration, tire_performance, rules, previous = (
                _retrieved_live_optional_inputs(args)
            )
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(args.capture, flags)
            with os.fdopen(descriptor, "rb", buffering=0) as capture_handle:
                receipt = write_retrieved_live_analysis_bundle_exclusive(
                    capture_handle,
                    live_session,
                    profile,
                    args.session_output,
                    args.artifact_output,
                    args.html_output,
                    args.receipt_output,
                    expected_live_engineer_session_sha256=(
                        args.expected_live_engineer_session_sha256
                    ),
                    expected_remote_capture_sha256=(
                        args.expected_remote_capture_sha256
                    ),
                    expected_remote_capture_byte_size=(
                        args.expected_remote_capture_byte_size
                    ),
                    expected_analysis_profile_sha256=(
                        args.expected_analysis_profile_sha256
                    ),
                    calibration_model=calibration,
                    expected_calibration_model_sha256=(
                        args.expected_calibration_model_sha256
                    ),
                    expected_calibration_source_receipt_sha256=(
                        args.expected_calibration_source_receipt_sha256
                    ),
                    tire_performance_model=tire_performance,
                    expected_tire_performance_model_sha256=(
                        args.expected_tire_performance_model_sha256
                    ),
                    expected_tire_performance_source_receipt_sha256=(
                        args.expected_tire_performance_source_receipt_sha256
                    ),
                    rules_profile=rules,
                    expected_rules_profile_sha256=(
                        args.expected_rules_profile_sha256
                    ),
                    expected_rules_source_sha256=(
                        args.expected_rules_source_sha256
                    ),
                    previous_m2_receipt=previous,
                    expected_previous_m2_sha256=(
                        args.expected_previous_m2_sha256
                    ),
                    expected_previous_revision=args.expected_previous_revision,
                    stale_after_s=args.stale_after_seconds,
                )
            session_file_sha256, session_byte_size = _regular_file_sha256(
                args.session_output, "engineer session"
            )
            artifact_file_sha256, artifact_byte_size = _regular_file_sha256(
                args.artifact_output, "report artifact"
            )
            html_file_sha256, html_byte_size = _regular_file_sha256(
                args.html_output, "report HTML"
            )
            receipt_file_sha256, receipt_byte_size = _regular_file_sha256(
                args.receipt_output, "analysis bundle receipt"
            )
            readiness = _plain_object(receipt["readiness"], "analysis readiness")
            payload = {
                "advisor_only": True,
                "artifact_byte_size": artifact_byte_size,
                "artifact_file_sha256": artifact_file_sha256,
                "artifact_path": str(args.artifact_output),
                "bundle_receipt_sha256": receipt["bundle_receipt_sha256"],
                "contract_version": RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION,
                "driving_practice_available": readiness[
                    "driving_practice_available"
                ],
                "engineer_session_byte_size": session_byte_size,
                "engineer_session_file_sha256": session_file_sha256,
                "engineer_session_path": str(args.session_output),
                "engineer_session_sha256": receipt["engineer_session_binding"][
                    "session_sha256"
                ],
                "html_byte_size": html_byte_size,
                "html_file_sha256": html_file_sha256,
                "html_path": str(args.html_output),
                "receipt_byte_size": receipt_byte_size,
                "receipt_file_sha256": receipt_file_sha256,
                "receipt_path": str(args.receipt_output),
                "report_sha256": receipt["report_binding"]["report_sha256"],
                "source_kind": receipt["source_binding"]["source_kind"],
                "status": receipt["status"],
                "strategy_advice_available": readiness[
                    "strategy_advice_available"
                ],
                "vehicle_control_enabled": False,
            }
        except FileNotFoundError as exc:
            _print_error(
                RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION,
                "WAIT_DATA",
                exc,
            )
            return 4
        except (
            RetrievedLiveAnalysisError,
            OSError,
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
        ) as exc:
            _print_error(
                RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION,
                "FINALIZE_LIVE_ANALYSIS_ERROR",
                exc,
            )
            return 3
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        if (
            args.require_strategy_advice
            and readiness["strategy_advice_available"] is not True
        ) or (
            args.require_driving_advice
            and readiness["driving_practice_available"] is not True
        ):
            return 5
        return 0
    if args.command == "verify-live-analysis":
        from .retrieved_live_analysis import (
            RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION,
            RetrievedLiveAnalysisError,
            verify_retrieved_live_analysis_bundle,
        )

        try:
            live_session = _read_regular_json_object(
                args.live_session, "live engineer session"
            )
            profile = _read_regular_json_object(
                args.analysis_profile, "analysis profile"
            )
            bundle_receipt = _read_regular_json_object(
                args.bundle_receipt, "analysis bundle receipt"
            )
            calibration, tire_performance, rules, previous = (
                _retrieved_live_optional_inputs(args)
            )
            session_bytes = _read_regular_file_bytes(
                args.engineer_session, "engineer session"
            )
            report_bytes = _read_regular_file_bytes(
                args.report_artifact, "report artifact"
            )
            html_bytes = _read_regular_file_bytes(args.report_html, "report HTML")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(args.capture, flags)
            with os.fdopen(descriptor, "rb", buffering=0) as capture_handle:
                receipt = verify_retrieved_live_analysis_bundle(
                    capture_handle,
                    live_session,
                    profile,
                    session_bytes,
                    report_bytes,
                    html_bytes,
                    bundle_receipt,
                    expected_live_engineer_session_sha256=(
                        args.expected_live_engineer_session_sha256
                    ),
                    expected_remote_capture_sha256=(
                        args.expected_remote_capture_sha256
                    ),
                    expected_remote_capture_byte_size=(
                        args.expected_remote_capture_byte_size
                    ),
                    expected_analysis_profile_sha256=(
                        args.expected_analysis_profile_sha256
                    ),
                    expected_bundle_receipt_sha256=(
                        args.expected_bundle_receipt_sha256
                    ),
                    calibration_model=calibration,
                    expected_calibration_model_sha256=(
                        args.expected_calibration_model_sha256
                    ),
                    expected_calibration_source_receipt_sha256=(
                        args.expected_calibration_source_receipt_sha256
                    ),
                    tire_performance_model=tire_performance,
                    expected_tire_performance_model_sha256=(
                        args.expected_tire_performance_model_sha256
                    ),
                    expected_tire_performance_source_receipt_sha256=(
                        args.expected_tire_performance_source_receipt_sha256
                    ),
                    rules_profile=rules,
                    expected_rules_profile_sha256=(
                        args.expected_rules_profile_sha256
                    ),
                    expected_rules_source_sha256=(
                        args.expected_rules_source_sha256
                    ),
                    previous_m2_receipt=previous,
                    expected_previous_m2_sha256=(
                        args.expected_previous_m2_sha256
                    ),
                    expected_previous_revision=args.expected_previous_revision,
                    stale_after_s=args.stale_after_seconds,
                )
            readiness = _plain_object(receipt["readiness"], "analysis readiness")
            payload = {
                "advisor_only": True,
                "bundle_receipt_sha256": receipt["bundle_receipt_sha256"],
                "contract_version": RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION,
                "driving_practice_available": readiness[
                    "driving_practice_available"
                ],
                "engineer_session_sha256": receipt["engineer_session_binding"][
                    "session_sha256"
                ],
                "report_sha256": receipt["report_binding"]["report_sha256"],
                "source_kind": receipt["source_binding"]["source_kind"],
                "status": receipt["status"],
                "strategy_advice_available": readiness[
                    "strategy_advice_available"
                ],
                "verification": "PASS_OBJECT_EXACT_REPLAY",
                "vehicle_control_enabled": False,
            }
        except FileNotFoundError as exc:
            _print_error(
                RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION,
                "WAIT_DATA",
                exc,
            )
            return 4
        except (
            RetrievedLiveAnalysisError,
            OSError,
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
        ) as exc:
            _print_error(
                RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION,
                "VERIFY_LIVE_ANALYSIS_ERROR",
                exc,
            )
            return 3
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        if (
            args.require_strategy_advice
            and readiness["strategy_advice_available"] is not True
        ) or (
            args.require_driving_advice
            and readiness["driving_practice_available"] is not True
        ):
            return 5
        return 0
    if args.command == "session-report":
        from .session_report import (
            ENGINEER_SESSION_REPORT_CONTRACT_VERSION,
            EngineerSessionReportError,
            build_engineer_session_report,
            write_engineer_session_report_bundle_exclusive,
        )

        try:
            session = _read_json_object(args.session, "engineer session")
            report = build_engineer_session_report(
                session,
                expected_engineer_session_sha256=(
                    args.expected_engineer_session_sha256
                ),
            )
            write_engineer_session_report_bundle_exclusive(
                args.artifact_output,
                args.html_output,
                report,
                session,
                expected_report_sha256=report["report_sha256"],
                expected_engineer_session_sha256=(
                    args.expected_engineer_session_sha256
                ),
            )
            artifact_file_sha256, artifact_byte_size = _regular_file_sha256(
                args.artifact_output, "session-report artifact"
            )
            html_file_sha256, html_byte_size = _regular_file_sha256(
                args.html_output, "session-report HTML"
            )
            payload = {
                "artifact_byte_size": artifact_byte_size,
                "artifact_file_sha256": artifact_file_sha256,
                "artifact_path": str(args.artifact_output),
                "contract_version": ENGINEER_SESSION_REPORT_CONTRACT_VERSION,
                "engineer_session_sha256": args.expected_engineer_session_sha256,
                "html_byte_size": html_byte_size,
                "html_file_sha256": html_file_sha256,
                "html_path": str(args.html_output),
                "report_sha256": report["report_sha256"],
                "status": report["status"],
            }
        except FileNotFoundError as exc:
            _print_error(
                ENGINEER_SESSION_REPORT_CONTRACT_VERSION,
                "WAIT_DATA",
                exc,
            )
            return 4
        except (
            EngineerSessionReportError,
            OSError,
            OverflowError,
            RecursionError,
            ValueError,
        ) as exc:
            _print_error(
                ENGINEER_SESSION_REPORT_CONTRACT_VERSION,
                "SESSION_REPORT_ERROR",
                exc,
            )
            return 3
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        if args.require_advice and report["status"] not in {
            "ADVICE_AVAILABLE",
            "PRACTICE_AVAILABLE",
        }:
            return 5
        return 0
    if args.command == "live-session-report":
        from .live_engineer_session import (
            LiveEngineerSessionError,
            write_retrieved_live_engineer_session_report_bundle,
        )

        try:
            live_session = _read_json_object(
                args.live_session, "live engineer session"
            )
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(args.capture, flags)
            with os.fdopen(descriptor, "rb", buffering=0) as capture_handle:
                payload = write_retrieved_live_engineer_session_report_bundle(
                    capture_handle,
                    live_session,
                    args.artifact_output,
                    args.html_output,
                    expected_live_engineer_session_sha256=(
                        args.expected_live_engineer_session_sha256
                    ),
                    expected_remote_capture_sha256=(
                        args.expected_remote_capture_sha256
                    ),
                    expected_remote_capture_byte_size=(
                        args.expected_remote_capture_byte_size
                    ),
                    stale_after_s=args.stale_after_seconds,
                )
            artifact_file_sha256, artifact_byte_size = _regular_file_sha256(
                args.artifact_output, "live session-report artifact"
            )
            html_file_sha256, html_byte_size = _regular_file_sha256(
                args.html_output, "live session-report HTML"
            )
            payload = {
                **payload,
                "artifact_byte_size": artifact_byte_size,
                "artifact_file_sha256": artifact_file_sha256,
                "html_byte_size": html_byte_size,
                "html_file_sha256": html_file_sha256,
            }
        except FileNotFoundError as exc:
            _print_error(
                "retrieved-live-session-report-write-v1",
                "WAIT_DATA",
                exc,
            )
            return 4
        except (
            LiveEngineerSessionError,
            OSError,
            OverflowError,
            RecursionError,
            ValueError,
        ) as exc:
            _print_error(
                "retrieved-live-session-report-write-v1",
                "LIVE_SESSION_REPORT_ERROR",
                exc,
            )
            return 3
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        if args.require_advice and payload["status"] not in {
            "ADVICE_AVAILABLE",
            "PRACTICE_AVAILABLE",
        }:
            return 5
        return 0
    if args.command == "inspect":
        from .contracts import QUALITY_PROFILE_VERSION
        from .ibt import IbtFormatError
        from .quality import analyze_ibt

        try:
            reports = [analyze_ibt(path) for path in args.paths]
        except FileNotFoundError as exc:
            _print_error(QUALITY_PROFILE_VERSION, "WAIT_DATA", exc)
            return 4
        except (IbtFormatError, OSError, ValueError) as exc:
            _print_error(QUALITY_PROFILE_VERSION, "IBT_INSPECTION_ERROR", exc)
            return 3
        payload = [report.to_dict() if args.full else report.summary_row() for report in reports]
    elif args.command == "replay":
        from .contracts import REPLAY_CONTRACT_VERSION
        from .ibt import IbtFormatError
        from .replay import replay_ibt

        try:
            payload = replay_ibt(
                args.path, frame_hash_chunk_size=args.frame_hash_chunk_size
            ).to_dict()
        except FileNotFoundError as exc:
            _print_error(REPLAY_CONTRACT_VERSION, "WAIT_DATA", exc)
            return 4
        except (IbtFormatError, OSError, ValueError) as exc:
            _print_error(REPLAY_CONTRACT_VERSION, "TELEMETRY_REPLAY_ERROR", exc)
            return 3
    elif args.command == "events":
        from .adapters import (
            TelemetryAdapterError,
            open_collector_jsonl,
            open_ibt_telemetry,
        )
        from .events import (
            EventPipelineError,
            process_telemetry_events,
        )
        from .ibt import IbtFormatError

        try:
            input_kind = _event_input_kind(args.path, args.input_kind)
            if input_kind == "ibt":
                if args.allow_incomplete_collector:
                    raise ValueError(
                        "--allow-incomplete-collector is valid only for collector input"
                    )
                if args.source_id is None or args.session_id is None:
                    raise ValueError("IBT event replay requires --source-id and --session-id")
                with open_ibt_telemetry(
                    args.path,
                    source_id=args.source_id,
                    session_id=args.session_id,
                    stale_after_s=args.stale_after_seconds,
                ) as run:
                    events, receipt = process_telemetry_events(run.samples)
                    bound = run.evidence.to_dict()
                    # Keep the event-v1 evidence shape stable while all values
                    # now originate from the same open descriptor as samples.
                    input_evidence = {
                        key: bound[key]
                        for key in (
                            "authenticity_status",
                            "byte_size",
                            "completion_status",
                            "session_id",
                            "source_id",
                            "source_kind",
                            "source_sha256",
                        )
                    }
            else:
                if args.source_id is not None or args.session_id is not None:
                    raise ValueError(
                        "collector identity is bound by its run record and cannot be relabeled"
                    )
                with open_collector_jsonl(
                    args.path,
                    stale_after_s=args.stale_after_seconds,
                    require_receipt=not args.allow_incomplete_collector,
                ) as run:
                    input_evidence = run.evidence.to_dict()
                    events, receipt = process_telemetry_events(run.samples)
            payload = _event_replay_payload(
                input_kind=input_kind,
                input_evidence=input_evidence,
                stale_after_s=args.stale_after_seconds,
                event_receipt=receipt.to_dict(),
                events=([event.to_dict() for event in events] if args.include_events else None),
            )
        except FileNotFoundError as exc:
            _print_error(
                EVENT_REPLAY_CONTRACT_VERSION,
                "WAIT_DATA",
                exc,
            )
            return 4
        except (
            EventPipelineError,
            IbtFormatError,
            OSError,
            OverflowError,
            RecursionError,
            TelemetryAdapterError,
            ValueError,
        ) as exc:
            _print_error(
                EVENT_REPLAY_CONTRACT_VERSION,
                "EVENT_REPLAY_ERROR",
                exc,
            )
            return 3
    elif args.command == "offline-demo":
        from .driving import DrivingAnalysisConfig
        from .driving_model_replay import build_driving_model_replay
        from .ibt import IbtFormatError
        from .offline_demo import (
            OFFLINE_ENGINEER_DEMO_CONTRACT_VERSION,
            OfflineEngineerDemoError,
            build_offline_engineer_demo,
        )
        from .shadow import ShadowReportError, build_shadow_report

        try:
            if args.output is not None:
                if args.output.exists() or args.output.is_symlink():
                    raise FileExistsError(f"offline-demo output already exists: {args.output}")
                if not args.output.parent.is_dir():
                    raise ValueError(
                        f"offline-demo output directory does not exist: {args.output.parent}"
                    )
            preset = _offline_demo_preset(args.manifest)

            def frozen_shadow_builder(path, *, analysis, fuel_scenario):
                return build_shadow_report(
                    path,
                    analysis=analysis,
                    fuel_scenario=fuel_scenario,
                    grid_step_m=_PUBLIC_AUDI_SPA_GRID_STEP_M,
                    top=_PUBLIC_AUDI_SPA_TOP,
                )

            def frozen_driving_builder(run):
                return build_driving_model_replay(
                    run,
                    config=DrivingAnalysisConfig(grid_step_m=_PUBLIC_AUDI_SPA_GRID_STEP_M),
                    top=_PUBLIC_AUDI_SPA_TOP,
                )

            payload = build_offline_engineer_demo(
                preset["path"],
                source_id=preset["source_id"],
                session_id=preset["session_id"],
                fuel_scenario=preset["fuel_scenario"],
                target_lap_ordinal=preset["target_lap_ordinal"],
                pending_label_payload=preset["label_payload"],
                shadow_builder=frozen_shadow_builder,
                driving_builder=frozen_driving_builder,
            )
            final_source_sha256, final_size = _regular_file_sha256(
                preset["path"], "offline-demo asset"
            )
            if (final_source_sha256, final_size) != (
                preset["expected_source_sha256"],
                preset["expected_size"],
            ):
                raise ValueError("offline-demo asset changed while the demo was running")
            trusted_offline_demo = _validate_offline_demo_output(
                payload,
                expected_component_hashes=preset["expected_component_hashes"],
                expected_source_sha256=preset["expected_source_sha256"],
                expected_size=preset["expected_size"],
            )
            failed_offline_demo_trust = not trusted_offline_demo
            if args.output is not None:
                _write_new_json(args.output, payload)
        except FileNotFoundError as exc:
            _print_error(OFFLINE_ENGINEER_DEMO_CONTRACT_VERSION, "WAIT_DATA", exc)
            return 4
        except FileExistsError as exc:
            _print_error(OFFLINE_ENGINEER_DEMO_CONTRACT_VERSION, "OUTPUT_EXISTS", exc)
            return 3
        except (
            IbtFormatError,
            OfflineEngineerDemoError,
            OSError,
            OverflowError,
            RecursionError,
            ShadowReportError,
            ValueError,
        ) as exc:
            _print_error(
                OFFLINE_ENGINEER_DEMO_CONTRACT_VERSION,
                "OFFLINE_DEMO_ERROR",
                exc,
            )
            return 3
    elif args.command == "fuel-replay":
        from .adapters import (
            TelemetryAdapterError,
            open_collector_jsonl,
            open_ibt_telemetry,
        )
        from .events import EventPipelineError
        from .ibt import IbtFormatError
        from .model_replay import (
            FUEL_MODEL_REPLAY_CONTRACT_VERSION,
            FuelModelReplayError,
            build_fuel_model_replay,
        )

        scenario = _fuel_scenario(parser, args)
        try:
            input_kind = _event_input_kind(args.path, args.input_kind)
            if input_kind == "ibt":
                if args.source_id is None or args.session_id is None:
                    raise ValueError("IBT fuel replay requires --source-id and --session-id")
                with open_ibt_telemetry(
                    args.path,
                    source_id=args.source_id,
                    session_id=args.session_id,
                    stale_after_s=args.stale_after_seconds,
                ) as run:
                    payload = build_fuel_model_replay(
                        run,
                        scenario=scenario,
                    )
            else:
                if args.source_id is not None or args.session_id is not None:
                    raise ValueError(
                        "collector identity is bound by its run record and cannot be relabeled"
                    )
                with open_collector_jsonl(
                    args.path,
                    stale_after_s=args.stale_after_seconds,
                    require_receipt=True,
                ) as run:
                    payload = build_fuel_model_replay(
                        run,
                        scenario=scenario,
                    )
            failed_fuel_replay = payload["capabilities"]["fuel_model_shadow"]["status"] != "PASS"
            if args.receipt_only:
                payload = {
                    "contract_version": payload["contract_version"],
                    "fuel_replay_sha256": payload["fuel_replay_sha256"],
                    "model_output_sha256": payload["model_output_sha256"],
                    "model_semantic_sha256": payload["model_semantic_sha256"],
                    "normalized_input_receipt": payload["normalized_input_receipt"],
                    "quality_gate": payload["quality_gate"],
                    "scenario_sha256": payload["scenario_sha256"],
                }
        except FileNotFoundError as exc:
            _print_error(
                FUEL_MODEL_REPLAY_CONTRACT_VERSION,
                "WAIT_DATA",
                exc,
            )
            return 4
        except (
            EventPipelineError,
            FuelModelReplayError,
            IbtFormatError,
            OSError,
            OverflowError,
            RecursionError,
            TelemetryAdapterError,
            ValueError,
        ) as exc:
            _print_error(
                FUEL_MODEL_REPLAY_CONTRACT_VERSION,
                "FUEL_MODEL_REPLAY_ERROR",
                exc,
            )
            return 3
    elif args.command == "driving-replay":
        from .adapters import (
            TelemetryAdapterError,
            open_collector_jsonl,
            open_ibt_telemetry,
        )
        from .driving import DrivingAnalysisConfig
        from .driving_model_replay import (
            DRIVING_MODEL_REPLAY_CONTRACT_VERSION,
            DrivingModelReplayError,
            build_driving_model_replay,
        )
        from .events import EventPipelineError
        from .ibt import IbtFormatError

        if args.grid_m > 10.0:
            parser.error("--grid-m must be at most 10")
        driving_config = DrivingAnalysisConfig(grid_step_m=args.grid_m)
        try:
            input_kind = _event_input_kind(args.path, args.input_kind)
            if input_kind == "ibt":
                if args.source_id is None or args.session_id is None:
                    raise ValueError("IBT driving replay requires --source-id and --session-id")
                with open_ibt_telemetry(
                    args.path,
                    source_id=args.source_id,
                    session_id=args.session_id,
                    stale_after_s=args.stale_after_seconds,
                ) as run:
                    payload = build_driving_model_replay(
                        run,
                        config=driving_config,
                        top=args.top,
                    )
            else:
                if args.source_id is not None or args.session_id is not None:
                    raise ValueError(
                        "collector identity is bound by its run record and cannot be relabeled"
                    )
                with open_collector_jsonl(
                    args.path,
                    stale_after_s=args.stale_after_seconds,
                    require_receipt=True,
                ) as run:
                    payload = build_driving_model_replay(
                        run,
                        config=driving_config,
                        top=args.top,
                    )
            failed_driving_replay = (
                payload["capabilities"]["driving_model_shadow"]["status"] != "PASS"
            )
            if args.receipt_only:
                payload = {
                    "contract_version": payload["contract_version"],
                    "driving_context_sha256": payload["driving_context_sha256"],
                    "driving_replay_sha256": payload["driving_replay_sha256"],
                    "model_output_sha256": payload["model_output_sha256"],
                    "model_semantic_sha256": payload["model_semantic_sha256"],
                    "normalized_input_receipt": payload["normalized_input_receipt"],
                    "quality_gate": payload["quality_gate"],
                    "readiness_status": payload["readiness_status"],
                    "semantic_input_receipt": payload["semantic_input_receipt"],
                }
        except FileNotFoundError as exc:
            _print_error(
                DRIVING_MODEL_REPLAY_CONTRACT_VERSION,
                "WAIT_DATA",
                exc,
            )
            return 4
        except (
            DrivingModelReplayError,
            EventPipelineError,
            IbtFormatError,
            OSError,
            OverflowError,
            RecursionError,
            TelemetryAdapterError,
            ValueError,
        ) as exc:
            _print_error(
                DRIVING_MODEL_REPLAY_CONTRACT_VERSION,
                "DRIVING_MODEL_REPLAY_ERROR",
                exc,
            )
            return 3
    elif args.command == "condition-cohort":
        from .adapters import (
            TelemetryAdapterError,
            open_collector_jsonl,
            open_ibt_telemetry,
        )
        from .condition_cohort import (
            CONDITION_COHORT_CONTRACT_VERSION,
            ApprovedTrackStateLabelSet,
            ConditionCohortError,
            build_condition_cohort,
        )
        from .ibt import IbtFormatError

        try:
            input_kind = _condition_input_kind(args.path, args.input_kind)
            if input_kind == "ibt":
                if args.source_id is None or args.session_id is None:
                    raise ValueError("IBT condition cohort requires --source-id and --session-id")
            elif args.source_id is not None or args.session_id is not None:
                raise ValueError(
                    "collector identity is bound by its run record and cannot be relabeled"
                )
            track_state_labels = (
                ApprovedTrackStateLabelSet.from_dict(
                    _read_json_object(
                        args.track_state_labels,
                        "track-state label input",
                    )
                )
                if args.track_state_labels is not None
                else None
            )
            if input_kind == "ibt":
                with open_ibt_telemetry(
                    args.path,
                    source_id=args.source_id,
                    session_id=args.session_id,
                    stale_after_s=args.stale_after_seconds,
                ) as run:
                    payload = build_condition_cohort(
                        run,
                        target_lap_ordinal=args.target_lap_ordinal,
                        track_state_labels=track_state_labels,
                    )
            else:
                with open_collector_jsonl(
                    args.path,
                    stale_after_s=args.stale_after_seconds,
                    require_receipt=True,
                ) as run:
                    payload = build_condition_cohort(
                        run,
                        target_lap_ordinal=args.target_lap_ordinal,
                        track_state_labels=track_state_labels,
                    )
            if payload["recommendations"] != []:
                raise ConditionCohortError(
                    "condition cohort CLI refuses recommendation-bearing output"
                )
            failed_condition_cohort = payload["trusted_readiness_status"] != "PASS"
            if args.receipt_only:
                payload = {
                    "condition_cohort_sha256": payload["condition_cohort_sha256"],
                    "condition_config_sha256": payload["condition_config_sha256"],
                    "condition_provenance_sha256": payload["condition_provenance_sha256"],
                    "condition_semantic_sha256": payload["condition_semantic_sha256"],
                    "contract_version": payload["contract_version"],
                    "input_kind": payload["input_kind"],
                    "matched_lap_ordinals": payload["matched_lap_ordinals"],
                    "matcher_config": payload["matcher_config"],
                    "normalized_input_receipt": payload["normalized_input_receipt"],
                    "quality_gate": payload["quality_gate"],
                    "readiness_status": payload["readiness_status"],
                    "recommendations": [],
                    "target_lap_ordinal": payload["target_lap_ordinal"],
                    "track_state_authenticity": payload["capabilities"]["track_state_authenticity"],
                    "trusted_readiness_status": payload["trusted_readiness_status"],
                }
        except FileNotFoundError as exc:
            _print_error(CONDITION_COHORT_CONTRACT_VERSION, "WAIT_DATA", exc)
            return 4
        except (
            ConditionCohortError,
            IbtFormatError,
            OSError,
            OverflowError,
            RecursionError,
            TelemetryAdapterError,
            ValueError,
        ) as exc:
            _print_error(
                CONDITION_COHORT_CONTRACT_VERSION,
                "CONDITION_COHORT_ERROR",
                exc,
            )
            return 3
    elif args.command == "driving-labels":
        from .driving_labels import (
            APPROVED,
            DRIVING_LABELS_CONTRACT_VERSION,
            SELF_ATTESTED_NOT_AUTHENTICATED,
            WAIT_HUMAN_AUTHENTICATION,
            DrivingLabelsError,
            build_driving_label_candidate,
            regress_driving_labels,
            validate_driving_labels,
        )

        try:
            if args.driving_labels_action == "propose":
                if args.output.exists():
                    raise FileExistsError(
                        f"driving-label candidate output already exists: {args.output}"
                    )
                if not args.output.parent.is_dir():
                    raise ValueError(
                        f"driving-label output directory does not exist: {args.output.parent}"
                    )
                replay_payload = _read_json_object(args.replay, "driving replay input")
                candidate = build_driving_label_candidate(
                    replay_payload,
                    label_set_id=args.label_set_id,
                    car_key=args.car_key,
                    track_key=args.track_key,
                    layout_key=args.layout_key,
                    condition_scope=args.condition_scope,
                )
                _write_new_json(args.output, candidate)
                payload = {
                    "artifact_path": str(args.output),
                    "artifact_sha256": candidate["artifact_sha256"],
                    "candidate_payload_sha256": candidate["candidate_payload_sha256"],
                    "contract_version": DRIVING_LABELS_CONTRACT_VERSION,
                    "reason": "INDEPENDENT_HUMAN_REVIEW_REQUIRED",
                    "review_authenticity_status": candidate["review"]["authenticity_status"],
                    "review_status": candidate["review"]["status"],
                    "status": "WAIT_HUMAN_LABELS",
                }
                failed_driving_labels = True
            elif args.driving_labels_action == "validate":
                labels_payload = validate_driving_labels(
                    _read_json_object(args.labels, "driving labels input")
                )
                review_status = labels_payload["review"]["status"]
                authenticity_status = labels_payload["review"]["authenticity_status"]
                if review_status != APPROVED:
                    validation_status = "WAIT_HUMAN_LABELS"
                    validation_reason = "LABEL_SET_NOT_APPROVED"
                elif authenticity_status == SELF_ATTESTED_NOT_AUTHENTICATED:
                    validation_status = WAIT_HUMAN_AUTHENTICATION
                    validation_reason = SELF_ATTESTED_NOT_AUTHENTICATED
                else:
                    validation_status = "PASS"
                    validation_reason = None
                failed_driving_labels = validation_status != "PASS"
                payload = {
                    "artifact_sha256": labels_payload["artifact_sha256"],
                    "candidate_payload_sha256": labels_payload["candidate_payload_sha256"],
                    "contract_version": DRIVING_LABELS_CONTRACT_VERSION,
                    "labels_content_sha256": labels_payload["labels_content_sha256"],
                    "reason": validation_reason,
                    "review_authenticity_status": authenticity_status,
                    "review_status": review_status,
                    "structural_validation_status": "PASS",
                    "status": validation_status,
                    "trusted_status": validation_status,
                }
            else:
                labels_payload = _read_json_object(args.labels, "driving labels input")
                replay_payload = _read_json_object(args.replay, "driving replay input")
                payload = regress_driving_labels(labels_payload, replay_payload)
                failed_driving_labels = payload["trusted_regression_status"] != "PASS"
        except FileNotFoundError as exc:
            _print_error(DRIVING_LABELS_CONTRACT_VERSION, "WAIT_DATA", exc)
            return 4
        except FileExistsError as exc:
            _print_error(DRIVING_LABELS_CONTRACT_VERSION, "OUTPUT_EXISTS", exc)
            return 3
        except (
            DrivingLabelsError,
            OSError,
            OverflowError,
            RecursionError,
            ValueError,
        ) as exc:
            _print_error(DRIVING_LABELS_CONTRACT_VERSION, "DRIVING_LABELS_ERROR", exc)
            return 3
    elif args.command == "shadow":
        from .contracts import SHADOW_REPORT_CONTRACT_VERSION
        from .ibt import IbtFormatError
        from .shadow import ShadowReportError, build_shadow_report

        fuel_scenario = _shadow_fuel_scenario(parser, args)
        try:
            payload = build_shadow_report(
                args.path,
                analysis=args.analysis,
                fuel_scenario=fuel_scenario,
                grid_step_m=args.grid_m,
                top=args.top,
            )
        except FileNotFoundError as exc:
            _print_error(
                SHADOW_REPORT_CONTRACT_VERSION,
                "WAIT_DATA",
                exc,
            )
            return 4
        except (IbtFormatError, OSError, ShadowReportError, ValueError) as exc:
            _print_error(
                SHADOW_REPORT_CONTRACT_VERSION,
                "SHADOW_ANALYSIS_ERROR",
                exc,
            )
            return 3
        failed_required = any(
            payload["capabilities"][name]["status"] != "PASS" for name in args.require_capability
        )
        if args.receipt_only:
            payload = {
                "contract_version": payload["contract_version"],
                "receipt": payload["receipt"],
            }
    elif args.command == "m0-accept":
        from .m0 import M0_ACCEPTANCE_CONTRACT_VERSION, M0AcceptanceError, accept_m0

        scenario = _fuel_scenario(parser, args)
        try:
            payload = accept_m0(
                args.capture,
                launch_receipt_path=args.launch_receipt,
                install_manifest_path=args.install_manifest,
                performance_receipt_path=args.performance_receipt,
                scenario=scenario,
                expected_installer_sha256=args.expected_installer_sha256,
                expected_launcher_sha256=args.expected_launcher_sha256,
                expected_requirements_sha256=args.expected_requirements_sha256,
                expected_wheel_sha256=args.expected_wheel_sha256,
                expected_wheelhouse_manifest_sha256=(args.expected_wheelhouse_manifest_sha256),
                minimum_capture_s=args.minimum_capture_seconds,
                subprocess_timeout_s=args.subprocess_timeout_seconds,
            )
            failed_m0 = payload["overall_gate"]["status"] != "PASS"
        except (M0AcceptanceError, OSError, OverflowError, RecursionError) as exc:
            _print_error(M0_ACCEPTANCE_CONTRACT_VERSION, "M0_ACCEPTANCE_ERROR", exc)
            return 3
    elif args.command == "live-preflight":
        from .live_preflight import (
            LIVE_PREFLIGHT_CONTRACT_VERSION,
            LivePreflightError,
            _run_windows_live_preflight_cli_only,
        )

        try:
            if args.output.exists():
                raise FileExistsError(f"preflight output already exists: {args.output}")
            payload = _run_windows_live_preflight_cli_only(
                args.output,
                source_id=args.source_id,
                session_id=args.session_id,
                expected_sim_process_id=args.expected_sim_process_id,
                expected_sim_start_time_utc_ticks=(
                    args.expected_sim_start_time_utc_ticks
                ),
                expected_windows_session_id=args.expected_windows_session_id,
                wait_seconds=args.wait_seconds,
                duration_s=args.duration_seconds,
                poll_seconds=args.poll_seconds,
                stale_after_s=args.stale_after_seconds,
            )
            failed_live_preflight = payload["status"] != "PASS"
        except FileExistsError as exc:
            _print_error(LIVE_PREFLIGHT_CONTRACT_VERSION, "OUTPUT_EXISTS", exc)
            return 3
        except (
            CollectorConsistencyError,
            LivePreflightError,
            OSError,
            OverflowError,
            RecursionError,
            SdkProbeConsistencyError,
            SdkProbeUnavailable,
            ValueError,
        ) as exc:
            _print_error(LIVE_PREFLIGHT_CONTRACT_VERSION, "LIVE_PREFLIGHT_ERROR", exc)
            return 3
    elif args.command == "sdk-probe":
        try:
            payload = probe_live_sdk(
                wait_seconds=args.wait_seconds,
                sample_seconds=args.sample_seconds,
                poll_seconds=args.poll_seconds,
                include_full_schema=args.full_schema,
            )
        except SdkProbeUnavailable as exc:
            print(str(exc), file=sys.stderr)
            print(
                json.dumps(
                    {
                        "contract_version": SDK_PROBE_CONTRACT_VERSION,
                        "error": "SDK_UNAVAILABLE",
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 2
        except SdkProbeConsistencyError as exc:
            print(str(exc), file=sys.stderr)
            print(
                json.dumps(
                    {
                        "contract_version": SDK_PROBE_CONTRACT_VERSION,
                        "error": "SDK_CONSISTENCY_ERROR",
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 3
    elif args.command == "monitor-live":
        def emit_live_monitor_record(record: dict[str, object]) -> None:
            print(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

        def emit_live_monitor_error(code: str, error: BaseException) -> None:
            print(str(error), file=sys.stderr)
            emit_live_monitor_record(
                {
                    "contract_version": LIVE_MONITOR_CONTRACT_VERSION,
                    "error": code,
                    "message": str(error),
                    "record_type": "live_monitor_error",
                }
            )

        try:
            receipt = monitor_live_transport(
                WindowsPyirsdkTransport(),
                emit=emit_live_monitor_record,
                source_id=args.source_id,
                session_id=args.session_id,
                expected_source_kind=_collector_source_kind(
                    args.expected_source_kind
                ),
                wait_seconds=args.wait_seconds,
                duration_s=args.duration_seconds,
                poll_seconds=args.poll_seconds,
                snapshot_seconds=args.snapshot_seconds,
                stale_after_s=args.stale_after_seconds,
            )
            emit_live_monitor_record(
                {
                    "record_type": "live_monitor_receipt",
                    "receipt": receipt.to_dict(),
                }
            )
            if args.require_in_car and receipt.in_car_snapshot_count == 0:
                return 5
            return 0
        except SdkProbeUnavailable as exc:
            emit_live_monitor_error("SDK_UNAVAILABLE", exc)
            return 2
        except LiveMonitorError as exc:
            emit_live_monitor_error(exc.code, exc)
            return 3
        except SdkProbeConsistencyError as exc:
            emit_live_monitor_error("SDK_CONSISTENCY_ERROR", exc)
            return 3
        except OSError as exc:
            emit_live_monitor_error("IO_ERROR", exc)
            return 3
    elif args.command == "collect-live":
        try:
            if args.output.exists():
                raise FileExistsError(f"collector output already exists: {args.output}")
            transport = WindowsPyirsdkTransport()
            receipt = collect_transport_to_jsonl(
                transport,
                args.output,
                source_id=args.source_id,
                session_id=args.session_id,
                expected_source_kind=_collector_source_kind(args.expected_source_kind),
                wait_seconds=args.wait_seconds,
                duration_s=args.duration_seconds,
                poll_seconds=args.poll_seconds,
                fields=None,
                stale_after_s=args.stale_after_seconds,
                include_driver_info=False,
                fsync_each_record=True,
            )
            payload = receipt.to_dict()
        except SdkProbeUnavailable as exc:
            _print_error(COLLECTOR_CONTRACT_VERSION, "SDK_UNAVAILABLE", exc)
            return 2
        except CollectorConsistencyError as exc:
            _print_error(COLLECTOR_CONTRACT_VERSION, "COLLECTOR_CONSISTENCY_ERROR", exc)
            return 3
        except SdkProbeConsistencyError as exc:
            _print_error(COLLECTOR_CONTRACT_VERSION, "SDK_CONSISTENCY_ERROR", exc)
            return 3
        except FileExistsError as exc:
            _print_error(COLLECTOR_CONTRACT_VERSION, "OUTPUT_EXISTS", exc)
            return 3
        except OSError as exc:
            _print_error(COLLECTOR_CONTRACT_VERSION, "IO_ERROR", exc)
            return 3
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if (
        args.command == "sdk-probe"
        and args.require_capability
        and payload["capabilities"][args.require_capability]["status"] != "READY"
    ):
        return 5
    if args.command == "live-preflight" and failed_live_preflight:
        return 5
    if args.command == "shadow" and failed_required:
        return 5
    if args.command == "offline-demo" and args.require_trusted and failed_offline_demo_trust:
        return 5
    if args.command == "fuel-replay" and args.require_ready and failed_fuel_replay:
        return 5
    if args.command == "driving-replay" and args.require_ready and failed_driving_replay:
        return 5
    if args.command == "condition-cohort" and args.require_ready and failed_condition_cohort:
        return 5
    if args.command == "driving-labels" and failed_driving_labels:
        return 5
    if args.command == "m0-accept" and args.require_pass and failed_m0:
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

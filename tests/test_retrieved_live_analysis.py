from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

import iracing_ai_engineer.cli as cli_module
import iracing_ai_engineer.retrieved_live_analysis as analysis_module
from iracing_ai_engineer.cli import main as cli_main
from iracing_ai_engineer.collector import CollectorSample, collect_samples_to_jsonl
from iracing_ai_engineer.engineer_session import canonical_sha256
from iracing_ai_engineer.live_engineer_session import build_live_engineer_session
from iracing_ai_engineer.retrieved_live_analysis import (
    MATCHED_PIT_CALIBRATION_DATASET_CONTRACT_VERSION,
    RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION,
    RETRIEVED_LIVE_ANALYSIS_PROFILE_CONTRACT_VERSION,
    RetrievedLiveAnalysisError,
    build_matched_pit_calibration_model,
    build_retrieved_live_analysis_profile,
    build_time_domain_rejoin_estimate,
    validate_retrieved_live_analysis_profile,
    validate_retrieved_live_analysis_receipt,
    verify_retrieved_live_analysis_bundle,
    write_retrieved_live_analysis_bundle_exclusive,
)
from iracing_ai_engineer.sdk_probe import RawSdkFrame


def _load_live_fixture_module() -> ModuleType:
    path = Path(__file__).with_name("test_live_engineer_session.py")
    spec = importlib.util.spec_from_file_location(
        "_retrieved_live_analysis_fixture", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _profile(*, fallback_laps: int | None = 10) -> dict[str, object]:
    material: dict[str, object] = {
        "contract_version": RETRIEVED_LIVE_ANALYSIS_PROFILE_CONTRACT_VERSION,
        "fuel_model": {
            "conservative_quantile": 0.9,
            "minimum_valid_laps": 5,
            "refuel_rate_l_per_s": 2.0,
            "reserve_l": 1.0,
            "tank_capacity_l": 120.0,
            "timed_race_extra_laps": 1,
        },
        "horizon_fallback": {
            "reference_lap_time_s": None,
            "remaining_laps": fallback_laps,
            "remaining_time_s": None,
        },
        "profile_id": "synthetic-user-event-profile",
        "profile_version": 1,
    }
    return {**material, "analysis_profile_sha256": canonical_sha256(material)}


def _complete_identity_calibration() -> dict[str, object]:
    identity = {
        "car_class_id": 27,
        "event_type": "Race",
        "official": True,
        "provenance": "SDK_DIRECT_SAME_SOURCE_CAPTURE",
        "race_week": 12,
        "season_id": 601,
        "series_id": 501,
        "sim_build": "2026.08.31.02",
        "track_config": "Grand Prix Pits",
        "track_id": 101,
    }
    material: dict[str, object] = {
        "contract_version": MATCHED_PIT_CALIBRATION_DATASET_CONTRACT_VERSION,
        "dataset_id": "complete-live-calibration-fixture",
        "dataset_version": 1,
        "event_identity": identity,
        "samples": [
            {
                "fuel_delivered_l": 30.0,
                "fuel_service_elapsed_s": 15.0,
                "label_receipt_sha256": "1" * 64,
                "matched_track_segment_elapsed_s": 30.0,
                "pit_road_elapsed_s": 60.0,
                "sample_id": "stop-1",
                "source_receipt_sha256": "4" * 64,
                "stationary_service_elapsed_s": 20.0,
                "tire_change_elapsed_s": 18.0,
            },
            {
                "fuel_delivered_l": 32.0,
                "fuel_service_elapsed_s": 16.0,
                "label_receipt_sha256": "2" * 64,
                "matched_track_segment_elapsed_s": 31.0,
                "pit_road_elapsed_s": 63.0,
                "sample_id": "stop-2",
                "source_receipt_sha256": "5" * 64,
                "stationary_service_elapsed_s": 21.0,
                "tire_change_elapsed_s": 19.0,
            },
            {
                "fuel_delivered_l": 29.0,
                "fuel_service_elapsed_s": 14.5,
                "label_receipt_sha256": "3" * 64,
                "matched_track_segment_elapsed_s": 32.0,
                "pit_road_elapsed_s": 61.0,
                "sample_id": "stop-3",
                "source_receipt_sha256": "6" * 64,
                "stationary_service_elapsed_s": 19.0,
                "tire_change_elapsed_s": 17.0,
            },
        ],
    }
    dataset = {**material, "dataset_sha256": canonical_sha256(material)}
    return build_matched_pit_calibration_model(
        dataset,
        expected_dataset_sha256=str(dataset["dataset_sha256"]),
    )


def _complete_identity_rules() -> dict[str, object]:
    material: dict[str, object] = {
        "contract_version": "event-rules-profile-v2",
        "official_rules": {
            "finish_rule": "LAP_LIMITED",
            "fuel_tire_service_timing": "SEQUENTIAL",
            "minimum_pit_stops": 0,
            "no_tire_service_allowed": False,
            "tire_change_required": True,
        },
        "profile_id": "synthetic-complete-live-rules",
        "profile_version": 1,
        "selector": {
            "car_class_id": 27,
            "event_type": "Race",
            "race_week": 12,
            "season_id": 601,
            "series_id": 501,
            "sim_build": "2026.08.31.02",
            "track_config": "Grand Prix Pits",
            "track_id": 101,
        },
        "source": {
            "authority": "IRACING_OFFICIAL",
            "document_id": "synthetic-contract-fixture-not-real-rules",
            "document_sha256": "e" * 64,
        },
    }
    return {**material, "profile_sha256": canonical_sha256(material)}


def _complete_identity_tire_performance_model() -> dict[str, object]:
    identity = {
        "car_class_id": 27,
        "event_type": "Race",
        "official": True,
        "provenance": "SDK_DIRECT_SAME_SOURCE_CAPTURE",
        "race_week": 12,
        "season_id": 601,
        "series_id": 501,
        "sim_build": "2026.08.31.02",
        "track_config": "Grand Prix Pits",
        "track_id": 101,
    }
    physical_wear = {
        "estimate_available": False,
        "measured_current_set": False,
        "provenance": "UNKNOWN",
        "reason_codes": [
            "NO_CURRENT_SET_DIRECT_WEAR_MEASUREMENT",
            "PERFORMANCE_SLOPE_IS_NOT_PHYSICAL_WEAR",
        ],
        "status": "SKIP_CURRENT_PHYSICAL_WEAR",
    }
    material: dict[str, object] = {
        "advisor_only": True,
        "contract_version": "tire-performance-model-v1",
        "estimate_available": True,
        "fuel_load_model_sha256": "f" * 64,
        "identity_sha256": canonical_sha256(identity),
        "independent_stint_count": 3,
        "max_supported_stint_age_laps": 100,
        "method_version": "fuel-adjusted-disjoint-pair-envelope-v1",
        "pair_count": 3,
        "performance_age_slope_s_per_lap": 10.0,
        "performance_age_slope_uncertainty_s_per_lap": [9.0, 11.0],
        "physical_wear": physical_wear,
        "source_receipt_sha256": "9" * 64,
        "status": "PASS_SHADOW_POSITIVE_DEGRADATION",
        "tire_compound": 0,
    }
    return {**material, "model_sha256": canonical_sha256(material)}


def _write_complete_identity_live_capture(
    path: Path,
    *,
    session_flags: int = 0,
) -> tuple[ModuleType, dict[str, object], dict[str, object]]:
    helper = _load_live_fixture_module()
    fixture = helper._load_paired_fixture_module()
    frames = [
        {
            **item,
            "PitsOpen": True,
            "PlayerCarClass": 27,
            "SessionFlags": session_flags,
        }
        for item in fixture._paired_frames()
    ]
    descriptors = fixture._descriptors(frames[0])
    session_info = {
        "WeekendInfo": {
            "BuildVersion": "2026.08.31.02",
            "EventType": "Race",
            "Official": 1,
            "RaceWeek": 12,
            "SeasonID": 601,
            "SeriesID": 501,
            "SimMode": "full",
            "TrackConfigName": "Grand Prix Pits",
            "TrackID": 101,
            "TrackLength": "300 m",
        },
        "DriverInfo": {
            "DriverUserID": 123456,
            "Drivers": [{"UserName": "Private Person"}],
        },
    }
    samples = [
        CollectorSample(
            frame=RawSdkFrame(
                buffer_tick=20_000 + index,
                session_info_update=1,
                values=frame,
                sim_mode_raw="full",
                captured_monotonic_s=float(frame["SessionTime"]),
            ),
            descriptors=descriptors,
            tick_rate_hz=fixture.TICK_RATE_HZ,
            session_info=session_info,
        )
        for index, frame in enumerate(frames)
    ]
    collect_samples_to_jsonl(
        samples,
        path,
        source_id="aeis-windows-sdk",
        session_id=f"live-{helper.RUN_ID}",
        stale_after_s=1.0,
        fsync_each_record=False,
    )
    with path.open("r+b", buffering=0) as raw:
        authority = helper._authority(raw, filename=path.name)
        with helper._active_run(raw) as run:
            live = build_live_engineer_session(
                run,
                raw,
                analysis_authority=authority,
                stale_after_s=1.0,
            )
    return helper, authority, live


@pytest.fixture(scope="module")
def live_input(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, dict[str, object], dict[str, object], dict[str, object]]:
    helper = _load_live_fixture_module()
    root = tmp_path_factory.mktemp("retrieved-live-analysis-input")
    capture = root / "live-20260823T220000Z.jsonl"
    helper._write_live_collector(capture, helper._load_paired_fixture_module())
    with capture.open("r+b", buffering=0) as raw:
        authority = helper._authority(raw, filename=capture.name)
        with helper._active_run(raw) as run:
            live = build_live_engineer_session(
                run,
                raw,
                analysis_authority=authority,
                stale_after_s=1.0,
            )
    return capture, authority, live, _profile()


@pytest.fixture(scope="module")
def analysis_bundle(
    live_input: tuple[Path, dict[str, object], dict[str, object], dict[str, object]],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, Path, Path, Path, dict[str, object], dict[str, object], dict[str, object]]:
    capture, authority, live, profile = live_input
    root = tmp_path_factory.mktemp("retrieved-live-analysis-output")
    session = root / "engineer-session.json"
    report = root / "session-report.json"
    html = root / "session-report.html"
    receipt_path = root / "analysis-bundle.json"
    with capture.open("rb", buffering=0) as handle:
        receipt = write_retrieved_live_analysis_bundle_exclusive(
            handle,
            live,
            profile,
            session,
            report,
            html,
            receipt_path,
            expected_live_engineer_session_sha256=str(
                live["live_engineer_session_sha256"]
            ),
            expected_remote_capture_sha256=str(authority["capture_sha256"]),
            expected_remote_capture_byte_size=int(authority["capture_byte_size"]),
            expected_analysis_profile_sha256=str(
                profile["analysis_profile_sha256"]
            ),
            stale_after_s=1.0,
        )
    return capture, session, report, html, receipt_path, authority, live, receipt


def test_profile_is_exact_self_hashed_and_independently_bound() -> None:
    profile = _profile()
    assert validate_retrieved_live_analysis_profile(
        profile,
        expected_analysis_profile_sha256=str(profile["analysis_profile_sha256"]),
    ) == profile

    with pytest.raises(RetrievedLiveAnalysisError) as raised:
        validate_retrieved_live_analysis_profile(
            profile,
            expected_analysis_profile_sha256="0" * 64,
        )
    assert raised.value.code == "PROFILE_INVALID"


def test_profile_builder_and_cli_never_accept_current_fuel(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile = build_retrieved_live_analysis_profile(
        profile_id="spa-gt3-user-rules",
        profile_version=1,
        tank_capacity_l=120.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=10,
    )
    assert "current_fuel_l" not in json.dumps(profile)
    output = tmp_path / "profile.json"
    result = cli_main(
        [
            "make-live-analysis-profile",
            str(output),
            "--profile-id",
            "spa-gt3-user-rules",
            "--tank-capacity-l",
            "120",
            "--refuel-rate-lps",
            "2",
            "--remaining-laps",
            "10",
        ]
    )
    assert result == 0
    summary = json.loads(capsys.readouterr().out)
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted == profile
    assert summary["analysis_profile_sha256"] == profile[
        "analysis_profile_sha256"
    ]
    assert summary["status"] == "PASS_PROFILE_CREATED"


def test_profile_rejects_ambiguous_or_missing_fallback_at_use_time(
    live_input: tuple[Path, dict[str, object], dict[str, object], dict[str, object]],
    tmp_path: Path,
) -> None:
    capture, authority, live, _ = live_input
    profile = _profile(fallback_laps=None)
    with (
        capture.open("rb", buffering=0) as handle,
        pytest.raises(RetrievedLiveAnalysisError) as raised,
    ):
        write_retrieved_live_analysis_bundle_exclusive(
                handle,
                live,
                profile,
                tmp_path / "session.json",
                tmp_path / "report.json",
                tmp_path / "report.html",
                tmp_path / "receipt.json",
                expected_live_engineer_session_sha256=str(
                    live["live_engineer_session_sha256"]
                ),
                expected_remote_capture_sha256=str(authority["capture_sha256"]),
                expected_remote_capture_byte_size=int(
                    authority["capture_byte_size"]
                ),
                expected_analysis_profile_sha256=str(
                    profile["analysis_profile_sha256"]
                ),
                stale_after_s=1.0,
        )
    assert raised.value.code == "HORIZON_UNAVAILABLE"
    assert not list(tmp_path.iterdir())


def test_bundle_closes_sdk_live_source_outputs_and_safety(
    analysis_bundle: tuple[
        Path,
        Path,
        Path,
        Path,
        Path,
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ],
) -> None:
    _, session_path, report_path, html_path, receipt_path, authority, live, receipt = (
        analysis_bundle
    )
    assert receipt["contract_version"] == RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION
    assert receipt["source_binding"]["source_kind"] == "SDK_LIVE"
    assert receipt["capture_binding"]["capture_sha256"] == authority["capture_sha256"]
    assert receipt["capture_binding"]["live_engineer_session_sha256"] == live[
        "live_engineer_session_sha256"
    ]
    assert receipt["horizon_binding"]["source"] == "USER_RULE_LAPS"
    assert receipt["advisor_only"] is True
    assert receipt["safety"] == {
        "audio_emitted": False,
        "executable": False,
        "network_accessed": False,
        "pit_black_box_control_enabled": False,
        "vehicle_control_enabled": False,
    }
    assert receipt["status"] == "WAIT_ADVICE"
    assert receipt["readiness"]["strategy_advice_available"] is False
    assert receipt["rules_binding"]["status"] == "WAIT_EVENT_RULES_IDENTITY"
    assert validate_retrieved_live_analysis_receipt(
        json.loads(receipt_path.read_text(encoding="utf-8")),
        expected_bundle_receipt_sha256=str(receipt["bundle_receipt_sha256"]),
    ) == receipt

    session = json.loads(session_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert session["input_lineage"]["source_kind"] == "SDK_LIVE"
    fuel_scenario = session["components"]["fuel_replay"]["scenario"]
    assert fuel_scenario["current_fuel_l"]["provenance"] == "SDK_DIRECT"
    assert fuel_scenario["remaining_laps"]["provenance"] == "USER_RULE"
    assert fuel_scenario["tank_capacity_l"]["provenance"] == "USER_RULE"
    assert session["components"]["m2_strategy"]["strategy_context"]["horizon"][
        "provenance"
    ] == "USER_RULE"
    assert report["sections"]["fuel"]["strategy_numbers_exposed"] is False
    html = html_path.read_bytes().lower()
    assert b"<script" not in html
    assert b"http://" not in html and b"https://" not in html


def test_bundle_promotes_complete_same_capture_identity_and_penalty_state(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "live-20260823T220000Z.jsonl"
    _, authority, live = _write_complete_identity_live_capture(capture)
    profile = _profile()
    session_path = tmp_path / "complete-identity-session.json"
    with capture.open("rb", buffering=0) as handle:
        receipt = write_retrieved_live_analysis_bundle_exclusive(
            handle,
            live,
            profile,
            session_path,
            tmp_path / "complete-identity-report.json",
            tmp_path / "complete-identity-report.html",
            tmp_path / "complete-identity-receipt.json",
            expected_live_engineer_session_sha256=str(
                live["live_engineer_session_sha256"]
            ),
            expected_remote_capture_sha256=str(authority["capture_sha256"]),
            expected_remote_capture_byte_size=int(authority["capture_byte_size"]),
            expected_analysis_profile_sha256=str(
                profile["analysis_profile_sha256"]
            ),
            stale_after_s=1.0,
        )

    session = json.loads(session_path.read_text(encoding="utf-8"))
    context = session["components"]["m2_strategy"]["strategy_context"]
    assert context["event_identity"] == {
        "car_class_id": 27,
        "event_type": "Race",
        "official": True,
        "provenance": "SDK_DIRECT_SAME_SOURCE_CAPTURE",
        "race_week": 12,
        "season_id": 601,
        "series_id": 501,
        "sim_build": "2026.08.31.02",
        "track_config": "Grand Prix Pits",
        "track_id": 101,
    }
    assert context["observation"]["penalty_state"] == "CLEAR"
    assert context["observation"]["pits_open"] is True
    assert session["components"]["m2_strategy"]["capabilities"][
        "pit_open_and_penalty_state"
    ]["status"] == "PASS_PIT_OPEN_AND_PENALTY_STATE"
    traffic = context["traffic_rejoin"]
    assert isinstance(traffic, dict)
    assert traffic["estimate_available"] is False
    assert traffic["rejoin_gap_range_s"] is None
    assert traffic["status"] == "OBSERVED_ONLY_WAIT_PIT_LOSS"
    assert session["components"]["m2_strategy"]["traffic_rejoin"][
        "status"
    ] == "WAIT_REJOIN_ESTIMATE"
    assert session["components"]["m2_strategy"]["capabilities"][
        "traffic_data"
    ] == {
        "reason_codes": [
            "PIT_LOSS_CALIBRATION_REQUIRED_FOR_REJOIN_ESTIMATE"
        ],
        "status": "WAIT_REJOIN_ESTIMATE",
    }
    assert receipt["source_binding"]["source_kind"] == "SDK_LIVE"
    assert receipt["readiness"]["strategy_advice_available"] is False
    assert "Private Person" not in capture.read_text(encoding="utf-8")
    assert "DriverUserID" not in json.dumps(session)


def test_bundle_binds_matched_calibration_and_advances_to_rejoin_model_wait(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "live-20260823T220000Z.jsonl"
    _, authority, live = _write_complete_identity_live_capture(capture)
    profile = _profile()
    calibration = _complete_identity_calibration()
    session_path = tmp_path / "calibrated-session.json"
    report_path = tmp_path / "calibrated-report.json"
    html_path = tmp_path / "calibrated-report.html"
    receipt_path = tmp_path / "calibrated-receipt.json"
    with capture.open("rb", buffering=0) as handle:
        receipt = write_retrieved_live_analysis_bundle_exclusive(
            handle,
            live,
            profile,
            session_path,
            report_path,
            html_path,
            receipt_path,
            expected_live_engineer_session_sha256=str(
                live["live_engineer_session_sha256"]
            ),
            expected_remote_capture_sha256=str(authority["capture_sha256"]),
            expected_remote_capture_byte_size=int(authority["capture_byte_size"]),
            expected_analysis_profile_sha256=str(
                profile["analysis_profile_sha256"]
            ),
            calibration_model=calibration,
            expected_calibration_model_sha256=str(calibration["model_sha256"]),
            expected_calibration_source_receipt_sha256=str(
                calibration["source_receipt_sha256"]
            ),
            stale_after_s=1.0,
        )

    session = json.loads(session_path.read_text(encoding="utf-8"))
    strategy = session["components"]["m2_strategy"]
    assert strategy["calibration"]["calibrated_model"] == calibration
    assert strategy["capabilities"]["pit_loss_calibration"] == {
        "reason_codes": [],
        "status": "PASS_CALIBRATED",
    }
    assert strategy["capabilities"]["service_labels"] == {
        "reason_codes": [],
        "status": "PASS_SERVICE_LABELS",
    }
    assert strategy["strategy_context"]["traffic_rejoin"]["status"] == (
        "OBSERVED_ONLY_WAIT_ACTION_BOUND_REJOIN"
    )
    assert strategy["capabilities"]["traffic_data"] == {
        "reason_codes": ["ACTION_BOUND_REJOIN_ESTIMATE_REQUIRED"],
        "status": "WAIT_REJOIN_ESTIMATE",
    }
    assert receipt["readiness"]["strategy_advice_available"] is False

    with capture.open("rb", buffering=0) as handle:
        verified = verify_retrieved_live_analysis_bundle(
            handle,
            live,
            profile,
            session_path.read_bytes(),
            report_path.read_bytes(),
            html_path.read_bytes(),
            receipt,
            expected_live_engineer_session_sha256=str(
                live["live_engineer_session_sha256"]
            ),
            expected_remote_capture_sha256=str(authority["capture_sha256"]),
            expected_remote_capture_byte_size=int(authority["capture_byte_size"]),
            expected_analysis_profile_sha256=str(
                profile["analysis_profile_sha256"]
            ),
            expected_bundle_receipt_sha256=str(receipt["bundle_receipt_sha256"]),
            calibration_model=calibration,
            expected_calibration_model_sha256=str(calibration["model_sha256"]),
            expected_calibration_source_receipt_sha256=str(
                calibration["source_receipt_sha256"]
            ),
            stale_after_s=1.0,
        )
    assert verified == receipt


def test_calibration_identity_must_match_same_capture_identity_before_outputs(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "live-20260823T220000Z.jsonl"
    _, authority, live = _write_complete_identity_live_capture(capture)
    profile = _profile()
    calibration = _complete_identity_calibration()
    changed = copy.deepcopy(calibration)
    changed["identity_sha256"] = "0" * 64
    changed["model_sha256"] = canonical_sha256(
        {key: value for key, value in changed.items() if key != "model_sha256"}
    )

    with (
        capture.open("rb", buffering=0) as handle,
        pytest.raises(RetrievedLiveAnalysisError) as raised,
    ):
        write_retrieved_live_analysis_bundle_exclusive(
            handle,
            live,
            profile,
            tmp_path / "rejected-session.json",
            tmp_path / "rejected-report.json",
            tmp_path / "rejected-report.html",
            tmp_path / "rejected-receipt.json",
            expected_live_engineer_session_sha256=str(
                live["live_engineer_session_sha256"]
            ),
            expected_remote_capture_sha256=str(authority["capture_sha256"]),
            expected_remote_capture_byte_size=int(authority["capture_byte_size"]),
            expected_analysis_profile_sha256=str(
                profile["analysis_profile_sha256"]
            ),
            calibration_model=changed,
            expected_calibration_model_sha256=str(changed["model_sha256"]),
            expected_calibration_source_receipt_sha256=str(
                changed["source_receipt_sha256"]
            ),
            stale_after_s=1.0,
        )

    assert raised.value.code == "ANALYSIS_COMPONENT_FAILED"
    assert "calibration model identity differs" in str(raised.value)
    assert not any(path.name.startswith("rejected-") for path in tmp_path.iterdir())


def test_same_capture_motion_closes_action_bound_rejoin_and_replays_exactly(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "live-20260823T220000Z.jsonl"
    _, authority, live = _write_complete_identity_live_capture(capture)
    profile = _profile()
    calibration = _complete_identity_calibration()
    rules = _complete_identity_rules()
    session_path = tmp_path / "rejoin-session.json"
    report_path = tmp_path / "rejoin-report.json"
    html_path = tmp_path / "rejoin-report.html"
    receipt_path = tmp_path / "rejoin-receipt.json"
    with capture.open("rb", buffering=0) as handle:
        receipt = write_retrieved_live_analysis_bundle_exclusive(
            handle,
            live,
            profile,
            session_path,
            report_path,
            html_path,
            receipt_path,
            expected_live_engineer_session_sha256=str(
                live["live_engineer_session_sha256"]
            ),
            expected_remote_capture_sha256=str(authority["capture_sha256"]),
            expected_remote_capture_byte_size=int(authority["capture_byte_size"]),
            expected_analysis_profile_sha256=str(
                profile["analysis_profile_sha256"]
            ),
            calibration_model=calibration,
            expected_calibration_model_sha256=str(calibration["model_sha256"]),
            expected_calibration_source_receipt_sha256=str(
                calibration["source_receipt_sha256"]
            ),
            rules_profile=rules,
            expected_rules_profile_sha256=str(rules["profile_sha256"]),
            expected_rules_source_sha256="e" * 64,
            stale_after_s=1.0,
        )

    session = json.loads(session_path.read_text(encoding="utf-8"))
    strategy = session["components"]["m2_strategy"]
    recommendation = strategy["recommendations"][0]
    estimate = strategy["traffic_rejoin"]["estimate"]
    assert strategy["capabilities"]["traffic_data"] == {
        "reason_codes": [],
        "status": "PASS_TRAFFIC_DATA",
    }
    assert strategy["traffic_rejoin"]["status"] == "PASS_TRAFFIC_DATA"
    assert estimate["contract_version"] == "time-domain-rejoin-estimate-v1"
    assert estimate["estimate_available"] is True
    assert estimate["status"] == "AVAILABLE_STABLE_BRACKET"
    assert estimate["decision_tick"] == strategy["strategy_context"]["observation"][
        "decision_tick"
    ]
    assert estimate["motion_context_sha256"] == strategy["strategy_context"][
        "traffic_rejoin"
    ]["motion_context_sha256"]
    assert estimate["service_scenario"]["fuel_add_l"] == recommendation["action"][
        "fuel_add_l"
    ]
    assert estimate["service_scenario"]["stationary_service_s"] == recommendation[
        "action"
    ]["estimated_stationary_service_s"]
    assert recommendation["evidence_ids"][-2] == (
        f"rejoin-estimate:{estimate['estimate_sha256']}"
    )
    assert recommendation["evidence_ids"][-1].startswith("tire-strategy:")
    assert strategy["tire_strategy"] == {
        "belief": None,
        "change_tires": True,
        "reason_codes": [],
        "status": "PASS_RULE_MANDATED_TIRE_CHANGE",
    }
    traffic_input = strategy["strategy_context"]["traffic_rejoin"]
    motion = traffic_input["motion_context"]
    independently_built = build_time_domain_rejoin_estimate(
        motion,
        calibration,
        expected_motion_sha256=str(motion["motion_sha256"]),
        expected_motion_source_receipt_sha256=str(
            motion["source_receipt_sha256"]
        ),
        expected_traffic_map_revision_sha256=str(
            motion["traffic_map_revision_sha256"]
        ),
        expected_calibration_model_sha256=str(calibration["model_sha256"]),
        expected_calibration_source_receipt_sha256=str(
            calibration["source_receipt_sha256"]
        ),
        expected_identity_sha256=str(traffic_input["identity_sha256"]),
        expected_decision_tick=int(traffic_input["observed_at_decision_tick"]),
        fuel_add_l=float(recommendation["action"]["fuel_add_l"]),
        change_tires=bool(recommendation["action"]["change_tires"]),
        fuel_tire_service_timing="SEQUENTIAL",
    )
    assert independently_built == estimate
    assert receipt["readiness"]["strategy_advice_available"] is True

    with capture.open("rb", buffering=0) as handle:
        verified = verify_retrieved_live_analysis_bundle(
            handle,
            live,
            profile,
            session_path.read_bytes(),
            report_path.read_bytes(),
            html_path.read_bytes(),
            receipt,
            expected_live_engineer_session_sha256=str(
                live["live_engineer_session_sha256"]
            ),
            expected_remote_capture_sha256=str(authority["capture_sha256"]),
            expected_remote_capture_byte_size=int(authority["capture_byte_size"]),
            expected_analysis_profile_sha256=str(
                profile["analysis_profile_sha256"]
            ),
            expected_bundle_receipt_sha256=str(receipt["bundle_receipt_sha256"]),
            calibration_model=calibration,
            expected_calibration_model_sha256=str(calibration["model_sha256"]),
            expected_calibration_source_receipt_sha256=str(
                calibration["source_receipt_sha256"]
            ),
            rules_profile=rules,
            expected_rules_profile_sha256=str(rules["profile_sha256"]),
            expected_rules_source_sha256="e" * 64,
            stale_after_s=1.0,
        )
    assert verified == receipt


def test_same_capture_tire_model_can_select_change_and_replay_exactly(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "live-20260823T220000Z.jsonl"
    _, authority, live = _write_complete_identity_live_capture(capture)
    profile = _profile()
    calibration = _complete_identity_calibration()
    tire_model = _complete_identity_tire_performance_model()
    rules = _complete_identity_rules()
    rules["official_rules"].update(
        {
            "no_tire_service_allowed": True,
            "tire_change_required": False,
        }
    )
    rules["profile_sha256"] = canonical_sha256(
        {key: value for key, value in rules.items() if key != "profile_sha256"}
    )
    session_path = tmp_path / "tire-session.json"
    report_path = tmp_path / "tire-report.json"
    html_path = tmp_path / "tire-report.html"
    receipt_path = tmp_path / "tire-receipt.json"
    kwargs = {
        "expected_live_engineer_session_sha256": str(
            live["live_engineer_session_sha256"]
        ),
        "expected_remote_capture_sha256": str(authority["capture_sha256"]),
        "expected_remote_capture_byte_size": int(authority["capture_byte_size"]),
        "expected_analysis_profile_sha256": str(
            profile["analysis_profile_sha256"]
        ),
        "calibration_model": calibration,
        "expected_calibration_model_sha256": str(calibration["model_sha256"]),
        "expected_calibration_source_receipt_sha256": str(
            calibration["source_receipt_sha256"]
        ),
        "tire_performance_model": tire_model,
        "expected_tire_performance_model_sha256": str(
            tire_model["model_sha256"]
        ),
        "expected_tire_performance_source_receipt_sha256": str(
            tire_model["source_receipt_sha256"]
        ),
        "rules_profile": rules,
        "expected_rules_profile_sha256": str(rules["profile_sha256"]),
        "expected_rules_source_sha256": "e" * 64,
        "stale_after_s": 1.0,
    }
    with capture.open("rb", buffering=0) as handle:
        receipt = write_retrieved_live_analysis_bundle_exclusive(
            handle,
            live,
            profile,
            session_path,
            report_path,
            html_path,
            receipt_path,
            **kwargs,
        )

    session = json.loads(session_path.read_text(encoding="utf-8"))
    strategy = session["components"]["m2_strategy"]
    tire_strategy = strategy["tire_strategy"]
    assert strategy["contract_version"] == "offline-m2-strategy-receipt-v2"
    assert tire_strategy["status"] == "PASS_MODEL_SELECTED_TIRE_CHANGE"
    assert tire_strategy["belief"]["status"] == "PASS_SHADOW_CHANGE_TIRES"
    assert tire_strategy["belief"]["physical_wear"]["estimate_available"] is False
    assert strategy["recommendations"][0]["action"]["change_tires"] is True
    assert receipt["readiness"]["strategy_advice_available"] is True

    with capture.open("rb", buffering=0) as handle:
        verified = verify_retrieved_live_analysis_bundle(
            handle,
            live,
            profile,
            session_path.read_bytes(),
            report_path.read_bytes(),
            html_path.read_bytes(),
            receipt,
            expected_bundle_receipt_sha256=str(receipt["bundle_receipt_sha256"]),
            **kwargs,
        )
    assert verified == receipt


def test_same_capture_black_flag_is_an_active_penalty(tmp_path: Path) -> None:
    capture = tmp_path / "live-20260823T220000Z.jsonl"
    _, authority, live = _write_complete_identity_live_capture(
        capture,
        session_flags=0x00010000,
    )
    with capture.open("rb", buffering=0) as handle:
        evidence = analysis_module._same_capture_strategy_evidence(
            handle,
            live["observed_live_evidence"],
            expected_capture_sha256=str(authority["capture_sha256"]),
            expected_capture_byte_size=int(authority["capture_byte_size"]),
            stale_after_s=1.0,
        )

    assert evidence["penalty_state"] == "ACTIVE"
    assert evidence["penalty_status"] == "PRESENT"
    assert evidence["session_flags"] == 0x00010000
    assert evidence["active_penalty_flag_mask"] & 0x00010000
    traffic = evidence["traffic_observation_context"]
    assert traffic["availability"] == "AVAILABLE"
    assert traffic["status"] == "VERIFIED"
    assert traffic["eligible_opponent_count"] == 1
    ahead = traffic["nearest_ahead"]
    behind = traffic["nearest_behind"]
    assert isinstance(ahead, dict)
    assert isinstance(behind, dict)
    assert ahead["car_idx"] == behind["car_idx"] == 1
    assert ahead["distance_mm"] == 120_000
    assert behind["distance_mm"] == 180_000
    assert ahead["lap_position_ppb"] == behind["lap_position_ppb"]
    assert ahead["race_lap_delta"] == behind["race_lap_delta"] == 0
    motion = evidence["traffic_motion_context"]
    assert motion["availability"] == "AVAILABLE"
    assert motion["status"] == "VERIFIED_TIME_DOMAIN_MOTION"
    assert motion["decision_tick"] == traffic["decision_tick"]
    assert motion["traffic_map_revision_sha256"] == traffic["context_sha256"]
    assert motion["player"]["car_idx"] == 0
    assert [item["car_idx"] for item in motion["opponents"]] == [1]
    tires = evidence["tire_stint_context"]
    assert tires["availability"] == "AVAILABLE"
    assert tires["status"] == "AVAILABLE_OBSERVED_STINT_AGE"
    assert tires["decision_tick"] == traffic["decision_tick"]
    assert tires["origin_kind"] == "OBSERVED_ZERO_COMPLETED_LAPS"
    assert tires["origin_laps_completed"] == 0
    assert tires["current_laps_completed"] == 6
    assert tires["stint_age_completed_laps"] == 6
    assert tires["current_tire_compound"] == 0
    assert tires["tire_sets_used"] == 1
    assert tires["physical_wear"]["estimate_available"] is False
    assert tires["physical_wear"]["status"] == "SKIP_CURRENT_PHYSICAL_WEAR"


def test_bundle_read_only_verifier_replays_every_object_and_html_byte(
    live_input: tuple[Path, dict[str, object], dict[str, object], dict[str, object]],
    analysis_bundle: tuple[
        Path,
        Path,
        Path,
        Path,
        Path,
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ],
) -> None:
    capture, authority, live, profile = live_input
    _, session_path, report_path, html_path, _, _, _, receipt = analysis_bundle
    with capture.open("rb", buffering=0) as handle:
        verified = verify_retrieved_live_analysis_bundle(
            handle,
            live,
            profile,
            session_path.read_bytes(),
            report_path.read_bytes(),
            html_path.read_bytes(),
            receipt,
            expected_live_engineer_session_sha256=str(
                live["live_engineer_session_sha256"]
            ),
            expected_remote_capture_sha256=str(authority["capture_sha256"]),
            expected_remote_capture_byte_size=int(authority["capture_byte_size"]),
            expected_analysis_profile_sha256=str(
                profile["analysis_profile_sha256"]
            ),
            expected_bundle_receipt_sha256=str(receipt["bundle_receipt_sha256"]),
            stale_after_s=1.0,
        )
    assert verified == receipt


def test_external_bundle_digest_rejects_a_total_rehash_attack(
    analysis_bundle: tuple[
        Path,
        Path,
        Path,
        Path,
        Path,
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ],
) -> None:
    receipt = copy.deepcopy(analysis_bundle[-1])
    receipt["status"] = "EVIDENCE_ONLY_WAIT_ADVICE"
    receipt["bundle_receipt_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in receipt.items()
            if key != "bundle_receipt_sha256"
        }
    )
    with pytest.raises(RetrievedLiveAnalysisError) as raised:
        validate_retrieved_live_analysis_receipt(
            receipt,
            expected_bundle_receipt_sha256=str(
                analysis_bundle[-1]["bundle_receipt_sha256"]
            ),
        )
    assert raised.value.code == "BUNDLE_INVALID"


def test_existing_output_rejects_before_capture_replay(
    live_input: tuple[Path, dict[str, object], dict[str, object], dict[str, object]],
    tmp_path: Path,
) -> None:
    capture, authority, live, profile = live_input
    existing = tmp_path / "existing.json"
    existing.write_text("preserve", encoding="utf-8")
    with (
        capture.open("rb", buffering=0) as handle,
        pytest.raises(RetrievedLiveAnalysisError) as raised,
    ):
        write_retrieved_live_analysis_bundle_exclusive(
                handle,
                live,
                profile,
                existing,
                tmp_path / "report.json",
                tmp_path / "report.html",
                tmp_path / "receipt.json",
                expected_live_engineer_session_sha256=str(
                    live["live_engineer_session_sha256"]
                ),
                expected_remote_capture_sha256=str(authority["capture_sha256"]),
                expected_remote_capture_byte_size=int(
                    authority["capture_byte_size"]
                ),
                expected_analysis_profile_sha256=str(
                    profile["analysis_profile_sha256"]
                ),
                stale_after_s=1.0,
        )
    assert raised.value.code == "OUTPUT_CREATE_FAILED"
    assert existing.read_text(encoding="utf-8") == "preserve"
    assert len(list(tmp_path.iterdir())) == 1


def test_wrong_remote_capture_digest_fails_before_any_output(
    live_input: tuple[Path, dict[str, object], dict[str, object], dict[str, object]],
    tmp_path: Path,
) -> None:
    capture, authority, live, profile = live_input
    with (
        capture.open("rb", buffering=0) as handle,
        pytest.raises(RetrievedLiveAnalysisError) as raised,
    ):
        write_retrieved_live_analysis_bundle_exclusive(
                handle,
                live,
                profile,
                tmp_path / "session.json",
                tmp_path / "report.json",
                tmp_path / "report.html",
                tmp_path / "receipt.json",
                expected_live_engineer_session_sha256=str(
                    live["live_engineer_session_sha256"]
                ),
                expected_remote_capture_sha256="0" * 64,
                expected_remote_capture_byte_size=int(
                    authority["capture_byte_size"]
                ),
                expected_analysis_profile_sha256=str(
                    profile["analysis_profile_sha256"]
                ),
                stale_after_s=1.0,
        )
    assert raised.value.code == "ANALYSIS_COMPONENT_FAILED"
    assert not list(tmp_path.iterdir())


def test_verifier_rejects_html_byte_change(
    live_input: tuple[Path, dict[str, object], dict[str, object], dict[str, object]],
    analysis_bundle: tuple[
        Path,
        Path,
        Path,
        Path,
        Path,
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ],
) -> None:
    capture, authority, live, profile = live_input
    _, session_path, report_path, html_path, _, _, _, receipt = analysis_bundle
    changed_html = html_path.read_bytes() + b"\n"
    with (
        capture.open("rb", buffering=0) as handle,
        pytest.raises(RetrievedLiveAnalysisError) as raised,
    ):
        verify_retrieved_live_analysis_bundle(
                handle,
                live,
                profile,
                session_path.read_bytes(),
                report_path.read_bytes(),
                changed_html,
                receipt,
                expected_live_engineer_session_sha256=str(
                    live["live_engineer_session_sha256"]
                ),
                expected_remote_capture_sha256=str(authority["capture_sha256"]),
                expected_remote_capture_byte_size=int(
                    authority["capture_byte_size"]
                ),
                expected_analysis_profile_sha256=str(
                    profile["analysis_profile_sha256"]
                ),
                expected_bundle_receipt_sha256=str(
                    receipt["bundle_receipt_sha256"]
                ),
                stale_after_s=1.0,
        )
    assert raised.value.code == "REPORT_REPLAY_MISMATCH"


def test_verify_cli_reports_object_exact_sdk_live_pass(
    live_input: tuple[Path, dict[str, object], dict[str, object], dict[str, object]],
    analysis_bundle: tuple[
        Path,
        Path,
        Path,
        Path,
        Path,
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture, authority, live, profile = live_input
    _, session_path, report_path, html_path, receipt_path, _, _, receipt = (
        analysis_bundle
    )
    live_path = tmp_path / "live-session.json"
    profile_path = tmp_path / "analysis-profile.json"
    live_path.write_text(
        json.dumps(live, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    profile_path.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result = cli_main(
        [
            "verify-live-analysis",
            str(capture),
            "--live-session",
            str(live_path),
            "--analysis-profile",
            str(profile_path),
            "--engineer-session",
            str(session_path),
            "--report-artifact",
            str(report_path),
            "--report-html",
            str(html_path),
            "--bundle-receipt",
            str(receipt_path),
            "--expected-live-engineer-session-sha256",
            str(live["live_engineer_session_sha256"]),
            "--expected-remote-capture-sha256",
            str(authority["capture_sha256"]),
            "--expected-remote-capture-byte-size",
            str(authority["capture_byte_size"]),
            "--expected-analysis-profile-sha256",
            str(profile["analysis_profile_sha256"]),
            "--expected-bundle-receipt-sha256",
            str(receipt["bundle_receipt_sha256"]),
            "--stale-after-seconds",
            "1.0",
        ]
    )
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verification"] == "PASS_OBJECT_EXACT_REPLAY"
    assert payload["source_kind"] == "SDK_LIVE"
    assert payload["vehicle_control_enabled"] is False


def test_finalize_cli_surfaces_all_four_output_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture = tmp_path / "capture.jsonl"
    live_path = tmp_path / "live.json"
    profile_path = tmp_path / "profile.json"
    calibration_path = tmp_path / "calibration.json"
    tire_performance_path = tmp_path / "tire-performance.json"
    capture.write_bytes(b"x")
    live_path.write_text("{}\n", encoding="utf-8")
    profile_path.write_text("{}\n", encoding="utf-8")
    calibration_path.write_text("{}\n", encoding="utf-8")
    tire_performance_path.write_text("{}\n", encoding="utf-8")
    receipt = {
        "bundle_receipt_sha256": "1" * 64,
        "engineer_session_binding": {"session_sha256": "2" * 64},
        "readiness": {
            "driving_practice_available": False,
            "strategy_advice_available": False,
        },
        "report_binding": {"report_sha256": "3" * 64},
        "source_binding": {"source_kind": "SDK_LIVE"},
        "status": "WAIT_ADVICE",
    }

    def fake_write(*args: object, **kwargs: object) -> dict[str, object]:
        assert kwargs["expected_remote_capture_sha256"] == "4" * 64
        assert kwargs["calibration_model"] == {}
        assert kwargs["expected_calibration_model_sha256"] == "8" * 64
        assert kwargs["expected_calibration_source_receipt_sha256"] == "9" * 64
        assert kwargs["tire_performance_model"] == {}
        assert kwargs["expected_tire_performance_model_sha256"] == "a" * 64
        assert (
            kwargs["expected_tire_performance_source_receipt_sha256"]
            == "b" * 64
        )
        return receipt

    monkeypatch.setattr(
        analysis_module, "write_retrieved_live_analysis_bundle_exclusive", fake_write
    )
    monkeypatch.setattr(
        cli_module, "_regular_file_sha256", lambda *_args: ("5" * 64, 123)
    )
    result = cli_main(
        [
            "finalize-live-analysis",
            str(capture),
            "--live-session",
            str(live_path),
            "--analysis-profile",
            str(profile_path),
            "--expected-live-engineer-session-sha256",
            "6" * 64,
            "--expected-remote-capture-sha256",
            "4" * 64,
            "--expected-remote-capture-byte-size",
            "1",
            "--expected-analysis-profile-sha256",
            "7" * 64,
            "--calibration-model",
            str(calibration_path),
            "--expected-calibration-model-sha256",
            "8" * 64,
            "--expected-calibration-source-receipt-sha256",
            "9" * 64,
            "--tire-performance-model",
            str(tire_performance_path),
            "--expected-tire-performance-model-sha256",
            "a" * 64,
            "--expected-tire-performance-source-receipt-sha256",
            "b" * 64,
            "--session-output",
            str(tmp_path / "session.json"),
            "--artifact-output",
            str(tmp_path / "report.json"),
            "--html-output",
            str(tmp_path / "report.html"),
            "--receipt-output",
            str(tmp_path / "receipt.json"),
        ]
    )
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["bundle_receipt_sha256"] == "1" * 64
    assert payload["engineer_session_file_sha256"] == "5" * 64
    assert payload["receipt_byte_size"] == 123
    assert payload["source_kind"] == "SDK_LIVE"


def test_finalize_cli_rejects_partial_tire_performance_pin_set(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture = tmp_path / "capture.jsonl"
    live_path = tmp_path / "live.json"
    profile_path = tmp_path / "profile.json"
    tire_path = tmp_path / "tire.json"
    capture.write_bytes(b"x")
    live_path.write_text("{}\n", encoding="utf-8")
    profile_path.write_text("{}\n", encoding="utf-8")
    tire_path.write_text("{}\n", encoding="utf-8")

    result = cli_main(
        [
            "finalize-live-analysis",
            str(capture),
            "--live-session",
            str(live_path),
            "--analysis-profile",
            str(profile_path),
            "--expected-live-engineer-session-sha256",
            "1" * 64,
            "--expected-remote-capture-sha256",
            "2" * 64,
            "--expected-remote-capture-byte-size",
            "1",
            "--expected-analysis-profile-sha256",
            "3" * 64,
            "--tire-performance-model",
            str(tire_path),
            "--session-output",
            str(tmp_path / "session.json"),
            "--artifact-output",
            str(tmp_path / "report.json"),
            "--html-output",
            str(tmp_path / "report.html"),
            "--receipt-output",
            str(tmp_path / "receipt.json"),
        ]
    )

    assert result == 3
    assert "must be supplied together" in capsys.readouterr().err

"""Replay-first foundations for the iRacing AI engineer.

Public offline helpers are loaded lazily so the lightweight Windows SDK probe
does not import the analytical stack before it has connected to iRacing.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "ApprovedTrackStateLabelSet",
    "CONDITION_COHORT_CONTRACT_VERSION",
    "ConditionCohortConfig",
    "ConditionCohortError",
    "EVENT_IDENTITY_CONTEXT_CONTRACT_VERSION",
    "HUMAN_TRACK_STATE_ATTESTATION",
    "IbtMetadata",
    "IbtReader",
    "FuelScenario",
    "FuelModelReplayError",
    "M0AcceptanceError",
    "MATCHED_PIT_CALIBRATION_DATASET_CONTRACT_VERSION",
    "MATCHED_PIT_CALIBRATION_METHOD_VERSION",
    "MATCHED_TIRE_PERFORMANCE_DATASET_CONTRACT_VERSION",
    "TIRE_PERFORMANCE_BELIEF_CONTRACT_VERSION",
    "TIRE_PERFORMANCE_BELIEF_METHOD_VERSION",
    "TIRE_PERFORMANCE_METHOD_VERSION",
    "TIRE_PERFORMANCE_MODEL_CONTRACT_VERSION",
    "TIRE_STINT_CONTEXT_CONTRACT_VERSION",
    "TIME_DOMAIN_REJOIN_ESTIMATE_CONTRACT_VERSION",
    "TIME_DOMAIN_REJOIN_METHOD_VERSION",
    "TRAFFIC_MOTION_CONTEXT_CONTRACT_VERSION",
    "OFFLINE_ENGINEER_DEMO_CONTRACT_VERSION",
    "OfflineEngineerDemoError",
    "PitCalibrationError",
    "TirePerformanceError",
    "FuelStrategyResult",
    "ENGINEER_SESSION_REPORT_CONTRACT_VERSION",
    "EngineerSessionReportError",
    "DrivingAnalysis",
    "DrivingLabelsError",
    "DrivingModelReplayError",
    "QualityReport",
    "ReplayReceipt",
    "RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION",
    "RETRIEVED_LIVE_ANALYSIS_PROFILE_CONTRACT_VERSION",
    "RetrievedLiveAnalysisError",
    "VariableInfo",
    "CollectorReceipt",
    "CollectorInputEvidence",
    "EventIdentityAvailability",
    "EventIdentityContextEvidence",
    "EventIdentityProvenance",
    "EventIdentityStatus",
    "TRAFFIC_OBSERVATION_CONTEXT_CONTRACT_VERSION",
    "TrafficNeighborEvidence",
    "TrafficObservationAvailability",
    "TrafficObservationContextEvidence",
    "TrafficObservationProvenance",
    "TrafficObservationStatus",
    "Presence",
    "QualityStatus",
    "SourceKind",
    "TelemetryEvent",
    "TelemetryEventPipeline",
    "TelemetryEventReceipt",
    "TelemetryField",
    "TelemetrySample",
    "ValidatedCollectorRun",
    "analyze_ibt",
    "accept_m0",
    "analyze_driving",
    "build_shadow_report",
    "build_retrieved_live_analysis_profile",
    "build_engineer_session_report",
    "build_condition_cohort",
    "build_fuel_model_replay",
    "build_offline_engineer_demo",
    "build_matched_pit_calibration_model",
    "build_tire_performance_belief",
    "build_tire_performance_model",
    "build_time_domain_rejoin_estimate",
    "build_driving_model_replay",
    "build_driving_label_candidate",
    "collect_transport_to_jsonl",
    "estimate_fuel_strategy",
    "iter_collector_jsonl_samples",
    "iter_ibt_samples",
    "normalize_sdk_frame",
    "open_collector_jsonl",
    "process_telemetry_events",
    "render_engineer_session_report_html",
    "regress_driving_labels",
    "replay_ibt",
    "validate_performance_receipt",
    "validate_matched_pit_calibration_dataset",
    "validate_matched_pit_calibration_model",
    "validate_matched_tire_performance_dataset",
    "validate_tire_performance_belief",
    "validate_tire_performance_model",
    "validate_tire_stint_context",
    "validate_time_domain_rejoin_estimate",
    "validate_traffic_motion_context",
    "validate_driving_labels",
    "validate_engineer_session_report",
    "validate_retrieved_live_analysis_profile",
    "validate_retrieved_live_analysis_receipt",
    "verify_retrieved_live_analysis_bundle",
    "write_engineer_session_report_bundle_exclusive",
    "write_matched_pit_calibration_model_exclusive",
    "write_tire_performance_model_exclusive",
    "write_retrieved_live_analysis_bundle_exclusive",
    "write_retrieved_live_analysis_profile_exclusive",
    "get_validated_event_identity_context",
    "get_validated_traffic_observation_context",
]

_EXPORTS = {
    "ApprovedTrackStateLabelSet": (
        ".condition_cohort",
        "ApprovedTrackStateLabelSet",
    ),
    "CONDITION_COHORT_CONTRACT_VERSION": (
        ".condition_cohort",
        "CONDITION_COHORT_CONTRACT_VERSION",
    ),
    "ConditionCohortConfig": (".condition_cohort", "ConditionCohortConfig"),
    "ConditionCohortError": (".condition_cohort", "ConditionCohortError"),
    "HUMAN_TRACK_STATE_ATTESTATION": (
        ".condition_cohort",
        "HUMAN_TRACK_STATE_ATTESTATION",
    ),
    "build_condition_cohort": (".condition_cohort", "build_condition_cohort"),
    "IbtMetadata": (".ibt", "IbtMetadata"),
    "IbtReader": (".ibt", "IbtReader"),
    "VariableInfo": (".ibt", "VariableInfo"),
    "FuelStrategyResult": (".fuel", "FuelStrategyResult"),
    "estimate_fuel_strategy": (".fuel", "estimate_fuel_strategy"),
    "DrivingAnalysis": (".driving", "DrivingAnalysis"),
    "analyze_driving": (".driving", "analyze_driving"),
    "DrivingModelReplayError": (
        ".driving_model_replay",
        "DrivingModelReplayError",
    ),
    "build_driving_model_replay": (
        ".driving_model_replay",
        "build_driving_model_replay",
    ),
    "DrivingLabelsError": (".driving_labels", "DrivingLabelsError"),
    "build_driving_label_candidate": (
        ".driving_labels",
        "build_driving_label_candidate",
    ),
    "regress_driving_labels": (".driving_labels", "regress_driving_labels"),
    "validate_driving_labels": (
        ".driving_labels",
        "validate_driving_labels",
    ),
    "QualityReport": (".quality", "QualityReport"),
    "analyze_ibt": (".quality", "analyze_ibt"),
    "ReplayReceipt": (".replay", "ReplayReceipt"),
    "replay_ibt": (".replay", "replay_ibt"),
    "FuelScenario": (".fuel", "FuelScenario"),
    "build_shadow_report": (".shadow", "build_shadow_report"),
    "ENGINEER_SESSION_REPORT_CONTRACT_VERSION": (
        ".session_report",
        "ENGINEER_SESSION_REPORT_CONTRACT_VERSION",
    ),
    "EngineerSessionReportError": (
        ".session_report",
        "EngineerSessionReportError",
    ),
    "build_engineer_session_report": (
        ".session_report",
        "build_engineer_session_report",
    ),
    "render_engineer_session_report_html": (
        ".session_report",
        "render_engineer_session_report_html",
    ),
    "validate_engineer_session_report": (
        ".session_report",
        "validate_engineer_session_report",
    ),
    "write_engineer_session_report_bundle_exclusive": (
        ".session_report",
        "write_engineer_session_report_bundle_exclusive",
    ),
    "RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION": (
        ".retrieved_live_analysis",
        "RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION",
    ),
    "RETRIEVED_LIVE_ANALYSIS_PROFILE_CONTRACT_VERSION": (
        ".retrieved_live_analysis",
        "RETRIEVED_LIVE_ANALYSIS_PROFILE_CONTRACT_VERSION",
    ),
    "RetrievedLiveAnalysisError": (
        ".retrieved_live_analysis",
        "RetrievedLiveAnalysisError",
    ),
    "build_retrieved_live_analysis_profile": (
        ".retrieved_live_analysis",
        "build_retrieved_live_analysis_profile",
    ),
    "validate_retrieved_live_analysis_profile": (
        ".retrieved_live_analysis",
        "validate_retrieved_live_analysis_profile",
    ),
    "validate_retrieved_live_analysis_receipt": (
        ".retrieved_live_analysis",
        "validate_retrieved_live_analysis_receipt",
    ),
    "verify_retrieved_live_analysis_bundle": (
        ".retrieved_live_analysis",
        "verify_retrieved_live_analysis_bundle",
    ),
    "write_retrieved_live_analysis_bundle_exclusive": (
        ".retrieved_live_analysis",
        "write_retrieved_live_analysis_bundle_exclusive",
    ),
    "write_retrieved_live_analysis_profile_exclusive": (
        ".retrieved_live_analysis",
        "write_retrieved_live_analysis_profile_exclusive",
    ),
    "FuelModelReplayError": (".model_replay", "FuelModelReplayError"),
    "build_fuel_model_replay": (".model_replay", "build_fuel_model_replay"),
    "M0AcceptanceError": (".m0", "M0AcceptanceError"),
    "accept_m0": (".m0", "accept_m0"),
    "validate_performance_receipt": (".m0", "validate_performance_receipt"),
    "MATCHED_PIT_CALIBRATION_DATASET_CONTRACT_VERSION": (
        ".retrieved_live_analysis",
        "MATCHED_PIT_CALIBRATION_DATASET_CONTRACT_VERSION",
    ),
    "MATCHED_PIT_CALIBRATION_METHOD_VERSION": (
        ".retrieved_live_analysis",
        "MATCHED_PIT_CALIBRATION_METHOD_VERSION",
    ),
    "MATCHED_TIRE_PERFORMANCE_DATASET_CONTRACT_VERSION": (
        ".retrieved_live_analysis",
        "MATCHED_TIRE_PERFORMANCE_DATASET_CONTRACT_VERSION",
    ),
    "TIRE_PERFORMANCE_BELIEF_CONTRACT_VERSION": (
        ".retrieved_live_analysis",
        "TIRE_PERFORMANCE_BELIEF_CONTRACT_VERSION",
    ),
    "TIRE_PERFORMANCE_BELIEF_METHOD_VERSION": (
        ".retrieved_live_analysis",
        "TIRE_PERFORMANCE_BELIEF_METHOD_VERSION",
    ),
    "TIRE_PERFORMANCE_METHOD_VERSION": (
        ".retrieved_live_analysis",
        "TIRE_PERFORMANCE_METHOD_VERSION",
    ),
    "TIRE_PERFORMANCE_MODEL_CONTRACT_VERSION": (
        ".retrieved_live_analysis",
        "TIRE_PERFORMANCE_MODEL_CONTRACT_VERSION",
    ),
    "TIRE_STINT_CONTEXT_CONTRACT_VERSION": (
        ".retrieved_live_analysis",
        "TIRE_STINT_CONTEXT_CONTRACT_VERSION",
    ),
    "TIME_DOMAIN_REJOIN_ESTIMATE_CONTRACT_VERSION": (
        ".retrieved_live_analysis",
        "TIME_DOMAIN_REJOIN_ESTIMATE_CONTRACT_VERSION",
    ),
    "TIME_DOMAIN_REJOIN_METHOD_VERSION": (
        ".retrieved_live_analysis",
        "TIME_DOMAIN_REJOIN_METHOD_VERSION",
    ),
    "TRAFFIC_MOTION_CONTEXT_CONTRACT_VERSION": (
        ".retrieved_live_analysis",
        "TRAFFIC_MOTION_CONTEXT_CONTRACT_VERSION",
    ),
    "PitCalibrationError": (".retrieved_live_analysis", "PitCalibrationError"),
    "TirePerformanceError": (
        ".retrieved_live_analysis",
        "TirePerformanceError",
    ),
    "build_matched_pit_calibration_model": (
        ".retrieved_live_analysis",
        "build_matched_pit_calibration_model",
    ),
    "build_tire_performance_belief": (
        ".retrieved_live_analysis",
        "build_tire_performance_belief",
    ),
    "build_tire_performance_model": (
        ".retrieved_live_analysis",
        "build_tire_performance_model",
    ),
    "build_time_domain_rejoin_estimate": (
        ".retrieved_live_analysis",
        "build_time_domain_rejoin_estimate",
    ),
    "validate_matched_pit_calibration_dataset": (
        ".retrieved_live_analysis",
        "validate_matched_pit_calibration_dataset",
    ),
    "validate_matched_pit_calibration_model": (
        ".retrieved_live_analysis",
        "validate_matched_pit_calibration_model",
    ),
    "validate_matched_tire_performance_dataset": (
        ".retrieved_live_analysis",
        "validate_matched_tire_performance_dataset",
    ),
    "validate_tire_performance_belief": (
        ".retrieved_live_analysis",
        "validate_tire_performance_belief",
    ),
    "validate_tire_performance_model": (
        ".retrieved_live_analysis",
        "validate_tire_performance_model",
    ),
    "validate_tire_stint_context": (
        ".retrieved_live_analysis",
        "validate_tire_stint_context",
    ),
    "validate_time_domain_rejoin_estimate": (
        ".retrieved_live_analysis",
        "validate_time_domain_rejoin_estimate",
    ),
    "validate_traffic_motion_context": (
        ".retrieved_live_analysis",
        "validate_traffic_motion_context",
    ),
    "write_matched_pit_calibration_model_exclusive": (
        ".retrieved_live_analysis",
        "write_matched_pit_calibration_model_exclusive",
    ),
    "write_tire_performance_model_exclusive": (
        ".retrieved_live_analysis",
        "write_tire_performance_model_exclusive",
    ),
    "OFFLINE_ENGINEER_DEMO_CONTRACT_VERSION": (
        ".offline_demo",
        "OFFLINE_ENGINEER_DEMO_CONTRACT_VERSION",
    ),
    "OfflineEngineerDemoError": (
        ".offline_demo",
        "OfflineEngineerDemoError",
    ),
    "build_offline_engineer_demo": (
        ".offline_demo",
        "build_offline_engineer_demo",
    ),
    "CollectorReceipt": (".collector", "CollectorReceipt"),
    "CollectorInputEvidence": (".adapters", "CollectorInputEvidence"),
    "EVENT_IDENTITY_CONTEXT_CONTRACT_VERSION": (
        ".adapters",
        "EVENT_IDENTITY_CONTEXT_CONTRACT_VERSION",
    ),
    "EventIdentityAvailability": (
        ".adapters",
        "EventIdentityAvailability",
    ),
    "EventIdentityContextEvidence": (
        ".adapters",
        "EventIdentityContextEvidence",
    ),
    "EventIdentityProvenance": (
        ".adapters",
        "EventIdentityProvenance",
    ),
    "EventIdentityStatus": (".adapters", "EventIdentityStatus"),
    "TRAFFIC_OBSERVATION_CONTEXT_CONTRACT_VERSION": (
        ".adapters",
        "TRAFFIC_OBSERVATION_CONTEXT_CONTRACT_VERSION",
    ),
    "TrafficNeighborEvidence": (".adapters", "TrafficNeighborEvidence"),
    "TrafficObservationAvailability": (
        ".adapters",
        "TrafficObservationAvailability",
    ),
    "TrafficObservationContextEvidence": (
        ".adapters",
        "TrafficObservationContextEvidence",
    ),
    "TrafficObservationProvenance": (
        ".adapters",
        "TrafficObservationProvenance",
    ),
    "TrafficObservationStatus": (
        ".adapters",
        "TrafficObservationStatus",
    ),
    "collect_transport_to_jsonl": (".collector", "collect_transport_to_jsonl"),
    "iter_collector_jsonl_samples": (".adapters", "iter_collector_jsonl_samples"),
    "iter_ibt_samples": (".adapters", "iter_ibt_samples"),
    "open_collector_jsonl": (".adapters", "open_collector_jsonl"),
    "get_validated_event_identity_context": (
        ".adapters",
        "get_validated_event_identity_context",
    ),
    "get_validated_traffic_observation_context": (
        ".adapters",
        "get_validated_traffic_observation_context",
    ),
    "Presence": (".telemetry", "Presence"),
    "QualityStatus": (".telemetry", "QualityStatus"),
    "SourceKind": (".telemetry", "SourceKind"),
    "TelemetryField": (".telemetry", "TelemetryField"),
    "TelemetrySample": (".telemetry", "TelemetrySample"),
    "ValidatedCollectorRun": (".adapters", "ValidatedCollectorRun"),
    "normalize_sdk_frame": (".telemetry", "normalize_sdk_frame"),
    "TelemetryEvent": (".events", "TelemetryEvent"),
    "TelemetryEventPipeline": (".events", "TelemetryEventPipeline"),
    "TelemetryEventReceipt": (".events", "TelemetryEventReceipt"),
    "process_telemetry_events": (".events", "process_telemetry_events"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value

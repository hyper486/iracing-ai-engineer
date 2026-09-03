from __future__ import annotations

from iracing_ai_engineer.laps import detect_boundaries, segment_laps


def test_two_normal_wraps_produce_one_complete_lap(two_wrap_channels):
    observations = segment_laps(two_wrap_channels, 60)
    complete = [item for item in observations if item.structurally_complete]

    assert len(complete) == 1
    assert complete[0].clean_for_driving
    assert complete[0].fuel_eligible
    assert 0.98 <= complete[0].distance_coverage_laps <= 1.02


def test_partial_prefix_and_suffix_are_not_complete(two_wrap_channels):
    observations = segment_laps(two_wrap_channels, 60)

    assert not observations[0].structurally_complete
    assert not observations[-1].structurally_complete


def test_pit_lane_frame_keeps_structure_but_disables_analysis(two_wrap_channels):
    two_wrap_channels["OnPitRoad"][20] = True
    observations = segment_laps(two_wrap_channels, 60)
    complete = next(item for item in observations if item.structurally_complete)

    assert "PIT_LAP" in complete.tags
    assert not complete.clean_for_driving
    assert not complete.fuel_eligible


def test_incident_increase_invalidates_driving_only(two_wrap_channels):
    two_wrap_channels["PlayerCarMyIncidentCount"][30:] = 1
    observations = segment_laps(two_wrap_channels, 60)
    complete = next(item for item in observations if item.structurally_complete)

    assert "INCIDENT_COUNT_INCREASED" in complete.invalid_reasons
    assert not complete.clean_for_driving
    assert complete.fuel_eligible


def test_incident_counter_regression_fails_closed(two_wrap_channels):
    two_wrap_channels["PlayerCarMyIncidentCount"][20:40] = 2
    two_wrap_channels["PlayerCarMyIncidentCount"][40:] = 1
    complete = next(
        item for item in segment_laps(two_wrap_channels, 60) if item.structurally_complete
    )

    assert "INCIDENT_COUNT_REGRESSION" in complete.invalid_reasons
    assert not complete.clean_for_driving


def test_incident_counter_falls_back_to_driver_when_my_is_absent(two_wrap_channels):
    driver = two_wrap_channels.pop("PlayerCarMyIncidentCount")
    two_wrap_channels["PlayerCarDriverIncidentCount"] = driver
    complete = next(
        item for item in segment_laps(two_wrap_channels, 60) if item.structurally_complete
    )

    assert complete.cleanliness_observable
    assert complete.clean_for_driving
    assert complete.incident_delta == 0


def test_any_observed_incident_counter_increase_invalidates_lap(two_wrap_channels):
    two_wrap_channels["PlayerCarDriverIncidentCount"] = (
        two_wrap_channels["PlayerCarMyIncidentCount"].copy()
    )
    two_wrap_channels["PlayerCarDriverIncidentCount"][30:] = 1
    complete = next(
        item for item in segment_laps(two_wrap_channels, 60) if item.structurally_complete
    )

    assert complete.incident_delta == 0
    assert "INCIDENT_COUNT_INCREASED" in complete.invalid_reasons
    assert not complete.clean_for_driving


def test_tick_gap_is_counted_and_invalidates_driving(two_wrap_channels):
    two_wrap_channels["SessionTime"][30:] += 0.2
    two_wrap_channels["SessionTick"][30:] += 12
    observations = segment_laps(two_wrap_channels, 60)
    complete = next(item for item in observations if item.structurally_complete)

    assert complete.max_gap_s > 0.1
    assert complete.tick_coverage < 0.999
    assert not complete.clean_for_driving


def test_counter_jump_creates_reset_not_complete_lap(two_wrap_channels):
    two_wrap_channels["Lap"][30:] += 2
    boundaries = detect_boundaries(two_wrap_channels, 60)

    assert any(boundary.kind == "source_reset" for boundary in boundaries)
    assert any("LAP_COUNTER_RESET_OR_JUMP" in boundary.tags for boundary in boundaries)


def test_counter_increment_one_frame_after_wrap_is_corroborating(two_wrap_channels):
    two_wrap_channels["Lap"][2] = 1
    two_wrap_channels["LapCompleted"][2] = 0
    boundaries = detect_boundaries(two_wrap_channels, 60)
    first_wrap = next(boundary for boundary in boundaries if boundary.frame_index == 2)

    assert first_wrap.kind == "strong_wrap"
    assert first_wrap.confidence == "high"
    assert "COUNTER_DISTANCE_MISMATCH" not in first_wrap.tags
    assert not any(boundary.kind == "source_reset" for boundary in boundaries)


def test_wrap_without_counter_evidence_is_not_structurally_complete(two_wrap_channels):
    two_wrap_channels["Lap"][:] = 1
    two_wrap_channels["LapCompleted"][:] = 0
    observations = segment_laps(two_wrap_channels, 60)

    assert not any(item.structurally_complete for item in observations)


def test_forward_teleport_creates_source_reset(two_wrap_channels):
    two_wrap_channels["LapDistPct"][30] += 0.1
    boundaries = detect_boundaries(two_wrap_channels, 60)

    assert any(
        boundary.kind == "source_reset" and "TELEPORT_OR_DISTANCE_RESET" in boundary.tags
        for boundary in boundaries
    )


def test_duplicate_session_time_does_not_create_reset(two_wrap_channels):
    two_wrap_channels["SessionTime"][30] = two_wrap_channels["SessionTime"][29]
    boundaries = detect_boundaries(two_wrap_channels, 60)
    complete = next(
        item for item in segment_laps(two_wrap_channels, 60) if item.structurally_complete
    )

    assert not any("TIME_RESET" in boundary.tags for boundary in boundaries)
    assert complete.duplicate_time_steps == 1
    assert complete.quality_complete
    assert not complete.clean_for_driving
    assert "TIME_DUPLICATE" in complete.invalid_reasons


def test_stationary_counter_change_near_line_is_reset(two_wrap_channels):
    two_wrap_channels["LapDistPct"][:] = 0.01
    two_wrap_channels["Lap"][:] = 1
    two_wrap_channels["LapCompleted"][:] = 0
    two_wrap_channels["Lap"][30:] = 2
    two_wrap_channels["LapCompleted"][30:] = 1
    two_wrap_channels["Speed"][29:31] = 0.0
    two_wrap_channels["PlayerCarInPitStall"][29:31] = True
    boundaries = detect_boundaries(two_wrap_channels, 60)

    assert any(
        boundary.kind == "source_reset" and "STATIONARY_COUNTER_CHANGE" in boundary.tags
        for boundary in boundaries
    )
    assert not any(boundary.kind == "counter_only" for boundary in boundaries)


def test_missing_pit_evidence_disables_fuel_eligibility(two_wrap_channels):
    del two_wrap_channels["OnPitRoad"]
    del two_wrap_channels["PlayerCarInPitStall"]
    complete = next(
        item for item in segment_laps(two_wrap_channels, 60) if item.structurally_complete
    )

    assert not complete.fuel_eligible


def test_missing_surface_and_incident_evidence_disables_clean_label(two_wrap_channels):
    del two_wrap_channels["PlayerTrackSurface"]
    del two_wrap_channels["PlayerCarMyIncidentCount"]
    complete = next(
        item for item in segment_laps(two_wrap_channels, 60) if item.structurally_complete
    )

    assert not complete.cleanliness_observable
    assert not complete.clean_for_driving
    assert "CLEANLINESS_UNOBSERVABLE" in complete.invalid_reasons


def test_ranges_are_ordered_and_non_overlapping(two_wrap_channels):
    observations = segment_laps(two_wrap_channels, 60)

    assert all(item.start_frame < item.end_frame_exclusive for item in observations)
    assert all(
        left.end_frame_exclusive <= right.start_frame
        for left, right in zip(observations[:-1], observations[1:], strict=True)
    )

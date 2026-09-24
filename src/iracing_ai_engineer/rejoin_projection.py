"""Pure interval projection for physical traffic around a circular track."""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping
from itertools import product

REJOIN_CONTRACT_VERSION = "time-domain-rejoin-estimate-v2"
REJOIN_METHOD_VERSION = "physical-progress-envelope-v2"


def _nearest(
    candidates: list[dict[str, object]],
    *, key: str = "gap_range_s",
) -> tuple[dict[str, object] | None, bool]:
    if not candidates:
        return None, True
    ordered = sorted(candidates, key=lambda row: (sum(row[key]), row["car_idx"]))
    winner = ordered[0]
    if any(winner[key][1] >= row[key][0] for row in ordered[1:]):
        return None, False
    return winner, True


def project_physical_rejoin(
    motion: Mapping[str, object],
    *,
    loss_range_s: tuple[float, float],
    recommended_lap_from_now: int,
) -> tuple[dict[str, object] | None, dict[str, object] | None, list[str]]:
    """Project the constant-rate envelope to a stop and return circular neighbors.

    A pit loss is represented as equivalent additional elapsed time after the
    player's counterfactual travel to the stop. For each observed rate bound,
    relative progress is ``delta + (opponent/player - 1)*laps + opponent*loss``.
    The input and output distances are reduced modulo one lap; race-order lap
    deficits never turn a physically adjacent car into a distant neighbor.
    An interval crossing any integer lap is an uncertain physical overlap.
    Rates are an empirical scenario envelope, not a calibrated forecast.
    """

    if type(recommended_lap_from_now) is not int or recommended_lap_from_now < 0:
        raise ValueError("recommended_lap_from_now must be a non-negative integer")
    ahead: list[dict[str, object]] = []
    behind: list[dict[str, object]] = []
    reasons: list[str] = []
    player_rates = motion["player"]["rate_range_laps_per_s"]
    for opponent in motion["opponents"]:
        # Round after removing race-order laps so adding an integer lap to
        # either actor cannot change the physical estimate through float noise.
        delta = round(float(opponent["current_signed_lap_delta"]) % 1.0, 9)
        opponent_rates = opponent["rate_range_laps_per_s"]
        projections = [
            delta + (opponent_rate / player_rate - 1.0) * recommended_lap_from_now
            + opponent_rate * loss
            for player_rate, opponent_rate, loss in product(
                player_rates, opponent_rates, loss_range_s
            )
        ]
        low, high = min(projections), max(projections)
        if math.ceil(low - 1e-12) <= math.floor(high + 1e-12):
            reasons.append("REJOIN_ZERO_CROSSING_WITHIN_UNCERTAINTY")
            continue
        forward_low, forward_high = low - math.floor(low), high - math.floor(high)
        # Every car has a forward and backward distance on the circular track.
        # A lone opponent can correctly be the nearest car in both directions.
        ahead.append({
            "car_idx": int(opponent["car_idx"]),
            "gap_range_s": [
                round(forward_low / max(player_rates), 6),
                round(forward_high / min(player_rates), 6),
            ],
        })
        behind.append({
            "car_idx": int(opponent["car_idx"]),
            "gap_range_s": [
                round((1.0 - forward_high) / max(opponent_rates), 6),
                round((1.0 - forward_low) / min(opponent_rates), 6),
            ],
        })
    nearest_ahead, ahead_stable = _nearest(ahead)
    nearest_behind, behind_stable = _nearest(behind)
    if not ahead_stable:
        reasons.append("REJOIN_AHEAD_ORDER_AMBIGUOUS")
    if not behind_stable:
        reasons.append("REJOIN_BEHIND_ORDER_AMBIGUOUS")
    if nearest_ahead is None and nearest_behind is None and not reasons:
        reasons.append("NO_REJOIN_NEIGHBOR_AVAILABLE")
    if reasons:
        return None, None, list(dict.fromkeys(reasons))
    return nearest_ahead, nearest_behind, []


def phase_time(profile, progress):
    """Counterfactual elapsed microseconds at an unwrapped track position."""
    times = profile["elapsed_us"]
    bins = len(times) - 1
    lap = math.floor(progress)
    phase = (progress - lap) * bins
    index = min(bins - 1, math.floor(phase))
    return lap * times[-1] + times[index] + (phase - index) * (times[index + 1] - times[index])


def phase_position(profile, elapsed_us):
    """Inverse of phase_time for a strictly increasing completed-lap profile."""
    times = profile["elapsed_us"]
    lap, phase = divmod(elapsed_us, times[-1])
    index = min(len(times) - 2, bisect_right(times, phase) - 1)
    fraction = (phase - times[index]) / (times[index + 1] - times[index])
    return lap + (index + fraction) / (len(times) - 1)


def project_phase_rejoin(motion, *, exit_progress_laps, loss_range_s):
    """Project to a mapped exit using both actors' two observed lap shapes.

    Full net pit loss is relative to counterfactual ordinary travel to that exit.
    We enumerate both completed profiles, service-loss endpoints and bounded
    sampling errors. This envelope is conditional on repeated historical pace;
    it is not a statistical prediction interval or a guarantee about future pits.
    Inputs are admitted by the live motion/scenario validators, not free text.
    """
    player = motion["player"]
    distance = exit_progress_laps - player["progress_laps"]
    if not 0 <= distance <= 3:
        raise ValueError("EXIT_DISTANCE_OUT_OF_RANGE")
    ahead, behind, reasons = [], [], []
    for opponent in motion["opponents"]:
        deltas, forward_gaps, backward_gaps = [], [], []
        for ours, theirs in product(player["lap_profiles"], opponent["lap_profiles"]):
            travel = (phase_time(ours, exit_progress_laps)
                      - phase_time(ours, player["progress_laps"])) / 1e6
            own_error = 2 * ours["sampling_error_us"] / 1e6 * (math.ceil(distance) + 1)
            for loss, sign in product(loss_range_s, (-1, 1)):
                elapsed = max(0, travel + loss + sign * own_error)
                other_laps = math.ceil(elapsed * 1e6 / theirs["elapsed_us"][-1])
                other_error = 2 * theirs["sampling_error_us"] / 1e6 * (other_laps + 1)
                for other_sign in (-1, 1):
                    future = phase_position(theirs, phase_time(theirs, opponent["progress_laps"])
                                            + max(0, elapsed + other_sign * other_error) * 1e6)
                    delta = round(future - exit_progress_laps, 9)
                    deltas.append(delta)
                    forward = delta % 1
                    a = (phase_time(ours, exit_progress_laps + forward)
                         - phase_time(ours, exit_progress_laps)) / 1e6
                    b = (phase_time(theirs, future + 1 - forward)
                         - phase_time(theirs, future)) / 1e6
                    forward_gaps.extend((max(0, a - 4 * ours["sampling_error_us"] / 1e6),
                                         a + 4 * ours["sampling_error_us"] / 1e6))
                    backward_gaps.extend((max(0, b - 4 * theirs["sampling_error_us"] / 1e6),
                                          b + 4 * theirs["sampling_error_us"] / 1e6))
        low, high = min(deltas), max(deltas)
        if math.ceil(low - 1e-12) <= math.floor(high + 1e-12):
            reasons.append("REJOIN_ZERO_CROSSING_WITHIN_UNCERTAINTY")
            continue
        forward_range = [low % 1, high % 1]
        backward_range = [1 - forward_range[1], 1 - forward_range[0]]
        for target, gaps, distances in ((ahead, forward_gaps, forward_range),
                                         (behind, backward_gaps, backward_range)):
            target.append({"car_idx": opponent["car_idx"],
                           "distance_range_laps": distances,
                           "gap_range_s": [round(min(gaps), 6), round(max(gaps), 6)]})
    # Physical ordering is by circular track distance, not relative pace. A
    # farther but faster car is not the nearest car behind in a multiclass race.
    nearest_ahead, a_stable = _nearest(ahead, key="distance_range_laps")
    nearest_behind, b_stable = _nearest(behind, key="distance_range_laps")
    if not a_stable or not b_stable:
        reasons.append("REJOIN_ORDER_AMBIGUOUS")
    if not ahead or not behind:
        reasons.append("NO_REJOIN_NEIGHBOR_AVAILABLE")
    if reasons:
        return None, None, sorted(set(reasons))
    return ({key: value for key, value in nearest_ahead.items() if key != "distance_range_laps"},
            {key: value for key, value in nearest_behind.items() if key != "distance_range_laps"},
            [])

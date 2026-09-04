"""Pure interval projection for physical traffic around a circular track."""

from __future__ import annotations

import math
from collections.abc import Mapping
from itertools import product

REJOIN_CONTRACT_VERSION = "time-domain-rejoin-estimate-v2"
REJOIN_METHOD_VERSION = "physical-progress-envelope-v2"


def _nearest(
    candidates: list[dict[str, object]],
) -> tuple[dict[str, object] | None, bool]:
    if not candidates:
        return None, True
    ordered = sorted(candidates, key=lambda row: (sum(row["gap_range_s"]), row["car_idx"]))
    winner = ordered[0]
    if any(winner["gap_range_s"][1] >= row["gap_range_s"][0] for row in ordered[1:]):
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

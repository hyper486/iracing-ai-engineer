# MVP Milestones

## M0: Freeze the input contract

Goal: prove what is being read before building AI behavior.

- Establish a typed Python 3.12 domain model.
- Build a Windows SDK probe that prints current session variable names, units, types, array lengths, and tick rate.
- Save raw frames, SessionInfo updates, and a schema manifest.
- Make the `.ibt` adapter and live adapter emit the same `TelemetrySample` contract.
- Record dropped ticks, stale sources, session resets, and missing variables.

Completion condition: one live session can be recorded without loss and replayed deterministically through the same event pipeline; two replays produce identical event and result hashes.

## M1: Laps, stints, and pit events

- Identify valid laps, out/in laps, resets, reverse travel, and session transitions.
- Identify pit entry, pit stall, service start/end, and pit exit.
- Calculate start/end fuel, green fraction, and traffic/draft risk for each lap.
- Establish versioned event-rules profiles.

Completion condition: on frozen samples, lap, stint, and pit events match human labels; abnormal input does not contaminate the next lap.

## M2: Live Fuel Engineer

- Robust per-lap fuel-use model and high-quantile fuel-to-end estimate.
- A time-based “possibly one more lap” branch.
- Pit window and fuel-latest estimates.
- Pit-loss and service-time calibration.
- Basic rejoin traffic.
- Shadow recommendations and explanation logs.

Completion condition: historical replays and AI/Hosted sessions produce no fuel-out or rules-violating recommendation; high-quantile coverage reaches its configured target.

## M3: Post-session Driving Engineer

- Clean-lap gates and condition matching.
- Distance grid, track template, corner, and driving-event detection.
- Closed-loop delta-time decomposition.
- Reproducible reference lap.
- Three initial diagnoses: late braking that hurts the exit, a long coast, and an early throttle application followed by a second lift.
- Top-three corner cards and a practice plan.

Completion condition: a human-labeled regression set passes; every recommendation shows supporting laps, counterexamples, condition matching, and the reason for its confidence.

## M4: Low-interruption speech

- Deterministic phrase templates.
- P0–P3 priority, cooldown, hysteresis, and supersession.
- Safe windows on straights, while not side-by-side, and outside braking zones.
- One-click mute and in-race advisor-only mode.
- Optional LLM for wording and questions only.

Completion condition: shadow mode contains no contradictory prompts; audio timing and cognitive load are manually reviewed in AI/Hosted sessions before official-race speech is enabled.

## Later versions

- Opponent stints and pit hazard;
- undercut/overcut Monte Carlo;
- personalized tire performance and wear belief;
- wet-tire crossover;
- multi-class traffic-loss learning;
- fuel saving versus splash-stop comparison;
- high-confidence curb and line recommendations;
- local conversation such as “what if we stay out two more laps?”;
- optional official pit-service settings, with separate opt-in.

## First real-data package

Start with one combination and collect:

1. A continuous 20–30 minute practice `.ibt` with at least 8–10 clean laps.
2. An AI or Hosted endurance session containing at least two pit stops.
3. The corresponding car, track configuration, setup/fixed setup, weather, and event rules.
4. Subjective labels for two or three corners that felt worst and one or two that felt good.
5. A replay when possible, for manual review of traffic, curbs, and incidents.

## First-target selection criteria

- The driver participates in the combination regularly.
- The lap is not so long that sample collection becomes impractical.
- There is at least one forced or natural pit stop.
- The first version prioritizes dry conditions.
- Start with a familiar GT3/GT4 instead of introducing hybrid energy, open tire compounds, and complex wet strategy at the same time.

## Three decisions before writing the collector

- Does iRacing run on the same Windows PC, or should UI/analysis run on another machine?
- What is the first car, track, and series?
- Should the race experience prioritize speech, an overlay, or both?

# Conditional fuel / tire service and rejoin comparison

The native trial now links the **same hypothetical stop** to its fuel dose,
fuel/tire service time and mapped exit traffic. This extends the live fuel and
rejoin projections (`live-fuel-stop-comparison-v2`, `live-mapped-rejoin-v2`).
It is advisor-only, `USER_RULE`, not calibrated tire advice, physical wear,
a race optimum or a live acceptance result.

## Native setup and questions

While stopped with fresh owned in-car telemetry, use **模型与本地设置**:

1. Confirm effective tank capacity and, for service comparisons, refuel rate.
2. Enter **四轮换胎耗时** and choose **并行** or **串行** together. These are
   assumptions about this car/event's service process, never inferred from a
   pit exit, tire counter or remote language model.
3. To link component costs to rejoin, provide transit net-loss bounds, plus
   **其他开销** low/high: all additional, non-overlapping overhead beyond the
   fuel/tire block, e.g. extra jacking, repairs, queue or driver change.
   Explicit `0 / 0` means none is assumed; empty means unknown. Do not count
   jacking in both tire time and other overhead.
4. Confirm entry/exit lap fractions for this track/layout. Without geometry,
   complete costs can be compared, but traffic cannot be projected.

Alternatively use the previous **fixed complete net loss** mode: leave other
overhead empty, enter the full low/high loss and both positions. Its range
must cover all services and compared doses. It does not distinguish tire
variants or add fuel/tire time to an already complete total. Mixing modes is
rejected. A historical pit-visit draft fills fixed mode and clears the other
overhead form fields; it never applies active strategy parameters automatically.

Settings remain memory-only and current-source/session/player scoped. Existing
parked-only confirmation, reset, freshness and revision guards are unchanged.
No game settings, voice preferences, credentials or SDK commands are changed.

- **换胎会多花多久 / 比较换胎耗时 / 比较进站服务**: local short service answer,
  with full endpoint comparisons in native text. **问换胎耗时** is a shortcut.
- **比较进站方案**: fuel window, next fill/stint and optional service comparisons.
- **出站预测**: mapped early/late entries with **仅补油对照** and
  **补油加四轮换胎** variants when complete components are available.

Exact questions bypass DeepSeek. Optional provider explanations only select
allowlisted computed facts; no raw traces or opponent identities are added.
Short speech identifies its conditional scenario, never ranks strategies, and
retains ten-second expiry plus configuration/plan withdrawal. Proximity priority
is unchanged.

## Arithmetic and boundaries

For next-stop fuel dose `A` and entered rate `r`:

```
fuel_time = A / r
parallel_service = max(fuel_time, four_tire_time)
sequential_service = fuel_time + four_tire_time
extra_tire_time = service - fuel_time
partial_loss = transit_net_loss + service
complete_loss = partial_loss + explicitly_confirmed_other_overhead
```

The fuel-only counterfactual uses `service = fuel_time`. It does **not** mean
retaining tires is safe, legal or desirable. Zero incremental tire time is not
evidence that changing tires is required or improves pace.

Invented example: 12 L at 2 L/s takes 6 s. A 20 s four-tire change adds 14 s
with parallel work or 20 s with sequential work. Transit loss `[20, 24]` s and
other overhead `[3, 5]` s produce complete losses `[29, 35]` s for fuel only,
`[43, 49]` s for parallel fuel/tires, and `[49, 55]` s for sequential fuel/tires.
These are user-input envelopes, not statistical confidence bounds.

Mapped entries recompute fractional-distance fuel and service costs; whole-lap
arrival quantities are not reused. Traffic still uses each car's phase profiles
and the mapped exit, not current gaps relabeled as future gaps. At most two
endpoints times two service variants are considered, each with its own complete
loss, neighbors and rejection reasons. Loss outside 0.1–600 s, distant entry,
excessive forecast horizon or ambiguous neighbors withdraws that variant.
Source, motion, permission and flag guards can withdraw all variants.

Missing other overhead never becomes zero; partial loss never enters rejoin.
Fixed totals remain one unspecified-service variant per endpoint. Exact typed
recomputation rejects modified costs, service labels, provenance, free text or
stale source data.

This is a **fuel-required-stop** comparison. A zero-stop fuel budget does not
create a speculative tire-only stop. It does not calculate new-tire benefit,
wear-safe retention, full-race service allocation, event-required stops or an
optimal sequence. Calibrated performance and reviewed live installation origins
remain unfinished; offline sealed-capture models are not admitted to live advice.

## Verification

Synthetic tests cover overlap/sequential/zero-dose/fuel-dominant cases, missing
versus zero overhead, exclusive modes, malformed inputs, mapped variants, exact
recomputation, local questions, stale answers and fake PTT cancellation. Isolated
native tests cover Chinese mode selection, parsing, clearing and the fixed-mode
draft. The packaged invented-stream test now requires a local service answer
and component-based rejoin alongside fuel/corner/stint/proximity paths.

Visible Computer Use QA inspected synthetic native fields, mode choices,
scrolling and explanatory text, plus the final frozen settings window.
No simulator, microphone, headphones, private
capture, saved key or provider was used. Real service calibration and hardware/VR
acceptance remain open.

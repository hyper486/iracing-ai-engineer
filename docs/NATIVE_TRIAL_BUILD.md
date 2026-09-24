# Integrated native trial build

This is a local, unsigned experimental EXE, not racing acceptance or a GitHub
binary release. It runs without Simulator Controller or a browser. Existing
installed copies and shortcuts are not automatically upgraded; build output is
`dist/AEIS-Engineer.exe` with an adjacent SHA-256/build receipt.

## What the integrated self-test actually exercises

The default frozen-binary self-test now feeds invented numeric frames through
the real normalization, proximity, fuel, recent-corner, observed-stint and conditional stop
owners. It requires left/clear transitions, learned fuel, repeated-corner
evidence, local fuel/coaching/stop/stint/tire-counter/raw-pace answers and bounded retained state. These
questions must use no provider requests. The fixture is streaming, with only
two cached lap shapes; it does not store a whole synthetic race in memory.
Seven numerical checks require at least eight invented laps including capture
guards; those guards are not six valid real driving laps or a live tire model.

The existing checks additionally exercise hidden native Tk controls, local
answer-worker shutdown, the window-close protocol, exact speech-model hashes
and Chinese TTS-to-STT in memory. Neither test opens a microphone or speaker,
loads saved credentials, starts an SDK transport or operates the simulator.
The receipt remains `SYNTHETIC`, `live_acceptance=false`.

Internal SDK-shaped fixture tags exercise production admission guards. They
are not proof of source authenticity and are never exported as a capture,
sealed SDK receipt or accepted driving evidence. Output is aggregate only.

## Numerical resource diagnostic

From a source checkout, choose a new ignored/local output path:

```powershell
uv run python scripts/check_synthetic_runtime.py --laps 500 --output build/new-synthetic-runtime.json
```

The diagnostic processes a 60 Hz virtual stream, pauses the virtual clock at
corner-worker boundaries, injects a refuel discontinuity every 50 laps and
retains only scalar counters/peaks. It checks retained lap, row, worker-byte,
fuel-history and audit limits. State samples are taken every half virtual
second; worker peaks include queued and active jobs. On Windows, current
private committed bytes are sampled at lap crossings, with a 24-lap warmup;
peak growth is a measurement, not an automatic memory-budget acceptance gate.
The final memory value is sampled after worker closure, not an RSS high-water
mark. Non-Windows private-memory measurements are unavailable, not zero.

This is **not** a wall-clock endurance soak, SDK-rate/latency measurement,
microphone/STT/device/recording stress test or VR performance test. In
particular, pacing a virtual clock hides neither a claimed latency result nor
an overload result: neither is measured. Real reader timing and selected-device
behavior still require a human-driven session.

The desktop answer-withdrawal state is separately constant-space. A monotonic
service-epoch/answer high-water mark replaces the race-long expired-ID set.
Twenty thousand synthetic withdrawals, old-ID replay and reconfiguration are
covered by regression tests. Old answers cannot revive after the set would
otherwise have been truncated; old packets cannot invalidate a newer answer.

## Focused practice checklist

1. Close the older AEIS app normally; run the newly built EXE explicitly.
   Existing keys and device preferences stay in the private local profile.
   Do not run a second independent recorder in parallel.
2. While parked, select microphone/output in **语音与 VR**, or keep system
   defaults. Use **试听** to verify the actual headphones. Enable PTT and the
   separate **启用近车语音** switch; old voice settings leave Spotter off.
3. Start and enter iRacing yourself. Check transport, proximity data and audio
   health separately. In ordinary safe traffic, verify a real left/right event
   and subsequent clear call. Do not deliberately create a hazardous manoeuvre
   to trigger the test. A greeting or `READY` label is not a hearing pass.
4. Ask **还有多少油** and compare with the simulator. Learned range normally
   needs an initial crossing plus five valid complete laps. Ask **每圈用多少油**
   and **当前燃油还能跑几圈** after learning; missing evidence must remain absent.
5. After several clean comparable laps, ask **哪里可以改进**. There must be a
   repeat-supported pattern; enough laps alone do not guarantee advice. Current
   observations do not establish curb geometry, tire wear or causal time gains.
   Ask **这一段跑了多久 / 轮胎怎么样 / 配速变化** for the observation slice.
   Partial attachment is not a complete stint. Pace needs six consecutive clean
   comparable laps; its raw median change cannot by itself justify new tires.
6. While parked, enter verified effective tank capacity and optional pumping/
   transit-loss assumptions under local settings. Ask **比较进站方案**. This is
   a whole-lap conditional fuel budget, not an optimal stop/rejoin command.
7. If something stays silent, preserve the ended private `trial-*.jsonl` and
   optional raw capture. Native **回放近车诊断日志…** separates no detector event,
   suppression and recorded playback outcomes without calling a model or
   playing audio. Never upload these private inputs to the public repository.

Acceptance remains open for actual acquisition quality, headset hearing, PTT
recognition/response timing and VR load. Full tire/rule/service/action-bound
rejoin integration is also unfinished. This build never sends vehicle,
simulator-launch or pit-black-box commands.

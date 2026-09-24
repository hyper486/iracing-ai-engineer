"""Frozen fake-PTT exercise of the real assertion mailbox and analysis owner.

All frames, utterances and audio endpoints are invented. No microphone, speaker,
SDK transport, settings, provider or service evidence is used.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

from .live_app import AppState, _LiveAnalysis
from .live_fuel import LiveFuelConfig
from .synthetic_runtime import synthetic_frames
from .voice_service import VoiceService
from .voice_settings import default_voice_settings
from .voice_tire_confirmation import tire_voice_acknowledged


def run_synthetic_tire_voice():
    now, phrase, release, spoken = [1.], [""], [None], []
    state = AppState(clock=lambda: now[0])
    state.connection("CONNECTED")
    owner = _LiveAnalysis(state, LiveFuelConfig(), identifier="synthetic-tire-voice",
        tick_rate=60, car_count=3, generation=state.generation, allowed=lambda: True)
    frames, tickets = iter(synthetic_frames(8)), []

    def advance(count):
        for _ in range(count):
            frame = next(frames)
            tick = frame.buffer_tick
            frame = replace(frame, values={**frame.values, "LapCompleted": 3, "Lap": 4,
                "OnPitRoad": tick >= 10, "PlayerCarInPitStall": tick >= 12,
                "PitstopActive": 12 <= tick < 20, "Speed": 0. if tick >= 12 else 30.,
                "PlayerTireCompound": 0, "TireSetsUsed": 1})
            now[0] = frame.captured_monotonic_s
            owner.process((frame, "Race", now[0], 1_200_000))

    def source():
        return {"lifecycle": "RUNNING", "telemetry": state.snapshot()}

    def confirm(kind, **kwargs):
        ticket = state.confirm_tire_service(kind, **kwargs)
        tickets.append(ticket)
        advance(35)  # Actual owner consumes once and publishes its matching receipt.
        return ticket

    def forbidden(*_a, **_kw):
        raise AssertionError("SYNTHETIC_TIRE_VOICE_NO_PROVIDER_OR_SETTINGS")

    def record(*_a, **_kw):
        release[0].set()  # Real release gesture, not priority-audio cancellation.
        return bytes(32_000)

    def synthesize(text, **_kw):
        spoken.append(text)
        return b"INVENTED_AUDIO_NOT_FOR_PLAYBACK"

    voice = VoiceService(source, forbidden, SimpleNamespace(), tire_confirm=confirm,
        audio=SimpleNamespace(play=lambda *_a, **_kw: None, record=record),
        speech=SimpleNamespace(synthesize=synthesize,
            recognize=lambda *_a, **_kw: {"text": phrase[0], "confidence": .99}),
        input_factory=lambda **_kw: SimpleNamespace(close=lambda: None,
            snapshot=lambda: {"status": "CLOSED"}))
    voice._settings = {**default_voice_settings(), "enabled": True}
    passed = False
    try:
        advance(36)
        for text in ("记录四胎已换新", "确认记录"):
            voice._invalidate(preserve_tire_review=True)
            voice._cancel, release[0] = threading.Event(), threading.Event()
            phrase[0] = text
            voice._listen((voice._epoch, voice._cancel, release[0], voice._settings))
            if text != "确认记录" and (tickets or voice._tire_review is None
                                      or not voice._tire_review.armed):
                raise AssertionError("SYNTHETIC_TIRE_VOICE_DRAFT")
        passed = (len(tickets) == 1 and tire_voice_acknowledged(source(), tickets[0])
                  and any("已记录你的确认" in text for text in spoken)
                  and voice._tire_review is None)
    finally:
        voice.close()
        owner.close()
        state.connection("STOPPED")
    return {"id": "SYNTHETIC_TIRE_VOICE_CONFIRMATION", "status": "PASS" if passed else "FAIL"}

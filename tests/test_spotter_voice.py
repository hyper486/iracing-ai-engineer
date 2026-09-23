"""Invented detector snapshots and fake PCM output, never audible acceptance."""

import copy
import io
import struct
import threading
import time
import wave

import pytest

from iracing_ai_engineer.priority_audio import AudioPreempted, PriorityAudio
from iracing_ai_engineer.spotter import PHRASES, SPOTTER_CONTRACT_VERSION
from iracing_ai_engineer.spotter_voice import SpotterVoice, trim_phrase_silence
from iracing_ai_engineer.voice_audio import AudioError, _decode_wav
from iracing_ai_engineer.voice_service import cue_wave
from iracing_ai_engineer.voice_settings import default_voice_settings


def wait_for(predicate):
    deadline = time.perf_counter() + 3
    while not predicate():
        if time.perf_counter() > deadline:
            raise AssertionError("SYNTHETIC_SPOTTER_TIMEOUT")
        time.sleep(0.005)


def source(code=1, *, sequence=1, generation=1, epoch=0, kind=None):
    kind = kind or {1: "ALL_CLEAR", 2: "CAR_LEFT", 3: "CAR_RIGHT", 4: "CARS_BOTH_SIDES",
                    5: "TWO_CARS_LEFT", 6: "TWO_CARS_RIGHT"}[code]
    return {
        "lifecycle": "RUNNING", "connection": "CONNECTED", "source_mode": "LIVE",
        "generation": generation,
        "spotter": {
            "contract_version": SPOTTER_CONTRACT_VERSION, "status": "READY", "epoch": epoch,
            "advisor_only": True, "updated_age_s": 0.01, "car_left_right": code,
            "candidate": {"sequence": sequence, "epoch": epoch, "kind": kind, "state": code,
                          "expires_in_s": 0.75, "text": "NEVER SPEAK SOURCE TEXT"},
        },
    }


class Speech:
    def __init__(self):
        self.calls = []
        self.waves = {}
        self.fail = False

    def synthesize(self, text, *, voice, rate=0):
        if self.fail:
            raise ValueError("SYNTHETIC PRIVATE ERROR")
        self.calls.append((text, voice))
        raw = cue_wave(800 + 10 * len(self.calls))
        self.waves[raw] = text
        return raw


class Audio:
    def __init__(self):
        self.calls = []
        self.started = []
        self.block = False
        self.fail = False
        self.entered = threading.Event()
        self.open_gate = None

    def devices(self):
        return {"inputs": [], "outputs": [{"id": "sd-" + "a" * 64, "name": "Synthetic"}]}

    def play(self, wav, stop, **kwargs):
        self.calls.append((wav, stop, kwargs))
        self.entered.set()
        if self.open_gate is not None:
            assert self.open_gate.wait(2)
        if stop.is_set() or not kwargs.get("start_guard", lambda: True)():
            stop.set()
            return
        if self.fail:
            raise AudioError("AUDIO_DEVICE_MISSING")
        self.started.append(wav)
        kwargs.get("on_started", lambda: None)()
        if self.block:
            stop.wait(2)


@pytest.fixture
def rig():
    audio, speech, current = Audio(), Speech(), source()
    current["spotter"]["candidate"] = None
    arbiter = PriorityAudio(audio)
    voice = SpotterVoice(lambda: copy.deepcopy(current), arbiter, speech=speech)
    voice.start()
    yield voice, audio, speech, current, arbiter
    voice.close()


def enable(voice, **changes):
    settings = {**default_voice_settings(), "spotter_enabled": True, **changes}
    voice.configure(settings)
    wait_for(lambda: voice.snapshot()["status"] in ("READY", "WAIT_DATA", "ERROR", "PAUSED"))


def test_disabled_service_never_renders_opens_audio_or_claims_acceptance(rig):
    voice, audio, speech, _, _ = rig
    assert voice.snapshot()["status"] == "OFF"
    assert voice.snapshot()["heard"] is voice.snapshot()["live_acceptance"] is False
    assert audio.calls == speech.calls == []


@pytest.mark.parametrize("code", range(2, 7))
def test_all_side_states_use_cached_fixed_phrases_without_fuel_stt_or_llm(rig, code):
    voice, audio, speech, current, _ = rig
    enable(voice)
    cache_calls = len(speech.calls)
    assert cache_calls == 11
    current.update(source(code))
    wait_for(lambda: voice.snapshot()["counts"].get("PLAYBACK_COMPLETED") == 1)
    assert len(speech.calls) == cache_calls
    assert speech.waves[audio.started[0]] == PHRASES[current["spotter"]["candidate"]["kind"]]
    assert audio.calls[0][2]["device"] == "default"
    assert "NEVER SPEAK SOURCE TEXT" not in str(speech.calls)
    assert voice.snapshot()["output_status"] == "STARTED_NOT_HEARING_CONFIRMED"
    assert all(row["heard"] is False for row in voice.snapshot()["audit"])


def test_one_candidate_is_not_replayed_each_worker_poll(rig):
    voice, audio, _, current, _ = rig
    enable(voice)
    current.update(source(2))
    wait_for(lambda: len(audio.started) == 1)
    time.sleep(0.08)
    assert len(audio.started) == 1
    current.update(source(2, generation=2))
    wait_for(lambda: len(audio.started) == 2)


@pytest.mark.parametrize("mutation", [
    {"lifecycle": "STOPPED"}, {"source_mode": "SYNTHETIC"}, {"connection": "DISCONNECTED"},
    {"generation": True},
])
def test_unknown_or_inactive_source_never_plays(rig, mutation):
    voice, audio, _, current, _ = rig
    current.update(source(2))
    current.update(mutation)
    enable(voice)
    time.sleep(0.05)
    assert audio.started == []


@pytest.mark.parametrize("mutation", [
    {"status": "STALE"}, {"updated_age_s": 0.3}, {"updated_age_s": -1},
    {"updated_age_s": float("nan")}, {"car_left_right": True}, {"advisor_only": False},
    {"candidate": None}, {"contract_version": "unknown"},
])
def test_bad_detector_state_never_plays(rig, mutation):
    voice, audio, _, current, _ = rig
    current.update(source(2))
    current["spotter"].update(mutation)
    enable(voice)
    time.sleep(0.05)
    assert audio.started == []


def test_changed_side_cancels_playing_phrase_and_latest_side_replaces_it(rig):
    voice, audio, speech, current, _ = rig
    audio.block = True
    enable(voice)
    current.update(source(2))
    wait_for(lambda: len(audio.started) == 1)
    current.update(source(3, sequence=2))
    wait_for(lambda: len(audio.started) == 2)
    assert audio.calls[0][1].is_set()
    assert [speech.waves[raw] for raw in audio.started] == [
        PHRASES["CAR_LEFT"], PHRASES["CAR_RIGHT"],
    ]


def test_start_deadline_does_not_clip_a_still_supported_phrase_but_stale_data_does(rig):
    voice, audio, _, current, _ = rig
    audio.block = True
    enable(voice)
    current.update(source(2))
    wait_for(lambda: len(audio.started) == 1)
    current["spotter"]["candidate"] = None
    time.sleep(0.3)
    assert not audio.calls[0][1].is_set()
    current["spotter"]["status"] = "STALE"
    wait_for(lambda: audio.calls[0][1].is_set())
    wait_for(lambda: voice.snapshot()["counts"].get("CANCELLED") == 1)


def test_slow_device_open_cannot_start_a_superseded_call(rig):
    voice, audio, _, current, _ = rig
    audio.open_gate = threading.Event()
    enable(voice)
    current.update(source(2))
    assert audio.entered.wait(1)
    current["spotter"]["status"] = "STALE"
    audio.open_gate.set()
    wait_for(lambda: voice.snapshot()["counts"].get("DROPPED_BEFORE_START") == 1)
    assert audio.started == []


def test_output_failure_is_latched_without_exposing_native_error_or_retry_storm(rig):
    voice, audio, _, current, _ = rig
    audio.fail = True
    enable(voice)
    current.update(source(2))
    wait_for(lambda: voice.snapshot()["status"] == "ERROR")
    assert voice.snapshot()["reason"] == "AUDIO_DEVICE_MISSING"
    time.sleep(0.08)
    assert len(audio.calls) == 1


def test_cache_failure_does_not_claim_output_readiness(rig):
    voice, audio, speech, _, _ = rig
    speech.fail = True
    enable(voice)
    assert voice.snapshot()["status"] == "ERROR"
    assert voice.snapshot()["reason"] == "PHRASE_CACHE_FAILED"
    assert "SYNTHETIC PRIVATE ERROR" not in str(voice.snapshot())
    assert audio.calls == []


def test_zero_volume_is_explicitly_paused(rig):
    voice, audio, speech, _, _ = rig
    enable(voice, volume=0)
    assert voice.snapshot()["reason"] == "ZERO_VOLUME"
    assert audio.calls == speech.calls == []


def test_stop_and_close_cancel_audio_and_stop_both_workers(rig):
    voice, audio, _, current, _ = rig
    audio.block = True
    enable(voice)
    current.update(source(2))
    wait_for(lambda: len(audio.started) == 1)
    voice.suspend()
    wait_for(lambda: audio.calls[0][1].is_set())
    current.update(source(3, sequence=2))
    time.sleep(0.05)
    assert len(audio.started) == 1 and voice.snapshot()["status"] == "PAUSED"
    voice.close()
    assert not voice._worker.is_alive() and not voice._guard_worker.is_alive()


def test_proximity_interrupts_long_low_priority_playback(rig):
    voice, audio, _, current, arbiter = rig
    enable(voice)
    audio.block = True
    outcomes = []

    def long_answer():
        try:
            arbiter.play(b"LONG_ANSWER", threading.Event())
        except AudioPreempted:
            outcomes.append("CANCELLED")

    worker = threading.Thread(target=long_answer)
    worker.start()
    assert audio.entered.wait(1)
    current.update(source(2))
    wait_for(lambda: len(audio.started) == 2)
    worker.join(1)
    assert outcomes == ["CANCELLED"]


def test_data_loss_warning_once_per_outage_only_after_an_armed_session(rig):
    voice, audio, speech, current, _ = rig
    enable(voice)
    wait_for(lambda: voice.snapshot()["status"] == "READY")
    current["spotter"]["status"] = "STALE"
    wait_for(lambda: len(audio.started) == 1)
    assert "暂不可用" in speech.waves[audio.started[0]]
    time.sleep(0.08)
    assert len(audio.started) == 1


def test_interrupted_question_notice_waits_until_clear_and_never_replays_a_question(rig):
    voice, audio, speech, current, _ = rig
    enable(voice)
    voice.interrupted_question()
    current.update(source(2))
    wait_for(lambda: len(audio.started) == 1)
    current.update(source())
    current["spotter"]["candidate"] = None
    wait_for(lambda: len(audio.started) == 2)
    assert "提问已取消" in speech.waves[audio.started[1]]


def test_raw_frame_to_audio_uses_fast_state_even_when_no_fuel_snapshot_has_ever_published():
    from iracing_ai_engineer.live_app import AppState
    from iracing_ai_engineer.runtime_clock import monotonic_now
    from iracing_ai_engineer.sdk_probe import RawSdkFrame

    state = AppState()
    state.connection("CONNECTED")
    audio, speech = Audio(), Speech()
    voice = SpotterVoice(lambda: {**state.spotter_snapshot(), "lifecycle": "RUNNING"},
                         PriorityAudio(audio), speech=speech)
    voice.start()
    try:
        enable(voice)
        for tick in range(1, 11):
            now = monotonic_now()
            state.feed_spotter(RawSdkFrame(
                buffer_tick=tick, session_info_update=1, sim_mode_raw="full",
                captured_monotonic_s=now,
                values={"SessionNum": 0, "SessionTick": tick, "SessionTime": tick / 60,
                        "PlayerCarIdx": 0, "CarLeftRight": 2, "IsOnTrack": True,
                        "IsOnTrackCar": True, "IsReplayPlaying": False,
                        "OnPitRoad": False, "PlayerCarInPitStall": False},
            ))
            time.sleep(1 / 60)
        wait_for(lambda: voice.snapshot()["counts"].get("PLAYBACK_COMPLETED") == 1)
        assert state.snapshot()["fuel"] is None
        assert state.snapshot()["connection"] == "DISCONNECTED"  # Slow display has no sample.
        assert state.spotter_snapshot()["connection"] == "CONNECTED"
        assert speech.waves[audio.started[0]] == PHRASES["CAR_LEFT"]
    finally:
        voice.close()


def test_disabling_while_cache_is_being_rendered_cannot_publish_old_ready_state(rig):
    voice, audio, speech, current, _ = rig
    entered, proceed = threading.Event(), threading.Event()
    original = speech.synthesize

    def blocked(*args, **kwargs):
        entered.set()
        assert proceed.wait(2)
        return original(*args, **kwargs)

    speech.synthesize = blocked
    voice.configure({**default_voice_settings(), "spotter_enabled": True})
    assert entered.wait(1)
    voice.configure(default_voice_settings())
    current.update(source(2))
    proceed.set()
    time.sleep(0.08)
    assert voice.snapshot()["status"] == "OFF"
    assert voice.snapshot()["cached_phrases"] == 0 and audio.calls == []


def test_unavailable_selected_output_fails_before_rendering_or_silent_fallback(rig):
    voice, audio, speech, _, _ = rig
    enable(voice, output_device="sd-" + "b" * 64)
    assert voice.snapshot()["status"] == "ERROR"
    assert voice.snapshot()["reason"] == "AUDIO_DEVICE_MISSING"
    assert audio.calls == speech.calls == []


@pytest.mark.parametrize("channels", [1, 2])
def test_trimming_only_removes_outer_zero_frames_and_preserves_quiet_speech(channels):
    samples = [0] * 1600 + [1, 0, -1] + [0] * 1600
    pcm = b"".join(struct.pack("<h", value) * channels for value in samples)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(pcm)
    trimmed, rate, count = _decode_wav(trim_phrase_silence(buffer.getvalue()))
    assert rate == 16000 and count == channels
    assert trimmed == pcm[(1600 - 320) * channels * 2:(1603 + 320) * channels * 2]


def test_entirely_silent_cache_phrase_is_not_ready(rig):
    voice, audio, speech, _, _ = rig
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * 1600)
    speech.synthesize = lambda *_args, **_kwargs: buffer.getvalue()
    enable(voice)
    assert voice.snapshot()["reason"] == "PHRASE_CACHE_FAILED"
    assert audio.calls == []

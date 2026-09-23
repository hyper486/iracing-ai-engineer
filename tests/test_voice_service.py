"""Synthetic VR pipeline tests: no microphone, speaker, keyboard or provider."""

from __future__ import annotations

import copy
import io
import threading
import time
import wave

import pytest

from iracing_ai_engineer.voice_service import (
    FuelVoicePolicy,
    VoiceService,
    concise_answer,
    cue_wave,
    safe_fuel_snapshot,
)
from iracing_ai_engineer.voice_settings import default_voice_settings


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("SYNTHETIC_VOICE_TIMEOUT")
        time.sleep(0.005)


def state():
    return {"lifecycle": "RUNNING", "telemetry": {},
            "engineer": {"instance_id": "0", "answer": None}}


def answer(text="当前燃油约二十升。请注意实验估计限制。"):
    return {"id": "1", "scope": "live_snapshot", "stale": False, "text": text}


class Store:
    def __init__(self, enabled=False):
        self.value = {**default_voice_settings(), "enabled": enabled}
        self.saves = []

    def load_voice(self):
        return copy.deepcopy(self.value)

    def save_voice(self, value):
        self.value = copy.deepcopy(value)
        self.saves.append(value)


class Input:
    def __init__(self, **callbacks):
        self.callbacks = callbacks
        self.active = False
        self.starts = []
        self.closes = 0
        self.status = "STOPPED"
        self.binding = None
        self.binds = 0

    def snapshot(self):
        return {"binding_active": self.active, "status": self.status,
                "binding": copy.deepcopy(self.binding)}

    def start(self, binding):
        self.starts.append(binding)
        self.binding = copy.deepcopy(binding)
        self.status = "READY"

    def set_binding(self, binding):
        self.starts.append(binding)
        self.binding = copy.deepcopy(binding)

    def begin_bind(self):
        self.active = True
        self.binds += 1

    def cancel_bind(self):
        self.active = False

    def close(self):
        self.closes += 1
        self.status = "CLOSED"
        self.active = False


class Audio:
    def __init__(self):
        self.records = []
        self.plays = []
        self.entered = threading.Event()
        self.block_speech = False
        self.speaking = threading.Event()

    def devices(self):
        return {"inputs": [{"id": "sd-" + "a" * 64, "name": "Synthetic mic"}],
                "outputs": [{"id": "sd-" + "b" * 64, "name": "Synthetic headset"}]}

    def record(self, stop, **kwargs):
        self.records.append(kwargs)
        self.entered.set()
        stop.wait(2)
        return b"\0\0" * 16000

    def play(self, wav, stop, **kwargs):
        self.plays.append((wav, stop, kwargs))
        if stop.is_set() or not kwargs.get("start_guard", lambda: True)():
            stop.set()
            return
        kwargs.get("on_started", lambda: None)()
        if wav == b"SYNTHETIC_SPEECH" and self.block_speech:
            self.speaking.set()
            stop.wait(2)


class Speech:
    def __init__(self):
        self.recognitions = []
        self.synthesis = []
        self.confidence = 0.9

    def probe(self):
        return {"recognizers": [{"culture": "zh-CN", "name": "Synthetic recognizer"}],
                "voices": [{"culture": "zh-CN", "name": "Synthetic voice"}]}

    def recognize(self, pcm, **kwargs):
        self.recognitions.append((pcm, kwargs))
        return {"text": "当前燃油还能跑几圈", "confidence": self.confidence}

    def synthesize(self, text, **kwargs):
        self.synthesis.append(text)
        return b"SYNTHETIC_SPEECH"


@pytest.fixture
def rig():
    audio, speech, store, current, submitted = Audio(), Speech(), Store(True), state(), []

    def submit(text, scope):
        submitted.append((text, scope))
        current["engineer"]["answer"] = answer()
        return 202, {"accepted": True}

    voice = VoiceService(lambda: copy.deepcopy(current), submit, store,
                         audio=audio, speech=speech, input_factory=Input)
    voice.start()
    wait_for(lambda: voice.snapshot()["devices"]["inputs"])
    yield voice, audio, speech, store, current, submitted
    voice.close()


def test_start_only_probes_devices_and_never_opens_audio(rig):
    voice, audio, speech, _, _, submitted = rig
    assert voice.snapshot()["status"] == "READY"
    assert audio.records == audio.plays == speech.recognitions == submitted == []


def proximity(code=1, sequence=1):
    from iracing_ai_engineer.spotter import SPOTTER_CONTRACT_VERSION

    return {
        "lifecycle": "RUNNING", "connection": "CONNECTED", "source_mode": "LIVE",
        "generation": 1,
        "spotter": {"contract_version": SPOTTER_CONTRACT_VERSION,
                    "status": "READY", "epoch": 0, "updated_age_s": 0.01,
                    "car_left_right": code, "advisor_only": True,
                    "candidate": None if code == 1 else {
                        "sequence": sequence, "epoch": 0, "state": code,
                        "kind": "CAR_LEFT", "expires_in_s": 0.75,
                    }},
    }


@pytest.mark.parametrize("phase", ["recording", "recognition", "model", "synthesis", "playback"])
def test_proximity_is_not_blocked_by_any_phase_of_the_question_pipeline(phase):
    audio, speech, store, current, near = Audio(), Speech(), Store(True), state(), proximity()
    store.value["spotter_enabled"] = True
    blocked, proceed = threading.Event(), threading.Event()
    submitted = []
    cache_speech = Speech()
    cache_speech.synthesize = lambda *_args, **_kwargs: cue_wave(900)

    if phase == "recognition":
        def recognize(*_args, **_kwargs):
            blocked.set()
            assert proceed.wait(3)
            return {"text": "当前燃油还能跑几圈", "confidence": 0.9}

        speech.recognize = recognize
    elif phase == "synthesis":
        def synthesize(*_args, **_kwargs):
            blocked.set()
            assert proceed.wait(3)
            return b"SYNTHETIC_SPEECH"

        speech.synthesize = synthesize
    elif phase == "playback":
        audio.block_speech = True

    def submit(text, scope):
        submitted.append((text, scope))
        if phase == "model":
            blocked.set()
        else:
            current["engineer"]["answer"] = answer()
        return 202, {}

    voice = VoiceService(lambda: copy.deepcopy(current), submit, store,
                         audio=audio, speech=speech, input_factory=Input,
                         spotter_source=lambda: copy.deepcopy(near), spotter_speech=cache_speech)
    try:
        voice.start()
        wait_for(lambda: voice.snapshot()["spotter"]["status"] == "READY")
        voice.press()
        assert audio.entered.wait(1)
        if phase != "recording":
            voice.release()
            assert (audio.speaking if phase == "playback" else blocked).wait(1)
        near.update(proximity(2))
        wait_for(lambda: voice.snapshot()["spotter"]["counts"].get("PLAYBACK_COMPLETED") == 1)
        if phase == "recording":
            wait_for(lambda: "取消" in voice.snapshot()["notice"])
            voice.release()
            assert speech.recognitions == submitted == []
        if phase == "playback":
            wait_for(lambda: "取消" in voice.snapshot()["notice"])
        assert voice.snapshot()["spotter"]["heard"] is False
    finally:
        voice.stop()
        proceed.set()
        voice.close()


def test_spotter_output_survives_disabled_ptt_and_failed_local_recognizer_probe():
    audio, speech, store, near = Audio(), Speech(), Store(False), proximity()
    store.value["spotter_enabled"] = True
    cache_speech = Speech()
    cache_speech.synthesize = lambda *_args, **_kwargs: cue_wave(900)

    def unavailable():
        raise ValueError("SYNTHETIC STT MODEL MISSING")

    speech.probe = unavailable
    voice = VoiceService(state, lambda *_: (409, {}), store, audio=audio, speech=speech,
                         input_factory=Input, spotter_source=lambda: copy.deepcopy(near),
                         spotter_speech=cache_speech)
    try:
        voice.start()
        wait_for(lambda: voice.snapshot()["status"] == "ERROR")
        wait_for(lambda: voice.snapshot()["spotter"]["status"] == "READY")
        near.update(proximity(2))
        wait_for(lambda: voice.snapshot()["spotter"]["counts"].get("PLAYBACK_COMPLETED") == 1)
        assert audio.records == speech.recognitions == []
        assert voice._input.starts == []
    finally:
        voice.close()


def test_stop_during_pending_save_keeps_spotter_paused_until_a_new_apply():
    audio, speech, store, near = Audio(), Speech(), Store(False), proximity()
    cache_speech = Speech()
    cache_speech.synthesize = lambda *_args, **_kwargs: cue_wave(900)
    saving, finish_save = threading.Event(), threading.Event()
    original_save = store.save_voice

    def blocked_save(settings):
        saving.set()
        assert finish_save.wait(3)
        original_save(settings)

    voice = VoiceService(state, lambda *_: (409, {}), store, audio=audio, speech=speech,
                         input_factory=Input, spotter_source=lambda: copy.deepcopy(near),
                         spotter_speech=cache_speech)
    try:
        voice.start()
        wait_for(lambda: voice.snapshot()["status"] == "OFF")
        store.save_voice = blocked_save
        settings = {**store.value, "spotter_enabled": True}
        voice.configure(settings)
        assert saving.wait(1)
        voice.stop()
        finish_save.set()
        wait_for(lambda: not voice._pending_configurations)
        assert voice.snapshot()["spotter"]["status"] == "PAUSED"
        assert voice.snapshot()["spotter"]["cached_phrases"] == 0
        assert audio.plays == []
        store.save_voice = original_save
        voice.configure(settings)
        wait_for(lambda: voice.snapshot()["spotter"]["status"] == "READY")
        near.update(proximity(2))
        wait_for(lambda: voice.snapshot()["spotter"]["counts"].get("PLAYBACK_COMPLETED") == 1)
    finally:
        finish_save.set()
        voice.close()


def test_ptt_release_recognizes_locally_then_reads_grounded_answer(rig):
    voice, audio, speech, _, _, submitted = rig
    voice.press()
    voice.press()  # A repeated held key must not open a second stream.
    assert audio.entered.wait(1)
    voice.release()
    wait_for(lambda: len(speech.synthesis) == 1)
    assert len(audio.records) == 1 and len(speech.recognitions) == 1
    assert submitted == [("当前燃油还能跑几圈", "live")]
    assert voice.snapshot()["transcript"] == "当前燃油还能跑几圈"
    assert "当前燃油约二十升" in speech.synthesis[0]
    assert audio.records[0]["device"] == "default"
    assert all(play[2]["device"] == "default" for play in audio.plays)


def test_recording_cap_without_release_cancels_instead_of_submitting_truncated_question(rig):
    voice, audio, speech, _, _, submitted = rig
    normal_record = audio.record

    def reached_cap(stop, **kwargs):
        assert not stop.is_set()
        audio.records.append(kwargs)
        return b"\0\0" * (16_000 * 12)

    audio.record = reached_cap
    voice.press()
    wait_for(lambda: "超过 12 秒" in voice.snapshot()["notice"])
    assert speech.recognitions == submitted == []
    assert "十二秒" in speech.synthesis[0]
    assert voice._held and not voice._release.is_set()
    voice.press()
    assert len(audio.records) == 1 and voice._jobs.empty()

    # Only releasing and pressing again starts a fresh user-authorized utterance.
    voice.release()
    assert not voice._held
    audio.record = normal_record
    voice.press()
    assert audio.entered.wait(1)
    voice.release()
    wait_for(lambda: len(submitted) == 1)
    assert len(audio.records) == 2 and len(speech.recognitions) == 1
    assert submitted == [("当前燃油还能跑几圈", "live")]


def test_device_preferences_preserve_explicit_mic_and_headset(rig):
    voice, audio, speech, store, _, _ = rig
    value = {**store.value, "input_device": "sd-" + "a" * 64,
             "output_device": "sd-" + "b" * 64, "volume": 0.4}
    voice.configure(value)
    wait_for(lambda: voice.snapshot()["settings"]["volume"] == 0.4)
    voice.press()
    assert audio.entered.wait(1)
    voice.release()
    wait_for(lambda: speech.synthesis)
    assert audio.records[0]["device"] == value["input_device"]
    assert all(play[2]["device"] == value["output_device"] for play in audio.plays)
    assert all(play[2]["volume"] == 0.4 for play in audio.plays)


def test_uncertain_recognition_never_calls_model(rig):
    voice, audio, speech, _, _, submitted = rig
    speech.confidence = 0.2
    voice.press()
    assert audio.entered.wait(1)
    voice.release()
    wait_for(lambda: speech.synthesis)
    assert submitted == [] and "没有听清" in speech.synthesis[0]


def test_stop_or_input_disconnect_cancels_recording_without_submission(rig):
    voice, audio, speech, _, _, submitted = rig
    voice.press()
    assert audio.entered.wait(1)
    voice._input.callbacks["on_error"]("DISCONNECTED")
    voice.release()
    wait_for(lambda: voice.snapshot()["status"] == "ERROR")
    assert not speech.recognitions and not submitted


def test_disabled_voice_ignores_ptt_but_explicit_output_test_works(rig):
    voice, audio, speech, store, _, submitted = rig
    voice.configure({**store.value, "enabled": False})
    wait_for(lambda: voice.snapshot()["settings"]["enabled"] is False)
    voice.press()
    voice.release()
    voice.test()
    wait_for(lambda: speech.synthesis)
    assert audio.records == [] and submitted == []
    assert "无线电检查" in speech.synthesis[0]


def test_stale_answer_interrupts_audio_already_playing(rig):
    voice, audio, _, _, current, _ = rig
    audio.block_speech = True
    voice.press()
    assert audio.entered.wait(1)
    voice.release()
    assert audio.speaking.wait(1)
    current["engineer"]["answer"]["stale"] = True
    wait_for(lambda: voice.snapshot()["status"] == "READY")
    assert audio.plays[-1][1].is_set()


def test_close_cancels_record_and_joins_workers(rig):
    voice, audio, speech, _, _, submitted = rig
    voice.press()
    assert audio.entered.wait(1)
    voice.close()
    assert not voice._worker.is_alive() and not voice._guard_worker.is_alive()
    assert not speech.recognitions and not submitted
    assert voice.snapshot()["status"] == "CLOSED"


def test_binding_while_disabled_does_not_record(rig):
    voice, audio, _, store, _, _ = rig
    voice.configure({**store.value, "enabled": False})
    wait_for(lambda: not voice.snapshot()["settings"]["enabled"])
    voice.bind()
    wait_for(lambda: voice.snapshot()["binding_active"])
    selected = {"kind": "joystick", "guid": "a" * 32,
                "name": "Synthetic wheel", "button": 5}
    voice._input.binding, voice._input.active = selected, False
    voice._input.callbacks["on_binding"](selected)
    wait_for(lambda: voice.snapshot()["settings"]["binding"]["kind"] == "joystick")
    assert audio.records == []


def test_queued_disable_cannot_be_overtaken_by_ptt(rig):
    voice, audio, speech, store, _, submitted = rig
    entered, release = threading.Event(), threading.Event()
    original = store.save_voice

    def blocked_save(value):
        entered.set()
        release.wait(2)
        original(value)

    store.save_voice = blocked_save
    voice.configure({**store.value, "enabled": False})
    assert entered.wait(1)
    try:
        voice.press()
        voice.release()
    finally:
        release.set()
    wait_for(lambda: not voice._pending_configurations)
    assert not voice.snapshot()["settings"]["enabled"]
    assert audio.records == speech.recognitions == submitted == []


def test_cancel_between_recognition_and_submit_cannot_send_text(rig):
    voice, audio, speech, _, _, submitted = rig
    original = voice._source

    def cancel_when_called():
        voice.stop()
        return original()

    voice._source = cancel_when_called
    voice.press()
    assert audio.entered.wait(1)
    voice.release()
    wait_for(lambda: speech.recognitions)
    wait_for(lambda: voice.snapshot()["status"] == "READY")
    assert submitted == []


def test_close_during_saved_configuration_cannot_restart_input(rig):
    voice, _, _, store, _, _ = rig
    voice.configure({**store.value, "enabled": False})
    wait_for(lambda: not voice._pending_configurations)
    starts_before = len(voice._input.starts)
    saving, finish_save = threading.Event(), threading.Event()
    original_save = store.save_voice

    def blocked_save(settings):
        saving.set()
        assert finish_save.wait(3)
        original_save(settings)

    store.save_voice = blocked_save
    voice.configure({**store.value, "enabled": True})
    assert saving.wait(1)
    closer = threading.Thread(target=voice.close)
    closer.start()
    try:
        wait_for(lambda: voice.snapshot()["status"] == "STOPPING")
        assert closer.is_alive()
    finally:
        finish_save.set()
        closer.join(3)
    assert not closer.is_alive()
    assert voice.snapshot()["status"] == "CLOSED"
    assert voice._input.status == "CLOSED" and len(voice._input.starts) == starts_before


def test_close_waits_for_input_closed_not_only_its_bounded_join(rig):
    voice, _, _, _, _, _ = rig
    close_requested, input_can_finish = threading.Event(), threading.Event()

    def delayed_close():
        voice._input.status = "CLOSED" if input_can_finish.is_set() else "STOPPING"
        close_requested.set()

    voice._input.close = delayed_close
    closer = threading.Thread(target=voice.close)
    closer.start()
    try:
        assert close_requested.wait(1)
        assert voice.snapshot()["status"] == "STOPPING" and closer.is_alive()
    finally:
        input_can_finish.set()
        closer.join(3)
    assert not closer.is_alive()
    assert voice.snapshot()["status"] == "CLOSED" and voice._input.status == "CLOSED"


def test_auto_candidate_interleaved_with_ptt_preserves_ptt_cancel_token():
    audio, speech, store = Audio(), Speech(), Store(True)
    voice = VoiceService(lambda: (voice.press() or safe_state()), lambda *_: (409, {}), store,
                         audio=audio, speech=speech, input_factory=Input)
    voice._settings = {**store.value, "auto_fuel": True}
    voice._status = "READY"
    voice._policy.candidate = lambda *_: "Synthetic fuel fact"
    try:
        voice._auto()  # Simulate input callback while fetching the auto snapshot.
        action, job = voice._jobs.get_nowait()
        assert action == "listen" and job[1] is voice._cancel
        assert voice._policy._last_at == -float("inf")
        assert audio.plays == speech.synthesis == []
        voice.stop()
        assert job[1].is_set() and job[2].is_set()
    finally:
        voice.close()


def test_playback_guard_cancels_its_own_token_not_unrelated_new_work(rig):
    voice, audio, _, store, _, _ = rig
    safe = threading.Event()
    safe.set()
    audio.block_speech = True
    cancel, unrelated = threading.Event(), threading.Event()
    epoch = voice._epoch
    speaker = threading.Thread(target=lambda: voice._say(
        "合成测试。", store.value, epoch, cancel, safe.is_set,
    ))
    speaker.start()
    try:
        assert audio.speaking.wait(1)
        with voice._lock:
            voice._cancel = unrelated
        safe.clear()
        assert cancel.wait(1)
        assert not unrelated.is_set()
    finally:
        cancel.set()
        speaker.join(3)
    assert not speaker.is_alive()


def test_cancel_queued_binding_never_starts_input_or_capture():
    voice = VoiceService(state, lambda *_: (409, {}), Store(), audio=Audio(), speech=Speech(),
                         input_factory=Input)
    try:
        voice.bind()
        assert voice.snapshot()["binding_active"]
        voice.cancel_bind()
        action, revision = voice._jobs.get_nowait()
        assert action == "bind"
        voice._begin_bind(revision)
        assert not voice.snapshot()["binding_active"]
        assert voice._input.starts == [] and voice._input.binds == 0
    finally:
        voice.close()


def test_cancel_after_selected_edge_rejects_delayed_binding_callback(rig):
    voice, _, _, store, _, _ = rig
    original = copy.deepcopy(store.value["binding"])
    selected = {"kind": "joystick", "guid": "a" * 32,
                "name": "Synthetic wheel", "button": 5}
    voice.bind()
    wait_for(lambda: voice._input.active)
    voice._input.binding = selected  # Edge selected but callback not yet delivered.
    voice._input.active = False
    voice.cancel_bind()
    voice._bound(selected)
    assert voice._input.binding == original
    assert voice.snapshot()["settings"]["binding"] == original
    assert store.saves == [] and not voice._pending_configurations


def test_old_bound_callback_cannot_complete_a_new_bind_request(rig):
    voice, _, _, store, _, _ = rig
    original = copy.deepcopy(store.value["binding"])
    selected = {"kind": "joystick", "guid": "a" * 32,
                "name": "Synthetic wheel", "button": 5}
    voice.bind()
    wait_for(lambda: voice._input.active)
    voice._input.binding, voice._input.active = selected, False
    voice.cancel_bind()
    voice.bind()
    wait_for(lambda: voice._input.active)
    voice._bound(selected)
    assert voice.snapshot()["binding_active"]
    assert voice._input.binding == original
    assert voice.snapshot()["settings"]["binding"] == original
    assert store.saves == [] and not voice._pending_configurations


@pytest.mark.parametrize("code", ["INPUT_BINDING_CHANGED", "INPUT_CLOSED", "INPUT_DEVICE_CHANGED"])
def test_intentional_input_cancellation_is_not_a_hardware_fault(rig, code):
    voice, audio, speech, _, _, submitted = rig
    voice.press()
    assert audio.entered.wait(1)
    voice._input_error(code)
    voice.release()
    wait_for(lambda: voice.snapshot()["status"] == "READY")
    assert speech.recognitions == submitted == []
    assert "取消" in voice.snapshot()["notice"]


def test_stop_and_intentional_cancel_do_not_hide_an_existing_input_fault(rig):
    voice, _, _, _, _, _ = rig
    voice._input_error("INPUT_DEVICE_MISSING")
    voice._input_error("INPUT_BINDING_CHANGED")
    voice.stop()
    assert voice.snapshot()["status"] == "ERROR"


def test_input_start_error_cannot_be_overwritten_by_apply_ready():
    voice = VoiceService(state, lambda *_: (409, {}), Store(), audio=Audio(), speech=Speech(),
                         input_factory=Input)
    original = voice._input.start

    def failed_start(binding):
        original(binding)
        voice._input_error("INPUT_DEVICE_MISSING")

    voice._input.start = failed_start
    try:
        voice._apply({**default_voice_settings(), "enabled": True}, persist=False)
        assert voice.snapshot()["status"] == "ERROR"
    finally:
        voice.close()


def test_recognition_error_after_user_cancel_does_not_become_hardware_fault(rig):
    voice, audio, speech, _, _, submitted = rig
    recognizing, proceed = threading.Event(), threading.Event()

    def delayed_failure(*_args, **_kwargs):
        recognizing.set()
        assert proceed.wait(3)
        raise ValueError("SYNTHETIC_RECOGNIZER_FAILURE")

    speech.recognize = delayed_failure
    voice.press()
    assert audio.entered.wait(1)
    voice.release()
    assert recognizing.wait(1)
    voice.stop()
    proceed.set()
    # A FIFO refresh provides an explicit barrier after the failed old job.
    completed = threading.Event()
    original_devices = audio.devices
    audio.devices = lambda: (completed.set() or original_devices())
    voice.refresh_devices()
    assert completed.wait(1)
    assert voice.snapshot()["status"] == "READY" and submitted == []


def test_optional_local_recognizer_cancel_and_close_are_called_outside_service_lock(rig):
    voice, audio, speech, _, _, submitted = rig
    cancellations, closes = [], []

    def cancel():
        assert not voice._lock._is_owned()
        cancellations.append(True)

    def close():
        assert not voice._lock._is_owned()
        closes.append(True)

    speech.cancel, speech.close = cancel, close
    voice.press()
    assert audio.entered.wait(1)
    voice.stop()
    assert len(cancellations) >= 2
    voice.close()
    assert len(cancellations) >= 3 and closes == [True]
    assert submitted == [] and voice.snapshot()["status"] == "CLOSED"
    voice.close()
    assert closes == [True]


def test_recognizer_cancel_unblocks_inference_before_close_joins(rig):
    voice, audio, speech, _, _, submitted = rig
    recognizing, cancelled = threading.Event(), threading.Event()

    def recognize(*_args, **_kwargs):
        cancelled.clear()
        recognizing.set()
        assert cancelled.wait(3)
        raise ValueError("SYNTHETIC_CANCELLED")

    speech.recognize, speech.cancel = recognize, cancelled.set
    voice.press()
    assert audio.entered.wait(1)
    voice.release()
    assert recognizing.wait(1)
    voice.close()
    assert cancelled.is_set() and not voice._worker.is_alive()
    assert submitted == [] and voice.snapshot()["status"] == "CLOSED"


def test_earcon_is_bounded_mono_pcm_and_answer_cannot_truncate_claim():
    with wave.open(io.BytesIO(cue_wave())) as handle:
        assert handle.getnchannels() == 1 and handle.getsampwidth() == 2
        assert handle.getframerate() == 16000 and handle.getnframes() == 1600
    assert "无法安全压缩" in concise_answer(answer("a" * 1000))
    assert "历史复盘" in concise_answer({**answer(), "scope": "historical_session"})
    assert "撤回" in concise_answer({**answer(), "stale": True})


def safe_state():
    return {"lifecycle": "RUNNING", "telemetry": {
        "generation": 1, "connection": "CONNECTED", "source_mode": "LIVE",
        "session_type": "Race", "updated_age_s": 0.1,
        "monitor": {"sequence": 1, "source_kind": "SDK_LIVE", "status": "READY",
                    "quality": {"status": "READY", "stale": False},
                    "context": {"sim_source_mode": "FULL",
                                "player_control_state": "IN_CAR_PHYSICS", "conflicts": []},
                    "interval_invalid_for_fuel": [], "interval_unsafe_for_speech": [],
                    "telemetry": {"brake": 0.0, "steering_angle_rad": 0.0,
                                  "speed_mps": 40.0, "on_pit_road": False, "car_left_right": 1}},
        "fuel": {"status": "READY", "advisor_only": True, "estimate_only": True,
                 "executable": False, "current_fuel_l": 5.0, "estimated_laps_remaining": 2.0},
    }}


def test_optional_low_fuel_fact_requires_continuous_safe_progress_and_cooldown():
    value, policy = safe_state(), FuelVoicePolicy()
    assert safe_fuel_snapshot(value)
    assert policy.candidate(value, 0) is None
    value["telemetry"]["monitor"]["sequence"] = 2
    assert "燃油偏低" in policy.candidate(value, 2.1)
    policy.mark_spoken(2.1)
    value["telemetry"]["monitor"]["sequence"] = 3
    assert policy.candidate(value, 2.2) is None
    assert policy.candidate(value, 100) is None  # Frozen sequence cannot become fresh.


@pytest.mark.parametrize("field,bad", [("brake", 0.3), ("steering_angle_rad", 0.2),
                                       ("car_left_right", 2), ("speed_mps", None),
                                       ("on_pit_road", True)])
def test_auto_fuel_is_silent_in_high_workload_or_unknown_context(field, bad):
    value = safe_state()
    value["telemetry"]["monitor"]["telemetry"][field] = bad
    assert not safe_fuel_snapshot(value)


def test_auto_fuel_rejects_spectator_synthetic_stale_or_missing_safety():
    for mutate in (
        lambda t: t.update(source_mode="SYNTHETIC_DEMO"),
        lambda t: t.update(updated_age_s=3),
        lambda t: t["monitor"]["context"].update(player_control_state="SPECTATOR"),
        lambda t: t["monitor"].pop("interval_unsafe_for_speech"),
    ):
        value = safe_state()
        mutate(value["telemetry"])
        assert not safe_fuel_snapshot(value)

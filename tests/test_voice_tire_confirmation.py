"""Invented service and fake microphone/playback; no game, provider or hearing claims."""

from __future__ import annotations

import copy
import time
from dataclasses import replace

import pytest
from test_live_tire_age import Rig
from test_voice_service import Audio, Input, Speech, Store, answer, wait_for

from iracing_ai_engineer.live_app import AppState
from iracing_ai_engineer.live_tire_age import confirmation_binding
from iracing_ai_engineer.voice_service import VoiceService
from iracing_ai_engineer.voice_tire_confirmation import (
    LABELS,
    tire_voice_acknowledged,
    tire_voice_intent,
)


@pytest.mark.parametrize(("phrase", "kind"), [
    ("记录四胎已换新。", "FULL_NEW_SET"), ("记录四条轮胎已换新", "FULL_NEW_SET"),
    (" 记录本次未换胎！ ", "NO_TIRE_CHANGE"), ("记录部分换胎", "PARTIAL_OR_UNKNOWN"),
    ("记录换胎情况不确定", "PARTIAL_OR_UNKNOWN"), ("确认记录", "CONFIRM"),
    ("取消记录", "CANCEL"), ("确认换胎", None), ("记录四胎已换新吗", None),
    ("记录四胎已换新？", None), ("不要记录四胎已换新", None), ("确认记录并进站", None),
    ("如果记录本次未换胎", None), ("我是不是该换胎了", None), (True, None),
])
def test_exact_assertion_vocabulary_never_guesses_intent(phrase, kind):
    assert tire_voice_intent(phrase) == kind


class VoiceRig:
    def __init__(self):
        self.rig, self.audio, self.speech = Rig(), Audio(), Speech()
        self.state = AppState()
        self.state.connection("CONNECTED")
        self.rig.park()
        self.publish()
        self.text, self.calls, self.submitted = "", [], []
        self.auto_ack, self.reply = True, None
        self.speech.recognize = lambda *_a, **_kw: {"text": self.text,
                                                  "confidence": self.speech.confidence}
        self.voice = VoiceService(self.snapshot, self.submit, Store(True),
            audio=self.audio, speech=self.speech, input_factory=Input, tire_confirm=self.confirm)
        self.voice.start()
        wait_for(lambda: self.voice.snapshot()["status"] == "READY")

    def publish(self):
        value = self.rig.value
        self.state.publish(value["monitor"], {}, None, "Race", tire_age=value["tire_age"])

    def change(self, **kwargs):
        self.rig.step(**kwargs)
        self.publish()

    def snapshot(self):
        return {"lifecycle": "RUNNING", "telemetry": self.state.snapshot(),
                "engineer": {"instance_id": "synthetic", "answer": self.reply}}

    def submit(self, text, scope):
        self.submitted.append((text, scope))
        self.reply = answer()
        return 202, {}

    def confirm(self, kind, **kwargs):
        ticket = self.state.confirm_tire_service(kind, **kwargs)
        self.calls.append(ticket)
        if self.auto_ack:
            self.ack()
        return ticket

    def ack(self, *, reject=False):
        command = self.state.take_tire_confirmation(self.state.generation)
        self.rig.step()
        receipt = None if reject else self.rig.tracker.confirm(command)
        self.state.complete_tire_confirmation(self.state.generation, receipt)
        self.change()

    def say(self, text, *, wait=True):
        self.text = text
        self.audio.entered.clear()
        self.voice.press()
        assert self.audio.entered.wait(1)
        self.voice.release()
        if wait:
            wait_for(lambda: self.voice.snapshot()["status"] == "READY")

    def draft(self, text="记录四胎已换新"):
        self.say(text)
        assert self.voice._tire_review is not None and self.voice._tire_review.armed
        assert self.calls == [] and self.submitted == []


@pytest.fixture
def vr():
    rig = VoiceRig()
    try:
        yield rig
    finally:
        rig.voice.close()


@pytest.mark.parametrize(("phrase", "kind"), [
    ("记录四胎已换新", "FULL_NEW_SET"), ("记录本次未换胎", "NO_TIRE_CHANGE"),
    ("记录换胎情况不确定", "PARTIAL_OR_UNKNOWN"),
])
def test_two_ptt_utterances_real_mailbox_receipt_and_no_model(vr, phrase, kind):
    vr.draft(phrase)
    assert vr.state.snapshot()["tire_confirmation_status"] == "IDLE"
    vr.say("确认记录")
    assert len(vr.calls) == 1 and vr.calls[0]["command"]["kind"] == kind
    assert tire_voice_acknowledged(vr.snapshot(), vr.calls[0])
    assert any("已记录你的确认" in s and LABELS[kind] in s for s in vr.speech.synthesis)
    assert not vr.submitted and vr.voice._tire_review is None
    vr.rig.exit()
    vr.publish()
    observed = vr.state.snapshot()["tire_age"]
    assert (observed["origin"] is not None) is (kind == "FULL_NEW_SET")
    vr.say("确认记录")
    assert len(vr.calls) == 1  # No duplicate or post-exit assertion.


@pytest.mark.parametrize("fault", ["expired", "cancel", "stop", "question", "confidence",
                                   "service", "move", "missing", "set", "visit", "gap"])
def test_draft_does_not_survive_cancellation_or_changed_context(vr, fault):
    vr.draft()
    if fault == "expired":
        vr.voice._tire_review = replace(vr.voice._tire_review, expires_at=time.monotonic() - 1)
    elif fault == "cancel":
        vr.say("取消记录")
    elif fault == "stop":
        vr.voice.stop()
    elif fault == "question":
        vr.say("还有多少油")
    elif fault == "confidence":
        vr.speech.confidence = .4
        vr.say("确认记录")
        vr.speech.confidence = .9
    elif fault in ("service", "move", "missing"):
        vr.rig.step(**{"service": {"PitstopActive": True}, "move": {"Speed": 1.},
                       "missing": {"PitstopActive": None}}[fault])
        # Even recovery before the next UI publication cannot resurrect a draft.
        vr.rig.step(PitstopActive=False, Speed=0.)
        vr.publish()
    elif fault == "set":
        vr.change(TireSetsUsed=2)
    elif fault == "visit":
        vr.rig.exit()
        vr.rig.park()
        vr.publish()
    elif fault == "gap":
        vr.change(tick_step=20)
    vr.say("确认记录")
    assert vr.calls == []
    assert vr.submitted == ([("还有多少油", "live")] if fault == "question" else [])
    assert not any("已记录你的确认" in s for s in vr.speech.synthesis)


def test_partial_readback_is_not_armed(vr):
    vr.audio.block_speech = True
    vr.say("记录四胎已换新", wait=False)
    assert vr.audio.speaking.wait(1)
    assert not vr.voice._tire_review.armed
    vr.audio.block_speech = False
    vr.say("确认记录")  # Press interrupts the first prompt before software completion.
    assert not vr.calls and not vr.submitted and vr.voice._tire_review is None


def test_state_changes_during_recognition_or_atomic_callback_are_refused(vr):
    vr.draft()
    original = vr.speech.recognize
    def changed(*args, **kwargs):
        vr.change(PitstopActive=True)
        vr.change(PitstopActive=False)
        return original(*args, **kwargs)
    vr.speech.recognize = changed
    vr.say("确认记录")
    assert not vr.calls
    vr.speech.recognize = original
    vr.draft()
    def at_commit(kind, **kwargs):
        vr.change(Speed=1.)
        return vr.confirm(kind, **kwargs)
    vr.voice._tire_confirm = at_commit
    vr.say("确认记录")
    assert not vr.calls and not vr.submitted


@pytest.mark.parametrize("outcome", ["pending", "rejected", "wrong_receipt", "disconnected"])
def test_queue_or_unrelated_receipt_cannot_be_announced_as_recorded(vr, monkeypatch, outcome):
    from iracing_ai_engineer import voice_service
    monkeypatch.setattr(voice_service, "ACK_SECONDS", .2)
    vr.auto_ack = False
    vr.draft()
    vr.say("确认记录", wait=False)
    wait_for(lambda: len(vr.calls) == 1)
    if outcome == "rejected":
        vr.ack(reject=True)
    elif outcome == "wrong_receipt":
        command = vr.state.take_tire_confirmation(vr.state.generation)
        vr.rig.step()
        receipt = vr.rig.tracker.confirm(replace(command, kind="PARTIAL_OR_UNKNOWN"))
        vr.state.complete_tire_confirmation(vr.state.generation, receipt)
        vr.change()
    elif outcome == "disconnected":
        vr.state.connection("DISCONNECTED")
    wait_for(lambda: vr.voice.snapshot()["status"] == "READY")
    assert not any("已记录你的确认" in s for s in vr.speech.synthesis)
    assert any("尚未核实" in s for s in vr.speech.synthesis)
    assert not vr.submitted


def test_confirmation_binding_and_ack_expire_on_source_or_readiness_changes(vr):
    vr.draft()
    vr.say("确认记录")
    ticket = vr.calls[0]
    good = vr.snapshot()
    assert tire_voice_acknowledged(good, ticket)
    for key, val in (("generation", True), ("source_mode", "REPLAY"),
                     ("updated_age_s", .76), ("connection", "DISCONNECTED")):
        bad = copy.deepcopy(good)
        bad["telemetry"][key] = val
        assert confirmation_binding(bad["telemetry"]) is None
        assert not tire_voice_acknowledged(bad, ticket)


def test_atomic_mailbox_binding_is_typed_and_session_scoped(vr):
    binding = confirmation_binding(vr.state.snapshot())
    wrongs = (list(binding), (True, *binding[1:]), (*binding[:4], binding[4] + 1, *binding[5:]))
    for wrong in wrongs:
        with pytest.raises(ValueError):
            vr.state.confirm_tire_service("FULL_NEW_SET", expected_binding=wrong)
    assert vr.state.snapshot()["tire_confirmation_status"] == "IDLE"


@pytest.mark.parametrize("phrase", ["记录四条轮胎已经换心。", "记录患胎情况不确定。",
                                   "确认记录？", "记录四胎已换新吗"])
def test_misrecognized_record_is_rejected_locally_not_guessed_or_sent_to_cloud(vr, phrase):
    vr.draft()
    vr.say(phrase)
    assert not vr.calls and not vr.submitted and vr.voice._tire_review is None
    assert "未识别到支持的记录口令" in vr.speech.synthesis[-1]


def test_frozen_synthetic_check_exercises_actual_owner_without_hardware():
    from iracing_ai_engineer.synthetic_tire_voice import run_synthetic_tire_voice
    assert run_synthetic_tire_voice() == {
        "id": "SYNTHETIC_TIRE_VOICE_CONFIRMATION", "status": "PASS"}


@pytest.mark.parametrize("action", ["configure", "bind", "input_error", "muted"])
def test_new_configuration_binding_or_known_muted_output_cannot_arm_review(vr, action):
    vr.draft()
    if action == "configure":
        vr.voice.configure(vr.voice.snapshot()["settings"])
        wait_for(lambda: not vr.voice._pending_configurations)
    elif action == "bind":
        vr.voice.bind()
        wait_for(lambda: vr.voice.snapshot()["binding_active"])
        vr.voice.cancel_bind()
    elif action == "input_error":
        vr.voice._input_error("INPUT_DEVICE_CHANGED")
    else:
        vr.voice._settings["volume"] = 0
    vr.say("确认记录")
    assert not vr.calls and not vr.submitted and vr.voice._tire_review is None


def test_request_cannot_start_on_track_then_arm_after_parking(vr):
    vr.change(OnPitRoad=False, PlayerCarInPitStall=False, Speed=30.)
    original = vr.speech.recognize
    def park_after_recording(*args, **kwargs):
        vr.rig.park()
        vr.publish()
        return original(*args, **kwargs)
    vr.speech.recognize = park_after_recording
    vr.say("记录四胎已换新")
    assert not vr.calls and not vr.submitted and vr.voice._tire_review is None


def test_cancellation_after_queue_never_reports_success_or_repeats_assertion(vr):
    vr.auto_ack = False
    vr.draft()
    vr.say("确认记录", wait=False)
    wait_for(lambda: len(vr.calls) == 1)
    vr.voice.stop()
    vr.ack()  # A submitted assertion is not undone by stopping the speech worker.
    wait_for(lambda: vr.voice.snapshot()["status"] == "READY")
    assert tire_voice_acknowledged(vr.snapshot(), vr.calls[0])
    assert not any("已记录你的确认" in s for s in vr.speech.synthesis)
    vr.say("确认记录")
    assert len(vr.calls) == 1 and not vr.submitted


def test_acknowledgement_is_withdrawn_if_context_changes_during_synthesis(vr):
    original = vr.speech.synthesize
    def changed(text, **kwargs):
        if "已记录你的确认" in text:
            vr.change(PitstopActive=True)
        return original(text, **kwargs)
    vr.speech.synthesize = changed
    vr.draft()
    played_before = len(vr.audio.plays)
    vr.say("确认记录")
    assert len(vr.calls) == 1
    assert len(vr.audio.plays) == played_before + 2  # Only PTT earcons; no stale success speech.
    assert not tire_voice_acknowledged(vr.snapshot(), vr.calls[0])

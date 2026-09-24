"""Synthetic diagnostics never open an audio endpoint or an external API."""

from __future__ import annotations

import io
import sys
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from iracing_ai_engineer import desktop_app
from iracing_ai_engineer import voice_diagnostics as diagnostics


def wav(rate=8000, channels=1, seconds=0.1, width=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(bytes(round(rate * seconds) * channels * width))
    return output.getvalue()


def test_synthetic_resample_is_mono_16k_pcm_in_memory():
    result = diagnostics.wav_to_pcm(wav(channels=2))
    assert len(result) == 3200
    assert np.frombuffer(result, dtype="<i2").max() == 0


@pytest.mark.parametrize("kwargs", [{"width": 1}, {"seconds": 13}, {"seconds": 0}])
def test_invalid_synthetic_wav_fails(kwargs):
    with pytest.raises(ValueError):
        diagnostics.wav_to_pcm(wav(**kwargs))


@pytest.mark.parametrize("accepted", [True, False])
def test_voice_check_closes_local_worker_without_starting_io(monkeypatch, accepted):
    calls = []

    class Speech:
        def synthesize(self, text):
            calls.append("synthesize")
            self.text = text
            return wav()

        def recognize(self, pcm, culture):
            calls.append("recognize")
            assert culture == "zh-CN" and pcm
            return {"text": self.text, "confidence": 0.9 if accepted else 0.1}

        def close(self):
            calls.append("close")

    monkeypatch.setitem(sys.modules, "pygame", SimpleNamespace(joystick=object()))
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(RawInputStream=object()))
    monkeypatch.setitem(sys.modules, "iracing_ai_engineer.voice_speech",
                        SimpleNamespace(LocalSpeech=Speech))
    monkeypatch.setattr(diagnostics, "_verify_model", lambda: calls.append("verify"))
    result = diagnostics.run_voice_self_test()
    assert calls == ["verify", *(["synthesize", "recognize"] * (6 if accepted else 1)), "close"]
    assert all(check["status"] == "PASS" for check in result) is accepted


def test_voice_cli_is_explicit_and_preserves_synthetic_provenance(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_app, "run_self_test", lambda: {
        "status": "PASS", "source_kind": "SYNTHETIC", "checks": [],
    })
    monkeypatch.setattr(diagnostics, "run_voice_self_test", lambda: [
        {"id": "SYNTHETIC_VOICE_RUNTIME", "status": "FAIL"},
    ])
    assert desktop_app.main(["--voice-self-test"]) == 2
    assert desktop_app.main(["--self-test", "--voice-self-test"]) == 1
